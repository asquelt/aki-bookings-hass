"""Logbook lines for the integration's events: every lock operation is on record, by booking ref."""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import (
    DOMAIN, EVENT_CODE_APPLIED, EVENT_CODE_FAILED, EVENT_CODE_REMOVED, EVENT_REC_DELETED, EVENT_REC_EMPTY, EVENT_REC_FAILED,
    EVENT_REC_LEFT, EVENT_REC_NONE, EVENT_REC_READY, EVENT_REC_UNASSIGNED, EVENT_SRC_DELETED, EVENT_SRC_FAILED, EVENT_SYNC_FAILED,
)

REC = {
    EVENT_REC_READY: "recordings of booking {ref} uploaded ({files} files); the link was reported",
    EVENT_REC_FAILED: "recording upload for booking {ref} failed: {error}",
    EVENT_REC_EMPTY: "booking {ref} opted in, but there was nothing to upload",
    EVENT_REC_LEFT: "recordings of booking {ref} are still on the cards or the console disk (no opt-in)",
    EVENT_REC_NONE: "booking {ref} left no recordings",
    EVENT_REC_UNASSIGNED: "files outside any booking are on the cards or the console disk",
    EVENT_REC_DELETED: "recordings of booking {ref} deleted from the cloud ({objects} objects)",
    EVENT_SRC_DELETED: "recordings of booking {ref} removed from the cards and the console disk ({files} files)",
    EVENT_SRC_FAILED: "{left} file(s) of booking {ref} could not be removed from the cards or the console disk",
}


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
        elif event.event_type in REC:
            message = REC[event.event_type].format_map({"ref": "?", "files": "?", "error": "?", "objects": "?", "left": "?", **data})
        else:
            message = f"booking feed failed ({data.get('consecutive_failures')} in a row): {data.get('error')}"
        return {LOGBOOK_ENTRY_NAME: "Aki Bookings", LOGBOOK_ENTRY_MESSAGE: message}

    for event_type in (EVENT_CODE_APPLIED, EVENT_CODE_REMOVED, EVENT_CODE_FAILED, EVENT_SYNC_FAILED, *REC):
        async_describe_event(DOMAIN, event_type, describe)
