"""Append-only, secret-safe ledger for every radio write attempt."""

import json
from typing import Any

from sqlalchemy.orm import Session

from basswiesn.app.core.masterlog import _sanitize
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.models import Device, DeviceActionJournal
from basswiesn.app.services.support_export import redact_payload


def _bounded(value: object, limit: int = 4096) -> str:
    # Key-only sanitising is insufficient when callers pass XML in a generic
    # ``xml`` field. Apply the shared structural/text redactor before the
    # bounded append-only value reaches disk.
    safe = redact_payload(_sanitize(value), anonymize_ips=False)
    return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))[:limit]


def record_action(db: Session, *, job_id: str, device_id: str, ip_address: str, action: str, trigger: str, phase: str, requested_state: object = None, backup_ref: str = "", before_state: object = None, result: str = "", readback: object = None, rollback_ref: str = "", after_state: object = None, duration_ms: int = 0, error_category: str = "", verified: bool = False) -> DeviceActionJournal:
    row = DeviceActionJournal(job_id=job_id[:80], device_id=device_id[:128], ip_address=ip_address[:64], action=action[:64], trigger=trigger[:32], phase=phase[:64], requested_state=_bounded(requested_state or {}), backup_ref=backup_ref[:512], before_state=_bounded(before_state or {}), result=result[:512], readback=_bounded(readback or {}), rollback_ref=rollback_ref[:512], after_state=_bounded(after_state or {}), duration_ms=max(0, duration_ms), error_category=error_category[:64], verified=verified)
    db.add(row)
    db.flush()
    return row


def record_transport_attempt(
    *,
    ip_address: str,
    device_id: str = "",
    action: str,
    trigger: str,
    phase: str = "transport_write",
    requested_state: Any = None,
    result: str,
    duration_ms: int = 0,
    error_category: str = "",
    job_id: str = "",
    backup_ref: str = "",
    readback: Any = None,
    rollback_ref: str = "",
    verified: bool = False,
) -> None:
    """Best-effort central ledger entry for transports without a request DB.

    The radio action must never be retried merely because the audit database
    is unavailable. Callers therefore record exactly once after each attempt
    and this helper contains all persistence failures.
    """

    try:
        from basswiesn.app import db as app_db

        db = app_db.SessionLocal()
        try:
            normalized_id = str(device_id or "").strip().upper()
            if not normalized_id:
                device = (
                    db.query(Device)
                    .filter(Device.ip_address == str(ip_address or "").strip())
                    .one_or_none()
                )
                normalized_id = str(
                    device.device_id if device is not None else ""
                ).strip().upper()
            record_action(
                db,
                job_id=job_id,
                device_id=normalized_id,
                ip_address=str(ip_address or ""),
                action=action,
                trigger=trigger,
                phase=phase,
                requested_state=requested_state,
                result=result,
                duration_ms=duration_ms,
                error_category=error_category,
                backup_ref=backup_ref,
                readback=readback,
                rollback_ref=rollback_ref,
                verified=verified,
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        write_masterlog(
            "write_ledger_failed",
            device_id=device_id,
            radio_ip=ip_address,
            action=action,
            error_type=exc.__class__.__name__,
        )
