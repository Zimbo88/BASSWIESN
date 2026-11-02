from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Setting


def _set_offline_mode(value: str) -> None:
    db = app_db.SessionLocal()
    db.add(Setting(key="offline_mode", value=value))
    db.commit()
    db.close()


def test_strict_offline_blocks_radio_browser_search_before_network():
    _set_offline_mode("strict")
    with TestClient(create_web_app()) as client:
        response = client.get("/api/stations/search-online?q=jazz")

    assert response.status_code == 409
    assert response.json()["detail"]["offline"]["mode"] == "strict"


def test_strict_offline_blocks_update_manifest_check():
    db = app_db.SessionLocal()
    db.add_all([
        Setting(key="offline_mode", value="strict"),
        Setting(key="update_manifest_url", value="https://updates.example.test/manifest.json"),
    ])
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.post("/api/update/check", json={})

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "blocked_by_offline_mode"
    assert body["offline"]["target_host"] == "updates.example.test"
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
