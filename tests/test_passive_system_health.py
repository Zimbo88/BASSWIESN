"""Page/diagnostic reads must not initiate radio tests or alter local files."""
import json

import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.config import get_settings
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device
from basswiesn.app.routers import api
from basswiesn.app.services import runtime_health

pytestmark = pytest.mark.integration


def test_health_read_has_no_radio_probes_or_tls_generation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(api, "_tcp_port_open", lambda *a, **k: (calls.append("radio") or False, "not probed"))
    def no_tls(*args):
        calls.append("TLS side effect")
        raise AssertionError("TLS creation forbidden on read")
    monkeypatch.setattr(api, "ensure_tls_files", no_tls)
    with app_db.SessionLocal() as db:
        db.add(Device(device_id="EXAMPLE", name="Example Radio", ip_address="192.0.2.42"))
        db.commit()
    response = TestClient(create_web_app(background_tasks=False)).get("/api/system/healthcheck")
    assert response.status_code == 200
    assert calls == []


def test_health_read_preserves_existing_file_and_missing_runtime_directories(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    root.mkdir()
    sentinel = root / ".healthcheck-write"
    sentinel.write_bytes(b"do not overwrite or delete")
    monkeypatch.setattr(get_settings(), "data_dir", root)
    response = TestClient(create_web_app(background_tasks=False)).get("/api/system/healthcheck")
    assert response.status_code == 200
    assert sentinel.read_bytes() == b"do not overwrite or delete"
    assert not (root / "tls").exists()


def _manifest():
    return {"format": 1, "version": get_settings().version,
            "files": [{"path": "README.md", "sha256": "a" * 64, "size": 10}]}


@pytest.mark.parametrize("mutation", [
    lambda value: None,
    lambda value: {**value, "format": 2},
    lambda value: {**value, "version": ""},
    lambda value: {**value, "version": "0.0.0-invalid"},
    lambda value: {**value, "files": []},
    lambda value: {**value, "files": [None]},
    lambda value: {**value, "files": [{"path": "/absolute", "sha256": "a" * 64, "size": 0}]},
    lambda value: {**value, "files": [{"path": "../outside", "sha256": "a" * 64, "size": 0}]},
    lambda value: {**value, "files": [{"path": "README.md", "sha256": "invalid", "size": 0}]},
    lambda value: {**value, "files": [{"path": "README.md", "sha256": "a" * 64, "size": -1}]},
])
def test_manifest_diagnostic_rejects_invalid_shape_without_integrity_claim(mutation):
    valid, message, _ = runtime_health.validate_manifest_data(
        mutation(_manifest()), expected_version=get_settings().version)
    assert valid is False and message


def test_manifest_diagnostic_is_bounded_schema_only_not_file_verification(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest()))
    result = runtime_health.manifest_status(required=True, candidates=[manifest])
    assert result["valid"] is True and result["verification_scope"] == "schema_only"
    assert not (tmp_path / "README.md").exists()
    manifest.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    result = runtime_health.manifest_status(required=True, candidates=[manifest])
    assert result["valid"] is False and result["message"] == "manifest parse failed: ValueError"


def test_unreadable_runtime_state_is_degraded_without_repair_or_error_leak(tmp_path, monkeypatch):
    root = tmp_path / "unavailable-runtime"
    monkeypatch.setattr(get_settings(), "data_dir", root)
    def unavailable(*args, **kwargs):
        raise OSError("private path canary")
    monkeypatch.setattr(runtime_health, "ensure_runtime_directories", unavailable)
    class FailedDatabase:
        def execute(self, statement):
            raise RuntimeError("database password canary")
    result = runtime_health.readiness(FailedDatabase())
    assert result["ready"] is False and result["status"] == "not_ready"
    assert result["checks"]["storage"]["write_probe"] == "NOT_RUN"
    assert result["error"] == "RuntimeError" and "canary" not in json.dumps(result)
    assert not root.exists()


@pytest.mark.parametrize("present", [False, True])
def test_health_does_not_certify_tls_from_certificate_presence(tmp_path, monkeypatch, present):
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_https", True)
    monkeypatch.setattr(settings, "cert_mode", "external")
    monkeypatch.setattr(settings, "tls_cert_file", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(settings, "tls_key_file", str(tmp_path / "key.pem"))
    if present:
        (tmp_path / "cert.pem").write_text("not a real certificate")
        (tmp_path / "key.pem").write_text("not a real private key")
    with app_db.SessionLocal() as db:
        result = runtime_health.release_healthcheck(db)
    check = next(row for row in result["checks"] if row["name"] == "https_status")
    assert check["status"] == "yellow"
    assert ("TLS not probed" if present else "missing") in check["message"]
