"""No network, no real accounts: credentials live only in pytest temp storage."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from basswiesn.app.core.admin_store import (
    ABSOLUTE_SECONDS, IDLE_SECONDS, AdminStore, AuthUnavailable,
    BootstrapConflict, LoginRejected, LoginThrottled,
)

PASSWORD = "offline example passphrase only"
pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    now = [2000000000]
    result = AdminStore(tmp_path / "admin" / "auth.sqlite3", clock=lambda: now[0])
    result.bootstrap("operator", PASSWORD)
    return result, now


def login(store, previous="", peer="test-peer"):
    return store.login("operator", PASSWORD, peer=peer, previous_cookie=previous)


def test_bootstrap_permissions_hashing_and_conflict(store):
    auth, _ = store
    assert auth.path.stat().st_mode & 0o777 == 0o600
    assert auth.path.parent.stat().st_mode & 0o777 == 0o700
    with sqlite3.connect(auth.path) as db:
        value = db.execute("SELECT password_hash FROM administrator").fetchone()[0]
    assert value.startswith("$argon2id$")
    assert PASSWORD not in value
    with pytest.raises(BootstrapConflict):
        auth.bootstrap("intruder", PASSWORD)
    assert login(auth).session.username == "operator"


def test_session_is_hashed_rotated_revoked_and_not_printed(store):
    auth, _ = store
    first = login(auth)
    second = login(auth, first.cookie)
    assert first.cookie != second.cookie
    assert auth.session(first.cookie) is None
    assert auth.session(second.cookie) == second.session
    assert second.cookie not in repr(second)
    assert second.session.csrf_token not in repr(second)
    contents = auth.path.read_bytes()
    assert second.cookie.encode() not in contents
    assert second.session.csrf_token.encode() not in contents
    assert PASSWORD.encode() not in contents
    auth.logout(second.cookie)
    assert auth.session(second.cookie) is None


def test_no_store_created_by_read_or_login(tmp_path):
    auth = AdminStore(tmp_path / "missing" / "auth.sqlite3")
    assert auth.session("malformed") is None
    with pytest.raises(AuthUnavailable):
        login(auth)
    assert not auth.path.exists()


@pytest.mark.parametrize("kind", ["file-mode", "parent-mode", "symlink", "hardlink", "corrupt"])
def test_unsafe_or_corrupt_storage_fails_closed(store, kind, tmp_path):
    auth, _ = store
    good = login(auth)
    if kind == "file-mode":
        auth.path.chmod(0o644)
    elif kind == "parent-mode":
        auth.path.parent.chmod(0o755)
    elif kind == "symlink":
        original = auth.path.with_suffix(".original")
        auth.path.rename(original)
        auth.path.symlink_to(original)
    elif kind == "hardlink":
        (tmp_path / "alias").hardlink_to(auth.path)
    else:
        auth.path.write_bytes(b"not a database")
    with pytest.raises(AuthUnavailable):
        auth.session(good.cookie)


def test_wrong_username_password_and_rate_limit_persist_across_instances(store):
    auth, now = store
    for username in ["wrong", "operator", "operator", "operator", "operator"]:
        with pytest.raises(LoginRejected):
            auth.login(username, "invalid", peer="test-peer")
    another = AdminStore(auth.path, clock=lambda: now[0])
    with pytest.raises(LoginThrottled):
        login(another)
    now[0] += 60
    assert login(another).session.username == "operator"


def test_global_attempt_budget_cannot_be_bypassed_by_peers(store):
    auth, _ = store
    for index in range(20):
        with pytest.raises(LoginRejected):
            auth.login("operator", "wrong", peer=str(index))
    with pytest.raises(LoginThrottled):
        login(auth, peer="new-peer")


@pytest.mark.parametrize("elapsed", [IDLE_SECONDS, ABSOLUTE_SECONDS, -1])
def test_idle_absolute_expiry_and_backwards_clock_fail_closed(store, elapsed):
    auth, now = store
    session = login(auth)
    now[0] += elapsed
    assert auth.session(session.cookie) is None


def test_active_session_still_has_absolute_expiry(store):
    auth, now = store
    session = login(auth)
    for _ in range(ABSOLUTE_SECONDS // 60 - 1):
        now[0] += 60
        assert auth.session(session.cookie) is not None
    now[0] += 60
    assert auth.session(session.cookie) is None


def test_password_reset_revokes_sessions(store):
    auth, _ = store
    old = login(auth)
    auth.reset_password("operator", "another offline test passphrase")
    assert auth.session(old.cookie) is None
    with pytest.raises(LoginRejected):
        login(auth)
    assert auth.login("operator", "another offline test passphrase", peer="test").session.username == "operator"


def test_bootstrap_race_only_one_winner(tmp_path):
    path = tmp_path / "admin" / "auth.sqlite3"
    def create(name):
        try:
            AdminStore(path).bootstrap(name, PASSWORD)
            return name
        except BootstrapConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ("alice", "bob")))
    assert sum(item is not None for item in results) == 1


@pytest.mark.parametrize("username,password", [("", PASSWORD), ("<bad>", PASSWORD), ("operator", "short"), ("operator", "ü" * 513)])
def test_invalid_bootstrap_does_not_create_file(tmp_path, username, password):
    auth = AdminStore(tmp_path / "admin" / "auth.sqlite3")
    with pytest.raises(ValueError):
        auth.bootstrap(username, password)
    assert not auth.path.exists()


def test_failed_attempt_does_not_revoke_existing_session(store):
    auth, _ = store
    session = login(auth)
    with pytest.raises(LoginRejected):
        auth.login("operator", "wrong", peer="test", previous_cookie=session.cookie)
    assert auth.session(session.cookie) is not None


def test_missing_account_and_corrupt_hash_do_not_create_replacement(store):
    auth, _ = store
    with sqlite3.connect(auth.path) as db:
        db.execute("UPDATE administrator SET password_hash='broken'")
    with pytest.raises(AuthUnavailable):
        login(auth)
    with sqlite3.connect(auth.path) as db:
        db.execute("DELETE FROM administrator")
    with pytest.raises(AuthUnavailable):
        login(auth)
    with pytest.raises(LoginRejected):
        auth.reset_password("operator", PASSWORD)


def test_concurrent_password_recovery_wins_over_inflight_login(store, monkeypatch):
    from basswiesn.app.core import admin_store
    auth, _ = store
    original_verify = admin_store._HASHER.verify
    def racing_verify(self, hash_value, password):
        result = original_verify(hash_value, password)
        auth.reset_password("operator", "changed while verifying password")
        return result
    monkeypatch.setattr(type(admin_store._HASHER), "verify", racing_verify)
    with pytest.raises(LoginRejected):
        login(auth)
    with sqlite3.connect(auth.path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_successful_hash_upgrade_and_touch_false(store, monkeypatch):
    from basswiesn.app.core import admin_store
    auth, now = store
    monkeypatch.setattr(type(admin_store._HASHER), "check_needs_rehash", lambda self, value: True)
    result = login(auth)
    now[0] += 300
    assert auth.session(result.cookie, touch=False) == result.session
    with sqlite3.connect(auth.path) as db:
        assert db.execute("SELECT seen FROM sessions").fetchone()[0] == now[0] - 300


def test_failed_attempts_do_not_log_submitted_secrets(store):
    auth, _ = store
    with pytest.raises(LoginRejected):
        auth.login("untrusted-user-input", "untrusted-password-input", peer="test-peer")
    with sqlite3.connect(auth.path) as db:
        audit = str(db.execute("SELECT * FROM audit").fetchall())
    assert "ADMIN_LOGIN_REJECTED" in audit
    assert "untrusted-user-input" not in audit
    assert "untrusted-password-input" not in audit


def test_local_cli_requires_terminal_and_never_accepts_password_argument(monkeypatch):
    from basswiesn import admin
    monkeypatch.setattr("sys.argv", ["admin", "bootstrap", "--username", "operator"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        admin.main()
    assert exc.value.code == 2


def test_local_cli_bootstrap_reset_conflict_and_mismatch(tmp_path, monkeypatch, capsys):
    from basswiesn import admin
    from basswiesn.app.config import get_settings
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.argv", ["admin", "bootstrap", "--username", "operator"])
    monkeypatch.setattr(admin.getpass, "getpass", lambda prompt: PASSWORD)
    assert admin.main() == 0
    assert admin.main() == 1
    monkeypatch.setattr("sys.argv", ["admin", "reset-password", "--username", "operator"])
    assert admin.main() == 0
    values = iter([PASSWORD, "does not match"])
    monkeypatch.setattr(admin.getpass, "getpass", lambda prompt: next(values))
    assert admin.main() == 1
    result = capsys.readouterr()
    assert PASSWORD not in result.out + result.err
