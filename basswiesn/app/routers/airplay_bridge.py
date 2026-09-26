"""Local inventory preview only; no enable/write route in the prototype."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from basswiesn.app.db import get_db
from basswiesn.app.models import Device
from basswiesn.app.services.airplay_bridge.planning import bridge_status


router = APIRouter(prefix="/api/airplay-bridge", tags=["airplay-bridge"])


@router.get("/status")
def status(db: Session = Depends(get_db)) -> dict:
    devices = db.query(Device).order_by(Device.name, Device.device_id).all()
    return bridge_status(devices)
