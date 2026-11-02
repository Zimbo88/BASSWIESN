"""Station transactions: actual simulated state, never radio contacts."""
import asyncio
from xml.etree import ElementTree as ET

from fastapi import HTTPException
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import ConfigBackup, Device, DeviceActionJournal, Setting, Station
from basswiesn.app.routers import stations_presets as api
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.playback_safety_gate import load_playback_safety_gate
from test_maintenance_reboot_safety import ID, BUILD, info, now, volume
from test_preset_recovery_safety import REQUEST

pytestmark = pytest.mark.integration
LOCATION = "http://192.0.2.80/station"


@pytest.fixture
def rig(monkeypatch):
    class Radio:
        def __init__(self):
            self.raw = {"/info": info(), "/now_playing": now(), "/volume": volume(15),
                        "/getZone": "<zone/>", "/presets": "<presets/>",
                        "/sources": '<sources><sourceItem source="LOCAL_INTERNET_RADIO" status="READY"/></sources>'}
            self.posts, self.reads = [], []
            self.constructed = 0
            self.error = None
            self.after_write = lambda: None
            self.after_read = lambda _: None
            self.respond_with = None
            self.ignore_volume = False

        def factory(self, *args, **kwargs):
            self.constructed += 1
            return self

        async def get_xml(self, path):
            self.reads.append(path)
            self.after_read(path)
            if path == "/standby":
                self.posts.append((path, ""))
                self.raw["/now_playing"] = now()
                return "<ok/>"
            value = self.raw[path]
            if isinstance(value, Exception):
                raise value
            return value

        async def post_xml(self, path, body):
            assert device_lock(ID).owned_by_current_task()
            self.posts.append((path, body))
            root = ET.fromstring(body)
            old = ET.fromstring(self.raw["/volume"])
            value, mute = int(old.findtext("actualvolume")), old.findtext("muteenabled")
            if path == "/select":
                self.raw["/now_playing"] = self.respond_with or (
                    f'<nowPlaying source="{root.get("source")}"><playStatus>PLAY_STATE</playStatus>{body}</nowPlaying>')
            elif path == "/volume" and not self.ignore_volume:
                self.raw[path] = volume(int(root.text), mute)
            elif path == "/key" and root.get("state") == "release":
                if root.text == "MUTE":
                    self.raw["/volume"] = volume(value, "false" if mute == "true" else "true")
                elif root.text == "POWER":
                    self.raw["/now_playing"] = now("AUX", "STOP_STATE")
                elif root.text == "STOP":
                    self.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "STOP_STATE", LOCATION)
            self.after_write()
            if self.error:
                raise self.error
            return "<ok/>"

    rig = Radio()
    monkeypatch.setattr(api, "SoundTouchClient", rig.factory)
    monkeypatch.setattr(api, "_station_location_or_409", lambda *_: LOCATION)
    monkeypatch.setattr(api, "SOURCE_ACCEPTANCE_INTERVAL_SECONDS", 0)
    async def no_wait(_):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    with app_db.SessionLocal() as db:
        rig.db = db
        rig.device = Device(device_id=ID, ip_address="192.0.2.42", model="SoundTouch 30", firmware=BUILD, info_xml=info())
        rig.station = Station(name="Example", stream_url="http://example.invalid/live.mp3")
        db.add_all([rig.device, rig.station])
        db.commit()
        yield rig


def run(rig, **payload):
    try:
        return asyncio.run(api.play_station_on_device(ID, rig.station.id, payload, REQUEST, rig.db))
    except HTTPException as exc:
        return {"rejected": True, "status_code": exc.status_code, "detail": exc.detail}


@pytest.mark.parametrize("failure", ["identity", "profile", "source-account", "source-missing-status", "zone", "malformed-volume"])
def test_untrusted_start_does_not_write(rig, failure):
    if failure == "identity":
        rig.raw["/info"] = info("BB22CC33DD44")
    elif failure == "profile":
        rig.raw["/info"] = info(firmware="27.99.0")
    elif failure.startswith("source"):
        rig.raw["/sources"] = rig.raw["/sources"].replace('status="READY"', 'sourceAccount="other"' if failure == "source-account" else "")
    elif failure == "zone":
        rig.raw["/getZone"] = f'<zone master="{ID}"><member ipaddress="192.0.2.43">BB22CC33DD44</member></zone>'
    else:
        rig.raw["/volume"] = "<volume><targetvolume>15</targetvolume></volume>"
    assert run(rig).get("rejected") is True
    assert not rig.posts


def test_protected_before_transport(rig):
    rig.device.ip_address = "192.168.50.25"
    rig.db.commit()
    assert run(rig).get("rejected") is True
    assert rig.constructed == 0


def test_unknown_state_never_guesses_power_toggle(rig):
    rig.raw["/now_playing"] = TimeoutError("synthetic")
    assert run(rig, target_volume=3).get("rejected") is True
    assert not rig.posts


@pytest.mark.parametrize("field,value", [("safe_volume", True), ("safe_volume", "1"), ("target_volume", 3.5), ("dry_run", "false")])
def test_request_types_are_not_coerced_into_writes(rig, field, value):
    assert run(rig, **{field: value}).get("status_code") == 400
    assert not rig.posts


def test_matching_source_with_wrong_station_is_not_success(rig):
    rig.respond_with = now("LOCAL_INTERNET_RADIO", "PLAY_STATE", "http://example.invalid/other")
    assert run(rig).get("rejected") is True
    assert [path for path, _ in rig.posts] == ["/select"]


def test_lost_select_reply_has_no_cleanup_or_fallback_writes(rig):
    rig.error = TimeoutError("synthetic lost reply")
    result = run(rig)
    assert result.get("verified") is True  # Actual matching state, not transport success.
    assert result["transport_uncertain"] is True
    assert [path for path, _ in rig.posts] == ["/select"]


def test_unconfirmed_volume_is_not_repeated(rig):
    rig.raw["/now_playing"] = now("AUX", "STOP_STATE")
    rig.ignore_volume = True
    assert run(rig, target_volume=3).get("rejected") is True
    assert [path for path, _ in rig.posts] == ["/volume"]


def test_cancelled_safe_step_leaves_failed_gate_and_durable_start(rig):
    rig.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run(rig, safe_volume=1)
    assert len(rig.posts) == 1
    assert load_playback_safety_gate(rig.db, ID)["state"] == "FAILED"
    assert rig.db.query(ConfigBackup).count() >= 1
    assert rig.db.query(DeviceActionJournal).filter_by(verified=False).count() >= 1


def test_target_change_during_command_never_triggers_followup(rig):
    def change():
        rig.device.ip_address = "192.0.2.99"
        rig.db.commit()
    rig.after_write = change
    assert run(rig, safe_volume=1).get("rejected") is True
    assert len(rig.posts) == 1


def test_confirmed_selection_and_buffering_remain_distinct(rig):
    result = run(rig)
    assert result["verified"] is True
    assert result["audible_audio_verified"] is False
    assert rig.db.query(ConfigBackup).count() >= 1


def test_preview_is_transport_free(rig):
    assert run(rig, dry_run=True)["dry_run"] is True
    assert rig.constructed == 0


@pytest.mark.parametrize("before,after,allowed", [
    ("http://example.invalid/previous", LOCATION, False),
    (LOCATION, "http://example.invalid/other", False),
    ("http://example.invalid/previous", "http://example.invalid/previous", True),
    (LOCATION, LOCATION, True),
])
def test_buffering_progress_requires_unchanged_content(rig, before, after, allowed):
    rig.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "BUFFERING_STATE", before)

    def progress(path):
        if path == "/info" and rig.reads.count("/info") == 2:
            rig.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE", after)

    rig.after_read = progress
    result = run(rig)
    if allowed:
        assert result["verified"] is True
        assert [path for path, _ in rig.posts] == ["/select"]
    else:
        assert result.get("rejected") is True
        assert rig.posts == []


def test_buffering_never_certifies_playback(rig):
    rig.respond_with = now("LOCAL_INTERNET_RADIO", "BUFFERING_STATE", LOCATION)
    result = run(rig)
    assert result["status"] == "PENDING_READBACK"
    assert result["selection_verified"] is True
    assert result["verified"] is False


@pytest.mark.parametrize("item_xml", [f'<ContentItem source="LOCAL_INTERNET_RADIO" location="{LOCATION}"/>', "", "<ContentItem/>"])
def test_safe_station_pending_state_does_not_deadlock_provider(rig, item_xml):
    rig.raw["/now_playing"] = '<nowPlaying source="STANDBY"><ContentItem source="STANDBY"/></nowPlaying>'
    rig.respond_with = f'<nowPlaying source="LOCAL_INTERNET_RADIO">{item_xml}</nowPlaying>'
    def observe(path):
        if path == "/now_playing" and load_playback_safety_gate(rig.db, ID).get("state") == "VERIFIED":
            rig.raw[path] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE", LOCATION)
    rig.after_read = observe
    result = run(rig, safe_volume=1)
    assert result["verified"] is True
    assert result["confirmed_volume"] == 1
    assert sum(path == "/select" for path, _ in rig.posts) == 1


def test_safe_station_rebuffer_of_owned_content_is_not_foreign_write(rig):
    observations = 0
    def observe(path):
        nonlocal observations
        if path == "/now_playing" and load_playback_safety_gate(rig.db, ID).get("state") == "VERIFIED":
            observations += 1
            state = "BUFFERING_STATE" if observations == 1 else "PLAY_STATE"
            rig.raw[path] = now("LOCAL_INTERNET_RADIO", state, LOCATION)
    rig.after_read = observe
    result = run(rig, safe_volume=1)
    assert result["verified"] is True
    assert sum(path == "/select" for path, _ in rig.posts) == 1


@pytest.mark.parametrize("change", ["backup", "station", "selection"])
def test_changed_transaction_evidence_stops_followups(rig, change):
    def after():
        if change == "backup":
            rig.db.query(ConfigBackup).filter_by(path="http8090:station-start").one().content = "tampered"
            rig.db.commit()
        elif change == "station":
            rig.station.stream_url = "http://example.invalid/changed"
            rig.db.commit()
        else:
            rig.raw["/now_playing"] = now("AUX", "PLAY_STATE")
    rig.after_write = after
    assert run(rig, safe_volume=1).get("rejected") is True
    assert len(rig.posts) == 1
    assert load_playback_safety_gate(rig.db, ID)["state"] == "FAILED"


def test_final_commit_failure_is_not_success_and_never_replays(rig, monkeypatch):
    from basswiesn.app.services import station_playback
    original = station_playback.record_action
    def fail(db, **kwargs):
        if kwargs["phase"] == "STATION_COMPLETED":
            raise RuntimeError("synthetic result persistence failure")
        return original(db, **kwargs)
    monkeypatch.setattr(station_playback, "record_action", fail)
    result = run(rig)
    assert result["detail"]["code"] == "STATION_RESULT_COMMIT_FAILED"
    assert result["detail"]["verified"] is False
    assert [path for path, _ in rig.posts] == ["/select"]
    assert rig.db.query(ConfigBackup).count() >= 1


def test_timer_pending_readback_does_not_count_as_success(rig, monkeypatch):
    from basswiesn.app.models import PlayHistory, ScheduledAction
    from basswiesn.app.services.alarm_engine import trigger_schedule
    async def pending(*args, **kwargs):
        return {"ok": False, "verified": False, "status": "PENDING_READBACK"}
    monkeypatch.setattr(api, "play_station_on_device", pending)
    row = ScheduledAction(name="Example", device_ids=ID, station_id=rig.station.id, dry_run=0)
    rig.db.add(row)
    rig.db.commit()
    result = asyncio.run(trigger_schedule(row, rig.db))
    assert result["verified"] is False
    assert result["status"] == "PENDING_READBACK"
    assert rig.db.query(PlayHistory).filter_by(success=1).count() == 0


def test_timer_protected_stop_is_rejected_before_transport(rig):
    from basswiesn.app.models import ScheduledAction
    from basswiesn.app.services.alarm_engine import stop_schedule
    rig.device.ip_address = "192.168.50.25"
    rig.db.commit()
    row = ScheduledAction(name="Example", device_ids=ID, dry_run=0, stop_action="standby")
    with pytest.raises(HTTPException):
        asyncio.run(stop_schedule(row, rig.db))
    assert rig.constructed == 0


def test_timer_unconfirmed_stop_does_not_send_standby(rig, monkeypatch):
    from basswiesn.app.models import ScheduledAction
    from basswiesn.app.routers import api as controls
    from basswiesn.app.services.alarm_engine import stop_schedule
    async def pending(*args, **kwargs):
        return {"verified": False}
    async def forbidden(*args, **kwargs):
        pytest.fail("uncertain STOP must not authorize standby")
    monkeypatch.setattr(controls, "_execute_key_command", pending)
    monkeypatch.setattr(controls, "device_power_action", forbidden)
    row = ScheduledAction(name="Example", device_ids=ID, dry_run=0, stop_action="stop_standby")
    result = asyncio.run(stop_schedule(row, rig.db))
    assert result["verified"] is False


def test_stale_timer_worker_cannot_reclaim_same_occurrence(rig):
    from basswiesn.app.models import RuntimeState
    from basswiesn.app.services.alarm_engine import _claim_schedule_marker
    marker = RuntimeState(key="alarm:last_run:example", value='{"minute":"old"}')
    rig.db.add(marker)
    rig.db.commit()
    with app_db.SessionLocal() as other:
        stale = other.get(RuntimeState, marker.id)
        assert stale.value == marker.value
        assert _claim_schedule_marker(rig.db, marker, "2026-10-03T09:00") is True
        assert _claim_schedule_marker(other, stale, "2026-10-03T09:00") is False
    assert _claim_schedule_marker(rig.db, marker, "2026-10-03T09:00") is False
    assert _claim_schedule_marker(rig.db, marker, "2026-10-04T09:00") is True


@pytest.mark.parametrize("value", ["broken", "[]", "null"])
def test_corrupt_schedule_marker_cannot_authorize_replay(rig, value):
    from basswiesn.app.models import RuntimeState
    from basswiesn.app.services.alarm_engine import _claim_schedule_marker
    marker = RuntimeState(key="alarm:last_run:example", value=value)
    rig.db.add(marker)
    rig.db.commit()
    assert _claim_schedule_marker(rig.db, marker, "2026-10-03T09:00") is False
    assert not rig.posts
