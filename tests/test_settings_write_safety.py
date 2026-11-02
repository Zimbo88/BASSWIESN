"""No hardware: lost replies, changing identity and partial settings writes."""

import json
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import ConfigBackup, Device, DeviceActionJournal, Setting
from basswiesn.app.routers import api

pytestmark = pytest.mark.integration
DEVICE_ID = "AA11BB22CC33"
BUILD = "27.0.6.46330.5043500"


def info(name="Before", device_id=DEVICE_ID, build=BUILD):
    return (f'<info deviceID="{device_id}"><name>{name}</name><type>SoundTouch 30</type>'
            f'<variant>mojo</variant><moduleType>SCM</moduleType><productID>0x0924</productID>'
            f'<components><component><softwareVersion>{build}</softwareVersion></component></components></info>')


class Radio:
    def __init__(self):
        self.calls = []
        self.name = "Before"
        self.volume = 5
        self.bass = -2
        self.device_id = DEVICE_ID
        self.build = BUILD
        self.after_write = False
        self.lost_reply = False
        self.fail_readback = False
        self.ignore_write = False
        self.fail_bass = False
        self.switch_identity = False
        self.constructors = []

    def factory(self, ip, **kwargs):
        self.constructors.append((ip, kwargs))
        return self

    async def get_xml(self, path):
        self.calls.append(("GET", path))
        if self.after_write and self.fail_readback:
            raise httpx.ReadTimeout("simulated missing readback")
        if path == "/info":
            identity = "BB22CC33DD44" if self.after_write and self.switch_identity else self.device_id
            return info(self.name, identity, self.build)
        if path == "/volume":
            return f'<volume deviceID="{self.device_id}"><targetvolume>1</targetvolume><actualvolume>{self.volume}</actualvolume></volume>'
        if path == "/bass":
            return f'<bass><targetbass>-3</targetbass><actualbass>{self.bass}</actualbass></bass>'
        raise AssertionError(f"Unplanned probe {path}")

    async def post_xml(self, path, xml):
        self.calls.append(("POST", path))
        # Backup and intent must survive even if the process dies in transport.
        with app_db.SessionLocal() as db:
            assert db.query(ConfigBackup).count() > 0, "missing durable START backup"
            assert db.query(DeviceActionJournal).filter(DeviceActionJournal.phase == "settings_intent").count() > 0
        self.after_write = True
        if not self.ignore_write:
            if path == "/name":
                self.name = ET.fromstring(xml).text
            elif path == "/volume":
                self.volume = int(ET.fromstring(xml).text)
            elif path == "/bass" and not self.fail_bass:
                self.bass = int(ET.fromstring(xml).text)
        if self.lost_reply:
            raise httpx.ReadTimeout("simulated lost POST reply")
        return "<status>OK</status>"


@pytest.fixture
def rig(monkeypatch):
    radio = Radio()
    with app_db.SessionLocal() as db:
        db.add(Device(device_id=DEVICE_ID, ip_address="192.0.2.42", name="Before",
                      model="SoundTouch 30", firmware=BUILD, info_xml=info()))
        db.commit()
    monkeypatch.setattr(api, "SoundTouchClient", radio.factory)

    async def no_ssh(*args, **kwargs):
        raise AssertionError("ordinary settings must not touch persistent SSH files")

    monkeypatch.setattr(api, "_read_ssh_hosts", no_ssh)
    monkeypatch.setattr(api, "_run_ssh_readonly_command", no_ssh)
    with TestClient(create_web_app(background_tasks=False), raise_server_exceptions=False) as client:
        yield radio, client


def post(client, suffix, payload):
    return client.post(f"/api/devices/{DEVICE_ID}/{suffix}", json=payload)


def test_rename_is_backed_up_identity_bound_and_readback_verified(rig):
    radio, client = rig
    response = post(client, "rename", {"name": "After", "dry_run": False})
    assert response.status_code == 200, response.text
    assert response.json()["verified"] == "After"
    assert response.json()["persistence"]["updated"] is False
    assert radio.constructors[0][1]["device_id"] == DEVICE_ID
    assert radio.calls[0] == ("GET", "/info")
    with app_db.SessionLocal() as db:
        assert db.query(Device).one().name == "After"
        assert db.query(DeviceActionJournal).filter_by(phase="settings_verified", verified=True).count() == 1


@pytest.mark.parametrize("failure", ["fail_readback", "ignore_write", "switch_identity"])
def test_rename_never_commits_an_unconfirmed_name(rig, failure):
    radio, client = rig
    setattr(radio, failure, True)
    response = post(client, "rename", {"name": "After", "dry_run": False})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["status"] == "DIVERGED"
    assert radio.calls.count(("POST", "/name")) == 1
    with app_db.SessionLocal() as db:
        assert db.query(Device).one().name == "Before"
        assert db.query(DeviceActionJournal).filter_by(phase="settings_diverged", verified=False).count() == 1


@pytest.mark.parametrize("suffix,payload", [
    ("rename", {"name": "After", "dry_run": False}),
    ("settings/volume", {"value": 1, "dry_run": False}),
    ("settings-apply", {"values": {"volume": 1}}),
])
def test_every_write_entry_obeys_ip_guard_before_transport(rig, suffix, payload):
    radio, client = rig
    with app_db.SessionLocal() as db:
        db.add(Setting(key="ip_write_guard", value="true"))
        db.commit()
    response = post(client, suffix, payload)
    assert response.status_code == 403
    assert radio.constructors == []
    assert radio.calls == []


@pytest.mark.parametrize("attribute,value", [("device_id", "OTHER"), ("build", "27.0.7.unknown")])
def test_identity_and_unknown_firmware_stop_before_any_write(rig, attribute, value):
    radio, client = rig
    setattr(radio, attribute, value)
    response = post(client, "settings/volume", {"value": 1, "dry_run": False})
    assert response.status_code == 409, response.text
    assert not any(method == "POST" for method, _ in radio.calls)


def test_actual_volume_not_target_and_no_blind_retry(rig):
    radio, client = rig
    radio.ignore_write = True
    response = post(client, "settings/volume", {"value": 1, "dry_run": False})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["status"] == "DIVERGED"
    assert radio.calls.count(("POST", "/volume")) == 1


def test_lost_post_reply_is_reconciled_by_readback_without_replay(rig):
    radio, client = rig
    radio.lost_reply = True
    response = post(client, "settings/volume", {"value": 1, "dry_run": False})
    assert response.status_code == 200, response.text
    assert response.json()["verified"] == 1
    assert response.json()["transport_uncertain"] is True
    assert radio.calls.count(("POST", "/volume")) == 1


def test_batch_partial_failure_reports_verified_prefix_and_stops(rig):
    radio, client = rig
    radio.fail_bass = True
    response = post(client, "settings-apply", {"values": {"volume": 1, "bass": -3, "name": "After"}})
    assert response.status_code == 409, response.text
    result = response.json()["detail"]
    assert result["applied"] == 1
    assert result["changed"][0]["setting"] == "volume"
    assert result["failed_setting"] == "bass"
    assert ("POST", "/name") not in radio.calls


def test_local_art_mode_never_creates_radio_transport(rig):
    radio, client = rig
    response = post(client, "settings-apply", {"values": {"station_art_mode": "no_station_logo"}})
    assert response.status_code == 200, response.text
    assert response.json()["preset_sync_required"] is True
    assert radio.constructors == []


@pytest.mark.parametrize("setting,value", [
    ("rebroadcastlatencymode", 'x" /><injected/>'), ("rebroadcastlatencymode", "UNKNOWN"),
    ("clockDisplay", "not-a-bool"), ("volume", "bad"), ("volume", True), ("bass", None),
    ("clockConfig", []),
])
def test_invalid_setting_is_client_error_without_transport(rig, setting, value):
    radio, client = rig
    response = post(client, f"settings/{setting}", {"value": value, "dry_run": False})
    assert response.status_code == 400, response.text
    assert radio.constructors == []


def test_precondition_mismatch_is_not_overwritten(rig):
    radio, client = rig
    response = post(client, "settings-apply", {"values": {"volume": 1}, "expected_previous": {"volume": 9}})
    assert response.status_code == 409, response.text
    assert not any(method == "POST" for method, _ in radio.calls)


def test_backup_hash_is_in_durable_intent(rig):
    _, client = rig
    response = post(client, "settings/volume", {"value": 1, "dry_run": False})
    assert response.status_code == 200, response.text
    with app_db.SessionLocal() as db:
        intent = db.query(DeviceActionJournal).filter_by(phase="settings_intent").one()
        state = json.loads(intent.before_state)
        assert len(state["sha256"]) == 64
        assert intent.backup_ref.startswith("config-backup:")


@pytest.mark.parametrize("suffix,payload", [
    ("rename", {"name": "After", "dry_run": False}),
    ("settings/volume", {"value": 1, "dry_run": False}),
    ("settings-apply", {"values": {"volume": 1}}),
])
def test_stable_id_protection_prevents_constructor(rig, suffix, payload):
    radio, client = rig
    with app_db.SessionLocal() as db:
        db.add(Setting(key="protected_device_ids", value=DEVICE_ID))
        db.commit()
    assert post(client, suffix, payload).status_code == 403
    assert radio.constructors == []


def test_missing_readback_field_blocks_before_write(rig, monkeypatch):
    radio, client = rig
    original = radio.get_xml

    async def missing_actual(path):
        return "<volume><targetvolume>1</targetvolume></volume>" if path == "/volume" else await original(path)

    monkeypatch.setattr(radio, "get_xml", missing_actual)
    response = post(client, "settings/volume", {"value": 1, "dry_run": False})
    assert response.status_code == 409
    assert not any(method == "POST" for method, _ in radio.calls)


def test_unavailable_shared_settings_route_is_requested_once_not_once_per_field(rig):
    radio, client = rig
    response = client.get(f"/api/devices/{DEVICE_ID}/settings?probe=true")
    assert response.status_code == 200
    assert radio.calls.count(("GET", "/systemtimeout")) == 1
    assert radio.calls.count(("GET", "/clockDisplay")) == 1
    current = response.json()["current"]
    assert current["systemtimeout"].startswith("ERROR:")
    assert current["powersaving"].startswith("ERROR:")
    assert current["volume"] == 5
    assert all(method == "GET" for method, _ in radio.calls)


def test_settings_probe_stops_at_mismatched_identity(rig):
    radio, client = rig
    radio.device_id = "OTHER"
    response = client.get(f"/api/devices/{DEVICE_ID}/settings?probe=true")
    assert response.status_code == 409
    assert radio.calls == [("GET", "/info")]
    with app_db.SessionLocal() as db:
        assert db.query(Device).one().info_xml == info()


def test_settings_catalog_and_dry_runs_never_probe(rig):
    radio, client = rig
    assert client.get(f"/api/devices/{DEVICE_ID}/settings").status_code == 200
    assert post(client, "rename", {"name": "After"}).json()["dry_run"] is True
    assert post(client, "settings/volume", {"value": 1}).json()["dry_run"] is True
    assert post(client, "settings-apply", {"values": {"volume": 1}, "dry_run": True}).json()["dry_run"] is True
    assert radio.constructors == []


def test_backup_persistence_failure_stops_before_write(rig, monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    radio, client = rig

    def fail_backup(session, *_):
        if any(isinstance(row, ConfigBackup) for row in session.new):
            raise OSError("simulated full backup storage")

    event.listen(Session, "before_flush", fail_backup)
    try:
        response = post(client, "settings/volume", {"value": 1, "dry_run": False})
    finally:
        event.remove(Session, "before_flush", fail_backup)
    assert response.status_code == 409
    assert not any(method == "POST" for method, _ in radio.calls)


def test_state_changes_between_snapshot_and_write_are_not_overwritten(rig, monkeypatch):
    radio, client = rig
    original = radio.get_xml
    volume_reads = 0

    async def front_panel_action(path):
        nonlocal volume_reads
        if path == "/volume":
            volume_reads += 1
            if volume_reads == 2:
                radio.volume = 8
        return await original(path)

    monkeypatch.setattr(radio, "get_xml", front_panel_action)
    response = post(client, "settings/volume", {"value": 1, "dry_run": False})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "WRITE_PRECONDITION_FAILED"
    assert radio.volume == 8
    assert not any(method == "POST" for method, _ in radio.calls)


def test_database_commit_failure_after_write_is_not_verified(rig):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    radio, client = rig

    def fail_verified_commit(session, *_):
        if any(isinstance(row, DeviceActionJournal) and row.phase == "settings_verified" for row in session.new):
            raise OSError("simulated journal failure")

    event.listen(Session, "before_flush", fail_verified_commit)
    try:
        response = post(client, "rename", {"name": "After", "dry_run": False})
    finally:
        event.remove(Session, "before_flush", fail_verified_commit)
    assert response.status_code == 409
    assert radio.name == "After"  # Radio changed, but commit was not durable.
    with app_db.SessionLocal() as db:
        assert db.query(Device).one().name == "Before"
        assert db.query(DeviceActionJournal).filter_by(phase="settings_intent").count() == 1
        assert db.query(DeviceActionJournal).filter_by(phase="settings_verified").count() == 0


def test_unchanged_actual_state_has_no_post(rig):
    radio, client = rig
    radio.volume = 1
    response = post(client, "settings/volume", {"value": 1, "dry_run": False})
    assert response.status_code == 200
    assert response.json()["unchanged"] == ["volume"]
    assert not any(method == "POST" for method, _ in radio.calls)


def test_corrupted_persisted_backup_blocks_write(rig, monkeypatch):
    from sqlalchemy.orm import Session

    radio, client = rig
    original = Session.get

    def corrupted(self, entity, ident, **kwargs):
        row = original(self, entity, ident, **kwargs)
        if entity is ConfigBackup and row is not None:
            row.content += "corruption"
        return row

    monkeypatch.setattr(Session, "get", corrupted)
    response = post(client, "settings/volume", {"value": 1, "dry_run": False})
    assert response.status_code == 409
    assert not any(method == "POST" for method, _ in radio.calls)


def test_concurrent_duplicate_setting_serializes_and_reads_before_second_write(rig, monkeypatch):
    import asyncio
    from basswiesn.app.services import device_policy
    from basswiesn.app.services.device_settings_writer import apply_settings
    from basswiesn.app.services.settings_contract import plan_setting

    radio, _ = rig
    monkeypatch.setattr(device_policy, "_locks", {})
    original = radio.post_xml

    async def delayed_post(path, xml):
        await asyncio.sleep(0.01)  # Allow the competing coroutine to enter.
        return await original(path, xml)

    monkeypatch.setattr(radio, "post_xml", delayed_post)

    async def action():
        with app_db.SessionLocal() as db:
            device = db.query(Device).one()
            return await apply_settings(db, device, [plan_setting("volume", 1)],
                                        client_factory=radio.factory, guard=api.enforce_ip_write_guard)

    async def together():
        return await asyncio.gather(action(), action())

    results = asyncio.run(together())
    assert radio.calls.count(("POST", "/volume")) == 1
    assert sorted(result["applied"] for result in results) == [0, 1]


@pytest.mark.parametrize("clock_config", [{"brightnessLevel": 0}, {"brightnessLevel": 0, "userEnable": False}])
def test_clock_batch_preserves_unrequested_enable_and_zero_brightness(rig, monkeypatch, clock_config):
    radio, client = rig
    clock = {"userEnable": "true", "brightnessLevel": "70", "timezoneInfo": "Europe/Berlin"}
    original_get, original_post = radio.get_xml, radio.post_xml

    async def get_xml(path):
        if path != "/clockDisplay":
            return await original_get(path)
        root = ET.Element("clockDisplay")
        ET.SubElement(root, "clockConfig", clock)
        return ET.tostring(root, encoding="unicode")

    async def post_xml(path, xml):
        if path == "/clockDisplay":
            clock.update(ET.fromstring(xml).find("clockConfig").attrib)
        return await original_post(path, xml)

    monkeypatch.setattr(radio, "get_xml", get_xml)
    monkeypatch.setattr(radio, "post_xml", post_xml)
    result = post(client, "settings-apply", {
        "values": {"clockDisplay": False, "clockConfig": clock_config},
        "expected_previous": {"clockDisplay": True, "clockConfig": {key: clock[key] for key in clock_config}},
    })
    assert result.status_code == 200, result.text
    assert result.json()["applied"] == 2
    assert clock == {"userEnable": "false", "brightnessLevel": "0", "timezoneInfo": "Europe/Berlin"}


def test_local_preference_precondition_blocks_before_mixed_radio_write(rig):
    radio, client = rig
    response = post(client, "settings-apply", {
        "values": {"station_art_mode": "station_logo", "volume": 1},
        "expected_previous": {"station_art_mode": "no_station_logo", "volume": 5},
    })
    assert response.status_code == 409
    assert response.json()["detail"]["failed_setting"] == "station_art_mode"
    assert radio.constructors == []
    assert radio.calls == []


def test_local_preference_matching_previous_needs_no_radio_transport(rig):
    radio, client = rig
    response = post(client, "settings-apply", {
        "values": {"station_art_mode": "station_logo"},
        "expected_previous": {"station_art_mode": "radio_symbol"},
    })
    assert response.status_code == 200
    assert response.json()["preset_sync_required"] is True
    assert radio.constructors == []


def test_local_preference_commit_failure_retains_radio_prefix(rig):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    radio, client = rig

    def fail_local(session, *_):
        if any(isinstance(row, Setting) and row.key.startswith("station_art_mode:") for row in session.new):
            raise OSError("simulated local setting failure")

    event.listen(Session, "before_flush", fail_local)
    try:
        response = post(client, "settings-apply", {"values": {"volume": 1, "station_art_mode": "no_station_logo"}})
    finally:
        event.remove(Session, "before_flush", fail_local)
    assert response.status_code == 409
    assert response.json()["detail"]["applied"] == 1
    assert response.json()["detail"]["failed_setting"] == "station_art_mode"
    assert radio.volume == 1


@pytest.mark.parametrize("replacement", [
    ("<variant>mojo</variant>", "<variant>unknown</variant>"),
    ("<moduleType>SCM</moduleType>", "<moduleType>SM2</moduleType>"),
    ("<productID>0x0924</productID>", "<productID>0x093B</productID>"),
])
def test_changed_hardware_facts_stop_before_any_write(rig, monkeypatch, replacement):
    radio, client = rig
    original = radio.get_xml

    async def changed_info(path):
        xml = await original(path)
        return xml.replace(*replacement) if path == "/info" else xml

    monkeypatch.setattr(radio, "get_xml", changed_info)
    response = post(client, "settings/volume", {"value": 1, "dry_run": False})
    assert response.status_code == 409
    assert not any(method == "POST" for method, _ in radio.calls)
