"""Explicit LAB patch selection. Saving a plan does NOT modify a radio."""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from basswiesn.app.db import get_db
from basswiesn.app.models import Device, Setting
from basswiesn.app.routers.shared import device_or_404
from basswiesn.app.services.protected_devices import is_device_access_protected, require_unprotected_device
from basswiesn.app.services.radio_patch_policy import catalogue, default_selection, setup_presentation_contract
from basswiesn.app.services.release_scope import require_lab

router = APIRouter(prefix="/api/lab/radio-patches", tags=["radio-patches"])
PREFIX = "radio_patch_selection:"
Choice = Literal["keep", "enable", "disable"]


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    display: Choice = "enable"
    bootlogo: Choice = "enable"
    battery: Choice = "keep"
    battery_kind: Literal["unknown", "original", "compatible_replacement"] = "unknown"
    revision: str


def lab_db(db: Session = Depends(get_db)) -> Session:
    require_lab(db)
    return db


def current(db: Session, device_id: str) -> tuple[str, dict[str, str]]:
    row = db.query(Setting).filter(Setting.key == PREFIX + device_id).one_or_none()
    raw = row.value if row else ""
    choice = json.loads(raw) if raw else default_selection()
    return raw, choice


@router.get("")
def index(response: Response, db: Session = Depends(lab_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    devices = [{"device_id": d.device_id, "name": d.name, "model": d.model}
               for d in db.query(Device).order_by(Device.name).all()
               if not is_device_access_protected(d.ip_address, d.device_id)]
    return {"patches": catalogue(), "devices": devices, "radio_contacted": False,
            "installation_available": False, "scope": "PRIVATE_LAB_SELECTION_AND_DOCUMENTATION",
            "setup_presentation": setup_presentation_contract()}


@router.get("/{device_id}/selection")
def selection(device_id: str, response: Response, db: Session = Depends(lab_db)) -> dict:
    device = device_or_404(db, device_id)
    require_unprotected_device(device, action="patch_selection_read", requester="radio_patches")
    response.headers["Cache-Control"] = "no-store"
    raw, choice = current(db, device.device_id)
    return {**choice, "revision": sha256(raw.encode()).hexdigest(), "status": "PLANNED",
            "installed": False, "radio_contacted": False}


@router.put("/{device_id}/selection")
def save_selection(device_id: str, body: Selection, db: Session = Depends(lab_db)) -> dict:
    device = device_or_404(db, device_id)
    require_unprotected_device(device, action="patch_selection_save", requester="radio_patches")
    if body.battery != "keep" and device.model != "SoundTouch Portable":
        raise HTTPException(409, detail={"code": "PORTABLE_REQUIRED"})
    if body.battery == "enable" and body.battery_kind != "compatible_replacement":
        raise HTTPException(409, detail={"code": "ORIGINAL_OR_UNKNOWN_BATTERY_DOES_NOT_NEED_PATCH"})
    raw, _ = current(db, device.device_id)
    if body.revision != sha256(raw.encode()).hexdigest():
        raise HTTPException(409, detail={"code": "PATCH_SELECTION_CHANGED"})
    value = json.dumps(body.model_dump(exclude={"revision"}), sort_keys=True)
    key = PREFIX + device.device_id
    try:
        if raw:
            changed = db.execute(update(Setting).where(Setting.key == key, Setting.value == raw).values(value=value))
            if changed.rowcount != 1:
                raise HTTPException(409, detail={"code": "PATCH_SELECTION_CHANGED"})
        else:
            db.add(Setting(key=key, value=value))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, detail={"code": "PATCH_SELECTION_CHANGED"}) from None
    return {"status": "PLANNED", "installed": False, "radio_contacted": False,
            "revision": sha256(value.encode()).hexdigest(),
            "next": "Exact-image inspection, private package, backup and manual hardware acceptance required."}
