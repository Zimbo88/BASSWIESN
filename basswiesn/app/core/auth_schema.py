"""Explicit local, additive upgrade; network requests never migrate credentials."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from basswiesn.app.core.admin_store import AdminStore, AuthUnavailable
from basswiesn.app.core.api_tokens import SCHEMA
# Share the stopped-generation reader's pure data contract: an upgrade must
# not accept a credential schema that rollback would silently interpret anew.
from basswiesn.update_helper.auth_state import AUTH_COLUMNS, TOKEN_COLUMNS


def enable_api_tokens(store: AdminStore) -> Path | None:
    """Run with the service stopped. Retain a private verified recovery snapshot.

    BEGIN IMMEDIATE prevents another writer changing the source during backup
    and DDL. A separate read connection allows SQLite's backup API to include
    committed WAL pages without deadlocking on the writer connection.
    """
    with store._db() as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = dict(AUTH_COLUMNS)
        if version == 1:
            expected["api_tokens"] = TOKEN_COLUMNS
        if version not in (0, 1) or tables != expected.keys():
            raise AuthUnavailable("Unknown authentication schema; no upgrade performed")
        for table, columns in expected.items():
            if [row[1] for row in db.execute(f"PRAGMA table_info({table})")] != columns:
                raise AuthUnavailable("Unknown authentication columns; no upgrade performed")
        if version == 1:
            return None
        if tuple(db.execute("PRAGMA quick_check").fetchone()) != ("ok",):
            raise AuthUnavailable("Authentication snapshot could not be verified")
        directory = Path(tempfile.mkdtemp(prefix="auth-schema-backup-", dir=store.path.parent.parent))
        backup = directory / "auth.sqlite3"
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(store.path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise AuthUnavailable("Authentication backup integrity failure")
        digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        manifest = directory / "manifest.json"
        with os.fdopen(os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            json.dump({"schema_from": 0, "schema_to": 1, "file": "auth.sqlite3", "sha256": digest}, output)
            output.flush()
            os.fsync(output.fileno())
        if hashlib.sha256(backup.read_bytes()).hexdigest() != json.loads(manifest.read_text())["sha256"]:
            raise AuthUnavailable("Authentication backup readback failure")
        # Persist both the snapshot and its directory entry before committing
        # the new schema. A failed sync is not a usable mandatory backup.
        for path in (backup, directory, directory.parent):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        db.execute(SCHEMA)
        db.execute("PRAGMA user_version=1")
        store._audit(db, "AUTH_TOKEN_SCHEMA_ENABLED", int(store.clock()))
        return directory
