from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, PlayHistory


def test_playback_stats_exclude_internal_events_but_count_user_playback():
    db = app_db.SessionLocal()
    db.add(Device(device_id="STATS1", name="Stats", ip_address="192.0.2.30", model="SoundTouch Test"))
    start = datetime.now(UTC) - timedelta(minutes=10)
    rows = [
        PlayHistory(device_id="STATS1", device_name="Stats", station_name="User Radio", started_at=start, ended_at=start + timedelta(minutes=5), trigger_type="manual", source_type="LOCAL_INTERNET_RADIO"),
        PlayHistory(device_id="STATS1", device_name="Stats", started_at=start, ended_at=start + timedelta(hours=1), trigger_type="keepalive_internal", source_type="keepalive_internal", internal_event=True),
        PlayHistory(device_id="STATS1", device_name="Stats", started_at=start, ended_at=start + timedelta(hours=1), trigger_type="setup_activation", source_type="setup_activation"),
        PlayHistory(device_id="STATS1", device_name="Stats", started_at=start, ended_at=start + timedelta(hours=1), trigger_type="manual", source_type="STANDBY"),
        PlayHistory(device_id="STATS1", device_name="Stats", station_display_name="Unbekannter Sender", started_at=start, ended_at=start + timedelta(minutes=5), trigger_type="station", source_type="LOCAL_INTERNET_RADIO"),
    ]
    db.add_all(rows)
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        stats = client.get("/api/stats/playback").json()

    assert stats["lifetime"]["total_plays"] == 1
    assert stats["lifetime"]["total_seconds"] == 5 * 60
    assert stats["lifetime"]["internal_events_excluded"] == 4
    assert stats["by_device"][0]["seconds"] == 5 * 60
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
