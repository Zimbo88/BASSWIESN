import hashlib
from contextlib import closing
import os
import secrets
import shutil
import sqlite3

import pytest

from basswiesn.app.core.admin_store import AdminStore
from basswiesn.update_helper.auth_state import assert_credential_continuity, AuthenticationChanged

pytestmark = pytest.mark.unit


@pytest.fixture
def generations(tmp_path):
    original = tmp_path / "original"
    current = tmp_path / "current"
    path = original / "data/secrets/admin/auth.sqlite3"
    store = AdminStore(path)
    store.bootstrap("operator", secrets.token_urlsafe(24))
    shutil.copytree(original, current)
    old = os.open(original, os.O_RDONLY | os.O_DIRECTORY)
    live = os.open(current, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield old, live, path, current / "data/secrets/admin/auth.sqlite3"
    finally:
        os.close(old)
        os.close(live)


def check(generations):
    old, live, *_ = generations
    assert_credential_continuity(live, old, owners={os.getuid()})


@pytest.mark.parametrize("tamper", ["file-link", "parent-link", "hardlink", "mode", "parent-mode", "corrupt", "column", "schema-version", "empty-admin", "empty-directory"])
def test_unsafe_credential_generation_is_never_restored(generations, tamper):
    _, _, old, path = generations
    if tamper == "file-link":
        path.unlink()
        path.symlink_to(old)
    elif tamper == "parent-link":
        saved = path.parent.with_name("retained")
        path.parent.rename(saved)
        path.parent.symlink_to(saved, target_is_directory=True)
    elif tamper == "hardlink":
        os.link(path, path.parent.parent / "other")
    elif tamper == "mode":
        path.chmod(0o644)
    elif tamper == "parent-mode":
        path.parent.chmod(0o755)
    elif tamper == "corrupt":
        path.write_bytes(b"invalid sqlite fixture")
    elif tamper == "empty-directory":
        path.unlink()
    else:
        with closing(sqlite3.connect(path)) as db, db:
            if tamper == "column": db.execute("ALTER TABLE administrator ADD COLUMN future TEXT")
            elif tamper == "schema-version": db.execute("PRAGMA user_version=2")
            else: db.execute("DELETE FROM administrator")
    with pytest.raises(AuthenticationChanged):
        check(generations)


def test_identical_stopped_wal_generations_are_verified_without_mutation(generations):
    _, _, old, path = generations
    db = sqlite3.connect(old)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO audit(created,code) VALUES (1,'FIXTURE')")
        db.commit()
        for item in old.parent.iterdir():
            shutil.copy2(item, path.parent / item.name)
        files = list(old.parent.iterdir()) + list(path.parent.iterdir())
        def signature():
            return {p: (p.stat().st_mtime_ns, p.stat().st_mode, hashlib.sha256(p.read_bytes()).hexdigest()) for p in files}
        before = signature()
        check(generations)
        assert signature() == before
        # A credential change existing only in WAL must not be missed.
        with closing(sqlite3.connect(path)) as modified, modified:
            modified.execute("PRAGMA wal_autocheckpoint=0")
            modified.execute("UPDATE administrator SET username='changed'")
            modified.commit()
            with pytest.raises(AuthenticationChanged):
                check(generations)
    finally:
        db.close()


def test_no_auth_on_both_old_generations_remains_compatible(tmp_path):
    with_empty = tmp_path / "empty"
    with_empty.mkdir()
    fd = os.open(with_empty, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert_credential_continuity(fd, fd, owners={os.getuid()})
    finally:
        os.close(fd)


@pytest.mark.parametrize("change", ["issue", "revoke", "rotate", "scope", "expiry", "usage"])
def test_tokens_are_part_of_rollback_authority_except_usage(generations, change):
    from basswiesn.app.core.api_tokens import ApiTokenStore
    _, _, original, current = generations
    store = ApiTokenStore(AdminStore(original))
    issued = store.issue("automation", ["read"], 3600)
    shutil.copy2(original, current)
    active = ApiTokenStore(AdminStore(current))
    if change == "issue": active.issue("second", ["read"], 3600)
    elif change == "revoke": active.revoke(issued.record.id)
    elif change == "rotate": active.rotate(issued.record.id)
    elif change in {"scope", "expiry"}:
        with closing(sqlite3.connect(current)) as db, db:
            db.execute("UPDATE api_tokens SET scopes='[\"admin\"]'" if change == "scope" else "UPDATE api_tokens SET expires=expires+1")
    else:
        assert active.verify(issued.token)
        check(generations)
        return
    with pytest.raises(AuthenticationChanged):
        check(generations)


def test_original_schema_remains_supported_but_cross_schema_restore_stops(generations):
    _, _, original, current = generations
    with closing(sqlite3.connect(original)) as db, db:
        db.execute("DROP TABLE api_tokens")
        db.execute("PRAGMA user_version=0")
    with pytest.raises(AuthenticationChanged):
        check(generations)
    shutil.copy2(original, current)
    check(generations)
