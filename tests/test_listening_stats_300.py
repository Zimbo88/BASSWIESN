from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, PlayHistory
from basswiesn.app.services.listening_stats import listening_summary


NOW = datetime(2026, 9, 23, 0, 30, tzinfo=UTC)


def row(**kwargs):
    fields = dict(id=1, device_id="FIXTURE", device_name="Old Name", station_name="Example Station",
                  started_at=NOW - timedelta(hours=1), ended_at=NOW)
    return PlayHistory(**(fields | kwargs))


def summary(rows):
    return listening_summary(rows, now=NOW, tolerance=360,
                             device_names={"FIXTURE": "Current Name"},
                             station_names={item.id: item.station_name for item in rows})


def test_session_crossing_midnight_is_clipped_not_discarded_or_fully_counted():
    result = summary([row()])
    assert result["periods"]["today"]["seconds"] == 1800
    assert result["periods"]["7d"]["seconds"] == 3600
    assert result["periods"]["all"]["by_device"][0]["name"] == "Current Name"
    assert result["audible_output_verified"] is False and result["duration_is_estimate"] is True


def test_open_session_is_bounded_by_last_confirmation_plus_poll_tolerance():
    value = row(started_at=NOW - timedelta(hours=2), ended_at=None,
                last_confirmed_playing_at=NOW - timedelta(hours=1))
    result = summary([value])["periods"]
    assert result["all"]["seconds"] == 3960
    assert result["today"]["seconds"] == 0


def test_future_or_reversed_timestamp_never_adds_playback():
    assert summary([row(started_at=NOW + timedelta(days=1))])["periods"]["all"]["seconds"] == 0
    assert summary([row(ended_at=NOW - timedelta(hours=2))])["periods"]["all"]["seconds"] == 0


def test_rolling_windows_count_only_overlapping_part():
    value = row(started_at=NOW - timedelta(days=8), ended_at=NOW - timedelta(days=6))
    assert summary([value])["periods"]["7d"]["seconds"] == 86400
    assert summary([value])["periods"]["30d"]["seconds"] == 172800


def test_stats_endpoint_excludes_internal_and_unconfirmed_and_handles_boundary(monkeypatch):
    from basswiesn.app.routers import api
    monkeypatch.setattr(api, "utc_now", lambda: NOW)
    with app_db.SessionLocal() as db:
        db.add(Device(device_id="FIXTURE", name="Current Name", ip_address="192.0.2.42"))
        db.add(row())
        db.add(row(id=2, internal_event=True))
        db.add(row(id=3, is_confirmed=False))
        db.commit()
    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.get("/api/stats/playback")
    assert response.status_code == 200
    value = response.json()
    assert value["listening"]["periods"]["today"]["seconds"] == 1800
    assert value["listening"]["periods"]["today"]["sessions"] == 1
    assert value["aggregate"]["today_hours"] == 0.5
    assert value["lifetime"]["total_seconds"] == 3600
