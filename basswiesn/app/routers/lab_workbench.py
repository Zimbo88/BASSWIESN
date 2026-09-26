"""LAB-only convenience tools; opening the workbench never probes hardware."""
from datetime import UTC, datetime
from io import BytesIO
import hashlib
import json
import re
from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
import qrcode
from qrcode.image.svg import SvgPathImage
from sqlalchemy.orm import Session

from basswiesn.app.db import get_db
from basswiesn.app.models import Device, PlayHistory, RuntimeState, Station
from basswiesn.app.routers.shared import device_or_404
from basswiesn.app.services import lab_workbench as workbench
from basswiesn.app.services.protected_devices import is_device_access_protected, require_unprotected_device
from basswiesn.app.services.release_scope import require_lab


def lab_db(db: Session = Depends(get_db)):
    require_lab(db)
    return db


router = APIRouter(prefix="/api/lab/workbench", tags=["lab-workbench"])
PREFIX = "lab:workbench:snapshot:"


def target(db, identity):
    device = device_or_404(db, identity)
    require_unprotected_device(device, action="lab_workbench", requester="lab_workbench")
    return device


def snapshot_prefix(identity):
    return PREFIX + hashlib.sha256(identity.encode()).hexdigest() + ":"


class CaptureBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="", max_length=60)


class ReplayBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: StrictBool
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    safe_volume: Literal[1] = 1


class QRBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str = Field(max_length=240)


@router.get("/devices")
def devices(db: Session = Depends(lab_db)):
    rows = db.query(Device).order_by(Device.name, Device.id).limit(200).all()
    return {"devices": [{"id": r.device_id, "name": workbench.text(r.name), "model": workbench.text(r.model)}
                        for r in rows if not is_device_access_protected(r.ip_address, r.device_id)],
            "radio_contacted": False}


@router.get("/formats")
def formats(db: Session = Depends(lab_db)):
    return workbench.format_inventory(db)


@router.get("/devices/{device_id}/overview")
def overview(device_id: str, hours: int = Query(default=24, ge=1, le=168), db: Session = Depends(lab_db)):
    device = target(db, device_id)
    now = datetime.now(UTC)
    snapshot = workbench.cached_snapshot(db, device, now=now)
    return {"snapshot": snapshot, "diagnosis": workbench.diagnose(snapshot),
            "timeline": workbench.timeline(db, device_id, now, hours),
            "recent": workbench.recent_stations(db, device_id, now)}


@router.post("/devices/{device_id}/recent/{history_id}/play")
async def replay(device_id: str, history_id: int, body: ReplayBody, request: Request, db: Session = Depends(lab_db)):
    target(db, device_id)
    if not body.approve:
        raise HTTPException(409, detail={"code": "CONFIRMATION_REQUIRED"})
    row = workbench.history_query(db, device_id).filter(PlayHistory.id == history_id).one_or_none()
    if row and workbench.aware(row.started_at) > datetime.now(UTC):
        row = None
    station = db.get(Station, row.station_id) if row and row.station_id else None
    if not station or station.internal or not station.stream_url or station.provider != "LOCAL_INTERNET_RADIO":
        raise HTTPException(409, detail={"code": "STATION_UNAVAILABLE"})
    if workbench.station_revision(station) != body.revision:
        raise HTTPException(409, detail={"code": "STATION_CHANGED"})
    # Delegate to the established identity, policy, audio-lock, safety-volume
    # and readback path. Never replay an expiring URL from the history row.
    from basswiesn.app.routers.stations_presets import play_station_on_device
    return await play_station_on_device(device_id, station.id,
        {"safe_volume": 1, "trigger": "lab_recent"}, request, db)


@router.get("/devices/{device_id}/listening.csv")
def listening(device_id: str, days: int = Query(default=7, ge=1, le=365), db: Session = Depends(lab_db)):
    target(db, device_id)
    content, truncated = workbench.listening_csv(db, device_id, now=datetime.now(UTC), days=days)
    return Response("\ufeff" + content, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="basswiesn-listening.csv"',
        "Cache-Control": "no-store", "X-BASSWIESN-Truncated": str(truncated).lower()})


@router.post("/devices/{device_id}/qr")
def remote_qr(device_id: str, body: QRBody, db: Session = Depends(lab_db)):
    target(db, device_id)
    try:
        origin = urlsplit(body.origin)
        valid = (origin.scheme in {"http", "https"} and origin.hostname and origin.port != 0
                 and not origin.username and not origin.password and not origin.query and not origin.fragment
                 and origin.path in {"", "/"} and not re.search(r"[\s\\<>\"']", body.origin))
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(422, detail={"code": "INVALID_ORIGIN"})
    # User-supplied server origin is only encoded, never resolved or fetched.
    # The UI uses its own location.origin, so proxy prefixes are not supported.
    url = f"{origin.scheme}://{origin.netloc}/remote/{quote(device_id, safe='')}"
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    output = BytesIO()
    qr.make_image(image_factory=SvgPathImage).save(output)
    return {"url": url, "svg": output.getvalue().decode(), "radio_contacted": False,
            "contains_device_id": True, "contains_credentials": False}


@router.get("/devices/{device_id}/snapshots")
def snapshots(device_id: str, db: Session = Depends(lab_db)):
    target(db, device_id)
    rows = db.query(RuntimeState).filter(RuntimeState.key.startswith(snapshot_prefix(device_id))).order_by(RuntimeState.updated_at.desc()).limit(10).all()
    items = []
    for row in rows:
        try:
            record = json.loads(row.value)
            label, captured = workbench.text(record["label"], 60), record["snapshot"]["captured_at"]
        except (ValueError, KeyError, TypeError):
            label, captured = "", workbench.iso(row.updated_at)
        items.append({"id": row.key.rsplit(":", 1)[1], "label": label, "captured_at": captured})
    return {"items": items, "limit": 10}


@router.post("/devices/{device_id}/snapshots")
def capture(device_id: str, body: CaptureBody, db: Session = Depends(lab_db)):
    device = target(db, device_id)
    # SQLite write serialization makes the retention limit apply to concurrent
    # tabs/workers too. A no-op UPDATE takes the database write lock, no radio I/O.
    db.query(RuntimeState).filter(RuntimeState.key == "lab:workbench:lock").update({RuntimeState.value: ""})
    prefix = snapshot_prefix(device_id)
    if (db.query(RuntimeState).filter(RuntimeState.key.startswith(prefix)).count() >= 10
            or db.query(RuntimeState).filter(RuntimeState.key.startswith(PREFIX)).count() >= 200):
        raise HTTPException(409, detail={"code": "SNAPSHOT_LIMIT"})
    identity = uuid4().hex
    snapshot = workbench.cached_snapshot(db, device)
    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    record = {"label": workbench.text(body.label, 60), "snapshot": snapshot,
              "sha256": hashlib.sha256(serialized.encode()).hexdigest()}
    db.add(RuntimeState(key=prefix + identity, value=json.dumps(record, ensure_ascii=False)))
    db.commit()
    return {"id": identity, **record}


def saved_row(db, device_id, snapshot_id):
    target(db, device_id)
    if not re.fullmatch(r"[a-f0-9]{32}", snapshot_id):
        raise HTTPException(404, detail={"code": "SNAPSHOT_NOT_FOUND"})
    row = db.query(RuntimeState).filter_by(key=snapshot_prefix(device_id) + snapshot_id).one_or_none()
    if row is None:
        raise HTTPException(404, detail={"code": "SNAPSHOT_NOT_FOUND"})
    return row


def saved(db, device_id, snapshot_id):
    row = saved_row(db, device_id, snapshot_id)
    try:
        record = json.loads(row.value)
        serialized = json.dumps(record["snapshot"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(serialized.encode()).hexdigest() != record["sha256"]:
            raise ValueError("hash mismatch")
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, detail={"code": "SNAPSHOT_INTEGRITY"}) from None
    return row, record


@router.get("/devices/{device_id}/snapshots/{snapshot_id}/compare")
def compare(device_id: str, snapshot_id: str, db: Session = Depends(lab_db)):
    _, record = saved(db, device_id, snapshot_id)
    current = workbench.cached_snapshot(db, target(db, device_id))
    return {"before_at": record["snapshot"]["captured_at"], "after_at": current["captured_at"],
            **workbench.compare_snapshots(record["snapshot"], current)}


@router.get("/devices/{device_id}/snapshots/{snapshot_id}/download")
def download(device_id: str, snapshot_id: str, db: Session = Depends(lab_db)):
    _, record = saved(db, device_id, snapshot_id)
    # Labels may be personal. Exports omit them and all device addresses/IDs.
    return Response(json.dumps({"snapshot": record["snapshot"], "sha256": record["sha256"]}, ensure_ascii=False, indent=2),
                    media_type="application/json", headers={"Content-Disposition": 'attachment; filename="basswiesn-cached-state.json"',
                                                            "Cache-Control": "no-store"})


@router.delete("/devices/{device_id}/snapshots/{snapshot_id}")
def remove(device_id: str, snapshot_id: str, db: Session = Depends(lab_db)):
    # A corrupt local snapshot must still be removable by explicit selection;
    # it must not consume a retention slot forever.
    row = saved_row(db, device_id, snapshot_id)
    db.delete(row)
    db.commit()
    return {"deleted": True, "radio_contacted": False}
