from __future__ import annotations

from datetime import UTC, datetime
import json
from uuid import uuid4

from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import AnnouncementJob, Device
from basswiesn.app.services.device_policy import policy_for_device
from basswiesn.app.services.events import create_event


MAX_TTS_TEXT_LENGTH = 300


def announcements_status() -> dict:
    return {
        "enabled": get_settings().experimental_announcements,
        "experimental": True,
        "cloud_required": False,
        "background_services_started": False,
        "text_limit": MAX_TTS_TEXT_LENGTH,
    }


def preview_announcement(db: Session, payload: dict) -> dict:
    text = str(payload.get("text") or "")
    if len(text) > MAX_TTS_TEXT_LENGTH:
        raise ValueError("announcement text too long")
    device = db.query(Device).filter(Device.device_id == str(payload.get("device_id") or "")).one_or_none()
    policy = policy_for_device(device, db).to_dict() if device is not None else {}
    return {
        "enabled": get_settings().experimental_announcements,
        "experimental": True,
        "device_id": getattr(device, "device_id", ""),
        "text_preview": text[:80],
        "language": str(payload.get("language") or "de"),
        "volume": min(max(int(payload.get("volume") or 20), 0), int(payload.get("max_volume") or 30)),
        "max_volume": min(max(int(payload.get("max_volume") or 30), 0), 100),
        "device_policy": policy,
        "will_power_device": False,
        "will_restore_state_only_after_readback": True,
        "cloud_tts_required": False,
    }


def create_announcement_job(db: Session, payload: dict, *, confirmation: str = "") -> dict:
    preview = preview_announcement(db, payload)
    if not get_settings().experimental_announcements:
        return {"ok": False, "preview": preview, "reason": "BASSWIESN_EXPERIMENTAL_ANNOUNCEMENTS=false"}
    if confirmation != "BASSWIESN ANNOUNCEMENT":
        return {"ok": False, "preview": preview, "confirmation_required": "BASSWIESN ANNOUNCEMENT"}
    job_id = str(uuid4())
    job = AnnouncementJob(
        job_id=job_id,
        device_id=preview["device_id"],
        status="planned",
        text_preview=preview["text_preview"],
        language=preview["language"],
        volume=preview["volume"],
        max_volume=preview["max_volume"],
        previous_state_json="{}",
        result_json=json.dumps({"manual_only": True}, ensure_ascii=False),
        created_at=datetime.now(UTC),
    )
    db.add(job)
    create_event(db, "announcement_started", device_id=job.device_id, payload={"job_id": job_id, "planned_only": True})
    return {"ok": True, "job_id": job_id, "preview": preview, "playback_started": False, "manual_only": True}
