import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, DiagnosticEvent
from basswiesn.app.services.device_policy import DeviceClass, policy_for_device
from basswiesn.app.services.device_interactions import DeviceInteractionCoordinator
from basswiesn.app.services.device_state import save_runtime_state
from basswiesn.app.services.playback_keepalive import run_playback_keepalive_for_device


def test_portable_is_classified_but_not_special_cased():
    db = app_db.SessionLocal()
    device = Device(device_id="PORT1", name="Portable", model="SoundTouch Portable", ip_address="192.0.2.41")
    db.add(device)
    db.commit()

    policy = policy_for_device(device, db)
    db.close()

    assert policy.device_class == DeviceClass.PORTABLE
    assert policy.safe_mode_active is False
    assert policy.allow_auto_wakeup is True
    assert policy.allow_preset_restore is True
    assert policy.allow_invalid_source_recovery is True
    assert policy.allow_battery_poll is False


def test_explicit_safe_mode_is_enforced_for_background_actions():
    db = app_db.SessionLocal()
    device = Device(
        device_id="SAFEALWAYS",
        name="Safe",
        model="SoundTouch 30",
        ip_address="192.0.2.45",
        safe_mode="always",
        maintenance_actions_allowed=True,
    )
    db.add(device)
    db.commit()

    policy = policy_for_device(device, db)
    db.close()

    assert policy.safe_mode_active is True
    assert policy.allow_auto_wakeup is False
    assert policy.allow_preset_restore is False
    assert policy.allow_invalid_source_recovery is False
    assert policy.allow_maintenance_actions is False


def test_auto_safe_mode_activates_for_unreachable_device():
    db = app_db.SessionLocal()
    device = Device(
        device_id="SAFEAUTO",
        name="Safe Auto",
        model="SoundTouch 30",
        ip_address="192.0.2.46",
        safe_mode="auto",
        reachable=False,
        failure_count=5,
    )
    db.add(device)
    db.commit()

    policy = policy_for_device(device, db)
    db.close()

    assert policy.safe_mode_active is True
    assert policy.allow_auto_wakeup is False
    assert policy.allow_invalid_source_recovery is False


def test_safe_mode_skip_flag_blocks_before_transport():
    db = app_db.SessionLocal()
    device = Device(
        device_id="SAFESKIP",
        name="Safe Skip",
        model="SoundTouch 30",
        ip_address="192.0.2.47",
        safe_mode="always",
    )
    db.add(device)
    db.commit()

    result = asyncio.run(
        DeviceInteractionCoordinator().request_xml(
            db,
            device,
            "/info",
            request_purpose="background_check",
            requester="test",
            allow_safe_mode_skip=True,
        )
    )
    db.close()

    assert result.ok is False
    assert result.skipped is True
    assert result.skip_reason == "device safe mode blocks automatic diagnostic access"


def test_portable_standby_keepalive_uses_normal_polling():
    db = app_db.SessionLocal()
    device = Device(device_id="PORT2", name="Portable", model="SoundTouch Portable", ip_address="192.0.2.42")
    db.add(device)
    db.commit()
    now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    save_runtime_state(
        db,
        device.device_id,
        {
            "current_source": "STANDBY",
            "playback_state": "STOP_STATE",
            "playback_keepalive": {"last_keepalive_at": now.isoformat()},
        },
    )

    class CountingClient:
        calls = 0

        def __init__(self, _ip):
            pass

        async def get_xml(self, endpoint):
            CountingClient.calls += 1
            if endpoint == "/now_playing":
                return '<nowPlaying source="STANDBY"><playStatus>STOP_STATE</playStatus></nowPlaying>'
            if endpoint == "/volume":
                return "<volume><actualvolume>5</actualvolume></volume>"
            return "<ok />"

    result = asyncio.run(
        run_playback_keepalive_for_device(
            device,
            db,
            now=now + timedelta(minutes=5),
            client_factory=CountingClient,
        )
    )
    db.close()

    assert result["ok"] is True
    assert result["safe_mode_active"] is False
    assert result["reads"] == ["/now_playing", "/volume"]
    assert CountingClient.calls == 2


def test_portable_invalid_source_is_diagnosed_without_automatic_recovery():
    db = app_db.SessionLocal()
    device = Device(device_id="PORT3", name="Portable", model="SoundTouch Portable", ip_address="192.0.2.43")
    db.add(device)
    db.commit()
    now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    save_runtime_state(
        db,
        device.device_id,
        {
            "playback_keepalive": {
                "last_source": "LOCAL_INTERNET_RADIO",
                "last_preset_slot": 1,
            }
        },
    )

    class InvalidSourceClient:
        posts = 0

        def __init__(self, _ip):
            pass

        async def get_xml(self, endpoint):
            if endpoint == "/now_playing":
                return '<nowPlaying source="INVALID_SOURCE"><playStatus>STOP_STATE</playStatus></nowPlaying>'
            if endpoint == "/volume":
                return "<volume><actualvolume>5</actualvolume></volume>"
            return "<ok />"

        async def post_xml(self, _endpoint, _xml):
            InvalidSourceClient.posts += 1
            return "<ok />"

    result = asyncio.run(run_playback_keepalive_for_device(device, db, now=now, client_factory=InvalidSourceClient))
    events = db.query(DiagnosticEvent).filter(DiagnosticEvent.device_id == "PORT3").all()
    db.close()

    assert result["ok"] is True
    assert result["invalid_source_action"] == "NONE"
    assert InvalidSourceClient.posts == 0
    assert [event.code for event in events] == [
        "INVALID_SOURCE_OBSERVED",
        "PROVIDER_SOURCE_INVALID",
        "PLAYBACK_FAILED",
    ]


def test_device_policy_api_allows_manual_override():
    db = app_db.SessionLocal()
    db.add(Device(device_id="PORTAPI", name="Portable API", model="SoundTouch Portable", ip_address="192.0.2.44"))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        updated = client.put(
            "/api/devices/PORTAPI/policy",
            json={"device_class_override": "stationary", "safe_mode": "disabled", "auto_restore_allowed": True},
        )
        policy = client.get("/api/devices/PORTAPI/policy")

    assert updated.status_code == 200
    assert policy.json()["device_class"] == "stationary"
    assert policy.json()["safe_mode_active"] is False
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
