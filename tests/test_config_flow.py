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


async def test_options_steps_keep_each_others_values_and_check_the_storage(hass, entry, lock, feed, monkeypatch):
    from custom_components.aki_bookings import config_flow
    from custom_components.aki_bookings.s3 import S3Error

    listed = []

    class FakeS3:
        def __init__(self, _session, endpoint, bucket, key, secret):
            self.args = (endpoint, bucket, key, secret)

        async def list(self, prefix):
            listed.append((self.args, prefix))
            if self.args[3] == "wrong":
                raise S3Error("403")
            return {}

    monkeypatch.setattr(config_flow, "S3", FakeS3)
    assert await hass.config_entries.async_setup(entry.entry_id)

    async def step(name, data):
        menu = await hass.config_entries.options.async_init(entry.entry_id)
        form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": name})
        return await hass.config_entries.options.async_configure(form["flow_id"], data)

    rec_input = {"recordings_enabled": True, "s3_root": "nagrania", "download_days": 5.0}
    assert (await step("recordings", rec_input))["errors"] == {"base": "storage_incomplete"}
    full = {**rec_input, "s3_endpoint": "https://acc.r2.cloudflarestorage.com/", "s3_bucket": "b", "s3_access_key": "k", "s3_secret_key": "wrong"}
    assert (await step("recordings", full))["errors"] == {"base": "storage_refused"}
    result = await step("recordings", {**full, "s3_secret_key": "right"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["dry_run"] is False and entry.options["download_days"] == 5
    assert entry.options["s3_endpoint"] == "https://acc.r2.cloudflarestorage.com" and listed[-1][1] == "nagrania/"

    await step("recordings", {"recordings_enabled": True, "s3_endpoint": "https://acc.r2.cloudflarestorage.com", "s3_bucket": "b", "s3_root": "nagrania", "download_days": 7})
    await hass.async_block_till_done()
    assert entry.options["s3_secret_key"] == "right"  # an empty key field keeps the stored key
    await step("codes", {"dry_run": True, "dry_run_report": False, "scan_interval": 300.0, "slot_limit": 8.0})
    await hass.async_block_till_done()
    assert entry.options["dry_run"] is True and entry.options["recordings_enabled"] is True and entry.options["scan_interval"] == 300
