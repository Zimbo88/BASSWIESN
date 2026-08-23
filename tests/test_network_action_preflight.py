from fastapi.testclient import TestClient

from basswiesn.app.main import create_web_app
from basswiesn.app.services.telnet_device_control import REBOOT_CONFIRMATION


def test_telnet_reboot_is_disabled_by_default():
    with TestClient(create_web_app()) as client:
        created = client.post("/api/devices", json={"device_id": "TELNETOFF", "name": "Offline", "ip_address": "192.168.50.193", "model": "SoundTouch Test", "firmware": "27.0.13"})
        assert created.status_code == 200
        response = client.post("/api/devices/TELNETOFF/telnet/reboot", json={"confirmation": REBOOT_CONFIRMATION})
    assert response.status_code == 409
    assert "BASSWIESN_TELNET_ENABLED=false" in str(response.json())


def test_telnet_reboot_rejects_legacy_yes_confirmation():
    with TestClient(create_web_app()) as client:
        created = client.post("/api/devices", json={"device_id": "TELNETYES", "name": "No legacy yes", "ip_address": "192.168.50.194", "model": "SoundTouch Test", "firmware": "27.0.13"})
        assert created.status_code == 200
        response = client.post("/api/devices/TELNETYES/telnet/reboot", json={"confirmation": "YES"})
    assert response.status_code == 409
    assert REBOOT_CONFIRMATION in str(response.json())
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
