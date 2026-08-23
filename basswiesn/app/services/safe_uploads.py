"""Bounded, atomic storage for user supplied files.

This module deliberately has no application-specific database knowledge.  A
route chooses the purpose directory and the allowed media types; this service
owns the filesystem safety rules.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
from typing import BinaryIO, Iterable
from uuid import uuid4


class UploadError(ValueError):
    """Base class for a rejected upload."""


class UploadTooLarge(UploadError):
    pass


class UnsupportedUploadType(UploadError):
    pass


class InvalidUpload(UploadError):
    pass


class UploadQuotaExceeded(UploadError):
    pass


@dataclass(frozen=True)
class StoredUpload:
    path: Path
    filename: str
    size_bytes: int
    content_type: str


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"}
_MAGIC = {
    ".png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    ".jpg": lambda data: data.startswith(b"\xff\xd8\xff"),
    ".jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    ".gif": lambda data: data.startswith((b"GIF87a", b"GIF89a")),
    ".webp": lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
    ".wav": lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE",
    ".flac": lambda data: data.startswith(b"fLaC"),
    ".ogg": lambda data: data.startswith(b"OggS"),
    ".m4a": lambda data: len(data) >= 12 and data[4:8] == b"ftyp",
    ".mp3": lambda data: data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0),
    ".aac": lambda data: len(data) >= 2 and data[0] == 0xFF and data[1] & 0xF6 == 0xF0,
}


def _safe_basename(original: str | None, allowed_suffixes: set[str]) -> str:
    value = str(original or "").replace("\\", "/")
    if not value or value.endswith("/") or "/" in value or _CONTROL_CHARACTERS.search(value):
        raise InvalidUpload("invalid upload filename")
    name = Path(value).name
    if name in {".", ".."} or ".." in Path(name).parts:
        raise InvalidUpload("invalid upload filename")
    suffix = Path(name).suffix.lower()
    if suffix not in allowed_suffixes:
        raise UnsupportedUploadType("unsupported upload file extension")
    if len(name) > 160:
        raise InvalidUpload("upload filename is too long")
    return name


def _existing_quota_bytes(directory: Path) -> int:
    total = 0
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _validate_magic(path: Path, suffix: str, content_type: str, sample: bytes) -> None:
    expected = _MAGIC.get(suffix)
    if expected is None or not expected(sample):
        raise InvalidUpload("file content does not match its declared type")
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    compatible = {
        ".png": {"image/png"}, ".jpg": {"image/jpeg"}, ".jpeg": {"image/jpeg"},
        ".gif": {"image/gif"}, ".webp": {"image/webp"}, ".wav": {"audio/wav", "audio/x-wav"},
        ".flac": {"audio/flac", "audio/x-flac"}, ".ogg": {"audio/ogg", "application/ogg"},
        ".mp3": {"audio/mpeg"}, ".m4a": {"audio/mp4", "video/mp4"}, ".aac": {"audio/aac"},
    }[suffix]
    if declared and declared not in compatible and declared != "application/octet-stream":
        raise UnsupportedUploadType("content type does not match the file extension")


def store_upload_stream(
    stream: BinaryIO,
    *,
    original_name: str | None,
    directory: Path,
    max_bytes: int,
    quota_bytes: int,
    allowed_suffixes: Iterable[str] = _IMAGE_SUFFIXES | _AUDIO_SUFFIXES,
    content_type: str = "",
) -> StoredUpload:
    """Store a bounded upload using a unique name and atomic replacement.

    The destination is resolved before writing and must be a real directory;
    symlinked directories are rejected.  A unique name avoids silently
    replacing an existing station or logo file.
    """

    allowed = {str(item).lower() if str(item).startswith(".") else f".{str(item).lower()}" for item in allowed_suffixes}
    name = _safe_basename(original_name, allowed)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise InvalidUpload("upload destination is not a safe directory")
    directory = directory.resolve()
    existing_bytes = _existing_quota_bytes(directory)
    if existing_bytes >= quota_bytes:
        raise UploadQuotaExceeded("upload storage quota exceeded")

    suffix = Path(name).suffix.lower()
    safe_name = f"{Path(name).stem[:100]}-{uuid4().hex}{suffix}"
    target = directory / safe_name
    temp_path: Path | None = None
    size = 0
    sample = bytearray()
    try:
        with tempfile.NamedTemporaryFile(prefix=".upload-", dir=directory, delete=False) as temp:
            temp_path = Path(temp.name)
            while True:
                chunk = stream.read(min(1024 * 1024, max_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLarge("upload exceeds the configured size limit")
                if len(sample) < 64 * 1024:
                    sample.extend(chunk[: 64 * 1024 - len(sample)])
                temp.write(chunk)
            temp.flush()
            os.fsync(temp.fileno())
        if size == 0:
            raise InvalidUpload("empty upload is not supported")
        if existing_bytes + size > quota_bytes:
            raise UploadQuotaExceeded("upload storage quota exceeded")
        _validate_magic(temp_path, suffix, content_type, bytes(sample))
        os.replace(temp_path, target)
        temp_path = None
        return StoredUpload(target, safe_name, size, content_type or "application/octet-stream")
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


async def store_upload(file, **kwargs) -> StoredUpload:
    """Run blocking multipart-file IO off the event loop."""

    return await asyncio.to_thread(
        store_upload_stream,
        file.file,
        original_name=getattr(file, "filename", None),
        content_type=getattr(file, "content_type", "") or "",
        **kwargs,
    )
