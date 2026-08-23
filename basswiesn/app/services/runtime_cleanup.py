"""Bounded cleanup for BASSWIESN-owned runtime artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    size_bytes: int
    action: str


_KNOWN_PATTERNS = {
    "logs": re.compile(r"^master\.log\.\d+$"),
    "diagnostics": re.compile(r"^basswiesn-diagnostic-[A-Za-z0-9_.-]+\.zip$"),
    "support": re.compile(r"^basswiesn-support-[A-Za-z0-9_.-]+\.zip$"),
}


def plan_runtime_cleanup(data_dir: Path, *, max_bytes_by_area: dict[str, int] | None = None) -> list[CleanupCandidate]:
    """Plan deletions only for known, regular files below known directories."""

    limits = max_bytes_by_area or {}
    candidates: list[CleanupCandidate] = []
    for area, pattern in _KNOWN_PATTERNS.items():
        directory = Path(data_dir) / area
        if directory.is_symlink() or not directory.is_dir():
            continue
        files = []
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file() or not pattern.fullmatch(path.name):
                continue
            try:
                files.append((path, path.stat().st_size, path.stat().st_mtime_ns))
            except OSError:
                continue
        limit = max(0, int(limits.get(area, 0)))
        total = sum(size for _, size, _ in files)
        for path, size, _ in sorted(files, key=lambda item: (item[2], item[0].name)):
            if limit <= 0 or total <= limit:
                break
            candidates.append(CleanupCandidate(path, size, "quota"))
            total -= size
    return candidates


def run_runtime_cleanup(data_dir: Path, *, max_bytes_by_area: dict[str, int] | None = None, dry_run: bool = True) -> dict:
    planned = plan_runtime_cleanup(data_dir, max_bytes_by_area=max_bytes_by_area)
    deleted = 0
    errors = []
    if not dry_run:
        for item in planned:
            try:
                if item.path.is_symlink() or not item.path.is_file():
                    continue
                item.path.unlink()
                deleted += 1
            except OSError as exc:
                errors.append({"path": str(item.path), "error": type(exc).__name__})
    return {
        "dry_run": dry_run,
        "planned": [{"path": str(item.path), "size_bytes": item.size_bytes, "action": item.action} for item in planned],
        "deleted": deleted,
        "errors": errors,
    }
