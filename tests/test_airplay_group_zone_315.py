"""Real zone/backup services against simulated radios; never household traffic."""
import asyncio
import json
from xml.etree import ElementTree as ET

import httpx
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import ConfigBackup, Device, DeviceActionJournal, Setting
from basswiesn.app.routers.shared import enforce_ip_write_guard
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.direct_selection import Selection
from basswiesn.app.services.zone_changes import ZonePorts
from basswiesn.app.services.airplay_bridge.group_zone import group_zone_scope
from test_airplay_group_session_315 import Inputs, MASTER, MEMBER, OTHER, GROUP, SEPARATE
from test_settings_write_safety import BUILD, info

pytestmark = pytest.mark.integration
ADDRESSES = {MASTER: '192.0.2.41', MEMBER: '192.0.2.42', OTHER: '192.0.2.43'}
SELECTION = Selection('LOCAL_INTERNET_RADIO', '', 'http://example.invalid/owned-session')


class Rig:
    def __init__(self):
        self.inputs = Inputs()
        self.zones = dict.fromkeys(ADDRESSES, '<zone />')
        self.volumes = {MASTER: 3, MEMBER: 7, OTHER: 9}
        self.latency = dict.fromkeys(ADDRESSES, 'SYNC_TO_ROOM')
        self.posts, self.calls, self.constructed = [], [], []
        self.held, self.holds = False, 0
        self.lost = self.ignore = self.fail_after = self.malformed = self.bad_identity = ''
        self.source = SELECTION.location
        self.play_status = 'PLAY_STATE'
        self.normalization = False
        self.cancel = False
        self.expire_after_write = False
        self.change_group_after_write = False
        self.raw = {}

    def hold(self):
        self.held = True
        self.holds += 1

    def factory(self, device):
        identity, rig = device.device_id, self
        self.constructed.append(identity)

        class Client:
            async def get_xml(self, path):
                rig.calls.append((identity, path))
                if rig.posts and path == rig.fail_after:
                    raise httpx.ReadTimeout('simulated lost readback')
                if path == rig.malformed:
                    return '<unexpected />'
                if path in rig.raw:
                    return rig.raw[path]
                if path == '/info':
                    return info(device_id='WRONG' if identity == rig.bad_identity else identity)
                if path == '/getZone':
                    return rig.zones[identity]
                if path == '/volume':
                    return (f'<volume deviceID="{identity}"><actualvolume>{rig.volumes[identity]}</actualvolume>'
                            '<muteenabled>false</muteenabled></volume>')
                if path == '/rebroadcastlatencymode':
                    return f'<rebroadcastlatencymode mode="{rig.latency[identity]}" />'
                if path == '/now_playing':
                    return (f'<nowPlaying deviceID="{identity}" source="LOCAL_INTERNET_RADIO">'
                        f'<ContentItem source="LOCAL_INTERNET_RADIO" sourceAccount="" location="{rig.source}" />'
                        f'<playStatus>{rig.play_status}</playStatus></nowPlaying>')
                raise AssertionError(path)

            async def post_xml(self, path, xml):
                assert rig.held
                assert all(device_lock(key).owned_by_current_task() for key in ADDRESSES)
                with app_db.SessionLocal() as db:
                    assert db.query(ConfigBackup).count() > 0
                    assert db.query(DeviceActionJournal).filter_by(phase='AIRPLAY_ZONE_START').count() == 1
                rig.posts.append((identity, path, xml))
                if rig.cancel:
                    raise asyncio.CancelledError()
                if path != rig.ignore:
                    if path == '/setZone':
                        member_ids = {node.text for node in ET.fromstring(xml).findall('member')}
                        for key in ADDRESSES:
                            rig.zones[key] = xml if key in member_ids | {identity} else '<zone />'
                        if rig.normalization:
                            rig.volumes[MEMBER] = 10
                    elif path == '/rebroadcastlatencymode':
                        rig.latency[identity] = ET.fromstring(xml).get('mode')
                    else:
                        raise AssertionError('unexpected radio write ' + path)
                if rig.expire_after_write:
                    rig.inputs.now += 6
                if rig.change_group_after_write:
                    rig.inputs.session.observe(rig.frame({MEMBER: ''}), now=rig.inputs.now)
                if path == rig.lost:
                    raise httpx.ReadTimeout('simulated lost POST reply')
                return '<status>OK</status>'

        return Client()

    def frame(self, changes=None):
        return self.inputs.frame(changes)

    def scope(self, db, **options):
        devices = [db.query(Device).filter_by(device_id=identity).one() for identity in ADDRESSES]
        return group_zone_scope(db, devices, self.inputs.session,
            ports=ZonePorts(client=self.factory, guard=enforce_ip_write_guard),
            selection=SELECTION, hold_audio=self.hold, clock=lambda: self.inputs.now, **options)


@pytest.fixture
def rig(monkeypatch):
    rig = Rig()
    with app_db.SessionLocal() as db:
        for identity, address in ADDRESSES.items():
            db.add(Device(device_id=identity, ip_address=address, name=identity,
                          model='SoundTouch 30', firmware=BUILD, info_xml=info(device_id=identity)))
        db.commit()
    async def no_sleep(_):
        pass
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    return rig


def prime(rig, members=(MEMBER,)):
    samples = rig.frame({MASTER: GROUP, **{key: GROUP for key in members}})
    rig.inputs.session.observe(samples, now=rig.inputs.now)
    return samples


def test_remote_audio_hold_acknowledged_before_each_zone_write(rig):
    acks = []
    async def hold_ack():
        acks.append(len(rig.posts))
        rig.hold()
    async def run():
        samples = prime(rig)
        with app_db.SessionLocal() as db:
            async with rig.scope(db, hold_acknowledged=hold_ack) as owner:
                await owner.update(samples, now=rig.inputs.now)
                assert len(acks) > len(rig.posts)
                assert all(index in acks for index in range(len(rig.posts)))
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['lost', 'topology', 'source', 'owner'])
def test_remote_hold_failure_or_precondition_change_during_ack_never_writes(rig, failure):
    async def hold_ack():
        if failure == 'lost':
            raise TimeoutError('no worker acknowledgement')
        if failure == 'topology':
            rig.zones[MEMBER] = '<zone master="FOREIGN" />'
        elif failure == 'source':
            rig.source = 'http://example.invalid/another-source'
        else:
            rig.inputs.now += 6
    async def run():
        samples = prime(rig)
        with app_db.SessionLocal() as db:
            async with rig.scope(db, hold_acknowledged=hold_ack) as owner:
                with pytest.raises(Exception):
                    await owner.update(samples, now=rig.inputs.now)
                assert not rig.posts and owner.failed
    asyncio.run(run())


def test_create_join_leave_and_stop_reuse_real_transactions_preserve_volume(rig):
    async def run():
        samples = prime(rig)
        with app_db.SessionLocal() as db:
            async with rig.scope(db) as owner:
                first = await owner.update(samples, now=rig.inputs.now)
                assert first.members == (MEMBER,) and first.audio_released is False
                assert first.volumes == tuple(sorted({MASTER: 3, MEMBER: 7, OTHER: 9}.items()))
                count = len(rig.posts)
                await owner.update(rig.frame(), now=rig.inputs.now)
                assert len(rig.posts) == count, 'same native state must not replay writes'
                joined = await owner.update(rig.frame({OTHER: GROUP}), now=rig.inputs.now)
                assert joined.members == tuple(sorted((MEMBER, OTHER)))
                left = await owner.update(rig.frame({MEMBER: ''}), now=rig.inputs.now)
                assert left.members == (OTHER,)
                assert rig.latency[MEMBER] == 'SYNC_TO_ROOM'
                ended = await owner.update(rig.frame({MASTER: '', OTHER: ''}), now=rig.inputs.now)
                assert ended.status == 'STOPPED' and not ended.members
                assert set(rig.latency.values()) == {'SYNC_TO_ROOM'}
    asyncio.run(run())
    assert not any(path in {'/volume', '/key', '/select'} for _, path, _ in rig.posts)
    assert rig.volumes == {MASTER: 3, MEMBER: 7, OTHER: 9}
    assert all(not device_lock(key).locked() for key in ADDRESSES)


def test_different_apple_group_is_not_joined(rig):
    async def run():
        samples = prime(rig)
        samples = rig.frame({OTHER: SEPARATE})
        with app_db.SessionLocal() as db:
            async with rig.scope(db) as owner:
                assert (await owner.update(samples, now=rig.inputs.now)).members == (MEMBER,)
                assert rig.zones[OTHER] == '<zone />'
    asyncio.run(run())
    assert not any(identity == OTHER for identity, _, _ in rig.posts)


@pytest.mark.parametrize('path', ['/setZone', '/rebroadcastlatencymode'])
def test_lost_reply_is_reconciled_by_readback_not_replayed(rig, path):
    rig.lost = path
    async def run():
        samples = prime(rig)
        with app_db.SessionLocal() as db:
            async with rig.scope(db) as owner:
                assert (await owner.update(samples, now=rig.inputs.now)).status == 'ACTIVE'
    asyncio.run(run())
    calls = [(identity, path) for identity, path, _ in rig.posts]
    assert len(calls) == len(set(calls))


@pytest.mark.parametrize('failure,path', [('ignore', '/setZone'), ('ignore', '/rebroadcastlatencymode'),
    ('fail_after', '/getZone'), ('fail_after', '/rebroadcastlatencymode')])
def test_no_false_success_or_automatic_rollback_after_partial_failure(rig, failure, path):
    setattr(rig, failure, path)
    async def run():
        samples = prime(rig)
        with app_db.SessionLocal() as db:
            async with rig.scope(db) as owner:
                with pytest.raises(Exception):
                    await owner.update(samples, now=rig.inputs.now)
                assert owner.failed and rig.held
                count = len(rig.posts)
                with pytest.raises(Exception):
                    await owner.update(rig.frame(), now=rig.inputs.now)
                assert len(rig.posts) == count
    asyncio.run(run())
    assert len(rig.posts) <= 3


def test_master_leave_dissolves_zone_but_requires_new_audio_grant(rig):
    async def run():
        samples = prime(rig)
        with app_db.SessionLocal() as db:
            async with rig.scope(db) as owner:
                await owner.update(samples, now=rig.inputs.now)
                result = await owner.update(rig.frame({MASTER: ''}), now=rig.inputs.now)
                assert result.status == 'HANDOFF_REQUIRED' and not result.audio_released
                assert owner.closed and rig.held
                assert rig.zones[MEMBER] == '<zone />'
    asyncio.run(run())


def test_owned_stopped_stream_still_allows_verified_zone_teardown(rig):
    async def run():
        samples = prime(rig)
        with app_db.SessionLocal() as db:
            async with rig.scope(db) as owner:
                await owner.update(samples, now=rig.inputs.now)
                rig.play_status = 'STOP_STATE'
                result = await owner.update(rig.frame({MASTER:'', MEMBER:''}), now=rig.inputs.now)
                assert result.status == 'STOPPED' and not result.members
                assert all(value == 'SYNC_TO_ROOM' for value in rig.latency.values())
    asyncio.run(run())


@pytest.mark.parametrize('protection', ['protected_device_ids', 'ip_write_guard'])
def test_policy_blocks_before_any_transport_is_created(rig, protection):
    with app_db.SessionLocal() as db:
        db.add(Setting(key=protection, value=MEMBER if protection == 'protected_device_ids' else 'true'))
        db.commit()
    async def run():
        prime(rig)
        with app_db.SessionLocal() as db:
            with pytest.raises(Exception):
                async with rig.scope(db):
                    pytest.fail('must not open')
    asyncio.run(run())
    assert not rig.constructed and not rig.posts


@pytest.mark.parametrize('failure', ['existing_zone', 'source', 'identity', 'unsupported_profile', 'bad_volume'])
def test_bad_preconditions_do_not_write(rig, failure):
    if failure == 'existing_zone':
        rig.zones[MEMBER] = '<zone master="SOMEONE-ELSE" />'
    elif failure == 'source':
        rig.source = 'http://example.invalid/manual-selection'
    elif failure == 'identity':
        rig.bad_identity = MEMBER
    elif failure == 'bad_volume':
        rig.malformed = '/volume'
    else:
        with app_db.SessionLocal() as db:
            db.query(Device).filter_by(device_id=MEMBER).one().firmware = '27.0.7.UNKNOWN'
            db.commit()
    async def run():
        prime(rig)
        with app_db.SessionLocal() as db:
            with pytest.raises(Exception):
                async with rig.scope(db):
                    pytest.fail('precondition was ignored')
    asyncio.run(run())
    assert not rig.posts


@pytest.mark.parametrize('external', ['source', 'topology', 'identity', 'owner_expired'])
def test_external_changes_or_owner_loss_stop_new_writes(rig, external):
    async def run():
        samples = prime(rig)
        with app_db.SessionLocal() as db:
            async with rig.scope(db) as owner:
                await owner.update(samples, now=rig.inputs.now)
                count = len(rig.posts)
                if external == 'source':
                    rig.source = 'http://example.invalid/user-choice'
                elif external == 'topology':
                    rig.zones[MEMBER] = '<zone master="UNOWNED" />'
                elif external == 'identity':
                    rig.bad_identity = OTHER
                else:
                    rig.inputs.now += 6
                with pytest.raises(Exception):
                    await owner.update(rig.frame({OTHER: GROUP}), now=rig.inputs.now)
                assert len(rig.posts) == count and rig.held
    asyncio.run(run())


def test_cancellation_closes_gate_and_does_not_blindly_undo_uncertain_write(rig):
    rig.cancel = True
    async def run():
        samples = prime(rig)
        with app_db.SessionLocal() as db:
            with pytest.raises(asyncio.CancelledError):
                async with rig.scope(db) as owner:
                    await owner.update(samples, now=rig.inputs.now)
    asyncio.run(run())
    assert len(rig.posts) == 1 and rig.held
    assert all(not device_lock(key).locked() for key in ADDRESSES)


def test_firmware_volume_normalization_is_reported_not_overwritten(rig):
    rig.normalization = True
    async def run():
        samples = prime(rig)
        with app_db.SessionLocal() as db:
            async with rig.scope(db) as owner:
                result = await owner.update(samples, now=rig.inputs.now)
                assert result.volume_changed == (MEMBER,) and result.audio_released is False
    asyncio.run(run())
    assert not any(path == '/volume' for _, path, _ in rig.posts)


def test_expired_owner_mid_transaction_prevents_remaining_writes(rig):
    rig.expire_after_write = True
    async def run():
        samples = prime(rig)
        with app_db.SessionLocal() as db:
            async with rig.scope(db) as owner:
                with pytest.raises(Exception):
                    await owner.update(samples, now=rig.inputs.now)
    asyncio.run(run())
    assert len(rig.posts) == 1 and rig.held


def test_failed_start_backup_commit_blocks_every_write(rig, monkeypatch):
    from sqlalchemy.orm import Session
    original = Session.commit
    def failing_commit(db):
        if db.query(ConfigBackup).count():
            raise OSError('synthetic disk full')
        return original(db)
    monkeypatch.setattr(Session, 'commit', failing_commit)
    async def run():
        prime(rig)
        with app_db.SessionLocal() as db:
            with pytest.raises(OSError):
                async with rig.scope(db):
                    pytest.fail('no durable backup')
    asyncio.run(run())
    assert not rig.posts


def test_native_membership_change_during_operation_prevents_remaining_writes(rig):
    rig.change_group_after_write = True
    async def run():
        samples = prime(rig)
        with app_db.SessionLocal() as db:
            async with rig.scope(db) as owner:
                with pytest.raises(Exception):
                    await owner.update(samples, now=rig.inputs.now)
    asyncio.run(run())
    assert len(rig.posts) == 1 and rig.held


@pytest.mark.parametrize('xml', [
    '<volume deviceID="OTHER"><actualvolume>3</actualvolume></volume>',
    '<volume><actualvolume>3</actualvolume><actualvolume>99</actualvolume></volume>',
    '<!DOCTYPE volume [<!ENTITY injected "3">]><volume><actualvolume>&injected;</actualvolume></volume>',
])
def test_ambiguous_or_wrong_identity_volume_does_not_authorize_grouping(rig, xml):
    rig.raw['/volume'] = xml
    async def run():
        prime(rig)
        with app_db.SessionLocal() as db:
            with pytest.raises(Exception):
                async with rig.scope(db):
                    pytest.fail('invalid volume accepted')
    asyncio.run(run())
    assert not rig.posts


def test_lost_final_audit_commit_is_not_success_or_permission_to_replay(rig, monkeypatch):
    from sqlalchemy.orm import Session
    original = Session.commit
    def failing_commit(db):
        if db.query(DeviceActionJournal).filter_by(phase='AIRPLAY_ZONE_READBACK').count():
            raise OSError('synthetic audit failure')
        return original(db)
    monkeypatch.setattr(Session, 'commit', failing_commit)
    async def run():
        samples = prime(rig)
        with app_db.SessionLocal() as db:
            async with rig.scope(db) as owner:
                with pytest.raises(OSError):
                    await owner.update(samples, now=rig.inputs.now)
                assert owner.failed and rig.held
                count = len(rig.posts)
                with pytest.raises(Exception):
                    await owner.update(rig.frame(), now=rig.inputs.now)
                assert len(rig.posts) == count
    asyncio.run(run())


def test_duplicate_target_ip_is_rejected_without_constructing_transports(rig):
    with app_db.SessionLocal() as db:
        db.query(Device).filter_by(device_id=MEMBER).one().ip_address = ADDRESSES[MASTER]
        db.commit()
    async def run():
        prime(rig)
        with app_db.SessionLocal() as db:
            with pytest.raises(Exception):
                async with rig.scope(db):
                    pytest.fail('aliased endpoints accepted')
    asyncio.run(run())
    assert not rig.constructed
