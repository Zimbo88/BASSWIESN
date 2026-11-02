"""Full private control loop with simulated receivers and real zone services."""
import asyncio
import json

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import Device
from basswiesn.app.routers.shared import enforce_ip_write_guard
from basswiesn.app.services.airplay_bridge.control_owner import AudioControl
from basswiesn.app.services.airplay_bridge.group_controller import GroupController
from basswiesn.app.services.airplay_bridge.group_zone import GroupZone
from basswiesn.app.services.airplay_bridge.group_session import GroupSession
from basswiesn.app.services.airplay_bridge.receiver_port import ReceiverPort
from basswiesn.app.services.zone_changes import Zone, ZonePorts
from test_airplay_group_zone_315 import rig, prime, ADDRESSES, SELECTION
from test_airplay_group_session_315 import MASTER, MEMBER, OTHER

pytestmark = pytest.mark.integration
REAL_SLEEP = asyncio.sleep
NONCE, BINARY = 'e'*32, 'd'*64


class Namespace:
    def __init__(self, rig, identity, *, clock=None):
        self.rig, self.identity = rig, identity
        self.clock = clock
        self.ackseq, self.sequence = 0, 0
        self.commands, self.releases = [], []
        self.record = None
        self.fail = False

    async def exchange(self, raw):
        await REAL_SLEEP(0)
        if self.fail:
            raise ConnectionError('simulated namespace loss')
        request = json.loads(raw)
        rig = self.rig
        # Pure state-machine tests use a logical clock. Real-time owner tests
        # sample their clock AFTER yielding, not by resetting this shared value
        # before concurrent exchanges then adding fictitious milliseconds.
        # Mixing those two clocks can make a fresh acknowledgement go backward.
        rig.inputs.now = self.clock() if self.clock is not None else rig.inputs.now + .001
        native = rig.inputs.records[self.identity]
        if request['operation'] == 'bind':
            result = dict(version=1, nonce=native['nonce'], pid=native['pid'], started=99.,
                          contract='basswiesn-group-v1', binary_sha256=BINARY)
        elif request['operation'] == 'observe':
            self.sequence += 1
            result = dict(native=native, observation=dict(version=1,
                heartbeat_sequence=self.sequence, observed_monotonic=rig.inputs.now,
                process_alive=True, playback={'state':'PLAYING', 'sequence':1, 'buffer_epoch':0},
                display={'session':native['nonce']}))
            if hasattr(self, 'volume'):
                result['observation'].update(volume=self.volume,
                    volume_mode=getattr(self, 'volume_mode', 'radio'))
        elif request['operation'] == 'control':
            self.record = request['record']
            self.commands.append(self.record)
            self.rig.held = not self.record['allow_audio']
            if not self.rig.held:
                self.releases.append(tuple(rig.posts))
            result = {'published': True}
        else:
            self.ackseq += 1
            result = dict(version=1, nonce=NONCE, sequence=self.ackseq,
                observed_monotonic=rig.inputs.now, command_sequence=self.record['sequence'],
                hold_epoch=self.record['hold_epoch'], allow_audio=self.record['allow_audio'],
                audio_released=self.record['allow_audio'])
        return json.dumps(result).encode()


async def build(rig):
    channels = {key: Namespace(rig, key) for key in ADDRESSES}
    ports = {key: ReceiverPort(value) for key, value in channels.items()}
    for key, port in ports.items():
        await port.bind(key, binary_sha256=BINARY)
    rig.inputs.session = GroupSession(tuple(port.binding for port in ports.values()),
                                      master=MASTER, started=rig.inputs.now)
    audio = AudioControl(NONCE, ports[MASTER], clock=lambda: rig.inputs.now)
    controller = GroupController(rig.inputs.session, ports, audio, clock=lambda: rig.inputs.now, interval=.05)
    return controller, channels


def test_namespace_clock_is_sampled_after_concurrent_exchange_yields(rig):
    from basswiesn.app.services.airplay_bridge.heartbeat import ReceiverHeartbeat
    stamps = iter([100.001, 100.002, 100.003, 100.004])
    clock = lambda: next(stamps)
    first = Namespace(rig, MASTER, clock=clock)
    second = Namespace(rig, MEMBER, clock=clock)
    first.record = {'sequence': 1, 'hold_epoch': 1, 'allow_audio': False}
    async def call(channel, operation):
        return json.loads(await channel.exchange(json.dumps({'operation': operation}).encode()))
    async def run():
        values = await asyncio.gather(call(first, 'observe'), call(second, 'observe'), call(first, 'ack'))
        following = await call(first, 'ack')
        heartbeat = ReceiverHeartbeat()
        for value in (values[2], following):
            heartbeat.check({'sequence': value['sequence'],
                             'observed_monotonic': value['observed_monotonic']}, now=101.)
        assert values[2]['observed_monotonic'] == 100.003
        assert following['observed_monotonic'] == 100.004
    asyncio.run(run())


def apple_event(rig, changes):
    for key, group in changes.items():
        value = rig.inputs.records[key]
        value.update(revision=value['revision']+1, connection=1 if group else 0,
                     state='GROUPED' if group else 'IDLE', group_hash=group,
                     event_monotonic=rig.inputs.now)


@pytest.mark.parametrize('bad', [
    {'sequence':True,'airplay_db':0}, {'sequence':0,'airplay_db':0},
    {'sequence':1,'airplay_db':1}, {'sequence':1,'airplay_db':float('nan')},
    {'sequence':1,'airplay_db':-31}, {'sequence':1,'airplay_db':True},
])
def test_receiver_rejects_bad_volume_before_it_can_authorize_write(rig, bad):
    async def run():
        controller, channels = await build(rig)
        channels[MASTER].volume = bad
        with pytest.raises(ValueError):
            await controller.receivers[MASTER].read()
        assert not rig.posts
    asyncio.run(run())


def test_receiver_volume_replay_cannot_change_same_sequence(rig):
    async def run():
        controller, channels = await build(rig)
        channel = channels[MASTER]
        channel.volume = {'sequence':2,'airplay_db':-15.0}
        port = controller.receivers[MASTER]
        first = await port.read()
        assert first.volume == channel.volume and first.volume_mode == 'radio'
        assert (await port.read()).volume == first.volume
        channel.volume = {'sequence':2,'airplay_db':0.0}
        with pytest.raises(ValueError, match='VOLUME_REPLAY'):
            await port.read()
        channel.volume = {'sequence':1,'airplay_db':-15.0}
        with pytest.raises(ValueError, match='VOLUME_REPLAY'):
            await port.read()
        assert not rig.posts
    asyncio.run(run())


def test_group_controller_creates_removes_and_tears_down_on_native_end(rig, monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', REAL_SLEEP)
    async def run():
        prime(rig)
        controller, channels = await build(rig)
        results, validations = [], []
        def result(value):
            results.append(value.status)
            if value.status == 'ACTIVE':
                assert value.members in {(MEMBER,), ()}
        async def validate(zone, frames):
            assert set(frames()) == set(ADDRESSES)
            assert await zone.volumes() == tuple(sorted(rig.volumes.items()))
            validations.append(True)
        async def apple():
            while not channels[MASTER].releases:
                await REAL_SLEEP(.01)
            assert any('/setZone' == path for _, path, _ in rig.posts)
            apple_event(rig, {MEMBER: ''})
            while rig.zones[MEMBER] != '<zone />':
                await REAL_SLEEP(.01)
            while results != ['ACTIVE', 'ACTIVE']:
                await REAL_SLEEP(.01)
            apple_event(rig, {MASTER: ''})
        simulator = asyncio.create_task(apple())
        with app_db.SessionLocal() as db:
            devices = db.query(Device).all()
            outcome = await asyncio.wait_for(controller.run(db, devices,
                ports=ZonePorts(client=rig.factory, guard=enforce_ip_write_guard),
                selection=SELECTION, validate_output=validate, on_result=result), timeout=8)
        await simulator
        assert outcome.status == 'STOPPED'
        assert results == ['ACTIVE', 'ACTIVE', 'STOPPED']
        assert validations and all(Zone.parse(xml).standalone(key) for key, xml in rig.zones.items())
        assert set(rig.latency.values()) == {'SYNC_TO_ROOM'}
        assert not channels[MASTER].commands[-1]['allow_audio']
        with pytest.raises(RuntimeError, match='ONE_SHOT'):
            await controller.run(None, [], ports=None, selection=None, validate_output=validate)
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['volume', 'metadata', 'receiver', 'ack'])
def test_bad_output_readback_or_receiver_loss_never_releases(rig, monkeypatch, failure):
    monkeypatch.setattr(asyncio, 'sleep', REAL_SLEEP)
    async def run():
        prime(rig)
        controller, channels = await build(rig)
        async def validate(zone, frames):
            if failure in {'volume', 'metadata'}:
                raise ValueError('unverified actual output')
            channels[MEMBER if failure == 'receiver' else MASTER].fail = True
            await REAL_SLEEP(.15)
        with app_db.SessionLocal() as db:
            with pytest.raises(Exception):
                await asyncio.wait_for(controller.run(db, db.query(Device).all(),
                    ports=ZonePorts(client=rig.factory, guard=enforce_ip_write_guard),
                    selection=SELECTION, validate_output=validate), timeout=5)
        assert not channels[MASTER].releases
    asyncio.run(run())


def test_native_end_during_transaction_blocks_remaining_writes(rig, monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', REAL_SLEEP)
    async def run():
        prime(rig)
        controller, channels = await build(rig)
        original = rig.factory
        def client(device):
            value = original(device)
            real_post = value.post_xml
            async def post(path, body):
                result = await real_post(path, body)
                apple_event(rig, {MEMBER: ''})
                await REAL_SLEEP(.15)
                return result
            value.post_xml = post
            return value
        async def validate(*_):
            pytest.fail('unverified group')
        with app_db.SessionLocal() as db:
            with pytest.raises(Exception):
                await controller.run(db, db.query(Device).all(),
                    ports=ZonePorts(client=client, guard=enforce_ip_write_guard),
                    selection=SELECTION, validate_output=validate)
        assert len(rig.posts) == 1 and not channels[MASTER].releases
    asyncio.run(run())


@pytest.mark.parametrize('during', ['snapshot', 'frames'])
def test_native_end_during_readonly_output_validation_tears_down(rig, monkeypatch, during):
    monkeypatch.setattr(asyncio, 'sleep', REAL_SLEEP)
    async def run():
        prime(rig)
        controller, channels = await build(rig)
        async def validate(zone, frames):
            apple_event(rig, {MASTER:'', MEMBER:''})
            await REAL_SLEEP(.15)
            if during == 'snapshot':
                zone.guard_all()
            else:
                frames()
        with app_db.SessionLocal() as db:
            result = await asyncio.wait_for(controller.run(db, db.query(Device).all(),
                ports=ZonePorts(client=rig.factory, guard=enforce_ip_write_guard),
                selection=SELECTION, validate_output=validate), timeout=8)
        assert result.status == 'STOPPED'
        assert not channels[MASTER].releases
        assert all(Zone.parse(xml).standalone(key) for key, xml in rig.zones.items())
        assert set(rig.latency.values()) == {'SYNC_TO_ROOM'}
    asyncio.run(run())


@pytest.mark.parametrize('stalled', [False, True])
def test_zone_preparation_progress_not_aggregate_duration_or_monitor_heartbeat(rig, monkeypatch, stalled):
    monkeypatch.setattr(asyncio, 'sleep', REAL_SLEEP)
    preparing = True
    prepared_at = []
    original_prepare = GroupZone.prepare
    async def prepare(zone):
        nonlocal preparing
        await original_prepare(zone)
        prepared_at.append(rig.inputs.now)
        preparing = False
    monkeypatch.setattr(GroupZone, 'prepare', prepare)
    class VerifiedEnough(Exception):
        pass
    async def run():
        prime(rig)
        controller, channels = await build(rig)
        started = rig.inputs.now
        def factory(device):
            client = rig.factory(device)
            original_read = client.get_xml
            async def read(path):
                if preparing:
                    # Heartbeats remain live during one stuck read, but only
                    # completion of a guarded foreground step counts as progress.
                    for _ in range(13 if stalled else 1):
                        rig.inputs.now += 2.5
                        await REAL_SLEEP(.065)
                else:
                    await REAL_SLEEP(0)
                return await original_read(path)
            client.get_xml = read
            return client
        async def validate(zone, frames):
            # The progress hook cannot be invoked by a sibling/monitor task.
            async def unrelated():
                zone.foreground_progress()
            with pytest.raises(RuntimeError, match='FOREGROUND_OWNER_REQUIRED'):
                await asyncio.create_task(unrelated())
            raise VerifiedEnough()
        with app_db.SessionLocal() as db:
            running = controller.run(db, db.query(Device).all(),
                ports=ZonePorts(client=factory, guard=enforce_ip_write_guard),
                selection=SELECTION, validate_output=validate)
            with pytest.raises(Exception) as failure:
                await asyncio.wait_for(running, timeout=12)
        if stalled:
            assert not prepared_at and not rig.posts
            assert isinstance(controller._failure, TimeoutError)
            assert str(controller._failure) == 'AIRPLAY_FOREGROUND_STALLED'
        else:
            assert isinstance(failure.value, VerifiedEnough)
            assert prepared_at[0] - started > 30
            assert controller._failure is None
        assert not channels[MASTER].releases
    asyncio.run(run())
