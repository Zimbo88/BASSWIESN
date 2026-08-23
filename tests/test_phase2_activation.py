import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, Setting, Station
from basswiesn.app.routers import multiroom
from basswiesn.app.routers import stations_presets
from basswiesn.app.services import offline_preflight
from basswiesn.app.services.logo_validation import validate_logo_reference


pytestmark = pytest.mark.integration


def test_feature_status_api_exposes_runtime_state_without_secrets():
    with TestClient(create_web_app()) as client:
        response = client.get("/api/features/status")

    assert response.status_code == 200
    payload = response.json()
    features = {item["id"]: item for item in payload["features"]}
    assert {"station_logo", "offline_mode", "preset_sync", "multiroom_scenes", "telnet_reboot"} <= features.keys()
    required = {"id", "title", "category", "status", "enabled", "available", "configured", "restart_required", "blockers", "requirements", "feature_flags", "activation_method", "hardware_status", "settings_target", "navigation_target", "documentation", "safe_test_available"}
    assert required <= features["station_logo"].keys()
    assert features["telnet_reboot"]["enabled"] is False
    assert features["telnet_reboot"]["blockers"]
    serialized = response.text.lower()
    assert '"telnet_username"' not in serialized
    assert '"telnet_password_file"' not in serialized
    assert ".env" not in serialized


def test_feature_documentation_route_is_whitelisted():
    with TestClient(create_web_app()) as client:
        known = client.get("/api/features/docs/activation-matrix")
        unknown = client.get("/api/features/docs/../../.env")

    assert known.status_code == 200
    assert "Production path" in known.text
    assert unknown.status_code in {404, 400}


def test_offline_preflight_classifies_without_network_request(monkeypatch):
    called = False

    async def unexpected_probe(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("probe must be explicit")

    monkeypatch.setattr(offline_preflight, "probe_stream_reference", unexpected_probe)
    with TestClient(create_web_app()) as client:
        response = client.post("/api/offline/preflight", json={"stream_url": "https://8.8.8.8/live.mp3"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_kind"] == "external_internet_stream"
    assert payload["probe"]["performed"] is False
    assert payload["radio_autark"]["status"] == "unbestätigt"
    assert called is False


def test_offline_preflight_probe_is_explicit_and_bounded(monkeypatch):
    async def fake_probe(url, *, allowed_hosts=None):
        return {"requested": True, "performed": True, "status": "erreichbar", "host": "8.8.8.8", "content_type": "audio/mpeg", "sample_bytes": 42, "reason": "mocked"}

    monkeypatch.setattr(offline_preflight, "probe_stream_reference", fake_probe)
    with TestClient(create_web_app()) as client:
        response = client.post("/api/offline/preflight", json={"stream_url": "https://8.8.8.8/live.mp3", "probe": True})

    assert response.status_code == 200
    assert response.json()["probe"]["performed"] is True


def test_invalid_logo_uses_radio_symbol_fallback_in_radio_payload():
    db = app_db.SessionLocal()
    station = Station(name="Ungültiges Logo", stream_url="http://example.test/live.mp3", image_url="file:///etc/passwd")
    db.add_all([Device(device_id="LOGOFALLBACK", ip_address="127.0.0.1"), station, Setting(key="station_art_mode:LOGOFALLBACK", value="station_logo")])
    db.flush()
    db.commit()
    db.close()

    assert validate_logo_reference("file:///etc/passwd")["valid"] is False
    with TestClient(create_web_app()) as client:
        response = client.get("/v1/systems/devices/LOGOFALLBACK/presets")

    assert response.status_code == 200
    assert ET.fromstring(response.text).find("preset/ContentItem/containerArt") is None


def test_phase2_ui_contains_activation_and_offline_followups():
    with TestClient(create_web_app()) as client:
        html = client.get("/").text
    assert 'data-view="features"' in html
    assert 'id="view-features"' in html
    assert 'id="device-settings-preset-sync-preview"' in html
    assert 'id="device-settings-preset-sync-confirm"' in html
    assert 'id="offline-preflight-run"' in html
    assert 'id="offline-preflight-probe"' in html


def test_multiroom_preview_reads_volumes_and_blocks_protected_devices(monkeypatch):
    db = app_db.SessionLocal()
    db.add_all([
        Device(device_id="M2MASTER", name="Master", ip_address="192.0.2.20"),
        Device(device_id="M2MEMBER", name="Member", ip_address="192.0.2.21"),
        Setting(key="protected_device_ips", value="192.0.2.21"),
    ])
    db.commit()
    db.close()

    async def fake_volume(device):
        return {"M2MASTER": 7, "M2MEMBER": 11}[device.device_id]

    monkeypatch.setattr(multiroom, "_read_volume", fake_volume)
    with TestClient(create_web_app()) as client:
        response = client.post("/api/multiroom/preview", json={"master_device_id": "M2MASTER", "member_device_ids": ["M2MEMBER"], "preserve_volumes": True, "read_volumes": True})

    assert response.status_code == 200
    payload = response.json()
    assert payload["preserve_volumes"] is True
    assert next(item for item in payload["current_volumes"] if item["device_id"] == "M2MASTER")["volume"] == 7
    protected_observation = next(item for item in payload["current_volumes"] if item["device_id"] == "M2MEMBER")
    assert protected_observation["volume"] is None
    assert protected_observation["read_skipped"] == "protected_device"
    assert payload["protected_devices"] == ["M2MEMBER"]
    assert payload["blocked"] is True
