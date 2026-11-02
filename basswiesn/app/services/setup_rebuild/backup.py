"""Off-device setup baseline storage with checksums."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
from uuid import uuid4

from basswiesn.app.config import get_settings


_SECRET_NAME = re.compile(r"(?i)(password|passwd|secret|token|credential|private.?key)")


@dataclass(frozen=True)
class BaselineArtifact:
    path: str
    sha256: str
    size: int


def _safe_segment(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return value[:128] or "unknown"


def baseline_root(timestamp: str, device_id: str) -> Path:
    stamp = _safe_segment(timestamp)
    identifier = _safe_segment(device_id.upper())
    return get_settings().data_dir / "setup-rebuild" / stamp / identifier / "baseline"


def _assert_non_secret_name(name: str) -> None:
    if _SECRET_NAME.search(name):
        raise ValueError("secret-named artifacts are not permitted in setup baselines")


def write_artifact(root: Path, name: str, content: bytes | str, *, max_bytes: int = 1048576) -> BaselineArtifact:
    _assert_non_secret_name(name)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", name):
        raise ValueError("unsafe artifact name")
    root = root.absolute()
    if ".." in root.parts:
        raise ValueError("unsafe artifact directory")
    path = root / name
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    if not 0 < max_bytes <= 20 * 1048576 or len(data) > max_bytes:
        raise ValueError("baseline artifact exceeds its bounded snapshot size")
    directory = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".capture-{uuid4().hex}"
    fd: int | None = None
    created = False
    try:
        for part in root.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                    os.fsync(directory)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("unsafe baseline directory ownership or permissions")
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise ValueError("unsafe existing baseline artifact")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        created = True
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("baseline write made no progress")
            remaining = remaining[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        # Preserve the existing update contract without truncating an old
        # artifact on write failure or following a replaced/symlink leaf.
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        created = False
        os.fsync(directory)
    finally:
        if fd is not None:
            os.close(fd)
        if created:
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)
    digest = hashlib.sha256(data).hexdigest()
    return BaselineArtifact(path=str(path), sha256=digest, size=len(data))


def write_json_artifact(root: Path, name: str, value: Any) -> BaselineArtifact:
    return write_artifact(root, name, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def read_json_artifact(path: Path) -> Any:
    """Read current JSON and the malformed literal-``\\n`` 1.6 baseline tail.

    The compatibility is deliberately limited to exactly one final literal
    escape produced by the old writer.  It does not attempt to repair an
    otherwise malformed or truncated backup.
    """

    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        if raw.endswith("\\n"):
            return json.loads(raw[:-2])
        raise


def write_baseline_metadata(root: Path, metadata: dict[str, Any]) -> BaselineArtifact:
    safe = {
        "created_at": datetime.now(UTC).isoformat(),
        "metadata": metadata,
    }
    return write_json_artifact(root, "baseline-metadata.json", safe)
