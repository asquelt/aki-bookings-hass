"""The few S3 calls the recording pipeline needs (Cloudflare R2 or any S3-compatible store),
signed with AWS Signature V4 by hand so the integration has no extra requirements.

The bridges upload the big files. HA only signs download links, writes the small index page,
lists a prefix to verify sizes and deletes it when the download time is over.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

import aiohttp

MAX_PRESIGN_S = 7 * 24 * 3600  # the S3 limit for signed links
TIMEOUT = aiohttp.ClientTimeout(total=60)
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


class S3Error(Exception):
    """A storage call failed. The message never contains a signed link."""


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _q(text: str, safe: str = "") -> str:
    return quote(text, safe=safe + "-_.~")


class S3:
    def __init__(self, session: aiohttp.ClientSession | None, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str = "auto") -> None:
        parsed = urlparse(endpoint.rstrip("/"))
        if parsed.scheme != "https" or not parsed.netloc:
            raise S3Error("the storage endpoint must be an https:// address")
        self._session = session
        self._origin = f"https://{parsed.netloc}"
        self._host = parsed.netloc
        self._bucket = bucket
        self._access = access_key
        self._secret = secret_key
        self._region = region

    # ---- signing ------------------------------------------------------------------------------

    def _path(self, key: str = "") -> str:
        return f"/{_q(self._bucket)}" + (f"/{_q(key, '/')}" if key else "")

    def _scope(self, day: str) -> str:
        return f"{day}/{self._region}/s3/aws4_request"

    def _signature(self, day: str, string_to_sign: str) -> str:
        key = _sign(_sign(_sign(_sign(("AWS4" + self._secret).encode(), day), self._region), "s3"), "aws4_request")
        return hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    def _string_to_sign(self, stamp: str, canonical: str) -> str:
        return "\n".join(["AWS4-HMAC-SHA256", stamp, self._scope(stamp[:8]), hashlib.sha256(canonical.encode()).hexdigest()])

    def presign_get(self, key: str, expires_s: int, now: datetime | None = None, content_type: str | None = None) -> str:
        """A link that downloads one object without credentials for `expires_s` seconds."""
        now = now or datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        params = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self._access}/{self._scope(stamp[:8])}",
            "X-Amz-Date": stamp,
            "X-Amz-Expires": str(max(1, min(int(expires_s), MAX_PRESIGN_S))),
            "X-Amz-SignedHeaders": "host",
        }
        if content_type:
            params["response-content-type"] = content_type
        query = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(params.items()))
        canonical = "\n".join(["GET", self._path(key), query, f"host:{self._host}\n", "host", "UNSIGNED-PAYLOAD"])
        return f"{self._origin}{self._path(key)}?{query}&X-Amz-Signature={self._signature(stamp[:8], self._string_to_sign(stamp, canonical))}"

    def _headers(self, method: str, path: str, query: dict[str, str], body: bytes, extra: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA
        headers = {"host": self._host, "x-amz-content-sha256": payload, "x-amz-date": stamp, **{k.lower(): v for k, v in (extra or {}).items()}}
        names = ";".join(sorted(headers))
        qs = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(query.items()))
        canonical = "\n".join([method, path, qs, "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers)), names, payload])
        auth = (
            f"AWS4-HMAC-SHA256 Credential={self._access}/{self._scope(stamp[:8])}, SignedHeaders={names}, "
            f"Signature={self._signature(stamp[:8], self._string_to_sign(stamp, canonical))}"
        )
        send = {k: v for k, v in headers.items() if k != "host"}
        send["Authorization"] = auth
        return f"{self._origin}{path}" + (f"?{qs}" if qs else ""), send

    async def _request(self, method: str, key: str = "", query: dict[str, str] | None = None, body: bytes = b"", extra: dict[str, str] | None = None) -> str:
        url, headers = self._headers(method, self._path(key), query or {}, body, extra)
        try:
            async with self._session.request(method, url, headers=headers, data=body or None, timeout=TIMEOUT) as resp:
                text = await resp.text()
                if resp.status >= 300:
                    raise S3Error(f"storage answered HTTP {resp.status} to {method} {key or 'list'}: {text[:200]}")
                return text
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise S3Error(f"storage unreachable: {type(err).__name__}: {err}") from err

    # ---- calls --------------------------------------------------------------------------------

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        await self._request("PUT", key, body=body, extra={"Content-Type": content_type})

    async def list(self, prefix: str) -> dict[str, int]:
        """key -> size for every object under the prefix."""
        out: dict[str, int] = {}
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            root = ElementTree.fromstring(await self._request("GET", query=query))
            ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
            for item in root.findall(f"{ns}Contents"):
                out[item.findtext(f"{ns}Key") or ""] = int(item.findtext(f"{ns}Size") or 0)
            token = root.findtext(f"{ns}NextContinuationToken")
            if not token or root.findtext(f"{ns}IsTruncated") != "true":
                return out

    async def delete(self, key: str) -> None:
        await self._request("DELETE", key)

    async def delete_prefix(self, prefix: str) -> int:
        """Delete everything under the prefix; this is the only way to revoke the signed links."""
        keys = await self.list(prefix)
        for key in keys:
            await self.delete(key)
        return len(keys)
