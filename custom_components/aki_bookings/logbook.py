"""Logbook lines for the integration's events: every lock operation is on record, by booking ref."""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_CODE_APPLIED, EVENT_CODE_FAILED, EVENT_CODE_REMOVED, EVENT_SYNC_FAILED


@callback
def async_describe_events(hass: HomeAssistant, async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None]) -> None:
    def describe(event: Event) -> dict[str, str]:
        data = event.data
        dry = " [dry-run]" if data.get("dry_run") else ""
        if event.event_type == EVENT_CODE_APPLIED:
            message = f"door code for booking {data.get('ref')} is on the lock{dry}"
        elif event.event_type == EVENT_CODE_REMOVED:
            message = f"door code for booking {data.get('ref')} removed from the lock ({data.get('reason')}){dry}"
        elif event.event_type == EVENT_CODE_FAILED:
            message = f"lock {data.get('operation')} failed for booking {data.get('ref')}: {data.get('error')}{dry}"
        else:
            message = f"booking feed failed ({data.get('consecutive_failures')} in a row): {data.get('error')}"
        return {LOGBOOK_ENTRY_NAME: "Aki Bookings", LOGBOOK_ENTRY_MESSAGE: message}

    for event_type in (EVENT_CODE_APPLIED, EVENT_CODE_REMOVED, EVENT_CODE_FAILED, EVENT_SYNC_FAILED):
        async_describe_event(DOMAIN, event_type, describe)
