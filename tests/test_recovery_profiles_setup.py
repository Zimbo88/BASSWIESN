import asyncio

import pytest

from basswiesn.app.models import Device
from fastapi.testclient import TestClient

from basswiesn.app.config import Settings
from basswiesn.app.main import create_web_app
from basswiesn.app.routers import api, setup
from basswiesn.app.routers.api import SETTINGS_CATALOG, _cloud_route_targets, _device_summary, _guided_setup_steps, _setup_cli17000_commands, _setup_wizard_steps


def test_device_summary_detects_basswiesn_target_from_marge_url():
    device = Device(
        device_id="ABC123",
        name="Kueche",
        model="SoundTouch 20",
        ip_address="192.0.2.10",
        info_xml="""<info><name>Kueche</name><type>SoundTouch 20</type><margeURL>http://basswiesn.local:1516</margeURL><components><component><softwareVersion>27.0.13</softwareVersion></component></components></info>""",
    )

    summary = _device_summary(device)

    assert summary["name"] == "Kueche"
    assert summary["model"] == "SoundTouch 20"
    assert summary["firmware"] == "27.0.13"
    assert summary["configured_for"] == "basswiesn"
    assert summary["ready"] is True


def test_device_summary_detects_bose_target_from_marge_url():
    device = Device(device_id="ABC123", ip_address="192.0.2.10", info_xml="<info><margeURL>https://content.api.bose.io</margeURL></info>")

    assert _device_summary(device)["configured_for"] == "bose"


def test_guided_setup_contains_profile_and_verify_steps():
    device = Device(device_id="ABC123", name="Bad", model="SoundTouch 10", ip_address="192.0.2.11")

    keys = [step["key"] for step in _guided_setup_steps(device)]

    assert keys == ["identify", "backup", "cloud_route", "settings", "presets", "verify"]


def test_catalog_exposes_power_and_recovery_endpoints():
    endpoints = {item["endpoint"] for item in SETTINGS_CATALOG}

    assert "/standby" in endpoints
    assert "/lowPowerStandby" in endpoints
    assert "/factoryDefault" in endpoints


def test_factory_reset_actions_are_retired_from_product_api():
    with TestClient(create_web_app(background_tasks=False)) as client:
        created = client.post(
            "/api/devices",
            json={"device_id": "NORESET", "name": "Safe", "model": "SoundTouch Test", "ip_address": "192.0.2.44"},
        )
        assert created.status_code == 200
        for action in ("factory_default", "factory_reset_fix_plan", "nuclear_reset_plan"):
            response = client.post(
                f"/api/devices/NORESET/recovery/{action}",
                json={"dry_run": False, "confirmation": "YES"},
            )
            assert response.status_code == 410
            assert response.json()["detail"]["error"] == "FACTORY_RESET_RETIRED"


def test_setup_wizard_targets_all_cloud_urls_to_basswiesn():
    targets = _cloud_route_targets("192.168.50.10", 1516)

    assert targets["margeServerUrl"] == "http://192.168.50.10:1516"
    assert targets["statsServerUrl"] == "http://192.168.50.10:1516"
    assert targets["swUpdateUrl"] == "http://192.168.50.10:1516/updates/soundtouch"
    assert targets["bmxRegistryUrl"] == "http://192.168.50.10:1516/bmx/registry/v1/services"


def test_setup_commands_write_every_route_field_explicitly():
    commands = _setup_cli17000_commands(_cloud_route_targets("192.168.50.77", 1516))

    assert any("margeServerUrl http://192.168.50.77:1516" in command for command in commands)
    assert any("statsServerUrl http://192.168.50.77:1516" in command for command in commands)
    assert any("swUpdateUrl http://192.168.50.77:1516/updates/soundtouch" in command for command in commands)


def test_setup_live_write_guard_uses_configured_allowlist(monkeypatch):
    settings = Settings(setup_write_radio_ips=("192.0.2.141",))
    monkeypatch.setattr(setup, "get_settings", lambda: settings)

    setup._require_setup_write_allowed(Device(device_id="OK", ip_address="192.0.2.141"))
    with pytest.raises(Exception) as exc:
        setup._require_setup_write_allowed(Device(device_id="NO", ip_address="192.0.2.47"))
    assert getattr(exc.value, "status_code", None) == 403


def test_ssh_unavailable_returns_remote_services_hint(monkeypatch):
    async def unavailable(*_args, **_kwargs):
        return {"returncode": 255, "stdout": "", "stderr": "connection refused"}

    monkeypatch.setattr(api, "_run_ssh_readonly_command", unavailable)
    result = asyncio.run(api._read_ssh_hosts("192.0.2.141"))

    assert result["available"] is False
    assert result["message"] == "CLI-only ist möglich. Full redirect benötigt SSH/remote_services."


def test_runtime_code_has_no_mathias_live_target_hardcoded():
    from pathlib import Path

    hits = [path for path in Path("basswiesn").rglob("*.py") if "192.168.50.200" in path.read_text(encoding="utf-8")]
    assert hits == []


def test_setup_wizard_steps_cover_enduser_flow():
    keys = [step["key"] for step in _setup_wizard_steps()]

    assert keys == ["server", "radio", "backup", "route", "reboot", "verify", "rollback"]


def test_setup_wizard_server_info_endpoint():
    with TestClient(create_web_app()) as client:
        response = client.get("/api/setup/wizard/server-info")

    assert response.status_code == 200
    body = response.json()
    assert body["strategy"] == "cli17000_cloud_route"
    assert body["cloud_port"] == 1516
    assert body["steps"]


def test_setup_wizard_server_info_prefers_configured_lan_host(monkeypatch):
    settings = Settings(
        lan_host="192.168.50.77",
        lan_host_configured=True,
        local_base_url="http://192.168.50.77:1516",
        web_base_url="http://192.168.50.77:1328",
        debug_base_url="http://192.168.50.77:1860",
    )
    monkeypatch.setattr(setup, "get_settings", lambda: settings)
    monkeypatch.setattr(
        setup.api_core,
        "_lan_ip_candidates",
        lambda: [{"ip": "172.18.0.2", "suggested_cidr": "172.18.0.0/24"}],
    )
    monkeypatch.setattr(setup.api_core, "_tcp_port_open", lambda *_args, **_kwargs: (True, "ok"))

    with TestClient(create_web_app()) as client:
        response = client.get("/api/setup/wizard/server-info")

    assert response.status_code == 200
    body = response.json()
    assert body["recommended_host"] == "192.168.50.77"
    assert body["suggested_scan_cidr"] == "192.168.50.0/24"
    assert body["cloud_base_url"] == "http://192.168.50.77:1516"
    assert any(row["ip"] == "172.18.0.2" for row in body["ip_candidates"])


def _server_info_settings(*, configured=False):
    return Settings(
        lan_host="192.168.50.20",
        lan_host_configured=configured,
        local_base_url="http://192.168.50.20:1516",
        web_base_url="http://192.168.50.20:1328",
        debug_base_url="http://192.168.50.20:1860",
    )


def test_server_info_prefers_safe_request_host_without_environment(monkeypatch):
    monkeypatch.setattr(setup, "get_settings", lambda: _server_info_settings())
    monkeypatch.setattr(setup.api_core, "_lan_ip_candidates", lambda: [])
    monkeypatch.setattr(setup.api_core, "_tcp_port_open", lambda *_args, **_kwargs: (True, "ok"))

    with TestClient(create_web_app(), base_url="http://192.168.50.77:1328") as client:
        body = client.get("/api/setup/wizard/server-info").json()

    assert body["recommended_host"] == "192.168.50.77"
    assert body["host_source"] == "browser"
    assert body["host_warning"] is None
    assert body["suggested_scan_cidr"] == "192.168.50.0/24"


def test_server_info_request_host_wins_over_stale_saved_lan_host(monkeypatch):
    monkeypatch.setattr(setup, "get_settings", lambda: _server_info_settings(configured=True))
    monkeypatch.setattr(setup, "_saved_lan_host", lambda: "192.168.50.20")
    monkeypatch.setattr(
        setup.api_core,
        "_lan_ip_candidates",
        lambda: [{"ip": "192.168.50.185", "suggested_cidr": "192.168.50.0/24", "source": "wlan0"}],
    )
    monkeypatch.setattr(setup.api_core, "_tcp_port_open", lambda *_args, **_kwargs: (True, "ok"))

    with TestClient(create_web_app(), base_url="http://192.168.50.185:1328") as client:
        body = client.get("/api/setup/wizard/server-info").json()

    assert body["recommended_host"] == "192.168.50.185"
    assert body["host_source"] == "browser"
    assert body["cloud_base_url"] == "http://192.168.50.185:1516"


def test_server_info_safe_request_host_overrides_environment(monkeypatch):
    monkeypatch.setattr(setup, "get_settings", lambda: _server_info_settings(configured=True))
    monkeypatch.setattr(setup.api_core, "_lan_ip_candidates", lambda: [])
    monkeypatch.setattr(setup.api_core, "_tcp_port_open", lambda *_args, **_kwargs: (True, "ok"))

    with TestClient(create_web_app(), base_url="http://192.168.50.77:1328") as client:
        body = client.get("/api/setup/wizard/server-info").json()

    assert body["recommended_host"] == "192.168.50.77"
    assert body["host_source"] == "browser"


def test_server_info_rejects_docker_and_loopback_request_hosts(monkeypatch):
    monkeypatch.setattr(setup, "get_settings", lambda: Settings(lan_host="content.api.bose.io"))
    monkeypatch.setattr(
        setup.api_core,
        "_lan_ip_candidates",
        lambda: [{"ip": "172.18.0.2", "suggested_cidr": "172.18.0.0/24", "source": "docker"}],
    )
    monkeypatch.setattr(setup.api_core, "_tcp_port_open", lambda *_args, **_kwargs: (False, "closed"))

    with TestClient(create_web_app(), base_url="http://172.18.0.2:1328") as client:
        docker = client.get("/api/setup/wizard/server-info").json()
    with TestClient(create_web_app(), base_url="http://127.0.0.1:1328") as client:
        loopback = client.get("/api/setup/wizard/server-info").json()

    for body in (docker, loopback):
        assert body["recommended_host"] == ""
        assert body["host_safe"] is False
        assert body["host_warning"] == "Bitte LAN-IP des BASSWIESN Hosts eintragen; localhost, Docker-IPs und öffentliche Hosts sind für Radios nicht erreichbar."


def test_browser_service_status_is_logged_without_arbitrary_service(monkeypatch):
    events = []
    monkeypatch.setattr(setup, "write_masterlog", lambda event, **fields: events.append((event, fields)))

    with TestClient(create_web_app()) as client:
        response = client.post(
            "/api/setup/wizard/service-status",
            json={"service": "cloud", "online": False, "reason": "Failed to fetch"},
        )
        rejected = client.post(
            "/api/setup/wizard/service-status",
            json={"service": "other", "online": True},
        )

    assert response.status_code == 200
    assert rejected.status_code == 400
    assert events == [("service_status_check", {"service": "cloud", "online": False, "error_reason": "Failed to fetch"})]
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
