from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import MediaItem, MediaRoot


SUPPORTED_SUFFIXES = {
    ".mp3": ("mp3", "audio/mpeg"),
    ".m4a": ("aac", "audio/mp4"),
    ".aac": ("aac", "audio/aac"),
    ".wav": ("wav", "audio/wav"),
    ".flac": ("flac", "audio/flac"),
}
MAX_SCAN_ITEMS = 5000


@dataclass(frozen=True)
class MediaPathCheck:
    ok: bool
    reason: str
    root: Path | None = None
    path: Path | None = None
    relative_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "root": str(self.root) if self.root else "",
            "path": str(self.path) if self.path else "",
            "relative_path": self.relative_path,
        }


def configured_roots(db: Session) -> list[MediaRoot]:
    roots = db.query(MediaRoot).filter(MediaRoot.enabled == True).order_by(MediaRoot.path).all()  # noqa: E712
    if roots:
        return roots
    for path in get_settings().media_roots:
        root = MediaRoot(path=str(Path(path).expanduser().resolve()), enabled=get_settings().media_enabled)
        db.add(root)
        roots.append(root)
    return roots


def validate_media_root(path_text: str) -> MediaPathCheck:
    path = Path(path_text).expanduser()
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        return MediaPathCheck(False, str(exc))
    if not resolved.is_absolute():
        return MediaPathCheck(False, "media root must be absolute")
    if not resolved.exists() or not resolved.is_dir():
        return MediaPathCheck(False, "media root must be an existing directory", path=resolved)
    return MediaPathCheck(True, "ok", root=resolved, path=resolved)


def resolve_media_path(db: Session, root_id: int, relative_path: str) -> MediaPathCheck:
    root = db.query(MediaRoot).filter(MediaRoot.id == root_id, MediaRoot.enabled == True).one_or_none()  # noqa: E712
    if root is None:
        return MediaPathCheck(False, "media root not found or disabled")
    root_path = Path(root.path).resolve()
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        return MediaPathCheck(False, "path traversal blocked", root=root_path, relative_path=relative_path)
    target = (root_path / relative).resolve()
    if root_path != target and root_path not in target.parents:
        return MediaPathCheck(False, "symlink escape blocked", root=root_path, path=target, relative_path=relative_path)
    if not target.exists() or not target.is_file():
        return MediaPathCheck(False, "media file not found", root=root_path, path=target, relative_path=relative_path)
    return MediaPathCheck(True, "ok", root=root_path, path=target, relative_path=str(target.relative_to(root_path)))


def _sha256_head(path: Path, max_bytes: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        remaining = max_bytes
        while remaining > 0:
            chunk = handle.read(min(64 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _format_for(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_SUFFIXES:
        return SUPPORTED_SUFFIXES[suffix]
    guessed = mimetypes.guess_type(path.name)[0] or ""
    if guessed.startswith("audio/"):
        return suffix.lstrip(".") or "unknown", guessed
    return "unknown", guessed


def _media_item_dict(item: MediaItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "root_id": item.root_id,
        "relative_path": item.relative_path,
        "title": item.title,
        "artist": item.artist,
        "album": item.album,
        "duration_seconds": item.duration_seconds,
        "format": item.format,
        "codec": item.codec,
        "bitrate": item.bitrate,
        "samplerate": item.samplerate,
        "size_bytes": item.size_bytes,
        "mime_type": item.mime_type,
        "favorite": item.favorite,
        "scanned_at": item.scanned_at.isoformat() if item.scanned_at else "",
    }


def upsert_media_root(db: Session, path_text: str, *, label: str = "", enabled: bool = True) -> dict:
    check = validate_media_root(path_text)
    if not check.ok:
        raise ValueError(check.reason)
    assert check.root is not None
    row = db.query(MediaRoot).filter(MediaRoot.path == str(check.root)).one_or_none()
    if row is None:
        row = MediaRoot(path=str(check.root))
        db.add(row)
    row.label = label or check.root.name
    row.enabled = bool(enabled)
    row.updated_at = datetime.now(UTC)
    return {"id": row.id, "path": row.path, "label": row.label, "enabled": row.enabled}


def scan_media_root(db: Session, root_id: int) -> dict:
    if not get_settings().media_enabled:
        return {"enabled": False, "scanned": 0, "items": [], "message": "Local media is disabled"}
    root = db.query(MediaRoot).filter(MediaRoot.id == root_id, MediaRoot.enabled == True).one_or_none()  # noqa: E712
    if root is None:
        raise ValueError("media root not found or disabled")
    root_path = Path(root.path).resolve()
    max_size = get_settings().media_max_file_size_mb * 1024 * 1024
    scanned = 0
    skipped: list[dict] = []
    items: list[dict] = []
    for path in root_path.rglob("*"):
        if scanned >= MAX_SCAN_ITEMS:
            skipped.append({"path": "", "reason": "scan item limit reached"})
            break
        try:
            if not path.is_file():
                continue
            resolved = path.resolve()
            if root_path != resolved and root_path not in resolved.parents:
                skipped.append({"path": str(path), "reason": "symlink escape"})
                continue
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            stat = path.stat()
            if stat.st_size > max_size:
                skipped.append({"path": str(path.relative_to(root_path)), "reason": "file too large"})
                continue
            media_format, mime = _format_for(path)
            relative = str(resolved.relative_to(root_path))
            item = db.query(MediaItem).filter(MediaItem.root_id == root.id, MediaItem.relative_path == relative).one_or_none()
            if item is None:
                item = MediaItem(root_id=root.id, relative_path=relative)
                db.add(item)
            item.title = path.stem
            item.format = media_format
            item.codec = media_format
            item.size_bytes = int(stat.st_size)
            item.mime_type = mime
            item.sha256 = _sha256_head(path)
            item.compatibility_json = "{}"
            item.scanned_at = datetime.now(UTC)
            items.append(_media_item_dict(item))
            scanned += 1
        except OSError as exc:
            skipped.append({"path": str(path), "reason": str(exc)})
    root.last_scan_at = datetime.now(UTC)
    root.last_error = ""
    return {"enabled": True, "root_id": root.id, "scanned": scanned, "items": items, "skipped": skipped}


def search_media(db: Session, *, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
    rows_query = db.query(MediaItem)
    if query:
        pattern = f"%{query}%"
        rows_query = rows_query.filter(MediaItem.title.like(pattern))
    rows = rows_query.order_by(MediaItem.title).limit(min(max(limit, 1), 500)).all()
    return [_media_item_dict(row) for row in rows]


def media_status(db: Session) -> dict:
    roots = db.query(MediaRoot).order_by(MediaRoot.path).all()
    return {
        "enabled": get_settings().media_enabled,
        "experimental": True,
        "roots": [
            {
                "id": root.id,
                "path": root.path,
                "label": root.label,
                "enabled": root.enabled,
                "last_scan_at": root.last_scan_at.isoformat() if root.last_scan_at else "",
                "last_error": root.last_error,
            }
            for root in roots
        ],
        "supported_formats": sorted(SUPPORTED_SUFFIXES),
    }
