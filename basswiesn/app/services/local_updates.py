from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import tarfile
from uuid import uuid4

from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import UpdateJob
from basswiesn.app.services.offline_mode import offline_mode
from basswiesn.app.services.archive_security import UnsafeArchive, validate_archive_members


MAX_UPDATE_ARCHIVE_BYTES = 512 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(name: str) -> bool:
    path = Path(name)
    return bool(name and not path.is_absolute() and ".." not in path.parts)


def inspect_local_release_archive(path_text: str, *, expected_sha256: str = "") -> dict:
    if not get_settings().update_allow_local_archive:
        return {"ok": False, "error": "local archive updates are disabled"}
    path = Path(path_text).expanduser().resolve()
    if not path.exists() or not path.is_file():
        return {"ok": False, "error": "archive not found", "path": str(path)}
    size = path.stat().st_size
    if size > MAX_UPDATE_ARCHIVE_BYTES:
        return {"ok": False, "error": "archive too large", "path": str(path), "size_bytes": size}
    digest = _sha256(path)
    if expected_sha256 and digest.lower() != expected_sha256.lower():
        return {"ok": False, "error": "sha256 mismatch", "path": str(path), "sha256": digest, "expected_sha256": expected_sha256}
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            try:
                names = validate_archive_members(members, max_entries=4096, max_uncompressed_bytes=512 * 1024 * 1024)
            except UnsafeArchive as exc:
                return {"ok": False, "error": str(exc), "path": str(path)}
            required = ["docker-compose.yml", "Dockerfile", "README.md"]
            present = {Path(name).name for name in names}
    except tarfile.TarError as exc:
        return {"ok": False, "error": f"invalid tar.gz archive: {exc}", "path": str(path)}
    missing = [item for item in required if item not in present]
    return {
        "ok": not missing,
        "path": str(path),
        "size_bytes": size,
        "sha256": digest,
        "missing_recommended_files": missing,
        "member_count": len(names),
        "sample_members": names[:50],
    }


def prepare_local_update(db: Session, path_text: str, *, expected_sha256: str = "", confirmation: str = "") -> dict:
    if offline_mode(db) == "strict" and not get_settings().update_allow_local_archive:
        return {"ok": False, "error": "updates are blocked in Strict Offline Mode"}
    preview = inspect_local_release_archive(path_text, expected_sha256=expected_sha256)
    if not preview.get("ok"):
        return {"ok": False, "preview": preview}
    if confirmation != "BASSWIESN LOCAL UPDATE":
        return {"ok": False, "preview": preview, "confirmation_required": "BASSWIESN LOCAL UPDATE"}
    job_id = str(uuid4())
    job = UpdateJob(
        job_id=job_id,
        source_type="local_archive",
        source=preview["path"],
        status="prepared",
        current_version=get_settings().version,
        target_version=get_settings().version,
        sha256=preview["sha256"],
        plan_json=json.dumps({
            "manual_only": True,
            "backup_required": True,
            "healthcheck_required": True,
            "no_remote_publish": True,
            "steps": [
                "Systembackup erstellen",
                "Container stoppen",
                "Release lokal entpacken",
                ".env und Docker-Volume erhalten",
                "Container starten",
                "Migration und Healthcheck ausführen",
            ],
        }, ensure_ascii=False),
        created_at=datetime.now(UTC),
    )
    db.add(job)
    return {"ok": True, "job_id": job_id, "preview": preview, "install_executed": False, "remote_publish": False}
