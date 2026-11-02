"""No hardware: the standalone multiroom latency route is a settings batch."""
import asyncio
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import ConfigBackup, Device, DeviceActionJournal
from basswiesn.app.routers import multiroom

pytestmark = pytest.mark.integration
BUILD = "27.0.6.46330.5043500"
IDS = ["AA11BB22CC33", "AA11BB22CC44", "AA11BB22CC55"]


def info(identity, build=BUILD):
    return (f'<info deviceID="{identity}"><name>Fixture</name><type>SoundTouch 30</type>'
            '<variant>mojo</variant><moduleType>SCM</moduleType><productID>0x0924</productID>'
            f'<components><component><softwareVersion>{build}</softwareVersion></component></components></info>')


class Radio:
    def __init__(self, identity):
        self.identity = identity
        self.build = BUILD
        self.mode = "SYNC_TO_ROOM"
        self.calls = []
        self.lost = self.ignore = self.malformed = self.readback_lost = False
        self.written = False
        self.after_post = lambda: None

    async def get_xml(self, path):
        self.calls.append(("GET", path))
        if self.written and self.readback_lost:
            raise httpx.ReadTimeout("synthetic readback timeout")
        if path == "/info":
            return info(self.identity, self.build)
        assert path == "/rebroadcastlatencymode"
        root = "other" if self.written and self.malformed else "rebroadcastlatencymode"
        return f'<{root} deviceID="{self.identity}" mode="{self.mode}" />'

    async def post_xml(self, path, body):
        self.calls.append(("POST", path))
        assert path == "/rebroadcastlatencymode"
        with app_db.SessionLocal() as db:
            assert db.query(ConfigBackup).filter_by(device_id=self.identity).count() >= 2
            assert db.query(DeviceActionJournal).filter_by(
                device_id=self.identity, phase="settings_intent", verified=False).count() >= 1
        self.written = True
        if not self.ignore:
            self.mode = ET.fromstring(body).get("mode")
        self.after_post()
        if self.lost:
            raise httpx.ReadTimeout("synthetic lost reply")
        return "<status>OK</status>"


@pytest.fixture
def rig(monkeypatch):
    radios = [Radio(identity) for identity in IDS]
    addresses = [f"192.0.2.{42+i}" for i in range(len(IDS))]
    constructors = []
    def factory(ip, **kwargs):
        constructors.append(ip)
        return radios[addresses.index(ip)]
    monkeypatch.setattr(multiroom, "SoundTouchClient", factory)
    with app_db.SessionLocal() as db:
        db.add_all([Device(device_id=identity, ip_address=ip, name="Fixture",
                           model="SoundTouch 30", firmware=BUILD, info_xml=info(identity))
                    for identity, ip in zip(IDS, addresses)])
        db.commit()
    with TestClient(create_web_app(background_tasks=False), raise_server_exceptions=False) as client:
        yield radios, client, constructors


def request(client, ids=IDS, **extra):
    return client.post('/api/multiroom/latency', json={
        'mode':'SYNC_TO_ZONE', 'device_ids':ids, **extra,
    })


def writes(radios):
    return [call for radio in radios for call in radio.calls if call[0] == 'POST']


def test_latency_uses_identity_durable_backup_and_actual_readback(rig):
    radios, client, _ = rig
    response = request(client)
    assert response.status_code == 200, response.text
    assert all(row['ok'] and row['status'] == 'VERIFIED' for row in response.json()['results'])
    assert len(writes(radios)) == 3
    assert all(r.calls[0] == ('GET', '/info') for r in radios)


@pytest.mark.parametrize('change', ['identity', 'firmware'])
def test_all_latency_targets_are_preflighted_before_first_write(rig, change):
    radios, client, _ = rig
    if change == 'identity':
        radios[1].identity = 'BB11BB22CC44'
    else:
        radios[1].build = '27.0.6.unknown'
    response = request(client)
    assert response.status_code == 409, response.text
    assert writes(radios) == []


def test_protected_member_blocks_before_any_transport(rig):
    radios, client, constructors = rig
    with app_db.SessionLocal() as db:
        row = db.query(Device).filter_by(device_id=IDS[1]).one()
        row.ip_address = '192.168.50.25'  # immutable synthetic test protection
        db.commit()
    assert request(client).status_code == 403
    assert constructors == [] and writes(radios) == []


def test_lost_latency_reply_is_read_back_without_retry(rig):
    radios, client, _ = rig
    radios[0].lost = True
    response = request(client, IDS[:1])
    assert response.status_code == 200, response.text
    assert response.json()['results'][0]['transport_uncertain'] is True
    assert len(writes(radios)) == 1


@pytest.mark.parametrize('failure', ['ignore', 'malformed', 'readback_lost'])
def test_partial_latency_batch_stops_and_keeps_verified_prefix(rig, failure):
    radios, client, _ = rig
    setattr(radios[1], failure, True)
    response = request(client)
    assert response.status_code == 409, response.text
    detail = response.json()['detail']
    assert detail['status'] == 'DIVERGED' and detail['reconciliation_required'] is True
    assert [r['device_id'] for r in detail['results']] == IDS[:1]
    assert detail['failed_device_id'] == IDS[1]
    assert len(writes(radios)) == 2
    assert not radios[2].written


def test_latency_batch_rechecks_captured_previous_state(rig):
    radios, client, _ = rig
    radios[0].after_post = lambda: setattr(radios[1], 'mode', 'SYNC_TO_ZONE')
    response = request(client)
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'WRITE_PRECONDITION_FAILED'
    assert len(writes(radios)) == 1


def test_empty_latency_selection_is_not_success(rig):
    radios, client, constructors = rig
    assert request(client, []).status_code == 400
    assert constructors == [] and writes(radios) == []


def test_repeated_verified_latency_is_a_noop_and_ownership_is_released(rig):
    radios, client, _ = rig
    assert request(client).status_code == 200
    assert request(client).status_code == 200
    assert len(writes(radios)) == 3


def test_unexpected_batch_interruption_retains_prefix_without_exception_text(rig, monkeypatch):
    from basswiesn.app.services import device_settings_writer as writer
    radios, client, _ = rig
    original = writer.apply_settings
    async def interrupted(db, device, plans, **kwargs):
        if device.device_id == IDS[1]:
            raise OSError('synthetic private diagnostic value')
        return await original(db, device, plans, **kwargs)
    monkeypatch.setattr(writer, 'apply_settings', interrupted)
    response = request(client)
    assert response.status_code == 409
    detail = response.json()['detail']
    assert detail['code'] == 'SETTINGS_BATCH_INTERRUPTED'
    assert detail['status'] == 'DIVERGED' and len(detail['results']) == 1
    assert 'synthetic private diagnostic value' not in response.text
    assert len(writes(radios)) == 1


@pytest.mark.parametrize('selection', ['empty', 'duplicate'])
def test_internal_batch_rejects_invalid_selection_before_transport(rig, selection):
    from basswiesn.app.services.device_settings_writer import apply_settings_batch
    from basswiesn.app.services.settings_contract import plan_setting
    radios, _, constructors = rig
    with app_db.SessionLocal() as db:
        device = db.query(Device).filter_by(device_id=IDS[0]).one()
        devices = [] if selection == 'empty' else [device, device]
        with pytest.raises(ValueError, match='nonempty unique'):
            asyncio.run(apply_settings_batch(db, devices, plan_setting('rebroadcastlatencymode', 'SYNC_TO_ZONE'),
                        client_factory=multiroom.SoundTouchClient, guard=lambda *_: pytest.fail('guard must not run')))
    assert constructors == [] and writes(radios) == []


def test_unknown_initial_latency_state_blocks_entire_batch(rig):
    radios, client, _ = rig
    radios[1].mode = 'UNRECOGNIZED'
    response = request(client)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'SETTINGS_PREFLIGHT_FAILED'
    assert writes(radios) == []
