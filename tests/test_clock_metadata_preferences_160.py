from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import pytest

from basswiesn.app.db.migrations import ensure_schema_baseline
from basswiesn.app.services.clock_metadata import (
    load_clock_metadata_preference,
    save_clock_metadata_preference,
)
from basswiesn.app.services.metadata_engine import ClockMetadataMode


pytestmark = pytest.mark.unit


def _db() -> tuple[object, Session]:
    engine = create_engine("sqlite:///:memory:")
    ensure_schema_baseline(engine)
    return engine, Session(engine)


def test_clock_metadata_is_per_device_experimental_and_off_by_default() -> None:
    engine, db = _db()
    try:
        assert load_clock_metadata_preference(db, "RADIO-A").as_dict() == {
            "enabled": False,
            "mode": "MISSING_TITLE",
            "interval_seconds": 60,
            "experimental": True,
        }
        saved = save_clock_metadata_preference(
            db, "RADIO-A", enabled=True, mode=ClockMetadataMode.APPEND,
        )
        assert saved.enabled is True
        assert load_clock_metadata_preference(db, "RADIO-A").mode == ClockMetadataMode.APPEND
        assert load_clock_metadata_preference(db, "RADIO-B").enabled is False
    finally:
        db.close()
        engine.dispose()


def test_clock_metadata_cannot_run_faster_than_conservative_lab_interval() -> None:
    engine, db = _db()
    try:
        with pytest.raises(ValueError, match="at least 60"):
            save_clock_metadata_preference(
                db, "RADIO-A", enabled=True, mode="MISSING_TITLE", interval_seconds=59,
            )
    finally:
        db.close()
        engine.dispose()
