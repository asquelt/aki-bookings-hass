"""Calendar of confirmed bookings, for calendar triggers (start/end with an offset) and the calendar panel."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import AkiConfigEntry
from .coordinator import BookingCoordinator
from .entity import AkiEntity
from .planner import Booking


async def async_setup_entry(hass: HomeAssistant, entry: AkiConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([BookingCalendar(entry.runtime_data)])


class BookingCalendar(AkiEntity, CalendarEntity):
    def __init__(self, coordinator: BookingCoordinator) -> None:
        super().__init__(coordinator, "bookings")

    def _event(self, booking: Booking) -> CalendarEvent:
        who = " · ".join(x for x in (booking.guest_first_name, booking.entity_name) if x)
        return CalendarEvent(
            start=dt_util.as_local(booking.start),
            end=dt_util.as_local(booking.end),
            summary=f"{booking.ref}{' · ' + who if who else ''}",
            description=f"code: {self.coordinator.code_status(booking)}\nrecording: {booking.recording_status}",
            uid=booking.id,
        )

    @property
    def event(self) -> CalendarEvent | None:
        booking = self.coordinator.current or self.coordinator.upcoming
        return self._event(booking) if booking else None

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime, end_date: datetime) -> list[CalendarEvent]:
        return [
            self._event(b) for b in self.coordinator.bookings
            if b.confirmed and b.end > start_date and b.start < end_date
        ]
