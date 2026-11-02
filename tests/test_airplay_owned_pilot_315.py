"""Source -> zone -> display -> teardown, all with synthetic radios/input.

The real transaction/backup/identity/metadata services run here. No transport
constructor, receiver daemon or radio endpoint is available to the simulator.
"""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from xml.etree import ElementTree as ET

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import Device, Station
from basswiesn.app.routers.shared import enforce_ip_write_guard
from basswiesn.app.services.airplay_bridge.display_session import DisplaySessionRejected, display_metadata
from basswiesn.app.services.airplay_bridge.pilot_source import PilotSource, pilot_source_scope
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.zone_changes import Zone, ZonePorts
from test_airplay_group_controller_315 import build, apple_event, REAL_SLEEP
from test_airplay_group_zone_315 import rig, ADDRESSES
from test_airplay_group_session_315 import MASTER, MEMBER, GROUP
from test_maintenance_reboot_safety import now

pytestmark = pytest.mark.integration
LOCATION = 'http://192.0.2.80/owned-session'


def simulated_source_ports(rig):
    states = dict.fromkeys(ADDRESSES, now())
    muted = dict.fromkeys(ADDRESSES, False)
    def factory(device):
        key = device.device_id
        base = rig.factory(device)
        class Client:
            async def get_xml(self, path):
                # Real radio I/O yields to receiver supervision. Immediate
                # in-memory returns otherwise starve it across DB-heavy steps.
                await REAL_SLEEP(0)
                if key == MASTER and path == '/presets' and getattr(rig, 'delay_output_read', False):
                    rig.delay_output_read = False
                    await REAL_SLEEP(5.1)
                if path == '/now_playing': return states[key]
                if path == '/presets': return '<presets />'
                if path == '/volume':
                    return (f'<volume><actualvolume>{rig.volumes[key]}</actualvolume>'
                            f'<muteenabled>{str(muted[key]).lower()}</muteenabled></volume>')
                if path == '/sources':
                    return '<sources><sourceItem source="LOCAL_INTERNET_RADIO" status="READY" /></sources>'
                if path == '/standby':
                    assert device_lock(key).owned_by_current_task()
                    rig.posts.append((key, path, ''))
                    states[key] = now()
                    return '<ok />'
                return await base.get_xml(path)

            async def post_xml(self, path, xml):
                await REAL_SLEEP(0)
                assert device_lock(key).owned_by_current_task()
                assert rig.held or (path == '/volume'
                    and 0 <= int(ET.fromstring(xml).text) <= 10
                    and 0 <= rig.volumes[key] <= 10
                    and ET.fromstring(states[key]).get('source') == 'LOCAL_INTERNET_RADIO')
                if path in {'/volume', '/select', '/key'}:
                    rig.posts.append((key, path, xml))
                    if path == '/volume':
                        rig.volumes[key] = int(ET.fromstring(xml).text)
                    elif path == '/select':
                        states[key] = (f'<nowPlaying source="LOCAL_INTERNET_RADIO">{xml}'
                                       '<playStatus>PLAY_STATE</playStatus></nowPlaying>')
                        if getattr(rig, 'source_normalization', False):
                            rig.volumes[key], muted[key] = 7, False
                    else:
                        root = ET.fromstring(xml)
                        assert root.text == 'MUTE'
                        if root.get('state') == 'release': muted[key] = not muted[key]
                    return '<ok />'
                reply = await base.post_xml(path, xml)
                if path == '/setZone':
                    if (getattr(rig, 'join_volume_normalization', False)
                            and MEMBER in {node.text for node in ET.fromstring(xml).findall('member')}):
                        rig.volumes[MEMBER] = 30
                    for identity in ADDRESSES:
                        if not Zone.parse(rig.zones[identity]).standalone(identity):
                            states[identity] = states[MASTER]
                return reply
        return Client()
    return ZonePorts(client=factory, guard=enforce_ip_write_guard), states


def test_standalone_select_uses_no_peer_io_before_actual_provider_release(rig, monkeypatch):
    from basswiesn.app.services import station_playback
    from basswiesn.app.services.playback_safety_gate import wait_for_provider_release
    monkeypatch.setattr(asyncio, 'sleep', REAL_SLEEP)
    rig.held, rig.source_normalization = True, True
    base, _ = simulated_source_ports(rig)
    phase = {'selected': False, 'verified': False}
    critical_reads, provider_tasks = [], []
    verify = station_playback.verify_playback_safety_gate
    def verified(*args, **kwargs):
        result = verify(*args, **kwargs)
        phase['verified'] = True
        return result
    monkeypatch.setattr(station_playback, 'verify_playback_safety_gate', verified)
    async def provider():
        with app_db.SessionLocal() as db:
            return await wait_for_provider_release(db, MASTER)
    def factory(device):
        client = base.client(device)
        class PacedClient:
            async def get_xml(self, path):
                if phase['selected']:
                    if not phase['verified']:
                        critical_reads.append((device.device_id, path))
                    await REAL_SLEEP(.01)
                return await client.get_xml(path)
            async def post_xml(self, path, body):
                result = await client.post_xml(path, body)
                if path == '/select':
                    phase['selected'] = True
                    provider_tasks.append(asyncio.create_task(provider()))
                    await REAL_SLEEP(0)
                return result
        return PacedClient()
    async def run():
        with app_db.SessionLocal() as db:
            devices = db.query(Device).filter(Device.device_id.in_([MASTER, MEMBER])).all()
            station = Station(name='Paired gate fixture', stream_url=LOCATION,
                              internal=True, lab_only=True, purpose='airplay_bridge')
            db.add(station)
            db.commit()
            before = {key: rig.volumes[key] for key in (MASTER, MEMBER)}
            async with pilot_source_scope(db, devices, master=MASTER, station=station,
                    xml=f'<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="{LOCATION}" />',
                    ports=ZonePorts(client=factory, guard=enforce_ip_write_guard), guard_audio=lambda: None) as source:
                release = await provider_tasks[0]
                assert release['state'] == 'VERIFIED' and release['volume_readback'] == 1
                assert phase['verified'] and critical_reads
                assert {key for key, _ in critical_reads} == {MASTER}
                assert all(device_lock(key).owned_by_current_task() for key in (MASTER, MEMBER))
                assert all(rig.volumes[key] == 1 for key in (MASTER, MEMBER))
                assert set(source.before) == {MASTER, MEMBER}
            assert source.restore_result == 'CAPTURED_STATE_RESTORED'
            assert {key: rig.volumes[key] for key in before} == before
            assert sum(path == '/select' for _, path, _ in rig.posts) == 1
    asyncio.run(run())


@pytest.mark.parametrize('change', ['source', 'presets', 'volume'])
def test_peer_change_during_master_select_is_rejected_before_group_preparation(rig, monkeypatch, change):
    from basswiesn.app.services.airplay_bridge import pilot_source
    monkeypatch.setattr(asyncio, 'sleep', REAL_SLEEP)
    rig.held = True
    ports, states = simulated_source_ports(rig)
    play = pilot_source.play_station
    async def changed(*args, **kwargs):
        result = await play(*args, **kwargs)
        if change == 'source':
            states[MEMBER] = now('AUX', 'PLAY_STATE')
        elif change == 'volume':
            rig.volumes[MEMBER] = 12
        else:
            original = kwargs['client_factory'](next(device for device in devices if device.device_id == MEMBER))
            read = original.get_xml
            async def get_xml(path):
                return '<presets><preset id="1" /></presets>' if path == '/presets' else await read(path)
            original.get_xml = get_xml
        return result
    monkeypatch.setattr(pilot_source, 'play_station', changed)
    async def run():
        nonlocal devices
        with app_db.SessionLocal() as db:
            devices = db.query(Device).filter(Device.device_id.in_([MASTER, MEMBER])).all()
            station = Station(name='Peer fence fixture', stream_url=LOCATION,
                              internal=True, lab_only=True, purpose='airplay_bridge')
            db.add(station)
            db.commit()
            with pytest.raises(ValueError, match='RESTORE_NEEDS_RECONCILIATION') as error:
                async with pilot_source_scope(db, devices, master=MASTER, station=station,
                        xml=f'<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="{LOCATION}" />',
                        ports=ports, guard_audio=lambda: None):
                    pytest.fail('changed peer must not authorize grouping')
            assert str(error.value.__context__) == 'AIRPLAY_PILOT_PEER_CHANGED_DURING_SELECTION'
            assert not any(path == '/setZone' for _, path, _ in rig.posts)
    devices = []
    asyncio.run(run())


@pytest.mark.parametrize('slow_readback,stale_display,join_volume,test_volume', [
    (False, False, 'unchanged', 1), (True, False, 'unchanged', 1), (True, True, 'unchanged', 1),
    (False, False, 'normalized', 1), (False, False, 'later_change', 1),
    (False, False, 'normalized', 5),
    (False, False, 'normalized', 10),
])
def test_wait_source_group_metadata_end_and_exact_captured_restore(rig, monkeypatch,
                                                                  slow_readback, stale_display, join_volume,
                                                                  test_volume):
    monkeypatch.setattr(asyncio, 'sleep', REAL_SLEEP)
    rig.join_volume_normalization = join_volume != 'unchanged'
    validate = PilotSource.validate_output
    async def validate_output(source, zone, frames):
        rig.delay_output_read = slow_readback
        if join_volume == 'later_change':
            assert zone.volume_normalizations[MEMBER] == 30
            rig.volumes[MEMBER] = 44
        await validate(source, zone, frames)
    monkeypatch.setattr(PilotSource, 'validate_output', validate_output)
    async def run():
        rig.source = LOCATION
        controller, channels = await build(rig)  # Initially all input IDLE.
        first_observed = time.monotonic()
        for channel in channels.values():
            original = channel.exchange
            async def exchange(raw, original=original):
                value = json.loads(await original(raw))
                if json.loads(raw)['operation'] == 'observe':
                    observation = value['observation']
                    observation['display'].update(version=1, sequence=observation['heartbeat_sequence'],
                        observed_monotonic=first_observed if stale_display else time.monotonic(), active=True,
                        fields={'track':'Synthetic song', 'artist':'Example artist'})
                return json.dumps(value).encode()
            channel.exchange = exchange
        ports, states = simulated_source_ports(rig)
        before = dict(rig.volumes)
        owners = []
        with app_db.SessionLocal() as db:
            devices = db.query(Device).all()
            station = Station(name='Private AP2 fixture', stream_url=LOCATION,
                              internal=True, lab_only=True, purpose='airplay_bridge')
            db.add(station)
            db.commit()
            @asynccontextmanager
            async def prepare(guard):
                async with pilot_source_scope(db, devices, master=MASTER, station=station,
                        xml=f'<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="{LOCATION}" />',
                        ports=ports, guard_audio=guard, test_volume=test_volume) as source:
                    owners.append(source)
                    yield source
            async def apple():
                while not channels[MASTER].commands:
                    await REAL_SLEEP(.01)
                assert not rig.posts and not rig.constructed
                # Real users select the master, then add the second receiver.
                # Do not race source preparation against that intermediate plan.
                apple_event(rig, {MASTER:GROUP})
                await REAL_SLEEP(.15)
                assert not rig.posts and not rig.constructed
                apple_event(rig, {MEMBER:GROUP})
                while not channels[MASTER].releases:
                    await REAL_SLEEP(.01)
                assert rig.volumes == dict.fromkeys(ADDRESSES, test_volume)
                assert display_metadata(db, owners[0].master, station) == {
                    'track':'Synthetic song', 'artist':'Example artist'}
                assert any(path == '/setZone' for _, path, _ in rig.posts)
                apple_event(rig, {MASTER:'', MEMBER:''})
            simulator = asyncio.create_task(apple())
            try:
                running = controller.run_owned(db, devices, ports=ports,
                    prepare=prepare, initial_members=frozenset({MEMBER}))
                if stale_display or join_volume == 'later_change':
                    with pytest.raises(ValueError, match='RESTORE_NEEDS_RECONCILIATION') as rejected:
                        await asyncio.wait_for(running, timeout=20)
                    if stale_display:
                        assert isinstance(rejected.value.__context__, DisplaySessionRejected)
                    else:
                        assert str(rejected.value.__context__) == 'AIRPLAY_PILOT_OUTPUT_VOLUME_OR_PRESETS_CHANGED'
                        assert rig.volumes[MEMBER] == 44
                        zone_index = next(i for i, (_, path, _) in enumerate(rig.posts) if path == '/setZone')
                        assert not any(path == '/volume' for _, path, _ in rig.posts[zone_index + 1:])
                    assert not channels[MASTER].releases
                    assert owners[0].restore_result == 'MANUAL_REQUIRED'
                    assert not display_metadata(db, owners[0].master, station)
                    return
                result = await asyncio.wait_for(running, timeout=20)
                await simulator
            finally:
                simulator.cancel()
                await asyncio.gather(simulator, return_exceptions=True)
            assert result.status == 'STOPPED'
            assert owners[0].restore_result == 'CAPTURED_STATE_RESTORED'
            assert not display_metadata(db, owners[0].master, station)
        assert rig.volumes == before
        assert all(ET.fromstring(xml).get('source') == 'STANDBY' for xml in states.values())
        assert all(Zone.parse(xml).standalone(key) for key, xml in rig.zones.items())
        assert set(rig.latency.values()) == {'SYNC_TO_ROOM'}
        assert sum(path == '/select' for _, path, _ in rig.posts) == 1
        if join_volume == 'normalized':
            zone_index = next(i for i, (_, path, _) in enumerate(rig.posts) if path == '/setZone')
            assert (MEMBER, '/volume', f'<volume>{test_volume}</volume>') in rig.posts[zone_index + 1:]
    asyncio.run(run())


def test_native_change_during_preparation_is_not_a_new_target(rig, monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', REAL_SLEEP)
    async def run():
        apple_event(rig, {MASTER:GROUP, MEMBER:GROUP})
        controller, channels = await build(rig)
        @asynccontextmanager
        async def prepare(guard):
            guard()
            apple_event(rig, {MEMBER:''})
            await REAL_SLEEP(.15)
            guard()
            yield None
        with pytest.raises(RuntimeError, match='PLAN_CHANGED_DURING_PREPARATION'):
            await controller.run_owned(None, [], ports=None, prepare=prepare)
        assert not rig.posts and not channels[MASTER].releases
    asyncio.run(run())


def test_wait_timeout_never_opens_a_radio_transport(rig, monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', REAL_SLEEP)
    async def run():
        controller, _ = await build(rig)
        @asynccontextmanager
        async def prepare(guard):
            pytest.fail('must not prepare without selected input')
            yield None
        with pytest.raises(TimeoutError, match='INPUT_WAIT_EXPIRED'):
            await controller.run_owned(None, [], ports=None, prepare=prepare, wait_seconds=.01)
        assert not rig.posts and not rig.constructed
    asyncio.run(run())


@pytest.mark.parametrize('members', [frozenset({'UNSELECTED'}), frozenset({MASTER})])
def test_invalid_initial_members_rejected_before_receiver_or_radio_use(rig, members):
    async def run():
        controller, channels = await build(rig)
        with pytest.raises(ValueError, match='EXPLICIT_INITIAL_MEMBERS'):
            await controller.run_owned(None, [], ports=None, prepare=None, initial_members=members)
        assert not rig.posts and not rig.constructed
        assert not channels[MASTER].commands
    asyncio.run(run())


def test_missing_second_receiver_expires_without_preparation(rig, monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', REAL_SLEEP)
    async def run():
        apple_event(rig, {MASTER:GROUP})
        controller, channels = await build(rig)
        @asynccontextmanager
        async def prepare(guard):
            pytest.fail('must wait for requested second receiver')
            yield None
        with pytest.raises(TimeoutError, match='INPUT_WAIT_EXPIRED'):
            await controller.run_owned(None, [], ports=None, prepare=prepare,
                initial_members=frozenset({MEMBER}), wait_seconds=.01)
        assert not rig.posts and not rig.constructed and not channels[MASTER].releases
    asyncio.run(run())


def test_three_hundred_second_wait_stays_held_without_radio_use(rig, monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', REAL_SLEEP)
    async def run():
        controller, channels = await build(rig)
        @asynccontextmanager
        async def prepare(guard):
            pytest.fail('waiting for human selection must not prepare a source')
            yield None
        owner = asyncio.create_task(controller.run_owned(None, [], ports=None, prepare=prepare,
            initial_members=frozenset({MEMBER}), wait_seconds=300))
        async with asyncio.timeout(2):
            while len(channels[MASTER].commands) < 2:
                await REAL_SLEEP(.01)
        assert not owner.done() and not rig.posts and not rig.constructed
        assert all(not record['allow_audio'] for record in channels[MASTER].commands)
        assert not channels[MASTER].releases
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert not rig.posts and not rig.constructed
    asyncio.run(run())


def test_three_hundred_one_second_wait_rejected_before_ipc_or_radio(rig):
    async def run():
        controller, channels = await build(rig)
        with pytest.raises(ValueError, match='BOUNDED_WAIT'):
            await controller.run_owned(None, [], ports=None, prepare=None, wait_seconds=301)
        assert not rig.posts and not rig.constructed
        assert not channels[MASTER].commands
    asyncio.run(run())


@pytest.mark.parametrize('foreground_advances', [True, False])
def test_preparation_progress_is_foreground_only_and_stalled_step_still_expires(
        rig, monkeypatch, foreground_advances):
    monkeypatch.setattr(asyncio, 'sleep', REAL_SLEEP)
    class PreparedEnough(Exception):
        pass
    async def run():
        apple_event(rig, {MASTER:GROUP, MEMBER:GROUP})
        controller, channels = await build(rig)
        started = rig.inputs.now
        checks = []
        @asynccontextmanager
        async def prepare(guard):
            guard()
            # Receiver/control heartbeats keep advancing independently while
            # the foreground performs (or gets stuck inside) a single step.
            for _ in range(18):
                rig.inputs.now += 2
                await REAL_SLEEP(.065)
                if foreground_advances:
                    guard()
                    checks.append(rig.inputs.now)
            guard()
            raise PreparedEnough()
            yield None
        if foreground_advances:
            with pytest.raises(PreparedEnough):
                await controller.run_owned(None, [], ports=None, prepare=prepare,
                    initial_members=frozenset({MEMBER}))
            assert len(checks) == 18 and rig.inputs.now - started > 30
            assert controller._failure is None
        else:
            with pytest.raises(RuntimeError, match='OBSERVATION_LOST') as failure:
                await controller.run_owned(None, [], ports=None, prepare=prepare,
                    initial_members=frozenset({MEMBER}))
            assert isinstance(failure.value.__cause__, TimeoutError)
            assert str(failure.value.__cause__) == 'AIRPLAY_FOREGROUND_STALLED'
        assert not rig.posts and not rig.constructed and not channels[MASTER].releases
        assert all(not command['allow_audio'] for command in channels[MASTER].commands)
    asyncio.run(run())
