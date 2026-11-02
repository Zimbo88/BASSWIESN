"""Offline startup fault injection, including committed WAL contents."""

from contextlib import closing
import hashlib
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from basswiesn.app.db import migration_safety as safety

pytestmark = pytest.mark.unit


@pytest.fixture
def local_database(tmp_path):
    path = tmp_path / "local.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE preserve(value TEXT)")
        db.execute("INSERT INTO preserve VALUES ('original')")
        db.commit()
    engine = create_engine("sqlite:///" + str(path))
    yield engine, path, tmp_path / "local.sqlite.migration-state"
    engine.dispose()


def test_snapshot_and_receipt_precede_first_write(local_database):
    engine, path, root = local_database

    def migrate(_):
        record = json.loads((root / "current.json").read_text())
        assert record["status"] == "PREPARED"
        snapshot = root / record["snapshot"]
        assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == record["snapshot_sha256"]
        assert snapshot.stat().st_mode & 0o777 == 0o600
        with closing(sqlite3.connect(snapshot)) as db:
            assert db.execute("SELECT value FROM preserve").fetchone() == ("original",)
        return "ready"

    assert safety.guarded_migration(engine, migrate) == "ready"
    assert json.loads((root / "current.json").read_text())["status"] == "COMPLETE"
    assert root.stat().st_mode & 0o777 == 0o700


def test_failed_attempt_keeps_original_and_next_start_does_not_mark_it_complete(local_database):
    engine, path, root = local_database

    def partial(_):
        with closing(sqlite3.connect(path)) as db:
            db.execute("ALTER TABLE preserve ADD COLUMN added TEXT")
            db.commit()
        raise RuntimeError("secret-bearing SQL must never enter journal")

    with pytest.raises(RuntimeError):
        safety.guarded_migration(engine, partial)
    failed = json.loads((root / "current.json").read_text())
    original = (root / failed["snapshot"]).read_bytes()
    assert failed["status"] == "FAILED"
    assert "secret-bearing" not in (root / "current.json").read_text()
    safety.guarded_migration(engine, lambda _: None)
    assert (root / failed["snapshot"]).read_bytes() == original
    assert json.loads((root / (failed["attempt"] + ".json")).read_text())["status"] == "FAILED"
    assert len(list(root.glob("*.sqlite"))) == 2


def test_unchanged_restart_does_not_accumulate_snapshots(local_database, monkeypatch):
    engine, _, root = local_database
    calls = []
    safety.guarded_migration(engine, lambda _: calls.append(1))
    safety.guarded_migration(engine, lambda _: calls.append(2))
    assert calls == [1, 2]  # Idempotent checks still run; no blind schema skip.
    assert len(list(root.glob("*.sqlite"))) == 1
    monkeypatch.setattr(safety, "migration_fingerprint", lambda: "new-code")
    safety.guarded_migration(engine, lambda _: calls.append(3))
    assert len(list(root.glob("*.sqlite"))) == 2


def test_missing_backup_or_failed_journal_prevents_migration(local_database, monkeypatch):
    engine, _, _ = local_database
    calls = []
    def full(*_):
        raise OSError("disk full")
    monkeypatch.setattr(safety, "_snapshot", full)
    with pytest.raises(OSError):
        safety.guarded_migration(engine, lambda _: calls.append(1))
    assert calls == []


def test_insufficient_space_stops_before_snapshot_or_migration(local_database, monkeypatch):
    engine, path, root = local_database
    original = path.read_bytes()
    monkeypatch.setattr(safety.shutil, "disk_usage", lambda _: SimpleNamespace(free=1024))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="Insufficient free space"):
            safety.guarded_migration(engine, lambda _: pytest.fail("must not migrate"))
    assert path.read_bytes() == original
    assert not list(root.glob("*.sqlite"))
    assert not (root / "current.json").exists()


def test_low_disk_unchanged_restart_does_not_require_another_snapshot(local_database, monkeypatch):
    engine, _, root = local_database
    safety.guarded_migration(engine, lambda _: None)
    snapshots = list(root.glob("*.sqlite"))
    monkeypatch.setattr(safety.shutil, "disk_usage", lambda _: SimpleNamespace(free=1024))
    safety.guarded_migration(engine, lambda _: None)
    assert list(root.glob("*.sqlite")) == snapshots


def test_snapshot_space_estimate_includes_wal_pages(local_database, monkeypatch):
    engine, path, root = local_database
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO preserve VALUES (zeroblob(1048576))")
        writer.commit()
        reserve = 64 * 1024 * 1024
        monkeypatch.setattr(safety.shutil, "disk_usage", lambda _: SimpleNamespace(free=path.stat().st_size + reserve))
        with pytest.raises(RuntimeError, match="Insufficient free space"):
            safety.guarded_migration(engine, lambda _: pytest.fail("must not migrate"))
        assert not list(root.glob("*.sqlite"))


def test_wal_commit_is_in_pre_migration_snapshot(local_database):
    engine, path, root = local_database
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO preserve VALUES ('wal-only')")
        writer.commit()
        safety.guarded_migration(engine, lambda _: None)
        record = json.loads((root / "current.json").read_text())
        with closing(sqlite3.connect(root / record["snapshot"])) as snapshot:
            assert snapshot.execute("SELECT COUNT(*) FROM preserve").fetchone() == (2,)


def test_existing_lock_rejects_another_startup(local_database):
    engine, _, root = local_database
    with safety._ownership(root), pytest.raises(RuntimeError, match="Another process"):
        safety.guarded_migration(engine, lambda _: pytest.fail("must not migrate concurrently"))


@pytest.mark.parametrize("kind", ["directory_permissions", "directory_symlink", "lock_symlink", "journal_symlink"])
def test_unsafe_state_fails_before_migration(local_database, tmp_path, kind):
    engine, _, root = local_database
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    if kind == "directory_symlink":
        root.symlink_to(unrelated, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        if kind == "directory_permissions":
            root.chmod(0o755)
        else:
            (unrelated / "keep").write_text("keep")
            (root / ("startup.lock" if kind == "lock_symlink" else "current.json")).symlink_to(unrelated / "keep")
    with pytest.raises((RuntimeError, OSError)):
        safety.guarded_migration(engine, lambda _: pytest.fail("unsafe startup"))


def test_new_database_and_in_memory_do_not_require_fake_backups(tmp_path):
    from basswiesn.app.db.migrations import ensure_schema_baseline
    for url in ("sqlite:///:memory:", "sqlite:///" + str(tmp_path / "new.sqlite")):
        engine = create_engine(url)
        try:
            assert safety.guarded_migration(engine, ensure_schema_baseline).ready
        finally:
            engine.dispose()
    assert not list((tmp_path / "new.sqlite.migration-state").glob("*.sqlite"))


def test_tampered_journal_is_not_a_reason_to_skip_backup(local_database):
    engine, _, root = local_database
    safety.guarded_migration(engine, lambda _: None)
    (root / "current.json").write_text('{"format":"other-format"}')
    with pytest.raises(RuntimeError, match="Invalid migration journal"):
        safety.guarded_migration(engine, lambda _: pytest.fail("tampered journal"))


def test_database_symlink_is_not_followed(local_database, tmp_path):
    _, path, _ = local_database
    link = tmp_path / "alias.sqlite"
    link.symlink_to(path)
    engine = create_engine("sqlite:///" + str(link))
    try:
        with pytest.raises(RuntimeError, match="non-symlink"):
            safety.guarded_migration(engine, lambda _: pytest.fail("symlink database"))
    finally:
        engine.dispose()


def test_new_private_directory_does_not_inherit_setgid(local_database):
    engine, path, root = local_database
    path.parent.chmod(0o2700)
    safety.guarded_migration(engine, lambda _: None)
    assert root.stat().st_mode & 0o7777 == 0o700


def test_journal_failure_prevents_migration(local_database, monkeypatch):
    engine, _, _ = local_database
    def fail(*_):
        raise OSError("synthetic journal storage failure")
    monkeypatch.setattr(safety, "_write_record", fail)
    with pytest.raises(OSError):
        safety.guarded_migration(engine, lambda _: pytest.fail("must not migrate without durable journal"))
