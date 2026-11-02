"""Read-only credential continuity check before reversing a stopped generation.

No application imports, Argon2 dependency, password output or state migration.
The host has already proved that all application writers are stopped. Unknown
credential schemas require operator recovery instead of an implicit downgrade.
"""
from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time


class AuthenticationChanged(ValueError):
    def __init__(self) -> None:
        super().__init__("AUTHENTICATION_CONTINUITY_UNVERIFIED")


AUTH_COLUMNS = {
    "administrator": ["id", "username", "password_hash"],
    "sessions": ["digest", "created", "seen", "expires"],
    "attempts": ["bucket", "start", "count"],
    "audit": ["id", "created", "code"],
}
TOKEN_COLUMNS = ["id", "digest", "name", "scopes", "created", "expires", "last_used", "revoked"]


@contextmanager
def _admin_directory(root: int, owners: set[int]) -> Iterator[int | None]:
    current = os.dup(root)
    try:
        for leaf in ("data", "secrets", "admin"):
            try:
                child = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            except FileNotFoundError:
                yield None
                return
            os.close(current)
            current = child
            info = os.fstat(current)
            if info.st_uid not in owners or info.st_mode & 0o002:
                raise AuthenticationChanged()
        if stat.S_IMODE(os.fstat(current).st_mode) != 0o700:
            raise AuthenticationChanged()
        yield current
    finally:
        os.close(current)


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns,
            info.st_size, info.st_mode, info.st_uid, info.st_nlink)


def _query(path: Path) -> str:
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        deadline = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise AuthenticationChanged()
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = db.execute("PRAGMA user_version").fetchone()[0]
        columns_by_table = dict(AUTH_COLUMNS)
        if version == 1:
            columns_by_table["api_tokens"] = TOKEN_COLUMNS
        if version not in (0, 1) or tables != columns_by_table.keys():
            raise AuthenticationChanged()
        for table, columns in columns_by_table.items():
            if [r[1] for r in db.execute(f"PRAGMA table_info({table})")] != columns:
                raise AuthenticationChanged()
        rows = db.execute("SELECT id,username,password_hash FROM administrator").fetchmany(2)
        if len(rows) != 1 or rows[0][0] != 1 or not all(isinstance(v, str) and v for v in rows[0][1:]):
            raise AuthenticationChanged()
        # Token usage timestamps are volatile. Credentials, scopes, expiration
        # and revocation are not: rollback must never resurrect an old grant.
        grants = db.execute("SELECT id,digest,name,scopes,created,expires,revoked FROM api_tokens ORDER BY id").fetchall() if version == 1 else []
        return hashlib.sha256(json.dumps([version, rows, grants], separators=(",", ":")).encode()).hexdigest()
    finally:
        db.close()


def _fingerprint(root: int, owners: set[int]) -> str | None:
    with _admin_directory(root, owners) as directory:
        if directory is None:
            return None
        # A sidecar can contain newer credentials than the main database.
        # Reject links/types/unknown files; use SQLite's normal read-only WAL
        # processing, never immutable=1 (which could hide a password reset).
        names = set(os.listdir(directory))
        if "auth.sqlite3" not in names or names - {"auth.sqlite3", "auth.sqlite3-wal", "auth.sqlite3-shm", "auth.sqlite3-journal"}:
            raise AuthenticationChanged()
        before = {}
        for name in names:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in owners
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 8 * 1024 * 1024):
                raise AuthenticationChanged()
            before[name] = _signature(info)
        # SQLite may update WAL reader marks even for a read-only connection.
        # Query a bounded private copy so both retained generations remain
        # byte/metadata-identical for the cutover verifier.
        with tempfile.TemporaryDirectory(prefix="basswiesn-auth-check-") as temporary:
            for name in sorted(names):
                source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    if _signature(os.fstat(source)) != before[name]:
                        raise AuthenticationChanged()
                    destination = os.open(Path(temporary) / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(destination, "wb") as output:
                        remaining = before[name][4]
                        while remaining:
                            chunk = os.read(source, min(65536, remaining))
                            if not chunk:
                                raise AuthenticationChanged()
                            output.write(chunk)
                            remaining -= len(chunk)
                finally:
                    os.close(source)
            fingerprint = _query(Path(temporary) / "auth.sqlite3")
        if names != set(os.listdir(directory)) or any(
            _signature(os.stat(name, dir_fd=directory, follow_symlinks=False)) != before[name] for name in names
        ):
            raise AuthenticationChanged()
        return fingerprint


def assert_credential_continuity(live: int, original: int, *, owners: set[int]) -> None:
    """Both arguments are already verified generation-root directory FDs."""
    try:
        if _fingerprint(live, owners) != _fingerprint(original, owners):
            raise AuthenticationChanged()
    except (OSError, sqlite3.Error, TypeError) as exc:
        raise AuthenticationChanged() from exc
