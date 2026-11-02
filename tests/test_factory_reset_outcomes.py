"""LAB reset outcome accounting uses only fake adapters, never a radio."""
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, DeviceActionJournal, DiagnosticEvent, Setting
from basswiesn.app.routers import api
from basswiesn.app.services.setup_rebuild import cli17000
from basswiesn.app.services import factory_reset as reset_service
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.setup_rebuild.candidates import candidate_from_device
from test_bugfix_251 import _profiled_device, PROFILED_DEVICE_ID
from factory_reset_fixtures import make_reset_baseline, reset_backup_directory  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
def rig(monkeypatch):
    calls = {'writes': 0, 'identities': 0, 'fail_send': False, 'fail_backup': False,
             'switch_identity': False, 'durable_intent': False, 'after_backup': lambda: None,
             'after_identity': lambda: None, 'identity_override': {}, 'zone': '<zone/>', 'cancel_send': False}
    class Adapter:
        async def identify(self, row):
            calls['identities'] += 1
            calls['after_identity']()
            return {'device_id': ('BB22CC33DD44' if calls['switch_identity'] and calls['identities'] > 1 else row.device_id),
                    'model': row.expected_model, 'firmware': '27.0.6.46330.5043500 epdbuild.release',
                    'product_id': '0X093B', 'variant': 'spotty', 'platform': 'sm2', 'write_profile': 'test-profile', **calls['identity_override']}
        async def backup(self, row):
            if calls['fail_backup']:
                raise OSError('private-upstream-secret-example')
            calls['backup'] = make_reset_baseline(_profiled_device())
            calls['after_backup']()
            return calls['backup']
    class Radio:
        async def get_xml(self, path):
            assert path == '/getZone'
            return calls['zone']
    async def send(*args):
        with app_db.SessionLocal() as db:
            calls['durable_intent'] = db.query(DeviceActionJournal).filter_by(phase='factory_reset_intent').count() == 1
        calls['writes'] += 1
        if calls['cancel_send']:
            raise asyncio.CancelledError()
        if calls['fail_send']:
            raise TimeoutError('private-upstream-secret-example')
        return cli17000.CliResult('factory_reset', (), '')
    monkeypatch.setattr(api, 'RadioSetupAdapter', Adapter)
    monkeypatch.setattr(api, 'cli_factory_reset', send)
    monkeypatch.setattr(api, 'SoundTouchClient', lambda *a, **kw: Radio())
    with app_db.SessionLocal() as db:
        db.add(_profiled_device())
        db.add(Setting(key='ui_mode', value='lab'))
        db.commit()
    with TestClient(create_web_app(background_tasks=False), raise_server_exceptions=False) as client:
        yield calls, client


def reset(client, **extra):
    return client.post(f'/api/lab/devices/{PROFILED_DEVICE_ID}/factory-reset',
        json={'dry_run': False, 'acknowledged': True, 'confirmation': 'FACTORY RESET RADIO', **extra})


def test_lost_command_reply_is_unknown_not_not_sent(rig):
    calls, client = rig
    calls['fail_send'] = True
    response = reset(client)
    assert response.status_code == 502
    assert response.json()['detail']['error'] == 'FACTORY_RESET_OUTCOME_UNKNOWN'
    assert 'private-upstream-secret-example' not in response.text
    assert calls['writes'] == 1 and calls['durable_intent']
    with app_db.SessionLocal() as db:
        events = db.query(DiagnosticEvent).filter_by(device_id=PROFILED_DEVICE_ID).all()
        assert all('was not sent' not in row.message for row in events)


def test_backup_failure_is_not_sent_and_does_not_echo_raw_error(rig):
    calls, client = rig
    calls['fail_backup'] = True
    response = reset(client)
    assert response.status_code == 502
    assert response.json()['detail']['error'] == 'FACTORY_RESET_NOT_SENT'
    assert 'private-upstream-secret-example' not in response.text
    assert calls['writes'] == 0


def test_send_receipt_does_not_invent_unreachable_or_factory_state(rig):
    calls, client = rig
    response = reset(client)
    assert response.status_code == 200
    assert response.json()['verified'] is False
    assert response.json()['reachability'] == 'NOT OBSERVED'
    assert calls['durable_intent']
    with app_db.SessionLocal() as db:
        assert db.query(Device).one().reachable is True
        assert not db.query(DeviceActionJournal).filter_by(verified=True).count()


def test_identity_change_after_backup_prevents_command(rig):
    calls, client = rig
    calls['switch_identity'] = True
    assert reset(client).status_code == 502
    assert calls['writes'] == 0


def test_target_change_after_backup_prevents_command(rig):
    calls, client = rig
    def change():
        with app_db.SessionLocal() as db:
            db.query(Device).one().ip_address = '192.0.2.43'
            db.commit()
    calls['after_backup'] = change
    assert reset(client).status_code == 502
    assert calls['writes'] == 0


def test_cli_sent_is_not_device_verified(monkeypatch):
    events = []
    async def send(*args, **kwargs):
        return ''
    monkeypatch.setattr(cli17000, '_send_fixed', send)
    monkeypatch.setattr(cli17000, 'record_transport_attempt', lambda **kw: events.append(kw))
    asyncio.run(cli17000.factory_reset('192.0.2.42', PROFILED_DEVICE_ID))
    assert events[-1]['verified'] is False


@pytest.mark.parametrize('field,value', [('firmware', '27.0.7.unknown'), ('model', 'SoundTouch 30'),
    ('platform', 'SCM'), ('variant', 'mojo'), ('product_id', '0xffff')])
def test_different_live_hardware_profile_blocks_reset(rig, field, value):
    calls, client = rig
    calls['identity_override'] = {field: value}
    assert reset(client).status_code == 502
    assert calls['writes'] == 0


@pytest.mark.parametrize('value', [None, 0, '', 'false'])
def test_reset_needs_explicit_boolean_write_intent(rig, value):
    calls, client = rig
    assert reset(client, dry_run=value).status_code == 400
    assert calls['identities'] == calls['writes'] == 0


@pytest.mark.parametrize('tamper', ['missing', 'hash', 'valid_xml_hash', 'manifest', 'traversal', 'symlink', 'directory_symlink',
    'hardlink', 'fifo', 'writable', 'huge', 'identity', 'zone', 'bad_xml', 'dtd', 'wrong_root', 'unverified', 'outside'])
def test_unusable_or_unsafe_baseline_prevents_reset(rig, tamper, tmp_path):
    calls, client = rig
    def modify():
        backup = calls['backup']
        root = Path(backup['backup_path'])
        file = root / 'info.xml'
        hashes = backup['sha256']
        if tamper == 'missing': file.unlink()
        if tamper == 'hash': file.write_text('changed')
        if tamper == 'valid_xml_hash': (root / 'presets.xml').write_text('<presets><preset id="1"/></presets>')
        if tamper == 'manifest': hashes['info.xml'] = 'b' * 64
        if tamper == 'traversal': hashes['../escape.xml'] = 'c' * 64
        if tamper == 'symlink':
            file.rename(root / 'other.xml')
            file.symlink_to(root / 'other.xml')
        if tamper == 'directory_symlink':
            moved = root.with_name('other')
            root.rename(moved)
            root.symlink_to(moved, target_is_directory=True)
        if tamper == 'hardlink': os.link(file, root / 'other.xml')
        if tamper == 'fifo':
            file.unlink()
            os.mkfifo(file)
        if tamper == 'writable': file.chmod(0o666)
        if tamper == 'huge': file.write_bytes(b'x' * 1048577)
        if tamper == 'unverified': backup['verified'] = False
        if tamper == 'outside': backup['backup_path'] = str(tmp_path)
        if tamper in {'identity', 'zone', 'bad_xml', 'dtd', 'wrong_root'}:
            file = root / ('getZone.xml' if tamper == 'zone' else 'info.xml')
            text = {'identity': file.read_text().replace(PROFILED_DEVICE_ID, 'BB22CC33DD44'),
                    'zone': '<zone master="BB22CC33DD44"/>', 'bad_xml': '<info',
                    'dtd': '<!DOCTYPE info><info/>', 'wrong_root': '<error/>'}[tamper]
            file.write_text(text)
            hashes[file.name] = sha256(file.read_bytes()).hexdigest()
            manifest = root / 'backup-manifest.json'
            manifest.write_text(json.dumps({'artifacts': {key: value for key, value in hashes.items() if key != manifest.name}}))
            hashes[manifest.name] = sha256(manifest.read_bytes()).hexdigest()
    calls['after_backup'] = modify
    response = reset(client)
    assert response.status_code == 502, response.text
    assert response.json()['detail']['error'] == 'FACTORY_RESET_NOT_SENT'
    assert calls['writes'] == 0


def test_corruption_after_durable_intent_is_rechecked(rig):
    calls, client = rig
    def change():
        if calls['identities'] == 2:
            (Path(calls['backup']['backup_path']) / 'presets.xml').write_text('changed')
    calls['after_identity'] = change
    assert reset(client).status_code == 502
    assert calls['writes'] == 0 and calls['identities'] == 2


def test_zone_join_after_backup_blocks_reset(rig):
    calls, client = rig
    calls['zone'] = f'<zone master="{PROFILED_DEVICE_ID}"><member>BB22CC33DD44</member></zone>'
    assert reset(client).status_code == 502
    assert calls['writes'] == 0


@pytest.mark.parametrize('phase', ['factory_reset_intent', 'factory_reset_sent'])
def test_commit_failure_reports_correct_command_uncertainty(rig, monkeypatch, phase):
    from sqlalchemy.orm import Session
    calls, client = rig
    original = Session.commit
    def commit(db):
        if db.query(DeviceActionJournal).filter_by(phase=phase).count():
            raise OSError('private database failure')
        return original(db)
    monkeypatch.setattr(Session, 'commit', commit)
    response = reset(client)
    assert response.status_code == 502
    expected = 'FACTORY_RESET_NOT_SENT' if phase == 'factory_reset_intent' else 'FACTORY_RESET_OUTCOME_UNKNOWN'
    assert response.json()['detail']['error'] == expected
    assert calls['writes'] == int(phase == 'factory_reset_sent')


def test_cancelled_reset_does_not_retry_and_releases_ownership(rig):
    calls, _ = rig
    calls['cancel_send'] = True
    with app_db.SessionLocal() as db:
        device = db.query(Device).one()
        candidate = candidate_from_device(device, db)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(reset_service.request_factory_reset(db, device, candidate, adapter=api.RadioSetupAdapter(),
                send=api.cli_factory_reset, client_factory=api.SoundTouchClient, guard=api.enforce_ip_write_guard))
    assert calls['writes'] == 1
    assert not device_lock(PROFILED_DEVICE_ID).locked()
    with app_db.SessionLocal() as db:
        assert db.query(DeviceActionJournal).filter_by(phase='factory_reset_diverged').count() == 1
