from fastapi.testclient import TestClient

from basswiesn.app.main import create_web_app
from basswiesn.app.api import routes_devices
from basswiesn.app.routers import api
from basswiesn.app.config import Settings


def test_local_device_create_update_and_list_preserve_api_contract(monkeypatch):
    class Client:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def get_xml(self, _path: str) -> str:
            suffix = self.ip_address.rsplit(".", 1)[-1]
            return (
                f'<info deviceID="LOCAL-API-{suffix}"><name>Radio {suffix}</name>'
                "<type>SoundTouch 20</type></info>"
            )

    monkeypatch.setattr(routes_devices, "SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        created = client.post(
            "/api/devices",
            json={
                "name": "Wohnzimmer",
                "ip_address": "192.0.2.51",
            },
        )
        updated = client.post(
            "/api/devices",
            json={
                "name": "Arbeitszimmer",
                "ip_address": "192.0.2.51",
            },
        )
        devices = client.get("/api/devices")

    assert created.status_code == 200
    assert created.json()["device_id"] == "LOCAL-API-51"
    assert updated.status_code == 200
    assert devices.status_code == 200
    assert len(devices.json()) == 1
    assert devices.json()[0]["name"] == "Arbeitszimmer"
    assert devices.json()[0]["ip_address"] == "192.0.2.51"


def test_local_device_create_requires_device_id_or_ip_address():
    with TestClient(create_web_app()) as client:
        response = client.post(
            "/api/devices",
            json={"name": "Ungültig", "model": "SoundTouch Test"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "ip_address is required"


def test_device_create_probe_uses_injected_service_client(monkeypatch):
    class Client:
        def __init__(self, ip_address: str):
            assert ip_address == "192.0.2.53"

        async def get_xml(self, path: str) -> str:
            assert path == "/info"
            return (
                '<info deviceID="LIVE-API-1"><name>Live API Radio</name>'
                "<type>SoundTouch 30</type></info>"
            )

    monkeypatch.setattr(routes_devices, "SoundTouchClient", Client)

    with TestClient(create_web_app()) as client:
        response = client.post(
            "/api/devices",
            json={
                "device_id": "LIVE-API-1",
                "ip_address": "192.0.2.53",
                "model": "SoundTouch 30",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "device_id": "LIVE-API-1",
        "identity": {"canonical_id": "LIVE-API-1", "merged": False, "probed": True},
    }


def test_device_create_rejects_server_host(monkeypatch):
    monkeypatch.setattr(routes_devices, "get_settings", lambda: Settings(lan_host="192.168.50.77"))

    with TestClient(create_web_app()) as client:
        response = client.post("/api/devices", json={"ip_address": "192.168.50.77"})

    assert response.status_code == 400
    assert "server host" in response.json()["detail"]


def test_device_create_rejects_unreachable_without_persisting(monkeypatch):
    class Client:
        def __init__(self, _ip_address: str):
            pass

        async def get_xml(self, _path: str) -> str:
            raise OSError("connection refused")

    monkeypatch.setattr(routes_devices, "SoundTouchClient", Client)

    with TestClient(create_web_app()) as client:
        response = client.post("/api/devices", json={"ip_address": "192.0.2.55"})
        devices = client.get("/api/devices").json()

    assert response.status_code == 400
    assert devices == []


def test_device_remove_deletes_only_local_record(monkeypatch):
    class Client:
        def __init__(self, _ip_address: str):
            pass

        async def get_xml(self, _path: str) -> str:
            return '<info deviceID="REMOVE-1"><name>Remove Me</name><type>SoundTouch 10</type></info>'

    monkeypatch.setattr(routes_devices, "SoundTouchClient", Client)

    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"ip_address": "192.0.2.56"})
        missing_yes = client.request("DELETE", "/api/devices/REMOVE-1", json={"confirmation": "no"})
        removed = client.request("DELETE", "/api/devices/REMOVE-1", json={"confirmation": "YES"})
        devices = client.get("/api/devices").json()

    assert missing_yes.status_code == 400
    assert removed.status_code == 200
    assert removed.json()["radio_write"] is False
    assert devices == []


def test_device_live_list_uses_injected_service_client(monkeypatch):
    class Client:
        def __init__(self, _ip_address: str):
            pass

        async def get_xml(self, _path: str) -> str:
            return (
                '<info deviceID="LIVE-LIST-1"><name>Refreshed Name</name>'
                "<type>SoundTouch Portable</type></info>"
            )

    monkeypatch.setattr(routes_devices, "SoundTouchClient", Client)

    with TestClient(create_web_app()) as client:
        client.post(
            "/api/devices",
            json={
                "device_id": "LIVE-LIST-1",
                "ip_address": "192.0.2.54",
                "model": "SoundTouch Test",
            },
        )
        response = client.get("/api/devices?live=true")

    assert response.status_code == 200
    assert response.json()[0]["name"] == "Refreshed Name"
    assert response.json()[0]["model"] == "SoundTouch Portable"


def test_device_get_route_is_registered_once_in_new_api_module():
    new_matches = [
        route
        for route in routes_devices.router.routes
        if getattr(route, "path", None) == "/api/devices"
        and "GET" in (getattr(route, "methods", None) or set())
    ]
    legacy_matches = [
        route
        for route in api.router.routes
        if getattr(route, "path", None) == "/api/devices"
        and "GET" in (getattr(route, "methods", None) or set())
    ]

    assert len(new_matches) == 1
    assert new_matches[0].endpoint is routes_devices.list_devices
    assert legacy_matches == []


def test_device_post_route_is_registered_once_in_new_api_module():
    new_matches = [
        route
        for route in routes_devices.router.routes
        if getattr(route, "path", None) == "/api/devices"
        and "POST" in (getattr(route, "methods", None) or set())
    ]
    legacy_matches = [
        route
        for route in api.router.routes
        if getattr(route, "path", None) == "/api/devices"
        and "POST" in (getattr(route, "methods", None) or set())
    ]

    assert len(new_matches) == 1
    assert new_matches[0].endpoint is routes_devices.add_device
    assert legacy_matches == []
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
