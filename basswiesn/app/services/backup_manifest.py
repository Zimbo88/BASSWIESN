"""Fail-closed contract for the existing v1 local system backup format.

Hashes detect corruption, not authenticity. An untrusted backup must never be
treated as an authenticated release or as permission to contact a device.
"""
from __future__ import annotations

from datetime import datetime
import re

SYSTEM_BACKUP_COMPONENTS = frozenset({"database.sqlite", "settings.json"})


def validate_system_manifest(value: object) -> dict[str, str]:
    """Return required component hashes only for a supported complete manifest."""
    if not isinstance(value, dict):
        raise ValueError("Backup manifest must be an object")
    if value.get("format") != "basswiesn-backup-v1":
        raise ValueError("Unsupported backup format")
    if type(value.get("schema_baseline")) is not int or value["schema_baseline"] != 2:
        raise ValueError("Unsupported backup schema baseline")
    if value.get("restore_mode") != "prepare_and_restart":
        raise ValueError("Unsupported restore mode")
    if value.get("quick_check") != "ok":
        raise ValueError("Backup database integrity was not confirmed")
    version = value.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise ValueError("Backup version is missing or invalid")
    created = value.get("created_at")
    if not isinstance(created, str):
        raise ValueError("Backup timestamp is missing")
    try:
        timestamp = datetime.fromisoformat(created)
    except ValueError:
        raise ValueError("Backup timestamp is invalid") from None
    if timestamp.tzinfo is None:
        raise ValueError("Backup timestamp must include a timezone")
    files = value.get("files")
    if not isinstance(files, list) or len(files) != len(SYSTEM_BACKUP_COMPONENTS):
        raise ValueError("Backup must list all required components exactly once")
    hashes: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Invalid backup component record")
        path, digest = item.get("path"), item.get("sha256")
        if not isinstance(path, str) or path not in SYSTEM_BACKUP_COMPONENTS or path in hashes:
            raise ValueError("Unknown or duplicate backup component")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid component SHA-256")
        hashes[path] = digest
    if set(hashes) != SYSTEM_BACKUP_COMPONENTS:
        raise ValueError("Backup is missing a required component")
    return hashes
