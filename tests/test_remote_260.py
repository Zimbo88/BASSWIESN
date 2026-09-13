"""Remote regression checks. All radio transports are test doubles."""
import json
from hashlib import sha256

from fastapi.testclient import TestClient
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import ConfigBackup, Device
from basswiesn.app.routers import multiroom, telemetry


@pytest.fixture
def remote_api(monkeypatch):
    calls = []
    state = {"zone": "<zone />", "wrong_identity": False, "broken": "", "allowed": "true", "zone_result": None}
    ids = {"192.0.2.41": "REMOTE-A", "192.0.2.42": "REMOTE-B"}
    with app_db.SessionLocal() as db:
        for ip, device_id in ids.items():
            db.add(Device(device_id=device_id, ip_address=ip, name=device_id, reachable=True))
        db.commit()

    class Radio:
        def __init__(self, ip, **kwargs):
            self.ip = ip

        async def get_xml(self, endpoint):
            calls.append((self.ip, "GET", endpoint))
            if endpoint == state["broken"]:
                raise TimeoutError()
            if endpoint == "/info":
                return f'<info deviceID="{"WRONG" if state["wrong_identity"] else ids[self.ip]}"><type>SoundTouch</type></info>'
            return {
                "/now_playing": '<nowPlaying source="LOCAL_INTERNET_RADIO"><stationName>Example Station</stationName><track>Example Track</track><artist>Example Artist</artist><playStatus>PLAY_STATE</playStatus></nowPlaying>',
                "/volume": '<volume><actualvolume>23</actualvolume><targetvolume>23</targetvolume><muteenabled>false</muteenabled></volume>',
                "/getZone": state["zone"],
                "/sources": f'<sources><sourceItem source="LOCAL_INTERNET_RADIO" multiroomallowed="{state["allowed"]}" /></sources>',
                "/presets": "<presets />",
                "/rebroadcastlatencymode": '<rebroadcastlatencymode mode="SYNC_TO_ZONE" />',
            }[endpoint]

        async def post_xml(self, endpoint, body):
            calls.append((self.ip, "POST", endpoint))
            # Every write sees fresh /info and durable per-radio backups.
            assert calls[-2] == (self.ip, "GET", "/info")
            with app_db.SessionLocal() as db:
                assert db.query(ConfigBackup).count() == 2
            assert endpoint != "/volume"
            if endpoint == "/setZone":
                state["zone"] = state["zone_result"] or body
            return "<status>OK</status>"

    monkeypatch.setattr(telemetry, "SoundTouchClient", Radio)
    monkeypatch.setattr(multiroom, "SoundTouchClient", Radio)

    async def no_sleep(_):
        pass

    monkeypatch.setattr(multiroom.asyncio, "sleep", no_sleep)
    with TestClient(create_web_app(background_tasks=False)) as client:
        yield client, state, calls


def test_structured_remote_state_reads_only_target_with_identity(remote_api):
    client, state, calls = remote_api
    response = client.get("/api/devices/REMOTE-A/remote-state")
    assert response.status_code == 200
    result = response.json()
    assert result["verified"] is True
    assert result["volume"] == 23
    assert result["now_playing"]["artist"] == "Example Artist"
    assert result["play_status"] == "PLAY_STATE"
    assert [call[2] for call in calls] == ["/info", "/now_playing", "/info", "/volume"]
    assert all(call[:2] == ("192.0.2.41", "GET") for call in calls)


def test_remote_identity_mismatch_stops_followups(remote_api):
    client, state, calls = remote_api
    state["wrong_identity"] = True
    assert client.get("/api/devices/REMOTE-A/remote-state").status_code == 409
    assert calls == [("192.0.2.41", "GET", "/info")]


def test_remote_failed_volume_is_not_reported_as_zero(remote_api):
    client, state, calls = remote_api
    state["broken"] = "/volume"
    response = client.get("/api/devices/REMOTE-A/remote-state")
    assert response.status_code == 502
    assert "volume" not in response.json()


def test_remote_group_backups_and_readback_without_volume_writes(remote_api):
    client, state, calls = remote_api
    response = client.post("/api/multiroom/remote-start", json={"master_device_id": "REMOTE-A", "member_device_ids": ["REMOTE-B"], "volume": 99})
    assert response.status_code == 200, response.json()
    result = response.json()
    assert result["automatic_volume_action"] == "NONE"
    assert result["preserve_volumes"] is True
    assert len(result["verification"]) == 2
    assert all(item["ok"] and item["volume"] == 23 for item in result["verification"])
    assert len(result["backup_ids"]) == 2
    assert not any(method == "POST" and endpoint in {"/volume", "/key", "/select"} for _, method, endpoint in calls)
    with app_db.SessionLocal() as db:
        for row in db.query(ConfigBackup).all():
            data = json.loads(row.content)
            assert data["sha256"] == {path: sha256(xml.encode()).hexdigest() for path, xml in data["http"].items()}


@pytest.mark.parametrize("failure", ["identity", "backup", "source", "zone"])
def test_remote_group_preflight_failure_never_writes(remote_api, failure):
    client, state, calls = remote_api
    if failure == "identity": state["wrong_identity"] = True
    elif failure == "backup": state["broken"] = "/presets"
    elif failure == "source": state["allowed"] = "false"
    else: state["zone"] = '<zone master="OTHER" />'
    response = client.post("/api/multiroom/remote-start", json={"master_device_id": "REMOTE-A", "member_device_ids": ["REMOTE-B"]})
    assert response.status_code in {409, 502}
    assert not any(method == "POST" for _, method, _ in calls)


def test_remote_protected_target_never_opens_transport(remote_api, monkeypatch):
    from basswiesn.app.config import get_settings
    client, state, calls = remote_api
    try:
        with monkeypatch.context() as scoped:
            scoped.setenv("PROTECTED_DEVICE_IDS", "REMOTE-A")
            get_settings.cache_clear()
            assert client.get("/api/devices/REMOTE-A/remote-state").status_code == 403
            assert client.post("/api/multiroom/remote-start", json={"master_device_id": "REMOTE-A", "member_device_ids": ["REMOTE-B"]}).status_code == 403
            assert client.put("/api/devices/REMOTE-A/metadata/clock", json={"enabled": True}).status_code == 403
            assert calls == []
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("zone", [
    '<zone master="REMOTE-A" />',
    '<zone master="REMOTE-A"><member>REMOTE-B</member><member>UNREQUESTED</member></zone>',
])
def test_remote_master_only_or_extra_member_is_not_a_verified_group(remote_api, zone):
    client, state, calls = remote_api
    state["zone_result"] = zone
    response = client.post("/api/multiroom/remote-start", json={"master_device_id": "REMOTE-A", "member_device_ids": ["REMOTE-B"]})
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert len(detail["backup_ids"]) == 2
    assert detail["cause"]["verification"][0]["zone_ok"] is False
    assert not any(method == "POST" and endpoint == "/volume" for _, method, endpoint in calls)
