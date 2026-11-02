from fastapi.testclient import TestClient

from basswiesn.app.api import routes_devices
from basswiesn.app.adapters.discovery import DiscoveryScanResult
from basswiesn.app.main import create_web_app
from basswiesn.app.routers import api
from basswiesn.app.config import Settings


DISCOVERED = {
    "device_id": "SCAN-DEVICE-1",
    "ip_address": "192.0.2.31",
    "name": "Scan Radio",
    "model": "SoundTouch 20",
    "raw": (
        '<info deviceID="SCAN-DEVICE-1"><name>Scan Radio</name>'
        "<type>SoundTouch 20</type><components><component>"
        "<softwareVersion>27.0.13</softwareVersion>"
        "</component></components></info>"
    ),
}


def test_device_scan_uses_adapter_and_persists_discovered_device(monkeypatch):
    captured = {}
    events = []

    async def fake_scan(cidr, *, limit, timeout, concurrency):
        captured.update(cidr=cidr, limit=limit, concurrency=concurrency)
        captured["timeout"] = timeout
        return DiscoveryScanResult(scanned=limit, devices=[DISCOVERED], failures=[])

    monkeypatch.setattr(routes_devices, "scan_subnet_detailed", fake_scan)
    monkeypatch.setattr(routes_devices, "write_masterlog", lambda event, **fields: events.append((event, fields)))

    with TestClient(create_web_app()) as client:
        response = client.post(
            "/api/devices/scan",
            json={"cidr": "192.0.2.0/29", "timeout": 0.25, "limit": 2, "save": True},
        )
        devices = client.get("/api/devices").json()

    assert response.status_code == 200
    assert response.json() == {
        "cidr": "192.0.2.0/29",
        "scanned": 2,
        "found": [
            {
                "device_id": "SCAN-DEVICE-1",
                "ip_address": "192.0.2.31",
                "name": "Scan Radio",
                "model": "SoundTouch 20",
                "firmware": "27.0.13",
            }
        ],
    }
    assert captured == {
        "cidr": "192.0.2.0/29",
        "limit": 2,
        "concurrency": 64,
        "timeout": 0.25,
    }
    assert any(item["device_id"] == "SCAN-DEVICE-1" for item in devices)
    assert [event for event, _fields in events] == ["device_scan_start", "device_scan_complete"]


def test_device_scan_save_false_does_not_persist(monkeypatch):
    async def fake_scan(_cidr, *, limit, timeout, concurrency):
        assert limit == 1
        assert concurrency == 64
        assert timeout == 0.1
        return DiscoveryScanResult(scanned=1, devices=[DISCOVERED], failures=[])

    monkeypatch.setattr(routes_devices, "scan_subnet_detailed", fake_scan)

    with TestClient(create_web_app()) as client:
        response = client.post(
            "/api/devices/scan",
            json={"cidr": "192.0.2.0/30", "timeout": 0.1, "limit": 1, "save": False},
        )
        devices = client.get("/api/devices").json()

    assert response.status_code == 200
    assert response.json()["found"][0]["device_id"] == "SCAN-DEVICE-1"
    assert devices == []


def test_device_scan_default_range_uses_configured_lan_host(monkeypatch):
    captured = {}

    async def fake_scan(cidr, *, limit, timeout, concurrency):
        captured["cidr"] = cidr
        return DiscoveryScanResult(scanned=0, devices=[], failures=[])

    monkeypatch.setattr(routes_devices, "scan_subnet_detailed", fake_scan)
    monkeypatch.setattr(
        routes_devices,
        "get_settings",
        lambda: Settings(lan_host="192.168.50.77"),
    )

    with TestClient(create_web_app()) as client:
        response = client.post("/api/devices/scan", json={"limit": 1})

    assert response.status_code == 200
    assert captured["cidr"] == "192.168.50.0/24"


def test_device_scan_default_range_can_use_request_host(monkeypatch):
    captured = {}

    async def fake_scan(cidr, *, limit, timeout, concurrency):
        captured["cidr"] = cidr
        return DiscoveryScanResult(scanned=0, devices=[], failures=[])

    monkeypatch.setattr(routes_devices, "scan_subnet_detailed", fake_scan)
    monkeypatch.setattr(routes_devices, "get_settings", lambda: Settings(lan_host=""))
    monkeypatch.setattr(routes_devices, "_default_lan_host", lambda: "")

    with TestClient(create_web_app(), base_url="http://10.1.2.3:1328") as client:
        response = client.post("/api/devices/scan", json={"limit": 1})

    assert response.status_code == 200
    assert captured["cidr"] == "10.1.2.0/24"


def test_device_scan_default_range_prefers_ui_host(monkeypatch):
    captured = {}

    async def fake_scan(cidr, *, limit, timeout, concurrency):
        captured["cidr"] = cidr
        return DiscoveryScanResult(scanned=0, devices=[], failures=[])

    monkeypatch.setattr(routes_devices, "scan_subnet_detailed", fake_scan)

    with TestClient(create_web_app(), base_url="http://192.168.1.50:1328") as client:
        response = client.post("/api/devices/scan", json={"host": "172.31.9.8", "limit": 1})

    assert response.status_code == 200
    assert captured["cidr"] == "172.31.9.0/24"


def test_device_scan_filters_blocked_and_server_hosts(monkeypatch):
    discovered = [
        {**DISCOVERED, "device_id": "SERVER", "ip_address": "192.168.50.77"},
        {**DISCOVERED, "device_id": "RADIO2", "ip_address": "192.168.50.200"},
        {**DISCOVERED, "device_id": "RADIO", "ip_address": "192.168.50.112"},
    ]

    async def fake_scan(_cidr, *, limit, timeout, concurrency):
        return DiscoveryScanResult(scanned=3, devices=discovered, failures=[])

    monkeypatch.setattr(routes_devices, "scan_subnet_detailed", fake_scan)
    monkeypatch.setattr(routes_devices, "get_settings", lambda: Settings(lan_host="192.168.50.77"))

    with TestClient(create_web_app()) as client:
        response = client.post("/api/devices/scan", json={"cidr": "192.168.50.0/24"})
        devices = client.get("/api/devices").json()

    assert response.status_code == 200
    assert {item["ip_address"] for item in response.json()["found"]} == {"192.168.50.200", "192.168.50.112"}
    assert {item["ip_address"] for item in devices} == {"192.168.50.200", "192.168.50.112"}


def test_device_scan_rejects_invalid_cidr_from_adapter(monkeypatch):
    async def invalid_scan(*_args, **_kwargs):
        raise ValueError("not-a-network does not appear to be an IPv4 or IPv6 network")

    monkeypatch.setattr(routes_devices, "scan_subnet_detailed", invalid_scan)

    with TestClient(create_web_app()) as client:
        response = client.post("/api/devices/scan", json={"cidr": "not-a-network"})

    assert response.status_code == 400


def test_device_scan_has_no_hardcoded_range_when_lan_host_is_unsafe(monkeypatch):
    monkeypatch.setattr(routes_devices, "get_settings", lambda: Settings(lan_host="content.api.bose.io"))
    monkeypatch.setattr(routes_devices, "_default_lan_host", lambda: "")

    with TestClient(create_web_app(), base_url="http://127.0.0.1:1328") as client:
        response = client.post("/api/devices/scan", json={})

    assert response.status_code == 400
    assert "LAN host or explicit Scan CIDR" in response.json()["detail"]


def test_device_scan_route_is_registered_once_in_new_api_module():
    new_matches = [
        route
        for route in routes_devices.router.routes
        if getattr(route, "path", None) == "/api/devices/scan"
        and "POST" in (getattr(route, "methods", None) or set())
    ]
    legacy_matches = [
        route
        for route in api.router.routes
        if getattr(route, "path", None) == "/api/devices/scan"
        and "POST" in (getattr(route, "methods", None) or set())
    ]

    assert len(new_matches) == 1
    assert new_matches[0].endpoint is routes_devices.scan_devices
    assert legacy_matches == []
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
