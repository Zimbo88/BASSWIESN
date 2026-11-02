"""An unavailable protection policy is not an empty protection policy."""
from __future__ import annotations

import asyncio

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.adapters import soundtouch_client
from basswiesn.app.models import Device
from basswiesn.app.services import protected_devices as protection
from basswiesn.app.services.network_security import validate_outbound_http_url

pytestmark = pytest.mark.integration


@pytest.fixture
def failed_policy_store(monkeypatch):
    def failed():
        raise OSError("simulated policy database failure")
    monkeypatch.setattr(app_db, "SessionLocal", failed)


@pytest.mark.parametrize("check", [
    lambda: protection.protected_device_ips(),
    lambda: protection.protected_device_ids(),
    lambda: protection.is_protected_ip("192.0.2.42"),
    lambda: protection.is_device_access_protected("192.0.2.42"),
    lambda: protection.is_explicit_discovery_target_protected("192.0.2.42", "AA11BB22CC33"),
    lambda: protection.first_protected_ip(["192.0.2.42"]),
    lambda: protection.is_protected_device(Device(device_id="AA11BB22CC33", ip_address="192.0.2.42")),
    lambda: validate_outbound_http_url("http://192.0.2.42/media"),
])
def test_all_policy_consumers_fail_closed(failed_policy_store, check):
    with pytest.raises(Exception) as caught:
        check()
    assert getattr(caught.value, "code", "") == "PROTECTION_POLICY_UNAVAILABLE"
    assert "simulated" not in str(caught.value)


def test_radio_transport_is_not_created_when_policy_cannot_be_read(failed_policy_store, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("HTTP transport was created without the protection policy")
    monkeypatch.setattr(soundtouch_client.httpx, "AsyncClient", forbidden)
    with pytest.raises(Exception) as caught:
        asyncio.run(soundtouch_client.SoundTouchClient("192.0.2.42").post_xml("/volume", "<volume>1</volume>"))
    assert getattr(caught.value, "code", "") == "PROTECTION_POLICY_UNAVAILABLE"


def test_missing_policy_rows_are_not_database_failure():
    # A fresh database is valid; only an unreadable policy must stop transport.
    assert not protection.is_device_access_protected("192.0.2.42", "AA11BB22CC33")
