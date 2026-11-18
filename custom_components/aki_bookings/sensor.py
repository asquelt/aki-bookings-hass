"""Sensors: current and next booking, codes on the lock, last sync."""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import AkiConfigEntry
from .const import STATE_APPLIED
from .coordinator import BookingCoordinator
from .entity import BOOKING_ATTRS, AkiEntity, booking_attributes


async def async_setup_entry(hass: HomeAssistant, entry: AkiConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities([
        BookingSensor(coordinator, "current_booking"),
        BookingSensor(coordinator, "next_booking"),
        ActiveCodesSensor(coordinator),
        LastSyncSensor(coordinator),
        RecordingsSensor(coordinator),
    ])


class BookingSensor(AkiEntity, SensorEntity):
    """State is the booking's ref, or `none`. The minute counters refresh once a minute."""

    _unrecorded_attributes = BOOKING_ATTRS

    def __init__(self, coordinator: BookingCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._current = key == "current_booking"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_track_time_interval(self.hass, self._tick, timedelta(minutes=1)))

    @callback
    def _tick(self, _now: datetime) -> None:
        self.async_write_ha_state()

    @property
    def _booking(self):
        return self.coordinator.current if self._current else self.coordinator.upcoming

    @property
    def native_value(self) -> str:
        booking = self._booking
        return booking.ref if booking else "none"

    @property
    def extra_state_attributes(self) -> dict:
        booking = self._booking
        if booking is None:
            return {}
        now = dt_util.utcnow()
        attrs = booking_attributes(self.coordinator, booking, now)
        if self._current:
            attrs["minutes_left"] = max(0, int((booking.end - now).total_seconds() // 60))
        else:
            attrs["minutes_to_start"] = max(0, int((booking.start - now).total_seconds() // 60))
        return attrs


class ActiveCodesSensor(AkiEntity, SensorEntity):
    """How many booking codes are on the lock now."""

    def __init__(self, coordinator: BookingCoordinator) -> None:
        super().__init__(coordinator, "active_codes")

    @property
    def _applied(self) -> list[dict]:
        return [r for r in self.coordinator.active_codes.values() if r.get("state") == STATE_APPLIED]

    @property
    def native_value(self) -> int:
        return len(self._applied)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "refs": sorted(str(r.get("ref")) for r in self._applied),
            "slot_limit": self.coordinator.slot_limit,
            "dry_run": self.coordinator.dry_run,
        }


class LastSyncSensor(AkiEntity, SensorEntity):
    """When the booking feed was last fetched successfully."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BookingCoordinator) -> None:
        super().__init__(coordinator, "last_sync")

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_sync

    @property
    def extra_state_attributes(self) -> dict:
        c = self.coordinator
        return {
            "result": "error" if c.last_error else "ok",
            "bookings_count": len(c.bookings),
            "error_message": c.last_error,
            "consecutive_failures": c.failures,
            "pending_reports": len(c.reports),
        }


class RecordingsSensor(AkiEntity, SensorEntity):
    """Sessions whose recordings still need something: an upload, a decision, or deletion at expiry."""

    def __init__(self, coordinator: BookingCoordinator) -> None:
        super().__init__(coordinator, "recordings")

    @property
    def native_value(self) -> int:
        manager = self.coordinator.recordings
        return len(manager.overview()) if manager else 0

    @property
    def extra_state_attributes(self) -> dict:
        manager = self.coordinator.recordings
        if manager is None:
            return {}
        active = manager.active
        return {
            "enabled": manager.enabled,
            "active": active["ref"] if active else None,
            "phase": active.get("phase") if active else None,
            "sessions": manager.overview(),
            "open_take": bool(manager.journal and manager.journal[-1].get("stop") is None),
            "last_error": manager.last_error,
        }
