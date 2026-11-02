import asyncio

from basswiesn.app.models import Device
from basswiesn.app.routers import setup


def test_legacy_readiness_profile_matches_old_soundtouch_models():
    assert setup._legacy_readiness_profile(Device(device_id="A", model="SoundTouch 20", firmware="27.0.6")) is True
    assert setup._legacy_readiness_profile(Device(device_id="B", model="SoundTouch 30", firmware="26.0.1")) is True
    assert setup._legacy_readiness_profile(Device(device_id="C", model="SoundTouch Portable", firmware="26.0.1")) is True
    assert setup._legacy_readiness_profile(Device(device_id="D", model="SoundTouch 10", firmware="27.0.6")) is True
    assert setup._legacy_readiness_profile(Device(device_id="E", model="SoundTouch 10", firmware="30.0.1")) is False


def test_source_bootstrap_readiness_checks_now_playing(monkeypatch):
    calls = []

    class Client:
        def __init__(self, _ip):
            pass

        async def get_xml(self, endpoint):
            calls.append(endpoint)
            return "<ok/>"

    class HTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            return type("Response", (), {"status_code": 200})()

    monkeypatch.setattr(setup.api_core, "SoundTouchClient", Client)
    monkeypatch.setattr(setup.httpx, "AsyncClient", lambda timeout: HTTP())
    result = asyncio.run(setup._source_bootstrap_readiness(Device(device_id="ST20", ip_address="192.0.2.40"), "127.0.0.1", 1516, 1))

    assert result["info"] is True
    assert result["sources"] is True
    assert result["now_playing"] is True
    assert "/now_playing" in calls
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
