"""A reachable error page is not a healthy BASSWIESN listener."""
import asyncio

import httpx
import pytest

from basswiesn.app.routers import api
from basswiesn.app.services.listener_health import http_service_status

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("status,body,online", [
    (200, b'{"live":true}', True),
    (200, b'{"live":false}', False),
    (200, b'{"ready":true}', False),
    (200, b'<html>login</html>', False),
    (401, b'{"live":true}', False),
    (404, b'{"live":true}', False),
    (500, b'{"live":true}', False),
])
def test_listener_requires_successful_liveness_contract(monkeypatch, status, body, online):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client(
        **options, transport=httpx.MockTransport(lambda _: httpx.Response(status, content=body))))
    result = asyncio.run(api._http_service_status("fixture", "http://127.0.0.1:8000/live"))
    assert result["online"] is online
    assert result["status_code"] == status


@pytest.mark.parametrize("target", [
    "http://192.0.2.42:8000/live", "http://localhost:8000/live",
    "http://127.0.0.1/live", "http://127.0.0.1:bad/live",
    "http://user:secret@127.0.0.1:8000/live", "https://127.0.0.1:8000/live",
    "http://127.0.0.1:8000/live?token=secret", "http://127.0.0.1:8000/about",
    "http://127.0.0.1:8000/live#fragment",
])
def test_rejected_target_creates_no_client_and_exposes_no_target(monkeypatch, target):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: pytest.fail("transport constructed"))
    result = asyncio.run(http_service_status("fixture", target))
    assert result == {"service": "fixture", "online": False, "error": "InvalidLivenessTarget"}


@pytest.mark.parametrize("body", [b'{"live":1}', b'{"live":true,"secret":"hidden"}',
                                  b'{"live":true}' + b' ' * 1024, b'\xff'])
def test_invalid_or_oversized_response_is_not_online(monkeypatch, body):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client(
        **options, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))))
    result = asyncio.run(http_service_status("fixture", "http://[::1]:8000/live"))
    assert result["online"] is False
    assert "hidden" not in str(result)


def test_timeout_does_not_leak_exception_details(monkeypatch):
    client = httpx.AsyncClient

    def fail(_request):
        raise httpx.ReadTimeout("authorization=never-export-this")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client(
        **options, transport=httpx.MockTransport(fail)))
    result = asyncio.run(http_service_status("fixture", "http://127.0.0.1:8000/live"))
    assert result["online"] is False and result["error"] == "ReadTimeout"
    assert "never-export-this" not in str(result)


def test_no_proxy_redirect_or_private_peer_in_liveness_check(monkeypatch):
    client = httpx.AsyncClient
    targets = []

    def redirect(request):
        targets.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://192.0.2.42/admin"})

    def factory(**options):
        assert options["trust_env"] is False and options["follow_redirects"] is False
        return client(**options, transport=httpx.MockTransport(redirect))

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    result = asyncio.run(http_service_status("fixture", "http://127.0.0.1:8000/live"))
    assert result["online"] is False and result["status_code"] == 302
    assert targets == ["http://127.0.0.1:8000/live"]
