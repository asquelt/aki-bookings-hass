"""Downloadable diagnostics. PINs, tokens and client names are redacted."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import AkiConfigEntry
from .const import CONF_SITE_TOKEN, CONF_TOKEN

REDACT = {CONF_TOKEN, CONF_SITE_TOKEN, "pin", "guest_first_name", "entity_name"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: AkiConfigEntry) -> dict[str, Any]:
    c = entry.runtime_data
    return async_redact_data({
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "dry_run": c.dry_run,
        "last_sync": c.last_sync.isoformat() if c.last_sync else None,
        "last_error": c.last_error,
        "consecutive_failures": c.failures,
        "auth_failed": c.auth_failed,
        "lock_entity": c.lock.entity_id,
        "lock_available": c.lock.available,
        "lock_error": c.lock_error,
        "feed": c._feed,  # noqa: SLF001
        "codes": c.codes,
        "dry_codes": c.dry_codes,
        "revoked": sorted(c.revoked),
        "pending_reports": c.reports,
    }, REDACT)
