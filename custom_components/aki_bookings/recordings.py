"""Pure rules of the recording pipeline: the take journal, matching files to bookings, when it is
safe to upload, and the download page. No Home Assistant imports.

Nothing here holds client data: a register entry is id, ref, times, the opt-in and file names.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

from .planner import Booking

TAKE_TOLERANCE_S = 10  # camera clocks are corrected hourly to within 2 s of the bridge
CAMERA_LEAD_MIN = 60  # cameras may start uploading only this long before the next code window
CAMERA_STOP_MIN = 18  # ...and must be back in Remote mode 15 min before it; cancelling takes a while
JOURNAL_KEEP_DAYS = 14
CONSOLE = "console"

# Register states
ST_WAITING = "waiting"  # session over, sources not inspected yet
ST_UPLOADING = "uploading"  # opt-in, files assigned, upload in progress or pending
ST_READY = "ready"  # link reported; the prefix is deleted at expires_at
ST_FAILED = "failed"  # reported failed; waits for the operator
ST_LEFT = "left"  # no opt-in, files still on the sources; waits for the operator
ST_DONE = "done"  # nothing more to do (empty, nothing recorded, expired, cancelled)

F_PENDING, F_SENT, F_FAILED = "pending", "sent", "failed"

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_CAPTURE = re.compile(r"_(\d{4})-(\d{2})-(\d{2})_\d+_(\d{2})-(\d{2})-(\d{2})$")


def safe_name(text: str) -> str:
    """A name the bridges accept as `dest`: letters, digits, '.', '_' and '-'."""
    return _SAFE.sub("_", text).strip("_") or "file"


def ts(dt: datetime) -> float:
    return dt.timestamp()


# ---- take journal -----------------------------------------------------------------------------

def open_take(journal: list[dict], now: datetime, booking_id: str | None) -> None:
    close_take(journal, now)
    journal.append({"start": ts(now), "stop": None, "booking_id": booking_id})


def close_take(journal: list[dict], now: datetime) -> None:
    if journal and journal[-1].get("stop") is None:
        journal[-1]["stop"] = ts(now)


def prune_journal(journal: list[dict], now: datetime) -> list[dict]:
    limit = ts(now - timedelta(days=JOURNAL_KEEP_DAYS))
    return [t for t in journal if (t.get("stop") or ts(now)) >= limit]


def takes_of(journal: list[dict], booking_id: str) -> list[dict]:
    return [t for t in journal if t.get("booking_id") == booking_id]


# ---- files ------------------------------------------------------------------------------------

def capture_ts(path: str, mtime_ts: float | None) -> float | None:
    """When a console capture started. Its folder is '4K CAPTURE_<date>_<n>_<HH-MM-SS>' in UTC."""
    folder = path.split("/", 1)[0]
    m = _CAPTURE.search(folder)
    if m:
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
        return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()
    return mtime_ts


def camera_file(camera_id: str, f: dict) -> dict | None:
    if not f.get("name") or f.get("recorded_ts") is None:
        return None
    return {
        "source": camera_id, "name": f["name"], "folder": f.get("folder") or "",
        "dest": safe_name(f"{camera_id}_{f['name']}"), "size": int(f.get("size") or 0), "ts": float(f["recorded_ts"]), "state": F_PENDING,
    }


def console_file(f: dict) -> dict | None:
    when = capture_ts(f.get("path") or "", f.get("mtime_ts"))
    if not f.get("path") or when is None:
        return None
    parts = f["path"].split("/")
    stamp = datetime.fromtimestamp(when, timezone.utc).strftime("%H%M%S")
    return {
        "source": CONSOLE, "path": f["path"], "dest": safe_name(f"iso_{stamp}_{'_'.join(parts[1:]) or parts[0]}"),
        "size": int(f.get("size") or 0), "ts": float(when), "state": F_PENDING,
    }


def file_key(f: dict) -> tuple:
    return (f["source"], f.get("folder", ""), f.get("name") or f.get("path"))


def assign_files(files: list[dict], entries: list[dict], journal: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
    """Give each file on the sources to the booking it belongs to.

    1. A file recorded during a take (recording mode on, ±10 s) goes to that take's booking.
    2. Otherwise a file recorded inside a booking's own window (the button on the camera body) goes to it.
    3. Anything else belongs to nobody and is never uploaded: it may be the previous client's footage.
    """
    by_id = {e["id"]: e for e in entries}
    out: dict[str, list[dict]] = {e["id"]: [] for e in entries}
    unassigned: list[dict] = []
    for f in files:
        owner = None
        for take in journal:
            stop = take.get("stop")
            if stop is None:
                continue
            if take["start"] - TAKE_TOLERANCE_S <= f["ts"] <= stop + TAKE_TOLERANCE_S:
                owner = take.get("booking_id")
                break
        else:
            for e in entries:
                if e["window_from"] <= f["ts"] <= e["window_to"]:
                    owner = e["id"]
                    break
        if owner in by_id:
            out[owner].append(f)
        else:
            unassigned.append(f)
    return out, unassigned


def merge_manifest(old: list[dict], new: list[dict]) -> list[dict]:
    """Keep what is already sent; files gone from the sources drop out unless they were sent."""
    sent = {file_key(f): f for f in old if f.get("state") == F_SENT}
    merged = [sent.pop(file_key(f), None) or f for f in new]
    return merged + list(sent.values())


def summary(files: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in files:
        s = out.setdefault(f["source"], {"count": 0, "bytes": 0})
        s["count"] += 1
        s["bytes"] += f.get("size", 0)
    return out


# ---- when uploading is allowed ------------------------------------------------------------------

def entry_from_booking(b: Booking) -> dict:
    return {
        "id": b.id, "ref": b.ref, "locale": b.guest_locale, "start": ts(b.start), "end": ts(b.end),
        "window_from": ts(b.valid_from or b.start), "window_to": ts(b.valid_to or b.end),
        "state": ST_WAITING, "files": [],
    }


def booking_at(bookings: list[Booking], now: datetime) -> Booking | None:
    """The booking whose client may be in the studio now (its code window is open)."""
    for b in bookings:
        if b.confirmed and (b.valid_from or b.start) <= now < (b.valid_to or b.end):
            return b
    return None


def next_window(bookings: list[Booking], now: datetime) -> datetime | None:
    starts = [(b.valid_from or b.start) for b in bookings if b.confirmed and (b.valid_from or b.start) > now]
    return min(starts) if starts else None


def camera_upload_allowed(bookings: list[Booking], now: datetime, guard_on: bool, local_date) -> bool:
    """Cameras lose remote control while they upload, so only after the day's last session,
    with nobody in the studio and at least an hour before the next code window."""
    if guard_on or booking_at(bookings, now):
        return False
    upcoming = next_window(bookings, now)
    if upcoming is None:
        return True
    if upcoming - now < timedelta(minutes=CAMERA_LEAD_MIN):
        return False
    return not any(b.confirmed and b.start > now and local_date(b.start) == local_date(now) for b in bookings)


def camera_upload_must_stop(bookings: list[Booking], now: datetime, guard_on: bool) -> bool:
    if guard_on or booking_at(bookings, now):
        return True
    upcoming = next_window(bookings, now)
    return upcoming is not None and upcoming - now <= timedelta(minutes=CAMERA_STOP_MIN)


def console_upload_allowed(bookings: list[Booking], now: datetime, guard_on: bool) -> bool:
    """The ISO upload only loads the uplink and the console disk: any time nobody is in."""
    return not guard_on and booking_at(bookings, now) is None


# ---- download page ------------------------------------------------------------------------------

def human_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1000
    return f"{n} B"


TEXT = {
    "pl": {"title": "Nagrania z sesji", "file": "Plik", "source": "Źródło", "size": "Rozmiar", "camera": "kamera", "console": "konsola (ISO)",
           "until": "Linki działają do", "after": "Po tym terminie pliki są usuwane.", "list": "Lista linków dla menedżera pobierania", "total": "Razem"},
    "en": {"title": "Session recordings", "file": "File", "source": "Source", "size": "Size", "camera": "camera", "console": "console (ISO)",
           "until": "The links work until", "after": "After that the files are deleted.", "list": "Link list for a download manager", "total": "Total"},
}


def render_index(ref: str, locale: str, files: list[dict], links: dict[str, str], list_url: str, expires_text: str) -> str:
    """The page the client opens: one signed link per file. `links` maps dest -> url."""
    t = TEXT["en" if locale == "en" else "pl"]
    rows = []
    for f in sorted(files, key=lambda f: (f["source"] == CONSOLE, f["source"], f["ts"], f["dest"])):
        source = t["console"] if f["source"] == CONSOLE else f"{t['camera']} {html.escape(f['source'])}"
        rows.append(
            f'<tr><td><a href="{html.escape(links[f["dest"]], quote=True)}" download>{html.escape(f["dest"])}</a></td>'
            f"<td>{source}</td><td class=n>{human_size(f['size'])}</td></tr>"
        )
    total = human_size(sum(f["size"] for f in files))
    return f"""<!doctype html>
<html lang="{'en' if locale == 'en' else 'pl'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, nofollow">
<title>{t['title']} {html.escape(ref)}</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px;background:#f6f6f4;color:#1c1c1a}}
main{{max-width:760px;margin:0 auto}}h1{{font-size:1.4rem;margin:0 0 4px}}p{{margin:4px 0 16px;color:#55554f}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}}
th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid #e6e6e0;word-break:break-all}}th{{font-size:.85rem;color:#55554f}}
.n{{text-align:right;white-space:nowrap;word-break:normal}}a{{color:#0b57d0}}tfoot td{{font-weight:600;border-bottom:0}}
@media (prefers-color-scheme:dark){{body{{background:#161615;color:#ecece6}}table{{background:#222220}}th,td{{border-color:#33332f}}p,th{{color:#a8a8a0}}a{{color:#8ab4f8}}}}
</style></head><body><main>
<h1>{t['title']} {html.escape(ref)}</h1>
<p>{t['until']} {html.escape(expires_text)}. {t['after']}</p>
<table><thead><tr><th>{t['file']}</th><th>{t['source']}</th><th class=n>{t['size']}</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody><tfoot><tr><td>{t['total']}</td><td></td><td class=n>{total}</td></tr></tfoot></table>
<p style="margin-top:16px"><a href="{html.escape(list_url, quote=True)}">{t['list']}</a></p>
</main></body></html>
"""
