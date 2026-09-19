"""Explicit reboot selections: GETs never contact a radio."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StrictBool, StrictInt, model_validator
from sqlalchemy.orm import Session

from basswiesn.app.db import get_db
from basswiesn.app.models import Device, Setting, RuntimeState
from basswiesn.app.services.protected_devices import is_device_access_protected
from basswiesn.app.services.radio_reboots import (
    JOB_PREFIX, MANUAL_CONFIRMATION, SCHEDULE_CONFIRMATION, SCHEDULE_KEY,
    put_value, read_value, resolve_targets, schedule_default,
)

router = APIRouter(tags=["radio-reboots"])


class Selection(BaseModel):
    model_config = {"extra": "forbid"}
    device_ids: list[str] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.device_ids)) != len(self.device_ids) or any(not value.strip() or len(value) > 128 for value in self.device_ids):
            raise ValueError("Distinct nonempty device IDs required")
        return self


class Start(Selection):
    confirmation: str = Field(max_length=80)
    allow_interrupt: StrictBool = False


class Schedule(BaseModel):
    model_config = {"extra": "forbid"}
    enabled: StrictBool = False
    device_ids: list[str] = Field(default_factory=list, max_length=16)
    time: str = Field(default="04:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[StrictInt] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)
    timezone: str = Field(default="Europe/Berlin", max_length=80)
    skip_active: StrictBool = True
    confirmation: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def valid_schedule(self):
        if self.skip_active is not True:
            raise ValueError("Scheduled reboots must skip active radios")
        if len(set(self.weekdays)) != len(self.weekdays) or not set(self.weekdays) <= set(range(7)):
            raise ValueError("Weekdays must be distinct integers0–6")
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Unknown timezone") from exc
        if self.enabled and (not self.device_ids or self.confirmation != SCHEDULE_CONFIRMATION):
            raise ValueError("Explicit schedule confirmation and radios required")
        if len(set(self.device_ids)) != len(self.device_ids):
            raise ValueError("Duplicate radios")
        return self


def checked_targets(db, ids):
    try:
        return resolve_targets(db, ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/reboots", include_in_schema=False)
def reboot_page():
    return FileResponse(Path(__file__).resolve().parents[1] / "static" / "reboots.html", media_type="text/html")


@router.get("/api/radio-reboots")
def configuration(db: Session = Depends(get_db)):
    # Do not resolve hostnames or probe ports here. Radio follow-ups occur only
    # after an explicit confirmed job or an enabled, due schedule.
    radios = [{"device_id": row.device_id, "name": row.name, "reachable": row.reachable}
              for row in db.query(Device).order_by(Device.name).all()
              if not is_device_access_protected(row.ip_address, row.device_id)]
    latest = db.query(RuntimeState).filter(RuntimeState.key.startswith(JOB_PREFIX)).order_by(RuntimeState.updated_at.desc()).first()
    return {"devices": radios, "schedule": read_value(db, SCHEDULE_KEY, Setting) or schedule_default(),
            "latest_job_id": latest.key[len(JOB_PREFIX):] if latest else None,
            "manual_confirmation": MANUAL_CONFIRMATION, "schedule_confirmation": SCHEDULE_CONFIRMATION,
            "maximum_parallel_commands": 4, "radio_requests": 0}


@router.post("/api/radio-reboots/preview")
def preview(payload: Selection, db: Session = Depends(get_db)):
    targets = checked_targets(db, payload.device_ids)
    return {"devices": [{"device_id": item.device_id, "name": item.name} for item in targets],
            "confirmation": MANUAL_CONFIRMATION, "backup_required": True, "identity_check_required": True,
            "stops_playback": True, "factory_reset": False, "active_zone_policy": "BLOCK",
            "resume_playback": False, "volume_commands": 0, "radio_requests": 0}


@router.post("/api/radio-reboots/start", status_code=202)
async def start(payload: Start, request: Request, db: Session = Depends(get_db)):
    if payload.confirmation != MANUAL_CONFIRMATION:
        raise HTTPException(status_code=409, detail="Explicit restart confirmation required")
    checked_targets(db, payload.device_ids)
    manager = getattr(request.app.state, "radio_reboots", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Reboot service unavailable")
    try:
        return manager.start(payload.device_ids, allow_interrupt=payload.allow_interrupt)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/radio-reboots/jobs/{job_id}")
def status(job_id: str, db: Session = Depends(get_db)):
    if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
        raise HTTPException(status_code=404, detail="Unknown reboot job")
    value = read_value(db, JOB_PREFIX + job_id)
    if value is None:
        raise HTTPException(status_code=404, detail="Unknown reboot job")
    return value


@router.put("/api/radio-reboots/schedule")
def schedule(payload: Schedule, db: Session = Depends(get_db)):
    if payload.enabled:
        checked_targets(db, payload.device_ids)
    value = payload.model_dump(exclude={"confirmation"})
    value["updated_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
    put_value(db, SCHEDULE_KEY, value, Setting)
    db.commit()
    return {"schedule": read_value(db, SCHEDULE_KEY, Setting), "radio_requests": 0,
            "missed_time_policy": "SKIP", "active_radio_policy": "SKIP_BATCH"}
