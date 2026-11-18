"""Polls the booking feed, keeps the lock in line with it and reports what happened.

The rule is reconciliation, not a schedule: timers only wake the reconciler; what belongs on the
lock is always decided by comparing the wanted state with what the lock reports. A restart, a
moved booking or a failed write therefore need no handling of their own - the next pass fixes it.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import ApiAuthError, ApiError, BookingApi, iso
from .const import (
    CONF_LOCK,
    DEFAULT_DRY_RUN,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLOT_LIMIT,
    DOMAIN,
    EVENT_CODE_APPLIED,
    EVENT_CODE_FAILED,
    EVENT_CODE_REMOVED,
    EVENT_SYNC_FAILED,
    OPT_DRY_RUN,
    OPT_DRY_RUN_REPORT,
    OPT_SCAN_INTERVAL,
    OPT_SLOT_LIMIT,
    RETRY_DELAYS_S,
    STALE_SYNC_S,
    STARTUP_DELAY_S,
    STATE_APPLIED,
    STATE_FAILED,
    STATE_PENDING,
    WINDOW_AHEAD_D,
    WINDOW_BACK_H,
)
from .lock import DryRunLock, LockError, MatterLock
from .planner import (
    REASON_ORPHAN,
    Booking,
    current_booking,
    desired_codes,
    lock_name,
    next_boundary,
    orphan_users,
    parse_bookings,
    parse_dt,
    record_matches_user,
    removal_reason,
    upcoming_booking,
    user_at,
)

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1


class BookingCoordinator(DataUpdateCoordinator[list[Booking]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: BookingApi) -> None:
        interval = entry.options.get(OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=timedelta(seconds=interval))
        self.entry = entry
        self.api = api
        self.lock = MatterLock(hass, entry.data[CONF_LOCK])
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._feed: list[dict] = []  # last good feed, as received (holds the PINs; lives only in .storage)
        self.bookings: list[Booking] = []
        self.codes: dict[str, dict] = {}  # code_id -> record of a code really on the lock
        self.dry_codes: dict[str, dict] = {}  # the same for dry-run, where the lock is simulated
        self.revoked: set[str] = set()
        self.reports: list[dict] = []  # status reports waiting for the booking system, oldest first
        self.last_sync: datetime | None = None
        self.last_error: str | None = None
        self.failures = 0
        self.auth_failed = False
        self.lock_error: str | None = None
        self._pass_lock = asyncio.Lock()
        self._retry_in: float | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._unsub_lock: CALLBACK_TYPE | None = None

    # ---- options ------------------------------------------------------------------------------

    @property
    def dry_run(self) -> bool:
        return self.entry.options.get(OPT_DRY_RUN, DEFAULT_DRY_RUN)

    @property
    def slot_limit(self) -> int:
        return int(self.entry.options.get(OPT_SLOT_LIMIT, DEFAULT_SLOT_LIMIT))

    @property
    def active_codes(self) -> dict[str, dict]:
        return self.dry_codes if self.dry_run else self.codes

    # ---- views for the entities ---------------------------------------------------------------

    @property
    def current(self) -> Booking | None:
        return current_booking(self.bookings, dt_util.utcnow())

    @property
    def upcoming(self) -> Booking | None:
        return upcoming_booking(self.bookings, dt_util.utcnow())

    def code_status(self, booking: Booking) -> str:
        """What the entities show: none, scheduled, pending, applied, failed, revoked or removed."""
        if not booking.code_id or not booking.confirmed:
            return "none"
        record = self.active_codes.get(booking.code_id)
        if record is not None:
            return record.get("state", STATE_PENDING)
        if booking.code_id in self.revoked:
            return "revoked"
        now = dt_util.utcnow()
        if booking.valid_from and now < booking.valid_from:
            return "scheduled"
        if booking.valid_to and now >= booking.valid_to:
            return "removed"
        return STATE_PENDING

    @property
    def failed_codes(self) -> list[dict]:
        return [
            {"ref": r.get("ref"), "booking_id": r.get("booking_id"), "operation": "remove" if r.get("alerted_remove") else "apply", "error": r.get("error")}
            for r in self.active_codes.values()
            if r.get("state") == STATE_FAILED or r.get("alerted_remove")
        ]

    @property
    def sync_stale(self) -> bool:
        return self.last_sync is None or (dt_util.utcnow() - self.last_sync).total_seconds() > STALE_SYNC_S

    # ---- storage ------------------------------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._feed = data.get("feed") or []
        self.bookings, _ = parse_bookings(self._feed)
        self.codes = data.get("codes") or {}
        self.dry_codes = data.get("dry_codes") or {}
        self.revoked = set(data.get("revoked") or [])
        self.reports = data.get("reports") or []
        if data.get("last_sync"):
            self.last_sync = parse_dt(data["last_sync"])
        self._unsub_lock = async_track_state_change_event(self.hass, [self.lock.entity_id], self._lock_changed)

    async def _save(self) -> None:
        await self._store.async_save({
            "feed": self._feed,
            "last_sync": iso(self.last_sync) if self.last_sync else None,
            "codes": self.codes,
            "dry_codes": self.dry_codes,
            "revoked": sorted(self.revoked),
            "reports": self.reports,
        })

    async def async_remove_store(self) -> None:
        await self._store.async_remove()

    @callback
    def async_stop(self) -> None:
        for unsub in (self._unsub_timer, self._unsub_lock):
            if unsub:
                unsub()
        self._unsub_timer = self._unsub_lock = None

    # ---- feed ---------------------------------------------------------------------------------

    async def _async_update_data(self) -> list[Booking]:
        """Never raises: with the booking system down we keep working from the last good feed."""
        now = dt_util.utcnow()
        try:
            raws = await self.api.bookings(now - timedelta(hours=WINDOW_BACK_H), now + timedelta(days=WINDOW_AHEAD_D))
            bookings, bad = parse_bookings(raws)
            if bad:
                # A record we cannot read must not look like "the booking is gone": that would
                # take a live code off the lock. Keep the previous feed instead.
                raise ApiError(f"{len(bad)} malformed booking record(s): {'; '.join(bad)[:300]}", 200)
        except ApiError as err:
            self._sync_failed(err)
        else:
            self._feed, self.bookings = raws, bookings
            self.last_sync, self.last_error, self.failures, self.auth_failed = now, None, 0, False
            self.revoked &= {b.code_id for b in bookings if b.code_id}
        await self.async_reconcile()
        return self.bookings

    def _sync_failed(self, err: ApiError) -> None:
        self.failures += 1
        self.last_error = str(err)
        if isinstance(err, ApiAuthError):
            self._auth_lost()
        _LOGGER.log(logging.WARNING if self.failures in (1, 6) else logging.DEBUG, "Booking feed failed (%d in a row): %s", self.failures, err)
        self.hass.bus.async_fire(EVENT_SYNC_FAILED, {"error": str(err), "consecutive_failures": self.failures})

    def _auth_lost(self) -> None:
        if not self.auth_failed:
            self.auth_failed = True
            self.entry.async_start_reauth(self.hass)

    # ---- reconciliation -----------------------------------------------------------------------

    async def async_reconcile(self) -> None:
        async with self._pass_lock:
            now = dt_util.utcnow()
            self._retry_in = None
            desired = desired_codes(self.bookings, now, self.revoked)
            try:
                if self.dry_run:
                    if self.codes:  # left on the real lock from before dry-run was switched on: take them off
                        await self._pass(self.lock, self.codes, {}, now, dry=False)
                    await self._pass(DryRunLock(self.dry_codes), self.dry_codes, desired, now, dry=True)
                else:
                    self.dry_codes.clear()
                    await self._pass(self.lock, self.codes, desired, now, dry=False)
                await self._flush_reports()
            finally:
                await self._save()
                self._schedule(now)

    async def _pass(self, lock: MatterLock | DryRunLock, codes: dict[str, dict], desired: dict[str, Booking], now: datetime, dry: bool) -> None:
        if not desired and not codes and dry:
            return
        if not lock.available:
            # Paused, without counting attempts; _lock_changed starts a pass when the lock is back.
            self.lock_error = "lock unavailable"
            return
        try:
            users, max_users = await lock.users()
        except LockError as err:
            self.lock_error = str(err)
            _LOGGER.warning("Could not read the lock's users: %s", err)
            self._want_retry(STARTUP_DELAY_S)
            return
        self.lock_error = None

        # 1. Codes we hold that are no longer wanted.
        for code_id, rec in list(codes.items()):
            if code_id in desired:
                continue
            reason = removal_reason(code_id, rec, self.bookings, now, self.revoked)
            index = rec.get("user_index")
            try:
                if index is not None and record_matches_user(rec, user_at(users, index)):
                    await lock.clear_user(index)
                    users = [u for u in users if u.get("user_index") != index]
                elif index is None and rec.get("credential_index") is not None:
                    await lock.clear_pin(rec["credential_index"])
            except LockError as err:
                self._op_failed(code_id, rec, "remove", err, dry)
                continue
            del codes[code_id]
            if rec.get("credential_index") is not None or rec.get("reported"):
                self._queue_report(rec, code_id, "removed", now, dry)
                self._fire(EVENT_CODE_REMOVED, rec, code_id, dry, reason=reason)
                _LOGGER.info("Code for booking %s removed from the lock (%s)%s", rec.get("ref"), reason, " [dry-run]" if dry else "")

        # A wanted code whose lock user is gone or no longer carries our name: forget the slot now,
        # so that a renamed leftover of ours is cleared as an orphan before the PIN is written again
        # (otherwise the lock would answer "duplicate").
        for code_id in desired:
            rec = codes.get(code_id)
            if rec and rec.get("user_index") is not None and not record_matches_user(rec, user_at(users, rec["user_index"])):
                _LOGGER.warning("Code for booking %s is not on the lock where it was written; writing it again", rec.get("ref"))
                rec.update(user_index=None, credential_index=None, named=False, state=STATE_PENDING)

        # 2. Our users (R-…, or RES-… from 0.1.0) nobody accounts for.
        for user in orphan_users(users, codes):
            try:
                await lock.clear_user(user["user_index"])
            except LockError as err:
                _LOGGER.warning("Could not remove orphan lock user %s: %s", user.get("user_name"), err)
                continue
            users.remove(user)
            _LOGGER.warning("Removed orphan lock user %s (slot %s): not in the integration's store", user.get("user_name"), user.get("user_index"))
            self.hass.bus.async_fire(EVENT_CODE_REMOVED, {
                "booking_id": None, "ref": lock_name(user).split("-", 1)[-1], "code_id": None, "reason": REASON_ORPHAN, "dry_run": dry,
            })

        # 3. Codes that should be on the lock.
        for code_id, booking in desired.items():
            rec = codes.get(code_id)
            on_lock = rec is not None and rec.get("user_index") is not None and record_matches_user(rec, user_at(users, rec["user_index"]))
            if not on_lock:
                if rec is None:
                    rec = codes[code_id] = {"booking_id": booking.id, "ref": booking.ref, "state": STATE_PENDING, "attempts": 0}
                rec.update(user_index=None, credential_index=None, named=False, valid_to=iso(booking.valid_to))
                held = sum(1 for r in codes.values() if r.get("credential_index") is not None)
                if held >= self.slot_limit or (max_users and len(users) >= max_users):
                    err = LockError(f"no free slots: {held} booking codes (limit {self.slot_limit}), {len(users)} of {max_users} lock users")
                    self._op_failed(code_id, rec, "apply", err, dry, immediate=True)
                    continue
                try:
                    cred_index, user_index = await lock.add_pin(booking.pin)
                except LockError as err:
                    self._op_failed(code_id, rec, "apply", err, dry, immediate=err.duplicate)
                    continue
                rec.update(credential_index=cred_index, user_index=user_index)
                await self._save()  # the slot numbers must survive a crash from here on, or the code could never be removed
                if user_index is None:
                    try:
                        _, rec["user_index"] = await lock.pin_exists(cred_index)
                    except LockError:
                        pass
                users.append({"user_index": rec["user_index"], "user_name": None})
            rec["valid_to"] = iso(booking.valid_to)

            if not rec.get("named") and rec.get("user_index") is not None:
                try:
                    await lock.name_user(rec["user_index"], booking.user_name)
                    rec["named"] = True
                except LockError as err:
                    # The PIN works without the name; the name is retried on the next pass.
                    _LOGGER.warning("Could not name lock user %s for booking %s: %s", rec["user_index"], booking.ref, err)
                    self._want_retry(RETRY_DELAYS_S[0])

            if rec.get("state") != STATE_APPLIED:
                try:
                    exists, _ = await lock.pin_exists(rec["credential_index"])
                except LockError as err:
                    self._op_failed(code_id, rec, "apply", err, dry)
                    continue
                if not exists:
                    self._op_failed(code_id, rec, "apply", LockError("the lock does not show the code after writing it"), dry)
                    continue
                rec.update(state=STATE_APPLIED, attempts=0, error=None, alerted_apply=False, applied_at=iso(now))
                self._queue_report(rec, code_id, "applied", now, dry)
                self._fire(EVENT_CODE_APPLIED, rec, code_id, dry, valid_from=iso(booking.valid_from), valid_to=iso(booking.valid_to))
                _LOGGER.info("Code for booking %s is on the lock (user slot %s)%s", booking.ref, rec.get("user_index"), " [dry-run]" if dry else "")

    def _op_failed(self, code_id: str, rec: dict, operation: str, err: LockError, dry: bool, immediate: bool = False) -> None:
        rec["attempts"] = attempts = int(rec.get("attempts") or 0) + 1
        rec["error"] = str(err)
        _LOGGER.warning("Lock %s failed for booking %s (attempt %d): %s", operation, rec.get("ref"), attempts, err)
        if not immediate and attempts <= len(RETRY_DELAYS_S):
            self._want_retry(RETRY_DELAYS_S[attempts - 1])
        if not immediate and attempts < len(RETRY_DELAYS_S):
            return
        if rec.get(f"alerted_{operation}"):
            return  # already in the Error state and announced; later passes keep trying quietly
        rec[f"alerted_{operation}"] = True
        if operation == "apply":
            rec["state"] = STATE_FAILED
            self._queue_report(rec, code_id, "failed", dt_util.utcnow(), dry, error=str(err))
        self._fire(EVENT_CODE_FAILED, rec, code_id, dry, operation=operation, error=str(err))

    def _want_retry(self, seconds: float) -> None:
        self._retry_in = seconds if self._retry_in is None else min(self._retry_in, seconds)

    def _fire(self, event: str, rec: dict, code_id: str, dry: bool, **extra: Any) -> None:
        self.hass.bus.async_fire(event, {"booking_id": rec.get("booking_id"), "ref": rec.get("ref"), "code_id": code_id, **extra, "dry_run": dry})

    # ---- reports to the booking system --------------------------------------------------------

    def _queue_report(self, rec: dict, code_id: str, status: str, at: datetime, dry: bool, error: str | None = None) -> None:
        rec["reported"] = status
        if dry and not self.entry.options.get(OPT_DRY_RUN_REPORT, False):
            return
        # The API keeps only the latest status of a code, so an older report still waiting is dropped:
        # a retried old report must never overtake a newer one.
        self.reports = [r for r in self.reports if r["code_id"] != code_id]
        self.reports.append({"booking_id": rec.get("booking_id"), "code_id": code_id, "status": status, "error": error, "at": iso(at)})

    async def _flush_reports(self) -> None:
        while self.reports:
            report = self.reports[0]
            try:
                await self.api.report_code(report["booking_id"], report["code_id"], report["status"], report.get("error"), report.get("at"))
            except ApiAuthError:
                self._auth_lost()
                return
            except ApiError as err:
                if err.transient:
                    _LOGGER.debug("Status report for code %s waits: %s", report["code_id"], err)
                    return
                _LOGGER.warning("Booking system refused the %s report for code %s: %s", report["status"], report["code_id"], err)
            self.reports.pop(0)

    # ---- timers -------------------------------------------------------------------------------

    def _schedule(self, now: datetime) -> None:
        """Wake at the next window edge (codes appear and vanish on time, not up to a poll late) or retry."""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        targets = []
        boundary = next_boundary(self.bookings, now)
        if boundary:
            targets.append(boundary + timedelta(seconds=1))
        if self._retry_in is not None:
            targets.append(now + timedelta(seconds=self._retry_in))
        if targets:
            self._unsub_timer = async_track_point_in_utc_time(self.hass, self._timer_fired, min(targets))

    @callback
    def _timer_fired(self, _now: datetime) -> None:
        self._unsub_timer = None
        self.entry.async_create_background_task(self.hass, self.async_reconcile_and_notify(), f"{DOMAIN} reconcile")

    @callback
    def _lock_changed(self, event: Event) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        was_down = old is None or old.state in ("unavailable", "unknown")
        if new is not None and new.state not in ("unavailable", "unknown") and was_down:
            self.entry.async_create_background_task(self.hass, self.async_reconcile_and_notify(), f"{DOMAIN} reconcile")

    async def async_reconcile_and_notify(self) -> None:
        await self.async_reconcile()
        self.async_update_listeners()

    # ---- actions ------------------------------------------------------------------------------

    def booking(self, booking_id: str) -> Booking | None:
        return next((b for b in self.bookings if booking_id in (b.id, b.ref)), None)

    async def async_revoke(self, booking: Booking) -> None:
        if booking.code_id:
            self.revoked.add(booking.code_id)
        await self.async_reconcile_and_notify()
