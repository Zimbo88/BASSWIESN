import asyncio
from pathlib import Path
import tarfile

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from basswiesn.app import db as app_db
from basswiesn.app.config import get_settings
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, MediaRoot, WebhookEndpoint
from basswiesn.app.services.announcements import announcements_status
from basswiesn.app.services.dlna_experimental import dlna_status, discover_renderers
from basswiesn.app.services.events import create_event, list_events
from basswiesn.app.services.local_media import resolve_media_path
from basswiesn.app.services.local_updates import inspect_local_release_archive, prepare_local_update
from basswiesn.app.services.model_library import resolve_device_model, set_capability_override
from basswiesn.app.services.network_security import validate_local_soundtouch_url, validate_public_callback_url
from basswiesn.app.services.quick_fixes import CONFIRMATION_PHRASE, execute_quick_fix
from basswiesn.app.services.setup_jobs import create_setup_job, list_setup_jobs, update_setup_job_status
from basswiesn.app.services.ssdp_discovery import SSDPCandidate, discover_ssdp, parse_ssdp_response
from basswiesn.app.services.webhooks import deliver_webhook, upsert_webhook_endpoint


def test_local_test_migration_tables_and_device_discovery_columns_exist():
    engine = app_db.engine
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {
        "device_model_definitions",
        "device_capability_overrides",
        "discovery_events",
        "healthcheck_runs",
        "healthcheck_results",
        "quick_fix_runs",
        "device_interactions",
        "events",
        "webhook_endpoints",
        "webhook_deliveries",
        "system_backups",
        "restore_jobs",
        "update_jobs",
        "setup_jobs",
        "setup_job_steps",
        "media_roots",
        "media_items",
        "media_playlist_items",
        "announcement_jobs",
        "telnet_device_profiles",
        "telnet_jobs",
        "standby_clock_jobs",
        "device_firmware_profiles",
        "device_capability_evidence",
        "webhook_delivery_queue",
        "restore_execution_jobs",
        "update_execution_jobs",
        "media_scan_jobs",
        "dlna_renderers",
        "health_status_history",
    } <= tables
    device_columns = {column["name"] for column in inspector.get_columns("devices")}
    assert {"discovery_method", "discovery_confidence", "descriptor_validated", "identity_verified"} <= device_columns
    with engine.connect() as connection:
        migrations = {row[0] for row in connection.execute(text("SELECT version FROM schema_migrations"))}
    assert {"1.1.0-discovery", "1.1.0-health", "1.1.0-events", "1.1.0-webhooks", "1.1.0-media", "1.5.0-core", "1.5.0-telnet-control", "1.5.0-standby-clock"} <= migrations


def test_model_library_classifies_portable_and_keeps_unknown_safe():
    db = app_db.SessionLocal()
    portable = Device(device_id="MODELPORT", name="Küche", model="SoundTouch Portable", ip_address="192.0.2.10")
    unknown = Device(device_id="MODELUNK", name="Mystery", model="SoundTouch X", ip_address="192.0.2.11")
    db.add_all([portable, unknown])
    db.commit()

    resolved_portable = resolve_device_model(portable, db)
    resolved_unknown = resolve_device_model(unknown, db)
    set_capability_override(db, unknown.device_id, "volume_supported", "true", "lab verified")
    resolved_override = resolve_device_model(unknown, db)
    db.close()

    assert resolved_portable.device_class == "portable"
    assert resolved_portable.capabilities["safe_auto_power"] is True
    assert resolved_portable.capabilities["battery_supported"] is False
    assert resolved_unknown.device_class == "unknown"
    assert resolved_unknown.capabilities["power_key_supported"] is False
    assert resolved_override.capabilities["volume_supported"] is True


def test_network_security_blocks_ssrf_and_allows_test_lan_descriptor():
    assert validate_local_soundtouch_url("http://192.0.2.44:8090/description.xml").ok is True
    assert validate_local_soundtouch_url("http://192.0.2.44:8091/description.xml").ok is False
    assert validate_local_soundtouch_url(
        "http://192.0.2.44:8091/description.xml", allowed_ports={80, 8090, 8091}
    ).ok is True
    assert validate_local_soundtouch_url("https://192.0.2.44/description.xml").ok is False
    assert validate_local_soundtouch_url("http://93.184.216.34/description.xml").ok is False
    assert validate_public_callback_url("http://127.0.0.1/hook").ok is False
    assert validate_public_callback_url("http://169.254.169.254/latest").ok is False
    assert validate_public_callback_url("https://93.184.216.34/hook").ok is True


def test_ssdp_response_is_deduplicated_and_upserts_device(monkeypatch):
    db = app_db.SessionLocal()
    raw = (
        b"HTTP/1.1 200 OK\r\n"
        b"LOCATION: http://192.0.2.55:8090/description.xml\r\n"
        b"USN: uuid:08DF1FTEST::upnp:rootdevice\r\n"
        b"SERVER: Bose SoundTouch\r\n\r\n"
    )
    candidate = parse_ssdp_response(raw, "192.0.2.55")
    assert candidate is not None

    async def fake_fetch(_candidate, timeout_seconds):
        return True, {"friendlyName": "Portable", "manufacturer": "Bose", "modelName": "SoundTouch Portable", "UDN": "uuid:08DF1FTEST"}, "ok"

    monkeypatch.setattr("basswiesn.app.services.ssdp_discovery._fetch_descriptor", fake_fetch)
    result = asyncio.run(discover_ssdp(db, candidates=[candidate, candidate]))
    db.commit()
    device = db.query(Device).filter(Device.device_id == "08DF1FTEST").one()
    db.close()

    assert len(result["devices"]) == 1
    assert device.discovery_method == "ssdp"
    assert device.identity_verified is True


def test_ssdp_accepts_soundtouch_upnp_descriptor_port_8091(monkeypatch):
    db = app_db.SessionLocal()
    candidate = SSDPCandidate(
        location="http://192.0.2.56:8091/XD/device.xml",
        usn="uuid:08DF1FPORT8091::upnp:rootdevice",
        server="Bose SoundTouch",
        st="ssdp:all",
        remote_ip="192.0.2.56",
    )

    async def fake_fetch(_candidate, timeout_seconds):
        assert timeout_seconds >= 1
        return True, {
            "friendlyName": "Port 8091 Radio",
            "manufacturer": "Bose",
            "modelName": "SoundTouch 20",
            "UDN": "uuid:08DF1FPORT8091",
        }, "ok"

    monkeypatch.setattr("basswiesn.app.services.ssdp_discovery._fetch_descriptor", fake_fetch)
    try:
        result = asyncio.run(discover_ssdp(db, candidates=[candidate]))
        assert [item["device_id"] for item in result["devices"]] == ["08DF1FPORT8091"]
        assert result["errors"] == []
    finally:
        db.close()


def test_ssdp_normalizes_real_soundtouch_upnp_uuid_and_keeps_identity_per_candidate(monkeypatch):
    db = app_db.SessionLocal()
    first_id = "A1B2C3D4E5F6"
    second_id = "B1C2D3E4F5A6"
    candidates = [
        SSDPCandidate(
            location=f"http://192.0.2.61:8091/XD/BO5EBO5E-F00D-F00D-FEED-{first_id}.xml",
            usn=f"uuid:BO5EBO5E-F00D-F00D-FEED-{first_id}::upnp:rootdevice",
            server="Bose SoundTouch",
            st="ssdp:all",
            remote_ip="192.0.2.61",
        ),
        SSDPCandidate(
            location=f"http://192.0.2.62:8091/XD/BO5EBO5E-F00D-F00D-FEED-{second_id}.xml",
            usn=f"uuid:BO5EBO5E-F00D-F00D-FEED-{second_id}::upnp:rootdevice",
            server="Bose SoundTouch",
            st="ssdp:all",
            remote_ip="192.0.2.62",
        ),
    ]

    async def fake_fetch(candidate, timeout_seconds):
        assert timeout_seconds >= 1
        device_id = first_id if candidate.remote_ip.endswith("61") else second_id
        return True, {
            "friendlyName": f"Radio {device_id}",
            "manufacturer": "Bose",
            "modelName": "SoundTouch",
            "UDN": f"uuid:BO5EBO5E-F00D-F00D-FEED-{device_id}",
        }, "ok"

    monkeypatch.setattr("basswiesn.app.services.ssdp_discovery._fetch_descriptor", fake_fetch)
    try:
        result = asyncio.run(discover_ssdp(db, candidates=candidates))
        assert {item["device_id"] for item in result["devices"]} == {first_id, second_id}
        assert result["errors"] == []
    finally:
        db.close()


def test_health_quickfix_event_setup_and_feature_flags():
    db = app_db.SessionLocal()
    device = Device(device_id="LOCALTESTDEV", name="Test", model="SoundTouch 20", ip_address="192.0.2.66", failure_count=4, reachable=False)
    db.add(device)
    db.commit()

    try:
        execute_quick_fix(db, "reset_circuit_breaker", confirmation="wrong", device_id=device.device_id)
        assert False, "confirmation must be required"
    except PermissionError:
        pass
    result = execute_quick_fix(db, "reset_circuit_breaker", confirmation=CONFIRMATION_PHRASE, device_id=device.device_id)
    event = create_event(db, "device_online", device_id=device.device_id, payload={"token": "secret-value"})
    job = create_setup_job(db, {"device_id": device.device_id, "job_type": "cloud_route"})
    updated = update_setup_job_status(db, job["job_id"], "waiting")
    db.commit()

    assert result["result"]["changed"] is True
    assert db.query(Device).filter(Device.device_id == device.device_id).one().failure_count == 0
    assert list_events(db, device_id=device.device_id)
    assert event.event_type == "device_online"
    assert updated["status"] == "waiting"
    assert list_setup_jobs(db)[0]["auto_resume"] is False
    assert dlna_status()["enabled"] is False
    assert asyncio.run(discover_renderers())["skipped"] is True
    assert announcements_status()["enabled"] is False
    db.close()


def test_webhooks_are_disabled_by_default_and_do_not_send_network():
    db = app_db.SessionLocal()
    endpoint = upsert_webhook_endpoint(db, {
        "name": "Test",
        "url": "https://93.184.216.34/hook",
        "enabled": True,
        "event_types": ["device_offline"],
        "secret": "not-logged",
    })
    event = create_event(db, "device_offline", payload={"device": {"id": "X"}})
    db.flush()
    result = asyncio.run(deliver_webhook(db, endpoint, event))
    db.commit()
    loaded = db.query(WebhookEndpoint).filter(WebhookEndpoint.id == endpoint.id).one()
    db.close()

    assert result["skipped"] is True
    assert loaded.secret_ref.startswith("local-file:")


def test_media_paths_block_traversal_and_symlink_escape(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "song.mp3").write_bytes(b"ID3")
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"ID3")
    (root / "escape.mp3").symlink_to(outside)
    db = app_db.SessionLocal()
    media_root = MediaRoot(path=str(root.resolve()), enabled=True)
    db.add(media_root)
    db.commit()

    ok = resolve_media_path(db, media_root.id, "song.mp3")
    traversal = resolve_media_path(db, media_root.id, "../outside.mp3")
    symlink = resolve_media_path(db, media_root.id, "escape.mp3")
    db.close()

    assert ok.ok is True
    assert traversal.ok is False
    assert "traversal" in traversal.reason
    assert symlink.ok is False
    assert "symlink" in symlink.reason


def test_local_update_preview_blocks_tar_slip_and_prepares_safe_archive(tmp_path):
    unsafe = tmp_path / "unsafe.tar.gz"
    with tarfile.open(unsafe, "w:gz") as archive:
        payload = tmp_path / "payload.txt"
        payload.write_text("x", encoding="utf-8")
        archive.add(payload, arcname="../payload.txt")
    assert inspect_local_release_archive(str(unsafe))["ok"] is False

    safe = tmp_path / "safe.tar.gz"
    for name in ("docker-compose.yml", "Dockerfile", "README.md"):
        (tmp_path / name).write_text("ok", encoding="utf-8")
    with tarfile.open(safe, "w:gz") as archive:
        for name in ("docker-compose.yml", "Dockerfile", "README.md"):
            archive.add(tmp_path / name, arcname=f"basswiesn/{name}")
    db = app_db.SessionLocal()
    preview = inspect_local_release_archive(str(safe))
    prepared = prepare_local_update(db, str(safe), expected_sha256=preview["sha256"], confirmation="BASSWIESN LOCAL UPDATE")
    db.commit()
    db.close()

    assert preview["ok"] is True
    assert prepared["ok"] is True
    assert prepared["remote_publish"] is False


def test_fulltest_router_smoke():
    with TestClient(create_web_app()) as client:
        assert client.get("/api/device-models").status_code == 200
        assert client.get("/api/quick-fixes").status_code == 200
        assert client.get("/api/events").status_code == 200
        assert client.get("/api/webhooks").json()["enabled_globally"] is False
        assert client.get("/api/dlna/status").json()["enabled"] is False
        assert client.get("/api/announcements/status").json()["enabled"] is False
        assert client.get("/api/lab/status").json()["factory_reset_executable"] is False
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
