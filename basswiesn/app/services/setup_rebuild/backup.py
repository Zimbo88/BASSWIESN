"""Off-device setup baseline storage with checksums."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any

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


def write_artifact(root: Path, name: str, content: bytes | str) -> BaselineArtifact:
    _assert_non_secret_name(name)
    path = root / _safe_segment(name)
    root.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    path.write_bytes(data)
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
