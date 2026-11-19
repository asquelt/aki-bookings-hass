"""Switch: the client's choice about uploading the recordings, for the booking that is in the studio now.

Meant for the studio kiosk. It exists only while a booking's code window is open, and it writes
straight to the booking system, which is the one place the choice lives.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import AkiConfigEntry
from .api import ApiError
from .coordinator import BookingCoordinator
from .entity import AkiEntity
from .recordings import booking_at


async def async_setup_entry(hass: HomeAssistant, entry: AkiConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([RecordingOptInSwitch(entry.runtime_data)])


class RecordingOptInSwitch(AkiEntity, SwitchEntity):
    _unrecorded_attributes = frozenset({"id", "ref", "recording_status"})

    def __init__(self, coordinator: BookingCoordinator) -> None:
        super().__init__(coordinator, "recording_opt_in")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_track_time_interval(self.hass, self._tick, timedelta(minutes=1)))

    @callback
    def _tick(self, _now: datetime) -> None:
        self.async_write_ha_state()  # the code window opens and closes by the clock

    @property
    def _booking(self):
        return booking_at(self.coordinator.bookings, dt_util.utcnow())

    @property
    def available(self) -> bool:
        return self._booking is not None

    @property
    def is_on(self) -> bool | None:
        booking = self._booking
        return booking.recording_opt_in if booking else None

    @property
    def extra_state_attributes(self) -> dict:
        booking = self._booking
        return {"id": booking.id, "ref": booking.ref, "recording_status": booking.recording_status} if booking else {}

    async def _set(self, opt_in: bool) -> None:
        booking = self._booking
        if booking is None:
            raise HomeAssistantError("No booking is in the studio now")
        try:
            await self.coordinator.api.set_recording_opt_in(booking.id, opt_in)
        except ApiError as err:  # e.g. 409 once the upload has started
            raise HomeAssistantError(f"Booking system refused the recording choice for {booking.ref}: {err}") from err
        await self.coordinator.async_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
