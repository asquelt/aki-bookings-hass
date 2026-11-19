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
    DEFAULT_DOWNLOAD_DAYS,
    DEFAULT_DRY_RUN,
    DEFAULT_GUARD,
    DEFAULT_S3_ROOT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLOT_LIMIT,
    DEFAULT_URL,
    DOMAIN,
    OPT_DELETE_SOURCES,
    OPT_DOWNLOAD_DAYS,
    OPT_DRY_RUN,
    OPT_DRY_RUN_REPORT,
    OPT_GUARD,
    OPT_REC_ENABLED,
    OPT_S3_BUCKET,
    OPT_S3_ENDPOINT,
    OPT_S3_KEY,
    OPT_S3_ROOT,
    OPT_S3_SECRET,
    OPT_SCAN_INTERVAL,
    OPT_SLOT_LIMIT,
)
from .s3 import S3, S3Error

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
        return self.async_show_menu(step_id="init", menu_options=["codes", "recordings"])

    def _save(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        """Each step edits its own part; the other part of the options stays as it was."""
        clean = {k: (int(v) if isinstance(v, float) else v) for k, v in user_input.items()}
        return self.async_create_entry(data={**self.config_entry.options, **clean})

    async def async_step_codes(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)
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
        return self.async_show_form(step_id="codes", data_schema=schema)

    async def async_step_recordings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = self.config_entry.options
        errors: dict[str, str] = {}
        if user_input is not None:
            # An empty key field keeps the stored key: the form never shows secrets back.
            for key in (OPT_S3_KEY, OPT_S3_SECRET):
                if not user_input.get(key) and options.get(key):
                    user_input[key] = options[key]
            if user_input.get(OPT_S3_ENDPOINT):
                user_input[OPT_S3_ENDPOINT] = user_input[OPT_S3_ENDPOINT].strip().rstrip("/")
            have = [bool(user_input.get(k)) for k in (OPT_S3_ENDPOINT, OPT_S3_BUCKET, OPT_S3_KEY, OPT_S3_SECRET)]
            if user_input.get(OPT_REC_ENABLED) and not all(have):
                errors["base"] = "storage_incomplete"
            elif all(have):
                try:
                    s3 = S3(async_get_clientsession(self.hass), user_input[OPT_S3_ENDPOINT], user_input[OPT_S3_BUCKET], user_input[OPT_S3_KEY], user_input[OPT_S3_SECRET])
                    await s3.list(f"{user_input.get(OPT_S3_ROOT, DEFAULT_S3_ROOT)}/")
                except S3Error:
                    errors["base"] = "storage_refused"
            if not errors:
                return self._save(user_input)
            options = {**options, **user_input}

        def text(key: str, default: str = "") -> dict:
            return {"description": {"suggested_value": options.get(key, default)}}

        schema = vol.Schema({
            vol.Required(OPT_REC_ENABLED, default=options.get(OPT_REC_ENABLED, False)): selector.BooleanSelector(),
            vol.Optional(OPT_GUARD, **text(OPT_GUARD, DEFAULT_GUARD)): selector.EntitySelector(),
            vol.Optional(OPT_S3_ENDPOINT, **text(OPT_S3_ENDPOINT)): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.URL)),
            vol.Optional(OPT_S3_BUCKET, **text(OPT_S3_BUCKET)): selector.TextSelector(),
            vol.Optional(OPT_S3_KEY): PASSWORD,
            vol.Optional(OPT_S3_SECRET): PASSWORD,
            vol.Required(OPT_S3_ROOT, default=options.get(OPT_S3_ROOT, DEFAULT_S3_ROOT)): selector.TextSelector(),
            vol.Required(OPT_DOWNLOAD_DAYS, default=options.get(OPT_DOWNLOAD_DAYS, DEFAULT_DOWNLOAD_DAYS)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=7, step=1, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(OPT_DELETE_SOURCES, default=options.get(OPT_DELETE_SOURCES, False)): selector.BooleanSelector(),
        })
        return self.async_show_form(step_id="recordings", data_schema=schema, errors=errors)
