"""Aki Bookings: studio bookings from the booking system, door codes on a Matter lock."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import ApiError, BookingApi
from .const import (
    CONF_SITE_TOKEN,
    CONF_TOKEN,
    CONF_URL,
    DOMAIN,
    SERVICE_RECONCILE_LOCK,
    SERVICE_REPORT_RECORDING,
    SERVICE_RESOLVE_RECORDINGS,
    SERVICE_RUN_RECORDINGS,
    SERVICE_REVOKE_CODE,
    SERVICE_SET_RECORDING_OPT_IN,
    SERVICE_SYNC_NOW,
)
from .coordinator import BookingCoordinator
from .recorder import RecordingManager

PLATFORMS = [Platform.BINARY_SENSOR, Platform.CALENDAR, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AkiConfigEntry = ConfigEntry[BookingCoordinator]

BOOKING = {vol.Required("booking_id"): cv.string}


def _coordinator(hass: HomeAssistant) -> BookingCoordinator:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry.runtime_data
    raise ServiceValidationError("Aki Bookings is not set up")


def _booking(coordinator: BookingCoordinator, call: ServiceCall):
    booking = coordinator.booking(call.data["booking_id"])
    if booking is None:
        raise ServiceValidationError(f"No booking {call.data['booking_id']} in the current feed (use its id or ref)")
    return booking


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def sync_now(call: ServiceCall) -> None:
        await _coordinator(hass).async_refresh()

    async def reconcile_lock(call: ServiceCall) -> None:
        await _coordinator(hass).async_reconcile_and_notify()

    async def revoke_code(call: ServiceCall) -> None:
        coordinator = _coordinator(hass)
        await coordinator.async_revoke(_booking(coordinator, call))

    async def set_recording_opt_in(call: ServiceCall) -> None:
        coordinator = _coordinator(hass)
        booking = _booking(coordinator, call)
        try:
            await coordinator.api.set_recording_opt_in(booking.id, call.data["opt_in"])
        except ApiError as err:
            raise HomeAssistantError(f"Booking system refused the recording choice for {booking.ref}: {err}") from err
        await coordinator.async_refresh()

    async def report_recording(call: ServiceCall) -> None:
        coordinator = _coordinator(hass)
        booking = _booking(coordinator, call)
        expires = call.data.get("expires_at")
        try:
            await coordinator.api.report_recording(
                booking.id, call.data["status"], call.data.get("url"), expires.isoformat() if expires else None, call.data.get("error")
            )
        except ApiError as err:  # the message never carries the download link
            raise HomeAssistantError(f"Booking system refused the recording report for {booking.ref}: {err}") from err
        await coordinator.async_refresh()

    async def resolve_recordings(call: ServiceCall) -> None:
        await _coordinator(hass).recordings.async_resolve(call.data["booking_id"], call.data["action"])

    async def run_recordings(call: ServiceCall) -> None:
        manager = _coordinator(hass).recordings
        if not manager.enabled:
            raise ServiceValidationError("The recording pipeline is switched off in the options")
        await manager.async_run()

    hass.services.async_register(
        DOMAIN, SERVICE_RESOLVE_RECORDINGS, resolve_recordings, vol.Schema({**BOOKING, vol.Required("action"): vol.In(["upload", "delete", "keep"])})
    )
    hass.services.async_register(DOMAIN, SERVICE_RUN_RECORDINGS, run_recordings, vol.Schema({}))
    hass.services.async_register(DOMAIN, SERVICE_SYNC_NOW, sync_now, vol.Schema({}))
    hass.services.async_register(DOMAIN, SERVICE_RECONCILE_LOCK, reconcile_lock, vol.Schema({}))
    hass.services.async_register(DOMAIN, SERVICE_REVOKE_CODE, revoke_code, vol.Schema(BOOKING))
    hass.services.async_register(
        DOMAIN, SERVICE_SET_RECORDING_OPT_IN, set_recording_opt_in, vol.Schema({**BOOKING, vol.Required("opt_in"): cv.boolean})
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REPORT_RECORDING, report_recording,
        vol.Schema({
            **BOOKING,
            vol.Required("status"): vol.In(["uploading", "ready", "failed", "empty"]),
            vol.Optional("url"): cv.url,
            vol.Optional("expires_at"): cv.datetime,
            vol.Optional("error"): cv.string,
        }),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AkiConfigEntry) -> bool:
    api = BookingApi(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN], entry.data.get(CONF_SITE_TOKEN))
    coordinator = BookingCoordinator(hass, entry, api)
    await coordinator.async_load()
    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_stop)
    entry.async_on_unload(entry.add_update_listener(_reload))
    await coordinator.async_config_entry_first_refresh()  # works from the stored feed when the API is down
    coordinator.recordings = RecordingManager(hass, entry, coordinator)
    await coordinator.recordings.async_start()
    entry.async_on_unload(coordinator.recordings.async_stop)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _reload(hass: HomeAssistant, entry: AkiConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AkiConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: AkiConfigEntry) -> None:
    """Forget the stored feed (it holds PINs). Codes still on the lock are not touched here."""
    from homeassistant.helpers.storage import Store

    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.recordings").async_remove()
