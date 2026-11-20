"""Camera presets per client: loaded when a booking's client may come in, saved back when they leave.

The preset is named after the booking's entity (the client's company), so the same client finds the
cameras as they left them. A client without a preset gets one made from the reference preset.
HA holds no settings itself: the files live in the a74_bridge preset library, and every step here is
one of its actions (preset_status, preset_list, preset_load, preset_save).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import iso
from .const import (
    DEFAULT_GUARD,
    DEFAULT_PRESET_REFERENCE,
    DOMAIN,
    EVENT_PRESET_FAILED,
    EVENT_PRESET_LOADED,
    EVENT_PRESET_SAVED,
    OPT_GUARD,
    OPT_PRESET_REFERENCE,
    OPT_PRESETS_ENABLED,
)
from .planner import preset_booking, preset_name

_LOGGER = logging.getLogger(__name__)

BRIDGE = "a74_bridge"
TICK = timedelta(seconds=60)
MAX_TRIES = 3  # per booking and operation; then the operator is told and the cameras are left alone

OPENING, OPEN, FAILED = "opening", "open", "failed"


class PresetError(Exception):
    pass


class PresetManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.presets")
        self.current: dict | None = None  # the booking whose preset is (being put) on the cameras
        self.last: dict | None = None  # how the previous one was closed
        self._lock = asyncio.Lock()
        self._unsubs: list[CALLBACK_TYPE] = []

    # ---- options ------------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.entry.options.get(OPT_PRESETS_ENABLED, False))

    @property
    def reference(self) -> str:
        return self.entry.options.get(OPT_PRESET_REFERENCE) or DEFAULT_PRESET_REFERENCE

    @property
    def _guard_entity(self) -> str | None:
        return self.entry.options.get(OPT_GUARD, DEFAULT_GUARD) or None

    def _blocked(self) -> str | None:
        """Settings files blank the HDMI output and need the cameras in Remote mode."""
        state = self.hass.states.get(self._guard_entity) if self._guard_entity else None
        if state is not None and state.state == "on":
            return "recording mode is on"
        active = self.coordinator.recordings.active if self.coordinator.recordings else None
        if active and active.get("phase") == "cameras":
            return "the cameras are uploading"
        return None

    # ---- lifecycle ----------------------------------------------------------------------------

    async def async_start(self) -> None:
        data = await self._store.async_load() or {}
        self.current, self.last = data.get("current"), data.get("last")
        if not self.enabled:
            return
        self._unsubs.append(async_track_time_interval(self.hass, self._wake, TICK))
        self._unsubs.append(self.coordinator.async_add_listener(self._wake))  # a booking appeared, moved or was cancelled
        if self._guard_entity:
            self._unsubs.append(async_track_state_change_event(self.hass, [self._guard_entity], self._guard_changed))

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    @callback
    def _guard_changed(self, event: Event) -> None:
        new = event.data.get("new_state")
        if new is not None and new.state == "off":
            self._wake()

    @callback
    def _wake(self, _now: datetime | None = None) -> None:
        if self.enabled and not self._lock.locked() and self.pending(dt_util.utcnow()):
            self.entry.async_create_background_task(self.hass, self.async_run(), f"{DOMAIN} presets")

    # ---- views --------------------------------------------------------------------------------

    def _wanted(self, now: datetime):
        return preset_booking(self.coordinator.bookings, now)

    def pending(self, now: datetime) -> bool:
        """Is there a save or a load still to do? The recording pipeline keeps the cameras free meanwhile."""
        if not self.enabled:
            return False
        wanted, cur = self._wanted(now), self.current
        if cur is None:
            return wanted is not None
        return cur["state"] == OPENING or wanted is None or wanted.id != cur["booking_id"]

    # ---- the loop -----------------------------------------------------------------------------

    async def async_run(self) -> None:
        async with self._lock:
            before = (dict(self.current) if self.current else None, self.last)
            try:
                await self._step(dt_util.utcnow())
            except Exception:  # noqa: BLE001 - one bad pass must not stop the loop
                _LOGGER.exception("Preset pass failed")
            if (self.current, self.last) != before:
                await self._store.async_save({"current": self.current, "last": self.last})
                self.coordinator.async_update_listeners()

    async def _step(self, now: datetime) -> None:
        wanted, cur = self._wanted(now), self.current
        if cur and (wanted is None or wanted.id != cur["booking_id"]):
            if not await self._close(cur, now):
                return
            self.current = cur = None
        if wanted and cur is None:
            self.current = cur = {
                "booking_id": wanted.id, "ref": wanted.ref, "preset": preset_name(wanted.entity_name),
                "state": OPENING, "tries": 0, "error": None, "created": False,
            }
        if cur and cur["state"] == OPENING:
            await self._open(cur, now)

    async def _open(self, cur: dict, now: datetime) -> None:
        cur["waiting"] = self._blocked()
        if cur["waiting"]:
            return
        cur["tries"] += 1
        name, reference = cur["preset"], self.reference
        try:
            status = await self._status()
            library = await self._library()
            if name and name not in library:
                # A new client: start from the reference and keep a copy under their name.
                if reference not in library:
                    raise PresetError(f"reference preset {reference} is not in the library")
                if not self._all_on(status, reference):
                    await self._load(reference)
                await self._save(name, None, f"client preset, made from {reference}")
                cur["created"] = True
            else:
                target = name or reference  # a booking without an entity name gets the reference
                if target not in library:
                    raise PresetError(f"preset {target} is not in the library")
                if not self._all_on(status, target):  # already there, e.g. the same client's next booking
                    await self._load(target)
        except PresetError as err:
            cur["error"] = str(err)
            _LOGGER.warning("Preset for booking %s not loaded (attempt %d): %s", cur["ref"], cur["tries"], err)
            if cur["tries"] >= MAX_TRIES:
                cur["state"] = FAILED
                self._fire(EVENT_PRESET_FAILED, cur, operation="load", error=str(err))
            return
        cur.update(state=OPEN, error=None, at=iso(now))
        self._fire(EVENT_PRESET_LOADED, cur, created=cur["created"], reference=reference)
        _LOGGER.info("Preset %s is on the cameras for booking %s%s", name or reference, cur["ref"], " (new, from the reference)" if cur["created"] else "")

    async def _close(self, cur: dict, now: datetime) -> bool:
        """Save what the client changed. True when there is nothing more to do for this booking."""
        name = cur["preset"]
        if cur["state"] != OPEN or not name:
            self.last = {"ref": cur["ref"], "preset": name, "result": "not_loaded" if cur["state"] != OPEN else "reference", "at": iso(now)}
            return True
        cur["waiting"] = self._blocked()
        if cur["waiting"]:
            return False
        try:
            status = await self._status()
            changed = sorted(cam for cam, s in status.items() if s.get("active") == name and s.get("modified"))
            # A camera on another preset was put there by hand: its settings are not this client's.
            foreign = sorted(cam for cam, s in status.items() if s.get("active") != name)
            if changed:
                await self._save(name, changed, None)
        except PresetError as err:
            cur["close_tries"] = cur.get("close_tries", 0) + 1
            cur["error"] = str(err)
            _LOGGER.warning("Preset %s of booking %s not saved (attempt %d): %s", name, cur["ref"], cur["close_tries"], err)
            if cur["close_tries"] < MAX_TRIES:
                return False
            self._fire(EVENT_PRESET_FAILED, cur, operation="save", error=str(err))
            self.last = {"ref": cur["ref"], "preset": name, "result": "failed", "error": str(err), "at": iso(now)}
            return True
        self.last = {"ref": cur["ref"], "preset": name, "result": "saved" if changed else "unchanged", "cameras": changed, "skipped": foreign, "at": iso(now)}
        if changed:
            self._fire(EVENT_PRESET_SAVED, cur, cameras=changed, skipped=foreign)
            _LOGGER.info("Preset %s saved after booking %s (%s)", name, cur["ref"], ", ".join(changed))
        return True

    # ---- bridges ------------------------------------------------------------------------------

    def _fire(self, event: str, cur: dict, **extra: Any) -> None:
        self.hass.bus.async_fire(event, {"booking_id": cur["booking_id"], "ref": cur["ref"], "preset": cur["preset"] or self.reference, **extra})

    async def _bridge(self, service: str, data: dict | None = None) -> dict:
        try:
            result = await self.hass.services.async_call(BRIDGE, service, data or {}, blocking=True, return_response=True)
        except Exception as err:  # noqa: BLE001 - refused by the guard, bridge down, service missing
            raise PresetError(f"{service}: {str(err) or type(err).__name__}") from err
        return result if isinstance(result, dict) else {}

    async def _status(self) -> dict[str, dict]:
        cameras = (await self._bridge("preset_status")).get("cameras") or {}
        if not cameras:
            raise PresetError("no camera bridge answered")
        errors = [f"{cam}: {s['error']}" for cam, s in cameras.items() if "error" in s]
        if errors:
            raise PresetError("; ".join(errors))
        return cameras

    async def _library(self) -> set[str]:
        return {p["name"] for p in (await self._bridge("preset_list")).get("presets") or []}

    @staticmethod
    def _all_on(status: dict[str, dict], preset: str) -> bool:
        return all(s.get("active") == preset and not s.get("modified") for s in status.values())

    async def _load(self, preset: str) -> None:
        jobs = (await self._bridge("preset_load", {"preset": preset})).get("cameras") or {}
        bad = [f"{cam}: {job.get('error') or job.get('state')}" for cam, job in jobs.items() if job.get("state") != "verified"]
        if bad or not jobs:
            raise PresetError(f"load of {preset} failed: {'; '.join(bad) or 'no camera'}")

    async def _save(self, preset: str, cameras: list[str] | None, note: str | None) -> None:
        data: dict[str, Any] = {"preset": preset}
        if note:
            data["note"] = note
        if cameras is not None:
            data["device_id"] = [self._device_for(cam) for cam in cameras]
        results = (await self._bridge("preset_save", data)).get("cameras") or {}
        bad = [f"{cam}: {r.get('error')}" for cam, r in results.items() if not r.get("ok")]
        if bad or not results:
            raise PresetError(f"save of {preset} failed: {'; '.join(bad) or 'no camera'}")

    def _device_for(self, camera_id: str) -> str:
        for device in dr.async_get(self.hass).devices.values():
            if device.name == camera_id and any(domain == BRIDGE for domain, _ in device.identifiers):
                return device.id
        raise PresetError(f"no a74_bridge device named {camera_id}")
