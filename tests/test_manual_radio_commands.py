"""Offline contracts for native station persistence and Wi-Fi transitions."""
import asyncio
import json

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from starlette.responses import JSONResponse

from basswiesn.app import db as app_db
from basswiesn.app.models import ConfigBackup, Device, DeviceActionJournal, Setting
from basswiesn.app.main import create_web_app
from basswiesn.app.routers import api
from basswiesn.app.services.device_policy import device_lock
from test_maintenance_reboot_safety import ID, BUILD, info, now, volume
from test_admin_boundary import secured, authenticate  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture(params=["station", "wifi"])
def radio(request, monkeypatch):
    class Radio:
        kind = request.param
        constructed = 0
        lost = False

        def __init__(self):
            self.calls, self.writes, self.durable, self.owned = [], [], [], []
            self.raw = {"/info": info(), "/now_playing": now("STANDBY", "STOP_STATE"),
                "/volume": volume(1), "/getZone": '<zone/>', "/presets": '<presets/>',
                "/sources": '<sources><sourceItem source="TUNEIN" sourceAccount="" status="READY"/></sources>',
                "/networkInfo": '<networkInfo wifiProfileCount="1"><interface type="WIFI_INTERFACE" state="NETWORK_WIFI_CONNECTED" ssid="Previous"/></networkInfo>',
                "/getActiveWirelessProfile": '<active><ssid>Previous</ssid><password>PRIVATE-PREVIOUS</password></active>'}

        def factory(self, *args, **kwargs):
            self.constructed += 1
            return self

        async def get_xml(self, path):
            self.calls.append(path)
            return self.raw[path]

        async def post_xml(self, path, body):
            self.writes.append((path, body))
            self.owned.append(device_lock(ID).owned_by_current_task())
            with app_db.SessionLocal() as db:
                self.durable.append(db.query(ConfigBackup).count() > 0 and db.query(DeviceActionJournal).count() > 0)
            if self.lost:
                raise TimeoutError("password=PRIVATE-REQUEST")
            return '<status password="PRIVATE-RESPONSE">OK</status>'

    instance = Radio()
    monkeypatch.setattr(api, "SoundTouchClient", instance.factory)
    with app_db.SessionLocal() as db:
        instance.db = db
        instance.device = Device(device_id=ID, ip_address="192.0.2.42", model="SoundTouch 30", firmware=BUILD, info_xml=info())
        db.add(instance.device)
        db.add(Setting(key="ui_mode", value="lab"))
        db.commit()
        yield instance


def run(radio, **changes):
    payload = {"dry_run": False, "confirmation": "YES", "acknowledge_manual_recovery": True,
               "operation_id": "0123456789abcdef0123456789abcdef"}
    if radio.kind == "station":
        payload.update(source="TUNEIN", sourceAccount="", token="PRIVATE-REQUEST", name="Example Radio")
        handler = api.native_station_add
    else:
        payload.update(ssid="Example Network", password="PRIVATE-REQUEST", security_type="wpa_or_wpa2")
        handler = api.add_wireless_profile
    payload.update(changes)
    try:
        result = asyncio.run(handler(ID, payload, radio.db))
        return json.loads(result.body) if isinstance(result, JSONResponse) else result
    except HTTPException as exc:
        return {"rejected": True, "http_status": exc.status_code, "detail": exc.detail}


def test_ack_is_not_persistence_and_start_is_owned_durable(radio):
    result = run(radio)
    assert len(radio.writes) == 1 and all(radio.owned) and all(radio.durable)
    assert result["verified"] is False and result["status"] == "PENDING_READBACK"
    assert result["persistence_verified"] is False and result["reconciliation_required"] is True
    assert "PRIVATE-" not in json.dumps(result)


@pytest.mark.parametrize("failure", ["identity", "profile", "zone", "protected"])
def test_preconditions_block_before_mutation(radio, failure):
    if failure == "identity":
        radio.raw["/info"] = info("BB22CC33DD44")
    elif failure == "profile":
        radio.raw["/info"] = info(firmware="27.99.0")
    elif failure == "zone":
        radio.raw["/getZone"] = '<zone master="OTHER"><member>OTHER</member></zone>'
    else:
        radio.device.ip_address = "192.168.50.25"  # Synthetic conftest protection.
        radio.db.commit()
    assert run(radio).get("rejected") is True
    assert not radio.writes
    if failure == "protected":
        assert not radio.constructed


@pytest.mark.parametrize("condition", ["preview", "confirmation", "mode", "manual_recovery"])
def test_execution_needs_explicit_lab_intent(radio, condition):
    payload = {}
    if condition == "preview":
        payload["dry_run"] = True
    elif condition == "confirmation":
        payload["confirmation"] = "NO"
    elif condition == "mode":
        radio.db.query(Setting).filter_by(key="ui_mode").one().value = "standard"
        radio.db.commit()
    else:
        payload["acknowledge_manual_recovery"] = False
    result = run(radio, **payload)
    assert result.get("dry_run") is True or result.get("rejected") is True
    assert not radio.constructed
    assert "PRIVATE-REQUEST" not in json.dumps(result)


def test_lost_reply_is_not_replayed_or_certified(radio):
    radio.lost = True
    result = run(radio)
    assert len(radio.writes) == 1
    assert result["verified"] is False and result["reconciliation_required"] is True
    assert result["response_received"] is False
    assert "PRIVATE-REQUEST" not in json.dumps(result)


def test_admin_session_csrf_and_service_keep_pending_outcomes(radio, secured):
    path = "station/add-native" if radio.kind == "station" else "wireless-profiles"
    payload = {"dry_run": False, "confirmation": "YES", "acknowledge_manual_recovery": True,
               "operation_id": "ab" * 16, "source": "TUNEIN", "token": "TEST-TOKEN", "name": "Example",
               "ssid": "Example", "password": "TEST-PASSPHRASE"}
    with TestClient(create_web_app(background_tasks=False), base_url="https://testserver") as client:
        csrf = authenticate(client)
        assert client.post(f"/api/devices/{ID}/{path}", json=payload).status_code == 403
        assert not radio.constructed
        response = client.post(f"/api/devices/{ID}/{path}", json=payload,
                               headers={"Origin": "https://testserver", "X-BASSWIESN-CSRF": csrf})
        assert response.status_code == 202, response.text
        assert response.json()["verified"] is False and response.json()["status"] == "PENDING_READBACK"
        assert len(radio.writes) == 1


def test_queued_reconnect_is_cancelled_durably_before_dispatch(radio, monkeypatch):
    from basswiesn.app.services import live_radio_reconnect as reconnect
    reconnect._store(radio.db, ID, {"phase": "PENDING", "generation": "old"})
    radio.db.commit()
    original = radio.post_xml
    async def observed(path, body):
        with app_db.SessionLocal() as db:
            value = reconnect.state(db, ID)[1]
            assert value["phase"] == "CANCELLED" and value["generation"] != "old"
        return await original(path, body)
    monkeypatch.setattr(radio, "post_xml", observed)
    assert run(radio)["status"] == "PENDING_READBACK"
    assert len(radio.writes) == 1


def test_failed_backup_commit_prevents_dispatch(radio, monkeypatch):
    def fail():
        raise RuntimeError("START could not be persisted")
    monkeypatch.setattr(radio.db, "commit", fail)
    try:
        run(radio)
    except RuntimeError:
        pass
    assert not radio.writes


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
def test_nonboolean_preview_cannot_authorize_execution(radio, value):
    assert run(radio, dry_run=value)["http_status"] == 400
    assert not radio.constructed


@pytest.mark.parametrize("value", [None, "", "0" * 31, "X" * 32, 1])
def test_explicit_operation_key_is_required(radio, value):
    assert run(radio, operation_id=value)["http_status"] == 400
    assert not radio.constructed


@pytest.mark.parametrize("changed_payload", [False, True])
def test_recorded_operation_id_prevents_another_dispatch(radio, changed_payload):
    first = run(radio)
    constructed = radio.constructed
    second = run(radio, **({"ssid": "Other network", "token": "Other token"} if changed_payload else {}))
    assert first["status"] == "PENDING_READBACK" and second["status"] == "ALREADY_RECORDED"
    assert first["job_id"] == second["job_id"]
    assert first["backup_ref"] == second["backup_ref"]
    assert second["verified"] is False
    assert len(radio.writes) == 1 and radio.constructed == constructed


@pytest.mark.parametrize("change", ["backup", "backup-path", "identity", "volume", "mapping", "source-or-ssid"])
def test_state_or_backup_change_during_intent_blocks_dispatch(radio, monkeypatch, change):
    original = radio.db.commit
    commits = 0
    def changed():
        nonlocal commits
        commits += 1
        original()
        if commits == 2:
            if change in {"backup", "backup-path"}:
                row = radio.db.query(ConfigBackup).one()
                if change == "backup":
                    row.content += " changed"
                else:
                    row.path = "unrelated"
                original()
            elif change == "identity":
                radio.raw["/info"] = info("BB22CC33DD44")
            elif change == "volume":
                radio.raw["/volume"] = volume(2)
            elif change == "mapping":
                radio.device.ip_address = "192.0.2.43"
                original()
            elif radio.kind == "station":
                radio.raw["/sources"] = '<sources><sourceItem source="TUNEIN" status="UNAVAILABLE"/></sources>'
            else:
                radio.raw["/getActiveWirelessProfile"] = '<active><ssid>Other</ssid></active>'
    monkeypatch.setattr(radio.db, "commit", changed)
    assert run(radio)["rejected"] is True
    assert not radio.writes


@pytest.mark.parametrize("failure", ["offline", "identity", "volume", "final-commit", "observed-only"])
def test_after_write_observations_do_not_prove_persistence(radio, monkeypatch, failure):
    original = radio.get_xml
    async def observed(path):
        if radio.writes:
            if failure == "offline":
                raise TimeoutError("password=PRIVATE-READBACK")
            if failure == "identity":
                radio.raw["/info"] = info("BB22CC33DD44")
            if failure == "volume":
                radio.raw["/volume"] = volume(2)
            if failure == "observed-only":
                radio.raw["/now_playing"] = now("TUNEIN", "PLAY_STATE")
                radio.raw["/getActiveWirelessProfile"] = '<active><ssid>Example Network</ssid></active>'
        return await original(path)
    monkeypatch.setattr(radio, "get_xml", observed)
    commit = radio.db.commit
    def final_commit():
        if radio.writes and failure == "final-commit":
            raise RuntimeError("database unavailable")
        commit()
    monkeypatch.setattr(radio.db, "commit", final_commit)
    result = run(radio)
    detail = result.get("detail", result)
    assert detail["verified"] is False and detail["persistence_verified"] is False
    assert detail["reconciliation_required"] is True
    assert len(radio.writes) == 1
    if failure == "volume":
        assert detail["unrequested_state_preserved"] is False
    if failure == "observed-only":
        assert detail["selected_source_matches" if radio.kind == "station" else "requested_ssid_observed"] is True
    assert radio.db.query(ConfigBackup).count() == 1
    assert not radio.db.query(DeviceActionJournal).filter_by(verified=True).count()
    assert "PRIVATE-" not in json.dumps(result)


def test_sensitive_request_values_are_absent_from_persisted_state_and_repr(radio):
    from basswiesn.app.services.manual_radio_commands import ManualCommand
    command = ManualCommand.wireless({"ssid": "Example", "password": "PRIVATE-REQUEST"})
    assert "PRIVATE-" not in repr(command)
    assert "PRIVATE-" not in json.dumps(command.preview())
    run(radio)
    for row in radio.db.query(ConfigBackup).all():
        assert "PRIVATE-" not in row.content
    for row in radio.db.query(DeviceActionJournal).all():
        for column in row.__table__.columns:
            assert "PRIVATE-" not in str(getattr(row, column.name))


def test_cancellation_retains_intent_and_blocks_replay(radio, monkeypatch):
    original = radio.post_xml
    async def cancelled(path, body):
        await original(path, body)
        raise asyncio.CancelledError()
    monkeypatch.setattr(radio, "post_xml", cancelled)
    with pytest.raises(asyncio.CancelledError):
        run(radio)
    assert not device_lock(ID).owned_by_current_task()
    assert run(radio)["status"] == "ALREADY_RECORDED"
    assert len(radio.writes) == 1


@pytest.mark.parametrize("error", ["xml", "oversized", "required"])
def test_invalid_request_fields_never_construct_transport(radio, error):
    field, value = {
        "station": {"xml": ("token", "bad\x00token"), "oversized": ("name", "x" * 129), "required": ("token", "")},
        "wifi": {"xml": ("password", "bad\x00value"), "oversized": ("ssid", "x" * 33), "required": ("password", "")},
    }[radio.kind][error]
    assert run(radio, **{field: value})["http_status"] == 400
    assert not radio.constructed
