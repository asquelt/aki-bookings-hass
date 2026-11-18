"""Binary sensors: a booking is running; something needs the staff's attention."""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from . import AkiConfigEntry
from .coordinator import BookingCoordinator
from .entity import AkiEntity


async def async_setup_entry(hass: HomeAssistant, entry: AkiConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities([BookingActiveSensor(coordinator), ProblemSensor(coordinator)])


class BookingActiveSensor(AkiEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _unrecorded_attributes = frozenset({"booking_id", "ref"})

    def __init__(self, coordinator: BookingCoordinator) -> None:
        super().__init__(coordinator, "booking_active")

    @property
    def is_on(self) -> bool:
        return self.coordinator.current is not None

    @property
    def extra_state_attributes(self) -> dict:
        booking = self.coordinator.current
        return {"booking_id": booking.id, "ref": booking.ref} if booking else {}


class ProblemSensor(AkiEntity, BinarySensorEntity):
    """On when a code is in the Error state, the token was refused, or the feed is over 30 minutes old."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: BookingCoordinator) -> None:
        super().__init__(coordinator, "problem")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # A stale feed is a matter of time passing, not of new data arriving.
        self.async_on_remove(async_track_time_interval(self.hass, self._tick, timedelta(minutes=1)))

    @callback
    def _tick(self, _now: datetime) -> None:
        self.async_write_ha_state()

    @property
    def _reasons(self) -> list[str]:
        c = self.coordinator
        reasons = []
        if c.failed_codes:
            reasons.append("code_failed")
        if c.auth_failed:
            reasons.append("auth_failed")
        if c.sync_stale:
            reasons.append("sync_stale")
        return reasons

    @property
    def is_on(self) -> bool:
        return bool(self._reasons)

    @property
    def extra_state_attributes(self) -> dict:
        c = self.coordinator
        return {"reasons": self._reasons, "failed_codes": c.failed_codes, "sync_error": c.last_error, "lock_error": c.lock_error}
