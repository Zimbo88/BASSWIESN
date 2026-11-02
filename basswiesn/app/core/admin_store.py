"""Local administrator credentials and revocable sessions, never radio secrets.

Separate storage intentionally keeps sessions out of application snapshots and
diagnostic exports. Restoring a radio/database backup must not resurrect login
capabilities. Only an explicit local bootstrap creates this database.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type


class AuthUnavailable(Exception):
    """The local authentication store is absent, unsafe or unreadable."""


class BootstrapConflict(Exception):
    """An administrator already exists; bootstrap never overwrites it."""


class LoginRejected(Exception):
    """Do not disclose whether the username or password was wrong."""


class LoginThrottled(Exception):
    """The durable attempt budget was exhausted before password hashing."""


@dataclass(frozen=True)
class AdminSession:
    username: str
    expires_at: int
    csrf_token: str = field(repr=False)


@dataclass(frozen=True)
class LoginResult:
    session: AdminSession
    cookie: str = field(repr=False)


_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, type=Type.ID)
ABSOLUTE_SECONDS = 8 * 60 * 60
IDLE_SECONDS = 30 * 60


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _csrf(cookie: str) -> str:
    # Recoverable from the presented session only, not from the stored digest.
    return _digest("basswiesn-admin-csrf-v1:" + cookie)


def _credentials(username: str, password: str) -> None:
    if not _USERNAME.fullmatch(username):
        raise ValueError("Use a 1–64 character local account name: letters, digits, dot, dash, underscore")
    if len(password) < 15 or len(password.encode("utf-8")) > 1024:
        raise ValueError("Use a password of at least 15 characters and at most 1024 UTF-8 bytes")


class AdminStore:
    """SQLite transactions serialize bootstrap, throttling and session changes.

    File/parent permissions protect against other local users. The application
    UID and a host administrator are trusted, not isolated by SQLite. Requests
    never initialize, repair, replace or reset a missing/corrupt store.
    """

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock

    def _check_private(self) -> None:
        try:
            parent = self.path.parent.lstat()
            file = self.path.lstat()
            if (
                not stat.S_ISDIR(parent.st_mode)
                or stat.S_IMODE(parent.st_mode) != 0o700
                or parent.st_uid != os.getuid()
                or not stat.S_ISREG(file.st_mode)
                or stat.S_IMODE(file.st_mode) != 0o600
                or file.st_uid != os.getuid()
                or file.st_nlink != 1
            ):
                raise AuthUnavailable("Authentication storage permissions are unsafe")
        except OSError as exc:
            raise AuthUnavailable("Authentication storage is unavailable") from exc

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        self._check_private()
        try:
            db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=3)
            try:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise AuthUnavailable("Authentication storage could not be verified") from exc

    @staticmethod
    def _audit(db: sqlite3.Connection, code: str, now: int) -> None:
        # Constant event codes only: never submitted names, cookies or passwords.
        db.execute("INSERT INTO audit (created, code) VALUES (?, ?)", (now, code))
        db.execute("DELETE FROM audit WHERE id <= (SELECT MAX(id) - 1000 FROM audit)")

    def bootstrap(self, username: str, password: str) -> None:
        _credentials(username, password)
        password_hash = _HASHER.hash(password)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS administrator (id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, password_hash TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, created INTEGER NOT NULL, seen INTEGER NOT NULL, expires INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS attempts (bucket TEXT PRIMARY KEY, start INTEGER NOT NULL, count INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, created INTEGER NOT NULL, code TEXT NOT NULL)")
            if db.execute("SELECT 1 FROM administrator").fetchone():
                raise BootstrapConflict("An administrator already exists")
            from basswiesn.app.core.api_tokens import SCHEMA
            db.execute(SCHEMA)
            db.execute("PRAGMA user_version=1")
            db.execute("INSERT INTO administrator VALUES (1, ?, ?)", (username, password_hash))
            self._audit(db, "ADMIN_BOOTSTRAPPED", int(self.clock()))

    def reset_password(self, username: str, password: str) -> None:
        """Local recovery only, not a browser endpoint. Revoke every session."""
        _credentials(username, password)
        password_hash = _HASHER.hash(password)
        with self._db() as db:
            row = db.execute("SELECT username FROM administrator WHERE id=1").fetchone()
            if row is None or row["username"] != username:
                raise LoginRejected("Local account does not match")
            db.execute("UPDATE administrator SET password_hash=? WHERE id=1", (password_hash,))
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM attempts")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_tokens'").fetchone():
                db.execute("UPDATE api_tokens SET revoked=1")
            self._audit(db, "ADMIN_PASSWORD_RESET", int(self.clock()))

    def revoke_sessions_for_startup(self) -> None:
        """Launcher-only, before serving requests, including restored generations.

        Sessions are runtime grants, not restorable configuration. Keep the
        password and rate-limit budget, but never revive a snapshot's cookies.
        """
        with self._db() as db:
            db.execute("DELETE FROM sessions")
            self._audit(db, "ADMIN_RUNTIME_RESTART", int(self.clock()))

    def _reserve_attempt(self, peer: str) -> tuple[str, str]:
        now = int(self.clock())
        with self._db() as db:
            row = db.execute("SELECT username, password_hash FROM administrator WHERE id=1").fetchone()
            if row is None:
                raise AuthUnavailable("Local administrator bootstrap is required")
            db.execute("DELETE FROM attempts WHERE start <= ? OR start > ?", (now - 60, now))
            for bucket, limit in (("global", 20), (_digest("peer:" + peer), 5)):
                attempt = db.execute("SELECT count FROM attempts WHERE bucket=?", (bucket,)).fetchone()
                if attempt is not None and attempt["count"] >= limit:
                    raise LoginThrottled("Too many login attempts; wait at least 60 seconds")
                db.execute("INSERT INTO attempts VALUES (?, ?, 1) ON CONFLICT(bucket) DO UPDATE SET count=count+1", (bucket, now))
            return str(row["username"]), str(row["password_hash"])

    def login(self, username: str, password: str, *, peer: str, previous_cookie: str = "") -> LoginResult:
        stored_user, stored_hash = self._reserve_attempt(peer)
        accepted = False
        if len(password.encode("utf-8")) <= 1024:
            try:
                # Verify even for a wrong username; no cheap user-existence oracle.
                accepted = _HASHER.verify(stored_hash, password) and secrets.compare_digest(
                    stored_user.encode(), username.encode()
                )
            except VerificationError:
                pass
            except InvalidHashError as exc:
                raise AuthUnavailable("Administrator password record is invalid") from exc
        now = int(self.clock())
        cookie = secrets.token_urlsafe(32)
        new_hash = _HASHER.hash(password) if accepted and _HASHER.check_needs_rehash(stored_hash) else stored_hash
        with self._db() as db:
            current = db.execute("SELECT password_hash FROM administrator WHERE id=1").fetchone()
            # A concurrent password reset must win over an in-flight login.
            accepted = bool(accepted and current is not None and current["password_hash"] == stored_hash)
            if accepted:
                db.execute("UPDATE administrator SET password_hash=? WHERE id=1", (new_hash,))
                db.execute("DELETE FROM sessions WHERE digest=? OR expires<=? OR seen<=? OR seen>?", (_digest(previous_cookie), now, now - IDLE_SECONDS, now))
                # Bound storage without invalidating the freshly issued session.
                db.execute("DELETE FROM sessions WHERE digest IN (SELECT digest FROM sessions ORDER BY created DESC LIMIT -1 OFFSET 19)")
                db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (_digest(cookie), now, now, now + ABSOLUTE_SECONDS))
            self._audit(db, "ADMIN_LOGIN_OK" if accepted else "ADMIN_LOGIN_REJECTED", now)
        if not accepted:
            raise LoginRejected("Invalid account or password")
        return LoginResult(AdminSession(stored_user, now + ABSOLUTE_SECONDS, _csrf(cookie)), cookie)

    def session(self, cookie: str, *, touch: bool = True) -> AdminSession | None:
        if not _TOKEN.fullmatch(cookie):
            return None
        with self._db() as db:
            return self.session_in_transaction(db, cookie, touch=touch)

    def session_in_transaction(self, db: sqlite3.Connection, cookie: str, *, touch: bool = False) -> AdminSession | None:
        """Same authority check inside a credential mutation's write transaction.

        A concurrent logout/reset after middleware authorization must win over
        an in-flight token creation or rotation, not create a new durable grant.
        """
        if not _TOKEN.fullmatch(cookie):
            return None
        now = int(self.clock())
        row = db.execute("SELECT s.*, a.username FROM sessions s CROSS JOIN administrator a WHERE a.id=1 AND s.digest=?", (_digest(cookie),)).fetchone()
        if row is None:
            return None
        if row["expires"] <= now or row["seen"] <= now - IDLE_SECONDS or row["seen"] > now:
            db.execute("DELETE FROM sessions WHERE digest=?", (_digest(cookie),))
            return None
        if touch:
            db.execute("UPDATE sessions SET seen=? WHERE digest=?", (now, _digest(cookie)))
        return AdminSession(str(row["username"]), int(row["expires"]), _csrf(cookie))

    def logout(self, cookie: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE digest=?", (_digest(cookie),))
            self._audit(db, "ADMIN_LOGOUT", int(self.clock()))
