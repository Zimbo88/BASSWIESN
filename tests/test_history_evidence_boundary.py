"""Client claims never mint radio-observed listening evidence; no network needed."""
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, PlayHistory
from basswiesn.app.services.playback_state import confirm_playback_session

pytestmark = pytest.mark.integration
IDENTITY = "HISTORYFIXTURE"


def inventory():
    with app_db.SessionLocal() as db:
        db.add(Device(device_id=IDENTITY, name="Example radio", ip_address="192.0.2.42"))
        db.commit()


def observed_session(*, age=0, confirmed=True):
    with app_db.SessionLocal() as db:
        device = db.query(Device).filter_by(device_id=IDENTITY).one()
        row = confirm_playback_session(db, device, observed_at=datetime.now(UTC) - timedelta(seconds=age),
                                       source="LOCAL_INTERNET_RADIO", station_name="Observed station")
        row.is_confirmed = confirmed
        db.commit()
        return row.id


def test_forged_play_state_and_clock_cannot_create_confirmed_history():
    inventory()
    with TestClient(create_web_app()) as client:
        response = client.post("/api/play-history/start", json={
            "device_id": IDENTITY, "play_status": "PLAY_STATE", "is_confirmed": True,
            "state_observed_at": datetime.now(UTC).isoformat(), "source": "LOCAL_INTERNET_RADIO",
        })
        assert response.status_code == 200
        assert response.json()["opened"] is False
    with app_db.SessionLocal() as db:
        assert db.query(PlayHistory).count() == 0


def test_existing_fresh_observation_is_returned_but_never_relabelled_by_client():
    inventory()
    with TestClient(create_web_app()) as client:
        identity = observed_session()
        response = client.post("/api/play-history/start", json={
            "device_id": IDENTITY, "source": "PRESET", "station_name": "Fabricated station",
            "volume": "bad", "preset_button": "bad", "state_observed_at": "bad",
        })
        assert response.status_code == 200
        assert response.json()["id"] == identity
        assert response.json()["playback_verified"] is True
    with app_db.SessionLocal() as db:
        row = db.query(PlayHistory).one()
        assert row.station_name == "Observed station"
        assert row.source == "LOCAL_INTERNET_RADIO"


@pytest.mark.parametrize("age,confirmed", [(600, True), (-60, True), (0, False)])
def test_stale_future_and_unconfirmed_records_are_not_fresh_proof(age, confirmed):
    inventory()
    with TestClient(create_web_app()) as client:
        observed_session(age=age, confirmed=confirmed)
        response = client.post("/api/play-history/start", json={"device_id": IDENTITY})
        assert response.status_code == 200
        assert response.json()["opened"] is False


@pytest.mark.parametrize("trigger", ["play", "stop", "pause", "stop_pause"])
def test_client_events_cannot_close_verified_session_or_mint_duration(trigger):
    inventory()
    with TestClient(create_web_app()) as client:
        identity = observed_session()
        response = client.post("/api/play-history/event", json={
            "device_id": IDENTITY, "trigger": trigger, "source": "LOCAL_INTERNET_RADIO",
            "station_name": "Claimed", "success": True, "is_confirmed": True,
        })
        assert response.status_code == 200
        assert response.json()["playback_verified"] is False
    with app_db.SessionLocal() as db:
        original = db.get(PlayHistory, identity)
        assert original.ended_at is None
        event = db.get(PlayHistory, response.json()["id"])
        assert event.is_confirmed is False
        assert event.last_confirmed_playing_at is None
        assert event.started_at == event.ended_at
        assert event.confirmed_duration_seconds == 0
        assert event.end_reason == "client_report_unverified"


def test_compatibility_stop_requires_backend_observation_not_a_client_click():
    inventory()
    with TestClient(create_web_app()) as client:
        identity = observed_session()
        response = client.post(f"/api/play-history/{identity}/stop")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "HISTORY_OBSERVATION_REQUIRED"
    with app_db.SessionLocal() as db:
        assert db.get(PlayHistory, identity).ended_at is None
