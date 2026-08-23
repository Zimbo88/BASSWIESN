import asyncio

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from basswiesn.app import db as app_db
from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.config import get_settings
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, Setting, Station
from basswiesn.app.routers import multiroom as multiroom_router
from basswiesn.app.services.protected_devices import is_protected_device, protected_device_ips
from basswiesn.app.services.telnet_device_control import telnet_capabilities


PROTECTED_IP = "192.0.2.25"


def _set_protected_ips(value: str = PROTECTED_IP) -> None:
    db = app_db.SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "protected_device_ips").one_or_none()
        if row is None:
            row = Setting(key="protected_device_ips")
            db.add(row)
        row.value = value
        db.commit()
    finally:
        db.close()


def test_protected_device_release_default_is_empty_and_db_setting_enforces_local_guard(monkeypatch):
    monkeypatch.delenv("PROTECTED_DEVICE_IPS", raising=False)
    monkeypatch.delenv("BASSWIESN_PROTECTED_DEVICE_IPS", raising=False)
    get_settings.cache_clear()
    assert PROTECTED_IP not in get_settings().protected_device_ips
    _set_protected_ips(PROTECTED_IP)
    assert PROTECTED_IP in protected_device_ips()


def test_soundtouch_post_to_protected_ip_is_blocked_before_network():
    _set_protected_ips(PROTECTED_IP)
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, text="<ok />")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            await SoundTouchClient(
                PROTECTED_IP,
                http_client=http_client,
                device_id="PROTECTED",
                request_purpose="unit_test_write",
            ).post_xml("/volume", "<volume>1</volume>")

    with pytest.raises(HTTPException) as error:
        asyncio.run(scenario())
    assert error.value.status_code == 403
    assert called is False


def test_protected_device_rejects_preset_write():
    _set_protected_ips(PROTECTED_IP)
    db = app_db.SessionLocal()
    station = Station(name="Protected Test", stream_url="http://example.test/live.mp3")
    db.add(Device(device_id="PROTID", name="Protected", ip_address=PROTECTED_IP, model="SoundTouch 20"))
    db.add(station)
    db.commit()
    station_id = station.id
    db.close()

    with TestClient(create_web_app()) as client:
        preset = client.post(f"/api/presets/PROTID/1", json={"station_id": station_id, "dry_run": False, "memory_checked": True})

    assert preset.status_code == 403
    assert preset.json()["detail"]["error"] == "protected_device"


def test_protected_device_telnet_is_reported_unsupported():
    _set_protected_ips(PROTECTED_IP)
    db = app_db.SessionLocal()
    device = Device(device_id="PROTECTED", name="Protected", model="Wave SoundTouch", firmware="27.0.13", ip_address=PROTECTED_IP)
    db.add(device)
    db.commit()

    result = telnet_capabilities(db, device)
    db.close()

    assert is_protected_device(device) is True
    assert result["supported"] is False
    assert result["reason"] == "device is protected by PROTECTED_DEVICE_IPS"


def test_protected_device_cannot_enter_battery_monitor_patch_plan():
    _set_protected_ips(PROTECTED_IP)
    db = app_db.SessionLocal()
    db.add(Device(device_id="PROTBATTERY", name="Protected", ip_address=PROTECTED_IP, model="SoundTouch Portable"))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.get("/api/devices/PROTBATTERY/battery/patch-plan")

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "protected_device"


def test_multiroom_preserve_volumes_does_not_send_volume_posts(monkeypatch):
    calls = []

    class Client:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def get_xml(self, path: str) -> str:
            calls.append(("get", self.ip_address, path))
            if path == "/getZone":
                return '<zone master="MRKEEPMASTER"><member ipaddress="192.0.2.82">MRKEEPMEMBER</member></zone>'
            if path == "/volume":
                return "<volume><actualvolume>17</actualvolume></volume>"
            raise AssertionError(path)

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            calls.append(("post", self.ip_address, path, body))
            return "<status>OK</status>"

    monkeypatch.setattr(multiroom_router, "SoundTouchClient", Client)

    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": "MRKEEPMASTER", "name": "Master", "ip_address": "192.0.2.81", "model": "SoundTouch Test"})
        client.post("/api/devices", json={"device_id": "MRKEEPMEMBER", "name": "Member", "ip_address": "192.0.2.82", "model": "SoundTouch Test"})
        response = client.post(
            "/api/multiroom/set",
            json={
                "master_device_id": "MRKEEPMASTER",
                "member_device_ids": ["MRKEEPMEMBER"],
                "volume": 42,
                "preserve_volumes": True,
                "dry_run": False,
                "memory_checked": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["preserve_volumes"] is True
    assert response.json()["volume"] is None
    assert not any(call[0] == "post" and call[2] == "/volume" for call in calls)
    assert any(call[0] == "post" and call[2] == "/setZone" for call in calls)


def test_protected_device_cannot_be_saved_in_multiroom_batch():
    _set_protected_ips(PROTECTED_IP)
    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": "MASTER", "name": "Master", "ip_address": "192.0.2.91", "model": "SoundTouch Test"})
        client.post("/api/devices", json={"device_id": "PROTECTED", "name": "Protected", "ip_address": PROTECTED_IP, "model": "SoundTouch Test"})
        scenario = client.post(
            "/api/multiroom/scenarios",
            json={"name": "Blocked", "master_device_id": "MASTER", "member_device_ids": ["PROTECTED"], "volume": 5},
        )
        schedule = client.post(
            "/api/schedules",
            json={"name": "Blocked Timer", "start_time": "08:00", "device_ids": ["PROTECTED"], "dry_run": True},
        )

    assert scenario.status_code == 403
    assert schedule.status_code == 403
    assert scenario.json()["detail"]["error"] == "protected_device"
    assert schedule.json()["detail"]["error"] == "protected_device"


def test_system_settings_can_manage_protected_device_ips():
    with TestClient(create_web_app()) as client:
        response = client.post("/api/system/settings", json={"protected_device_ips": "192.0.2.44, 192.0.2.45"})
        invalid = client.post("/api/system/settings", json={"protected_device_ips": "not-an-ip"})

    assert response.status_code == 200
    assert response.json()["protected_device_ips"] == "192.0.2.44,192.0.2.45"
    assert "192.0.2.44" in protected_device_ips()
    assert invalid.status_code == 400


def test_final_hardware_gate_migration_marker_and_column_exist():
    inspector = inspect(app_db.engine)
    columns = {column["name"] for column in inspector.get_columns("multiroom_scenarios")}
    assert "preserve_volumes" in columns
    with app_db.engine.connect() as connection:
        migrations = {row[0] for row in connection.execute(text("SELECT version FROM schema_migrations"))}
    assert "1.5.0-final-hardware-gates" in migrations
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
