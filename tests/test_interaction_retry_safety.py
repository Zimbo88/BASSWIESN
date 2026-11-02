"""A response lost after a write must not cause a second radio mutation."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import Device, DeviceInteraction
from basswiesn.app.services import device_interactions as interactions

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("method,expected_calls,expected_ok", [("POST", 1, False), ("GET", 2, True)])
def test_only_read_requests_can_spend_retry_budget(monkeypatch, method, expected_calls, expected_ok):
    calls = []
    class FakeRadio:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get_xml(self, endpoint):
            calls.append(("GET", endpoint))
            if len(calls) == 1:
                raise httpx.ReadTimeout("response lost")
            return "<volume><actualvolume>1</actualvolume></volume>"

        async def post_xml(self, endpoint, body):
            calls.append(("POST", endpoint, body))
            if len(calls) == 1:
                # The simulated radio already applied the write. Only its reply is lost.
                raise httpx.ReadTimeout("response lost after write")
            return "<status>accepted</status>"

    monkeypatch.setattr(interactions, "SoundTouchClient", FakeRadio)
    with app_db.SessionLocal() as db:
        device = Device(device_id="AA11BB22CC33", name="Example Radio", ip_address="192.0.2.42")
        db.add(device)
        db.flush()
        result = asyncio.run(interactions.coordinator.request_xml(
            db, device, "/volume", method=method, body="<volume>1</volume>",
            request_purpose="offline regression", requester="test", retry_budget=2,
        ))
        db.flush()
        event = db.query(DeviceInteraction).filter_by(event_id=result.event_id).one()
        assert event.attempt == expected_calls
        assert event.result == ("ok" if expected_ok else "error")
    assert len(calls) == expected_calls
    assert result.ok is expected_ok
    if method == "POST":
        assert result.error_class == "ReadTimeout"
        assert not result.cache_hit
