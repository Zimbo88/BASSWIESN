"""Opaque credentials in temporary stores; no network or installed accounts."""
from contextlib import closing
import sqlite3

import pytest

from basswiesn.app.core.admin_store import AdminStore

pytestmark = pytest.mark.unit


@pytest.fixture
def tokens(tmp_path):
    from basswiesn.app.core.api_tokens import ApiTokenStore
    now = [2000000000]
    admin = AdminStore(tmp_path / "admin/auth.sqlite3", clock=lambda: now[0])
    admin.bootstrap("operator", "offline example passphrase only")
    return ApiTokenStore(admin), now


def test_issue_hash_once_rotate_revoke_and_usage(tokens):
    store, now = tokens
    issued = store.issue("automation", ["read"], 3600)
    assert issued.token not in repr(issued)
    assert issued.token.encode() not in store.admin.path.read_bytes()
    assert store.verify(issued.token, touch=False).scopes == frozenset({"read"})
    assert store.list()[0].last_used is None
    now[0] += 10
    assert store.verify(issued.token).last_used == now[0]
    rotated = store.rotate(issued.record.id)
    assert rotated.token != issued.token
    assert rotated.record.scopes == issued.record.scopes
    assert rotated.record.expires_at == issued.record.expires_at
    assert store.verify(issued.token) is None
    assert store.revoke(rotated.record.id)
    assert store.verify(rotated.token) is None
    with closing(sqlite3.connect(store.admin.path)) as db:
        audit = str(db.execute("SELECT code FROM audit").fetchall())
    for code in ("API_TOKEN_ISSUED", "API_TOKEN_ROTATED", "API_TOKEN_REVOKED"):
        assert code in audit
    assert issued.token not in audit and "automation" not in audit


def test_password_recovery_revokes_tokens_but_runtime_restart_does_not(tokens):
    store, _ = tokens
    issued = store.issue("automation", ["read"], 3600)
    store.admin.revoke_sessions_for_startup()
    assert store.verify(issued.token) is not None
    store.admin.reset_password("operator", "different offline test passphrase")
    assert store.verify(issued.token) is None


@pytest.mark.parametrize("scopes", [[], ["root"], ["READ"], ["read", "read"], "read"])
def test_invalid_scopes_fail_without_grant(tokens, scopes):
    store, _ = tokens
    with pytest.raises(ValueError):
        store.issue("automation", scopes, 3600)
    assert store.list() == []


@pytest.mark.parametrize("offset", [-1, 3600])
def test_clock_and_expiry_fail_closed(tokens, offset):
    store, now = tokens
    issued = store.issue("automation", ["read"], 3600)
    now[0] += offset
    assert store.verify(issued.token) is None


@pytest.mark.parametrize("name,lifetime", [("<script>", 3600), (None, 3600), ("ok", True), ("ok", 59), ("ok", 7776001), ("ok", "3600")])
def test_invalid_fields_never_create_grants(tokens, name, lifetime):
    with pytest.raises(ValueError):
        tokens[0].issue(name, ["read"], lifetime)
    assert tokens[0].list() == []


def test_redaction_covers_bare_bearer_and_nested_tokens(tokens):
    from basswiesn.app.core.redaction import redact_payload, redact_text
    value = tokens[0].issue("automation", ["read"], 3600).token
    for text in (value, f"Authorization: Bearer {value}", f"unexpected value={value}"):
        assert value not in redact_text(text)
    assert value not in str(redact_payload({"nested": [{"payload": value}]}))


def test_invalid_revoked_unknown_corrupt_and_bounded_records(tokens, monkeypatch):
    from basswiesn.app.core import api_tokens
    from basswiesn.app.core.admin_store import AuthUnavailable
    store, _ = tokens
    for text in ("", "wrong", "bwapi_" + "x" * 10000):
        assert store.verify(text) is None
    assert store.rotate("bad") is None
    assert store.revoke("bad") is False
    assert store.rotate("a" * 32) is None
    assert store.revoke("a" * 32) is False
    issued = store.issue("first", ["read"], 3600)
    monkeypatch.setattr(api_tokens, "MAX_RECORDS", 1)
    with pytest.raises(ValueError, match="TOKEN_RECORD_LIMIT"):
        store.issue("second", ["read"], 3600)
    with closing(sqlite3.connect(store.admin.path)) as db, db:
        db.execute("UPDATE api_tokens SET scopes=?", ('["unreviewed"]',))
    with pytest.raises(AuthUnavailable):
        store.verify(issued.token)


def test_rotation_and_revocation_race_never_reactivates_revoked_grant(tokens):
    from concurrent.futures import ThreadPoolExecutor
    store, _ = tokens
    issued = store.issue("automation", ["read"], 3600)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rotation = pool.submit(store.rotate, issued.record.id)
        revoke = pool.submit(store.revoke, issued.record.id)
        result = rotation.result()
        assert revoke.result() is True
    assert store.verify(issued.token) is None
    if result:
        assert store.verify(result.token) is None
    assert store.rotate(issued.record.id) is None


def test_empty_session_is_not_local_authority(tokens):
    from basswiesn.app.core.admin_store import LoginRejected
    with pytest.raises(LoginRejected):
        tokens[0].issue("automation", ["admin"], 3600, session_cookie="")
    assert tokens[0].list() == []


def test_explicit_old_schema_upgrade_has_verified_private_backup(tokens):
    import hashlib
    import json
    from basswiesn.app.core.admin_store import AuthUnavailable
    from basswiesn.app.core.auth_schema import enable_api_tokens
    store, _ = tokens
    with closing(sqlite3.connect(store.admin.path)) as db, db:
        db.execute("DROP TABLE api_tokens")
        db.execute("PRAGMA user_version=0")
    with pytest.raises(AuthUnavailable):
        store.issue("automation", ["read"], 3600)
    backup = enable_api_tokens(store.admin)
    assert backup.stat().st_mode & 0o777 == 0o700
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["sha256"] == hashlib.sha256((backup / "auth.sqlite3").read_bytes()).hexdigest()
    assert (backup / "auth.sqlite3").stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(backup / "auth.sqlite3")) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='api_tokens'").fetchone() is None
    assert enable_api_tokens(store.admin) is None
    assert store.verify(store.issue("automation", ["read"], 3600).token)


def test_unknown_schema_and_backup_failure_prevent_ddl(tokens, monkeypatch):
    from basswiesn.app.core import auth_schema
    from basswiesn.app.core.admin_store import AuthUnavailable
    store, _ = tokens
    with closing(sqlite3.connect(store.admin.path)) as db, db:
        db.execute("DROP TABLE api_tokens")
        db.execute("PRAGMA user_version=99")
    with pytest.raises(AuthUnavailable):
        auth_schema.enable_api_tokens(store.admin)
    with closing(sqlite3.connect(store.admin.path)) as db, db:
        db.execute("PRAGMA user_version=0")
    def no_disk(*args, **kwargs):
        raise OSError("offline injected disk failure")
    monkeypatch.setattr(auth_schema.tempfile, "mkdtemp", no_disk)
    with pytest.raises(OSError):
        auth_schema.enable_api_tokens(store.admin)
    with closing(sqlite3.connect(store.admin.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='api_tokens'").fetchone() is None


def test_corrupt_grant_timestamps_and_missing_admin_fail_closed(tokens):
    from basswiesn.app.core.admin_store import AuthUnavailable
    store, _ = tokens
    issued = store.issue("automation", ["read"], 3600)
    with closing(sqlite3.connect(store.admin.path)) as db, db:
        db.execute("UPDATE api_tokens SET expires=created-1")
    with pytest.raises(AuthUnavailable):
        store.verify(issued.token)
    with closing(sqlite3.connect(store.admin.path)) as db, db:
        db.execute("DELETE FROM administrator")
    with pytest.raises(AuthUnavailable):
        store.issue("another", ["read"], 3600)


def test_migration_refuses_tampered_backup_manifest(tokens, monkeypatch):
    from basswiesn.app.core import auth_schema
    from basswiesn.app.core.admin_store import AuthUnavailable
    store, _ = tokens
    with closing(sqlite3.connect(store.admin.path)) as db, db:
        db.execute("DROP TABLE api_tokens")
        db.execute("PRAGMA user_version=0")
    original = auth_schema.json.dump
    def tamper(value, output):
        original({**value, "sha256": "0" * 64}, output)
    monkeypatch.setattr(auth_schema.json, "dump", tamper)
    with pytest.raises(AuthUnavailable):
        auth_schema.enable_api_tokens(store.admin)
    with closing(sqlite3.connect(store.admin.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='api_tokens'").fetchone() is None


def test_local_upgrade_cli_needs_no_password_and_is_idempotent(tokens, monkeypatch, capsys):
    from basswiesn import admin
    from basswiesn.app.config import get_settings
    store, _ = tokens
    monkeypatch.setattr("sys.argv", ["admin", "enable-api-tokens"])
    monkeypatch.setattr(admin, "AdminStore", lambda path: store.admin)
    assert admin.main() == 0
    assert "already current" in capsys.readouterr().out
    store.admin.path.chmod(0o644)
    assert admin.main() == 1
    assert "refused" in capsys.readouterr().err


def test_local_cli_upgrade_retains_backup_and_old_schema_password_recovery(tokens, monkeypatch, capsys):
    from basswiesn import admin
    store, _ = tokens
    with closing(sqlite3.connect(store.admin.path)) as db, db:
        db.execute("DROP TABLE api_tokens")
        db.execute("PRAGMA user_version=0")
    store.admin.reset_password("operator", "new offline example passphrase")
    monkeypatch.setattr("sys.argv", ["admin", "enable-api-tokens"])
    monkeypatch.setattr(admin, "AdminStore", lambda path: store.admin)
    assert admin.main() == 0
    output = capsys.readouterr().out
    assert "Private backup retained in the administrator store." in output
    assert str(store.admin.path.parent) not in output
    backups = list(store.admin.path.parent.parent.glob("auth-schema-backup-*/auth.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as backup:
        assert backup.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert backup.execute("PRAGMA user_version").fetchone() == (0,)
        assert backup.execute("SELECT name FROM sqlite_master WHERE name='api_tokens'").fetchone() is None
    monkeypatch.setattr("sys.argv", ["admin", "bootstrap"])
    with pytest.raises(SystemExit) as error:
        admin.main()
    assert error.value.code == 2


@pytest.mark.parametrize("version", [0, 1])
def test_upgrade_rejects_unrecognized_columns_even_with_known_version(tokens, version):
    from basswiesn.app.core.auth_schema import enable_api_tokens
    from basswiesn.app.core.admin_store import AuthUnavailable
    store, _ = tokens
    with closing(sqlite3.connect(store.admin.path)) as db, db:
        if version == 0:
            db.execute("DROP TABLE api_tokens")
            db.execute("PRAGMA user_version=0")
        db.execute("ALTER TABLE administrator ADD COLUMN future_credential TEXT")
    with pytest.raises(AuthUnavailable):
        enable_api_tokens(store.admin)
    assert not list(store.admin.path.parent.parent.glob("auth-schema-backup-*"))


def test_backup_sync_failure_prevents_schema_change(tokens, monkeypatch):
    from basswiesn.app.core import auth_schema
    store, _ = tokens
    with closing(sqlite3.connect(store.admin.path)) as db, db:
        db.execute("DROP TABLE api_tokens")
        db.execute("PRAGMA user_version=0")
    original = auth_schema.os.fsync
    calls = []
    def failure(fd):
        calls.append(fd)
        if len(calls) == 3:  # manifest, database, then directory durability
            raise OSError("offline directory sync failure")
        original(fd)
    monkeypatch.setattr(auth_schema.os, "fsync", failure)
    with pytest.raises(OSError):
        auth_schema.enable_api_tokens(store.admin)
    with closing(sqlite3.connect(store.admin.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='api_tokens'").fetchone() is None
