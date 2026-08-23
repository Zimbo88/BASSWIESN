import json
from io import BytesIO
import ipaddress
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.models import (
    AirPlayReadinessState,
    Device,
    DeviceActionJournal,
    DeviceFirmwareProfile,
    Diagnostic,
    DiagnosticEvent,
    MetadataState,
    PlaybackHealthState,
    ProviderHealthState,
    RecoveryOperation,
    ReportingState,
    RequestLog,
    RestrictionState,
    Setting,
    SetupRebuildJob,
)
from basswiesn.app.services.device_state import read_device_state
from basswiesn.app.config import get_settings
from basswiesn.app.services.support_export import SupportBundleTooLarge, build_support_bundle as build_deterministic_bundle, redact_payload, redact_text, tail_text


_IP_CANDIDATE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_SECRET_TEXT = re.compile(r"(?i)(token|credential|password|authorization)(\s*[=:/>]\s*)([^<\s,&\"]+)")
_SECRET_JSON_TEXT = re.compile(r'(?i)("?(?:token|credential|password|authorization|secret)"?\s*:\s*")([^"]+)(")')


def redact_support_text(value: str) -> str:
    return redact_text(value)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def research_diagnostics_snapshot(db: Session, *, device_id: str = "") -> dict[str, Any]:
    """Return redacted 2.0 contract state without performing any network I/O."""

    scoped = str(device_id or "").strip().upper()
    def rows(model, *, limit: int = 500):
        query = db.query(model)
        if scoped and hasattr(model, "device_id"):
            query = query.filter(model.device_id == scoped)
        if hasattr(model, "updated_at"):
            query = query.order_by(model.updated_at.desc())
        elif hasattr(model, "occurred_at"):
            query = query.order_by(model.occurred_at.desc())
        return query.limit(limit).all()

    return redact_payload({
        "generated_at": datetime.now(UTC).isoformat(),
        "device_id": scoped or None,
        "firmware_profiles": [
            {
                "profile_key": row.profile_key,
                "version": row.version,
                "build": row.build,
                "product_id": row.product_id,
                "variant": row.variant,
                "model": row.model,
                "platform": row.platform,
                "limitations": row.limitations,
                "observed_at": _iso(row.observed_at),
            }
            for row in rows(DeviceFirmwareProfile)
        ],
        "provider_health": [
            {"device_id": row.device_id, "provider_id": row.provider_id, "state": row.state,
             "cause": row.cause, "last_success_at": _iso(row.last_success_at), "updated_at": _iso(row.updated_at)}
            for row in rows(ProviderHealthState)
        ],
        "playback_health": [
            {"device_id": row.device_id, "state": row.state, "cause": row.reason,
             # PlaybackHealthState intentionally stores the source contract as
             # evidence instead of duplicating selection identity in another
             # column.  Older databases therefore have no ``source`` member.
             "source": getattr(row, "source", None), "source_valid": row.source_valid,
             "stream_alive": row.stream_alive, "position_advancing": row.position_advancing,
             "observed_at": _iso(row.observed_at), "updated_at": _iso(row.updated_at)}
            for row in rows(PlaybackHealthState)
        ],
        "reporting": [
            {"device_id": row.device_id, "provider_id": row.provider_id, "status": row.state,
             "queue_depth": row.queue_depth, "retry_count": row.retry_count,
             "due_at": _iso(row.next_due_at), "last_success_at": _iso(row.last_success_at),
             "last_error": row.last_failure_json, "updated_at": _iso(row.updated_at)}
            for row in rows(ReportingState)
        ],
        "restrictions": [
            {"device_id": row.device_id, "source_key": row.source_key,
             "inactivity_timeout_s": row.inactivity_timeout_s, "timer_enabled": row.timer_enabled,
             "timer_started_at": _iso(row.timer_started_at), "effective_until": _iso(row.effective_until),
             "received_at": _iso(row.received_at), "origin": row.origin}
            for row in rows(RestrictionState)
        ],
        "metadata": [
            {"device_id": row.device_id, "station_name": row.station_name, "track": row.track,
             "artist": row.artist, "album": row.album, "artwork_url": row.artwork_url,
             "provider": row.provider, "source": row.source, "provenance": row.provenance,
             "confidence": row.confidence, "stale": row.stale, "updated_at": _iso(row.updated_at)}
            for row in rows(MetadataState)
        ],
        "airplay_readiness": [
            {"device_id": row.device_id, "firmware_version": row.firmware_version,
             "firmware_build": row.firmware_build, "product_id": row.product_id,
             "variant": row.variant, "platform": row.platform, "source_visible": row.source_visible,
             "mdns_visible": row.mdns_visible, "pairing_ready": row.pairing_ready,
             "ptp_ready": row.ptp_ready, "audio_ready": row.audio_ready,
             "blocking_stage": row.blocking_stage, "confidence": row.confidence,
             "observed_at": _iso(row.observed_at), "updated_at": _iso(row.updated_at)}
            for row in rows(AirPlayReadinessState)
        ],
        "timeline": [
            {"event_id": row.event_id, "device_id": row.device_id, "domain": row.domain,
             "code": row.code, "severity": row.severity, "message": row.message,
             "occurred_at": _iso(row.occurred_at), "correlation_id": row.correlation_id}
            for row in rows(DiagnosticEvent, limit=1000)
        ],
        "recovery": [
            {"operation_id": row.operation_id, "device_id": row.device_id, "status": row.status,
             "stage": row.stage, "trigger_domain": row.trigger_domain, "reason": row.reason,
             "manual_required": row.manual_required, "started_at": _iso(row.started_at),
             "completed_at": _iso(row.completed_at)}
            for row in rows(RecoveryOperation)
        ],
        "setup_jobs": [
            {"job_id": row.job_id, "status": row.status, "current_device_id": row.current_device_id,
             "current_state": row.current_state, "progress": row.progress, "error": row.error,
             "started_at": _iso(row.started_at), "ended_at": _iso(row.ended_at)}
            for row in rows(SetupRebuildJob)
        ],
        "write_ledger": [
            {"timestamp": _iso(row.ts), "job_id": row.job_id, "device_id": row.device_id,
             "action": row.action, "trigger": row.trigger, "phase": row.phase,
             "backup_ref": getattr(row, "backup_ref", ""), "result": row.result,
             "readback": getattr(row, "readback", "{}"),
             "rollback_ref": getattr(row, "rollback_ref", ""), "verified": row.verified,
             "error_category": row.error_category}
            for row in rows(DeviceActionJournal, limit=1000)
        ],
    })


async def build_support_bundle(device: Device, db: Session) -> BytesIO:
    client = SoundTouchClient(device.ip_address)
    endpoint_files = {
        "info.xml": "/info", "capabilities.xml": "/capabilities", "sources.xml": "/sources",
        "presets.xml": "/presets", "now_playing.xml": "/now_playing", "volume.xml": "/volume",
        "bass.xml": "/bass", "marge.xml": "/marge", "serviceAvailability.xml": "/serviceAvailability",
    }
    payloads: dict[str, str] = {}
    for filename, endpoint in endpoint_files.items():
        try:
            payloads[filename] = await client.get_xml(endpoint)
        except Exception as exc:
            fallback = device.info_xml if filename == "info.xml" else device.capabilities_xml if filename == "capabilities.xml" else ""
            payloads[filename] = fallback or f'<error endpoint="{endpoint}">{exc}</error>'

    state = await read_device_state(device, db)
    rows = db.query(Setting).order_by(Setting.key).all()
    config = {row.key: row.value for row in rows if not any(secret in row.key.lower() for secret in ("token", "password", "secret", "credential"))}
    config["device"] = {"device_id": device.device_id, "name": device.name, "model": device.model, "firmware": device.firmware, "ip_address": device.ip_address}
    requests = db.query(RequestLog).order_by(desc(RequestLog.ts)).limit(500).all()
    request_log = "\n".join(f"{row.ts.isoformat()} {row.service} {row.method} {row.host} {row.path} {row.status_code}" for row in requests)
    masterlog_path = get_settings().data_dir / "logs" / "master.log"
    masterlog_tail = tail_text(masterlog_path, max_lines=500)

    entries = {filename: redact_support_text(content) for filename, content in payloads.items()}
    entries.update({
        "runtime_state.json": redact_support_text(json.dumps(state["runtime_state"], indent=2, ensure_ascii=False)),
        "provider_state.json": redact_support_text(json.dumps(state["runtime_state"]["provider_state"], indent=2, ensure_ascii=False)),
        "sanitized_config.json": redact_support_text(json.dumps(config, indent=2, ensure_ascii=False)),
        "request_log.txt": redact_support_text(request_log),
        "masterlog_tail.txt": redact_support_text(masterlog_tail),
        "research_diagnostics.json": redact_support_text(
            json.dumps(research_diagnostics_snapshot(db, device_id=device.device_id), indent=2, ensure_ascii=False)
        ),
    })
    return build_deterministic_bundle(
        entries,
        max_bytes=max(1, int(getattr(get_settings(), "support_bundle_max_mb", 50))) * 1024 * 1024,
        metadata={"device_id": device.device_id, "kind": "device-support"},
    )


def log_request(
    db: Session,
    *,
    direction: str,
    service: str,
    method: str,
    path: str,
    host: str = "",
    status_code: int = 0,
    body: bytes | str = b"",
) -> None:
    if isinstance(body, bytes):
        body_text = body[:4096].decode("utf-8", errors="replace")
    else:
        body_text = body[:4096]
    db.add(
        RequestLog(
            direction=direction,
            service=service,
            method=method,
            path=path,
            host=host,
            status_code=status_code,
            body=body_text,
        )
    )
    db.commit()


def diagnostics_snapshot(db: Session) -> dict[str, Any]:
    requests = db.query(RequestLog).order_by(desc(RequestLog.ts)).limit(100).all()
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "request_count": db.query(RequestLog).count(),
        "recent_requests": [
            {
                "ts": item.ts.isoformat() + "Z",
                "direction": item.direction,
                "service": item.service,
                "method": item.method,
                "path": item.path,
                "host": item.host,
                "status_code": item.status_code,
            }
            for item in requests
        ],
    }
    db.add(Diagnostic(payload=json.dumps(payload, indent=2)))
    db.commit()
    return payload
