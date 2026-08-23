from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device
from basswiesn.app.services.maintenance_reboot import run_due_maintenance, run_maintenance_reboot


def test_problem_radio_defaults_are_safe():
    row = Device(device_id="SAFEDEFAULT", ip_address="192.0.2.190")
    assert row.maintenance_reboot_enabled is None or row.maintenance_reboot_enabled is False


def test_legacy_scheduler_state_is_disabled_without_device_access():
    import asyncio
    db = app_db.SessionLocal()
    disabled = Device(device_id="DISABLED", ip_address="192.0.2.191", maintenance_reboot_enabled=False)
    enabled = Device(device_id="ENABLED", ip_address="192.0.2.192", maintenance_reboot_enabled=True, maintenance_reboot_interval_hours=24)
    db.add_all([disabled, enabled])
    db.commit()
    assert asyncio.run(run_due_maintenance(db)) == []
    assert disabled.maintenance_next_run_at is None
    assert enabled.maintenance_reboot_enabled is False
    assert enabled.maintenance_next_run_at is None
    assert enabled.maintenance_last_result == "automatic_reboot_disabled_in_1_6"
    db.close()


def test_automatic_reboot_entrypoint_is_denied_before_any_preflight_or_client():
    import asyncio

    db = app_db.SessionLocal()
    device = Device(device_id="AUTODENIED", ip_address="192.0.2.193")
    db.add(device)
    db.commit()

    async def forbidden_cli(*_args, **_kwargs):
        raise AssertionError("automatic reboot must not reach CLI")

    def forbidden_client(*_args, **_kwargs):
        raise AssertionError("automatic reboot must not create a radio client")

    result = asyncio.run(
        run_maintenance_reboot(
            device,
            db,
            trigger="automatic",
            send_cli=forbidden_cli,
            client_factory=forbidden_client,
        )
    )
    db.close()

    assert result["ok"] is False
    assert result["code"] == "AUTOMATIC_RADIO_REBOOT_DISABLED"


def test_manual_reboot_api_is_lab_only_and_scheduler_cannot_be_enabled():
    with TestClient(create_web_app()) as client:
        created = client.post(
            "/api/devices",
            json={"device_id": "LABGUARD", "name": "Guard", "ip_address": "192.0.2.194", "model": "SoundTouch Test"},
        )
        schedule = client.put(
            "/api/devices/LABGUARD/maintenance-reboot",
            json={"enabled": True, "interval_hours": 24},
        )
        manual = client.post(
            "/api/devices/LABGUARD/maintenance-reboot/run",
            json={"confirmation": "REBOOT RADIO"},
        )

    assert created.status_code == 200
    assert schedule.status_code == 409
    assert schedule.json()["detail"]["error"] == "automatic_radio_reboot_disabled"
    assert manual.status_code == 403
    assert manual.json()["detail"]["error"] == "experimental_lab_only"


def test_web_startup_never_registers_maintenance_reboot_task(monkeypatch):
    from basswiesn.app import main as main_module

    started = []

    def capture_start(name, factory, *, stop_event=None):
        del factory, stop_event
        started.append(name)
        return None

    monkeypatch.setattr(main_module, "start_owned_task", capture_start)
    with TestClient(create_web_app(background_tasks=True)) as client:
        assert client.get("/api/health").status_code == 200

    assert "playback_keepalive" in started
    assert "alarm_engine" in started
    assert "maintenance_reboot" not in started
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
