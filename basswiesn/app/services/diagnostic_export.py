from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import platform
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import Device, DeviceInteraction, Event, RuntimeState, Setting
from basswiesn.app.services.health_center import latest_healthchecks
from basswiesn.app.services.offline_mode import offline_status
from basswiesn.app.services.support_export import build_support_bundle, redact_payload, SupportBundleTooLarge, tail_text


SECRET_KEYS = ("password", "token", "secret", "authorization", "cookie", "private", "ssh", "api_key", "key")
MAX_LOG_LINES = 1000


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("***REDACTED***" if any(token in key.lower() for token in SECRET_KEYS) else _redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value[:500]]
    if isinstance(value, str):
        lowered = value.lower()
        if any(token in lowered for token in ("authorization:", "password=", "token=", "secret=")):
            return "***REDACTED***"
        return value[:4000]
    return value


def _hash_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else ""


def _device_summary(device: Device, *, anonymize: bool) -> dict:
    return {
        "device_id": _hash_id(device.device_id) if anonymize else device.device_id,
        "name": f"device-{_hash_id(device.name)}" if anonymize and device.name else device.name,
        "model": device.model,
        "ip_address": _hash_id(device.ip_address) if anonymize else device.ip_address,
        "firmware": device.firmware,
        "reachable": device.reachable,
        "failure_count": device.failure_count,
        "discovery_method": getattr(device, "discovery_method", "unknown"),
        "discovery_confidence": getattr(device, "discovery_confidence", 0),
        "safe_mode": getattr(device, "safe_mode", "auto"),
        "polling_profile_override": getattr(device, "polling_profile_override", "auto"),
    }


def diagnostic_preview(db: Session, *, include_logs: bool = True, anonymize: bool = True) -> dict:
    settings = get_settings()
    tables = inspect(db.bind).get_table_names() if db.bind is not None else []
    logs = []
    log_path = settings.data_dir / "logs" / "master.log"
    if include_logs and log_path.exists():
        logs.append({"path": "logs/master.log", "max_lines": MAX_LOG_LINES, "redacted": True})
    return {
        "diagnostic_id": str(uuid4()),
        "version": settings.version,
        "created_at": datetime.now(UTC).isoformat(),
        "anonymize": anonymize,
        "include_logs": include_logs,
        "sections": [
            "version", "schema", "host", "healthchecks", "database_quick_check", "table_stats",
            "devices", "device_interactions", "events", "offline_mode", "settings_redacted", "logs_limited",
        ],
        "tables": sorted(tables),
        "logs": logs,
        "excluded": [".env", "backups", "secrets", "private keys", "tokens", "reference archives"],
        "max_size_mb": settings.diagnostic_max_size_mb,
    }


def _tail_lines(path: Path, limit: int) -> str:
    return "\n".join(str(_redact(line)) for line in tail_text(path, max_lines=limit).splitlines())


def create_diagnostic_export(db: Session, *, include_logs: bool = True, anonymize: bool = True) -> dict:
    preview = diagnostic_preview(db, include_logs=include_logs, anonymize=anonymize)
    diag_id = preview["diagnostic_id"]
    root = get_settings().data_dir / "diagnostics"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"basswiesn-diagnostic-{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}-{diag_id[:8]}.zip"
    quick_check = "unknown"
    try:
        quick_check = str(db.execute(text("PRAGMA quick_check")).scalar() or "unknown")
    except Exception as exc:
        quick_check = f"error: {exc}"
    table_stats = {}
    for table in preview["tables"]:
        try:
            quoted = '"' + table.replace('"', '""') + '"'
            table_stats[table] = int(db.execute(text(f"SELECT COUNT(*) FROM {quoted}")).scalar() or 0)
        except Exception:
            table_stats[table] = "error"
    payload = {
        "manifest": preview,
        "system": {
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "version": get_settings().version,
        },
        "database": {"quick_check": quick_check, "table_stats": table_stats},
        "healthchecks": latest_healthchecks(db, limit=5),
        "offline_mode": offline_status(db),
        "devices": [_device_summary(device, anonymize=anonymize) for device in db.query(Device).order_by(Device.device_id).all()],
        "settings": _redact({row.key: row.value for row in db.query(Setting).order_by(Setting.key).all()}),
        "runtime_state_keys": [row.key for row in db.query(RuntimeState).order_by(RuntimeState.key).limit(500).all()],
        "recent_events": [
            {"event_id": row.event_id, "event_type": row.event_type, "timestamp": row.timestamp.isoformat() if row.timestamp else "", "severity": row.severity}
            for row in db.query(Event).order_by(Event.timestamp.desc()).limit(200).all()
        ],
        "recent_device_interactions": [
            {
                "event_id": row.event_id,
                "device_id": _hash_id(row.device_id) if anonymize else row.device_id,
                "endpoint": row.endpoint,
                "request_purpose": row.request_purpose,
                "result": row.result,
                "status_code": row.status_code,
                "duration_ms": row.duration_ms,
                "started_at": row.started_at.isoformat() if row.started_at else "",
                "error_class": row.error_class,
            }
            for row in db.query(DeviceInteraction).order_by(DeviceInteraction.started_at.desc()).limit(300).all()
        ],
    }
    entries = {
        "manifest.json": json.dumps(redact_payload(preview), ensure_ascii=False, indent=2, sort_keys=True),
        "diagnostic.json": json.dumps(_redact(payload), ensure_ascii=False, indent=2, sort_keys=True),
    }
    if include_logs:
        log_path = get_settings().data_dir / "logs" / "master.log"
        if log_path.exists():
            entries["logs/master.log.redacted.tail"] = _tail_lines(log_path, MAX_LOG_LINES)
    try:
        bundle = build_support_bundle(
            entries,
            max_bytes=max(1, int(getattr(get_settings(), "diagnostic_max_size_mb", 50))) * 1024 * 1024,
            metadata={"kind": "diagnostic-export", "diagnostic_id": diag_id},
        )
    except SupportBundleTooLarge:
        return {"ok": False, "error": "diagnostic export exceeds the configured size limit", "preview": preview}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".diagnostic-", dir=path.parent, delete=False) as handle:
            tmp_path = Path(handle.name)
            handle.write(bundle.read())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"ok": True, "path": str(path), "filename": path.name, "sha256": digest, "preview": preview}
