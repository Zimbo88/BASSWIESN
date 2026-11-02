"""Adversarial local archives; no device or production database access."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import os
import stat
from pathlib import Path
from zipfile import ZipFile

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.config import get_settings
from basswiesn.app.services import backup_restore

pytestmark = pytest.mark.integration


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    settings = get_settings().model_copy(update={
        "data_dir": tmp_path,
        "database_url": str(app_db.engine.url),
    })
    monkeypatch.setattr(backup_restore, "get_settings", lambda: settings)
    return backup_restore.backup_root()


def manifest() -> dict:
    return {
        "format": "basswiesn-backup-v1", "version": "1.5.0", "schema_baseline": 2,
        "created_at": "2026-09-27T12:00:00+00:00", "quick_check": "ok",
        "restore_mode": "prepare_and_restart",
        "files": [
            {"path": "database.sqlite", "sha256": hashlib.sha256(b"database").hexdigest()},
            {"path": "settings.json", "sha256": hashlib.sha256(b"{}").hexdigest()},
        ],
    }


def archive_at(root: Path, payload: object, *, settings: bytes = b"{}") -> Path:
    path = root / "candidate.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(payload))
        archive.writestr("database.sqlite", b"database")
        archive.writestr("settings.json", settings)
    return path


@pytest.mark.parametrize("changes", [
    {"files": []}, {"files": None}, {"files": "database.sqlite"},
    {"format": "unknown-format"}, {"schema_baseline": 999}, {"schema_baseline": True},
    {"restore_mode": "unknown-mode"}, {"quick_check": "database corruption"},
    {"created_at": "not-a-time"}, {"version": ""},
    {"files": [{"path": "database.sqlite", "sha256": "0" * 64}]},
    {"files": [{"path": "../database.sqlite", "sha256": "0" * 64}]},
])
def test_invalid_manifest_fails_closed(backup_dir, changes):
    payload = {**manifest(), **changes}
    result = backup_restore.preview_system_backup(str(archive_at(backup_dir, payload)))
    assert result["ok"] is False


@pytest.mark.parametrize("payload", [{}, [], None, "not an object"])
def test_missing_manifest_structure_is_not_a_valid_backup(backup_dir, payload):
    result = backup_restore.preview_system_backup(str(archive_at(backup_dir, payload)))
    assert result["ok"] is False


def test_duplicate_component_is_not_an_integrity_proof(backup_dir):
    payload = deepcopy(manifest())
    payload["files"].append(payload["files"][0])
    assert not backup_restore.preview_system_backup(str(archive_at(backup_dir, payload)))["ok"]


def test_hash_mismatch_is_rejected(backup_dir):
    path = archive_at(backup_dir, manifest(), settings=b'{"changed": true}')
    assert not backup_restore.preview_system_backup(str(path))["ok"]


def test_matching_hashes_do_not_make_non_sqlite_payload_valid(backup_dir):
    path = archive_at(backup_dir, manifest())
    assert not backup_restore.preview_system_backup(str(path))["ok"]


def test_changed_candidate_is_not_prepared(backup_dir, monkeypatch):
    with app_db.SessionLocal() as db:
        source = backup_restore.create_system_backup(db)
        real_copy = backup_restore.shutil.copyfile
        def changed_during_copy(src, dst):
            result = real_copy(src, dst)
            Path(dst).write_bytes(b"changed after preview")
            return result
        monkeypatch.setattr(backup_restore.shutil, "copyfile", changed_during_copy)
        result = backup_restore.prepare_system_restore(db, source["path"])
    assert not result["prepared"]
    assert not (backup_dir / "restore-pending.zip").exists()
    assert not list(backup_dir.glob(".restore-*"))


def test_failed_safety_backup_keeps_existing_pending_restore(backup_dir, monkeypatch):
    with app_db.SessionLocal() as db:
        source = backup_restore.create_system_backup(db)
        pending = backup_dir / "restore-pending.zip"
        pending.write_bytes(b"previous pending state")
        def fail(_db):
            raise OSError("simulated disk full")
        monkeypatch.setattr(backup_restore, "create_system_backup", fail)
        result = backup_restore.prepare_system_restore(db, source["path"])
        assert not result["prepared"]
        assert result["code"] == "RESTORE_PREPARATION_FAILED"
        assert "disk full" not in str(result)
    assert pending.read_bytes() == b"previous pending state"
    assert not list(backup_dir.glob(".restore-*"))


def test_malformed_archive_returns_failed_preview(backup_dir):
    path = backup_dir / "broken.zip"
    path.write_bytes(b"not a zip archive")
    assert not backup_restore.preview_system_backup(str(path))["ok"]


def test_invalid_restore_never_creates_safety_backup_or_pending_file(backup_dir, monkeypatch):
    path = archive_at(backup_dir, {**manifest(), "files": []})
    def forbidden(_db):
        pytest.fail("invalid restore must stop before any backup or pending-file mutation")
    monkeypatch.setattr(backup_restore, "create_system_backup", forbidden)
    with app_db.SessionLocal() as db:
        result = backup_restore.prepare_system_restore(db, str(path))
    assert result["prepared"] is False
    assert not (backup_dir / "restore-pending.zip").exists()


def test_two_backups_in_one_second_do_not_overwrite_each_other(backup_dir, monkeypatch):
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(backup_restore, "datetime", FixedClock)
    with app_db.SessionLocal() as db:
        first = backup_restore.create_system_backup(db)
        original = Path(first["path"]).read_bytes()
        second = backup_restore.create_system_backup(db)
    assert first["path"] != second["path"]
    assert Path(first["path"]).read_bytes() == original
    assert Path(second["path"]).stat().st_mode & 0o777 == 0o600


def test_missing_database_cannot_produce_successful_backup(backup_dir, monkeypatch):
    monkeypatch.setattr(backup_restore, "_sqlite_path", lambda: backup_dir / "missing.sqlite")
    with app_db.SessionLocal() as db, pytest.raises(ValueError, match="database"):
        backup_restore.create_system_backup(db)
    assert not list(backup_dir.glob("*.zip"))


def test_second_prepare_cannot_overwrite_pending_restore(backup_dir):
    with app_db.SessionLocal() as db:
        source = backup_restore.create_system_backup(db)
        first = backup_restore.prepare_system_restore(db, source["path"])
        before = (backup_dir / "restore-pending.zip").read_bytes()
        second = backup_restore.prepare_system_restore(db, source["path"])
    assert first["prepared"] is True
    assert second["prepared"] is False
    assert second["code"] == "RESTORE_ALREADY_PENDING"
    assert (backup_dir / "restore-pending.zip").read_bytes() == before
    assert not list(backup_dir.glob(".restore-*"))


def test_concurrent_prepare_has_exactly_one_winner(backup_dir, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    with app_db.SessionLocal() as db:
        source = backup_restore.create_system_backup(db)
    barrier = Barrier(2)
    original = backup_restore.create_system_backup
    def synchronized_safety(_):
        with app_db.SessionLocal() as db:
            result = original(db)
        barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(backup_restore, "create_system_backup", synchronized_safety)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: backup_restore.prepare_system_restore(None, source["path"]), range(2)))
    assert sum(result["prepared"] for result in results) == 1
    assert backup_restore.preview_system_backup("restore-pending.zip")["ok"] is True


@pytest.mark.parametrize("failure", ["reported-failure", "corrupt-file"])
def test_unverified_safety_backup_cannot_authorize_restore(backup_dir, monkeypatch, failure):
    with app_db.SessionLocal() as db:
        source = backup_restore.create_system_backup(db)
        original = backup_restore.create_system_backup

        def failed_safety(session):
            if failure == "reported-failure":
                return {"ok": False}
            result = original(session)
            Path(result["path"]).write_bytes(b"corrupted mandatory safety backup")
            return result

        monkeypatch.setattr(backup_restore, "create_system_backup", failed_safety)
        result = backup_restore.prepare_system_restore(db, source["path"])
    assert result["prepared"] is False
    assert not (backup_dir / "restore-pending.zip").exists()
    assert not list(backup_dir.glob(".restore-*"))


def test_backup_sync_failure_is_not_reported_successful(backup_dir, monkeypatch):
    def failed_sync(_fd):
        raise OSError("synthetic storage sync failure")

    monkeypatch.setattr(backup_restore.os, "fsync", failed_sync)
    with app_db.SessionLocal() as db, pytest.raises(OSError, match="sync failure"):
        backup_restore.create_system_backup(db)
    assert not list(backup_dir.glob(".backup-*"))


def test_backup_syncs_bytes_before_publishing_directory(backup_dir, monkeypatch):
    original = backup_restore.os.fsync
    synced = []

    def observed_sync(fd):
        synced.append("file" if stat.S_ISREG(os.fstat(fd).st_mode) else "directory")
        if synced[-1] == "file":
            assert not list(backup_dir.glob("*.zip"))
        else:
            assert len(list(backup_dir.glob("*.zip"))) == 1
        original(fd)

    monkeypatch.setattr(backup_restore.os, "fsync", observed_sync)
    with app_db.SessionLocal() as db:
        assert backup_restore.create_system_backup(db)["ok"]
    assert synced == ["file", "directory"]


def test_nonregular_backup_is_rejected_before_open(backup_dir):
    path = backup_dir / "not-a-file.zip"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular"):
        backup_restore._validate_backup_path(str(path))


@pytest.mark.parametrize("failure", ["directory-sync", "readback", "disappeared"])
def test_uncertain_publication_preserves_evidence_not_success(backup_dir, monkeypatch, failure):
    with app_db.SessionLocal() as db:
        source = backup_restore.create_system_backup(db)
        original_sync = backup_restore._sync_directory

        def publication_fault(path):
            pending = backup_dir / "restore-pending.zip"
            if pending.exists():
                if failure == "directory-sync":
                    raise OSError("synthetic private storage detail")
                if failure == "readback":
                    pending.write_bytes(b"corrupted after publication")
                else:
                    pending.unlink()
            original_sync(path)

        monkeypatch.setattr(backup_restore, "_sync_directory", publication_fault)
        result = backup_restore.prepare_system_restore(db, source["path"])
    assert not result["ok"] and not result["prepared"]
    assert result["state"] == "DIVERGED"
    assert result["code"] == "RESTORE_PUBLICATION_UNCONFIRMED"
    assert backup_restore.preview_system_backup(result["safety_backup"]["path"])["ok"]
    assert "private storage detail" not in str(result)
    assert (backup_dir / "restore-pending.zip").exists() == (failure != "disappeared")
    assert not list(backup_dir.glob(".restore-*"))


def test_verified_preparation_reports_actual_published_hash(backup_dir):
    with app_db.SessionLocal() as db:
        source = backup_restore.create_system_backup(db)
        result = backup_restore.prepare_system_restore(db, source["path"])
    assert result["prepared"]
    assert result["pending_sha256"] == hashlib.sha256(Path(result["pending_archive"]).read_bytes()).hexdigest()
    assert Path(result["pending_archive"]).stat().st_mode & 0o777 == 0o600
