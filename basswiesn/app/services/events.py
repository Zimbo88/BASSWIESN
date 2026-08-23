from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import Event


EVENT_TYPES = {
    "device_discovered", "device_online", "device_offline", "device_ip_changed", "device_state_changed",
    "playback_started", "playback_stopped", "station_changed", "volume_changed", "source_changed",
    "preset_changed", "preset_write_failed", "multiroom_created",
    "multiroom_changed", "multiroom_removed", "circuit_breaker_opened", "circuit_breaker_half_open",
    "circuit_breaker_closed", "healthcheck_failed", "healthcheck_recovered", "backup_created",
    "restore_prepared", "restore_completed", "restore_failed", "update_available", "update_started",
    "update_completed", "update_failed", "setup_job_started", "setup_job_completed", "setup_job_failed",
    "announcement_started", "announcement_completed", "announcement_failed",
    "telnet_reboot_started", "telnet_reboot_completed", "telnet_reboot_failed",
    "standby_clock_restore_started", "standby_clock_restore_completed", "standby_clock_restore_failed",
}

SECRET_TOKENS = ("password", "token", "secret", "authorization", "cookie", "ssh", "private_key", "api_key")


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("***REDACTED***" if any(token in key.lower() for token in SECRET_TOKENS) else _redact_value(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value[:200]]
    if isinstance(value, str):
        text = value
        for token in SECRET_TOKENS:
            if token in text.lower():
                return "***REDACTED***"
        return text[:2000]
    return value


def create_event(
    db: Session,
    event_type: str,
    *,
    source: str = "basswiesn",
    device_id: str = "",
    correlation_id: str = "",
    severity: str = "info",
    payload: dict[str, Any] | None = None,
) -> Event:
    if event_type not in EVENT_TYPES:
        severity = "warning" if severity == "info" else severity
    event = Event(
        event_id=str(uuid4()),
        event_type=event_type,
        timestamp=datetime.now(UTC),
        source=source,
        device_id=device_id,
        correlation_id=correlation_id,
        severity=severity,
        payload_json=json.dumps(_redact_value(payload or {}), ensure_ascii=False),
        redaction="automatic",
        delivery_status="pending",
    )
    db.add(event)
    return event


def event_to_dict(event: Event) -> dict[str, Any]:
    try:
        payload = json.loads(event.payload_json or "{}")
    except ValueError:
        payload = {}
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "timestamp": event.timestamp.isoformat() if event.timestamp else "",
        "source": event.source,
        "device_id": event.device_id,
        "correlation_id": event.correlation_id,
        "severity": event.severity,
        "payload": payload,
        "redaction": event.redaction,
        "delivery_status": event.delivery_status,
    }


def list_events(
    db: Session,
    *,
    limit: int = 100,
    event_type: str = "",
    device_id: str = "",
    severity: str = "",
) -> list[dict[str, Any]]:
    query = db.query(Event)
    if event_type:
        query = query.filter(Event.event_type == event_type)
    if device_id:
        query = query.filter(Event.device_id == device_id)
    if severity:
        query = query.filter(Event.severity == severity)
    rows = query.order_by(Event.timestamp.desc()).limit(min(max(limit, 1), 500)).all()
    return [event_to_dict(row) for row in rows]


def apply_event_retention(db: Session) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=get_settings().event_retention_days)
    deleted = db.query(Event).filter(Event.timestamp < cutoff).delete(synchronize_session=False)
    return int(deleted or 0)
