"""Distributed failures must not become successful zone removal. No radios."""
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
from basswiesn.app.services import zone_changes
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.zone_changes import Zone, ZoneChangeError, ZonePorts, change_zone
from test_settings_write_safety import BUILD, info
from test_admin_boundary import secured, authenticate  # noqa: F401

pytestmark = pytest.mark.integration
MASTER, MEMBER, OTHER = 'ZONE-MASTER', 'ZONE-MEMBER', 'ZONE-OTHER'
ADDRESSES = {MASTER: '192.0.2.41', MEMBER: '192.0.2.42', OTHER: '192.0.2.43'}


class Rig:
    def __init__(self):
        self.zone = (f'<zone master="{MASTER}"><member ipaddress="{ADDRESSES[MEMBER]}">{MEMBER}</member>'
                     f'<member ipaddress="{ADDRESSES[OTHER]}">{OTHER}</member></zone>')
        self.zones = dict.fromkeys(ADDRESSES, self.zone)
        self.calls, self.constructed, self.posts = [], [], []
        self.lost_reply, self.rejected, self.ignore = False, False, False
        self.fail_after, self.fail_before, self.wrong_identity, self.malformed = '', '', '', ''
        self.changed = False
        self.build = BUILD
        self.bad_reply_ip = False

    def factory(self, ip, **kwargs):
        rig = self
        identity = next(key for key, value in ADDRESSES.items() if value == ip)
        self.constructed.append(identity)
        class Client:
            async def get_xml(self, path):
                rig.calls.append((identity, path))
                if identity == rig.fail_before or (rig.changed and identity == rig.fail_after):
                    raise httpx.ReadTimeout('simulated read failure')
                if path == '/info':
                    return info(device_id='CHANGED' if rig.changed and identity == rig.wrong_identity else identity,
                                build=rig.build)
                assert path == '/getZone'
                if rig.changed and identity == rig.malformed:
                    return '<status>OK</status>'
                zone = rig.zones[identity]
                return zone.replace(ADDRESSES[OTHER], '192.0.2.99') if rig.changed and rig.bad_reply_ip else zone

            async def post_xml(self, path, xml):
                assert path == '/setZone' and identity == MASTER
                assert all(device_lock(key).owned_by_current_task() for key in ADDRESSES)
                with app_db.SessionLocal() as db:
                    backup = db.query(ConfigBackup).one()
                    assert set(json.loads(backup.content)) == set(ADDRESSES)
                    assert db.query(DeviceActionJournal).filter_by(phase='ZONE_INTENT').count() == 1
                rig.posts.append((identity, path, xml))
                rig.changed = True
                if rig.rejected:
                    raise httpx.HTTPStatusError('rejected', request=httpx.Request('POST', 'http://example.invalid'),
                                                response=httpx.Response(400))
                if not rig.ignore:
                    remaining = {node.text for node in ET.fromstring(xml).findall('member')}
                    rig.zones[MASTER] = xml
                    for key in (MEMBER, OTHER):
                        rig.zones[key] = xml if key in remaining else '<zone />'
                if rig.lost_reply:
                    raise httpx.ReadTimeout('lost reply after applying change')
                return '<status>OK</status>'
        return Client()


@pytest.fixture
def rig(monkeypatch):
    rig = Rig()
    with app_db.SessionLocal() as db:
        for identity, address in ADDRESSES.items():
            db.add(Device(device_id=identity, ip_address=address, name=identity, model='SoundTouch 30',
                          firmware=BUILD, info_xml=info(device_id=identity)))
        db.commit()
    monkeypatch.setattr(multiroom, 'SoundTouchClient', rig.factory)
    async def no_sleep(_):
        pass
    monkeypatch.setattr(zone_changes.asyncio, 'sleep', no_sleep)
    return rig


def request(action):
    client = TestClient(create_web_app(background_tasks=False), raise_server_exceptions=False)
    if action == 'remove':
        return client.post('/api/multiroom/remove-device', json={'device_id': MEMBER, 'confirmation': 'REMOVE MEMBER'})
    if action == 'clear-all':
        return client.post('/api/multiroom/clear-all', json={})
    return client.post('/api/multiroom/clear', json={'master_device_id': MASTER, 'dry_run': False, 'memory_checked': True})


@pytest.mark.parametrize('action', ['remove', 'clear', 'clear-all'])
@pytest.mark.parametrize('lost', [False, True])
def test_single_write_owns_all_participants_and_requires_distributed_readback(rig, action, lost):
    rig.lost_reply = lost
    response = request(action)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['removed' if action == 'remove' else 'cleared'] is True
    assert len(rig.posts) == 1
    assert all(not device_lock(key).locked() for key in ADDRESSES)
    with app_db.SessionLocal() as db:
        assert db.query(DeviceActionJournal).filter_by(phase='VERIFIED', verified=True).count() == 1
        assert db.query(Device).count() == 3


@pytest.mark.parametrize('action', ['remove', 'clear'])
@pytest.mark.parametrize('failure,identity', [
    ('fail_after', MASTER), ('fail_after', MEMBER), ('fail_after', OTHER),
    ('wrong_identity', MEMBER), ('malformed', OTHER), ('ignore', True), ('rejected', True),
])
def test_unavailable_or_wrong_readback_is_never_success(rig, action, failure, identity):
    setattr(rig, failure, identity)
    response = request(action)
    assert response.status_code == 502, response.text
    assert response.json()['detail']['status'] == 'DIVERGED'
    assert len(rig.posts) == 1
    with app_db.SessionLocal() as db:
        assert db.query(DeviceActionJournal).filter_by(verified=True).count() == 0
        assert db.query(DeviceActionJournal).filter_by(phase='READBACK_MISMATCH').count() == 1


@pytest.mark.parametrize('action', ['remove', 'clear', 'clear-all'])
def test_unreachable_participant_before_write_blocks_change(rig, action):
    rig.fail_before = OTHER
    response = request(action)
    if action == 'clear-all':
        assert not response.json()['cleared'] and not response.json()['already_clear']
    else:
        assert response.status_code == 409, response.text
    assert not rig.posts


@pytest.mark.parametrize('target', [MASTER, MEMBER, OTHER])
def test_protected_indirect_member_is_never_contacted_or_modified(rig, target):
    with app_db.SessionLocal() as db:
        db.add(Setting(key='protected_device_ids', value=target))
        db.commit()
    response = request('clear')
    assert response.status_code == 403, response.text
    assert target not in rig.constructed and not rig.posts


def test_unknown_or_moved_member_cannot_be_silently_dropped(rig):
    rig.zones[MASTER] = rig.zone.replace(OTHER, 'UNKNOWN')
    response = request('clear')
    assert response.status_code == 409 and not rig.posts
    rig.zones[MASTER] = rig.zone.replace(ADDRESSES[OTHER], '192.0.2.99')
    assert request('clear').status_code == 409 and not rig.posts


def test_changed_topology_between_plan_and_lock_never_writes(rig):
    async def run():
        with app_db.SessionLocal() as db:
            master = db.query(Device).filter_by(device_id=MASTER).one()
            rig.zones[MASTER] = '<zone />'
            ports = ZonePorts(client=lambda device: rig.factory(device.ip_address), guard=lambda *_: None)
            with pytest.raises(ZoneChangeError, match='ZONE_PRECONDITION_CHANGED'):
                await change_zone(db, master, Zone.parse(rig.zone), ports=ports)
    asyncio.run(run())
    assert not rig.posts


def test_backup_failure_prevents_transport_write(rig, monkeypatch):
    from sqlalchemy.orm import Session
    monkeypatch.setattr(Session, 'commit', lambda _: (_ for _ in ()).throw(OSError('simulated full disk')))
    response = request('clear')
    assert response.status_code == 503 and not rig.posts
    assert all(not device_lock(key).locked() for key in ADDRESSES)


def test_clear_all_does_not_call_offline_inventory_already_clear(rig):
    rig.fail_before = MASTER
    rig.zones[MEMBER] = rig.zones[OTHER] = '<zone />'
    result = request('clear-all').json()
    assert not result['already_clear'] and not result['cleared']
    assert len(result['unreachable']) == 1 and not rig.posts


def test_preview_and_ip_write_guard_never_construct_write_transport(rig):
    client = TestClient(create_web_app(background_tasks=False))
    response = client.post('/api/multiroom/clear', json={'master_device_id': MASTER, 'dry_run': True})
    assert response.status_code == 200 and not rig.constructed
    with app_db.SessionLocal() as db:
        db.add(Setting(key='ip_write_guard', value='true'))
        db.commit()
    assert request('clear').status_code == 403 and not rig.constructed


@pytest.mark.parametrize('xml', ['<status/>', '<zone><member>A</member></zone>',
    '<zone master="A"><member/></zone>', '<zone master="A"><member>B</member><member>B</member></zone>'])
def test_malformed_topology_is_not_standalone(xml):
    with pytest.raises(ValueError):
        Zone.parse(xml)


@pytest.mark.parametrize('failure', ['former-master', 'hidden-member', 'wrong-ip', 'malformed', 'unavailable'])
def test_create_zone_blocks_unowned_or_unknown_topology_before_any_write(rig, failure):
    # Only two selected participants. The third must not be implicitly detached
    # merely because it appeared in a read response.
    root = f'<zone master="{MASTER}"><member ipaddress="{ADDRESSES[MEMBER]}">{MEMBER}</member></zone>'
    rig.zones = dict.fromkeys(ADDRESSES, root)
    if failure == 'former-master':
        rig.zones[MEMBER] = f'<zone master="{OTHER}" />'
    elif failure == 'hidden-member':
        rig.zones[MASTER] = rig.zone
    elif failure == 'wrong-ip':
        rig.zones[MASTER] = root.replace(ADDRESSES[MEMBER], '192.0.2.99')
    elif failure == 'malformed':
        rig.zones[MASTER] = '<status/>'
    else:
        rig.fail_before = MEMBER
    client = TestClient(create_web_app(background_tasks=False))
    response = client.post('/api/multiroom/set', json={
        'master_device_id': MASTER, 'member_device_ids': [MEMBER],
        'dry_run': False, 'memory_checked': True, 'preserve_volumes': True,
    })
    assert response.status_code in {409, 503}, response.text
    assert not rig.posts


@pytest.mark.parametrize('kind', ['foreign-master', 'remove-master', 'remove-missing', 'conflicting-member'])
def test_service_rejects_invalid_scope_before_writing(rig, kind):
    async def run():
        with app_db.SessionLocal() as db:
            master = db.query(Device).filter_by(device_id=MASTER).one()
            removed = None
            expected = Zone.parse(rig.zone)
            if kind == 'foreign-master':
                expected = Zone('UNRELATED', expected.members)
            elif kind == 'remove-master':
                removed = master
            elif kind == 'remove-missing':
                removed = Device(device_id='MISSING')
            else:
                rig.zones[OTHER] = '<zone master="UNRELATED" />'
            with pytest.raises(ZoneChangeError):
                await change_zone(db, master, expected, ports=multiroom._zone_change_ports(), remove=removed)
    asyncio.run(run())
    assert not rig.posts


@pytest.mark.parametrize('failure', ['changed', 'unavailable'])
def test_external_change_after_backup_stops_before_post(rig, monkeypatch, failure):
    original = zone_changes.read_zone
    calls = []
    async def altered(device, ports):
        calls.append(device.device_id)
        if len(calls) == 5:  # discovery + three START snapshots; now precondition
            if failure == 'unavailable':
                raise httpx.ReadTimeout('readback disappeared')
            rig.zones[device.device_id] = '<zone />'
        return await original(device, ports)
    monkeypatch.setattr(zone_changes, 'read_zone', altered)
    # Router's initial discovery imports read_zone directly, so this service's
    # fourth call is already the first precondition check. The fifth remains
    # before POST and targets a member.
    response = request('clear')
    assert response.status_code == 409, response.text
    assert not rig.posts
    with app_db.SessionLocal() as db:
        assert db.query(ConfigBackup).count() == 1


@pytest.mark.parametrize('failure', [False, True])
def test_authenticated_zone_removal_keeps_the_same_hardware_safety_contract(secured, rig, failure):
    client = TestClient(create_web_app(background_tasks=False), base_url='https://testserver')
    body = {'device_id': MEMBER, 'confirmation': 'REMOVE MEMBER'}
    assert client.post('/api/multiroom/remove-device', json=body,
                       headers={'Origin': 'https://testserver'}).status_code == 401
    assert not rig.constructed
    csrf = authenticate(client)
    assert client.post('/api/multiroom/remove-device', json=body).status_code == 403
    assert not rig.constructed
    if failure:
        rig.fail_after = MEMBER
    response = client.post('/api/multiroom/remove-device', json=body,
                           headers={'Origin': 'https://testserver', 'X-BASSWIESN-CSRF': csrf})
    assert response.status_code == (502 if failure else 200), response.text
    assert len(rig.posts) == 1


def test_new_zone_service_does_not_implicitly_expand_automation_grants(secured, rig):
    from basswiesn.app.core.api_tokens import ApiTokenStore
    token = ApiTokenStore(secured[1]).issue('offline scope check', ['multiroom', 'admin'], 3600)
    client = TestClient(create_web_app(background_tasks=False), base_url='https://testserver')
    response = client.post('/api/multiroom/clear-all', json={},
                           headers={'Authorization': f'Bearer {token.token}'})
    assert response.status_code == 403 and not rig.constructed


def test_unknown_exact_firmware_never_gets_zone_write_permission(rig):
    rig.build = '27.0.6.UNAPPROVED'
    with app_db.SessionLocal() as db:
        master = db.query(Device).filter_by(device_id=MASTER).one()
        master.firmware = rig.build
        master.info_xml = info(device_id=MASTER, build=rig.build)
        db.commit()
    response = request('clear')
    assert response.status_code == 503 and not rig.posts


@pytest.mark.parametrize('damage', ['corrupt', 'delete'])
def test_persisted_zone_backup_is_verified_before_post(rig, monkeypatch, damage):
    from sqlalchemy.orm import Session
    original = Session.commit
    changed = False

    def corrupt_snapshot(session):
        nonlocal changed
        original(session)
        backup = session.query(ConfigBackup).first()
        if backup is not None and not changed:
            changed = True
            # Keep the top-level shape to ensure this tests integrity rather
            # than the transport fixture's basic snapshot-presence assertion.
            if damage == 'delete':
                session.delete(backup)
            else:
                backup.content = json.dumps(dict.fromkeys(ADDRESSES, {}))
            original(session)

    monkeypatch.setattr(Session, 'commit', corrupt_snapshot)
    response = request('clear')
    assert response.status_code == 409, response.text
    assert response.json()['detail']['error'] == 'ZONE_BACKUP_INTEGRITY'
    assert not rig.posts


def test_zone_target_address_cannot_change_during_durable_preparation(rig, monkeypatch):
    from sqlalchemy.orm import Session
    original = Session.commit
    changed = False

    def move_target(session):
        nonlocal changed
        original(session)
        if session.query(ConfigBackup).count() and not changed:
            changed = True
            session.query(Device).filter_by(device_id=MASTER).one().ip_address = '192.0.2.99'
            original(session)

    monkeypatch.setattr(Session, 'commit', move_target)
    # Even if a changed endpoint supplies the same expected identity, this
    # operation was authorized for the original endpoint, not rediscovery.
    monkeypatch.setattr(multiroom, 'SoundTouchClient',
                        lambda ip, **kwargs: rig.factory(ADDRESSES[MASTER] if ip == '192.0.2.99' else ip, **kwargs))
    response = request('clear')
    assert response.status_code == 409, response.text
    assert response.json()['detail']['error'] == 'ZONE_TARGET_CHANGED'
    assert not rig.posts


def test_topology_response_cannot_claim_a_different_device(rig):
    rig.zones[MASTER] = rig.zone.replace('<zone ', '<zone deviceID="WRONG" ', 1)
    response = request('clear')
    assert response.status_code >= 400, response.text
    assert not rig.posts


def test_verified_group_cannot_contain_a_contradictory_member_address(rig):
    rig.bad_reply_ip = True
    response = request('remove')
    assert response.status_code == 502, response.text
    assert response.json()['detail']['status'] == 'DIVERGED'
    assert len(rig.posts) == 1


def test_failed_final_journal_commit_is_uncertain_and_never_replayed(rig, monkeypatch):
    from sqlalchemy.orm import Session
    original = Session.commit

    def fail_final(session):
        if rig.changed:
            raise OSError('simulated failed final journal commit')
        return original(session)

    monkeypatch.setattr(Session, 'commit', fail_final)
    response = request('clear')
    assert response.status_code == 502, response.text
    assert response.json()['detail']['status'] == 'DIVERGED'
    assert response.json()['detail']['error'] == 'ZONE_FINAL_RECORD_FAILED'
    assert response.json()['detail']['backup_ref'].startswith('config_backup:')
    assert len(rig.posts) == 1
    with app_db.SessionLocal() as db:
        assert db.query(DeviceActionJournal).filter_by(phase='ZONE_INTENT').count() == 1
        assert db.query(DeviceActionJournal).filter_by(verified=True).count() == 0


def test_selected_but_conflicting_coordinator_blocks_removal(rig):
    rig.zones[MEMBER] = f'<zone master="{OTHER}"><member>{MASTER}</member></zone>'
    response = request('clear')
    assert response.status_code == 409, response.text
    assert response.json()['detail']['error'] == 'ZONE_CONFLICTING_TOPOLOGY'
    assert not rig.posts


def test_valid_explicit_zone_response_identity_remains_supported(rig):
    rig.zones = {identity: xml.replace('<zone ', f'<zone deviceID="{identity.lower()}" ', 1)
                 for identity, xml in rig.zones.items()}
    response = request('clear')
    assert response.status_code == 200, response.text
    assert len(rig.posts) == 1
