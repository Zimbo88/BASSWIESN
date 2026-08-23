"""Transactional preflight coverage for the human device-settings path."""

import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device
from basswiesn.app.routers import api


pytestmark = pytest.mark.integration


def test_settings_apply_validates_every_field_before_first_transport(monkeypatch):
    db = app_db.SessionLocal()
    db.add(Device(device_id="SETTINGS-PREFLIGHT", ip_address="192.0.2.210"))
    db.commit()
    db.close()

    class NoTransport:
        def __init__(self, _ip):
            raise AssertionError("transport must not start before full validation")

    monkeypatch.setattr(api, "SoundTouchClient", NoTransport)
    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.post(
            "/api/devices/SETTINGS-PREFLIGHT/settings-apply",
            json={"values": {"volume": 1, "language": "id:0"}},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "unsupported Stockholm language"


def test_settings_apply_rejects_unknown_timezone_before_transport(monkeypatch):
    db = app_db.SessionLocal()
    db.add(Device(device_id="SETTINGS-TIMEZONE", ip_address="192.0.2.211"))
    db.commit()
    db.close()

    class NoTransport:
        def __init__(self, _ip):
            raise AssertionError("transport must not start before full validation")

    monkeypatch.setattr(api, "SoundTouchClient", NoTransport)
    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.post(
            "/api/devices/SETTINGS-TIMEZONE/settings-apply",
            json={
                "values": {
                    "volume": 1,
                    "clockConfig": {
                        "timezoneInfo": "NOT_SET",
                        "timeFormat": "TIME_FORMAT_24HOUR_ID",
                    },
                }
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "unsupported timezone"
