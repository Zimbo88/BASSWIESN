from __future__ import annotations

import asyncio
import json

import pytest

from basswiesn.app import db as app_db
from basswiesn.app import config
from basswiesn.app.models import Device, RuntimeState
from basswiesn.app.services.device_state import runtime_state_key
from basswiesn.app.services.ssdp_discovery import (
    SSDPCandidate,
    rediscover_device_by_id,
)
from basswiesn.app.services.playback_keepalive import (
    run_playback_keepalive_for_device,
)


pytestmark = pytest.mark.integration


def _candidate(device_id: str, ip_address: str) -> SSDPCandidate:
    return SSDPCandidate(
        location=f"http://{ip_address}:8091/XD/{device_id}.xml",
        usn=f"uuid:{device_id}::upnp:rootdevice",
        server="Bose SoundTouch",
        st="ssdp:all",
        remote_ip=ip_address,
    )


def test_targeted_rediscovery_updates_verified_ip_and_closes_circuit(monkeypatch):
    device_id = "A1B2C3D4E5F6"
    old_ip = "192.0.2.47"
    new_ip = "192.0.2.147"
    db = app_db.SessionLocal()
    db.add(
        Device(
            device_id=device_id,
            name="Sound Big",
            model="SoundTouch 30",
            ip_address=old_ip,
            reachable=False,
            failure_count=7,
            offline_reason="stale endpoint",
        )
    )
    db.add(
        RuntimeState(
            key=runtime_state_key(device_id),
            value=json.dumps(
                {
                    "playback_keepalive": {
                        "consecutive_failures": 7,
                        "paused": True,
                        "next_retry_at": "2099-01-01T00:00:00+00:00",
                        "circuit_state": "open",
                    }
                }
            ),
        )
    )
    db.commit()

    async def descriptor(candidate, *, timeout_seconds):
        assert candidate.remote_ip == new_ip
        assert timeout_seconds >= 1
        return True, {
            "friendlyName": "Sound Big",
            "manufacturer": "Bose",
            "modelName": "SoundTouch 30",
            "modelDescription": "",
            "UDN": f"uuid:{device_id}",
        }, "ok"

    monkeypatch.setattr(
        "basswiesn.app.services.ssdp_discovery._fetch_descriptor", descriptor
    )
    result = asyncio.run(
        rediscover_device_by_id(db, device_id, candidates=[_candidate(device_id, new_ip)])
    )
    moved = db.query(Device).filter(Device.device_id == device_id).one()
    runtime = json.loads(
        db.query(RuntimeState)
        .filter(RuntimeState.key == runtime_state_key(device_id))
        .one()
        .value
    )["playback_keepalive"]
    db.close()

    assert result["status"] == "ip_changed"
    assert result["verified"] is True
    assert moved.ip_address == new_ip
    assert moved.reachable is True
    assert moved.failure_count == 0
    assert moved.offline_reason == ""
    assert runtime["consecutive_failures"] == 0
    assert runtime["paused"] is False
    assert runtime["next_retry_at"] == ""
    assert runtime["circuit_state"] == "closed"
    assert runtime["rediscovered_from_ip"] == old_ip
    assert runtime["rediscovered_to_ip"] == new_ip


def test_targeted_rediscovery_never_fetches_nonmatching_or_protected_bystanders(
    monkeypatch,
):
    target_id = "A1B2C3D4E5F6"
    protected_id = "CCDDEEFF0011"
    db = app_db.SessionLocal()
    db.add(Device(device_id=target_id, name="Target", ip_address="192.0.2.47"))
    db.commit()
    monkeypatch.setenv("PROTECTED_DEVICE_IDS", protected_id)
    config.get_settings.cache_clear()
    fetched: list[str] = []

    async def forbidden_fetch(candidate, *, timeout_seconds):
        fetched.append(candidate.remote_ip)
        raise AssertionError("a non-target SSDP reply must not receive unicast")

    monkeypatch.setattr(
        "basswiesn.app.services.ssdp_discovery._fetch_descriptor", forbidden_fetch
    )
    try:
        result = asyncio.run(
            rediscover_device_by_id(
                db,
                target_id,
                candidates=[
                    _candidate("B1C2D3E4F5A6", "192.0.2.112"),
                    _candidate(protected_id, "192.0.2.25"),
                ],
            )
        )
    finally:
        config.get_settings.cache_clear()
        db.close()

    assert result["status"] == "not_found"
    assert result["unicast_attempted"] is False
    assert fetched == []


def test_protected_target_is_blocked_before_multicast(monkeypatch):
    protected_id = "CCDDEEFF0011"
    db = app_db.SessionLocal()
    db.add(
        Device(
            device_id=protected_id,
            name="Protected",
            ip_address="192.0.2.25",
        )
    )
    db.commit()
    monkeypatch.setenv("PROTECTED_DEVICE_IDS", protected_id)
    config.get_settings.cache_clear()

    def forbidden_msearch(*_args, **_kwargs):
        raise AssertionError("protected target must be blocked before multicast")

    monkeypatch.setattr(
        "basswiesn.app.services.ssdp_discovery._send_msearch", forbidden_msearch
    )
    try:
        result = asyncio.run(rediscover_device_by_id(db, protected_id))
    finally:
        config.get_settings.cache_clear()
        db.close()

    assert result == {
        "status": "protected",
        "verified": False,
        "unicast_attempted": False,
    }


@pytest.mark.parametrize("starts_in_backoff", [False, True])
def test_keepalive_recovers_stale_ip_by_verified_identity(
    monkeypatch, starts_in_backoff
):
    device_id = "A1B2C3D4E5F6"
    old_ip = "192.0.2.47"
    new_ip = "192.0.2.147"
    db = app_db.SessionLocal()
    device = Device(
        device_id=device_id,
        name="Sound Big",
        model="SoundTouch 30",
        ip_address=old_ip,
        reachable=not starts_in_backoff,
        failure_count=5 if starts_in_backoff else 0,
    )
    db.add(device)
    if starts_in_backoff:
        db.add(
            RuntimeState(
                key=runtime_state_key(device_id),
                value=json.dumps(
                    {
                        "playback_keepalive": {
                            "consecutive_failures": 5,
                            "paused": True,
                            "next_retry_at": "2099-01-01T00:00:00+00:00",
                            "circuit_state": "open",
                        }
                    }
                ),
            )
        )
    db.commit()

    async def descriptor(_candidate, *, timeout_seconds):
        return True, {
            "friendlyName": "Sound Big",
            "manufacturer": "Bose",
            "modelName": "SoundTouch 30",
            "modelDescription": "",
            "UDN": f"uuid:{device_id}",
        }, "ok"

    async def rediscovery(session, requested_id):
        assert requested_id == device_id
        return await rediscover_device_by_id(
            session,
            requested_id,
            candidates=[_candidate(requested_id, new_ip)],
        )

    class Client:
        def __init__(self, ip_address):
            self.ip_address = ip_address

        async def get_xml(self, endpoint):
            if self.ip_address == old_ip:
                raise OSError("stale DHCP endpoint")
            if endpoint == "/now_playing":
                return (
                    '<nowPlaying source="STANDBY">'
                    '<playStatus>STOP_STATE</playStatus></nowPlaying>'
                )
            return "<volume><actualvolume>1</actualvolume></volume>"

    monkeypatch.setattr(
        "basswiesn.app.services.ssdp_discovery._fetch_descriptor", descriptor
    )
    result = asyncio.run(
        run_playback_keepalive_for_device(
            device,
            db,
            client_factory=Client,
            rediscovery_handler=rediscovery,
        )
    )
    db.refresh(device)
    runtime = json.loads(
        db.query(RuntimeState)
        .filter(RuntimeState.key == runtime_state_key(device_id))
        .one()
        .value
    )["playback_keepalive"]
    db.close()

    assert result["ok"] is True
    if not starts_in_backoff:
        assert result["rediscovery"]["status"] == "ip_changed"
    assert runtime["rediscovery_status"] == "ip_changed"
    assert device.ip_address == new_ip
    assert device.reachable is True
    assert device.failure_count == 0
