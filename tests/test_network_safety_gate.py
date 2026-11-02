import asyncio

import httpx
import pytest
from fastapi import HTTPException

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app import config
from basswiesn.app.services.protected_devices import (
    is_device_access_protected,
    protected_device_ips,
)
from basswiesn.app.services.setup_rebuild.ssh_runner import (
    SshConfig,
    build_internal_ssh_command,
)


def test_runtime_protected_ip_and_identity_are_enforced(monkeypatch):
    monkeypatch.setenv("PROTECTED_DEVICE_IPS", "192.0.2.25")
    monkeypatch.setenv("PROTECTED_DEVICE_IDS", "001122334455")
    config.get_settings.cache_clear()
    try:
        assert "192.0.2.25" in protected_device_ips()
        assert is_device_access_protected("192.0.2.25") is True
        assert is_device_access_protected("", "001122334455") is True
    finally:
        config.get_settings.cache_clear()


def test_http_client_never_receives_protected_request(monkeypatch):
    monkeypatch.setenv("PROTECTED_DEVICE_IPS", "192.0.2.25")
    config.get_settings.cache_clear()
    called = []

    async def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url)
        return httpx.Response(200, text="<unexpected />")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await SoundTouchClient(
                "192.0.2.25",
                http_client=client,
                request_purpose="safety-test",
            ).get_xml("/info")

    with pytest.raises(HTTPException) as error:
        asyncio.run(scenario())
    assert error.value.status_code == 403
    assert called == []
    config.get_settings.cache_clear()


def test_ssh_command_has_no_secret_or_user_shell_parameter(tmp_path):
    key = tmp_path / "id_test"
    key.write_text("not used by fake runner", encoding="utf-8")
    config = SshConfig(
        username="root",
        port=22,
        timeout_seconds=4,
        retry_count=0,
        password_file="",
        private_key_file=str(key),
        known_hosts_file=str(tmp_path / "known_hosts"),
        host_key_policy="strict",
    )
    command, operation = build_internal_ssh_command(
        "192.0.2.176",
        "AABBCCDDEE04",
        "common.read_ssh_state",
        config=config,
        approved_only=False,
    )
    assert operation.operation_id == "common.read_ssh_state"
    assert "not used by fake runner" not in " ".join(command)
    assert "StrictHostKeyChecking=yes" in command
    assert "UserKnownHostsFile=" in " ".join(command)
