from __future__ import annotations

from datetime import UTC, datetime
from contextlib import closing
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from typing import Any
from uuid import uuid4
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import Setting
from basswiesn.app.services.archive_security import UnsafeArchive, validate_archive_members
from basswiesn.app.services.backup_manifest import SYSTEM_BACKUP_COMPONENTS, validate_system_manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _data_path() -> Path:
    return get_settings().data_dir.resolve()


def backup_root() -> Path:
    root = (_data_path() / "backups" / "system").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sqlite_path() -> Path:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        raise ValueError("Only SQLite database URLs are supported for local backup")
    path = Path(url.replace("sqlite:///", "", 1))
    return path if path.is_absolute() else path.resolve()


def _redacted_settings(db: Session) -> dict[str, Any]:
    secret_tokens = ("password", "token", "secret", "credential", "authorization", "cookie", "key")
    rows = {}
    for row in db.query(Setting).order_by(Setting.key).all():
        rows[row.key] = "<redacted>" if any(token in row.key.lower() for token in secret_tokens) else row.value
    return rows


def _copy_sqlite_backup(target: Path) -> None:
    source = _sqlite_path()
    if not source.is_file():
        raise ValueError("Source database is missing; no backup was created")
    # Read-only source prevents a race from creating an empty database. Check
    # the copy, not the live database, because the copy is what will be restored.
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        rows = dst.execute("PRAGMA quick_check").fetchall()
        if rows != [("ok",)]:
            raise ValueError("Copied database failed its integrity check")


def create_system_backup(db: Session) -> dict[str, Any]:
    root = backup_root()
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    # A pre-restore backup can happen in the same second as its source backup.
    # Never overwrite that source or a concurrent snapshot.
    final_path = root / f"basswiesn-system-backup-{stamp}-{uuid4().hex}.zip"
    with tempfile.TemporaryDirectory(prefix="basswiesn-backup-") as tmp_name:
        stage = Path(tmp_name)
        db_copy = stage / "database.sqlite"
        settings_file = stage / "settings.json"
        _copy_sqlite_backup(db_copy)
        settings_file.write_text(json.dumps(_redacted_settings(db), ensure_ascii=False, indent=2), encoding="utf-8")
        files = {
            "database.sqlite": _sha256(db_copy),
            "settings.json": _sha256(settings_file),
        }
        manifest = {
            "format": "basswiesn-backup-v1",
            "created_at": datetime.now(UTC).isoformat(),
            "version": get_settings().version,
            "schema_baseline": 2,
            "quick_check": "ok",
            "files": [{"path": path, "sha256": digest} for path, digest in files.items()],
            "restore_mode": "prepare_and_restart",
        }
        manifest_file = stage / "manifest.json"
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        files["manifest.json"] = _sha256(manifest_file)
        with tempfile.NamedTemporaryFile(dir=root, prefix=".backup-", suffix=".tmp", delete=False) as handle:
            tmp_path = Path(handle.name)
        try:
            with ZipFile(tmp_path, "w", ZIP_DEFLATED) as archive:
                for filename in ("manifest.json", "database.sqlite", "settings.json"):
                    archive.write(stage / filename, filename)
            os.chmod(tmp_path, 0o600)
            _sync_file(tmp_path)
            tmp_path.replace(final_path)
            _sync_directory(root)
        finally:
            tmp_path.unlink(missing_ok=True)
    return {"ok": True, "path": str(final_path), "filename": final_path.name, "manifest": manifest}


def _safe_archive_member(name: str) -> bool:
    path = Path(name)
    return bool(name and not path.is_absolute() and ".." not in path.parts)


def _validate_backup_path(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = backup_root() / path
    resolved = path.resolve()
    root = backup_root()
    if root not in resolved.parents and resolved != root:
        raise ValueError("Backup path must stay below BASSWIESN data/backups/system")
    if not resolved.exists():
        raise FileNotFoundError(str(resolved))
    if not resolved.is_file():
        raise ValueError("Backup must be a regular file")
    return resolved


def _validate_database_copy(path: Path) -> None:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as snapshot:
        if snapshot.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Backup database integrity check failed")
        tables = {row[0] for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"devices", "settings"}.issubset(tables):
            raise ValueError("Backup database is missing core tables")


def preview_system_backup(path_text: str) -> dict[str, Any]:
    path = _validate_backup_path(path_text)
    try:
        with ZipFile(path) as archive:
            names = validate_archive_members(archive.infolist(), max_entries=256, max_uncompressed_bytes=128 * 1024 * 1024)
            if set(names) != SYSTEM_BACKUP_COMPONENTS | {"manifest.json"}:
                raise ValueError("Archive must contain exactly the supported backup components")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            hashes = validate_system_manifest(manifest)
            checks = []
            for member, expected in hashes.items():
                digest = hashlib.sha256(archive.read(member)).hexdigest()
                checks.append({"path": member, "ok": digest == expected, "sha256": digest})
            if all(item["ok"] for item in checks):
                if not isinstance(json.loads(archive.read("settings.json")), dict):
                    raise ValueError("Backup settings must be an object")
                # A forged quick_check field or matching hash alone cannot prove
                # that the payload is a usable SQLite database.
                with tempfile.TemporaryDirectory(prefix="basswiesn-backup-preview-") as stage:
                    copied = Path(stage) / "database.sqlite"
                    copied.write_bytes(archive.read("database.sqlite"))
                    _validate_database_copy(copied)
    except (UnsafeArchive, ValueError, BadZipFile, KeyError, RuntimeError, NotImplementedError, sqlite3.DatabaseError):
        # Do not echo attacker-controlled manifest contents or archive errors.
        return {"ok": False, "path": str(path), "error": "Invalid, incomplete or unsupported backup archive"}
    return {"ok": all(item["ok"] for item in checks), "path": str(path), "manifest": manifest, "checks": checks}


def prepare_system_restore(db: Session, path_text: str) -> dict[str, Any]:
    preview = preview_system_backup(path_text)
    if not preview["ok"]:
        return {**preview, "prepared": False}
    target = _validate_backup_path(path_text)
    pending = backup_root() / "restore-pending.zip"
    with tempfile.NamedTemporaryFile(dir=backup_root(), prefix=".restore-", suffix=".zip", delete=False) as handle:
        staged = Path(handle.name)
    try:
        shutil.copyfile(target, staged)
        # Validate the bytes actually staged, not just the original pathname.
        staged_preview = preview_system_backup(str(staged))
        if not staged_preview["ok"] or staged_preview["manifest"] != preview["manifest"]:
            return {"ok": False, "prepared": False, "error": "Backup changed during restore preparation"}
        try:
            safety = create_system_backup(db)
            # A returned path/success flag is not evidence that the mandatory
            # recovery copy is complete, readable or internally consistent.
            if safety.get("ok") is not True or not isinstance(safety.get("path"), str):
                raise ValueError("Unverified safety backup")
            safety_preview = preview_system_backup(safety["path"])
            if not safety_preview["ok"] or safety_preview["manifest"] != safety.get("manifest"):
                raise ValueError("Unverified safety backup")
            _sync_file(staged)
            staged_hash = _sha256(staged)
        except (OSError, ValueError, sqlite3.DatabaseError):
            return {"ok": False, "prepared": False, "code": "RESTORE_PREPARATION_FAILED",
                    "error": "Safety backup or durable staging could not be verified; no restore was published"}
        try:
            # Atomic create-if-absent: a double click or another worker cannot
            # replace an already prepared restore. No live database is replaced.
            os.link(staged, pending)
        except FileExistsError:
            return {"ok": False, "prepared": False, "code": "RESTORE_ALREADY_PENDING",
                    "error": "A restore is already pending; inspect it before preparing another",
                    "safety_backup": safety}
        try:
            _sync_directory(pending.parent)
            pending_preview = preview_system_backup(str(pending))
            if (not pending_preview["ok"] or pending_preview["manifest"] != staged_preview["manifest"]
                    or _sha256(pending) != staged_hash):
                raise ValueError("Pending restore readback mismatch")
        except (OSError, ValueError, sqlite3.DatabaseError):
            # Publication may already have happened. Keep the candidate and
            # recovery backup: deleting or blindly retrying could destroy the
            # only useful evidence, or overwrite another worker's pending job.
            return {"ok": False, "prepared": False, "state": "DIVERGED",
                    "code": "RESTORE_PUBLICATION_UNCONFIRMED", "pending_archive": str(pending),
                    "safety_backup": safety,
                    "error": "Pending restore exists but durability/readback is unconfirmed; manual review required"}
    finally:
        staged.unlink(missing_ok=True)
    return {
        **preview,
        "prepared": True,
        "pending_archive": str(pending),
        "safety_backup": safety,
        "pending_sha256": staged_hash,
        "requires_container_stop": True,
        "next_step": "Container stoppen, Datenbank aus restore-pending.zip nach data/basswiesn.db ersetzen, Container starten, Migration/Healthcheck ausfuehren.",
    }
