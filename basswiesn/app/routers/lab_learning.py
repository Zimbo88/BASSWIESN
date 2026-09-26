"""LAB simulator and reusable app-only display profiles. No hardware routes."""
import hashlib
import json
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from basswiesn.app.models import RuntimeState
from basswiesn.app.routers.lab_workbench import lab_db
from basswiesn.app.services.lab_learning import SCENARIOS, preview_profile, simulate
from basswiesn.app.services.station_metadata import DISPLAY_FIELDS, DisplayPreference, validate_display_preference

router = APIRouter(prefix="/api/lab/learning", tags=["lab-learning"])
PREFIX = "lab:display-profile:"
LIMIT = 30


class SimulationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: Literal["network_loss", "stale_ip", "metadata_missing", "reporting_failure", "provider_unavailable", "invalid_source", "partial_zone"]
    step: int = Field(default=0, ge=0, le=3, strict=True)


class Layout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[str] = Field(max_length=5)
    field_order: list[str] = Field(max_length=5)

    @model_validator(mode="after")
    def check(self):
        validate_display_preference(DisplayPreference("CUSTOM", "other" in self.fields,
                                                     tuple(self.fields), tuple(self.field_order)))
        return self


class Preview(Layout):
    scenario: Literal["normal", "missing", "long"] = "normal"


class Profile(Layout):
    name: str = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def clean_name(self):
        self.name = self.name.strip()
        if not self.name or any(not c.isprintable() for c in self.name):
            raise ValueError("invalid profile name")
        return self


class DeleteProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


def item(row):
    try:
        data = Profile.model_validate_json(row.value).model_dump()
    except ValueError:
        raise HTTPException(409, detail={"code": "PROFILE_INVALID"})
    return {"id": row.key.removeprefix(PREFIX), **data,
            "revision": hashlib.sha256(row.value.encode()).hexdigest()}


@router.get("/simulator")
def scenarios(db: Session = Depends(lab_db)):
    return {"scenarios": [{"id": key, "steps": len(steps)} for key, steps in SCENARIOS.items()],
            "synthetic": True, "radio_contacted": False}


@router.post("/simulator")
def run_simulator(body: SimulationBody, db: Session = Depends(lab_db)):
    return simulate(body.scenario, body.step)


@router.post("/display-preview")
def preview(body: Preview, db: Session = Depends(lab_db)):
    return preview_profile(body.fields, body.field_order, body.scenario)


@router.get("/display-profiles")
def profiles(db: Session = Depends(lab_db)):
    rows = db.query(RuntimeState).filter(RuntimeState.key.like(PREFIX + "%")).order_by(RuntimeState.key).limit(LIMIT + 1).all()
    return {"items": [item(row) for row in rows[:LIMIT]], "limit": LIMIT,
            "truncated": len(rows) > LIMIT, "radio_contacted": False}


@router.post("/display-profiles")
def save_profile(body: Profile, db: Session = Depends(lab_db)):
    # Serialize capacity check and insertion in SQLite, including concurrency.
    db.execute(sql_text("BEGIN IMMEDIATE"))
    if db.query(RuntimeState).filter(RuntimeState.key.like(PREFIX + "%")).count() >= LIMIT:
        raise HTTPException(409, detail={"code": "PROFILE_LIMIT"})
    row = RuntimeState(key=PREFIX + uuid4().hex, value=json.dumps(body.model_dump(), ensure_ascii=False, sort_keys=True))
    db.add(row)
    db.commit()
    return {"profile": item(row), "radio_contacted": False, "applied_to_radio": False}


@router.delete("/display-profiles/{profile_id}")
def remove_profile(profile_id: str, body: DeleteProfile, db: Session = Depends(lab_db)):
    if len(profile_id) != 32 or any(c not in "0123456789abcdef" for c in profile_id):
        raise HTTPException(404, detail={"code": "PROFILE_NOT_FOUND"})
    db.execute(sql_text("BEGIN IMMEDIATE"))
    row = db.query(RuntimeState).filter_by(key=PREFIX + profile_id).one_or_none()
    if row is None:
        raise HTTPException(404, detail={"code": "PROFILE_NOT_FOUND"})
    if item(row)["revision"] != body.revision:
        raise HTTPException(409, detail={"code": "PROFILE_CHANGED"})
    db.delete(row)
    db.commit()
    return {"deleted": True, "radio_contacted": False}
