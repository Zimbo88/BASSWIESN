"""Retired compatibility routes fail before transport or worker construction."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from basswiesn.app.main import create_web_app
from basswiesn.app.routers import api, setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("path", [
    "/api/setup/jobs/start",
    "/api/setup/wizard/apply/RETIRED",
    "/api/setup/account/RETIRED",
    "/api/setup/activation-playback/RETIRED",
    "/api/setup/cloud-route/RETIRED/apply",
    "/api/setup/cloud-route/RETIRED/rollback",
    "/api/devices/RETIRED/factory-fix",
    "/api/devices/RETIRED/setup/live-test",
])
def test_retired_write_routes_reject_before_transport(monkeypatch, path, force):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("unexpected_transport_or_worker")
        raise AssertionError("retired entry constructed transport or scheduled work")

    device = SimpleNamespace(device_id="RETIRED", ip_address="192.0.2.42", name="Example Radio")
    monkeypatch.setattr(api, "_device_or_404", lambda *_: device)
    monkeypatch.setattr(setup, "_setup_target_host", lambda *_: "192.0.2.5")
    monkeypatch.setattr(api, "SoundTouchClient", forbidden)
    monkeypatch.setattr(api, "_tcp_port_open", forbidden)
    monkeypatch.setattr(api, "_run_ssh_readonly_command", forbidden)
    monkeypatch.setattr(api, "_send_cli17000", forbidden)
    monkeypatch.setattr(setup, "_persist_setup_job", forbidden)
    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.post(path, json={
            "dry_run": False, "device_ids": ["RETIRED"], "account_id": "12345",
            "host": "192.0.2.5", "port": 1516, "confirmation": "YES", "force": force,
            "safe_startup_volume": 1,
        })
    assert response.status_code == 410, response.text
    assert response.json()["detail"]["error"] == "legacy_setup_retired"
    assert calls == []
