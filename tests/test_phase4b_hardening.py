import asyncio
from collections import defaultdict
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile, ZipInfo

import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.config import get_settings
from basswiesn.app.core import masterlog
from basswiesn.app.main import create_web_app
from basswiesn.app.main import _duplicate_contract_routes
from basswiesn.app.services.archive_security import UnsafeArchive, validate_archive_members
from basswiesn.app.services.filesystem_contract import FilesystemContractError, ensure_runtime_directories
from basswiesn.app.services.safe_uploads import InvalidUpload, UploadTooLarge, store_upload_stream
from basswiesn.app.services.support_export import build_support_bundle
from basswiesn.app.services.runtime_cleanup import run_runtime_cleanup
from basswiesn.app.services.task_registry import clear_owned_tasks_for_tests, start_owned_task, stop_owned_task


pytestmark = pytest.mark.integration


def test_safe_upload_rejects_traversal_and_invalid_magic(tmp_path):
    upload_dir = tmp_path / "upload"
    with pytest.raises(InvalidUpload):
        store_upload_stream(BytesIO(b"not-an-image"), original_name="../logo.png", directory=upload_dir, max_bytes=1024, quota_bytes=4096)
    with pytest.raises(InvalidUpload):
        store_upload_stream(BytesIO(b"not-an-image"), original_name="logo.png", directory=upload_dir, max_bytes=1024, quota_bytes=4096)


def test_safe_upload_is_bounded_atomic_and_unique(tmp_path):
    upload_dir = tmp_path / "upload"
    png = b"\x89PNG\r\n\x1a\n" + b"payload"
    stored = store_upload_stream(BytesIO(png), original_name="logo.png", directory=upload_dir, max_bytes=1024, quota_bytes=4096, content_type="image/png")
    assert stored.path.exists()
    assert stored.path.parent == upload_dir
    assert stored.filename != "logo.png"
    assert not list(upload_dir.glob(".upload-*"))
    with pytest.raises(UploadTooLarge):
        store_upload_stream(BytesIO(b"x" * 1025), original_name="a.mp3", directory=upload_dir, max_bytes=1024, quota_bytes=4096, content_type="audio/mpeg")


def test_station_upload_returns_security_specific_errors(monkeypatch):
    settings = get_settings().model_copy(update={"station_upload_max_mb": 1, "station_upload_quota_mb": 4})
    from basswiesn.app.routers import stations_presets
    monkeypatch.setattr(stations_presets, "get_settings", lambda: settings)
    with TestClient(create_web_app()) as client:
        too_large = client.post("/api/stations/upload", params={"name": "large"}, files={"file": ("a.mp3", b"x" * (1024 * 1024 + 1), "audio/mpeg")})
        wrong_type = client.post("/api/stations/upload", params={"name": "wrong"}, files={"file": ("a.txt", b"hello", "text/plain")})
    assert too_large.status_code == 413
    assert wrong_type.status_code == 415


def test_archive_validator_rejects_symlinks_and_expansion_limits():
    info = ZipInfo("escape")
    info.external_attr = (0o120777 << 16) | 0xA0000000
    with pytest.raises(UnsafeArchive):
        validate_archive_members([info])

    normal = ZipInfo("large.bin")
    normal.file_size = 100
    with pytest.raises(UnsafeArchive):
        validate_archive_members([normal], max_uncompressed_bytes=10)


def test_support_bundle_has_manifest_checksums_and_is_deterministic():
    first = build_support_bundle({"z.txt": "z", "a.json": json.dumps({"token": "hidden"})}, max_bytes=4096, metadata={"kind": "test"}).getvalue()
    second = build_support_bundle({"z.txt": "z", "a.json": json.dumps({"token": "hidden"})}, max_bytes=4096, metadata={"kind": "test"}).getvalue()
    assert first == second
    with ZipFile(BytesIO(first)) as archive:
        assert archive.namelist() == ["a.json", "z.txt", "support-manifest.json", "SHA256SUMS"]
        assert "hidden" not in archive.read("a.json").decode()
        assert "a.json" in archive.read("SHA256SUMS").decode()


def test_masterlog_rotation_keeps_active_file(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_dir=tmp_path, masterlog_enabled=True, masterlog_max_mb=1, masterlog_backup_count=2)
    monkeypatch.setattr(masterlog, "get_settings", lambda: settings)
    path = tmp_path / "logs" / "master.log"
    path.parent.mkdir()
    path.write_bytes(b"x" * (1024 * 1024))
    masterlog.write_masterlog("after-limit")
    assert path.exists()
    assert (tmp_path / "logs" / "master.log.1").exists()


def test_runtime_cleanup_only_removes_known_rotated_files(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "master.log").write_bytes(b"active")
    (logs / "master.log.1").write_bytes(b"old")
    (logs / "unrelated.bin").write_bytes(b"keep")
    (logs / "master.log.2").symlink_to(tmp_path / "outside.log")
    result = run_runtime_cleanup(tmp_path, max_bytes_by_area={"logs": 2}, dry_run=True)
    paths = {item["path"] for item in result["planned"]}
    assert str(logs / "master.log") not in paths
    assert str(logs / "unrelated.bin") not in paths
    assert str(logs / "master.log.2") not in paths
    assert str(logs / "master.log.1") in paths

    result = run_runtime_cleanup(tmp_path, max_bytes_by_area={"logs": 2}, dry_run=False)
    assert result["deleted"] == 1
    assert (logs / "master.log").exists()
    assert (logs / "unrelated.bin").exists()


def test_filesystem_contract_rejects_symlinked_runtime_directory(tmp_path):
    (tmp_path / "logs").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(FilesystemContractError):
        ensure_runtime_directories(tmp_path)


def test_owned_task_start_is_idempotent_and_shutdown_is_clean():
    async def scenario():
        clear_owned_tasks_for_tests()
        stop = asyncio.Event()

        async def worker():
            await stop.wait()

        first = start_owned_task("test-owner", worker, stop_event=stop)
        second = start_owned_task("test-owner", worker, stop_event=stop)
        assert first is second
        await stop_owned_task("test-owner")
        assert first.done()
        clear_owned_tasks_for_tests()

    asyncio.run(scenario())


def test_owned_task_exception_is_logged_and_does_not_escape_shutdown(monkeypatch):
    from basswiesn.app.services import task_registry
    events = []
    monkeypatch.setattr(task_registry, "write_masterlog", lambda event, **fields: events.append((event, fields)))

    async def scenario():
        clear_owned_tasks_for_tests()

        async def failing_worker():
            raise RuntimeError("expected")

        task = start_owned_task("failing-owner", failing_worker)
        await task
        await stop_owned_task("failing-owner")
        clear_owned_tasks_for_tests()

    asyncio.run(scenario())
    assert any(event == "background_task_failed" for event, _ in events)


def test_readiness_is_local_and_response_contracts_are_stable(monkeypatch):
    from basswiesn.app.routers import api

    monkeypatch.setattr(api, "_tcp_port_open", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("readiness contacted a device")))
    with TestClient(create_web_app()) as client:
        readiness = client.get("/api/readiness")
        version = client.get("/api/version")
    assert readiness.status_code == 200
    assert readiness.json()["ready"] is True
    assert version.json()["build_type"] == "Stable Release"


def test_telnet_reboot_contract_is_unique():
    routes = _duplicate_contract_routes(create_web_app())
    assert routes == []


def test_web_app_has_no_duplicate_method_path_contracts():
    contracts: dict[tuple[str, str], list[str]] = defaultdict(list)
    app = create_web_app(background_tasks=False)
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            context = getattr(route, "include_context", None)
            prefix = getattr(context, "prefix", "")
            candidates = getattr(original_router, "routes", [])
        else:
            prefix = ""
            candidates = [route]
        for candidate in candidates:
            path = getattr(candidate, "path", "")
            full_path = path if path.startswith(prefix or "\0") else f"{prefix}{path}"
            handler = getattr(getattr(candidate, "endpoint", None), "__name__", "unknown")
            for method in getattr(candidate, "methods", set()) or set():
                contracts[(method, full_path)].append(handler)
    duplicates = {
        f"{method} {path}": handlers
        for (method, path), handlers in contracts.items()
        if len(handlers) > 1
    }
    assert duplicates == {}


def test_normal_ui_javascript_never_calls_retired_setup_devices_endpoint():
    javascript = Path("basswiesn/app/static/app.js").read_text(encoding="utf-8")
    assert 'getJson("/api/setup/devices")' not in javascript
