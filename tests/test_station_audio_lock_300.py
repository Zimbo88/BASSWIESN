"""A persisted safety stop must also protect the normal Stations route."""
import json

import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, RuntimeState, Setting, SetupRebuildDeviceState, Station
from basswiesn.app.routers import stations_presets
from basswiesn.app.services.setup_rebuild.audio_safety import load_audio_safety


def _seed(kind):
    with app_db.SessionLocal() as db:
        station = Station(name="Offline safety fixture", stream_url="https://example.invalid/live.mp3")
        db.add_all([Device(device_id="AUDIO-LOCK-FIXTURE", ip_address="192.0.2.42"), station,
                    Setting(key="lan_host", value="192.0.2.100")])
        if kind == "legacy":
            db.add(SetupRebuildDeviceState(job_id="prior-test", device_id="AUDIO-LOCK-FIXTURE",
                                           evidence_json=json.dumps({"audio_test_locked": True})))
        else:
            db.add(RuntimeState(key="device:AUDIO-LOCK-FIXTURE:audio_safety",
                value="not-json" if kind == "malformed" else json.dumps({"locked": True, "reason": "prior excursion"})))
        db.commit()
        return station.id


def _no_transport(*args, **kwargs):
    raise AssertionError("Locked playback must stop before constructing a radio transport")


@pytest.mark.parametrize("kind", ["explicit", "legacy", "malformed"])
@pytest.mark.parametrize("payload", [{"safe_volume": 1}, {}])
def test_station_play_rejects_existing_audio_lock_without_transport(monkeypatch, kind, payload):
    station_id = _seed(kind)
    monkeypatch.setattr(stations_presets, "SoundTouchClient", _no_transport)
    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.post(f"/api/devices/AUDIO-LOCK-FIXTURE/stations/{station_id}/play", json=payload)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "audio_safety_locked"
    with app_db.SessionLocal() as db:
        assert load_audio_safety(db, "AUDIO-LOCK-FIXTURE").locked
        assert db.query(RuntimeState).filter(RuntimeState.key.like("%:playback_safety_gate")).count() == 0


@pytest.mark.parametrize("kind", ["explicit", "legacy", "malformed"])
def test_locked_station_preview_remains_read_only_and_preserves_lock(monkeypatch, kind):
    station_id = _seed(kind)
    monkeypatch.setattr(stations_presets, "SoundTouchClient", _no_transport)
    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.post(f"/api/devices/AUDIO-LOCK-FIXTURE/stations/{station_id}/play",
                               json={"dry_run": True, "safe_volume": 1})
    assert response.status_code == 200
    assert response.json()["dry_run"] is True
    with app_db.SessionLocal() as db:
        assert load_audio_safety(db, "AUDIO-LOCK-FIXTURE").locked
