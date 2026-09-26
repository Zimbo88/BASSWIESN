"""Explicit LAB entry for unfinished 3.0.0 workflows, not an auth boundary."""
from fastapi import HTTPException
from basswiesn.app.models import Setting


def require_lab(db):
    row = db.query(Setting).filter(Setting.key == "ui_mode").one_or_none()
    if row is None or row.value != "lab":
        raise HTTPException(409, detail={"code": "LAB_MODE_REQUIRED"})
