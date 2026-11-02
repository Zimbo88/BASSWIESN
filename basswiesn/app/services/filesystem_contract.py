"""Runtime directory and path safety contract."""

from __future__ import annotations

import os
from pathlib import Path


class FilesystemContractError(RuntimeError):
    pass


RUNTIME_DIRECTORIES = (
    "logs",
    "backups/system",
    "media",
    "uploads",
    "support",
    "tmp",
    "diagnostics",
)


def _safe_directory(path: Path, *, create: bool) -> Path:
    if path.is_symlink():
        raise FilesystemContractError(f"runtime directory must not be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise FilesystemContractError(f"runtime path is not a directory: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def ensure_runtime_directories(data_dir: Path, *, create: bool = True) -> dict:
    """Create only known runtime directories and reject unsafe path shapes."""

    root = _safe_directory(Path(data_dir), create=create)
    directories = {"data": root}
    for relative in RUNTIME_DIRECTORIES:
        directories[relative] = _safe_directory(root / relative, create=create)
    return {key: str(value) for key, value in directories.items()}


def filesystem_status(data_dir: Path) -> dict:
    try:
        paths = ensure_runtime_directories(Path(data_dir), create=True)
    except FilesystemContractError as exc:
        return {"ok": False, "error": str(exc), "directories": {}}
    checks = {}
    for name in ("data", *RUNTIME_DIRECTORIES):
        path = Path(paths[name])
        checks[name] = {
            "path": str(path),
            "exists": path.is_dir(),
            "writable": os.access(path, os.W_OK),
            "mode": oct(path.stat().st_mode & 0o777),
        }
    required = ("data", "logs", "tmp")
    ok = all(checks[name]["exists"] and checks[name]["writable"] for name in required)
    degraded = any(not checks[name]["exists"] or not checks[name]["writable"] for name in checks if name not in required)
    return {"ok": ok, "degraded": degraded, "directories": checks, "contract_directories": paths}


def safe_runtime_child(data_dir: Path, relative: str) -> Path:
    """Return a child below data_dir without following an external symlink."""

    root = Path(data_dir).resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise FilesystemContractError("runtime path escapes data directory")
    if (root / relative).is_symlink():
        raise FilesystemContractError("runtime path must not be a symlink")
    return candidate
