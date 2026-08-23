import asyncio
from datetime import UTC, datetime, timedelta
import json

from basswiesn.app import db as app_db
from basswiesn.app.models import Device, RuntimeState
from basswiesn.app.services.playback_keepalive import run_playback_keepalive_once


def test_keepalive_marks_offline_pauses_and_recovers():
    db = app_db.SessionLocal()
    db.add(Device(device_id="KCB1", name="Breaker", model="SoundTouch 20", ip_address="192.0.2.20"))
    db.commit()

    class FailingClient:
        def __init__(self, _ip):
            pass

        async def get_xml(self, _endpoint):
            raise OSError("All connection attempts failed")

    now = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)
    results = [asyncio.run(run_playback_keepalive_once(db, client_factory=FailingClient, now=now + timedelta(minutes=index)))[0] for index in range(5)]
    device = db.query(Device).filter(Device.device_id == "KCB1").one()
    runtime = json.loads(db.query(RuntimeState).filter(RuntimeState.key == "device:KCB1:runtime_state").one().value)

    assert results[2]["failure_count"] == 3
    assert device.reachable is False
    assert device.failure_count == 3
    assert results[2]["paused"] is False
    assert results[2]["backoff_seconds"] == 5 * 60
    assert results[3]["skipped"] is True
    assert results[3]["reason"] == "backoff active"
    assert results[-1]["skipped"] is True
    assert runtime["playback_keepalive"]["next_retry_at"]

    class OkClient:
        def __init__(self, _ip):
            pass

        async def get_xml(self, endpoint):
            if endpoint == "/now_playing":
                return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus><ContentItem source="LOCAL_INTERNET_RADIO" location="x"/></nowPlaying>'
            return "<volume><actualvolume>5</actualvolume></volume>"

        async def post_xml(self, endpoint, xml):
            raise AssertionError("recovery keepalive must not write")

    recovered = asyncio.run(run_playback_keepalive_once(db, client_factory=OkClient, now=now + timedelta(minutes=10)))[0]
    db.refresh(device)
    runtime = json.loads(db.query(RuntimeState).filter(RuntimeState.key == "device:KCB1:runtime_state").one().value)
    db.close()

    assert recovered["ok"] is True
    assert device.reachable is True
    assert device.failure_count == 0
    assert runtime["playback_keepalive"]["consecutive_failures"] == 0
    assert runtime["playback_keepalive"]["next_retry_at"] == ""
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
