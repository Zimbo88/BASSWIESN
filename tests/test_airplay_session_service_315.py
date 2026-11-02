"""Durable lifecycle tests: no network, Docker, radio or synthetic hardware PASS."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from basswiesn.app.services.airplay_bridge.pilot_runner import PilotConfig, PilotDevice
from basswiesn.app.services.airplay_bridge.session_service import SessionService, SessionConflict

pytestmark = pytest.mark.unit


@pytest.fixture
def config(tmp_path):
    return PilotConfig(data_dir=tmp_path, database_path=tmp_path / 'db.sqlite',
        devices=(PilotDevice('AP2-ONE', '192.0.2.41', ('AP2-ONE', '192.0.2.41', 'model', 'fw', 'variant'),
                             tmp_path / 'receiver.sock'),),
        master='AP2-ONE', receiver_binary_sha256='b' * 64, controller_nonce='c' * 32,
        relay_url='http://192.0.2.88:8098/relay/' + 'd' * 64 + '.mp3',
        base_url='http://192.0.2.80:1517', listen_host='192.0.2.80', listen_port=1517,
        lifetime_seconds=30, wait_seconds=2, test_volume=1, ipc_peer_uid=0,
        status_path=tmp_path / 'status.json', receipt_path=tmp_path / 'receipt.json',
        stop_path=tmp_path / 'stop')


class Worker:
    def __init__(self, fail=False, restored=True):
        self.stop = asyncio.Event()
        self.allow_finish = asyncio.Event()
        self.allow_finish.set()
        self.receipt = {'status': 'WAITING'}
        self.fail, self.restored, self.cancelled = fail, restored, False

    async def run(self):
        try:
            await self.stop.wait()
            await self.allow_finish.wait()
            if self.fail:
                raise RuntimeError('secret-token-must-not-escape')
            return {'status': 'FINISHED', 'restore': 'CAPTURED_STATE_RESTORED' if self.restored else 'MANUAL_REQUIRED',
                    'station_cleanup': 'DELETED_EXACT_OWNED_ROW', 'backup_ids': [1],
                    'unknown-secret': 'never-export-this'}
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def service(tmp_path, factory, preflight=lambda config: None):
    root = tmp_path / 'runtime'
    root.mkdir(mode=0o700, exist_ok=True)
    return SessionService(root, factory, preflight=preflight)


def test_durable_reservation_idempotency_and_verified_stop(tmp_path, config):
    workers = []
    def factory(cfg):
        saved = json.loads((tmp_path / 'runtime/sessions.json').read_text())
        assert saved['sessions']['a' * 32]['state'] == 'STARTING'
        workers.append(Worker())
        return workers[-1]
    async def run():
        owner = service(tmp_path, factory)
        try:
            owner.start('a' * 32, config)
            owner.start('a' * 32, config)
            await asyncio.sleep(0)
            assert len(workers) == 1
            assert owner.status()['sessions'][0]['state'] == 'WAITING'
            with pytest.raises(SessionConflict, match='REQUEST_ID_REUSED'):
                owner.start('a' * 32, replace(config, test_volume=5))
            owner.stop('a' * 32)
            owner.stop('a' * 32)
            await owner.tasks['a' * 32]
            assert owner.status()['sessions'][0]['state'] == 'FINISHED'
            owner.start('a' * 32, config)
            assert len(workers) == 1
            serialized = (tmp_path / 'runtime/sessions.json').read_text() + json.dumps(owner.status())
            for forbidden in (config.relay_url, config.controller_nonce, 'unknown-secret', 'never-export-this'):
                assert forbidden not in serialized
        finally:
            await owner.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure', [True, False])
def test_failure_or_unverified_restore_reserves_target_across_restart(tmp_path, config, failure):
    async def run():
        owner = service(tmp_path, lambda cfg: Worker(fail=failure, restored=False))
        owner.start('a' * 32, config)
        await asyncio.sleep(0)
        await owner.close()
        assert owner.entries['a' * 32]['state'] == 'RECOVERY_REQUIRED'
        other = service(tmp_path, lambda cfg: pytest.fail('must not restart'))
        try:
            with pytest.raises(SessionConflict, match='BUSY_OR_UNRECONCILED'):
                other.start('b' * 32, config)
        finally:
            await other.close()
    asyncio.run(run())


def test_crash_record_is_never_resumed(tmp_path, config):
    async def run():
        owner = service(tmp_path, lambda cfg: pytest.fail('no worker before journal check'))
        # Durable STARTING models process death after reserving but before a
        # result. Close the test lock only; no worker was ever constructed.
        from basswiesn.app.services.airplay_bridge.session_service import configuration_digest, grant_digests
        owner.entries['a' * 32] = {'state': 'STARTING', 'targets': ['AP2-ONE'],
            'digest': configuration_digest(config), 'grants': grant_digests(config), 'receipt': {}}
        owner._save()
        os.close(owner.fd)
        owner.closed = True
        other = service(tmp_path, lambda cfg: pytest.fail('crashed sessions cannot autorun'))
        try:
            assert other.entries['a' * 32]['state'] == 'RECOVERY_REQUIRED'
            with pytest.raises(SessionConflict):
                other.start('b' * 32, config)
        finally:
            await other.close()
    asyncio.run(run())


def test_same_targets_or_listener_cannot_run_twice(tmp_path, config):
    async def run():
        owner = service(tmp_path, lambda cfg: Worker())
        try:
            owner.start('a' * 32, config)
            with pytest.raises(SessionConflict, match='BUSY_OR_UNRECONCILED'):
                owner.start('b' * 32, config)
            device = replace(config.devices[0], device_id='AP2-TWO', ip_address='192.0.2.42',
                fingerprint=('AP2-TWO', '192.0.2.42', 'model', 'fw', 'variant'),
                receiver_socket=tmp_path / 'second.sock')
            with pytest.raises(SessionConflict, match='RESOURCE_ALREADY_OWNED'):
                owner.start('c' * 32, replace(config, master='AP2-TWO', devices=(device,),
                    controller_nonce='e' * 32, relay_url=config.relay_url.replace('d' * 64, 'f' * 64)))
        finally:
            await owner.close()
    asyncio.run(run())


def test_close_timeout_does_not_cancel_restore_or_release_lock(tmp_path, config):
    async def run():
        worker = Worker()
        worker.allow_finish.clear()
        owner = service(tmp_path, lambda cfg: worker)
        owner.start('a' * 32, config)
        await asyncio.sleep(0)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(owner.close(), .02)
        assert not worker.cancelled and not owner.tasks['a' * 32].done()
        with pytest.raises(BlockingIOError):
            service(tmp_path, lambda cfg: Worker())
        worker.allow_finish.set()
        await owner.close()
        assert not worker.cancelled
        other = service(tmp_path, lambda cfg: Worker())
        await other.close()
    asyncio.run(run())


def test_preflight_rejection_has_no_worker_or_journal_side_effect(tmp_path, config):
    def denied(cfg):
        raise ValueError('DEVICE_PROTECTED')
    async def run():
        owner = service(tmp_path, lambda cfg: pytest.fail('protected worker'), denied)
        try:
            with pytest.raises(ValueError, match='DEVICE_PROTECTED'):
                owner.start('a' * 32, config)
            assert owner.entries == {} and not owner.journal.exists()
        finally:
            await owner.close()
    asyncio.run(run())


def test_stop_before_worker_creation_opens_no_transport(tmp_path, config):
    async def run():
        owner = service(tmp_path, lambda cfg: pytest.fail('stopped before worker creation'))
        owner.start('a' * 32, config)
        owner.stop('a' * 32)
        await owner.close()
        assert owner.entries['a' * 32]['state'] == 'FINISHED'
        assert owner.entries['a' * 32]['receipt']['restore'] == 'NO_RADIO_WRITE_ATTEMPTED'
    asyncio.run(run())


def test_unsafe_directory_and_journal_are_rejected(tmp_path):
    root = tmp_path / 'runtime'
    root.mkdir(mode=0o755)
    # Earlier isolation tests can leave a restrictive process umask. Explicitly
    # establish the unsafe fixture instead of assuming mkdir bypasses umask.
    root.chmod(0o755)
    with pytest.raises(ValueError):
        SessionService(root, lambda cfg: Worker(), preflight=lambda cfg: None)
    root.chmod(0o700)
    (root / 'sessions.json').symlink_to(tmp_path / 'missing')
    with pytest.raises((ValueError, OSError)):
        SessionService(root, lambda cfg: Worker(), preflight=lambda cfg: None)


def test_disk_failure_still_stops_worker_and_never_claims_clean_shutdown(tmp_path, config, monkeypatch):
    from basswiesn.app.services.airplay_bridge import session_service
    async def run():
        worker = Worker()
        owner = service(tmp_path, lambda cfg: worker)
        owner.start('a' * 32, config)
        await asyncio.sleep(0)
        def fail(*args, **kwargs):
            raise OSError('disk full')
        monkeypatch.setattr(session_service, '_state_file', fail)
        with pytest.raises(OSError):
            owner.stop('a' * 32)
        assert worker.stop.is_set() and owner.storage_failed
        with pytest.raises(SessionConflict):
            owner.start('b' * 32, config)
        with pytest.raises(RuntimeError, match='JOURNAL_FAILED'):
            await owner.close()
        assert not worker.cancelled and owner.tasks['a' * 32].done()
    asyncio.run(run())


def test_completed_grants_cannot_be_reused_by_changing_configuration(tmp_path, config):
    async def run():
        owner = service(tmp_path, lambda cfg: Worker())
        owner.start('a' * 32, config)
        await asyncio.sleep(0)
        await owner.close()
        other = service(tmp_path, lambda cfg: Worker())
        try:
            # Neither a different filename nor a different volume revives an
            # old URL, even when the controller nonce has already been rotated.
            changed = replace(config, test_volume=5, receipt_path=tmp_path / 'new.json',
                              controller_nonce='e' * 32)
            with pytest.raises(SessionConflict, match='FRESH_PROFILE_REQUIRED'):
                other.start('b' * 32, changed)
            with pytest.raises(SessionConflict, match='FRESH_PROFILE_REQUIRED'):
                other.start('c' * 32, replace(config,
                    relay_url=config.relay_url.replace('d' * 64, 'f' * 64)))
            fresh = replace(changed, relay_url=config.relay_url.replace('d' * 64, 'f' * 64))
            other.start('d' * 32, fresh)
            await asyncio.sleep(0)
            assert other.status()['sessions'][-1]['state'] == 'WAITING'
        finally:
            await other.close()
    asyncio.run(run())


def test_journal_loss_stops_queued_workers_before_factory(tmp_path, config, monkeypatch):
    from basswiesn.app.services.airplay_bridge import session_service
    async def run():
        owner = service(tmp_path, lambda cfg: pytest.fail('queued worker must not touch radios'))
        owner.start('a' * 32, config)
        def full(*args, **kwargs):
            raise OSError('disk full')
        monkeypatch.setattr(session_service, '_state_file', full)
        with pytest.raises(OSError):
            owner._save()
        assert owner.entries['a' * 32]['state'] == 'STOPPING'
        with pytest.raises(RuntimeError, match='JOURNAL_FAILED'):
            await owner.close()
        assert owner.entries['a' * 32]['receipt']['restore'] == 'NO_RADIO_WRITE_ATTEMPTED'
    asyncio.run(run())
