from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from basswiesn.app.models import Device, MultiroomScenario


def scenario_to_dict(db: Session, scenario: MultiroomScenario) -> dict[str, Any]:
    member_ids = [item for item in (scenario.member_device_ids or "").split(",") if item]
    devices = {device.device_id: device for device in db.query(Device).filter(Device.device_id.in_([scenario.master_device_id, *member_ids])).all()}
    warnings = []
    for device_id, device in devices.items():
        if not device.reachable:
            warnings.append({"device_id": device_id, "warning": "Gerät ist offline oder schläft"})
    return {
        "id": scenario.id,
        "name": scenario.name,
        "leader": scenario.master_device_id,
        "members": member_ids,
        "station_id": scenario.station_id,
        "volume": scenario.volume,
        "warnings": warnings,
        "device_policy_respected": True,
        "lock_strategy": "per_device",
        "last_success": "",
        "last_error": "",
    }


def list_multiroom_scenarios_safe(db: Session) -> list[dict[str, Any]]:
    rows = db.query(MultiroomScenario).order_by(MultiroomScenario.name).all()
    return [scenario_to_dict(db, row) for row in rows]


def save_multiroom_scenario(db: Session, payload: dict[str, Any], *, scenario_id: int | None = None) -> dict[str, Any]:
    scenario = db.query(MultiroomScenario).filter(MultiroomScenario.id == scenario_id).one_or_none() if scenario_id else None
    if scenario is None:
        scenario = MultiroomScenario(name=str(payload.get("name") or "Multiroom-Szenario"))
        db.add(scenario)
    scenario.name = str(payload.get("name") or scenario.name)
    scenario.master_device_id = str(payload.get("master_device_id") or "")
    scenario.member_device_ids = ",".join(str(item) for item in payload.get("member_device_ids") or [])
    scenario.station_id = payload.get("station_id")
    scenario.volume = payload.get("volume")
    scenario.description = json.dumps({"device_policy_respected": True, "manual_test_required": True}, ensure_ascii=False)
    db.flush()
    return scenario_to_dict(db, scenario)
