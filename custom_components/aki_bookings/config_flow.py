"""Config flow: API address and tokens, the lock; options for polling, slot limit and dry-run."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiAuthError, ApiError, ApiGateError, BookingApi
from .const import (
    CONF_LOCK,
    CONF_SITE_TOKEN,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_DRY_RUN,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLOT_LIMIT,
    DEFAULT_URL,
    DOMAIN,
    OPT_DRY_RUN,
    OPT_DRY_RUN_REPORT,
    OPT_SCAN_INTERVAL,
    OPT_SLOT_LIMIT,
)

PASSWORD = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))


def _user_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_URL, default=defaults.get(CONF_URL, DEFAULT_URL)): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
        ),
        vol.Required(CONF_TOKEN): PASSWORD,
        vol.Optional(CONF_SITE_TOKEN): PASSWORD,
        vol.Required(CONF_LOCK, default=defaults.get(CONF_LOCK, vol.UNDEFINED)): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="lock", integration="matter")
        ),
    })


class AkiBookingsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _check(self, url: str, token: str, site_token: str | None) -> str | None:
        """Call /health; returns an error key or None."""
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            return "https_required"  # the feed carries PINs
        try:
            health = await BookingApi(async_get_clientsession(self.hass), url, token, site_token).health()
        except ApiGateError:
            return "invalid_site_token"
        except ApiAuthError:
            return "invalid_auth"
        except ApiError:
            return "cannot_connect"
        return None if health.get("apiVersion") == 1 else "unsupported_api"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_URL] = user_input[CONF_URL].strip().rstrip("/")
            error = await self._check(user_input[CONF_URL], user_input[CONF_TOKEN], user_input.get(CONF_SITE_TOKEN))
            if error:
                errors["base"] = error
            else:
                host = urlparse(user_input[CONF_URL]).netloc
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=host, data=user_input)
        return self.async_show_form(step_id="user", data_schema=_user_schema(user_input or {}), errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            site_token = user_input.get(CONF_SITE_TOKEN) or entry.data.get(CONF_SITE_TOKEN)
            error = await self._check(entry.data[CONF_URL], user_input[CONF_TOKEN], site_token)
            if error:
                errors["base"] = error
            else:
                data = {**entry.data, CONF_TOKEN: user_input[CONF_TOKEN]}
                if site_token:
                    data[CONF_SITE_TOKEN] = site_token
                return self.async_update_reload_and_abort(entry, data=data)
        schema = vol.Schema({vol.Required(CONF_TOKEN): PASSWORD, vol.Optional(CONF_SITE_TOKEN): PASSWORD})
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors, description_placeholders={"url": entry.data[CONF_URL]}
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return AkiBookingsOptionsFlow()


class AkiBookingsOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema = vol.Schema({
            vol.Required(OPT_DRY_RUN, default=options.get(OPT_DRY_RUN, DEFAULT_DRY_RUN)): selector.BooleanSelector(),
            vol.Required(OPT_DRY_RUN_REPORT, default=options.get(OPT_DRY_RUN_REPORT, False)): selector.BooleanSelector(),
            vol.Required(OPT_SCAN_INTERVAL, default=options.get(OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=60, max=1800, step=30, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(OPT_SLOT_LIMIT, default=options.get(OPT_SLOT_LIMIT, DEFAULT_SLOT_LIMIT)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=10, step=1, mode=selector.NumberSelectorMode.BOX)
            ),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
