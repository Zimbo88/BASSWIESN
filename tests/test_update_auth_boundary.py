"""Synthetic local credentials and snapshots only; never an installed service."""
from contextlib import closing
from pathlib import Path
import secrets
import sqlite3

import pytest

from basswiesn.app.core.admin_store import AdminStore
from basswiesn.update_helper import host_executor as executor
from basswiesn.update_helper.protocol import Request
from basswiesn.update_helper.transaction import TransactionRunner
from test_update_host_executor_300 import DockerFixture, JOB, NEW_IMAGE, VERSION, setup  # noqa: F401
from test_update_installation_snapshot_300 import context  # noqa: F401
from test_update_host_preflight_300 import enrolled  # noqa: F401

pytestmark = pytest.mark.unit


def test_runtime_restart_revokes_snapshot_sessions_without_resetting_password(tmp_path):
    store = AdminStore(tmp_path / "admin" / "auth.sqlite3")
    password = secrets.token_urlsafe(24)
    store.bootstrap("operator", password)
    login = store.login("operator", password, peer="fixture")
    # This file models a retained old generation, not the current credential DB.
    restored = tmp_path / "restored" / "auth.sqlite3"
    restored.parent.mkdir(mode=0o700)
    restored.write_bytes(store.path.read_bytes())
    restored.chmod(0o600)
    store.logout(login.cookie)
    old = AdminStore(restored)
    old.revoke_sessions_for_startup()
    assert old.session(login.cookie) is None
    assert old.login("operator", password, peer="fixture").cookie != login.cookie
    with closing(sqlite3.connect(restored)) as db, db:
        assert db.execute("SELECT code FROM audit ORDER BY id DESC LIMIT 2").fetchall() == [
            ("ADMIN_LOGIN_OK",), ("ADMIN_RUNTIME_RESTART",)]


@pytest.mark.parametrize("change", ["password", "schema", "missing", "token-revoke", "token-rotate"])
def test_failed_update_does_not_restore_stale_or_unknown_credentials(setup, change):
    policy, root, fetch, journal = setup
    path = Path(policy.root) / "data/secrets/admin/auth.sqlite3"
    store = AdminStore(path)
    store.bootstrap("operator", secrets.token_urlsafe(24))
    from basswiesn.app.core.api_tokens import ApiTokenStore
    token = ApiTokenStore(store).issue("fixture", ["read"], 3600)
    docker = DockerFixture(policy, "health")
    new_password = secrets.token_urlsafe(24)

    def run(args, *, seconds):
        result = docker(args, seconds=seconds)
        if args[0] == "start" and docker.containers[args[1]]["Image"] == NEW_IMAGE:
            if change == "password":
                AdminStore(path).reset_password("operator", new_password)
            elif change == "schema":
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("CREATE TABLE future_credentials (value TEXT)")
            elif change == "missing":
                path.rename(path.with_suffix(".retained"))
            elif change == "token-revoke": ApiTokenStore(AdminStore(path)).revoke(token.record.id)
            elif change == "token-rotate": ApiTokenStore(AdminStore(path)).rotate(token.record.id)
        return result

    adapter = executor.ProductionExecutor(root=str(root), owner_uid=policy.installation_owner_uid,
        run=run, observe=docker.observe, stopped=docker.stopped, fetch=fetch, health_seconds=1)
    outcome = TransactionRunner(journal, adapter).execute(Request("install", JOB, VERSION, policy.version))
    assert outcome["state"] == "MANUAL_ACTION_REQUIRED"
    assert not any(c["State"]["Running"] for c in docker.containers.values())
    if change == "password":
        assert AdminStore(path).login("operator", new_password, peer="fixture")


def test_session_activity_alone_does_not_prevent_verified_rollback(setup):
    policy, root, fetch, journal = setup
    path = Path(policy.root) / "data/secrets/admin/auth.sqlite3"
    password = secrets.token_urlsafe(24)
    AdminStore(path).bootstrap("operator", password)
    docker = DockerFixture(policy, "health")

    def run(args, *, seconds):
        result = docker(args, seconds=seconds)
        if args[0] == "start" and docker.containers[args[1]]["Image"] == NEW_IMAGE:
            store = AdminStore(path)
            cookie = store.login("operator", password, peer="fixture").cookie
            store.logout(cookie)
        return result

    adapter = executor.ProductionExecutor(root=str(root), owner_uid=policy.installation_owner_uid,
        run=run, observe=docker.observe, stopped=docker.stopped, fetch=fetch, health_seconds=1)
    assert TransactionRunner(journal, adapter).execute(Request("install", JOB, VERSION, policy.version))["state"] == "RESTORED"
