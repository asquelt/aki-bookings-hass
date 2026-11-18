"""Shared entity base and the booking attributes."""
from __future__ import annotations

from datetime import datetime

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BookingCoordinator
from .planner import Booking

# Client data and anything that ticks every minute stays out of the recorder's history.
BOOKING_ATTRS = frozenset({
    "id", "ref", "entity_name", "guest_first_name", "guest_locale", "start", "end",
    "code_status", "recording_opt_in", "recording_status", "minutes_left", "minutes_to_start",
})


def booking_attributes(coordinator: BookingCoordinator, booking: Booking | None, now: datetime) -> dict:
    """Attributes of a booking for the sensors. Never the PIN, never the download link."""
    if booking is None:
        return {}
    return {
        "id": booking.id,
        "ref": booking.ref,
        "entity_name": booking.entity_name,
        "guest_first_name": booking.guest_first_name,
        "guest_locale": booking.guest_locale,
        "start": booking.start.isoformat(),
        "end": booking.end.isoformat(),
        "code_status": coordinator.code_status(booking),
        "recording_opt_in": booking.recording_opt_in,
        "recording_status": booking.recording_status,
    }


class AkiEntity(CoordinatorEntity[BookingCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: BookingCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name="Rezerwacje studia",
            manufacturer="Aki",
            model="Booking system",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=coordinator.entry.data.get("url"),
        )

    @property
    def available(self) -> bool:
        return True  # the entities keep showing the last good feed while the booking system is down
