"""Real local Unix socket, bounded commands; no radio or host provisioning."""
import asyncio
import json
import os

import pytest

from test_airplay_session_service_315 import Worker, config, service  # noqa: F401
from basswiesn.app.services.airplay_bridge.service_control import ServiceControl, decode_request, exchange

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('raw', [b'[]', b'{}', b'{"version":true,"action":"status"}',
    b'{"version":1,"version":1,"action":"status"}',
    b'{"version":1,"action":"shell","command":"whoami"}',
    b'{"version":1,"action":"status","path":"/tmp/a"}',
    b'{"version":1,"action":"start","request_id":"bad","profile":"one"}',
    b'{"version":1,"action":"start","request_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","profile":"../../x"}',
    b'x' * 2049])
def test_strict_command_schema(raw):
    with pytest.raises((ValueError, TypeError)):
        decode_request(raw)


def test_real_socket_start_stop_and_cleanup(tmp_path, config):
    async def run():
        workers = []
        def factory(cfg):
            workers.append(Worker())
            return workers[-1]
        owner = service(tmp_path, factory)
        control = ServiceControl(owner, {'one': config})
        await control.start()
        try:
            assert control.path.stat().st_mode & 0o777 == 0o600
            value = await exchange(owner.root, b'{"version":1,"action":"status"}')
            assert value['result']['available'] and value['result']['maturity'] == 'LAB'
            assert not workers
            body = {'version': 1, 'action': 'start', 'request_id': 'a' * 32, 'profile': 'one'}
            await exchange(owner.root, json.dumps(body).encode())
            await exchange(owner.root, json.dumps(body).encode())
            assert len(workers) == 1
            reply = await exchange(owner.root, json.dumps({**body, 'profile': 'unknown'}).encode())
            assert reply == {'ok': False, 'code': 'AIRPLAY_PROFILE_UNKNOWN'}
            await exchange(owner.root, json.dumps({'version': 1, 'action': 'stop', 'request_id': 'a' * 32}).encode())
            await owner.tasks['a' * 32]
            value = await exchange(owner.root, b'{"version":1,"action":"status"}')
            assert value['result']['sessions'][0]['state'] == 'FINISHED'
            assert value['result']['profiles'][0]['consumed']
            body['request_id'] = 'b' * 32
            assert (await exchange(owner.root, json.dumps(body).encode()))['code'] == 'AIRPLAY_FRESH_PROFILE_REQUIRED'
        finally:
            await control.close()
        assert not control.path.exists()
    asyncio.run(run())


def test_client_rejects_untrusted_peer_before_start(tmp_path, config, monkeypatch):
    from basswiesn.app.services.airplay_bridge import unix_ipc
    async def run():
        owner = service(tmp_path, lambda cfg: pytest.fail('untrusted request must not execute'))
        control = ServiceControl(owner, {'one': config})
        await control.start()
        try:
            # Keep filesystem checks truthful, but the client demands another
            # UID. SO_PEERCRED is checked before transmitting any command.
            client = unix_ipc.UnixReceiverIPC(control.path, peer_uid=os.geteuid() + 1)
            with pytest.raises(ValueError, match='PEER_MISMATCH'):
                await client.exchange(b'{"version":1,"action":"status"}')
            assert not owner.entries
        finally:
            await control.close()
    asyncio.run(run())


def test_dispatch_redacts_preflight_exception(tmp_path, config):
    def fail(cfg):
        raise ValueError('super-secret-relay-url')
    async def run():
        owner = service(tmp_path, lambda cfg: Worker(), fail)
        control = ServiceControl(owner, {'one': config})
        result = control.dispatch(json.dumps({'version': 1, 'action': 'start',
            'profile': 'one', 'request_id': 'a' * 32}).encode())
        assert result == {'ok': False, 'code': 'AIRPLAY_PREFLIGHT_REJECTED'}
        await owner.close()
    asyncio.run(run())


def test_socket_removed_on_journal_failure_but_recovery_evidence_remains(tmp_path, config, monkeypatch):
    from basswiesn.app.services.airplay_bridge import session_service
    async def run():
        worker = Worker()
        owner = service(tmp_path, lambda cfg: worker)
        control = ServiceControl(owner, {'one': config})
        await control.start()
        owner.start('a' * 32, config)
        await asyncio.sleep(0)
        def full(*args, **kwargs):
            raise OSError('disk full')
        monkeypatch.setattr(session_service, '_state_file', full)
        with pytest.raises(RuntimeError, match='JOURNAL_FAILED'):
            await control.close()
        assert worker.stop.is_set() and not worker.cancelled
        assert not control.path.exists() and owner.journal.exists()
        assert json.loads(owner.journal.read_text())['sessions']['a' * 32]['state'] == 'STARTING'
    asyncio.run(run())
