"""The recording pipeline: take journal, nightly upload through the camera bridges, download page,
reports to the booking system, deletion after the download time.

HA moves no media itself. It keeps the journal, decides when uploading is safe, tells the
a74_bridge integration what to send where, signs the links and reports the state.
Only bookings with the client's opt-in are ever uploaded.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import recordings as rec
from .api import ApiError, iso
from .const import (
    DEFAULT_DOWNLOAD_DAYS,
    DEFAULT_GUARD,
    DEFAULT_S3_ROOT,
    DOMAIN,
    EVENT_REC_DELETED,
    EVENT_REC_EMPTY,
    EVENT_REC_FAILED,
    EVENT_REC_LEFT,
    EVENT_REC_NONE,
    EVENT_REC_READY,
    EVENT_REC_UNASSIGNED,
    EVENT_SRC_DELETED,
    EVENT_SRC_FAILED,
    OPT_DELETE_SOURCES,
    OPT_DOWNLOAD_DAYS,
    OPT_GUARD,
    OPT_REC_ENABLED,
    OPT_S3_BUCKET,
    OPT_S3_ENDPOINT,
    OPT_S3_KEY,
    OPT_S3_ROOT,
    OPT_S3_SECRET,
)
from .planner import parse_bookings
from .s3 import S3, S3Error

_LOGGER = logging.getLogger(__name__)

BRIDGE = "a74_bridge"
TICK = timedelta(seconds=60)
WIDE_BACK = timedelta(days=7)
WIDE_MAX_AGE = timedelta(minutes=15)
ATTEMPTS_PER_NIGHT = 3
STALLED_NIGHTS = 3
JOB_GRACE_S = 45  # a job needs a moment to show up in the bridge's status after it is accepted
FINISHED = ("done", "failed", "cancelled")
KEEP_DONE = timedelta(days=8)


class RecordingManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.recordings")
        self.journal: list[dict] = []
        self.register: dict[str, dict] = {}
        self.enabled_at: float | None = None
        self.unassigned_on: str | None = None
        self.last_error: str | None = None
        self._refused = False  # the last report was refused for good (4xx), not merely lost
        self._wide_at: datetime | None = None
        self._lock = asyncio.Lock()
        self._unsubs: list[CALLBACK_TYPE] = []

    # ---- options ------------------------------------------------------------------------------

    def _opt(self, key: str, default: Any = None) -> Any:
        return self.entry.options.get(key, default)

    @property
    def enabled(self) -> bool:
        return bool(self._opt(OPT_REC_ENABLED, False))

    @property
    def guard_entity(self) -> str | None:
        return self._opt(OPT_GUARD, DEFAULT_GUARD) or None

    @property
    def guard_on(self) -> bool:
        state = self.hass.states.get(self.guard_entity) if self.guard_entity else None
        return state is not None and state.state == "on"

    @property
    def download_days(self) -> int:
        return max(1, min(7, int(self._opt(OPT_DOWNLOAD_DAYS, DEFAULT_DOWNLOAD_DAYS))))

    @property
    def delete_sources(self) -> bool:
        return bool(self._opt(OPT_DELETE_SOURCES, False))

    def _s3(self) -> S3:
        missing = [k for k in (OPT_S3_ENDPOINT, OPT_S3_BUCKET, OPT_S3_KEY, OPT_S3_SECRET) if not self._opt(k)]
        if missing:
            raise S3Error("storage is not configured: " + ", ".join(missing))
        return S3(async_get_clientsession(self.hass), self._opt(OPT_S3_ENDPOINT), self._opt(OPT_S3_BUCKET), self._opt(OPT_S3_KEY), self._opt(OPT_S3_SECRET))

    # ---- lifecycle ----------------------------------------------------------------------------

    async def async_start(self) -> None:
        data = await self._store.async_load() or {}
        self.journal = data.get("journal") or []
        self.register = data.get("register") or {}
        self.enabled_at = data.get("enabled_at")
        self.unassigned_on = data.get("unassigned_on")
        if self.enabled and self.enabled_at is None:
            self.enabled_at = dt_util.utcnow().timestamp()  # sessions that ended before this are not ours to judge
        if self.guard_entity:
            self._unsubs.append(async_track_state_change_event(self.hass, [self.guard_entity], self._guard_changed))
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, TICK))

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def _save(self) -> None:
        await self._store.async_save({
            "journal": self.journal, "register": self.register, "enabled_at": self.enabled_at, "unassigned_on": self.unassigned_on,
        })

    # ---- take journal -------------------------------------------------------------------------

    @callback
    def _guard_changed(self, event: Event) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if new is None or old is None or old.state == new.state:
            return
        now = dt_util.utcnow()
        if new.state == "on":
            booking = rec.booking_at(self.coordinator.bookings, now)
            rec.open_take(self.journal, now, booking.id if booking else None)
        elif old.state == "on":
            rec.close_take(self.journal, now)
        else:
            return
        self.journal = rec.prune_journal(self.journal, now)
        self.entry.async_create_background_task(self.hass, self._save(), f"{DOMAIN} journal")
        self.coordinator.async_update_listeners()  # the sensor's open_take follows at once

    # ---- views --------------------------------------------------------------------------------

    @property
    def active(self) -> dict | None:
        return next((e for e in self.register.values() if e.get("phase")), None)

    def takes_view(self, count: int = 50) -> list[dict]:
        """The latest takes for the sensor: when recording mode was on, and for which booking."""
        refs = {b.id: b.ref for b in self.coordinator.bookings} | {e["id"]: e["ref"] for e in self.register.values()}
        out = []
        for t in self.journal[-count:]:
            stop = t.get("stop")
            out.append({
                "start": dt_util.utc_from_timestamp(t["start"]).isoformat(timespec="seconds"),
                "stop": dt_util.utc_from_timestamp(stop).isoformat(timespec="seconds") if stop else None,
                "seconds": round(stop - t["start"]) if stop else None,
                "ref": refs.get(t.get("booking_id")),
            })
        return out

    def overview(self) -> list[dict]:
        out = []
        for e in sorted(self.register.values(), key=lambda e: e["end"]):
            if e["state"] == rec.ST_DONE and e.get("cleanup") != rec.CLEAN_PENDING:
                continue
            files = e.get("files") or []
            out.append({
                "ref": e["ref"], "state": e["state"], "phase": e.get("phase"), "files": len(files),
                "sent": sum(1 for f in files if f["state"] == rec.F_SENT),
                "bytes": sum(f.get("size", 0) for f in files),
                "expires_at": dt_util.utc_from_timestamp(e["expires_at"]).isoformat() if e.get("expires_at") else None,
                "error": e.get("error"),
                "cleanup": e.get("cleanup"),
                "on_sources": sum(1 for f in files if f.get("src") != rec.SRC_DELETED),
            })
        return out

    def find(self, booking_id: str) -> dict | None:
        return next((e for e in self.register.values() if booking_id in (e["id"], e["ref"])), None)

    # ---- the loop -----------------------------------------------------------------------------

    @callback
    def _tick(self, _now: datetime) -> None:
        if self.enabled and not self._lock.locked():
            self.entry.async_create_background_task(self.hass, self.async_run(), f"{DOMAIN} recordings")

    async def async_run(self) -> None:
        async with self._lock:
            now = dt_util.utcnow()
            try:
                await self._expire(now)
                bookings = self.coordinator.bookings
                active = self.active
                if active:
                    await self._drive(active, now, bookings)
                else:
                    await self._next_step(now, bookings)
            except Exception:  # noqa: BLE001 - one bad pass must not stop the nightly loop
                _LOGGER.exception("Recording pass failed")
                self.last_error = "internal error, see the log"
            await self._save()
            self.coordinator.async_update_listeners()

    def _today(self, now: datetime) -> str:
        return dt_util.as_local(now).date().isoformat()

    @staticmethod
    def _local_date(dt: datetime):
        return dt_util.as_local(dt).date()

    @staticmethod
    def _touched_after_start(b) -> bool:
        """A cancelled booking changed after its start: the session had most likely begun before the cancellation."""
        changed = dt_util.parse_datetime(b.updated_at) if b.updated_at else None
        return bool(changed and changed.tzinfo and changed > b.start)

    async def _refresh_register(self, now: datetime, force: bool = False) -> bool:
        """Read a week of sessions: the opt-in can change after the session, long after the booking
        left the short feed the door codes use. Only id, ref, times and the choice are kept."""
        if not force and self._wide_at and now - self._wide_at < WIDE_MAX_AGE:
            return True
        try:
            raws = await self.coordinator.api.bookings(now - WIDE_BACK, now + timedelta(hours=1))
        except ApiError as err:
            self.last_error = f"feed: {err}"
            return False
        bookings, _bad = parse_bookings(raws)
        seen = set()
        for b in bookings:
            seen.add(b.id)
            entry = self.register.get(b.id)
            if not b.confirmed and not (entry or rec.takes_of(self.journal, b.id) or self._touched_after_start(b)):
                continue  # cancelled before its start: nobody was let in, and the API answers 404 for it
            # A booking cancelled after its session began is handled like any other: the footage exists.
            if b.end > now or (self.enabled_at and b.end.timestamp() < self.enabled_at):
                continue
            if entry is None:
                entry = self.register[b.id] = rec.entry_from_booking(b)
            entry["opt_in"], entry["remote"] = b.recording_opt_in, b.recording_status
        limit = (now - KEEP_DONE).timestamp()
        for key, entry in list(self.register.items()):
            if entry.get("cleanup") == rec.CLEAN_PENDING:
                continue  # its files are still to be removed from the sources
            if entry["state"] == rec.ST_DONE and entry["end"] < limit:
                del self.register[key]
            elif key not in seen and entry["state"] in (rec.ST_WAITING, rec.ST_LEFT):
                del self.register[key]  # older than the feed reaches: nothing more will be learnt about it
        self._wide_at = now
        return True

    async def _next_step(self, now: datetime, bookings) -> None:
        guard = self.guard_on
        if not await self._refresh_register(now):
            return
        cameras_ok = rec.camera_upload_allowed(bookings, now, guard, self._local_date)
        console_ok = rec.console_upload_allowed(bookings, now, guard)
        today = self._today(now)

        if cameras_ok and any(self._inspectable(e) and e.get("inspected_on") != today for e in self.register.values()):
            if await self._refresh_register(now, force=True):  # the freshest opt-in, right before deciding
                await self._inspect(now, today)
            return  # the cameras need a few seconds to return to Remote mode after a listing

        for e in sorted(self.register.values(), key=lambda e: e["end"]):
            if e.get("cleanup") != rec.CLEAN_PENDING:
                continue
            tries = e.setdefault("tries", {})
            cams = cameras_ok and any(f["source"] != rec.CONSOLE and f.get("src") != rec.SRC_DELETED for f in e["files"])
            cons = console_ok and any(f["source"] == rec.CONSOLE and f.get("src") != rec.SRC_DELETED for f in e["files"])
            if (cams or cons) and tries.get(f"{today}-del", 0) < ATTEMPTS_PER_NIGHT:
                tries[f"{today}-del"] = tries.get(f"{today}-del", 0) + 1
                await self._cleanup(e, cams, cons, last=tries[f"{today}-del"] >= ATTEMPTS_PER_NIGHT)
                return  # the cameras need a few seconds to return to Remote mode

        for e in sorted(self.register.values(), key=lambda e: e["end"]):
            if e["state"] != rec.ST_UPLOADING:
                continue
            cam_pending = [f for f in e["files"] if f["source"] != rec.CONSOLE and f["state"] != rec.F_SENT]
            con_pending = [f for f in e["files"] if f["source"] == rec.CONSOLE and f["state"] != rec.F_SENT]
            tries = e.setdefault("tries", {})
            if not cam_pending and not con_pending:
                await self._finalize(e, now)
                return
            if cam_pending and cameras_ok and tries.get(f"{today}-cam", 0) < ATTEMPTS_PER_NIGHT:
                tries[f"{today}-cam"] = tries.get(f"{today}-cam", 0) + 1
                await self._start_cameras(e, cam_pending, now)
                return
            if not cam_pending and con_pending and console_ok and tries.get(f"{today}-con", 0) < ATTEMPTS_PER_NIGHT:
                tries[f"{today}-con"] = tries.get(f"{today}-con", 0) + 1
                await self._start_console(e, con_pending, now)
                return

    # ---- bridges ------------------------------------------------------------------------------

    async def _bridge(self, service: str, data: dict | None = None) -> dict:
        try:
            result = await self.hass.services.async_call(BRIDGE, service, data or {}, blocking=True, return_response=True)
        except Exception as err:  # noqa: BLE001 - refused by the guard, bridge down, service missing
            return {"error": str(err) or type(err).__name__}
        return result if isinstance(result, dict) else {}

    def _device_for(self, camera_id: str) -> str | None:
        for device in dr.async_get(self.hass).devices.values():
            if device.name == camera_id and any(domain == BRIDGE for domain, _ in device.identifiers):
                return device.id
        return None

    @staticmethod
    def _inspectable(e: dict) -> bool:
        """Sessions the daily look at the sources still has to sort out. One waiting for its files to be deleted is settled."""
        return e["state"] in (rec.ST_WAITING, rec.ST_LEFT, rec.ST_UPLOADING) and e.get("cleanup") != rec.CLEAN_PENDING

    async def _inspect(self, now: datetime, today: str) -> None:
        """List the cards and the console disk once and sort out every finished session."""
        complete = True
        files: list[dict] = []
        cameras = await self._bridge("media_list")
        if "error" in cameras:
            self.last_error = f"card listing: {cameras['error']}"
            return
        for camera_id, answer in cameras.items():
            if not isinstance(answer, dict) or "error" in answer:
                complete = False
                self.last_error = f"card listing on {camera_id}: {(answer or {}).get('error') if isinstance(answer, dict) else answer}"
                continue
            files += [f for f in (rec.camera_file(camera_id, raw) for raw in answer.get("files") or []) if f]
        console = await self._bridge("console_media_list")
        if "error" in console:
            if "no bridge has the console upload configured" not in str(console["error"]):
                complete = False
                self.last_error = f"console listing: {console['error']}"
        else:
            files += [f for f in (rec.console_file(raw) for raw in console.get("files") or []) if f]
        if complete:
            self.last_error = None

        candidates = [e for e in self.register.values() if self._inspectable(e)]
        assigned, unassigned = rec.assign_files(files, candidates, self.journal)
        for e in candidates:
            mine = assigned[e["id"]]
            had_takes = bool(rec.takes_of(self.journal, e["id"]))
            if e.get("opt_in") and e.get("remote") in ("none", "failed", "uploading"):
                already_sent = any(f["state"] == rec.F_SENT for f in e.get("files") or [])
                if mine or already_sent:
                    if e["state"] != rec.ST_UPLOADING:
                        if not await self._report(e, "uploading"):  # this locks the client's choice
                            if self._refused:
                                e["inspected_on"] = today  # asking again today gives the same answer
                            continue
                        e["state"], e["stalled"] = rec.ST_UPLOADING, 0
                        e["sent_before"] = sum(1 for f in e.get("files") or [] if f["state"] == rec.F_SENT)
                        if not e.get("prefix"):  # kept across a retry, so files already in the bucket are not sent twice
                            e["prefix"] = f"{self._opt(OPT_S3_ROOT, DEFAULT_S3_ROOT)}/{e['ref']}-{secrets.token_hex(8)}"
                    else:
                        sent = sum(1 for f in e["files"] if f["state"] == rec.F_SENT)
                        e["stalled"] = e.get("stalled", 0) + 1 if sent <= e.get("sent_before", 0) else 0
                        e["sent_before"] = sent
                        if e["stalled"] >= STALLED_NIGHTS:
                            await self._fail(e, f"no progress for {STALLED_NIGHTS} nights")
                            continue
                    e["files"] = rec.merge_manifest(e.get("files") or [], mine)
                elif complete:
                    # Opt-in, but nothing to send: nothing was recorded, or the client took the files.
                    if await self._report(e, "uploading") and await self._report(e, "empty"):
                        e["state"] = rec.ST_DONE
                        self._fire(EVENT_REC_EMPTY, e, had_takes=had_takes)
                    elif self._refused:
                        e["inspected_on"] = today
                    continue
                else:
                    continue
            elif e.get("remote") in ("ready", "empty") and e["state"] != rec.ST_UPLOADING:
                e["state"] = rec.ST_DONE
                continue
            elif mine:
                e["state"], e["files"] = rec.ST_LEFT, mine
                if e.get("signal_on") != today:
                    e["signal_on"] = today
                    self._fire(EVENT_REC_LEFT, e, sources=rec.summary(mine), had_takes=had_takes)
            elif complete:
                if e["state"] == rec.ST_WAITING:
                    self._fire(EVENT_REC_NONE, e, had_takes=had_takes)
                e["state"] = rec.ST_DONE
                continue
            else:
                continue
            e["inspected_on"] = today
        if unassigned and self.unassigned_on != today:
            self.unassigned_on = today
            self.hass.bus.async_fire(EVENT_REC_UNASSIGNED, {"sources": rec.summary(unassigned)})

    async def _start_cameras(self, e: dict, pending: list[dict], now: datetime) -> None:
        started = False
        by_camera: dict[str, list[dict]] = {}
        for f in pending:
            by_camera.setdefault(f["source"], []).append(f)
        for camera_id, files in by_camera.items():
            device = self._device_for(camera_id)
            if device is None:
                self.last_error = f"no a74_bridge device named {camera_id}"
                continue
            answer = await self._bridge("media_upload", {
                "device_id": device, "prefix": e["prefix"],
                "files": [{"name": f["name"], "folder": f["folder"], "dest": f["dest"]} for f in files],
            })
            result = answer.get(camera_id, answer)
            if isinstance(result, dict) and "error" in result:
                self.last_error = f"upload on {camera_id}: {result['error']}"
            else:
                started = True
        if started:
            e["phase"], e["phase_at"] = "cameras", now.timestamp()

    async def _start_console(self, e: dict, pending: list[dict], now: datetime) -> None:
        answer = await self._bridge("console_media_upload", {
            "prefix": e["prefix"], "files": [{"path": f["path"], "dest": f["dest"]} for f in pending],
        })
        errors = [v["error"] for v in [answer, *answer.values()] if isinstance(v, dict) and "error" in v]
        if errors:
            self.last_error = f"console upload: {errors[0]}"
            return
        e["phase"], e["phase_at"] = "console", now.timestamp()

    @staticmethod
    def _apply_job(e: dict, source: str | None, job: dict) -> bool:
        """Copy file states from a bridge job into the manifest. Returns True while the job runs."""
        if not isinstance(job, dict) or job.get("prefix") != e.get("prefix"):
            return False
        by_dest = {f["dest"]: f for f in e["files"] if source is None or f["source"] == source}
        for jf in job.get("files") or []:
            f = by_dest.get(jf.get("dest"))
            if f is None:
                continue
            if jf.get("state") in ("uploaded", "skipped"):
                f["state"], f["error"] = rec.F_SENT, None  # an earlier attempt's error no longer applies
            elif jf.get("state") in ("failed", "missing"):
                f["state"], f["error"] = rec.F_FAILED, jf.get("error")
        return job.get("state") not in FINISHED

    async def _drive(self, e: dict, now: datetime, bookings) -> None:
        guard = self.guard_on
        young = now.timestamp() - e.get("phase_at", 0) < JOB_GRACE_S
        if e["phase"] == "cameras":
            if rec.camera_upload_must_stop(bookings, now, guard):
                # The bridges must be back in Remote mode before the next client; files stay pending.
                await self._bridge("media_cancel")
                e["phase"] = None
                return
            jobs = await self._bridge("media_job")
            running = False
            for camera_id, job in jobs.items():
                running = self._apply_job(e, camera_id, job) or running
            if not running and not young:
                e["phase"] = None
        else:
            if not rec.console_upload_allowed(bookings, now, guard):
                await self._bridge("console_media_cancel")
                e["phase"] = None
                return
            job = await self._bridge("console_media_job")
            if not self._apply_job(e, rec.CONSOLE, job) and not young:
                e["phase"] = None

    # ---- finishing ----------------------------------------------------------------------------

    async def _report(self, e: dict, status: str, **extra: Any) -> bool:
        self._refused = False
        try:
            await self.coordinator.api.report_recording(e["id"], status, **extra)
        except ApiError as err:
            self.last_error = f"report {status} for {e['ref']}: {err}"
            self._refused = not err.transient  # the same request would be refused again
            if err.status == 404:
                e["state"] = rec.ST_DONE  # cancelled before its start: the booking system wants no reports
            elif err.code == "recording_delivered":
                e["state"], e["remote"], e["phase"] = rec.ST_DONE, "ready", None  # a link is out; it is never replaced by a lesser status
            elif err.code == "recording_not_opted_in":
                e["opt_in"] = False  # consent withdrawn since the last feed: nothing is sent
            return False
        e["remote"] = status
        return True

    def _fire(self, event: str, e: dict, **extra: Any) -> None:
        self.hass.bus.async_fire(event, {"booking_id": e["id"], "ref": e["ref"], **extra})

    async def _fail(self, e: dict, error: str) -> None:
        if await self._report(e, "failed", error=error):
            e["state"], e["error"], e["phase"] = rec.ST_FAILED, error, None
            self._fire(EVENT_REC_FAILED, e, error=error)

    async def _finalize(self, e: dict, now: datetime) -> None:
        """Everything is in the bucket: check sizes, write the page, report the link."""
        try:
            s3 = self._s3()
            sizes = await s3.list(e["prefix"] + "/")
            wrong = [f for f in e["files"] if sizes.get(f"{e['prefix']}/{f['dest']}") != f["size"]]
            if wrong:
                for f in wrong:
                    f["state"] = rec.F_PENDING  # sent again on the next attempt
                self.last_error = f"{len(wrong)} file(s) of {e['ref']} differ in size in the bucket"
                return
            seconds = self.download_days * 86400
            expires = now + timedelta(seconds=seconds)
            links = {f["dest"]: s3.presign_get(f"{e['prefix']}/{f['dest']}", seconds, now) for f in e["files"]}
            list_key, index_key = f"{e['prefix']}/links.txt", f"{e['prefix']}/index.html"
            await s3.put(list_key, ("\n".join(links.values()) + "\n").encode(), "text/plain; charset=utf-8")
            page = rec.render_index(
                e["ref"], e.get("locale") or "pl", e["files"], links, s3.presign_get(list_key, seconds, now),
                dt_util.as_local(expires).strftime("%d.%m.%Y %H:%M"),
            )
            await s3.put(index_key, page.encode(), "text/html; charset=utf-8")
            url = s3.presign_get(index_key, seconds, now)
        except S3Error as err:
            self.last_error = str(err)
            return
        if await self._report(e, "ready", url=url, expires_at=iso(expires)):
            e["state"], e["expires_at"], e["error"] = rec.ST_READY, expires.timestamp(), None
            if self.delete_sources:  # every size was just checked against the bucket: the copy there is complete
                e["cleanup"], e["cleanup_by"] = rec.CLEAN_PENDING, "upload"
            self._fire(EVENT_REC_READY, e, files=len(e["files"]), bytes=sum(f["size"] for f in e["files"]), expires_at=iso(expires))

    async def _cleanup(self, e: dict, cameras: bool, console: bool, last: bool) -> None:
        """Remove a session's files from the sources: exactly these names and sizes, checked by the bridge afterwards."""
        def apply(files: list[dict], key: str, answer: dict) -> None:
            by_key = {f[key]: f for f in files}
            for jf in answer.get("files") or []:
                f = by_key.get(jf.get(key))
                if f is None:
                    continue
                if jf.get("state") in ("deleted", "missing"):  # missing: somebody removed it already
                    f["src"], f["src_error"] = rec.SRC_DELETED, None
                else:
                    f["src"], f["src_error"] = rec.SRC_FAILED, jf.get("error") or jf.get("state")

        todo = [f for f in e["files"] if f.get("src") != rec.SRC_DELETED]
        if cameras:
            by_camera: dict[str, list[dict]] = {}
            for f in todo:
                if f["source"] != rec.CONSOLE:
                    by_camera.setdefault(f["source"], []).append(f)
            for camera_id, files in by_camera.items():
                device = self._device_for(camera_id)
                if device is None:
                    self.last_error = f"no a74_bridge device named {camera_id}"
                    continue
                answer = await self._bridge("media_delete", {"device_id": device, "files": [{"name": f["name"], "size": f["size"]} for f in files]})
                result = answer.get(camera_id, answer)
                if not isinstance(result, dict) or "error" in result:
                    self.last_error = f"delete on {camera_id}: {result.get('error') if isinstance(result, dict) else result}"
                else:
                    apply(files, "name", result)
        if console:
            files = [f for f in todo if f["source"] == rec.CONSOLE]
            if files:
                answer = await self._bridge("console_media_delete", {"files": [{"path": f["path"], "size": f["size"]} for f in files]})
                if "error" in answer:
                    self.last_error = f"delete on the console: {answer['error']}"
                else:
                    apply(files, "path", answer)
        left = [f for f in e["files"] if f.get("src") != rec.SRC_DELETED]
        if not left:
            e["cleanup"] = rec.CLEAN_DONE
            if e.get("cleanup_by") == "operator":
                if e.get("prefix") and e["state"] == rec.ST_FAILED:
                    try:  # a failed upload may have left parts of the session in the bucket
                        await self._s3().delete_prefix(e["prefix"] + "/")
                    except S3Error as err:
                        self.last_error = str(err)
                e["state"] = rec.ST_DONE
            self._fire(EVENT_SRC_DELETED, e, files=len(e["files"]), bytes=sum(f["size"] for f in e["files"]), by=e.get("cleanup_by"))
        elif last:  # once a day; the next night tries again
            errors = sorted({f"{f['dest']}: {f['src_error']}" for f in left if f.get("src_error")})
            self._fire(EVENT_SRC_FAILED, e, left=len(left), errors=errors[:5], last_error=self.last_error)

    async def _expire(self, now: datetime) -> None:
        for e in self.register.values():
            if e["state"] == rec.ST_READY and e.get("expires_at", 0) <= now.timestamp():
                try:
                    count = await self._s3().delete_prefix(e["prefix"] + "/")
                except S3Error as err:
                    self.last_error = str(err)
                    continue
                e["state"] = rec.ST_DONE
                self._fire(EVENT_REC_DELETED, e, objects=count)

    # ---- operator -----------------------------------------------------------------------------

    async def async_resolve(self, booking_id: str, action: str) -> None:
        async with self._lock:
            e = self.find(booking_id)
            if e is None or e["state"] not in (rec.ST_LEFT, rec.ST_FAILED):
                raise HomeAssistantError(f"No booking {booking_id} is waiting for a decision about its recordings")
            if action == "delete":
                if not e.get("files"):
                    raise HomeAssistantError(f"Booking {e['ref']} has no files listed on the sources")
                # Done by the loop as soon as nobody is in: a card delete takes the camera out of Remote mode.
                e.update(cleanup=rec.CLEAN_PENDING, cleanup_by="operator", tries={})
            if action == "upload":
                try:
                    await self.coordinator.api.set_recording_opt_in(e["id"], True)
                except ApiError as err:
                    raise HomeAssistantError(f"Booking system refused the opt-in for {e['ref']}: {err}") from err
                e.update(opt_in=True, state=rec.ST_WAITING, inspected_on=None, stalled=0, sent_before=-1, tries={}, error=None)
            # "keep": nothing changes; the signal comes back tomorrow
            await self._save()
        self.coordinator.async_update_listeners()
