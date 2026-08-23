from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from basswiesn.app.models import Device, Preset
from basswiesn.app.services.device_policy import policy_for_device


def preset_snapshot(db: Session, device_id: str) -> list[dict[str, Any]]:
    rows = db.query(Preset).filter(Preset.device_id == device_id).order_by(Preset.button).all()
    return [
        {
            "button": row.button,
            "station_id": row.station_id,
            "source": row.source,
            "source_account": row.source_account,
            "location": row.location,
            "content_item_xml": row.content_item_xml,
            "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        }
        for row in rows
    ]


def build_preset_write_plan(db: Session, device_id: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
    device = db.query(Device).filter(Device.device_id == device_id).one_or_none()
    if device is None:
        raise ValueError("device not found")
    policy = policy_for_device(device, db)
    before = preset_snapshot(db, device_id)
    warnings = []
    for change in changes:
        if not change.get("name") and not change.get("station_id"):
            warnings.append({"button": change.get("button"), "warning": "empty station name or station_id"})
        location = str(change.get("location") or "")
        if location.startswith("basswiesn-internal:") or "activation" in location.lower():
            warnings.append({"button": change.get("button"), "warning": "internal or activation stream"})
    return {
        "device_id": device_id,
        "device_policy": policy.to_dict(),
        "before_snapshot": before,
        "requested_change": changes,
        "after_snapshot": None,
        "readback_required": True,
        "will_power_device": False,
        "will_auto_restore": False,
        "rollback_possible": True,
        "warnings": warnings,
        "plan_json": json.dumps({"before": before, "changes": changes}, ensure_ascii=False),
    }
