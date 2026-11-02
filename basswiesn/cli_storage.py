"""Offline SQLite and private no-overwrite artifacts; never imports app startup.

The existing backup service imports the ORM/settings lifecycle. This adapter
instead shares its manifest and archive validators, and emits that same v1
format using a stopped database or checkpointed snapshot. It is not a restore
implementation. Authentication databases are never included in an export.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import tempfile
import time
from typing import BinaryIO
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile
import zlib

from basswiesn import __version__
from basswiesn.app.services.archive_security import validate_archive_members
from basswiesn.app.services.backup_manifest import SYSTEM_BACKUP_COMPONENTS, validate_system_manifest
from basswiesn.cli_contract import JsonValue, OfflineError
from basswiesn.cli_schema import declared_baseline, schema_compatible

MAX_DATABASE_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
SQL_SECONDS = 5.0


def checked_path(path: Path, *, directory: bool = False) -> os.stat_result:
    """Refuse symlinks (including ancestors), special files and hardlinked files."""
    absolute = path.absolute()
    try:
        for ancestor in (*reversed(absolute.parents), absolute):
            info = ancestor.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise OfflineError("path.unsafe", "A selected path contains a symlink.", "Select a real local file/directory without symlink components.")
        if directory:
            safe = stat.S_ISDIR(info.st_mode)
        else:
            safe = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        if not safe:
            raise OfflineError("path.unsafe", "A selected path is not a supported regular file/directory.", "Use a regular single-link local file or a real directory.")
        return info
    except OSError:
        raise OfflineError("path.unavailable", "A selected local path is missing or inaccessible.", "Check the explicit data, database, configuration or artifact path and local permissions.") from None


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _no_journal(path: Path) -> None:
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and checked_path(sidecar).st_size):
            raise OfflineError("database.active_journal", "The selected database has an active or unsafe journal/WAL.", "Stop writers cleanly and use a checkpointed backup/snapshot. The CLI never checkpoints or deletes journals.")


@contextmanager
def offline_database(path: Path) -> Iterator[sqlite3.Connection]:
    """Read immutable SQLite without creating DB, WAL, SHM or migration files.

    Immutable is deliberately restricted to stopped/checkpointed inputs. Detect
    observed changes before returning any result; no claim of live DB health.
    """
    info = checked_path(path)
    if info.st_size > MAX_DATABASE_BYTES:
        raise OfflineError("database.too_large", "The offline database exceeds the 128 MiB safety limit.", "Use the application backup workflow or a smaller checkpointed diagnostic snapshot.")
    _no_journal(path)
    deadline = time.monotonic() + SQL_SECONDS
    try:
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            yield db
        _no_journal(path)
        if _fingerprint(checked_path(path)) != _fingerprint(info):
            raise OfflineError("database.changed", "The database changed during the offline read; results were discarded.", "Stop all writers and retry against a stable checkpointed snapshot.")
    except sqlite3.Error:
        raise OfflineError("database.unreadable", "SQLite could not safely complete the bounded read.", "Check the selected snapshot, permissions and integrity; do not run migrations automatically.") from None


def quick_check(db: sqlite3.Connection) -> None:
    rows = db.execute("PRAGMA quick_check").fetchall()
    if len(rows) != 1 or rows[0][0] != "ok":
        raise OfflineError("database.integrity", "SQLite integrity verification failed.", "Retain the original files and recover from a separately verified backup; do not overwrite the source.")


def table_columns(db: sqlite3.Connection) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    names = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    if len(names) > 256:
        raise OfflineError("database.schema", "The database contains too many tables for this offline contract.", "Select a supported BASSWIESN database.")
    for row in names:
        name = str(row[0])
        # Table names are bound parameters, never concatenated into SQL.
        result[name] = {str(item[1]) for item in db.execute("SELECT * FROM pragma_table_info(?)", (name,))}
    return result


def require_core(db: sqlite3.Connection) -> None:
    columns = table_columns(db)
    if not {"device_id", "ip_address", "reachable"}.issubset(columns.get("devices", set())) or not {"key", "value"}.issubset(columns.get("settings", set())):
        raise OfflineError("database.schema", "The database is missing the supported core tables or columns.", "Select a BASSWIESN application snapshot; inspect/upgrade its schema through the documented installation workflow.")


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


@dataclass(frozen=True)
class _PinnedDirectory:
    descriptor: int
    bindings: tuple[tuple[int, str, int], ...]

    def validate(self) -> None:
        try:
            for parent, name, child in self.bindings:
                entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(entry.st_mode) or _identity(entry) != _identity(os.fstat(child)):
                    raise ValueError("directory changed")
        except (OSError, ValueError):
            raise OfflineError("output.path_changed", "A selected directory changed during artifact creation.", "Use a stable private output directory; no substituted path is trusted.") from None


@contextmanager
def _pinned_directory(path: Path) -> Iterator[_PinnedDirectory]:
    """Walk from the filesystem root with openat; retain every directory inode."""
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise OfflineError("path.unsafe", "Parent traversal is not supported for artifact paths.", "Use an explicit absolute output path without parent traversal or symlinks.")
    descriptors: list[int] = []
    bindings: list[tuple[int, str, int]] = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        current = os.open(absolute.anchor, flags)
        descriptors.append(current)
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=current)
            descriptors.append(child)
            bindings.append((current, component, child))
            current = child
        directory = _PinnedDirectory(current, tuple(bindings))
        directory.validate()
        yield directory
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _private_directory(directory: _PinnedDirectory) -> None:
    info = os.fstat(directory.descriptor)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise OfflineError("output.permissions", "The output directory is not private to the current local user.", "Create/select an owned 0700 directory; backups contain sensitive application data.")


def _entry_matches(directory: int, name: str, expected: os.stat_result) -> bool:
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        return stat.S_ISREG(current.st_mode) and _identity(current) == _identity(expected)
    except FileNotFoundError:
        return False


def _unlink_owned(directory: int, name: str, expected: os.stat_result) -> None:
    # Never follow or remove a substituted symlink/file during cleanup. The
    # validated 0700 directory is the boundary against other local UIDs.
    if _entry_matches(directory, name, expected):
        os.unlink(name, dir_fd=directory)


@contextmanager
def private_artifact(output: Path) -> Iterator[BinaryIO]:
    """Write/verify only a retained inode; publish that inode without replacement.

    Linux procfs permits linkat(AT_SYMLINK_FOLLOW) of our own retained descriptor
    without privileged AT_EMPTY_PATH. No path-based fallback is permitted.
    """
    with _pinned_directory(output.parent) as directory:
        _private_directory(directory)
        try:
            os.stat(output.name, dir_fd=directory.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise OfflineError("output.exists", "The output path already exists; nothing was replaced.", "Choose a new artifact filename.")
        name = f".basswiesnctl-{secrets.token_hex(16)}.tmp"
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory.descriptor)
        info = os.fstat(fd)
        published = False
        complete = False
        try:
            with os.fdopen(fd, "w+b") as staged:
                yield staged
                staged.flush()
                os.fchmod(staged.fileno(), 0o600)
                os.fsync(staged.fileno())
                directory.validate()
                _private_directory(directory)
                if not _entry_matches(directory.descriptor, name, info) or os.fstat(staged.fileno()).st_nlink != 1:
                    raise OfflineError("output.stage_changed", "The staging file was replaced or linked; publication was refused.", "Use a stable private directory and retry with a new filename.")
                # The source is the retained file descriptor, not the attackable
                # staging name. Destination link creation is atomic/exclusive.
                os.link(f"/proc/self/fd/{staged.fileno()}", output.name, dst_dir_fd=directory.descriptor, follow_symlinks=True)
                published = True
                directory.validate()
                _private_directory(directory)
                if not _entry_matches(directory.descriptor, output.name, info):
                    raise OfflineError("output.stage_changed", "Published artifact identity could not be confirmed.", "Review the private output directory before retrying; no existing filename was overwritten.")
                os.fsync(directory.descriptor)
                complete = True
        finally:
            if published and not complete:
                _unlink_owned(directory.descriptor, output.name, info)
            _unlink_owned(directory.descriptor, name, info)


def write_json_artifact(output: Path, payload: dict[str, JsonValue]) -> None:
    with private_artifact(output) as staged:
        staged.write((json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8"))


def create_backup(source: Path, output: Path) -> dict[str, JsonValue]:
    """Create a shared-format backup; snapshot DB remains sensitive, not redacted."""
    # Avoid output containing aliases of the source, even before its existence check.
    if output.absolute() == source.absolute():
        raise OfflineError("output.source", "The backup cannot replace its database source.", "Choose a new filename in a private backup directory.")
    with private_artifact(output) as staged:
        with tempfile.TemporaryDirectory(prefix="basswiesnctl-backup-") as temporary:
            snapshot = Path(temporary) / "database.sqlite"
            with offline_database(source) as db:
                quick_check(db)
                require_core(db)
                if not schema_compatible(db):
                    raise OfflineError("backup.schema", "The source does not match the current declared application schema.", "Retain the source and inspect the documented upgrade workflow; no baseline was inferred and no archive was published.")
                baseline = declared_baseline()
                deadline = time.monotonic() + SQL_SECONDS

                def progress(status: int, remaining: int, total: int) -> None:
                    del status, remaining, total
                    if time.monotonic() > deadline:
                        raise OfflineError("backup.timeout", "The bounded SQLite backup timed out.", "Retry against a stable smaller snapshot; no archive was published.")

                with closing(sqlite3.connect(snapshot)) as destination:
                    db.backup(destination, pages=128, progress=progress)
            with offline_database(snapshot) as copied:
                quick_check(copied)
                require_core(copied)
            # The database is the authoritative settings backup. Do not duplicate
            # arbitrary values/credentials in a supposedly redacted JSON sidecar.
            settings = b"{}\n"
            database_bytes = snapshot.read_bytes()
            components = {"database.sqlite": database_bytes, "settings.json": settings}
            manifest: dict[str, JsonValue] = {
                "format": "basswiesn-backup-v1",
                "created_at": datetime.now(UTC).isoformat(),
                "version": __version__,
                "schema_baseline": baseline,
                "quick_check": "ok",
                "restore_mode": "prepare_and_restart",
                "files": [{"path": name, "sha256": hashlib.sha256(content).hexdigest()} for name, content in components.items()],
            }
            validate_system_manifest(manifest)
            with ZipFile(staged, "w", ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
                for name, content in components.items():
                    archive.writestr(name, content)
        staged.flush()
        verified = _verify_backup_handle(staged)
    return {**verified, "created": True, "contains_sensitive_database": True, "authentication_store_included": False}


def verify_backup(path: Path) -> dict[str, JsonValue]:
    """Verify untrusted ZIP metadata, hashes and SQLite; no extraction to data_dir."""
    info = checked_path(path)
    with _pinned_directory(path.parent) as directory:
        # A leaf replaced by a FIFO between inspection and open must not hang.
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory.descriptor)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or _fingerprint(info) != _fingerprint(opened):
                raise OfflineError("backup.changed", "The selected archive changed before verification.", "Select a stable private backup file.")
            verified = _verify_backup_handle(handle)
            directory.validate()
            if not _entry_matches(directory.descriptor, path.name, info):
                raise OfflineError("backup.changed", "The selected archive changed during verification.", "Select a stable private backup file.")
            return verified


def _verify_backup_handle(handle: BinaryIO) -> dict[str, JsonValue]:
    info = os.fstat(handle.fileno())
    if info.st_size > MAX_ARCHIVE_BYTES:
        raise OfflineError("backup.too_large", "The archive exceeds the 128 MiB safety limit.", "Use a supported bounded system backup.")
    try:
        handle.seek(0)
        with ZipFile(handle) as archive:
            names = validate_archive_members(archive.infolist(), max_entries=3, max_uncompressed_bytes=MAX_ARCHIVE_BYTES)
            if set(names) != SYSTEM_BACKUP_COMPONENTS | {"manifest.json"}:
                raise ValueError("components")
            if any(archive.getinfo(name).file_size > MAX_METADATA_BYTES for name in ("manifest.json", "settings.json")):
                raise ValueError("metadata limit")
            manifest = json.loads(archive.read("manifest.json"))
            hashes = validate_system_manifest(manifest)
            contents = {name: archive.read(name) for name in hashes}
            if any(hashlib.sha256(contents[name]).hexdigest() != digest for name, digest in hashes.items()):
                raise ValueError("hash mismatch")
            if not isinstance(json.loads(contents["settings.json"]), dict):
                raise ValueError("settings object")
            with tempfile.TemporaryDirectory(prefix="basswiesnctl-verify-") as temporary:
                snapshot = Path(temporary) / "database.sqlite"
                snapshot.write_bytes(contents["database.sqlite"])
                with offline_database(snapshot) as db:
                    quick_check(db)
                    require_core(db)
                    compatible_schema = schema_compatible(db)
                    compatible_version = manifest.get("version") == __version__
        if _fingerprint(os.fstat(handle.fileno())) != _fingerprint(info):
            raise ValueError("archive changed")
    except (BadZipFile, ValueError, KeyError, RuntimeError, NotImplementedError, RecursionError, OfflineError, zlib.error):
        raise OfflineError("backup.invalid", "The archive is invalid, corrupt, changed or unsupported.", "Retain the source and use a complete trusted backup with matching hashes; verification is not authenticity or restore authorization.") from None
    return {
        "verified": True,
        "integrity_verified": True,
        "schema_compatible": compatible_schema,
        "version_compatible": compatible_version,
        "compatibility": "STRUCTURAL_MATCH" if compatible_schema and compatible_version else "NOT_CONFIRMED",
        "format": "basswiesn-backup-v1",
        "schema_baseline": 2,
        "components": ["database.sqlite", "settings.json"],
        "authenticity": "NOT_VERIFIED",
        "restore_performed": False,
    }
