from datetime import UTC, datetime, timedelta

from basswiesn.app import db as app_db
from basswiesn.app.models import Device, PlayHistory, Station
from basswiesn.app.services.playback_state import (
    close_open_sessions,
    confirm_playback_session,
    conservative_duration_seconds,
    is_confirmed_playing,
    reconcile_open_play_history,
)


def test_only_fresh_reachable_play_state_is_confirmed():
    now = datetime.now(UTC)
    base = dict(reachable=True, current_source="LOCAL_INTERNET_RADIO", playback_state="PLAY_STATE", play_status=None, state_observed_at=now, now=now, stale_after_seconds=60)
    assert is_confirmed_playing(**base)
    for change in ({"reachable": False}, {"current_source": "STANDBY"}, {"current_source": "INVALID_SOURCE"}, {"current_source": "SOURCE_DISCONNECTED"}, {"playback_state": "STOP_STATE"}, {"playback_state": "PAUSE_STATE"}, {"state_observed_at": now - timedelta(seconds=61)}):
        assert not is_confirmed_playing(**{**base, **change})


def test_confirmed_session_is_unique_and_offline_closes_at_confirmation():
    db = app_db.SessionLocal()
    device = Device(device_id="CONFIRMED", name="Confirmed", ip_address="192.0.2.180")
    db.add(device)
    db.flush()
    first = datetime.now(UTC) - timedelta(minutes=2)
    row = confirm_playback_session(db, device, observed_at=first, source="LOCAL_INTERNET_RADIO")
    confirm_playback_session(db, device, observed_at=first + timedelta(seconds=30), source="LOCAL_INTERNET_RADIO")
    assert db.query(PlayHistory).filter(PlayHistory.ended_at.is_(None)).count() == 1
    close_open_sessions(db, device.device_id, reason="offline", device_last_seen=device.last_seen)
    db.commit()
    assert row.ended_at == row.last_confirmed_playing_at
    db.close()


def test_unknown_open_session_is_reidentified_from_later_stream_evidence():
    db = app_db.SessionLocal()
    device = Device(device_id="REIDENTIFY", name="Reidentify", ip_address="192.0.2.181")
    station = Station(name="Bayern 3", stream_url="http://stream.example.test/bayern3.mp3")
    db.add_all([device, station])
    db.flush()
    first = datetime.now(UTC) - timedelta(minutes=2)

    row = confirm_playback_session(db, device, observed_at=first, source="LOCAL_INTERNET_RADIO")
    assert row.station_display_name == "Unbekannter Sender"

    updated = confirm_playback_session(
        db,
        device,
        observed_at=first + timedelta(seconds=30),
        source="LOCAL_INTERNET_RADIO",
        stream_url="http://stream.example.test/bayern3.mp3",
    )

    assert updated.id == row.id
    assert updated.station_display_name == "Bayern 3"
    assert updated.station_id == station.id
    assert updated.identity_confidence >= 90
    db.close()


def test_open_duration_is_capped_and_startup_reconciliation_idempotent():
    db = app_db.SessionLocal()
    start = datetime.now(UTC) - timedelta(days=3)
    row = PlayHistory(device_id="OLDOPEN", started_at=start, last_confirmed_playing_at=start + timedelta(seconds=20))
    db.add(row)
    db.commit()
    assert conservative_duration_seconds(row, poll_tolerance_seconds=60) == 80
    assert reconcile_open_play_history(db) == 1
    ended = row.ended_at
    assert reconcile_open_play_history(db) == 0
    assert row.ended_at == ended
    db.close()
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
