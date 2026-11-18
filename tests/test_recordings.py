"""The recording pipeline: pure rules, link signing, and a whole night against fake bridges and storage."""
from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events, async_fire_time_changed

from custom_components.aki_bookings import recordings as rec
from custom_components.aki_bookings.const import DOMAIN
from custom_components.aki_bookings.planner import parse_bookings
from custom_components.aki_bookings.recorder import RecordingManager
from custom_components.aki_bookings.s3 import S3

from .conftest import NOW, booking

GUARD = "input_boolean.tryb_nagrywania"
OPTIONS = {
    "dry_run": True, "recordings_enabled": True, "s3_endpoint": "https://store.test", "s3_bucket": "bucket",
    "s3_access_key": "key", "s3_secret_key": "secret", "s3_root": "nagrania", "download_days": 7,
}


def at(hours=0, minutes=0, seconds=0) -> float:
    return (NOW + timedelta(hours=hours, minutes=minutes, seconds=seconds)).timestamp()


# ---- pure rules -------------------------------------------------------------------------------

def entry(ref="AB12CD", start=NOW, hours=2):
    (b,), _ = parse_bookings([booking(ref, start, hours, code=f"c-{ref}")])
    return rec.entry_from_booking(b)


def cam(name, when, size=100, camera="CAM1"):
    return rec.camera_file(camera, {"name": name, "folder": "2030-06-03", "size": size, "recorded_ts": when})


def test_files_go_to_the_take_then_to_the_window_and_otherwise_to_nobody():
    a, b = entry("AB12CD"), entry("ZZ99ZZ", NOW + timedelta(hours=3), 1)
    journal = [{"start": at(0, 30), "stop": at(0, 40), "booking_id": a["id"]}, {"start": at(5), "stop": at(5, 5), "booking_id": None}]
    files = [
        cam("TAKE.MP4", at(0, 30, -8)), cam("BODY.MP4", at(1, 30)), cam("OLD.MP4", at(-2)), cam("B.MP4", at(3, 10)),
        cam("STAFF.MP4", at(5, 1)), cam("LATE.MP4", at(2, 30)),
    ]
    assigned, unassigned = rec.assign_files(files, [a, b], journal)
    assert [f["name"] for f in assigned[a["id"]]] == ["TAKE.MP4", "BODY.MP4"]
    assert [f["name"] for f in assigned[b["id"]]] == ["B.MP4"]
    assert [f["name"] for f in unassigned] == ["OLD.MP4", "STAFF.MP4", "LATE.MP4"]


def test_console_capture_time_comes_from_the_folder_name_in_utc():
    f = rec.console_file({"path": "4K CAPTURE_2030-06-03_4_08-30-00/Video/1_CAMERA 1.mp4", "size": 5, "mtime_ts": 1})
    assert f["ts"] == datetime(2030, 6, 3, 8, 30, tzinfo=timezone.utc).timestamp()
    assert f["dest"] == "iso_083000_Video_1_CAMERA_1.mp4" and f["source"] == "console"
    assert rec.console_file({"path": "odd/file.wav", "size": 5, "mtime_ts": 77.0})["ts"] == 77.0
    assert rec.camera_file("CAM 1", {"name": "C0001.MP4", "recorded_ts": 1, "size": 1})["dest"] == "CAM_1_C0001.MP4"


def test_upload_windows():
    bookings, _ = parse_bookings([booking("AB12CD", NOW, 2), booking("ZZ99ZZ", NOW + timedelta(hours=4), 1, "222222", "c2")])
    local = lambda dt: dt.date()  # noqa: E731
    t = lambda **kw: NOW + timedelta(**kw)  # noqa: E731
    assert not rec.camera_upload_allowed(bookings, t(hours=1), False, local)  # a client is in
    assert not rec.camera_upload_allowed(bookings, t(hours=2, minutes=20), False, local)  # another session later today
    assert rec.console_upload_allowed(bookings, t(hours=2, minutes=20), False)  # ISO may go between sessions
    assert not rec.console_upload_allowed(bookings, t(hours=2, minutes=20), True)  # but never while recording
    assert rec.camera_upload_allowed(bookings, t(hours=6), False, local)  # the day is over
    assert not rec.camera_upload_allowed(bookings, t(hours=6), True, local)
    tomorrow, _ = parse_bookings([booking("AB12CD", NOW + timedelta(days=1), 2)])
    assert rec.camera_upload_allowed(tomorrow, NOW, False, local)
    assert not rec.camera_upload_allowed(tomorrow, t(hours=23), False, local)  # under an hour to the next code window
    assert not rec.camera_upload_must_stop(tomorrow, t(hours=23, minutes=20), False)
    assert rec.camera_upload_must_stop(tomorrow, t(hours=23, minutes=28), False)  # 17 min before valid_from
    assert rec.camera_upload_must_stop(tomorrow, NOW, True)


def test_merge_keeps_sent_files_and_drops_vanished_pending_ones():
    sent = {**cam("A.MP4", 1), "state": "sent"}
    merged = rec.merge_manifest([sent, cam("GONE.MP4", 2)], [cam("A.MP4", 1), cam("NEW.MP4", 3)])
    assert [(f["name"], f["state"]) for f in merged] == [("A.MP4", "sent"), ("NEW.MP4", "pending")]
    assert [(f["name"]) for f in rec.merge_manifest([sent], [])] == ["A.MP4"]


def test_index_page_lists_files_and_escapes():
    files = [cam("A.MP4", 1, 2_500_000_000), rec.console_file({"path": "4K CAPTURE_2030-06-03_4_08-30-00/Audio/mix.wav", "size": 1000, "mtime_ts": 1})]
    links = {f["dest"]: f"https://s/{f['dest']}?X-Amz-Signature=a&b=<1>" for f in files}
    page = rec.render_index("AB12CD", "pl", files, links, "https://s/links.txt?x=1", "10.06.2030 12:11")
    assert "Nagrania z sesji AB12CD" in page and "2.5 GB" in page and "konsola (ISO)" in page and "10.06.2030 12:11" in page
    assert "&amp;b=&lt;1&gt;" in page and "<1>" not in page
    assert "Session recordings" in rec.render_index("AB12CD", "en", files, links, "https://s/l", "x")


def test_presigned_link_matches_the_aws_reference_signature():
    """The worked example from the S3 documentation (GET test.txt, 24 h)."""
    class Example(S3):
        def _path(self, key: str = "") -> str:
            return "/" + key

    s3 = Example(None, "https://examplebucket.s3.amazonaws.com", "", "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1")
    url = s3.presign_get("test.txt", 86400, datetime(2013, 5, 24, tzinfo=timezone.utc))
    assert url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")
    assert "X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request" in url


# ---- a whole night ----------------------------------------------------------------------------

class FakeStore:
    """The bucket, shared by the fake bridges (which fill it) and the fake S3 client HA uses."""

    def __init__(self) -> None:
        self.objects: dict[str, int] = {}
        self.bodies: dict[str, bytes] = {}

    def presign_get(self, key, expires_s, now=None, content_type=None) -> str:
        return f"https://store.test/bucket/{key}?X-Amz-Expires={expires_s}&X-Amz-Signature=sig"

    async def put(self, key, body, content_type) -> None:
        self.objects[key], self.bodies[key] = len(body), body

    async def list(self, prefix) -> dict[str, int]:
        return {k: v for k, v in self.objects.items() if k.startswith(prefix)}

    async def delete_prefix(self, prefix) -> int:
        keys = [k for k in self.objects if k.startswith(prefix)]
        for k in keys:
            del self.objects[k]
        return len(keys)


class FakeBridges:
    """a74_bridge's media actions for one camera and the console."""

    def __init__(self, hass: HomeAssistant, store: FakeStore) -> None:
        self.hass, self.store = hass, store
        self.cards = {"CAM1": []}
        self.console: list[dict] = []
        self.jobs: dict[str, dict] = {}
        self.console_job: dict = {}
        self.calls: list[str] = []
        self.hang = False  # jobs never finish
        owner = MockConfigEntry(domain="a74_bridge")
        owner.add_to_hass(hass)
        self.device = dr.async_get(hass).async_get_or_create(config_entry_id=owner.entry_id, identifiers={("a74_bridge", "SER1")}, name="CAM1")
        reg = hass.services.async_register
        for name in ("media_list", "media_upload", "media_job", "media_cancel", "console_media_list", "console_media_upload", "console_media_job", "console_media_cancel"):
            reg("a74_bridge", name, getattr(self, "_" + name), supports_response=SupportsResponse.OPTIONAL)

    def _guard(self, call: ServiceCall) -> None:
        self.calls.append(call.service)
        if self.hass.states.get(GUARD).state == "on":
            raise HomeAssistantError(f"{call.service} refused: {GUARD} is on (studio session in progress)")

    def _job(self, prefix, files, key, sizes) -> dict:
        out = []
        for f in files:
            size = sizes.get(f[key])
            if size is None:
                out.append({**f, "state": "missing", "error": "not there"})
            elif self.hang:
                out.append({**f, "state": "uploading"})
            else:
                self.store.objects[f"{prefix}/{f['dest']}"] = size
                out.append({**f, "state": "uploaded", "size": size})
        return {"id": "j", "prefix": prefix, "state": "uploading" if self.hang else "done", "files": out}

    async def _media_list(self, call):
        self._guard(call)
        return {cam: {"files": files, "count": len(files)} for cam, files in self.cards.items()}

    async def _media_upload(self, call):
        self._guard(call)
        assert call.data["device_id"] == self.device.id
        self.jobs["CAM1"] = self._job(call.data["prefix"], call.data["files"], "name", {f["name"]: f["size"] for f in self.cards["CAM1"]})
        return {"CAM1": {"job_id": "j", "state": "queued"}}

    async def _media_job(self, call):
        self.calls.append(call.service)
        return {cam: self.jobs.get(cam, {"error": "no media job yet", "status": 404}) for cam in self.cards}

    async def _media_cancel(self, call):
        self.calls.append(call.service)
        for job in self.jobs.values():
            job["state"] = "cancelled"
        return {}

    async def _console_media_list(self, call):
        self.calls.append(call.service)
        return {"files": self.console, "count": len(self.console)}

    async def _console_media_upload(self, call):
        self._guard(call)
        self.console_job = self._job(call.data["prefix"], call.data["files"], "path", {f["path"]: f["size"] for f in self.console})
        return {"CAM1": {"job_id": "j", "state": "queued"}}

    async def _console_media_job(self, call):
        self.calls.append(call.service)
        return self.console_job or {"error": "no console job yet"}

    async def _console_media_cancel(self, call):
        self.calls.append(call.service)
        self.console_job["state"] = "cancelled"
        return {}


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()
    monkeypatch.setattr(RecordingManager, "_s3", lambda self: fake)
    return fake


@pytest.fixture
def bridges(hass, store) -> FakeBridges:
    hass.states.async_set(GUARD, "off")
    return FakeBridges(hass, store)


@pytest.fixture(autouse=True)
def _time(freezer):
    freezer.move_to(NOW)


async def start(hass, entry, options=OPTIONS):
    hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data.recordings


async def minutes(hass, freezer, count):
    for _ in range(count):
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)


async def take(hass, freezer, length=10):
    hass.states.async_set(GUARD, "on")
    await hass.async_block_till_done()
    await minutes(hass, freezer, length)
    hass.states.async_set(GUARD, "off")
    await hass.async_block_till_done()


def session_files(bridges):
    bridges.cards["CAM1"] = [
        {"folder": "2030-06-03", "name": "C0001.MP4", "size": 100, "recorded_ts": at(0, 31)},  # in the take
        {"folder": "2030-06-03", "name": "C0002.MP4", "size": 70, "recorded_ts": at(-1)},  # the previous client's
        {"folder": "2030-06-03", "name": "C0003.MP4", "size": 30, "recorded_ts": at(1, 30)},  # started on the camera body
    ]
    bridges.console = [{"path": "4K CAPTURE_2030-06-03_1_08-30-00/Video/1_CAMERA 1.mp4", "capture": "x", "size": 50, "mtime_ts": at(0, 41)}]


async def test_opted_in_session_is_uploaded_reported_and_deleted_after_a_week(hass, entry, lock, feed, bridges, store, freezer):
    feed.set([booking(recording={"opt_in": True, "status": "none"})])
    ready = async_capture_events(hass, f"{DOMAIN}_recordings_ready")
    deleted = async_capture_events(hass, f"{DOMAIN}_recordings_deleted")
    manager = await start(hass, entry)
    await minutes(hass, freezer, 30)
    await take(hass, freezer)
    assert manager.journal[0]["booking_id"] == "id-AB12CD" and manager.journal[0]["stop"] is not None
    session_files(bridges)

    await minutes(hass, freezer, 85)  # 10:05, the code window is still open
    assert "media_list" not in bridges.calls and feed.recording_reports == []
    await minutes(hass, freezer, 15)

    assert [r[1] for r in feed.recording_reports] == ["uploading", "ready"]
    _ref, _status, url, expires = feed.recording_reports[-1]
    prefix = manager.find("AB12CD")["prefix"]
    assert prefix.startswith("nagrania/AB12CD-") and url.startswith(f"https://store.test/bucket/{prefix}/index.html?")
    assert sorted(k.removeprefix(prefix + "/") for k in store.objects) == [
        "CAM1_C0001.MP4", "CAM1_C0003.MP4", "index.html", "iso_083000_Video_1_CAMERA_1.mp4", "links.txt",
    ]
    page = store.bodies[f"{prefix}/index.html"].decode()
    assert "CAM1_C0001.MP4" in page and "C0002" not in page
    assert expires.startswith("2030-06-10T") and ready[0].data["files"] == 3 and "url" not in ready[0].data
    sensor = next(s for s in hass.states.async_all("sensor") if "sessions" in s.attributes)
    assert sensor.state == "1" and sensor.attributes["sessions"][0]["state"] == "ready" and url not in str(sensor.attributes)

    freezer.tick(timedelta(days=7, minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert store.objects == {} and deleted[0].data["objects"] == 5
    assert manager.find("AB12CD")["state"] == "done"


async def test_without_opt_in_nothing_is_uploaded_and_the_operator_is_told(hass, entry, lock, feed, bridges, store, freezer):
    feed.set([booking()])
    left = async_capture_events(hass, f"{DOMAIN}_recordings_left")
    manager = await start(hass, entry)
    await minutes(hass, freezer, 30)
    await take(hass, freezer)
    session_files(bridges)
    await minutes(hass, freezer, 100)
    assert "media_upload" not in bridges.calls and store.objects == {} and feed.recording_reports == []
    assert left[0].data["ref"] == "AB12CD" and left[0].data["sources"] == {"CAM1": {"count": 2, "bytes": 130}, "console": {"count": 1, "bytes": 50}}
    assert len(left) == 1  # once a day, not every minute

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "resolve_recordings", {"booking_id": "AB12CD", "action": "delete"}, blocking=True)
    feed.set([booking(recording={"opt_in": True, "status": "none"})])
    await hass.services.async_call(DOMAIN, "resolve_recordings", {"booking_id": "AB12CD", "action": "upload"}, blocking=True)
    assert feed.opt_ins == [("AB12CD", True)]
    await minutes(hass, freezer, 20)
    assert [r[1] for r in feed.recording_reports] == ["uploading", "ready"] and manager.find("AB12CD")["state"] == "ready"


async def test_opt_in_with_nothing_to_send_is_reported_empty(hass, entry, lock, feed, bridges, store, freezer):
    feed.set([booking(recording={"opt_in": True, "status": "none"})])
    empty = async_capture_events(hass, f"{DOMAIN}_recordings_empty")
    await start(hass, entry)
    await minutes(hass, freezer, 30)
    await take(hass, freezer)  # recorded, but the client took the card
    await minutes(hass, freezer, 100)
    assert [r[1] for r in feed.recording_reports] == ["uploading", "empty"]
    assert empty[0].data["had_takes"] is True and "media_upload" not in bridges.calls


async def test_recording_mode_interrupts_a_camera_upload_and_it_resumes(hass, entry, lock, feed, bridges, store, freezer):
    feed.set([booking(recording={"opt_in": True, "status": "none"})])
    manager = await start(hass, entry)
    session_files(bridges)
    bridges.hang = True
    await minutes(hass, freezer, 135)  # 10:15: inspected, camera job running and never finishing
    assert manager.active and manager.active["phase"] == "cameras"
    hass.states.async_set(GUARD, "on")
    await hass.async_block_till_done()
    await minutes(hass, freezer, 1)
    assert "media_cancel" in bridges.calls and manager.active is None
    await minutes(hass, freezer, 5)
    assert bridges.calls.count("media_upload") == 1  # nothing starts while a take is on

    bridges.hang = False
    hass.states.async_set(GUARD, "off")
    await hass.async_block_till_done()
    await minutes(hass, freezer, 12)
    assert [r[1] for r in feed.recording_reports] == ["uploading", "ready"]


async def test_three_nights_without_progress_fail_the_upload(hass, entry, lock, feed, bridges, store, freezer):
    feed.set([booking(recording={"opt_in": True, "status": "none"})])
    failed = async_capture_events(hass, f"{DOMAIN}_recordings_failed")
    manager = await start(hass, entry)
    session_files(bridges)
    bridges._job = lambda prefix, files, key, sizes: {
        "id": "j", "prefix": prefix, "state": "failed", "files": [{**f, "state": "failed", "error": "card error"} for f in files],
    }
    await minutes(hass, freezer, 150)
    assert bridges.calls.count("media_upload") == 3  # three tries a night, then it waits for the next one
    for night in range(3):
        assert not failed, night
        freezer.tick(timedelta(days=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        await minutes(hass, freezer, 12)
    assert [r[1] for r in feed.recording_reports] == ["uploading", "failed"]
    assert failed[0].data["ref"] == "AB12CD" and manager.find("AB12CD")["state"] == "failed" and store.objects == {}


async def test_disabled_pipeline_still_keeps_the_journal_and_touches_nothing(hass, entry, lock, feed, bridges, store, freezer):
    feed.set([booking(recording={"opt_in": True, "status": "none"})])
    manager = await start(hass, entry, {"dry_run": True})
    await take(hass, freezer, 2)
    session_files(bridges)
    await minutes(hass, freezer, 150)
    assert len(manager.journal) == 1 and bridges.calls == [] and feed.recording_reports == []
