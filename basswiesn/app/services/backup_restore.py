from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import hashlib
import json
import shutil
import sqlite3
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import text
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import Setting
from basswiesn.app.services.archive_security import UnsafeArchive, validate_archive_members


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _redacted_settings(db: Session) -> dict:
    secret_tokens = ("password", "token", "secret", "credential", "authorization", "cookie", "key")
    rows = {}
    for row in db.query(Setting).order_by(Setting.key).all():
        rows[row.key] = "<redacted>" if any(token in row.key.lower() for token in secret_tokens) else row.value
    return rows


def _copy_sqlite_backup(target: Path) -> None:
    source = _sqlite_path()
    if not source.exists():
        target.write_bytes(b"")
        return
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def create_system_backup(db: Session) -> dict:
    root = backup_root()
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    final_path = root / f"basswiesn-system-backup-{stamp}.zip"
    tmp_path = final_path.with_suffix(".zip.tmp")
    with tempfile.TemporaryDirectory(prefix="basswiesn-backup-") as tmp_name:
        stage = Path(tmp_name)
        db_copy = stage / "database.sqlite"
        settings_file = stage / "settings.json"
        _copy_sqlite_backup(db_copy)
        settings_file.write_text(json.dumps(_redacted_settings(db), ensure_ascii=False, indent=2), encoding="utf-8")
        quick_check = "unknown"
        try:
            quick_check = str(db.execute(text("PRAGMA quick_check")).scalar() or "unknown")
        except Exception as exc:
            quick_check = f"error: {exc}"
        files = {
            "database.sqlite": _sha256(db_copy),
            "settings.json": _sha256(settings_file),
        }
        manifest = {
            "format": "basswiesn-backup-v1",
            "created_at": datetime.now(UTC).isoformat(),
            "version": get_settings().version,
            "schema_baseline": 2,
            "quick_check": quick_check,
            "files": [{"path": path, "sha256": digest} for path, digest in files.items()],
            "restore_mode": "prepare_and_restart",
        }
        manifest_file = stage / "manifest.json"
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        files["manifest.json"] = _sha256(manifest_file)
        with ZipFile(tmp_path, "w", ZIP_DEFLATED) as archive:
            for filename in ("manifest.json", "database.sqlite", "settings.json"):
                archive.write(stage / filename, filename)
        tmp_path.replace(final_path)
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
    return resolved


def preview_system_backup(path_text: str) -> dict:
    path = _validate_backup_path(path_text)
    with ZipFile(path) as archive:
        try:
            names = validate_archive_members(archive.infolist(), max_entries=256, max_uncompressed_bytes=128 * 1024 * 1024)
        except UnsafeArchive as exc:
            return {"ok": False, "path": str(path), "error": str(exc)}
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        checks = []
        for item in manifest.get("files", []):
            member = item.get("path", "")
            expected = item.get("sha256", "")
            if member not in names:
                checks.append({"path": member, "ok": False, "error": "missing"})
                continue
            digest = hashlib.sha256(archive.read(member)).hexdigest()
            checks.append({"path": member, "ok": digest == expected, "sha256": digest})
    return {"ok": all(item["ok"] for item in checks), "path": str(path), "manifest": manifest, "checks": checks}


def prepare_system_restore(db: Session, path_text: str) -> dict:
    preview = preview_system_backup(path_text)
    if not preview["ok"]:
        return {**preview, "prepared": False}
    safety = create_system_backup(db)
    target = _validate_backup_path(path_text)
    pending = backup_root() / "restore-pending.zip"
    shutil.copy2(target, pending)
    return {
        **preview,
        "prepared": True,
        "pending_archive": str(pending),
        "safety_backup": safety,
        "requires_container_stop": True,
        "next_step": "Container stoppen, Datenbank aus restore-pending.zip nach data/basswiesn.db ersetzen, Container starten, Migration/Healthcheck ausfuehren.",
    }
