"""Reporting persistence failures never cause provider failure or radio writes."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import OperationalError

from basswiesn.app.routers import cloud
from basswiesn.app import db as app_db
from basswiesn.app.models import Device


@pytest.mark.parametrize("body,status", [({"eventType": "STOP", "reason": "FINISH", "timeIntoTrack": 21604}, 200), ({"eventType": 42}, 400)])
def test_busy_reporting_database_isolated_from_provider_contract(monkeypatch, body, status):
    app = FastAPI()
    app.include_router(cloud.router)
    logged = []

    def broken(*args, **kwargs):
        raise OperationalError("query", {}, Exception("database is locked"))

    monkeypatch.setattr(cloud, "_persist_reporting_response", broken)
    monkeypatch.setattr(cloud, "write_masterlog", lambda event, **fields: logged.append((event, fields)))
    with TestClient(app) as client:
        result = client.post("/bmx/orion/reporting/station/test-station", json=body)
    assert result.status_code == status
    if status == 200:
        assert result.json()["nextReportIn"] == 6
        assert result.json()["_embedded"] == {}  # no clearing of the previous track/artist
        assert result.headers["X-BASSWIESN-Reporting-Storage"] == "unavailable"
        assert logged == [("reporting_persistence_failed", {"provider_id": cloud.LOCAL_PROVIDER_ID, "error_type": "OperationalError", "persistence": "UNAVAILABLE"})]
    else:
        assert logged == []


def test_finish_diagnostic_has_device_and_media_position_not_free_reason(monkeypatch):
    app = FastAPI()
    app.include_router(cloud.router)
    logged = []
    monkeypatch.setattr(cloud, "write_masterlog", lambda event, **fields: logged.append((event, fields)))
    with app_db.SessionLocal() as db:
        db.add(Device(device_id="REPORT-A", ip_address="192.0.2.81", name="Example Radio"))
        db.commit()
    with TestClient(app) as client:
        for reason in ("FINISH", "untrusted-free-value"):
            response = client.post("/bmx/orion/reporting/station/example", headers={"X-Device-ID": "REPORT-A"},
                                   json={"eventType": "STOP", "reason": reason, "timeIntoTrack": 21604})
            assert response.status_code == 200
    events = [fields for event, fields in logged if event == "playback_stop_reported"]
    assert [event["reason"] for event in events] == ["FINISH", "OTHER_OR_UNSPECIFIED"]
    assert all(event["device_id"] == "REPORT-A" and event["time_into_track_seconds"] == 21604 and event["automatic_action"] == "NONE" for event in events)
