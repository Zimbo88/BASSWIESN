from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import RuntimeState, Setting


MODES = {"off", "auto", "strict"}


@dataclass(frozen=True)
class ExternalDecision:
    allowed: bool
    mode: str
    service: str
    target_host: str
    reason: str
    required: bool
    strict_allowed: bool

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "mode": self.mode,
            "service": self.service,
            "target_host": self.target_host,
            "reason": self.reason,
            "required": self.required,
            "strict_allowed": self.strict_allowed,
        }


def _setting(db: Session, key: str, fallback: str = "") -> str:
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    return row.value if row is not None and row.value != "" else fallback


def offline_mode(db: Session) -> str:
    configured = _setting(db, "offline_mode", get_settings().offline_mode).strip().lower()
    return configured if configured in MODES else "auto"


def allowed_stream_hosts(db: Session) -> set[str]:
    configured = _setting(db, "offline_allowed_stream_hosts", ",".join(get_settings().offline_allowed_stream_hosts))
    return {item.strip().lower() for item in configured.split(",") if item.strip()}


def target_host(url_or_host: str) -> str:
    value = (url_or_host or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"http://{value}")
    return (parsed.hostname or value).strip().lower()


def external_request_decision(
    db: Session,
    *,
    service: str,
    url_or_host: str,
    reason: str,
    required: bool = False,
    stream_target: bool = False,
    manual_action: bool = False,
) -> ExternalDecision:
    mode = offline_mode(db)
    host = target_host(url_or_host)
    strict_allowed = bool(stream_target and host and host in allowed_stream_hosts(db))
    allowed = True
    if mode == "strict":
        allowed = bool(strict_allowed or (manual_action and required))
    return ExternalDecision(
        allowed=allowed,
        mode=mode,
        service=service,
        target_host=host,
        reason=reason,
        required=required,
        strict_allowed=strict_allowed,
    )


def record_dependency(db: Session, decision: ExternalDecision) -> None:
    key = f"external_dependency:{decision.service}:{decision.target_host or 'unknown'}"
    row = db.query(RuntimeState).filter(RuntimeState.key == key).one_or_none()
    if row is None:
        row = RuntimeState(key=key)
        db.add(row)
    row.value = json.dumps(decision.to_dict(), ensure_ascii=False)
    row.updated_at = datetime.now(UTC)


def dependency_overview(db: Session) -> list[dict]:
    rows = db.query(RuntimeState).filter(RuntimeState.key.like("external_dependency:%")).order_by(RuntimeState.updated_at.desc()).all()
    result = []
    for row in rows:
        parts = row.key.split(":", 2)
        service = parts[1] if len(parts) > 1 else ""
        host = parts[2] if len(parts) > 2 else ""
        result.append({
            "service": service,
            "target_host": host,
            "reason": "",
            "last_used_at": row.updated_at.isoformat() if row.updated_at else "",
            "required": False,
            "strict_offline_allowed": host in allowed_stream_hosts(db),
            "status": "recorded",
        })
    configured = [
        {
            "service": "radio_browser",
            "target_host": "*.api.radio-browser.info",
            "reason": "optionale Online-Sendersuche",
            "last_used_at": "",
            "required": False,
            "strict_offline_allowed": False,
            "status": "optional",
        },
        {
            "service": "update_check",
            "target_host": target_host(_setting(db, "update_manifest_url", get_settings().update_manifest_url)),
            "reason": "manuelle Release-Manifest-Pruefung",
            "last_used_at": "",
            "required": False,
            "strict_offline_allowed": False,
            "status": "manual_only",
        },
    ]
    return configured + result


def offline_status(db: Session) -> dict:
    mode = offline_mode(db)
    dependencies = dependency_overview(db)
    blocked = [item for item in dependencies if mode == "strict" and not item["strict_offline_allowed"] and item["status"] != "manual_only"]
    return {
        "mode": mode,
        "local_core_available": True,
        "status": "vollstaendig lokal" if mode == "strict" and not blocked else "teilweise extern" if mode != "strict" else "lokal mit blockierten optionalen Diensten",
        "allowed_stream_hosts": sorted(allowed_stream_hosts(db)),
        "dependencies": dependencies,
        "blocked_optional_dependencies": blocked,
    }
