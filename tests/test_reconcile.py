"""The whole integration against a fake lock and a fake booking system."""
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed

from custom_components.aki_bookings.const import DOMAIN

from .conftest import LOCK, NOW, booking


async def setup(hass: HomeAssistant, entry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def advance(hass: HomeAssistant, freezer, **delta) -> None:
    freezer.tick(timedelta(**delta))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.fixture(autouse=True)
def _time(freezer):
    freezer.move_to(NOW)


async def test_code_goes_on_at_window_start_and_off_at_its_end(hass, entry, lock, feed, freezer):
    feed.set([booking(start=NOW + timedelta(hours=1), hours=1)])  # code window 08:45-10:10
    applied = async_capture_events(hass, f"{DOMAIN}_code_applied")
    removed = async_capture_events(hass, f"{DOMAIN}_code_removed")
    await setup(hass, entry)
    assert lock.users == {} and feed.reports == []

    await advance(hass, freezer, minutes=45, seconds=2)  # the boundary timer, not the 5-minute poll
    assert lock.names == {1: "RES-AB12CD"} and lock.users[1]["pin"] == "012345"
    assert feed.reports == [("AB12CD", "code-1", "applied")]
    assert len(applied) == 1 and applied[0].data["ref"] == "AB12CD" and not applied[0].data["dry_run"]
    assert "pin" not in applied[0].data

    await advance(hass, freezer, hours=1, minutes=25)
    assert lock.users == {}
    assert feed.reports[-1] == ("AB12CD", "code-1", "removed")
    assert removed[0].data["reason"] == "ended"


async def test_entities(hass, entry, lock, feed, freezer):
    feed.set([booking(), booking("ZZ99ZZ", NOW + timedelta(hours=5), 1, "222222", "c2")])
    await setup(hass, entry)
    states = {s.entity_id: s for s in hass.states.async_all() if s.entity_id != LOCK}
    by_key = {eid.split(".")[0] + ":" + s.attributes.get("friendly_name", ""): s for eid, s in states.items()}
    current = next(s for s in states.values() if s.state == "AB12CD")
    assert current.attributes["guest_first_name"] == "Anna" and current.attributes["code_status"] == "applied"
    assert current.attributes["minutes_left"] == 120
    nxt = next(s for s in states.values() if s.state == "ZZ99ZZ")
    assert nxt.attributes["minutes_to_start"] == 300 and nxt.attributes["code_status"] == "scheduled"
    assert all("pin" not in s.attributes and "012345" not in str(s.attributes) for s in states.values())
    assert sum(1 for eid in states if eid.startswith("binary_sensor.")) == 2
    assert sum(1 for eid in states if eid.startswith("calendar.")) == 1 and by_key


async def test_dry_run_touches_nothing(hass, entry, lock, feed, freezer):
    hass.config_entries.async_update_entry(entry, options={})  # the default is dry-run
    feed.set([booking()])
    applied = async_capture_events(hass, f"{DOMAIN}_code_applied")
    await setup(hass, entry)
    assert lock.calls == [] and lock.users == {} and feed.reports == []
    assert len(applied) == 1 and applied[0].data["dry_run"] is True
    await advance(hass, freezer, minutes=5)
    assert len(applied) == 1 and lock.calls == []


async def test_switching_to_dry_run_takes_real_codes_off(hass, entry, lock, feed, freezer):
    feed.set([booking()])
    await setup(hass, entry)
    assert lock.names == {1: "RES-AB12CD"}
    hass.config_entries.async_update_entry(entry, options={"dry_run": True})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert lock.users == {}
    assert feed.reports[-1] == ("AB12CD", "code-1", "removed")


async def test_cancellation_removes_code(hass, entry, lock, feed, freezer):
    feed.set([booking()])
    removed = async_capture_events(hass, f"{DOMAIN}_code_removed")
    await setup(hass, entry)
    feed.set([booking(status="cancelled")])
    await advance(hass, freezer, minutes=5)
    assert lock.users == {} and removed[0].data["reason"] == "cancelled"
    assert feed.reports == [("AB12CD", "code-1", "applied"), ("AB12CD", "code-1", "removed")]


async def test_regenerated_pin_swaps_codes(hass, entry, lock, feed, freezer):
    feed.set([booking()])
    await setup(hass, entry)
    feed.set([booking(pin="654321", code="code-2")])
    await advance(hass, freezer, minutes=5)
    assert [u["pin"] for u in lock.users.values()] == ["654321"]
    assert feed.reports[1:] == [("AB12CD", "code-1", "removed"), ("AB12CD", "code-2", "applied")]


async def test_booking_gone_from_a_good_feed_removes_code_but_a_bad_feed_does_not(hass, entry, lock, feed, freezer):
    feed.set([booking()])
    await setup(hass, entry)
    bad = booking()
    del bad["start"]
    feed.set([bad])
    await advance(hass, freezer, minutes=5)
    assert lock.names == {1: "RES-AB12CD"}  # an unreadable record is not "gone"
    feed.set_down(True)
    await advance(hass, freezer, minutes=5)
    assert lock.names == {1: "RES-AB12CD"}
    feed.set_down(False)
    feed.set([])
    await advance(hass, freezer, minutes=5)
    assert lock.users == {}


async def test_api_down_still_removes_on_time_and_reports_later(hass, entry, lock, feed, freezer):
    feed.set([booking(hours=1)])  # code until 09:10
    await setup(hass, entry)
    feed.set_down(True)
    failed = async_capture_events(hass, f"{DOMAIN}_sync_failed")
    await advance(hass, freezer, hours=1, minutes=11)
    assert lock.users == {} and failed
    assert feed.reports == [("AB12CD", "code-1", "applied")]
    feed.set_down(False)
    feed.set([])
    await advance(hass, freezer, minutes=5)
    assert feed.reports[-1] == ("AB12CD", "code-1", "removed")


async def test_duplicate_fails_at_once_without_retrying_another_pin(hass, entry, lock, feed, freezer):
    lock.add_manual(1, "Kasia", pin="012345")
    feed.set([booking()])
    failed = async_capture_events(hass, f"{DOMAIN}_code_failed")
    await setup(hass, entry)
    assert lock.names == {1: "Kasia"}
    assert feed.reports == [("AB12CD", "code-1", "failed")]
    assert "duplicate" in failed[0].data["error"]
    await advance(hass, freezer, minutes=5)
    assert feed.reports == [("AB12CD", "code-1", "failed")] and len(failed) == 1  # announced once
    problem = next(s for s in hass.states.async_all("binary_sensor") if s.attributes.get("device_class") == "problem")
    assert problem.state == "on" and "code_failed" in problem.attributes["reasons"]


async def test_three_timeouts_then_error_then_recovery(hass, entry, lock, feed, freezer):
    lock.fail["set_lock_credential"] = [HomeAssistantError("CHIP timeout")] * 3
    feed.set([booking()])
    failed = async_capture_events(hass, f"{DOMAIN}_code_failed")
    await setup(hass, entry)
    assert feed.reports == [] and not failed
    await advance(hass, freezer, seconds=31)
    assert feed.reports == [] and not failed
    await advance(hass, freezer, seconds=61)
    assert feed.reports == [("AB12CD", "code-1", "failed")] and len(failed) == 1
    await advance(hass, freezer, seconds=121)
    assert lock.names == {1: "RES-AB12CD"}
    assert feed.reports[-1] == ("AB12CD", "code-1", "applied")


async def test_orphan_is_removed_and_manual_users_are_left_alone(hass, entry, lock, feed, freezer):
    lock.add_manual(1, "Kasia", "1111")
    lock.add_manual(2, "RES-OLD001", "2222")
    lock.add_manual(3, None, "3333")
    feed.set([booking()])
    removed = async_capture_events(hass, f"{DOMAIN}_code_removed")
    await setup(hass, entry)
    assert lock.names == {1: "Kasia", 2: "RES-AB12CD", 3: None}
    assert removed[0].data["reason"] == "orphan" and removed[0].data["ref"] == "OLD001"


async def test_slot_limit(hass, entry, lock, feed, freezer):
    hass.config_entries.async_update_entry(entry, options={"dry_run": False, "slot_limit": 2})
    feed.set([booking(f"S{i}", NOW, 2, f"11111{i}", f"c{i}") for i in (1, 2, 3)])
    failed = async_capture_events(hass, f"{DOMAIN}_code_failed")
    await setup(hass, entry)
    assert sorted(n for n in lock.names.values()) == ["RES-S1", "RES-S2"]
    assert ("S3", "c3", "failed") in feed.reports and "no free slots" in failed[0].data["error"]


async def test_lock_unavailable_pauses_and_resumes(hass, entry, lock, feed, freezer):
    hass.states.async_set(LOCK, "unavailable")
    feed.set([booking()])
    failed = async_capture_events(hass, f"{DOMAIN}_code_failed")
    await setup(hass, entry)
    await advance(hass, freezer, minutes=20)
    assert lock.calls == [] and not failed and feed.reports == []
    hass.states.async_set(LOCK, "locked")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert lock.names == {1: "RES-AB12CD"}


async def test_restart_keeps_the_code_and_does_not_write_it_twice(hass, entry, lock, feed, freezer):
    feed.set([booking()])
    await setup(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert lock.calls.count("set_lock_credential") == 1 and lock.names == {1: "RES-AB12CD"}
    assert feed.reports == [("AB12CD", "code-1", "applied")]


async def test_code_deleted_by_hand_is_written_again(hass, entry, lock, feed, freezer):
    feed.set([booking()])
    await setup(hass, entry)
    lock.users.clear()
    await advance(hass, freezer, minutes=5)
    assert lock.names == {1: "RES-AB12CD"}


async def test_revoke_code_service(hass, entry, lock, feed, freezer):
    feed.set([booking()])
    await setup(hass, entry)
    await hass.services.async_call(DOMAIN, "revoke_code", {"booking_id": "AB12CD"}, blocking=True)
    assert lock.users == {} and feed.reports[-1] == ("AB12CD", "code-1", "removed")
    await advance(hass, freezer, minutes=5)
    assert lock.users == {}  # stays off while the feed still carries the same code_id


async def test_revoked_token_starts_reauth_and_keeps_serving_the_last_feed(hass, entry, lock, feed, freezer):
    feed.set([booking()])
    await setup(hass, entry)
    feed.set([], status=401)
    await advance(hass, freezer, minutes=5)
    assert lock.names == {1: "RES-AB12CD"}
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
