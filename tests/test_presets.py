"""Client presets: whose settings belong on the cameras, and the load/save round against a fake a74_bridge."""
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events, async_fire_time_changed

from custom_components.aki_bookings.planner import parse_bookings, preset_booking, preset_name

from .conftest import NOW, booking

GUARD = "input_boolean.tryb_nagrywania"
REF = "checklista-4k25-pp11"
CLIENT = "klient-studio-xyz"
OPTIONS = {"dry_run": True, "presets_enabled": True}


def test_preset_name_is_a_plain_slug_of_the_entity():
    assert preset_name("Studio XYZ sp. z o.o.") == "klient-studio-xyz-sp-z-o-o"
    assert preset_name("  Żółta Łódź / Kraków ") == "klient-zolta-lodz-krakow"
    assert preset_name("") is None and preset_name(None) is None and preset_name("!!!") is None


def test_the_session_in_progress_wins_then_the_one_coming_then_the_one_leaving():
    a = booking("AB12CD", NOW, 2, code="a")
    b = booking("ZZ99ZZ", NOW + timedelta(hours=2, minutes=5), 1, code="b", entity_name="Other")
    gone = booking("CC33CC", NOW + timedelta(hours=5), 1, code="c", status="cancelled")
    bookings, _ = parse_bookings([a, b, gone])
    ref = lambda **kw: getattr(preset_booking(bookings, NOW + timedelta(**kw)), "ref", None)  # noqa: E731
    assert ref(minutes=-20) is None
    assert ref(minutes=-10) == "AB12CD"  # the code window is open
    assert ref(hours=1, minutes=55) == "AB12CD"  # still in session although the next code already works
    assert ref(hours=2, minutes=1) == "ZZ99ZZ"  # A is packing, B is about to start
    assert ref(hours=3, minutes=10) == "ZZ99ZZ"  # B packing
    assert ref(hours=3, minutes=20) is None
    assert ref(hours=5, minutes=10) is None  # cancelled


class FakeCameras:
    """a74_bridge's preset actions for two cameras."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.library = {REF}
        self.cams = {"CAM1": {"active": REF, "modified": False}, "CAM2": {"active": REF, "modified": False}}
        self.calls: list[tuple] = []
        self.fail_load = 0
        owner = MockConfigEntry(domain="a74_bridge")
        owner.add_to_hass(hass)
        reg = dr.async_get(hass)
        self.devices = {cam: reg.async_get_or_create(config_entry_id=owner.entry_id, identifiers={("a74_bridge", cam)}, name=cam).id for cam in self.cams}
        hass.states.async_set(GUARD, "off")
        for name in ("preset_status", "preset_list", "preset_load", "preset_save"):
            hass.services.async_register("a74_bridge", name, getattr(self, "_" + name), supports_response=SupportsResponse.OPTIONAL)

    def _guard(self, call: ServiceCall) -> None:
        if self.hass.states.get(GUARD).state == "on":
            raise HomeAssistantError(f"{call.service} refused: {GUARD} is on")

    def did(self, service: str) -> list:
        return [c[1:] for c in self.calls if c[0] == service]

    async def _preset_status(self, call):
        return {"cameras": {cam: {**s, "changes": {}} for cam, s in self.cams.items()}}

    async def _preset_list(self, call):
        return {"presets": [{"name": n} for n in sorted(self.library)]}

    async def _preset_load(self, call):
        self._guard(call)
        preset = call.data["preset"]
        self.calls.append(("preset_load", preset))
        if self.fail_load:
            self.fail_load -= 1
            return {"preset": preset, "cameras": {"CAM1": {"state": "verified"}, "CAM2": {"state": "failed", "error": "camera did not reboot"}}}
        for s in self.cams.values():
            s.update(active=preset, modified=False)
        return {"preset": preset, "cameras": {cam: {"state": "verified"} for cam in self.cams}}

    async def _preset_save(self, call):
        self._guard(call)
        preset = call.data["preset"]
        cams = [c for c, d in self.devices.items() if d in call.data["device_id"]] if call.data.get("device_id") else list(self.cams)
        self.calls.append(("preset_save", preset, cams))
        self.library.add(preset)
        for cam in cams:
            self.cams[cam].update(active=preset, modified=False)
        return {"preset": preset, "cameras": {cam: {"ok": True} for cam in cams}}


@pytest.fixture
def cameras(hass) -> FakeCameras:
    return FakeCameras(hass)


@pytest.fixture(autouse=True)
def _time(freezer):
    freezer.move_to(NOW)


async def start(hass, entry, options=OPTIONS):
    hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data.presets


async def minutes(hass, freezer, count):
    for _ in range(count):
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_new_client_gets_a_copy_of_the_reference_and_changes_are_saved_on_leaving(hass, entry, lock, feed, cameras, freezer):
    feed.set([booking()])
    loaded, saved = async_capture_events(hass, "aki_bookings_preset_loaded"), async_capture_events(hass, "aki_bookings_preset_saved")
    manager = await start(hass, entry)
    await minutes(hass, freezer, 1)
    assert cameras.did("preset_load") == []  # the reference was on the cameras already
    assert cameras.did("preset_save") == [(CLIENT, ["CAM1", "CAM2"])]
    assert loaded[0].data["created"] is True and loaded[0].data["preset"] == CLIENT and loaded[0].data["ref"] == "AB12CD"
    assert manager.current["state"] == "open" and not manager.pending(NOW + timedelta(minutes=1))
    state = hass.states.get("sensor.rezerwacje_studia_client_preset")
    assert state.state == CLIENT and state.attributes["created"] is True

    cameras.cams["CAM2"]["modified"] = True  # the client moved the white balance on one camera
    await minutes(hass, freezer, 125)
    assert len(cameras.did("preset_save")) == 1  # packing: the code still works, nothing is closed yet
    await minutes(hass, freezer, 6)
    assert cameras.did("preset_save")[-1] == (CLIENT, ["CAM2"])
    assert saved[0].data["cameras"] == ["CAM2"] and manager.current is None and manager.last["result"] == "saved"
    assert hass.states.get("sensor.rezerwacje_studia_client_preset").state == "none"


async def test_known_client_gets_their_preset_and_an_untouched_one_is_not_saved(hass, entry, lock, feed, cameras, freezer):
    cameras.library.add(CLIENT)
    feed.set([booking()])
    saved = async_capture_events(hass, "aki_bookings_preset_saved")
    manager = await start(hass, entry)
    await minutes(hass, freezer, 1)
    assert cameras.did("preset_load") == [(CLIENT,)] and cameras.did("preset_save") == []
    await minutes(hass, freezer, 131)
    assert cameras.did("preset_save") == [] and not saved and manager.last["result"] == "unchanged"


async def test_the_same_clients_next_booking_does_not_reload_and_a_hand_loaded_preset_is_not_saved_over(hass, entry, lock, feed, cameras, freezer):
    cameras.library.add(CLIENT)
    feed.set([booking(), booking("ZZ99ZZ", NOW + timedelta(hours=2), 1, code="b")])
    manager = await start(hass, entry)
    await minutes(hass, freezer, 1)
    cameras.cams["CAM1"].update(active=REF, modified=True)  # the operator put the reference back on one camera and tuned it
    cameras.cams["CAM2"]["modified"] = True
    await minutes(hass, freezer, 121)
    assert manager.current["ref"] == "ZZ99ZZ" and manager.last["skipped"] == ["CAM1"]
    assert cameras.did("preset_save") == [(CLIENT, ["CAM2"])]
    assert cameras.did("preset_load") == [(CLIENT,), (CLIENT,)]  # the second one only because CAM1 was on another preset


async def test_nothing_is_loaded_while_recording_mode_is_on(hass, entry, lock, feed, cameras, freezer):
    cameras.library.add(CLIENT)
    hass.states.async_set(GUARD, "on")
    feed.set([booking()])
    manager = await start(hass, entry)
    await minutes(hass, freezer, 5)
    assert cameras.did("preset_load") == [] and manager.current["waiting"] == "recording mode is on" and manager.current["tries"] == 0
    hass.states.async_set(GUARD, "off")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert cameras.did("preset_load") == [(CLIENT,)] and manager.current["state"] == "open"


async def test_a_load_that_keeps_failing_is_reported_once_and_nothing_is_saved_afterwards(hass, entry, lock, feed, cameras, freezer):
    cameras.library.add(CLIENT)
    cameras.fail_load = 99
    feed.set([booking()])
    failed = async_capture_events(hass, "aki_bookings_preset_failed")
    manager = await start(hass, entry)
    await minutes(hass, freezer, 10)
    assert len(cameras.did("preset_load")) == 3 and len(failed) == 1 and failed[0].data["operation"] == "load"
    assert "CAM2: camera did not reboot" in failed[0].data["error"] and manager.current["state"] == "failed"
    cameras.cams["CAM1"]["modified"] = True
    await minutes(hass, freezer, 125)
    assert cameras.did("preset_save") == [] and manager.current is None and manager.last["result"] == "not_loaded"


async def test_switched_off_it_touches_nothing(hass, entry, lock, feed, cameras, freezer):
    feed.set([booking()])
    manager = await start(hass, entry, {"dry_run": True})
    await minutes(hass, freezer, 3)
    assert cameras.calls == [] and manager.current is None and not manager.pending(NOW)


async def test_options_step_checks_the_bridge_and_the_reference(hass, entry, lock, feed, cameras):
    assert await hass.config_entries.async_setup(entry.entry_id)

    async def submit(data):
        menu = await hass.config_entries.options.async_init(entry.entry_id)
        form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "presets"})
        return await hass.config_entries.options.async_configure(form["flow_id"], data)

    assert (await submit({"presets_enabled": True, "preset_reference": "nope"}))["errors"] == {"preset_reference": "reference_missing"}
    hass.services.async_remove("a74_bridge", "preset_status")
    assert (await submit({"presets_enabled": True, "preset_reference": REF}))["errors"] == {"base": "bridge_missing"}
    await submit({"presets_enabled": False, "preset_reference": REF})
    await hass.async_block_till_done()
    assert entry.options["presets_enabled"] is False and entry.options["dry_run"] is False
