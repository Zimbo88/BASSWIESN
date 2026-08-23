from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Setting, Station


def test_internal_station_is_server_filtered_and_lab_gated():
    db = app_db.SessionLocal()
    db.add_all([Station(name="Normal", stream_url="http://example.test/normal.mp3"), Station(name="BASSWIESN Activation AAC", stream_url="http://example.test/aac", internal=True, purpose="activation", lab_only=True)])
    db.commit()
    db.close()
    with TestClient(create_web_app()) as client:
        assert [row["name"] for row in client.get("/api/stations").json()] == ["Normal"]
        assert [row["name"] for row in client.get("/api/stations?include_internal=true").json()] == ["Normal"]
        db = app_db.SessionLocal()
        db.add(Setting(key="lab_mode", value="true"))
        db.commit()
        db.close()
        names = [row["name"] for row in client.get("/api/stations?include_internal=true").json()]
        assert names == ["BASSWIESN Activation AAC", "Normal"]
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
