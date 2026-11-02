"""Creation must persist START and reconcile uncertain writes, using mocks only."""
import asyncio
import json
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import ConfigBackup, Device, DeviceActionJournal, Setting
from basswiesn.app.routers import multiroom
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services import zone_creation
from test_settings_write_safety import BUILD, info

pytestmark = pytest.mark.integration
MASTER, MEMBER = 'CREATE-MASTER', 'CREATE-MEMBER'
ADDRESSES = {MASTER: '192.0.2.61', MEMBER: '192.0.2.62'}


class Rig:
    def __init__(self):
        self.zones = dict.fromkeys(ADDRESSES, '<zone />')
        self.volumes = dict.fromkeys(ADDRESSES, 1)
        self.latency = dict.fromkeys(ADDRESSES, 'SYNC_TO_ROOM')
        self.build = BUILD
        self.posts, self.calls, self.constructors, self.backups_at_write = [], [], [], []
        self.lost = self.ignore = self.fail_read = self.wrong_identity = ''
        self.fail_initial, self.bad_readback, self.reject = '', '', ''
        self.cancel = False
        self.raw = {}
        self.changed = False

    def factory(self, ip, **kwargs):
        rig = self
        identity = next(key for key, value in ADDRESSES.items() if value == ip)
        rig.constructors.append(identity)

        class Client:
            async def get_xml(self, path):
                rig.calls.append((identity, path))
                if path == rig.fail_initial:
                    raise httpx.ReadTimeout('simulated unavailable initial state')
                if rig.changed and path == rig.fail_read:
                    raise httpx.ReadTimeout('simulated unavailable readback')
                if rig.changed and path == rig.bad_readback:
                    return '<unexpected />'
                if path in rig.raw:
                    return rig.raw[path]
                if path == '/info':
                    return info(device_id='WRONG' if rig.changed and identity == rig.wrong_identity else identity,
                                build=rig.build)
                if path == '/volume':
                    return f'<volume><actualvolume>{rig.volumes[identity]}</actualvolume></volume>'
                if path == '/getZone':
                    return rig.zones[identity]
                if path == '/rebroadcastlatencymode':
                    return f'<rebroadcastlatencymode mode="{rig.latency[identity]}" />'
                raise AssertionError(path)

            async def post_xml(self, path, xml):
                assert all(device_lock(key).owned_by_current_task() for key in ADDRESSES)
                with app_db.SessionLocal() as db:
                    backups = db.query(ConfigBackup).all()
                    rig.backups_at_write.append([json.loads(row.content) for row in backups])
                rig.posts.append((identity, path, xml))
                rig.changed = True
                if rig.cancel:
                    raise asyncio.CancelledError()
                if rig.ignore != path:
                    if path == '/volume':
                        rig.volumes[identity] = int(ET.fromstring(xml).text)
                    elif path == '/rebroadcastlatencymode':
                        rig.latency[identity] = ET.fromstring(xml).get('mode')
                    elif path == '/setZone':
                        rig.zones = dict.fromkeys(ADDRESSES, xml)
                    else:
                        raise AssertionError(path)
                if path == rig.lost:
                    raise httpx.ReadTimeout('lost response after action')
                if path == rig.reject:
                    raise httpx.HTTPStatusError('rejected despite observed state',
                        request=httpx.Request('POST', 'http://example.invalid'), response=httpx.Response(400))
                return '<status>OK</status>'

        return Client()


@pytest.fixture
def rig(monkeypatch):
    rig = Rig()
    with app_db.SessionLocal() as db:
        for identity, address in ADDRESSES.items():
            db.add(Device(device_id=identity, ip_address=address, name=identity,
                          model='SoundTouch 30', firmware=BUILD, info_xml=info(device_id=identity)))
        db.commit()
    monkeypatch.setattr(multiroom, 'SoundTouchClient', rig.factory)
    async def no_sleep(_):
        pass
    monkeypatch.setattr(multiroom.asyncio, 'sleep', no_sleep)
    return rig


def request(**options):
    with TestClient(create_web_app(background_tasks=False), raise_server_exceptions=False) as client:
        return client.post('/api/multiroom/set', json={
            'master_device_id': MASTER, 'member_device_ids': [MEMBER],
            'memory_checked': True, 'dry_run': False, 'preserve_volumes': True,
            **options,
        })


def test_durable_complete_snapshot_exists_before_first_zone_creation_write(rig):
    response = request()
    assert response.status_code == 200, response.text
    assert rig.backups_at_write and all(rig.backups_at_write)
    with app_db.SessionLocal() as db:
        backup = db.query(ConfigBackup).one()
        saved = json.loads(backup.content)
        assert set(saved['devices']) == set(ADDRESSES)
        for state in saved['devices'].values():
            assert set(state) == {'/info', '/getZone', '/volume', '/rebroadcastlatencymode'}
        assert db.query(DeviceActionJournal).filter_by(phase='ZONE_CREATION_INTENT').count() == 1


def test_unapproved_firmware_is_rejected_before_creation_writes(rig):
    rig.build = '27.0.7.unknown'
    response = request()
    assert response.status_code == 409, response.text
    assert not rig.posts


@pytest.mark.parametrize('path', ['/rebroadcastlatencymode', '/setZone'])
def test_lost_response_is_read_back_without_replaying_write(rig, path):
    rig.lost = path
    response = request()
    assert response.status_code == 200, response.text
    matching = [(identity, endpoint) for identity, endpoint, _ in rig.posts if endpoint == path]
    assert len(matching) == len(set(matching))
    assert all(item['ok'] for item in response.json()['verification'])


@pytest.mark.parametrize('path', ['/rebroadcastlatencymode', '/setZone'])
def test_http_success_without_state_change_is_diverged_and_stops_batch(rig, path):
    rig.ignore = path
    response = request()
    assert response.status_code == 502, response.text
    assert response.json()['detail']['status'] == 'DIVERGED'
    assert response.json()['detail']['automatic_retry'] is False
    assert response.json()['detail']['reconciliation_required'] is True
    if path == '/rebroadcastlatencymode':
        assert not any(endpoint == '/setZone' for _, endpoint, _ in rig.posts)


def test_unknown_readback_after_a_write_retains_durable_failure_evidence(rig):
    rig.fail_read = '/rebroadcastlatencymode'
    response = request()
    assert response.status_code == 502, response.text
    assert len(rig.posts) == 1
    with app_db.SessionLocal() as db:
        assert db.query(ConfigBackup).count() == 1
        assert db.query(DeviceActionJournal).filter_by(phase='ZONE_CREATION_DIVERGED').count() == 1


def test_actual_changed_identity_stops_remaining_creation_steps(rig):
    rig.wrong_identity = MASTER
    response = request()
    assert response.status_code == 502, response.text
    assert len(rig.posts) == 1


def test_ip_write_guard_blocks_before_constructing_any_radio_client(rig):
    with app_db.SessionLocal() as db:
        db.add(Setting(key='ip_write_guard', value='true'))
        db.commit()
    response = request()
    assert response.status_code == 403
    assert not rig.constructors and not rig.posts


def test_failed_backup_commit_prevents_every_radio_write(rig, monkeypatch):
    with app_db.SessionLocal() as db:
        session_type = type(db)
    original = session_type.commit
    def fail_commit(session):
        # Do not inject into unrelated application startup; fail the actual
        # persisted START gate before the radio transport is allowed to write.
        if session.query(ConfigBackup).count():
            raise RuntimeError('simulated persistence failure')
        return original(session)
    monkeypatch.setattr(session_type, 'commit', fail_commit)
    response = request()
    assert response.status_code >= 400
    assert not rig.posts


@pytest.mark.parametrize('path', zone_creation.READS)
def test_missing_initial_state_is_not_a_default_value(rig, path):
    rig.fail_initial = path
    assert request().status_code == 409
    assert not rig.posts


@pytest.mark.parametrize('path,xml', [
    ('/volume', '<volume><targetvolume>1</targetvolume></volume>'),
    ('/volume', '<volume><actualvolume>-1</actualvolume></volume>'),
    ('/volume', '<volume><actualvolume>101</actualvolume></volume>'),
    ('/volume', '<volume deviceID="OTHER"><actualvolume>1</actualvolume></volume>'),
    ('/rebroadcastlatencymode', '<rebroadcastlatencymode mode="FUTURE" />'),
    ('/getZone', '<zone master="UNSELECTED" />'),
    ('/getZone', '<zone master="CREATE-MASTER"><member ipaddress="192.0.2.99">CREATE-MEMBER</member></zone>'),
    ('/info', '<!DOCTYPE info [<!ENTITY a "x">]><info>&a;</info>'),
])
def test_ambiguous_or_unsafe_snapshot_blocks_the_first_write(rig, path, xml):
    rig.raw[path] = xml
    assert request().status_code == 409
    assert not rig.posts


@pytest.mark.parametrize('path', ['/rebroadcastlatencymode', '/setZone'])
def test_explicit_http_rejection_does_not_become_success(rig, path):
    rig.reject = path
    response = request()
    assert response.status_code == 502
    assert response.json()['detail']['status'] == 'DIVERGED'
    assert len({(identity, endpoint) for identity, endpoint, _ in rig.posts}) == len(rig.posts)


@pytest.mark.parametrize('failure', ['ignore', 'lost'])
def test_individual_start_volume_needs_actual_readback_not_just_a_reply(rig, failure):
    setattr(rig, failure, '/volume')
    response = request(preserve_volumes=False, set_start_volumes=True,
                       start_volumes={MASTER: 2, MEMBER: 3})
    assert response.status_code == (502 if failure == 'ignore' else 200), response.text
    volume_posts = [(identity, path) for identity, path, _ in rig.posts if path == '/volume']
    assert len(volume_posts) == len(set(volume_posts))
    if failure == 'ignore':
        assert len(rig.posts) == 1


@pytest.mark.parametrize('path', ['/info', '/volume', '/getZone', '/rebroadcastlatencymode'])
def test_malformed_post_state_is_not_confirmed(rig, path):
    rig.bad_readback = path
    response = request()
    assert response.status_code == 502, response.text
    assert response.json()['detail']['reconciliation_required']
    assert len(rig.posts) <= 3


def test_changed_topology_after_backup_is_not_overwritten(rig, monkeypatch):
    with app_db.SessionLocal() as db:
        session_type = type(db)
    original = session_type.commit
    def change_after_commit(session):
        original(session)
        if session.query(ConfigBackup).count() and not rig.posts:
            rig.zones[MEMBER] = '<zone master="EXTERNAL" />'
    monkeypatch.setattr(session_type, 'commit', change_after_commit)
    response = request()
    assert response.status_code == 409, response.text
    assert not rig.posts


def test_corrupted_persisted_backup_stops_before_write(rig, monkeypatch):
    with app_db.SessionLocal() as db:
        session_type = type(db)
    original = session_type.commit
    damaged = False
    def damage_after_commit(session):
        nonlocal damaged
        original(session)
        if not damaged and session.query(ConfigBackup).count():
            damaged = True
            session.query(ConfigBackup).update({'content': '{}'})
            original(session)
    monkeypatch.setattr(session_type, 'commit', damage_after_commit)
    response = request()
    assert response.status_code == 409, response.text
    assert response.json()['detail']['error'] == 'ZONE_CREATION_BACKUP_INTEGRITY'
    assert not rig.posts


def test_journal_failure_after_write_stops_batch_without_replay(rig, monkeypatch):
    with app_db.SessionLocal() as db:
        session_type = type(db)
    original = session_type.commit
    def fail_after_write(session):
        if rig.posts and zone_creation._active.get() is not None:
            raise RuntimeError('simulated journal failure after radio mutation')
        return original(session)
    monkeypatch.setattr(session_type, 'commit', fail_after_write)
    response = request()
    assert response.status_code == 502, response.text
    assert len(rig.posts) == 1
    with app_db.SessionLocal() as db:
        assert db.query(ConfigBackup).count() == 1
        assert db.query(DeviceActionJournal).filter_by(phase='ZONE_CREATION_WRITE_INTENT').count() == 1


def test_cancellation_releases_all_ownership_but_keeps_uncertain_write_evidence(rig):
    rig.cancel = True
    async def run():
        with app_db.SessionLocal() as db:
            with pytest.raises(asyncio.CancelledError):
                await multiroom.multiroom_set({
                    'master_device_id': MASTER, 'member_device_ids': [MEMBER],
                    'dry_run': False, 'memory_checked': True, 'preserve_volumes': True,
                }, db)
            assert zone_creation._active.get() is None
            assert db.query(DeviceActionJournal).filter_by(phase='ZONE_CREATION_DIVERGED').count() == 1
        assert all(not device_lock(identity).locked() for identity in ADDRESSES)
    asyncio.run(run())
    assert len(rig.posts) == 1


@pytest.mark.parametrize('identity', [MASTER, MEMBER])
def test_any_protected_participant_blocks_before_every_transport(rig, identity):
    with app_db.SessionLocal() as db:
        db.add(Setting(key='protected_device_ids', value=identity))
        db.commit()
    assert request().status_code == 403
    assert not rig.constructors and not rig.posts


def test_dry_preview_neither_connects_nor_creates_a_backup(rig):
    assert request(dry_run=True).status_code == 200
    assert not rig.calls and not rig.constructors and not rig.posts
    with app_db.SessionLocal() as db:
        assert not db.query(ConfigBackup).count()


@pytest.mark.parametrize('path', ['/now_playing', '/getZone?other=1', '/volume/', '/key'])
def test_zone_readback_authority_rejects_unplanned_routes_before_xml_parsing(path):
    # The shared AP2 helper must not silently expand the zone transaction's
    # read authority. Bad XML ensures rejection is by route, not parser accident.
    device = Device(device_id=MASTER, ip_address=ADDRESSES[MASTER])
    with pytest.raises(ValueError, match='^unplanned zone read$'):
        zone_creation.read_zone_value(path, 'not XML', device)


@pytest.mark.parametrize('changed_target', [False, True])
def test_creation_rechecks_target_and_task_ownership_before_transport(changed_target):
    guarded = []

    def forbidden_client(device):
        pytest.fail('an unowned or changed target must not construct a transport')

    async def run():
        with app_db.SessionLocal() as db:
            device = Device(device_id=MASTER, ip_address=ADDRESSES[MASTER],
                            model='SoundTouch 30', firmware=BUILD, info_xml=info(device_id=MASTER))
            ports = zone_creation.ZonePorts(
                client=forbidden_client, guard=lambda session, target: guarded.append(target.device_id))
            creation = zone_creation.Creation(db, [device], ports, {})
            if changed_target:
                device.ip_address = '192.0.2.99'
            code = ('ZONE_CREATION_TARGET_CHANGED' if changed_target
                    else 'ZONE_CREATION_OWNERSHIP_REQUIRED')
            with pytest.raises(zone_creation.ZoneCreationError, match=code) as caught:
                await creation.prepare()
            assert guarded == [MASTER]
            assert not creation.clients and not creation.attempted
            assert caught.value.detail['status'] == 'BLOCKED'
            assert caught.value.detail['automatic_retry'] is False
            assert db.query(ConfigBackup).count() == 0

    asyncio.run(run())
