import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from basswiesn.app.core import admin_requests
from basswiesn.app.main import create_web_app
from test_admin_boundary import secured, authenticate  # noqa: F401

pytestmark = pytest.mark.integration


def request(raw=b'{}', headers=(), receive=None):
    async def body():
        return {'type': 'http.request', 'body': raw, 'more_body': False}
    return Request({'type': 'http', 'headers': list(headers)}, receive=receive or body)


@pytest.mark.parametrize('raw,headers', [
    (b'{"name":"private-canary","name":"other"}', ()),
    (b'{"a":NaN}', ()), (b'{"a":Infinity}', ()), (b'[]', ()), (b'\xff', ()),
    (b'[' * 2000, ()), (b'x' * 33, ()),
    (b'{}', ((b'content-length', b'1'),)),
    (b'{}', ((b'content-length', b'2'), (b'content-length', b'2'))),
    (b'{}', ((b'content-length', b'-1'),)),
    (b'{}', ((b'content-length', b'9' * 20),)),
    (b'{}', ((b'content-encoding', b'gzip'),)),
])
def test_invalid_credential_body_is_rejected_without_echo(raw, headers):
    with pytest.raises(ValueError, match='invalid administration request') as caught:
        asyncio.run(admin_requests.read_admin_object(request(raw, headers), limit=32))
    assert 'private-canary' not in str(caught.value)


def test_whole_request_deadline_and_disconnect(monkeypatch):
    monkeypatch.setattr(admin_requests, 'BODY_TIMEOUT', 0.01)
    async def slow():
        await asyncio.Event().wait()
    async def disconnected():
        return {'type': 'http.disconnect'}
    for receive in (slow, disconnected):
        with pytest.raises(ValueError):
            asyncio.run(admin_requests.read_admin_object(request(receive=receive), limit=32))


def test_chunked_unicode_request_and_cancellation():
    async def exercise():
        chunks = iter((b'{"name":"\xc3', b'\xa4"}'))
        async def receive():
            chunk = next(chunks)
            return {'type': 'http.request', 'body': chunk, 'more_body': not chunk.endswith(b'}')}
        assert await admin_requests.read_admin_object(request(receive=receive), limit=32) == {'name': 'ä'}
        async def cancel():
            raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await admin_requests.read_admin_object(request(receive=cancel), limit=32)
    asyncio.run(exercise())


def test_duplicate_login_fields_never_reach_password_verification(secured, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('ambiguous credentials reached password verification')
    monkeypatch.setattr(type(secured[1]), 'login', forbidden)
    with TestClient(create_web_app(background_tasks=False), base_url='https://testserver') as client:
        response = client.post('/auth/login', headers={'Origin': 'https://testserver'},
            content='{"username":"operator","password":"private-canary","password":"other"}')
    assert response.status_code == 400 and 'private-canary' not in response.text


def test_duplicate_scope_fields_cannot_mint_token(secured):
    with TestClient(create_web_app(background_tasks=False), base_url='https://testserver') as client:
        csrf = authenticate(client)
        response = client.post('/auth/tokens', headers={'Origin': 'https://testserver', 'X-BASSWIESN-CSRF': csrf},
            content='{"name":"test","scopes":["read"],"scopes":["admin"],"lifetime_seconds":3600}')
        assert response.status_code == 400
        assert client.get('/auth/tokens').json() == {'tokens': []}
