"""Private, durable pre-migration snapshots for the file-backed SQLite startup.

The existing additive migrator remains authoritative. This boundary does not
promise transactional DDL or restore a database underneath live connections.
An interrupted attempt remains explicit and its original snapshot is retained.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
from typing import TypeVar
from uuid import uuid4

from sqlalchemy import Engine

T = TypeVar("T")


def migration_fingerprint() -> str:
    app = Path(__file__).resolve().parents[1]
    sources = [Path(__file__), app / "db" / "migrations.py", app / "db" / "models.py",
               *sorted((app / "models").glob("*.py"))]
    digest = hashlib.sha256()
    for source in sources:
        digest.update(str(source.relative_to(app)).encode() + b"\0" + source.read_bytes() + b"\0")
    return digest.hexdigest()


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _schema(path: Path) -> str:
    if not path.exists():
        return "EMPTY"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def _ownership(root: Path) -> Iterator[None]:
    created = False
    try:
        root.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    if created:
        # A setgid parent can add 02000 despite mkdir(0700). Remove inheritance
        # only from our new directory; never silently repair an existing one.
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("Migration state directory must be private and owned by this user")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock: int | None = None
    try:
        lock = os.open("startup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=directory)
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError("Unsafe migration lock")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another process owns database startup; retry startup after it finishes") from None
        yield
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def _write_record(path: Path, record: dict[str, object]) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".journal-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write((json.dumps(record, sort_keys=True, indent=2) + "\n").encode())
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


def _read_record(path: Path) -> dict[str, object]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384):
            raise RuntimeError("Unsafe migration journal")
        record = json.loads(handle.read())
    if not isinstance(record, dict) or record.get("format") != "basswiesn-migration-v1":
        raise RuntimeError("Invalid migration journal")
    return record


def _snapshot(source: Path, destination: Path) -> str:
    # SQLite's online backup includes committed WAL pages; copying the main
    # database file alone can silently miss those pages.
    # A retrying service must not fill its live database filesystem with an
    # incomplete copy. Count committed WAL pages through SQLite, not just the
    # potentially smaller main file. This is a preflight, not a disk reservation.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        pages = original.execute("PRAGMA page_count").fetchone()[0]
        page_size = original.execute("PRAGMA page_size").fetchone()[0]
    estimated = max(source.stat().st_size, pages * page_size)
    reserve = max(64 * 1024 * 1024, estimated // 10)
    if shutil.disk_usage(destination.parent).free < estimated + reserve:
        raise RuntimeError("Insufficient free space for verified migration snapshot and runtime reserve")
    with destination.open("xb"):
        os.chmod(destination, 0o600)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(destination)) as copied:
            original.backup(copied)
            if copied.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise RuntimeError("Pre-migration snapshot failed integrity verification")
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    return _digest(destination)


def guarded_migration(bind: Engine, migrate: Callable[[Engine], T]) -> T:
    """Serialize startup and back up a changed migration/schema before writes.

    In-memory test databases have no durable state to back up. Existing file
    databases get a new snapshot when the migration code or schema changes, or
    an earlier attempt failed. No automatic backup deletion or rollback.
    """
    if bind.dialect.name != "sqlite":
        raise RuntimeError("Guarded migrations currently support SQLite only")
    database = bind.url.database
    if database in (None, "", ":memory:"):
        return migrate(bind)
    source = Path(os.path.abspath(str(database)))
    if source.is_symlink() or source.exists() and not source.is_file():
        raise RuntimeError("Database path must be a regular non-symlink file")
    source.parent.mkdir(parents=True, exist_ok=True)
    root = source.parent / (source.name + ".migration-state")
    with _ownership(root):
        fingerprint = migration_fingerprint()
        before = _schema(source)
        journal = root / "current.json"
        previous = _read_record(journal)
        changed = (previous.get("status") != "COMPLETE" or previous.get("fingerprint") != fingerprint
                   or previous.get("schema_after") != before)
        record: dict[str, object] = {
            "format": "basswiesn-migration-v1", "attempt": uuid4().hex,
            "started_at": datetime.now(UTC).isoformat(), "status": "PREPARED",
            "fingerprint": fingerprint, "schema_before": before,
            "snapshot": None, "snapshot_sha256": None,
        }
        if changed and source.exists():
            snapshot = root / (str(record["attempt"]) + ".sqlite")
            record["snapshot_sha256"] = _snapshot(source, snapshot)
            record["snapshot"] = snapshot.name
        if changed:
            _write_record(root / (str(record["attempt"]) + ".json"), record)
        _write_record(journal, record)
        try:
            result = migrate(bind)
            record.update(status="COMPLETE", schema_after=_schema(source))
        except BaseException as error:
            record.update(status="FAILED", error_type=type(error).__name__)
            _write_record(journal, record)
            if changed:
                _write_record(root / (str(record["attempt"]) + ".json"), record)
            raise
        _write_record(journal, record)
        if changed:
            _write_record(root / (str(record["attempt"]) + ".json"), record)
        return result
