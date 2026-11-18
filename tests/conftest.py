"""Fixtures: a booking API answered by aioclient_mock and a fake Matter lock behind real HA services."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aki_bookings.const import DOMAIN

URL = "https://booking.test"
API = URL + "/api/ha/v1"
LOCK = "lock.front_door"
NOW = datetime(2030, 6, 3, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _enable_custom(enable_custom_integrations):
    yield


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def booking(ref="AB12CD", start=NOW, hours=2, pin="012345", code="code-1", status="confirmed", **extra) -> dict:
    end = start + timedelta(hours=hours)
    return {
        "id": f"id-{ref}", "ref": ref, "status": status, "start": iso(start), "end": iso(end),
        "entity_name": "Studio XYZ", "guest_first_name": "Anna", "guest_locale": "pl",
        "access": None if status == "cancelled" else {
            "code_id": code, "pin": pin,
            "valid_from": iso(start - timedelta(minutes=15)), "valid_to": iso(end + timedelta(minutes=10)),
        },
        "recording": {"opt_in": False, "status": "none"}, "updated_at": iso(NOW), **extra,
    }


class FakeLock:
    """A Matter lock with user management, as HA's matter.* actions present it."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.max_users = 10
        self.users: dict[int, dict] = {}
        self.fail: dict[str, list[Exception]] = {}
        self.calls: list[str] = []
        hass.states.async_set(LOCK, "locked")
        reg = hass.services.async_register
        reg("matter", "get_lock_users", self._users, supports_response=SupportsResponse.ONLY)
        reg("matter", "set_lock_credential", self._set_credential, supports_response=SupportsResponse.ONLY)
        reg("matter", "get_lock_credential_status", self._status, supports_response=SupportsResponse.ONLY)
        reg("matter", "set_lock_user", self._set_user)
        reg("matter", "clear_lock_user", self._clear_user)
        reg("matter", "clear_lock_credential", self._clear_credential)

    def _enter(self, call: ServiceCall) -> None:
        assert call.data["entity_id"] in (LOCK, [LOCK])
        self.calls.append(call.service)
        queue = self.fail.get(call.service)
        if queue:
            raise queue.pop(0)

    def add_manual(self, index: int, name: str | None, pin: str = "9999") -> None:
        self.users[index] = {"user_index": index, "user_name": name, "pin": pin}

    @property
    def names(self) -> dict[int, str | None]:
        return {i: u["user_name"] for i, u in self.users.items()}

    async def _users(self, call: ServiceCall):
        self._enter(call)
        users = [{"user_index": i, "user_name": u["user_name"], "credentials": [{"type": "pin", "index": i}]} for i, u in sorted(self.users.items())]
        return {LOCK: {"max_users": self.max_users, "users": users}}

    async def _set_credential(self, call: ServiceCall):
        self._enter(call)
        pin = call.data["credential_data"]
        assert isinstance(pin, str)
        if any(u["pin"] == pin for u in self.users.values()):
            raise HomeAssistantError("Failed to set credential: duplicate")
        index = next((i for i in range(1, self.max_users + 1) if i not in self.users), None)
        if index is None:
            raise HomeAssistantError("No available user slots on the lock")
        self.users[index] = {"user_index": index, "user_name": None, "pin": pin}
        return {LOCK: {"credential_index": index, "user_index": index, "next_credential_index": index + 1}}

    async def _status(self, call: ServiceCall):
        self._enter(call)
        index = call.data["credential_index"]
        return {LOCK: {"credential_exists": index in self.users, "user_index": index if index in self.users else None}}

    async def _set_user(self, call: ServiceCall) -> None:
        self._enter(call)
        name = call.data["user_name"]
        # Like the Aqara U200: ten bytes including the terminator, so the tenth character is lost.
        self.users[call.data["user_index"]]["user_name"] = name[:9] + "\x00" if len(name) > 9 else name

    async def _clear_user(self, call: ServiceCall) -> None:
        self._enter(call)
        self.users.pop(call.data["user_index"], None)

    async def _clear_credential(self, call: ServiceCall) -> None:
        self._enter(call)
        self.users.pop(call.data["credential_index"], None)


@pytest.fixture
def lock(hass: HomeAssistant) -> FakeLock:
    return FakeLock(hass)


class Feed:
    """The booking system: a feed we can change between polls, and a record of the reports it got."""

    def __init__(self, aioclient_mock) -> None:
        self.mock = aioclient_mock
        self.down = False
        self.reports: list[tuple[str, str, str]] = []  # (ref, code_id, status) of every report that got through
        self.recording_reports: list[tuple] = []  # (ref, status, url, expires_at)
        self.opt_ins: list[tuple] = []
        self.recording_refusals: dict[str, str] = {}  # status -> error code answered with 409
        self.recording_refused: list[tuple] = []
        self.set([])

    def set(self, bookings: list[dict], status: int = 200) -> None:
        self.bookings = bookings
        self._register(status)

    def _register(self, status: int = 200) -> None:
        import aiohttp
        from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

        self.mock.clear_requests()

        async def patched(method, url, data):
            if self.down:
                raise aiohttp.ClientConnectionError("down")
            self.reports.append((str(url).split("/id-")[1].split("/")[0], data["code_id"], data["status"]))
            return AiohttpClientMockResponse(method, url, json={"data": {"code_id": data["code_id"], "status": data["status"], "current": True}})

        async def recording(method, url, data):
            if data["status"] in self.recording_refusals:
                self.recording_refused.append((str(url).split("/id-")[1].split("/")[0], data["status"]))
                code = self.recording_refusals[data["status"]]
                return AiohttpClientMockResponse(method, url, status=409, json={"error": {"code": code, "message": "refused"}})
            self.recording_reports.append((str(url).split("/id-")[1].split("/")[0], data["status"], data.get("url"), data.get("expires_at")))
            return AiohttpClientMockResponse(method, url, json={"data": {"status": data["status"], "expires_at": data.get("expires_at")}})

        async def opt_in(method, url, data):
            self.opt_ins.append((str(url).split("/id-")[1].split("/")[0], data["opt_in"]))
            return AiohttpClientMockResponse(method, url, json={"data": {"opt_in": data["opt_in"], "status": "none"}})

        for ref in ("AB12CD", "ZZ99ZZ", "CC33CC", "S1", "S2", "S3"):
            self.mock.patch(API + f"/bookings/id-{ref}/access-code", side_effect=patched)
            self.mock.patch(API + f"/bookings/id-{ref}/recording", side_effect=recording)
            self.mock.put(API + f"/bookings/id-{ref}/recording/opt-in", side_effect=opt_in)
        self.mock.get(API + "/health", json={"data": {"status": "ok", "apiVersion": 1, "settings": {}}})
        if self.down:
            self.mock.get(API + "/bookings", exc=aiohttp.ClientConnectionError("down"))
        elif status == 200:
            self.mock.get(API + "/bookings", json={"data": self.bookings})
        else:
            self.mock.get(API + "/bookings", status=status, json={"error": {"code": "unauthenticated", "message": "no"}})

    def set_down(self, down: bool) -> None:
        self.down = down
        self._register()


@pytest.fixture
def feed(aioclient_mock) -> Feed:
    return Feed(aioclient_mock)


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    e = MockConfigEntry(
        domain=DOMAIN, title="booking.test", unique_id="booking.test",
        data={"url": URL, "token": "sha_test", "lock_entity": LOCK}, options={"dry_run": False},
    )
    e.add_to_hass(hass)
    return e
