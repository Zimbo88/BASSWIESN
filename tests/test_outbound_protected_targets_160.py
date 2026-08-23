from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.services import lab_tools, network_security, protected_devices
from basswiesn.app.services.logo_validation import probe_logo_reference
from basswiesn.app.services.offline_preflight import probe_stream_reference
from basswiesn.app.services.setup_rebuild.server_target import resolve_server_target
from basswiesn.app.services.ssdp_discovery import SSDPCandidate, _fetch_descriptor
from basswiesn.app.services.stream_compat import (
    ProtectedStreamTarget,
    resolve_stream_url,
)
from basswiesn.app.services.updates import fetch_manifest


pytestmark = pytest.mark.unit
PROTECTED_IP = "192.168.50.25"


def _dns(address: str):
    return [
        (
            2,
            1,
            6,
            "",
            (address, 0),
        )
    ]


def test_direct_and_dns_alias_stream_targets_never_construct_transport(monkeypatch):
    constructed: list[dict] = []

    def forbidden_client(**kwargs):
        constructed.append(kwargs)
        raise AssertionError("protected stream reached HTTP client construction")

    monkeypatch.setattr(
        "basswiesn.app.services.stream_compat.httpx.AsyncClient",
        forbidden_client,
    )
    with pytest.raises(ProtectedStreamTarget):
        asyncio.run(resolve_stream_url(f"http://{PROTECTED_IP}/live.mp3"))

    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns(PROTECTED_IP),
    )
    with pytest.raises(ProtectedStreamTarget):
        asyncio.run(resolve_stream_url("http://protected-radio.invalid/live.mp3"))
    assert constructed == []


def test_stream_redirect_is_validated_before_second_transport(monkeypatch):
    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns("93.184.216.34"),
    )
    calls: list[str] = []

    class Response:
        status_code = 302
        headers = {"location": f"http://{PROTECTED_IP}/live.mp3"}
        content = b""
        text = ""

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **_kwargs):
            calls.append(str(url))
            return Response()

    monkeypatch.setattr(
        "basswiesn.app.services.stream_compat.httpx.AsyncClient", Client
    )
    with pytest.raises(ProtectedStreamTarget):
        asyncio.run(resolve_stream_url("http://safe-stream.invalid/list.m3u"))
    assert calls == ["http://93.184.216.34/list.m3u"]


def test_update_manifest_direct_and_redirect_targets_never_reach_protected_transport(monkeypatch):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": f"http://{PROTECTED_IP}/manifest.json"},
        )

    with pytest.raises(ValueError, match="protected device"):
        asyncio.run(
            fetch_manifest(
                f"http://{PROTECTED_IP}/manifest.json",
                transport=httpx.MockTransport(handler),
            )
        )
    assert calls == []

    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns("93.184.216.34"),
    )
    with pytest.raises(ValueError, match="protected device"):
        asyncio.run(
            fetch_manifest(
                "https://updates.invalid/manifest.json",
                transport=httpx.MockTransport(handler),
            )
        )
    assert calls == ["https://93.184.216.34/manifest.json"]


def test_offline_probe_rejects_protected_redirect_without_following(monkeypatch):
    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns("93.184.216.34"),
    )
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            307,
            headers={"location": f"http://{PROTECTED_IP}/audio.mp3"},
        )

    result = asyncio.run(
        probe_stream_reference(
            "https://stream.invalid/live",
            transport=httpx.MockTransport(handler),
        )
    )
    assert result["status"] == "blockiert"
    assert "protected device" in result["reason"]
    assert calls == ["https://93.184.216.34/live"]


def test_setup_rebuild_rejects_immutable_and_configured_protected_server_hosts(monkeypatch):
    with pytest.raises(ValueError):
        resolve_server_target({"server_host": PROTECTED_IP})

    monkeypatch.setattr(
        protected_devices,
        "protected_device_ips",
        lambda: {PROTECTED_IP, "192.0.2.77"},
    )
    with pytest.raises(ValueError):
        resolve_server_target({"server_host": "192.0.2.77"})


def test_lab_tcp_probe_blocks_dns_alias_before_socket(monkeypatch):
    monkeypatch.setattr(
        lab_tools,
        "get_settings",
        lambda: SimpleNamespace(lab_mode=True),
    )
    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns(PROTECTED_IP),
    )

    def forbidden_socket(*_args, **_kwargs):
        raise AssertionError("protected DNS alias reached socket transport")

    monkeypatch.setattr(lab_tools.socket, "create_connection", forbidden_socket)
    result = lab_tools.probe_port("protected-radio.invalid", 8090)
    assert result["skipped"] is True
    assert "protected device" in result["reason"]


def test_soundtouch_and_ssdp_dns_aliases_stop_before_http_transport(monkeypatch):
    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns(PROTECTED_IP),
    )
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="<unexpected />")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(Exception):
                await SoundTouchClient(
                    "protected-radio.invalid", http_client=client
                ).get_xml("/info")
        candidate = SSDPCandidate(
            location="http://protected-radio.invalid:8090/description.xml",
            usn="uuid:test",
            server="Bose SoundTouch",
            st="ssdp:all",
            remote_ip="192.168.50.176",
        )
        ok, details, reason = await _fetch_descriptor(candidate, timeout_seconds=1)
        assert ok is False
        assert details["validation"]["addresses"] == [PROTECTED_IP]
        assert "protected device" in reason

    asyncio.run(scenario())
    assert calls == []


def test_logo_dns_alias_to_protected_device_is_not_probed(monkeypatch):
    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns(PROTECTED_IP),
    )

    class ForbiddenClient:
        def __init__(self, **_kwargs):
            raise AssertionError("protected logo alias reached HTTP transport")

    monkeypatch.setattr(
        "basswiesn.app.services.logo_validation.httpx.AsyncClient",
        ForbiddenClient,
    )
    result = asyncio.run(
        probe_logo_reference("https://protected-radio.invalid/logo.png")
    )
    assert result["verification"] == "probe_blocked"
    assert "protected device" in result["reason"]
