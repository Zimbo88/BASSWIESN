from __future__ import annotations

from datetime import UTC, datetime
import json
import re
from typing import Any
from uuid import uuid4
import xml.etree.ElementTree as ET

from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import Device, StandbyClockJob, utc_now
from basswiesn.app.services.device_interactions import InteractionPriority, coordinator
from basswiesn.app.services.events import create_event
from basswiesn.app.services.model_library import resolve_device_model
from basswiesn.app.services.protected_devices import is_protected_device, require_unprotected_device


STANDBY_CLOCK_CONFIRMATION = "BASSWIESN STANDBY CLOCK"


def _parse_clock_enabled(xml_text: str) -> bool | None:
    if not xml_text or len(xml_text) > 64_000:
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    config = root.find(".//clockConfig")
    if config is None:
        return None
    raw = (config.attrib.get("userEnable") or config.findtext("userEnable") or "").strip().lower()
    if raw in {"true", "1", "yes", "on"}:
        return True
    if raw in {"false", "0", "no", "off"}:
        return False
    return None


def _safe_timezone(value: str) -> str:
    text = str(value or "Europe/Berlin").strip()
    if not re.fullmatch(r"[A-Za-z0-9_./+-]{1,80}", text):
        return "Europe/Berlin"
    return text


def standby_clock_status(db: Session, device: Device) -> dict[str, Any]:
    model = resolve_device_model(device, db)
    capabilities = model.capabilities
    display_known = capabilities.get("display_supported") is True or capabilities.get("clock_display") is True
    enabled = get_settings().standby_clock_recovery_enabled
    protected = is_protected_device(device)
    supported = bool(enabled and display_known and not protected)
    return {
        "device_id": device.device_id,
        "device_name": device.name,
        "ip_address": device.ip_address,
        "model": device.model,
        "firmware": device.firmware,
        "enabled": enabled,
        "supported": supported,
        "status": "unknown" if supported else "unsupported",
        "model_resolution": model.to_dict(),
        "requires_confirmation": STANDBY_CLOCK_CONFIRMATION,
        "protected": protected,
        "reason": "" if supported else (
            "device is protected by PROTECTED_DEVICE_IPS"
            if protected
            else "BASSWIESN_STANDBY_CLOCK_RECOVERY_ENABLED=false"
            if not enabled
            else "model has no confirmed display/clock capability"
        ),
        "warning": "Read-back entscheidet ueber Erfolg; falls Firmware den Status nicht bestaetigt, ist manuelle Sichtpruefung erforderlich.",
    }


def _job_to_dict(job: StandbyClockJob) -> dict[str, Any]:
    def load(text: str) -> dict[str, Any]:
        try:
            value = json.loads(text or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    return {
        "job_id": job.job_id,
        "device_id": job.device_id,
        "status": job.status,
        "profile_key": job.profile_key,
        "command_key": job.command_key,
        "correlation_id": job.correlation_id,
        "before_state": load(job.before_state_json),
        "readback": load(job.readback_json),
        "result": load(job.result_json),
        "error": job.error,
        "started_at": job.started_at.isoformat() if job.started_at else "",
        "updated_at": job.updated_at.isoformat() if job.updated_at else "",
        "finished_at": job.finished_at.isoformat() if job.finished_at else "",
    }


async def restore_standby_clock(db: Session, device: Device, *, confirmation: str, timezone: str = "Europe/Berlin") -> dict[str, Any]:
    if str(confirmation or "").strip() != STANDBY_CLOCK_CONFIRMATION:
        raise PermissionError(f"confirmation required: {STANDBY_CLOCK_CONFIRMATION}")
    require_unprotected_device(device, action="standby_clock_restore", requester="standby_clock_recovery", method="POST", endpoint="/clockDisplay")
    status = standby_clock_status(db, device)
    if not status["supported"]:
        raise PermissionError(status["reason"] or "standby clock recovery unsupported")
    active = (
        db.query(StandbyClockJob)
        .filter(StandbyClockJob.device_id == device.device_id, StandbyClockJob.status.in_(("pending", "restoring")))
        .one_or_none()
    )
    if active is not None:
        raise PermissionError("standby clock recovery job is already active for this device")

    correlation_id = str(uuid4())
    job = StandbyClockJob(
        job_id=str(uuid4()),
        device_id=device.device_id,
        status="restoring",
        profile_key="http_clock_display",
        command_key="clockDisplay.userEnable",
        correlation_id=correlation_id,
    )
    db.add(job)
    db.flush()
    create_event(db, "standby_clock_restore_started", device_id=device.device_id, correlation_id=correlation_id, payload={"job_id": job.job_id})
    before = await coordinator.request_xml(
        db,
        device,
        "/clockDisplay",
        request_purpose="standby_clock_status",
        requester="standby_clock_recovery",
        priority=InteractionPriority.USER_ACTION,
        timeout_seconds=5,
        retry_budget=0,
        cache_ttl_seconds=0,
    )
    before_enabled = _parse_clock_enabled(before.payload) if before.ok else None
    job.before_state_json = json.dumps({"ok": before.ok, "enabled": before_enabled, "error": before.error}, ensure_ascii=False)
    clock_xml = (
        '<clockDisplay><clockConfig '
        f'timezoneInfo="{_safe_timezone(timezone)}" '
        'userEnable="true" timeFormat="TIME_FORMAT_24HOUR_ID" '
        'userOffsetMinute="0" brightnessLevel="7" userUtcTime="0" />'
        '</clockDisplay>'
    )
    write = await coordinator.request_xml(
        db,
        device,
        "/clockDisplay",
        method="POST",
        body=clock_xml,
        request_purpose="standby_clock_restore",
        requester="standby_clock_recovery",
        priority=InteractionPriority.USER_ACTION,
        timeout_seconds=5,
        retry_budget=0,
        cache_ttl_seconds=0,
        correlation_id=correlation_id,
    )
    if not write.ok:
        job.status = "failed"
        job.error = write.error or write.error_class or "clockDisplay write failed"
        job.finished_at = utc_now()
        job.updated_at = utc_now()
        job.result_json = json.dumps({"write_ok": False}, ensure_ascii=False)
        create_event(db, "standby_clock_restore_failed", device_id=device.device_id, correlation_id=correlation_id, severity="warning", payload={"job_id": job.job_id, "error": job.error})
        return _job_to_dict(job)

    readback = await coordinator.request_xml(
        db,
        device,
        "/clockDisplay",
        request_purpose="standby_clock_readback",
        requester="standby_clock_recovery",
        priority=InteractionPriority.USER_ACTION,
        timeout_seconds=5,
        retry_budget=0,
        cache_ttl_seconds=0,
        correlation_id=correlation_id,
    )
    readback_enabled = _parse_clock_enabled(readback.payload) if readback.ok else None
    job.readback_json = json.dumps({"ok": readback.ok, "enabled": readback_enabled, "error": readback.error}, ensure_ascii=False)
    if readback_enabled is True:
        job.status = "restored"
        event_type = "standby_clock_restore_completed"
    elif readback.ok:
        job.status = "manual_verification_required"
        event_type = "standby_clock_restore_completed"
    else:
        job.status = "failed"
        event_type = "standby_clock_restore_failed"
        job.error = readback.error or readback.error_class or "clockDisplay read-back failed"
    job.result_json = json.dumps(
        {
            "write_ok": True,
            "readback_ok": readback.ok,
            "readback_enabled": readback_enabled,
            "manual_verification_required": readback_enabled is not True,
        },
        ensure_ascii=False,
    )
    job.finished_at = utc_now()
    job.updated_at = utc_now()
    create_event(db, event_type, device_id=device.device_id, correlation_id=correlation_id, severity="warning" if job.status == "failed" else "info", payload={"job_id": job.job_id, "status": job.status})
    return _job_to_dict(job)


def get_standby_clock_job(db: Session, job_id: str) -> dict[str, Any]:
    job = db.query(StandbyClockJob).filter(StandbyClockJob.job_id == job_id).one_or_none()
    if job is None:
        raise ValueError("standby clock job not found")
    return _job_to_dict(job)
