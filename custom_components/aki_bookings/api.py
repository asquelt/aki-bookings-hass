"""Client for the booking system's Home Assistant API (/api/ha/v1). Every call is made by HA."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .const import API_PATH

TIMEOUT = aiohttp.ClientTimeout(total=30)


class ApiError(Exception):
    """Any failure talking to the booking system. `status` is the HTTP status, 0 when there was no answer."""

    def __init__(self, message: str, status: int = 0, code: str | None = None, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details

    @property
    def transient(self) -> bool:
        """Worth sending the same request again later."""
        return self.status == 0 or self.status == 429 or self.status >= 500


class ApiAuthError(ApiError):
    """401 from the API itself: the sha_ token is missing, unknown or revoked."""


class ApiGateError(ApiError):
    """401 from the staging site gate (an HTML page): X-Site-Access-Token is missing or wrong."""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BookingApi:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str, site_token: str | None = None) -> None:
        self._session = session
        self._base = url.rstrip("/") + API_PATH
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if site_token:
            self._headers["X-Site-Access-Token"] = site_token

    async def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> Any:
        try:
            async with self._session.request(
                method, self._base + path, headers=self._headers, params=params, json=body, timeout=TIMEOUT
            ) as resp:
                status = resp.status
                text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError) as err:
            raise ApiError(f"no answer from the booking system: {type(err).__name__}: {err}") from err
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None  # e.g. the staging gate's HTML page

        if status == 401 and payload is None:
            raise ApiGateError("the site gate refused the request (X-Site-Access-Token)", status)
        if not isinstance(payload, dict):
            raise ApiError(f"unexpected answer (HTTP {status}, not JSON)", status)
        if status < 300 and "data" in payload:
            return payload["data"]
        error = payload.get("error") or {}
        message = f"{error.get('code') or status}: {error.get('message') or 'no message'}"
        cls = ApiAuthError if status == 401 else ApiError
        raise cls(message, status, error.get("code"), error.get("details"))

    async def health(self) -> dict:
        return await self._request("GET", "/health")

    async def bookings(self, start: datetime, end: datetime) -> list[dict]:
        data = await self._request("GET", "/bookings", params={"from": iso(start), "to": iso(end)})
        if not isinstance(data, list):
            raise ApiError("unexpected answer: the bookings feed is not a list", 200)
        return data

    async def report_code(self, booking_id: str, code_id: str, status: str, error: str | None = None, at: str | None = None) -> dict:
        body: dict[str, Any] = {"code_id": code_id, "status": status}
        if error:
            body["error"] = error[:500]
        if at:
            body["at"] = at
        return await self._request("PATCH", f"/bookings/{booking_id}/access-code", body=body)

    async def set_recording_opt_in(self, booking_id: str, opt_in: bool) -> dict:
        return await self._request("PUT", f"/bookings/{booking_id}/recording/opt-in", body={"opt_in": opt_in})

    async def report_recording(
        self, booking_id: str, status: str, url: str | None = None, expires_at: str | None = None, error: str | None = None
    ) -> dict:
        body: dict[str, Any] = {"status": status}
        if url:
            body["url"] = url
        if expires_at:
            body["expires_at"] = expires_at
        if error:
            body["error"] = error[:500]
        return await self._request("PATCH", f"/bookings/{booking_id}/recording", body=body)
