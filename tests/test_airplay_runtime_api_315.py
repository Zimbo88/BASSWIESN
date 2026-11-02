"""Human boundary precedes any local bridge command, including legacy LAN mode."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from basswiesn.app.core.admin_store import AdminSession
from basswiesn.app.routers import airplay_bridge as api

pytestmark = pytest.mark.integration


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    flags = SimpleNamespace(auth=True, calls=[], fail=False)
    @app.middleware('http')
    async def auth(request, call_next):
        if flags.auth:
            request.state.admin_session = AdminSession('fixture', 9999999999, 'csrf-test')
        return await call_next(request)
    app.dependency_overrides[api.get_db] = lambda: None
    monkeypatch.setattr(api, 'require_lab', lambda db: None)
    async def exchange(root, raw):
        flags.calls.append(raw)
        if flags.fail:
            raise OSError('secret-must-not-leak')
        return {'ok': True, 'result': {'state': 'WAITING'}}
    monkeypatch.setattr(api.service_control, 'exchange', exchange)
    with TestClient(app, base_url='https://testserver') as client:
        client.headers.update({'Origin': 'https://testserver', 'X-BASSWIESN-CSRF': 'csrf-test'})
        yield client, flags


@pytest.mark.parametrize('action', ['start', 'stop'])
def test_control_requires_human_auth(client, action):
    http, flags = client
    flags.auth = False
    assert http.post('/api/airplay-bridge/runtime/' + action, json={}).status_code == 401
    assert not flags.calls


@pytest.mark.parametrize('headers', [{'Origin': 'https://evil.invalid'}, {'X-BASSWIESN-CSRF': 'wrong'},
                                    {'Sec-Fetch-Site': 'cross-site'}])
def test_origin_and_csrf_before_control(client, headers):
    http, flags = client
    assert http.post('/api/airplay-bridge/runtime/start', json={}, headers=headers).status_code == 403
    assert not flags.calls


@pytest.mark.parametrize('body', [{'request_id': 'a' * 32, 'profile': '../../shell'},
    {'request_id': 'a' * 32, 'profile': 'one', 'command': 'reboot'},
    {'request_id': 'bad', 'profile': 'one'}])
def test_no_arbitrary_commands_or_profiles(client, body):
    http, flags = client
    assert http.post('/api/airplay-bridge/runtime/start', json=body).status_code == 400
    assert not flags.calls


def test_accepted_does_not_claim_playback_and_timeout_does_not_retry(client):
    http, flags = client
    body = {'request_id': 'a' * 32, 'profile': 'one'}
    result = http.post('/api/airplay-bridge/runtime/start', json=body)
    assert result.status_code == 202 and result.json()['result']['state'] == 'WAITING'
    flags.fail = True
    result = http.post('/api/airplay-bridge/runtime/start', json=body)
    assert result.status_code == 202 and result.json()['code'] == 'AIRPLAY_SUBMISSION_UNCERTAIN'
    assert len(flags.calls) == 2 and 'secret' not in result.text


def test_status_failure_is_not_provisioned_success(client):
    http, flags = client
    flags.fail = True
    result = http.get('/api/airplay-bridge/runtime/status')
    assert result.json()['available'] is False
