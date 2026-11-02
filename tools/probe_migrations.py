#!/usr/bin/env python3
"""Exercise real runtime SQLite/migrations on private synthetic databases only.

Run explicitly inside a network-none release container. This tests additive
migration, durable safety copies, restart and recovery, not host-helper cutover
or a historical product release. It requires no pytest/dev dependencies.
"""
from __future__ import annotations

from contextlib import closing, ExitStack
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sqlite3
import sys
import tempfile
from typing import Any
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine

PROFILES: dict[str, dict[str, tuple[str, ...]]] = {
    "device-state": {"devices": ("reachable", "last_failed_at", "failure_count", "offline_reason")},
    "history-identity": {"play_history": ("station_display_name", "identity_source", "identity_confidence", "is_confirmed")},
    "device-policy": {"devices": ("device_class_override", "safe_mode", "polling_profile_override")},
}


def denied(*args: object, **kwargs: object) -> None:
    raise RuntimeError("MIGRATION_PROBE_NETWORK_FORBIDDEN")


def dump(path: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        return tuple(db.iterdump())


def stable_state(path: Path) -> dict[str, object]:
    """Ignore only derived refresh timestamps on two curated profile catalogs."""
    result: dict[str, object] = {}
    def quote(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        result["schema"] = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            columns = [row[1] for row in db.execute("PRAGMA table_info(" + quote(table) + ")")]
            if table in {"device_model_definitions", "telnet_device_profiles"}:
                columns = [column for column in columns if column != "updated_at"]
            result[table] = sorted(db.execute("SELECT " + ",".join(map(quote, columns)) + " FROM " + quote(table)).fetchall(), key=repr)
    return result


def fixture(path: Path, profile: str) -> None:
    from basswiesn.app.db.database import Base
    engine = create_engine("sqlite:///" + str(path))
    try:
        # Construct a bounded compatibility shape, not a false historical image.
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    with closing(sqlite3.connect(path)) as db:
        for table, columns in PROFILES[profile].items():
            for index in db.execute(f'PRAGMA index_list("{table}")').fetchall():
                fields = {row[2] for row in db.execute(f'PRAGMA index_info("{index[1]}")')}
                if fields.intersection(columns):
                    db.execute(f'DROP INDEX "{index[1]}"')
            for column in columns:
                db.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        db.execute("CREATE TABLE preserved_extension (id INTEGER PRIMARY KEY, value BLOB)")
        db.execute("INSERT INTO preserved_extension VALUES (1, ?)", (b"synthetic-preserved-data",))
        db.execute("INSERT INTO presets (device_id, button, source, source_account, content_item_xml, location, updated_at) VALUES (?, 1, '10003', '', ?, ?, CURRENT_TIMESTAMP)",
                   ("SYNTHETIC", '<ContentItem source="10003" type="stationurl" location="https://example.invalid/audio.mp3"><itemName>Example station</itemName></ContentItem>',
                    "https://example.invalid/audio.mp3"))
        db.commit()


def probe(root: Path) -> dict[str, Any]:
    from basswiesn.app.db.migration_safety import guarded_migration
    from basswiesn.app.db.migrations import ensure_schema_baseline, inspect_schema
    results = []
    for profile in PROFILES:
        path = root / (profile + ".sqlite")
        fixture(path, profile)
        before = dump(path)
        engine = create_engine("sqlite:///" + str(path))
        try:
            assert guarded_migration(engine, ensure_schema_baseline).ready
            state = path.with_name(path.name + ".migration-state")
            record = json.loads((state / "current.json").read_text())
            assert record["status"] == "COMPLETE"
            snapshot = state / record["snapshot"]
            assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == record["snapshot_sha256"]
            assert dump(snapshot) == before
            with closing(sqlite3.connect(path)) as db:
                assert db.execute("SELECT value FROM preserved_extension").fetchone() == (b"synthetic-preserved-data",)
                source, xml = db.execute("SELECT source,content_item_xml FROM presets").fetchone()
                assert source == "LOCAL_INTERNET_RADIO" and 'source="LOCAL_INTERNET_RADIO"' in xml
                for table, columns in PROFILES[profile].items():
                    actual = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                    assert set(columns) <= actual
            snapshots = sorted(state.glob("*.sqlite"))
            after = stable_state(path)
            assert guarded_migration(engine, ensure_schema_baseline).ready
            assert stable_state(path) == after and sorted(state.glob("*.sqlite")) == snapshots
        finally:
            engine.dispose()
        # Recovery uses a stopped, separate database. Never overwrite a live DB.
        restored = root / (profile + "-restored.sqlite")
        shutil.copyfile(snapshot, restored)
        assert dump(restored) == before
        restored_engine = create_engine("sqlite:///" + str(restored))
        try:
            assert guarded_migration(restored_engine, ensure_schema_baseline).ready
            assert inspect_schema(restored_engine).ready
        finally:
            restored_engine.dispose()
        results.append({"profile": profile, "migration": "PASS", "snapshot": "PASS",
                        "idempotent_restart": "PASS", "restore_and_restart": "PASS"})
    path = root / "interrupted.sqlite"
    fixture(path, "device-state")
    before = dump(path)
    engine = create_engine("sqlite:///" + str(path))
    try:
        def interrupted(_bind: object) -> None:
            with closing(sqlite3.connect(path)) as db:
                db.execute("ALTER TABLE preserved_extension ADD COLUMN attempted TEXT")
                db.commit()
            raise RuntimeError("synthetic interruption")
        try:
            guarded_migration(engine, interrupted)
        except RuntimeError as error:
            assert str(error) == "synthetic interruption"
        else:
            raise AssertionError("Interruption incorrectly accepted")
        state = path.with_name(path.name + ".migration-state")
        failed = json.loads((state / "current.json").read_text())
        assert failed["status"] == "FAILED"
        assert dump(state / failed["snapshot"]) == before
        assert guarded_migration(engine, ensure_schema_baseline).ready
        assert json.loads((state / (failed["attempt"] + ".json")).read_text())["status"] == "FAILED"
    finally:
        engine.dispose()
    return {"status": "PASS", "profiles": results, "interruption_recovery": "PASS",
            "architecture": platform.machine(), "python": platform.python_version(),
            "hardware": "NOT_RUN", "host_helper_cutover": "NOT_RUN",
            "scope": "Synthetic compatibility schemas; actual locked runtime SQLite and migration code"}


def main() -> int:
    original = Path.cwd()
    try:
        with tempfile.TemporaryDirectory(prefix="basswiesn-migration-probe-") as temporary, ExitStack() as stack:
            # Application imports may initialize relative configuration paths.
            # Isolate those too, before importing any application module.
            os.chdir(temporary)
            stack.enter_context(patch.dict(os.environ, {"BASSWIESN_LAN_HOST": "192.0.2.42"}))
            for method in ("connect", "connect_ex", "sendto"):
                stack.enter_context(patch.object(socket.socket, method, denied))
            for method in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
                stack.enter_context(patch.object(socket, method, denied))
            result = probe(Path(temporary))
    finally:
        os.chdir(original)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
