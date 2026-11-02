"""Behavioral remote-key contracts; in-memory radios and isolated DB only."""
import asyncio
from contextlib import asynccontextmanager
from xml.etree import ElementTree as ET

from fastapi import HTTPException
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import ConfigBackup, Device, DeviceActionJournal, Preset
from basswiesn.app.routers import api
from basswiesn.app.services.device_policy import device_lock
from test_maintenance_reboot_safety import BUILD, ID, info, now, volume

pytestmark = pytest.mark.integration
ITEM = '<ContentItem source="LOCAL_INTERNET_RADIO" sourceAccount="" type="stationurl" location="http://example.invalid/live.mp3"><itemName>Example</itemName></ContentItem>'


@pytest.fixture
def keys(monkeypatch):
    class Radio:
        def __init__(self):
            self.raw = {"/info": info(), "/now_playing": now("LOCAL_INTERNET_RADIO", "PLAY_STATE"),
                        "/volume": volume(1), "/getZone": "<zone/>",
                        "/presets": f'<presets><preset id="1">{ITEM}</preset></presets>',
                        "/sources": '<sources><sourceItem source="LOCAL_INTERNET_RADIO" sourceAccount="" status="READY"/></sources>'}
            self.calls, self.writes, self.durable, self.owned = [], [], [], []
            self.constructed = 0
            self.fail_read = ""
            self.fail_write = ""
            self.apply = True
            self.after_read = lambda path: None
            self.after_write = lambda path: None

        def factory(self, *args, **kwargs):
            self.constructed += 1
            return self

        async def get_xml(self, path):
            self.calls.append(("GET", path))
            if path == "/standby":
                self.writes.append((path, ""))
                self.raw["/now_playing"] = '<nowPlaying source="STANDBY"><ContentItem source="STANDBY"/></nowPlaying>'
                return '<status>OK</status>'
            if path == self.fail_read:
                raise TimeoutError("secret=do-not-copy-this-upstream-message")
            data = self.raw[path]
            self.after_read(path)
            return data

        async def post_xml(self, path, xml):
            self.calls.append(("POST", path))
            self.writes.append((path, xml))
            with app_db.SessionLocal() as db:
                self.durable.append(db.query(ConfigBackup).count() > 0 and
                                    db.query(DeviceActionJournal).count() > 0)
            self.owned.append(device_lock(ID).owned_by_current_task())
            node = ET.fromstring(xml)
            if self.apply:
                if path == "/volume":
                    self.raw[path] = volume(int(node.text))
                elif path == "/select":
                    self.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE", node.get("location"))
                elif path == "/key" and node.get("state") == "press":
                    key = node.text
                    old = ET.fromstring(self.raw["/now_playing"]).findtext("playStatus")
                    if key in {"STOP", "PAUSE"} or (key == "PLAY_PAUSE" and old == "PLAY_STATE"):
                        self.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "PAUSE_STATE" if key != "STOP" else "STOP_STATE")
                    elif key in {"PLAY", "PLAY_PAUSE", "PRESET_1"}:
                        self.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE")
                    elif key == "MUTE":
                        mute = ET.fromstring(self.raw["/volume"]).findtext("muteenabled")
                        self.raw["/volume"] = volume(1, "false" if mute == "true" else "true")
            self.after_write(path)
            if path == self.fail_write:
                raise TimeoutError("secret=do-not-copy-this-upstream-message")
            return '<status>OK</status>'

    radio = Radio()
    async def no_sleep(_):
        pass
    monkeypatch.setattr(api, "SoundTouchClient", radio.factory)
    monkeypatch.setattr(api.asyncio, "sleep", no_sleep)
    with app_db.SessionLocal() as db:
        radio.db = db
        radio.device = Device(device_id=ID, ip_address="192.0.2.42", model="SoundTouch 30",
                              firmware=BUILD, info_xml=info(), reachable=True)
        db.add(radio.device)
        db.add(Preset(device_id=ID, button=1, source="LOCAL_INTERNET_RADIO", location="http://example.invalid/live.mp3", content_item_xml=ITEM))
        db.commit()
        yield radio


def run(keys, key="STOP", **payload):
    try:
        return asyncio.run(api.send_key_command(ID, {"key": key, **payload}, keys.db))
    except HTTPException as exc:
        return {"rejected": True, "status_code": exc.status_code, "detail": exc.detail}


def test_key_has_durable_intent_and_ownership(keys):
    result = run(keys)
    assert result.get("verified") is True
    assert keys.writes and all(keys.durable) and all(keys.owned)


@pytest.mark.parametrize("path", ["/info", "/now_playing", "/volume", "/getZone", "/presets"])
def test_missing_start_state_never_guesses_standby(keys, path):
    keys.fail_read = path
    result = run(keys, "PRESET_1", safe_volume=1)
    assert result.get("rejected") is True
    assert not keys.writes
    assert "do-not-copy" not in str(result)


@pytest.mark.parametrize("change", ["id", "firmware", "profile", "zone"])
def test_unverified_target_never_gets_key(keys, change):
    if change == "id": keys.raw["/info"] = info("BB22CC33DD44")
    elif change == "firmware": keys.raw["/info"] = info(firmware="27.99.0")
    elif change == "profile":
        keys.device.info_xml = keys.raw["/info"] = info().replace("SCM", "UNKNOWN")
        keys.db.commit()
    else: keys.raw["/getZone"] = '<zone master="OTHER"><member>OTHER</member></zone>'
    assert run(keys).get("rejected") is True
    assert not keys.writes


def test_play_pause_uses_previous_state(keys):
    result = run(keys, "PLAY_PAUSE")
    assert result.get("verified") is True
    assert result["readback_active"] is False


def test_power_off_is_explicit_standby_not_toggle_pair(keys):
    result = run(keys, "POWER", safe_volume=1)
    assert result["verified"] is True
    assert result["path"] == "/standby"
    assert keys.writes == [("/standby", "")]


def test_play_from_confirmed_standby_uses_native_wake_without_power(keys):
    keys.raw["/now_playing"] = '<nowPlaying source="STANDBY"><ContentItem source="STANDBY"/></nowPlaying>'
    result = run(keys, "PLAY", safe_volume=1)
    assert result["verified"] is True
    assert result["command_sent"] == "PLAY_PAUSE"
    commands = [ET.fromstring(body).text for path, body in keys.writes
                if path == "/key" and 'state="press"' in body]
    assert commands.count("PLAY_PAUSE") == 1
    assert "PLAY" not in commands and "POWER" not in commands


def test_rebuffer_before_unmute_is_observed_not_replayed(keys):
    from basswiesn.app.services.playback_safety_gate import load_playback_safety_gate
    readbacks = 0
    def observe(path):
        nonlocal readbacks
        if path == "/now_playing" and load_playback_safety_gate(keys.db, ID).get("state") == "VERIFIED":
            readbacks += 1
            if readbacks == 1:
                keys.raw[path] = now("LOCAL_INTERNET_RADIO", "BUFFERING_STATE")
            elif readbacks == 3:
                keys.raw[path] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE")
    keys.after_read = observe
    result = run(keys, "PRESET_1", safe_volume=1)
    assert result["verified"] is True
    assert readbacks >= 3
    assert sum('state="press"' in body and ">PRESET_1<" in body for _, body in keys.writes) == 1


@pytest.mark.parametrize("divergence", [None, "content", "volume"])
def test_power_wake_buffer_completion_before_unmute(keys, divergence):
    from basswiesn.app.services.playback_safety_gate import load_playback_safety_gate
    keys.raw["/now_playing"] = '<nowPlaying source="STANDBY"><ContentItem source="STANDBY"/></nowPlaying>'
    def wake(path):
        if path == "/key" and ">POWER<" in keys.writes[-1][1]:
            keys.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "BUFFERING_STATE")
    def progress(path):
        if path == "/now_playing" and load_playback_safety_gate(keys.db, ID).get("state") == "VERIFIED":
            location = "http://example.invalid/other.mp3" if divergence == "content" else "http://example.invalid/live.mp3"
            keys.raw[path] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE", location)
            if divergence == "volume":
                keys.raw["/volume"] = volume(2, "true")
    keys.after_write, keys.after_read = wake, progress
    result = run(keys, "POWER", safe_volume=1)
    assert sum('state="press"' in body and ">POWER<" in body for _, body in keys.writes) == 1
    if divergence is None:
        assert result.get("verified") is True
        assert result["confirmed_volume"] == 1
        assert ET.fromstring(keys.raw["/volume"]).findtext("muteenabled") == "false"
    else:
        assert result.get("rejected") is True
        assert ET.fromstring(keys.raw["/volume"]).findtext("muteenabled") == "true"


@pytest.mark.parametrize("key", ["PLAY", "PLAY_PAUSE", "PRESET_1"])
def test_real_standby_without_play_status_can_start_safely(keys, key):
    # Evidence-derived structure, synthetic identity. Real standby does not
    # contain the STOP_STATE that earlier simulator fixtures always supplied.
    keys.raw["/now_playing"] = (
        f'<nowPlaying deviceID="{ID}" source="STANDBY">'
        '<ContentItem source="STANDBY" isPresetable="false"/></nowPlaying>'
    )
    keys.raw["/volume"] = volume(32)
    def wake(path):
        if path == "/key" and keys.writes[-1][1].endswith(f'>{key}</key>'):
            from basswiesn.app.services.playback_safety_gate import load_playback_safety_gate
            assert load_playback_safety_gate(keys.db, ID)["state"] == "ARMED"
        if path == "/key" and keys.writes[-1][1].endswith('>POWER</key>'):
            keys.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE")
    keys.after_write = wake
    result = run(keys, key, safe_volume=1)
    assert result.get("verified") is True
    assert result["confirmed_volume"] == 1
    assert keys.writes[0] == ("/volume", "<volume>1</volume>")
    assert all(keys.durable) and all(keys.owned)


def test_preset_wakes_without_power_and_normalized_volume_stays_muted(keys):
    from basswiesn.app.services.playback_safety_gate import load_playback_safety_gate
    keys.raw["/now_playing"] = (
        '<nowPlaying source="STANDBY"><ContentItem source="STANDBY" isPresetable="false"/></nowPlaying>'
    )
    keys.raw["/volume"] = volume(32)
    gate_states = []
    def firmware(path):
        body = keys.writes[-1][1]
        if path == "/key" and 'state="press"' in body:
            gate_states.append(load_playback_safety_gate(keys.db, ID).get("state"))
            if ">PRESET_1<" in body:
                assert ET.fromstring(keys.raw["/volume"]).findtext("muteenabled") == "true"
                keys.raw["/volume"] = volume(10, "true")
            elif ">MUTE<" in body and ET.fromstring(keys.raw["/volume"]).findtext("muteenabled") == "false":
                assert gate_states[-1] == "VERIFIED"
                assert ET.fromstring(keys.raw["/volume"]).findtext("actualvolume") == "1"
    keys.after_write = firmware
    result = run(keys, "PRESET_1", safe_volume=1)
    assert result["verified"] is True
    assert "ARMED" in gate_states and "VERIFIED" in gate_states
    assert not any(">POWER<" in body for _, body in keys.writes)
    assert [body for path, body in keys.writes if path == "/volume"] == ["<volume>1</volume>"] * 2
    assert load_playback_safety_gate(keys.db, ID) == {}


def test_explicit_sparse_invalid_source_is_not_playback_confirmation():
    from basswiesn.app.services.direct_selection import Selection
    observed = Selection.read(
        '<nowPlaying source="INVALID_SOURCE"><ContentItem source="INVALID_SOURCE" isPresetable="true"/></nowPlaying>', ID)
    assert observed.play_status == "INVALID_SOURCE"
    assert not observed.same_content(Selection.content(ET.fromstring(ITEM)))


def test_delayed_selection_never_releases_provider_on_stale_standby(keys):
    from basswiesn.app.services.playback_safety_gate import load_playback_safety_gate
    standby = '<nowPlaying source="STANDBY"><ContentItem source="STANDBY"/></nowPlaying>'
    keys.raw["/now_playing"] = standby
    released = False
    observations = 0
    def write(path):
        nonlocal released
        body = keys.writes[-1][1]
        if path == "/key" and ">PRESET_1<" in body:
            keys.raw["/now_playing"] = standby
            released = 'state="release"' in body
    def observe(path):
        nonlocal observations
        if released and path == "/now_playing" and observations < 3:
            observations += 1
            assert load_playback_safety_gate(keys.db, ID)["state"] == "ARMED"
            if observations == 3:
                keys.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "BUFFERING_STATE")
                keys.raw["/volume"] = volume(10, "false")
        elif released and load_playback_safety_gate(keys.db, ID).get("state") == "VERIFIED":
            keys.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE")
    keys.after_write, keys.after_read = write, observe
    result = run(keys, "PRESET_1", safe_volume=1)
    assert result.get("verified") is True
    assert ET.fromstring(keys.raw["/volume"]).findtext("actualvolume") == "1"


@pytest.mark.parametrize("recovers", [True, False])
@pytest.mark.parametrize("item_xml", [ITEM, "", "<ContentItem/>"])
def test_post_command_incomplete_state_is_observed_not_replayed(keys, recovers, item_xml):
    reads = 0
    pending = False
    def selected(path):
        nonlocal pending
        if path == "/key" and 'state="press"' in keys.writes[-1][1] and ">PRESET_1<" in keys.writes[-1][1]:
            pending = True
            keys.raw["/now_playing"] = f'<nowPlaying source="LOCAL_INTERNET_RADIO">{item_xml}</nowPlaying>'
    def observed(path):
        nonlocal reads
        if pending and path == "/now_playing":
            reads += 1
            if recovers and reads == 2:
                keys.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE")
    keys.after_write, keys.after_read = selected, observed
    result = run(keys, "PRESET_1", safe_volume=1)
    assert sum('state="press"' in body and ">PRESET_1<" in body for _, body in keys.writes) == 1
    assert result.get("verified", False) is recovers
    if not recovers:
        assert ET.fromstring(keys.raw["/volume"]).findtext("muteenabled") == "true"


@pytest.mark.parametrize("xml", [
    '<nowPlaying source="STANDBY"/>',
    '<nowPlaying source="LOCAL_INTERNET_RADIO"><ContentItem source="LOCAL_INTERNET_RADIO"/></nowPlaying>',
    '<nowPlaying source="STANDBY"><playStatus/></nowPlaying>',
    '<nowPlaying source="STANDBY"><playStatus>PLAY_STATE</playStatus></nowPlaying>',
    '<nowPlaying source="STANDBY"><ContentItem source="AUX"/></nowPlaying>',
    '<nowPlaying source="STANDBY"><playStatus>STOP_STATE</playStatus><playStatus>STOP_STATE</playStatus></nowPlaying>',
    '<nowPlaying><ContentItem source="STANDBY"/></nowPlaying>',
])
def test_standby_compatibility_never_guesses_missing_or_conflicting_state(keys, xml):
    keys.raw["/now_playing"] = xml
    assert run(keys, "PLAY", safe_volume=1).get("rejected") is True
    assert not keys.writes


def test_wrong_station_is_not_preset_success_or_fallback_permission(keys):
    keys.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE", "http://example.invalid/other.mp3")
    keys.apply = False
    result = run(keys, "PRESET_1")
    assert result.get("rejected") is True
    assert not any(path == "/select" for path, _ in keys.writes)


def test_lost_press_reply_stops_sequence_and_observes(keys):
    keys.fail_write = "/key"
    result = run(keys)
    assert result.get("rejected") is True
    assert len(keys.writes) == 1
    last_post = max(index for index, call in enumerate(keys.calls) if call[0] == "POST")
    assert any(call == ("GET", "/now_playing") for call in keys.calls[last_post + 1:])
    assert "do-not-copy" not in str(result)


def test_mute_requires_actual_mute_readback(keys):
    keys.apply = False
    assert run(keys, "MUTE").get("rejected") is True


def test_safe_volume_mismatch_does_not_repeat_write(keys):
    keys.apply = False
    result = run(keys, "PRESET_1", safe_volume=3)
    assert result.get("rejected") is True
    assert [path for path, _ in keys.writes] == ["/volume"]


def test_backup_commit_failure_prevents_key(keys, monkeypatch):
    monkeypatch.setattr(keys.db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")))
    assert run(keys).get("rejected") is True
    assert not keys.writes


def test_readback_identity_change_stops_release(keys):
    keys.after_write = lambda path: keys.raw.update({"/info": info("BB22CC33DD44")})
    assert run(keys).get("rejected") is True
    assert len(keys.writes) == 1


def test_preset_key_no_hidden_persistent_preset_write(keys):
    result = run(keys, "PRESET_1", safe_volume=1)
    assert result.get("verified") is True
    assert all(path not in {"/storePreset", "/removePreset"} for path, _ in keys.writes)


@pytest.mark.parametrize("value", [True, 1.5, "1.5", {}, -1, 101])
def test_invalid_safe_volume_rejected_before_transport(keys, value):
    assert run(keys, "PLAY", safe_volume=value).get("status_code") == 400
    assert not keys.constructed


def test_dry_run_never_constructs_transport(keys):
    assert run(keys, "PLAY", dry_run=True)["dry_run"] is True
    assert not keys.constructed


@pytest.mark.parametrize("change", ["target", "backup", "presets", "source", "volume"])
def test_changes_while_intent_is_persisted_stop_before_write(keys, monkeypatch, change):
    original = keys.db.commit
    def commit():
        original()
        if change == "target":
            with app_db.SessionLocal() as db:
                db.query(Device).filter_by(device_id=ID).one().ip_address = "192.0.2.43"
                db.commit()
        elif change == "backup":
            with app_db.SessionLocal() as db:
                db.query(ConfigBackup).first().content += " "
                db.commit()
        elif change == "presets": keys.raw["/presets"] = "<presets/>"
        elif change == "source": keys.raw["/sources"] = '<sources/>'
        else: keys.raw["/volume"] = volume(4)
    monkeypatch.setattr(keys.db, "commit", commit)
    assert run(keys, "PRESET_1").get("rejected") is True
    assert not keys.writes


def test_final_commit_failure_is_unverified_without_replay(keys, monkeypatch):
    original = keys.db.commit
    def commit():
        if keys.db.query(DeviceActionJournal).filter_by(phase="KEY_COMPLETED").count():
            raise RuntimeError("commit unavailable")
        original()
    monkeypatch.setattr(keys.db, "commit", commit)
    result = run(keys)
    assert result["detail"]["code"] == "KEY_RESULT_COMMIT_FAILED"
    assert len(keys.writes) == 2
    assert not keys.db.query(DeviceActionJournal).filter_by(phase="KEY_COMPLETED").count()


def test_cancellation_keeps_intent_and_releases_owner(keys):
    def cancel(path):
        raise asyncio.CancelledError()
    keys.after_write = cancel
    with pytest.raises(asyncio.CancelledError):
        run(keys)
    assert len(keys.writes) == 1
    assert keys.db.query(DeviceActionJournal).filter_by(phase="KEY_WRITE_INTENT").count() == 1
    assert not device_lock(ID).locked()


def test_busy_device_does_not_construct_transport(keys):
    from basswiesn.app.services.device_transactions import device_transaction
    from basswiesn.app.services.device_ownership import DeviceBusy
    async def check():
        device_lock(ID).timeout = 0.01
        async with device_transaction([ID]):
            with pytest.raises(DeviceBusy):
                await asyncio.create_task(api.send_key_command(ID, {"key": "STOP"}, keys.db))
    asyncio.run(check())
    assert not keys.constructed


@pytest.mark.parametrize("field", ["PROTECTED_DEVICE_IDS", "PROTECTED_DEVICE_IPS"])
def test_protection_precedes_transport(keys, monkeypatch, field):
    from basswiesn.app.config import get_settings
    monkeypatch.setenv(field, ID if field.endswith("IDS") else "192.0.2.42")
    get_settings.cache_clear()
    assert run(keys).get("status_code") == 403
    assert not keys.constructed


@pytest.mark.parametrize("key", ["NEXT_TRACK", "SHUFFLE_ON", "REPEAT_ALL"])
def test_ack_without_established_state_contract_is_not_verified(keys, key):
    result = run(keys, key)
    assert result["detail"]["verified"] is False
    assert result["detail"]["reconciliation_required"] is True
    assert len(keys.writes) == 2


def test_known_readback_does_not_claim_audible_audio(keys):
    result = run(keys, "PRESET_1")
    assert result["verified"] is True
    assert result["audible_audio_verified"] is False


def test_browser_payload_cannot_expand_zone_scope(keys):
    keys.raw["/getZone"] = f'<zone master="{ID}"><member>OTHER</member></zone>'
    assert run(keys, participants=[ID, "OTHER"]).get("rejected") is True
    assert not keys.writes


@pytest.mark.parametrize("bad_member", [False, True])
def test_server_scoped_zone_key_checks_every_participant(keys, monkeypatch, bad_member):
    member_id = "BB22CC33DD44"
    member = Device(device_id=member_id, ip_address="192.0.2.43", model="SoundTouch 30",
                    firmware=BUILD, info_xml=info(member_id))
    keys.db.add(member)
    keys.db.commit()
    keys.raw["/getZone"] = f'<zone master="{ID}"><member ipaddress="192.0.2.43">{member_id}</member></zone>'
    reads = []
    class Member:
        async def get_xml(self, path):
            reads.append(path)
            if path == "/info": return info("WRONG" if bad_member else member_id)
            if path == "/getZone": return f'<zone master="{ID}"/>'
            return keys.raw[path]
        async def post_xml(self, path, xml):
            pytest.fail("Master key must not issue member writes")
    monkeypatch.setattr(api, "SoundTouchClient", lambda ip, **kwargs: keys if ip == "192.0.2.42" else Member())
    async def perform():
        return await api._execute_key_command(ID, {"key": "STOP"}, keys.db, participants=[keys.device, member])
    if bad_member:
        with pytest.raises(HTTPException):
            asyncio.run(perform())
        assert not keys.writes
    else:
        assert asyncio.run(perform())["verified"] is True
        assert "/info" in reads
        assert len(keys.writes) == 2


@pytest.mark.parametrize("failure", ["none", "policy", "uri", "lost_reply"])
def test_only_definite_matching_invalid_source_can_use_one_fallback(keys, monkeypatch, failure):
    from types import SimpleNamespace
    from basswiesn.app.services import key_actions
    monkeypatch.setattr(key_actions, "validate_outbound_http_url", lambda url: SimpleNamespace(ok=failure != "uri"))
    def invalid_source(path):
        if path == "/key" and ">PRESET_1</key>" in keys.writes[-1][1]:
            keys.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "INVALID_SOURCE")
    keys.after_write = invalid_source
    if failure == "policy":
        keys.device.auto_restore_allowed = False
        keys.db.commit()
    if failure == "lost_reply": keys.fail_write = "/select"
    result = run(keys, "PRESET_1", safe_volume=1)
    count = sum(path == "/select" for path, _ in keys.writes)
    assert count == (failure in {"none", "lost_reply"})
    assert result.get("verified", False) == (failure == "none")
    if failure == "lost_reply":
        assert result["detail"]["code"] == "KEY_OUTCOME_UNKNOWN"


@pytest.mark.parametrize("key", ["PRESET_1", "POWER"])
def test_known_standby_sets_and_reads_volume_before_wake(keys, key):
    keys.raw["/now_playing"] = now()
    keys.raw["/volume"] = volume(9)
    def wake(path):
        if path == "/key" and ">POWER<" in keys.writes[-1][1]:
            keys.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE")
    keys.after_write = wake
    assert run(keys, key, safe_volume=1).get("verified") is True
    assert keys.writes[0] == ("/volume", "<volume>1</volume>")
    assert all(keys.owned) and all(keys.durable)


@pytest.mark.parametrize("key,start_volume,target", [("MUTE", 1, 1), ("VOLUME_UP", 1, 2), ("VOLUME_DOWN", 2, 1)])
def test_volume_and_mute_keys_require_actual_changes(keys, key, start_volume, target):
    keys.raw["/volume"] = volume(start_volume)
    def action(path):
        if path == "/key" and 'state="press"' in keys.writes[-1][1]:
            keys.raw["/volume"] = volume(target, "true" if key == "MUTE" else "false")
    keys.after_write = action
    assert run(keys, key).get("verified") is True


def test_explicit_manual_stop_is_not_restarted_by_preset_fallback(keys):
    keys.after_write = lambda path: keys.raw.update({"/now_playing": now("LOCAL_INTERNET_RADIO", "STOP_STATE")})
    assert run(keys, "PRESET_1").get("rejected") is True
    assert not any(path == "/select" for path, _ in keys.writes)
