"""Manual legacy reboot: fake radios only, never hardware or CLI transport."""
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import ConfigBackup, Device, DeviceActionJournal, Setting, Station
from basswiesn.app.services import maintenance_reboot as service

pytestmark = pytest.mark.integration
ID = "AA11BB22CC33"
BUILD = "27.0.6.46330.5043500"


def info(identity=ID, firmware=BUILD):
    return (f'<info deviceID="{identity}"><type>SoundTouch 30</type><variant>mojo</variant>'
            '<moduleType>SCM</moduleType><productID>0x0924</productID>'
            f'<components><component><softwareVersion>{firmware}</softwareVersion></component></components></info>')


def volume(value=1, muted="false"):
    return f'<volume><actualvolume>{value}</actualvolume><muteenabled>{muted}</muteenabled></volume>'


def now(source="STANDBY", status="STOP_STATE", location="http://example.invalid/live.mp3", account=""):
    item = (f'<ContentItem source="{source}" sourceAccount="{account}" type="stationurl" '
            f'location="{location}"/>' if source != "STANDBY" else "")
    return f'<nowPlaying source="{source}">{item}<playStatus>{status}</playStatus></nowPlaying>'


@pytest.fixture
def rig(monkeypatch):
    class Radio:
        def __init__(self):
            self.raw = {"/info": info(), "/now_playing": now(), "/volume": volume(),
                        "/getZone": "<zone/>", "/presets": "<presets/>"}
            self.after_reboot = {}
            self.writes = []
            self.reads = []
            self.cli = []
            self.constructed = 0
            self.offline = False
            self.cli_error = None
            self.durable = False
            self.after_read = lambda path: None
            self.after_write = lambda path: None
            self.after_cli = lambda: None
            self.apply_writes = False
            self.write_error = None

        def factory(self, *args, **kwargs):
            self.constructed += 1
            return self

        async def get_xml(self, path):
            self.reads.append(path)
            value = self.raw[path]
            self.after_read(path)
            return value

        async def post_xml(self, path, xml):
            # Deliberately do not make a write imply actual state. Tests must
            # fail when a command is accepted but actual readback disagrees.
            self.writes.append((path, xml))
            if self.apply_writes:
                root = ET.fromstring(xml)
                if path == "/volume":
                    self.raw[path] = volume(int(root.text), ET.fromstring(self.raw[path]).findtext("muteenabled"))
                elif path == "/select":
                    self.raw["/now_playing"] = now(root.get("source"), "PLAY_STATE", root.get("location"), root.get("sourceAccount", ""))
                elif path == "/key" and root.get("state") == "press":
                    if root.text == "MUTE":
                        old = ET.fromstring(self.raw["/volume"])
                        self.raw["/volume"] = volume(int(old.findtext("actualvolume")),
                            "false" if old.findtext("muteenabled") == "true" else "true")
                    elif root.text == "POWER":
                        self.raw["/now_playing"] = now()
                    elif root.text == "PRESET_1":
                        self.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE")
            self.after_write(path)
            if self.write_error:
                raise self.write_error
            return '<status>OK</status>'

        async def send_cli(self, ip, commands, **kwargs):
            with app_db.SessionLocal() as check:
                self.durable = (check.query(ConfigBackup).count() > 0 and
                    check.query(DeviceActionJournal).filter_by(phase="reboot_requested").count() == 1)
            self.cli.append((ip, commands))
            self.raw.update(self.after_reboot)
            self.after_cli()
            if self.cli_error:
                raise self.cli_error
            return "command accepted"

        async def port_open(self, *args):
            self.offline = not self.offline
            return not self.offline

    radio = Radio()
    async def preflight(*args, **kwargs):
        return {"ok": True}
    async def sleep(seconds):
        return None
    monkeypatch.setattr(service, "action_preflight", preflight)
    monkeypatch.setattr(service, "port_open", radio.port_open)
    monkeypatch.setattr(service.asyncio, "sleep", sleep)
    with app_db.SessionLocal() as db:
        device = Device(device_id=ID, ip_address="192.0.2.42", model="SoundTouch 30",
                        firmware=BUILD, info_xml=info(), reachable=True)
        db.add_all([device, Setting(key="lab_mode", value="true")])
        db.commit()
        radio.db, radio.device = db, device
        yield radio


def run(radio, **kwargs):
    return asyncio.run(service.run_maintenance_reboot(
        radio.device, radio.db, trigger="manual_lab", send_cli=radio.send_cli,
        client_factory=radio.factory, **kwargs))


def test_idle_exact_readback_is_required(rig):
    result = run(rig)
    assert result["ok"] is True
    assert rig.durable
    assert len(rig.cli) == 1
    assert not rig.writes  # matching state needs no gratuitous volume write


@pytest.mark.parametrize("path,xml", [
    ("/now_playing", now("AUX", "PLAY_STATE")),
    ("/volume", volume(9)),
    ("/volume", volume(1, "true")),
    ("/getZone", '<zone master="OTHER"><member>OTHER</member></zone>'),
    ("/presets", '<presets><preset id="1"/></presets>'),
])
def test_wrong_final_state_never_advances_success(rig, path, xml):
    previous = datetime(2026, 1, 1, tzinfo=UTC)
    rig.device.maintenance_last_success_at = previous
    rig.db.commit()
    rig.after_reboot[path] = xml
    result = run(rig)
    assert result["ok"] is False
    rig.db.refresh(rig.device)
    assert rig.device.maintenance_last_success_at.replace(tzinfo=UTC) == previous


@pytest.mark.parametrize("when", ["before", "returned"])
def test_reused_ip_never_receives_restore_commands(rig, when):
    if when == "before":
        rig.raw["/info"] = info("BB22CC33DD44")
    else:
        rig.after_reboot["/info"] = info("BB22CC33DD44")
    result = run(rig)
    assert result["ok"] is False
    assert not rig.writes
    assert len(rig.cli) == (when == "returned")


def test_unknown_profile_denies_reboot(rig):
    rig.raw["/info"] = info(firmware="27.99.0")
    assert run(rig)["ok"] is False
    assert not rig.cli and not rig.writes


@pytest.mark.parametrize("xml", ["<volume/>", "<volume><targetvolume>1</targetvolume></volume>",
    volume(101), volume(1, "unknown"), volume(1).replace("</volume>", "<actualvolume>2</actualvolume></volume>")])
def test_unknown_start_state_denies_reboot(rig, xml):
    rig.raw["/volume"] = xml
    assert run(rig)["ok"] is False
    assert not rig.cli


def test_no_wait_never_claims_restore_or_sends_followup_writes(rig):
    result = run(rig, wait_for_return=False)
    assert result["ok"] is False
    assert result["code"] == "REBOOT_PENDING_READBACK"
    assert not rig.writes
    assert rig.device.maintenance_last_success_at is None


def test_paused_source_is_not_resumed_by_guess_after_reboot(rig):
    rig.raw['/now_playing'] = now('LOCAL_INTERNET_RADIO', 'PAUSE_STATE')
    rig.after_reboot['/now_playing'] = now()
    result = run(rig)
    assert not result['ok'] and rig.device.maintenance_last_success_at is None
    assert not rig.writes


def test_disabled_legacy_scheduler_is_a_noop_without_device_calls(rig):
    assert asyncio.run(service.run_due_maintenance(rig.db)) == []
    asyncio.run(service.maintenance_reboot_loop(asyncio.Event()))
    assert not rig.cli and not rig.writes and rig.constructed == 0


def test_lost_cli_reply_is_uncertain_not_offline_or_retried(rig):
    rig.cli_error = TimeoutError("private upstream detail")
    result = run(rig)
    assert result["ok"] is False
    assert result["reconciliation_required"] is True
    assert result["code"] == "REBOOT_OUTCOME_UNKNOWN"
    assert "private upstream detail" not in str(result)
    assert len(rig.cli) == 1 and not rig.writes


def test_play_state_for_different_content_is_not_restore(rig):
    rig.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE")
    rig.raw["/presets"] = ('<presets><preset id="1"><ContentItem source="LOCAL_INTERNET_RADIO" '
        'sourceAccount="" location="http://example.invalid/live.mp3"/></preset></presets>')
    rig.after_reboot["/now_playing"] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE", account="wrong-account")
    assert run(rig)["ok"] is False
    assert rig.device.maintenance_last_success_at is None


@pytest.mark.parametrize("mode", ["standby", "preset", "select"])
def test_supported_restore_is_verified_and_volume_precedes_playback(rig, mode):
    rig.apply_writes = True
    rig.after_reboot["/volume"] = volume(7, "true")
    if mode == "standby":
        rig.after_reboot["/now_playing"] = now("AUX", "PLAY_STATE")
    else:
        rig.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE")
        rig.after_reboot["/now_playing"] = now()
        if mode == "preset":
            rig.raw["/presets"] = ('<presets><preset id="1"><ContentItem source="LOCAL_INTERNET_RADIO" '
                'sourceAccount="" location="http://example.invalid/live.mp3"/></preset></presets>')
    result = run(rig)
    assert result["ok"] and result["verified"]
    assert result["audible_audio_verified"] is False
    assert rig.writes[0] == ("/volume", "<volume>1</volume>")
    assert rig.raw["/volume"] == volume(1)
    assert rig.device.maintenance_last_success_at is not None
    rows = rig.db.query(DeviceActionJournal).all()
    assert all(not row.verified for row in rows if row.phase != "completed")
    assert [row.verified for row in rows if row.phase == "completed"] == [True]


@pytest.mark.parametrize("phase", ["snapshot_captured", "reboot_requested", "restoring_state", "completed"])
def test_commit_failure_never_claims_success_or_replays(rig, monkeypatch, phase):
    rig.after_reboot["/volume"] = volume(7)
    rig.apply_writes = True
    original = rig.db.commit
    def commit():
        if rig.device.maintenance_phase == phase:
            raise RuntimeError("private DB failure")
        original()
    monkeypatch.setattr(rig.db, "commit", commit)
    result = run(rig)
    assert result["ok"] is False
    assert len(rig.cli) <= 1
    assert len(rig.writes) <= 1
    if phase in {"snapshot_captured", "reboot_requested"}:
        assert not rig.cli and not rig.writes
    if phase == "restoring_state":
        assert not rig.writes
    rig.db.refresh(rig.device)
    assert rig.device.maintenance_last_success_at is None
    assert "private DB failure" not in str(result)


@pytest.mark.parametrize("kind", ["deleted", "changed", "identity"])
def test_changed_backup_blocks_command(rig, monkeypatch, kind):
    original = rig.db.commit
    def commit():
        original()
        if rig.device.maintenance_phase == "snapshot_captured":
            with app_db.SessionLocal() as other:
                backup = other.query(ConfigBackup).one()
                if kind == "deleted":
                    other.delete(backup)
                elif kind == "changed":
                    backup.content += "changed"
                else:
                    backup.device_id = "BB22CC33DD44"
                other.commit()
    monkeypatch.setattr(rig.db, "commit", commit)
    assert run(rig)["ok"] is False
    assert not rig.cli and not rig.writes


@pytest.mark.parametrize("phase", ["snapshot_captured", "reboot_requested", "restoring_state"])
def test_state_changes_at_commit_are_not_overwritten(rig, monkeypatch, phase):
    if phase == "restoring_state":
        rig.after_reboot["/volume"] = volume(7)
    original = rig.db.commit
    def commit():
        original()
        if rig.device.maintenance_phase == phase:
            rig.raw["/volume"] = volume(8)
    monkeypatch.setattr(rig.db, "commit", commit)
    assert run(rig)["ok"] is False
    assert not rig.writes
    assert len(rig.cli) == (phase == "restoring_state")


@pytest.mark.parametrize("field,value", [("ip_address", "192.0.2.43"), ("info_xml", info("OTHER")),
                                      ("model", "unknown"), ("firmware", "unknown")])
def test_cached_target_cannot_change_after_start(rig, monkeypatch, field, value):
    original = rig.db.commit
    def commit():
        original()
        if rig.device.maintenance_phase == "snapshot_captured":
            with app_db.SessionLocal() as other:
                setattr(other.query(Device).filter_by(device_id=ID).one(), field, value)
                other.commit()
    monkeypatch.setattr(rig.db, "commit", commit)
    result = run(rig)
    assert result["code"] == "IDENTITY_MISMATCH"
    assert not rig.cli and not rig.writes


def test_restore_lost_reply_stops_composite_and_keeps_uncertain_result(rig):
    rig.after_reboot["/volume"] = volume(7)
    rig.apply_writes = True
    rig.write_error = TimeoutError("private response")
    result = run(rig)
    assert not result["ok"] and result["reconciliation_required"]
    assert len(rig.writes) == 1 and len(rig.cli) == 1
    assert rig.device.maintenance_last_success_at is None
    assert result["last_observation"]["volume"] == 1


def test_restore_lost_reply_and_readback_failure_remain_uncertain(rig):
    rig.after_reboot["/volume"] = volume(7)
    rig.write_error = TimeoutError("private response")
    def no_readback(path):
        rig.raw["/info"] = "<invalid>"
    rig.after_write = no_readback
    result = run(rig)
    assert not result["ok"] and result["readback_unavailable"]
    assert result["reconciliation_required"] and len(rig.writes) == 1


def test_cancelled_reboot_retains_intent_and_releases_ownership(rig):
    from basswiesn.app.services.device_transactions import device_transaction
    rig.cli_error = asyncio.CancelledError()
    async def attempt():
        with pytest.raises(asyncio.CancelledError):
            await service.run_maintenance_reboot(rig.device, rig.db, trigger="manual_lab",
                send_cli=rig.send_cli, client_factory=rig.factory)
        async with device_transaction([ID]):
            pass
    asyncio.run(attempt())
    assert rig.db.query(DeviceActionJournal).filter_by(phase="reboot_requested").count() == 1
    assert rig.db.query(DeviceActionJournal).filter_by(verified=True).count() == 0
    assert rig.durable and len(rig.cli) == 1 and not rig.writes


@pytest.mark.parametrize("protected", ["id", "ip"])
def test_protection_denies_before_transport(rig, monkeypatch, protected):
    monkeypatch.setenv("PROTECTED_DEVICE_IDS" if protected == "id" else "PROTECTED_DEVICE_IPS",
                       ID if protected == "id" else "192.0.2.42")
    from basswiesn.app.config import get_settings
    get_settings.cache_clear()
    assert run(rig)["ok"] is False
    assert rig.constructed == 0 and not rig.cli


@pytest.mark.parametrize("path,xml", [
    ("/now_playing", '<nowPlaying deviceID="OTHER" source="STANDBY"><playStatus>STOP_STATE</playStatus></nowPlaying>'),
    ("/presets", '<!DOCTYPE presets [<!ENTITY unsafe "x">]><presets>&unsafe;</presets>'),
    ("/info", "<info>"), ("/presets", "x" * 131073),
    ("/getZone", '<zone master="OTHER"><member>OTHER</member></zone>'),
])
def test_invalid_or_grouped_start_never_reboots(rig, path, xml):
    rig.raw[path] = xml
    assert run(rig)["ok"] is False
    assert not rig.cli and not rig.writes


def test_no_offline_transition_cannot_be_called_success(rig, monkeypatch):
    ticks = iter([0, 0, 100000])
    monkeypatch.setattr(service, "monotonic", lambda: next(ticks, 100000))
    async def always_online(*args):
        return True
    monkeypatch.setattr(service, "port_open", always_online)
    result = run(rig)
    assert result["code"] == "REBOOT_OUTCOME_UNKNOWN"
    assert not rig.writes and rig.device.maintenance_last_success_at is None


def test_corrupted_backup_after_reboot_never_commits_success(rig):
    def corrupt():
        with app_db.SessionLocal() as other:
            other.query(ConfigBackup).one().content += "changed"
            other.commit()
    rig.after_cli = corrupt
    assert not run(rig)["ok"]
    assert rig.device.maintenance_last_success_at is None


@pytest.mark.parametrize("path,xml", [("/presets", "<wrong/>"),
    ("/now_playing", '<nowPlaying source="STANDBY"><playStatus/></nowPlaying>')])
def test_missing_state_or_wrong_root_is_not_a_valid_backup(rig, path, xml):
    rig.raw[path] = xml
    assert not run(rig)["ok"]
    assert not rig.cli


@pytest.mark.parametrize("fault", ["busy", "namespace"])
def test_ownership_failure_preserves_actionable_code(rig, monkeypatch, fault):
    from basswiesn.app.services.device_ownership import DeviceBusy, DeviceLockUnavailable
    @asynccontextmanager
    async def refused(*args):
        raise DeviceBusy() if fault == "busy" else DeviceLockUnavailable()
        yield  # pragma: no cover - contextmanager protocol, never acquired
    monkeypatch.setattr(service, "device_transaction", refused)
    assert run(rig)["code"] == ("DEVICE_BUSY" if fault == "busy" else "DEVICE_LOCK_UNAVAILABLE")
    assert not rig.cli and rig.constructed == 0


def test_disabled_lab_denies_before_client(rig):
    rig.db.query(Setting).filter_by(key="lab_mode").one().value = "false"
    rig.db.commit()
    assert run(rig)["code"] == "LAB_MODE_REQUIRED"
    assert rig.constructed == 0


def test_preflight_offline_denies_before_client(rig, monkeypatch):
    async def unavailable(*args, **kwargs):
        return {"ok": False, "code": "DEVICE_UNREACHABLE", "message": "unavailable"}
    monkeypatch.setattr(service, "action_preflight", unavailable)
    assert run(rig)["code"] == "DEVICE_UNREACHABLE"
    assert rig.constructed == 0


def test_compatibility_snapshot_is_identity_checked_and_read_only(rig):
    snapshot = asyncio.run(service.capture_reboot_snapshot(rig.device, rig.db, client_factory=rig.factory))
    assert snapshot["source"] == "STANDBY" and snapshot["volume"] == 1
    assert rig.reads[0] == "/info"
    assert not rig.cli and not rig.writes
    assert {"captured_at", "reachable", "now_playing", "station_id", "station_name",
            "stream_url", "zone_xml", "zone_master_id"} <= snapshot.keys()


def test_snapshot_retains_existing_station_labels_and_content_fields(rig):
    rig.db.add(Station(name="Example station", stream_url="http://example.invalid/live.mp3", internal=False))
    rig.db.commit()
    rig.raw["/now_playing"] = now("LOCAL_INTERNET_RADIO", "PLAY_STATE")
    snapshot = asyncio.run(service.capture_reboot_snapshot(rig.device, rig.db, client_factory=rig.factory))
    assert snapshot["station_name"] == "Example station"
    assert snapshot["station_id"] is not None
    assert snapshot["content_item"]["type"] == "stationurl"


def test_duplicate_slot_never_becomes_a_physical_preset_restore(rig):
    rig.raw["/now_playing"] = now("AIRPLAY", "PLAY_STATE")
    rig.raw["/presets"] = ('<presets><preset id="1"><ContentItem source="AIRPLAY" sourceAccount="" '
        'location="http://example.invalid/live.mp3"/></preset><preset id="1"><ContentItem source="AUX"/></preset></presets>')
    rig.after_reboot["/now_playing"] = now()
    assert not run(rig)["ok"]
    assert not rig.writes


@pytest.mark.parametrize("kind", ["paused", "external", "type", "slot"])
def test_unrestorable_or_unsupported_state_is_not_replaced_with_guesses(rig, kind):
    rig.raw["/now_playing"] = now("AIRPLAY" if kind == "external" else "LOCAL_INTERNET_RADIO",
                                 "PAUSE_STATE" if kind == "paused" else "PLAY_STATE")
    if kind == "type":
        rig.raw["/now_playing"] = rig.raw["/now_playing"].replace('type="stationurl"', 'type="unknown"')
    if kind == "slot":
        rig.raw["/presets"] = ('<presets><preset id="7"><ContentItem source="LOCAL_INTERNET_RADIO" '
            'sourceAccount="" location="http://example.invalid/live.mp3"/></preset></presets>')
    rig.after_reboot["/now_playing"] = now()
    result = run(rig)
    assert not result["ok"] and result["reconciliation_required"]
    assert not any("PRESET_7" in xml for path, xml in rig.writes)
    if kind != "slot":
        assert not rig.writes


def test_default_cli_adapter_is_used_once_with_fixed_command(rig, monkeypatch):
    from basswiesn.app.routers import api
    monkeypatch.setattr(api, "_send_cli17000", rig.send_cli)
    result = asyncio.run(service.run_maintenance_reboot(rig.device, rig.db, trigger="manual_lab",
        client_factory=rig.factory))
    assert result["ok"]
    assert rig.cli == [("192.0.2.42", ["sys reboot"])]


def test_secondary_database_failure_does_not_replay_command(rig, monkeypatch):
    def unavailable():
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(rig.db, "commit", unavailable)
    assert not run(rig)["ok"]
    assert not rig.cli and not rig.writes


def test_topology_changes_during_restore_stop_before_next_write(rig):
    rig.apply_writes = True
    rig.after_reboot["/volume"] = volume(7, "true")
    def altered(path):
        rig.raw["/getZone"] = f'<zone master="{ID}"/>'
    rig.after_write = altered
    assert not run(rig)["ok"]
    assert len(rig.writes) == 1
