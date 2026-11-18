"""Config flow: /health decides."""
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.aki_bookings.const import DOMAIN

from .conftest import API, LOCK, URL

INPUT = {"url": URL + "/", "token": "sha_x", "lock_entity": LOCK}


async def _submit(hass: HomeAssistant, data: dict):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(result["flow_id"], dict(data))


async def test_success(hass, lock, aioclient_mock):
    aioclient_mock.get(API + "/health", json={"data": {"status": "ok", "apiVersion": 1}})
    aioclient_mock.get(API + "/bookings", json={"data": []})
    result = await _submit(hass, INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result.get("errors")
    assert result["data"]["url"] == URL and result["title"] == "booking.test"
    await hass.async_block_till_done()


async def test_errors(hass, lock, aioclient_mock):
    assert (await _submit(hass, {**INPUT, "url": "http://booking.test"}))["errors"] == {"base": "https_required"}
    aioclient_mock.get(API + "/health", status=401, text="<html>Private preview</html>", headers={"Content-Type": "text/html"})
    assert (await _submit(hass, INPUT))["errors"] == {"base": "invalid_site_token"}
    aioclient_mock.clear_requests()
    aioclient_mock.get(API + "/health", status=401, json={"error": {"code": "unauthenticated", "message": "no"}})
    assert (await _submit(hass, INPUT))["errors"] == {"base": "invalid_auth"}
    aioclient_mock.clear_requests()
    aioclient_mock.get(API + "/health", status=503, json={"error": {"code": "busy", "message": "later"}})
    assert (await _submit(hass, INPUT))["errors"] == {"base": "cannot_connect"}
