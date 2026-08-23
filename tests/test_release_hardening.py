import io
import json
from zipfile import ZipFile

from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, RuntimeState, TelemetryEvent
from basswiesn.app.routers import api
from basswiesn.app.config import get_settings


def test_support_bundle_zip_contains_release_files_and_redacts_secrets():
    db = app_db.SessionLocal()
    db.add(Device(device_id="SUPPORT1", name="Support Radio", ip_address="192.0.2.90", model="SoundTouch Portable"))
    db.add(RuntimeState(key="setup_job:test", value=json.dumps({"job_id": "test", "token": "secret-token"})))
    db.add(TelemetryEvent(device_id="SUPPORT1", event_type="battery", endpoint="/powerManagement", payload="password=secret", parsed_summary="ok"))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.get("/api/support-bundle")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="support_bundle.zip"'
    with ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {
            "version.json",
            "manifest.json",
            "healthcheck.json",
            "devices.json",
            "masterlog.txt",
            "emulator_gaps.json",
            "setup_jobs.json",
            "battery_polling_removed.json",
        } <= names
        setup_jobs = archive.read("setup_jobs.json").decode("utf-8")
        assert "secret-token" not in setup_jobs
        assert "***REDACTED***" in setup_jobs


def test_system_healthcheck_returns_release_status_and_checks():
    with TestClient(create_web_app()) as client:
        response = client.get("/api/system/healthcheck")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"green", "yellow", "red"}
    names = {item["name"] for item in payload["checks"]}
    assert {"api_alive", "database_alive", "writable_storage", "manifest_valid", "emulator_healthy", "websocket_polling"} <= names


def test_manifest_check_is_explicitly_optional_without_a_release_artifact(monkeypatch):
    monkeypatch.setattr(api, "_manifest_candidates", lambda: [])

    result = api._manifest_status(required=False)

    assert result["present"] is False
    assert result["valid"] is True
    assert result["runtime_optional"] is True
    assert "source development" in result["message"]


def test_manifest_check_rejects_invalid_release_metadata(monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"format": 1, "version": get_settings().version, "files": [{"path": "README.md", "sha256": "bad", "size": 1}]}), encoding="utf-8")
    monkeypatch.setattr(api, "_manifest_candidates", lambda: [manifest])

    result = api._manifest_status(required=True)

    assert result["present"] is True
    assert result["valid"] is False
    assert "sha256" in result["message"]


def test_manifest_check_rejects_unsafe_paths(monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"format": 1, "version": get_settings().version, "files": [{"path": "../outside", "sha256": "0" * 64, "size": 1}]}), encoding="utf-8")
    monkeypatch.setattr(api, "_manifest_candidates", lambda: [manifest])

    result = api._manifest_status(required=True)

    assert result["valid"] is False
    assert "unsafe path" in result["message"]


def test_multiroom_frontend_has_last_known_state_and_render_guards():
    js = open("basswiesn/app/static/app.js", encoding="utf-8").read()
    html = TestClient(create_web_app()).get("/").text

    assert 'data-view="multiroom" data-capability' not in html
    assert "lastKnownMultiroomState" in js
    assert "state.refreshSeq" in js
    assert "state.multiroomScenarios = state.lastKnownMultiroomState.scenarios" in js
    assert "state.multiroomMethods = state.lastKnownMultiroomState.methods" in js
    assert 'view.hidden = false' in js
    assert "Multiroom ist bereit. Methoden werden geladen." in js
import pytest as _pytest_marker
pytestmark = [_pytest_marker.mark.integration, _pytest_marker.mark.release]
