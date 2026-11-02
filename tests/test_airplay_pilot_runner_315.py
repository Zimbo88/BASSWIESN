"""Bounded application composition with synthetic receivers/radios only."""
import asyncio
from dataclasses import asdict, replace
import errno
import json
import os
from pathlib import Path
import time
import sqlite3

import pytest
from sqlalchemy.exc import OperationalError

from basswiesn.app import db as app_db
from basswiesn.app.config import get_settings
from basswiesn.app.models import Device, Preset, Station
from basswiesn.app.services.airplay_bridge import pilot_runner as runner
from basswiesn.app.services.device_transactions import device_fingerprint
from test_airplay_group_controller_315 import Namespace, apple_event, BINARY, NONCE, REAL_SLEEP
from test_airplay_group_session_315 import MASTER, MEMBER, GROUP
from test_airplay_group_zone_315 import rig
import test_airplay_group_zone_315 as zone_fixture
from test_airplay_owned_pilot_315 import simulated_source_ports

pytestmark = pytest.mark.integration


class FakeServer:
    instances = []
    fail = False

    def __init__(self, provider, config):
        self.provider, self.live = provider, False
        self.instances.append(self)

    async def start(self):
        if self.fail:
            raise OSError('private bind failure must not appear in receipts')
        self.live = True

    async def close(self):
        self.live = False


@pytest.fixture
def setup(rig, monkeypatch, tmp_path):
    monkeypatch.setattr(asyncio, 'sleep', REAL_SLEEP)
    monkeypatch.setattr(zone_fixture, 'ADDRESSES', {MASTER:'192.0.2.41', MEMBER:'192.0.2.42'})
    monkeypatch.setattr(FakeServer, 'instances', [])
    monkeypatch.setattr(FakeServer, 'fail', False)
    monkeypatch.setattr(runner, 'ProviderServer', FakeServer)
    state = tmp_path / 'private'
    state.mkdir(mode=0o700)
    (tmp_path / 'device-locks').mkdir(mode=0o700)
    with app_db.SessionLocal() as db:
        devices = db.query(Device).filter(Device.device_id.in_([MASTER, MEMBER])).all()
        for device in devices:
            device.capabilities_xml = '<supportedURLs><getZone/><setZone/><rebroadcastlatencymode/></supportedURLs>'
        db.commit()
        targets = tuple(runner.PilotDevice(item.device_id, item.ip_address,
            device_fingerprint(item), state / (item.device_id + '.sock')) for item in devices)
    config = runner.PilotConfig(data_dir=tmp_path, database_path=tmp_path / 'test.db',
        devices=targets, master=MASTER, receiver_binary_sha256=BINARY, controller_nonce=NONCE,
        relay_url='http://192.0.2.88:8098/relay/'+'a'*64+'.mp3', base_url='http://192.0.2.80:1517',
        listen_host='192.0.2.80', listen_port=1517, lifetime_seconds=30, wait_seconds=2,
        test_volume=1, ipc_peer_uid=0, status_path=state/'STATUS.json', receipt_path=state/'RECEIPT.json',
        stop_path=state/'STOP')
    channels = {key: Namespace(rig, key, clock=time.monotonic) for key in (MASTER, MEMBER)}
    for channel in channels.values():
        original = channel.exchange
        async def exchange(raw, original=original):
            value = json.loads(await original(raw))
            if json.loads(raw)['operation'] == 'observe':
                observation = value['observation']
                observation['display'].update(version=1, sequence=observation['heartbeat_sequence'],
                    observed_monotonic=time.monotonic(), active=True, fields={'track':'Fixture song'})
            return json.dumps(value).encode()
        channel.exchange = exchange
    monkeypatch.setattr(runner, 'UnixReceiverIPC', lambda path, **_: channels[path.stem])
    ports, states = simulated_source_ports(rig)
    owner = runner.PilotRunner(config, app_db.SessionLocal, ports=ports)
    return owner, rig, channels, states


async def until(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await REAL_SLEEP(.01)


@pytest.mark.parametrize('test_volume', [1, 5, 10])
def test_real_composition_waits_for_both_native_inputs_and_restores(setup, test_volume):
    owner, rig, channels, states = setup
    owner.config = replace(owner.config, test_volume=test_volume)
    before = dict(rig.volumes)
    async def run():
        task = asyncio.create_task(owner.run())
        await until(lambda: owner.receipt['status'] == 'WAITING')
        assert FakeServer.instances[0].live and channels[MASTER].commands
        assert not rig.constructed and not rig.posts
        apple_event(rig, {MASTER:GROUP})
        await REAL_SLEEP(.65)
        assert not rig.constructed and not rig.posts
        apple_event(rig, {MEMBER:GROUP})
        await until(lambda: channels[MASTER].releases)
        assert owner.receipt['status'] == 'ACTIVE'
        assert {rig.volumes[key] for key in (MASTER, MEMBER)} == {test_volume}
        assert all(value == before[key] for key, value in rig.volumes.items() if key not in (MASTER, MEMBER))
        apple_event(rig, {MASTER:'', MEMBER:''})
        receipt = await asyncio.wait_for(task, 15)
        assert receipt['restore'] == 'CAPTURED_STATE_RESTORED'
        assert receipt['station_cleanup'] == 'DELETED_EXACT_OWNED_ROW'
        assert receipt['backup_ids']
        assert not FakeServer.instances[0].live
        assert rig.volumes == before
        assert sum(path == '/select' for _, path, _ in rig.posts) == 1
        with app_db.SessionLocal() as db:
            assert db.query(Station).count() == 0
        text = owner.config.receipt_path.read_text()
        assert owner.config.relay_url not in text and owner.config.controller_nonce not in text
    asyncio.run(run())


def test_stop_while_waiting_is_transport_free_and_receipt_is_private(setup):
    owner, rig, _, _ = setup
    async def run():
        task = asyncio.create_task(owner.run())
        await until(lambda: owner.receipt['status'] == 'WAITING')
        owner.config.stop_path.touch(mode=0o600)
        receipt = await task
        assert receipt['status'] == 'FINISHED'
        assert receipt['restore'] == 'NO_RADIO_WRITE_ATTEMPTED'
        assert not rig.constructed and not rig.posts
        assert owner.config.receipt_path.stat().st_mode & 0o777 == 0o600
    asyncio.run(run())


def test_single_receiver_is_independently_usable_and_restored(setup):
    owner, rig, channels, _ = setup
    owner.config = replace(owner.config,
        devices=tuple(item for item in owner.config.devices if item.device_id == MASTER))
    owner.config.validate()
    before = dict(rig.volumes)

    async def run():
        task = asyncio.create_task(owner.run())
        try:
            await until(lambda: owner.receipt['status'] == 'WAITING')
            assert not rig.constructed and not rig.posts
            apple_event(rig, {MASTER: GROUP})
            await until(lambda: channels[MASTER].releases)
            assert owner.receipt['status'] == 'ACTIVE'
            assert MEMBER not in rig.constructed
            assert rig.volumes[MEMBER] == before[MEMBER]
            owner.stop.set()
            receipt = await asyncio.wait_for(task, 15)
            assert receipt['restore'] == 'CAPTURED_STATE_RESTORED'
            assert receipt['station_cleanup'] == 'DELETED_EXACT_OWNED_ROW'
            assert rig.volumes == before
            assert all(identity != MEMBER for identity, _, _ in rig.posts)
        finally:
            owner.stop.set()
            await asyncio.wait_for(task, 15)
    asyncio.run(run())


def test_service_controls_real_runner_and_restores_before_releasing_targets(setup, tmp_path):
    from basswiesn.app.services.airplay_bridge.session_service import SessionService
    from basswiesn.app.services.airplay_bridge.service_control import ServiceControl, exchange
    owner, rig, channels, _ = setup
    owner.manage_signals = False
    owner.config = replace(owner.config,
        devices=tuple(item for item in owner.config.devices if item.device_id == MASTER))
    root = tmp_path / 'runtime'
    root.mkdir(mode=0o700)
    before = dict(rig.volumes)
    constructed = []
    def create(config):
        constructed.append(config)
        return owner
    def preflight(config):
        with app_db.SessionLocal() as db:
            runner.preflight(db, config)
    async def run():
        service = SessionService(root, create, preflight=preflight)
        control = ServiceControl(service, {'solo': owner.config})
        await control.start()
        try:
            status = await exchange(root, b'{"version":1,"action":"status"}')
            assert status['ok'] and not rig.constructed and not constructed
            body = json.dumps({'version': 1, 'action': 'start', 'request_id': 'a' * 32,
                               'profile': 'solo'}).encode()
            assert (await exchange(root, body))['ok']
            assert (await exchange(root, body))['ok']
            await until(lambda: owner.receipt['status'] == 'WAITING')
            assert len(constructed) == 1 and not rig.posts
            apple_event(rig, {MASTER: GROUP})
            await until(lambda: channels[MASTER].releases)
            assert service.status()['sessions'][0]['state'] == 'ACTIVE'
            await exchange(root, json.dumps({'version': 1, 'action': 'stop',
                                             'request_id': 'a' * 32}).encode())
            await asyncio.wait_for(service.tasks['a' * 32], 15)
            result = (await exchange(root, b'{"version":1,"action":"status"}'))['result']
            assert result['sessions'][0]['state'] == 'FINISHED'
            assert result['sessions'][0]['receipt']['restore'] == 'CAPTURED_STATE_RESTORED'
            assert result['hardware_acceptance'] == 'NOT_ESTABLISHED'
            assert rig.volumes == before and MEMBER not in rig.constructed
            with app_db.SessionLocal() as db:
                assert db.query(Station).count() == 0
        finally:
            await control.close()
        assert not control.path.exists()
    asyncio.run(run())


def test_explicit_stop_of_active_group_restores_without_cancelling_owner(setup):
    owner, rig, channels, states = setup
    before = dict(rig.volumes)
    async def run():
        task = asyncio.create_task(owner.run())
        await until(lambda: owner.receipt['status'] == 'WAITING')
        apple_event(rig, {MASTER:GROUP, MEMBER:GROUP})
        await until(lambda: channels[MASTER].releases)
        owner.stop.set()
        receipt = await asyncio.wait_for(task, 15)
        assert receipt['status'] == 'FINISHED'
        assert receipt['restore'] == 'CAPTURED_STATE_RESTORED'
        assert receipt['station_cleanup'] == 'DELETED_EXACT_OWNED_ROW'
        assert receipt['stop_reason'] == 'STOP_REQUESTED'
        assert not owner.owner.cancelled() and not owner.owner.cancelling()
        assert rig.volumes == before
        assert not channels[MASTER].commands[-1]['allow_audio']
        assert not FakeServer.instances[0].live
    asyncio.run(run())


@pytest.mark.parametrize('slow_volume', [False, True])
def test_independent_apple_volume_mute_resume_and_stop_restore(setup, monkeypatch, slow_volume):
    from basswiesn.app.services.airplay_bridge.pilot_source import PilotSource
    from basswiesn.app.services.airplay_bridge.display_session import display_metadata
    previous = PilotSource.volume_write
    delayed = []
    delay_completed = asyncio.Event()
    async def volume_write(source, device, value, expected):
        await previous(source, device, value, expected)
        if slow_volume and value == 0 and not delayed and source.display is not None:
            delayed.append(source.display._record.session)
            # Actual wall-clock delay while receiver monitoring stays alive.
            await REAL_SLEEP(10.1)
            delay_completed.set()
    monkeypatch.setattr(PilotSource, 'volume_write', volume_write)
    owner, rig, channels, states = setup
    # This scenario deliberately spends over ten seconds inside a write and
    # then exercises several independent commands plus restoration. Its total
    # test budget must not race the unrelated short-pilot expiry on a busy CI
    # host. Production foreground/lease deadlines are deliberately unchanged.
    owner.config = replace(owner.config, test_volume=10, radio_volume_forwarding=True,
                           lifetime_seconds=60 if slow_volume else 30)
    before = dict(rig.volumes)
    for channel in channels.values():
        channel.volume = {'sequence':1, 'airplay_db':-15.0}
    async def run():
        task = asyncio.create_task(owner.run())
        async def reached(identity, value):
            try:
                await until(lambda: rig.volumes[identity] == value or task.done())
            except TimeoutError:
                pytest.fail(f"volume readback timed out: expected={value}, observed={rig.volumes[identity]}, receipt={owner.receipt}")
            assert not task.done(), owner.receipt
            assert rig.volumes[identity] == value, owner.receipt
        def current_display():
            # Fresh transaction: a previous Session must not cache the row
            # before the foreground publishes its verified metadata snapshot.
            with app_db.SessionLocal() as db:
                station = db.query(Station).filter_by(purpose='airplay_bridge').one()
                device = db.query(Device).filter_by(device_id=MASTER).one()
                return display_metadata(db, device, station)
        try:
            await until(lambda: owner.receipt['status'] == 'WAITING')
            apple_event(rig, {MASTER:GROUP, MEMBER:GROUP})
            await until(lambda: channels[MASTER].releases)
            assert rig.volumes[MASTER] == rig.volumes[MEMBER] == 10
            command_boundary = len(channels[MASTER].commands)
            channels[MEMBER].volume = {'sequence':2, 'airplay_db':-144.0}
            await reached(MEMBER, 0)
            assert rig.volumes[MASTER] == 10
            assert not rig.held
            if slow_volume:
                # Observe completion of the injected slow write, not a guessed
                # sleep duration relative to the first radio-side mutation.
                await asyncio.wait_for(delay_completed.wait(), 20)
                await until(lambda: owner.source.expected_volume[MEMBER] == 0 or task.done())
                assert not task.done() and delayed
                # Volume readback precedes topology/source validation and
                # metadata publication. Observe that separate completion too.
                await until(lambda: task.done() or bool(current_display()))
                assert not task.done(), owner.receipt
                assert current_display()
            channels[MEMBER].volume = {'sequence':3, 'airplay_db':-29.1}
            await reached(MEMBER, 3)
            assert rig.volumes[MASTER] == 10
            channels[MASTER].volume = {'sequence':2, 'airplay_db':-144.0}
            await until(lambda: rig.volumes[MASTER] == 0 or task.done())
            assert not task.done(), owner.receipt
            assert rig.volumes[MEMBER] == 3
            assert all(command['allow_audio'] for command in channels[MASTER].commands[command_boundary:])
            owner.stop.set()
            receipt = await asyncio.wait_for(task, 15)
            assert receipt['status'] == 'FINISHED'
            assert receipt['restore'] == 'CAPTURED_STATE_RESTORED'
            assert rig.volumes == before
            assert receipt['station_cleanup'] == 'DELETED_EXACT_OWNED_ROW'
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('bad_mode', ['software', 'missing'])
def test_forwarding_without_attested_mode_or_slider_never_releases(setup, bad_mode):
    owner, rig, channels, _ = setup
    owner.config = replace(owner.config, radio_volume_forwarding=True)
    for channel in channels.values():
        channel.volume = None if bad_mode == 'missing' else {'sequence':1, 'airplay_db':0.0}
        channel.volume_mode = 'radio' if bad_mode == 'missing' else 'software'
    async def run():
        task = asyncio.create_task(owner.run())
        await until(lambda: owner.receipt['status'] == 'WAITING')
        apple_event(rig, {MASTER:GROUP, MEMBER:GROUP})
        receipt = await asyncio.wait_for(task, 15)
        assert receipt['status'] == 'MANUAL_REQUIRED'
        assert not channels[MASTER].releases
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['timeout', 'mismatch'])
def test_uncertain_live_volume_holds_audio_without_replaying_write(setup, monkeypatch, failure):
    from basswiesn.app.services.airplay_bridge.pilot_source import PilotSource
    previous = PilotSource.volume_write
    owner, rig, channels, _ = setup
    owner.config = replace(owner.config, test_volume=10, radio_volume_forwarding=True)
    for channel in channels.values():
        channel.volume = {'sequence':1, 'airplay_db':-15.0}
    async def changed(source, device, value, expected):
        await previous(source, device, value, expected)
        if device.device_id == MEMBER and value == 0:
            if failure == 'timeout':
                raise TimeoutError('simulated missing confirmation')
            rig.volumes[MEMBER] = 2
    monkeypatch.setattr(PilotSource, 'volume_write', changed)
    async def run():
        task = asyncio.create_task(owner.run())
        try:
            await until(lambda: owner.receipt['status'] == 'WAITING')
            apple_event(rig, {MASTER:GROUP, MEMBER:GROUP})
            await until(lambda: channels[MASTER].releases)
            channels[MEMBER].volume = {'sequence':2, 'airplay_db':-144.0}
            receipt = await asyncio.wait_for(task, 15)
            assert receipt['status'] == 'MANUAL_REQUIRED'
            assert not channels[MASTER].commands[-1]['allow_audio']
            assert sum(key == MEMBER and path == '/volume' and xml == '<volume>0</volume>'
                       for key, path, xml in rig.posts) == 1
            assert rig.volumes[MEMBER] == (0 if failure == 'timeout' else 2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_stop_received_during_startup_cannot_race_into_source(setup):
    owner, rig, _, _ = setup
    apple_event(rig, {MASTER:GROUP, MEMBER:GROUP})
    owner.stop.set()
    receipt = asyncio.run(owner.run())
    assert receipt['restore'] == 'NO_RADIO_WRITE_ATTEMPTED'
    assert not rig.constructed and not rig.posts


def test_target_drift_while_waiting_is_rejected_before_transport(setup):
    owner, rig, _, _ = setup
    async def run():
        task = asyncio.create_task(owner.run())
        await until(lambda: owner.receipt['status'] == 'WAITING')
        with app_db.SessionLocal() as db:
            db.query(Device).filter_by(device_id=MEMBER).one().ip_address = '192.0.2.99'
            db.commit()
        apple_event(rig, {MASTER:GROUP, MEMBER:GROUP})
        receipt = await task
        assert receipt['status'] == 'FAILED'
        assert receipt['error_code'] == 'AIRPLAY_DATABASE_TARGET_MISMATCH'
        assert not rig.constructed and not rig.posts
    asyncio.run(run())


def test_cancelled_outer_task_waits_for_source_restoration(setup, monkeypatch):
    owner, rig, _, _ = setup
    entered, cleanup, proceed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_scope = runner.pilot_source_scope
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def source(*args, **kwargs):
        try:
            entered.set()
            await asyncio.Event().wait()
            async with real_scope(*args, **kwargs) as value:
                yield value
        finally:
            cleanup.set()
            await proceed.wait()
    monkeypatch.setattr(runner, 'pilot_source_scope', source)
    async def run():
        task = asyncio.create_task(owner.run())
        await until(lambda: owner.receipt['status'] == 'WAITING')
        apple_event(rig, {MASTER:GROUP, MEMBER:GROUP})
        await entered.wait()
        task.cancel()
        await cleanup.wait()
        assert not task.done() and not owner.config.receipt_path.exists()
        proceed.set()
        receipt = await task
        assert receipt['restore'] == 'NO_RADIO_WRITE_ATTEMPTED'
        assert not rig.posts
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['fingerprint', 'provider', 'receiver'])
def test_failed_startup_never_constructs_radio_transports(setup, monkeypatch, failure):
    owner, rig, channels, _ = setup
    if failure == 'fingerprint':
        with app_db.SessionLocal() as db:
            device = db.query(Device).filter_by(device_id=MEMBER).one()
            device.ip_address = '192.0.2.99'
            db.commit()
    elif failure == 'provider':
        monkeypatch.setattr(FakeServer, 'fail', True)
    else:
        channels[MEMBER].fail = True
    receipt = asyncio.run(owner.run())
    assert receipt['status'] == 'FAILED'
    assert receipt['startup_phase'] == {'fingerprint':'DB_PREFLIGHT',
        'provider':'PROVIDER_BIND', 'receiver':'RECEIVER_BIND'}[failure]
    assert not rig.posts and not rig.constructed
    assert 'private bind failure' not in json.dumps(receipt)
    with app_db.SessionLocal() as db:
        assert db.query(Station).count() == 0


def test_station_with_new_preset_reference_is_not_removed(setup):
    owner, _, _, _ = setup
    async def run():
        task = asyncio.create_task(owner.run())
        await until(lambda: owner.receipt['status'] == 'WAITING')
        with app_db.SessionLocal() as db:
            db.add(Preset(device_id=MASTER, button=1, station_id=owner.receipt['station_id']))
            db.commit()
        owner.stop.set()
        receipt = await task
        assert receipt['station_cleanup'] == 'RETAINED'
        with app_db.SessionLocal() as db:
            assert db.query(Station).count() == 1
    asyncio.run(run())


def test_check_only_readonly_no_state_no_transport(setup, monkeypatch):
    owner, rig, channels, _ = setup
    monkeypatch.setattr(runner, 'get_settings', lambda:get_settings().model_copy(
        update={'data_dir':owner.config.data_dir}))
    with app_db.SessionLocal() as db:
        for device in db.query(Device).all():
            device.capabilities_xml = ''
        db.commit()
    receipt = runner.check_only(owner.config)
    assert receipt['status'] == 'PREFLIGHT_OK'
    assert receipt['capability_check'] == 'PENDING_LIVE_READBACK'
    assert not rig.constructed and not any(channel.commands for channel in channels.values())
    assert not owner.config.status_path.exists()
    with app_db.SessionLocal() as db:
        assert db.query(Station).count() == 0


def test_bad_live_capability_blocks_before_first_source_write(setup):
    owner, rig, _, _ = setup
    rig.raw['/rebroadcastlatencymode'] = '<rebroadcastlatencymode mode="UNKNOWN" />'
    async def run():
        task = asyncio.create_task(owner.run())
        await until(lambda: owner.receipt['status'] == 'WAITING')
        apple_event(rig, {MASTER:GROUP, MEMBER:GROUP})
        receipt = await task
        assert receipt['status'] == 'FAILED'
        assert receipt['restore'] == 'NO_RADIO_WRITE_ATTEMPTED'
        assert receipt['startup_phase'] == 'LIVE_CAPABILITY'
        assert not rig.posts
    asyncio.run(run())


@pytest.mark.parametrize('change', [dict(test_volume=11), dict(test_volume=0), dict(test_volume=True), dict(lifetime_seconds=601),
    dict(wait_seconds=301), dict(listen_port=0), dict(master='UNSELECTED'),
    dict(relay_url='https://example.invalid/audio'), dict(controller_nonce='secret'),
    dict(lifetime_seconds=float('nan'))])
def test_invalid_config_is_rejected(setup, change):
    owner, _, _, _ = setup
    with pytest.raises(ValueError):
        replace(owner.config, **change).validate()


def test_three_hundred_second_input_wait_config_is_accepted(setup):
    owner, _, _, _ = setup
    replace(owner.config, wait_seconds=300, lifetime_seconds=600).validate()


def test_private_config_roundtrip_and_duplicate_rejection(setup):
    owner, _, _, _ = setup
    raw = asdict(owner.config)
    raw['version'] = 1
    path = owner.config.status_path.parent / 'config.json'
    path.write_text(json.dumps(raw, default=str))
    path.chmod(0o600)
    assert runner.load_config(path) == owner.config
    path.write_text('{"version":1,"version":1}')
    with pytest.raises(ValueError, match='DUPLICATE'):
        runner.load_config(path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match='PRIVATE_CONFIG'):
        runner.load_config(path)


def test_safe_error_never_exposes_arbitrary_exception_message():
    assert runner.safe_error(ValueError('AIRPLAY_DATABASE_TARGET_MISMATCH'))['error_code']
    assert runner.safe_error(ValueError('http://private/secret')) == {'error_type':'ValueError'}


def test_safe_error_exposes_only_errno_and_fixed_causal_codes():
    inner = OSError(errno.EADDRINUSE, 'http://private/secret', '/private/secret')
    outer = RuntimeError('AIRPLAY_CONTROLLER_OBSERVATION_LOST')
    outer.__cause__ = inner
    detail = runner.safe_error(outer)
    assert detail['error_code'] == 'AIRPLAY_CONTROLLER_OBSERVATION_LOST'
    assert detail['error_chain'][1] == {'error_type':'OSError', 'error_errno':errno.EADDRINUSE}
    assert 'private' not in json.dumps(detail)
    inner.__cause__ = outer
    assert len(runner.safe_error(outer)['error_chain']) == 2


def test_safe_error_exposes_sqlite_code_without_statement_or_parameters():
    with sqlite3.connect(':memory:') as db:
        try:
            db.execute('SELECT private_secret FROM private_secret')
        except sqlite3.OperationalError as original:
            error = OperationalError('SELECT private_secret', {'token':'private_secret'}, original)
            detail = runner.safe_error(error)
    assert detail['error_chain'][1]['sqlite_error_name'] == 'SQLITE_ERROR'
    assert detail['error_chain'][1]['sqlite_error_code'] == sqlite3.SQLITE_ERROR
    assert 'private_secret' not in json.dumps(detail)
