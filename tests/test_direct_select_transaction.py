"""Offline regression contract for the explicit display/playback select action."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import ConfigBackup, Device, DeviceActionJournal, RuntimeState, Setting
from basswiesn.app.routers import api
from basswiesn.app.services import direct_selection as selection
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.device_state import runtime_state_key

pytestmark = pytest.mark.integration
ID = "AA11BB22CC33"
BUILD = "27.0.6.46330.5043500"
STREAM = "http://192.0.2.80/radio.mp3"


def info(identity=ID, firmware=BUILD):
    return (f'<info deviceID="{identity}"><name>Example</name><type>SoundTouch 30</type>'
            '<variant>mojo</variant><moduleType>SCM</moduleType><productID>0x0924</productID>'
            f'<components><component><softwareVersion>{firmware}</softwareVersion></component></components></info>')


@pytest.fixture
def rig(monkeypatch):
    class Radio:
        def __init__(self):
            self.constructed = 0
            self.reads = []
            self.writes = []
            self.identity = ID
            self.firmware = BUILD
            self.zone = '<zone/>'
            self.sources = '<sources><sourceItem source="LOCAL_INTERNET_RADIO" sourceAccount="" status="READY"/></sources>'
            self.now = f'<nowPlaying deviceID="{ID}" source="STANDBY"><playStatus>STOP_STATE</playStatus></nowPlaying>'
            self.volume = f'<volume deviceID="{ID}"><targetvolume>1</targetvolume><actualvolume>1</actualvolume><muteenabled>false</muteenabled></volume>'
            self.ignore = False
            self.lost_reply = False
            self.fail_readback = False
            self.cancel_write = False
            self.after_post = lambda: None
            self.after_read = lambda path: None
            self.play_status = "PLAY_STATE"
            self.durable_before_write = False

        async def get_xml(self, path):
            self.reads.append(path)
            if self.writes and self.fail_readback:
                raise TimeoutError("simulated private upstream error")
            value = {"/info": info(self.identity, self.firmware), "/sources": self.sources,
                     "/getZone": self.zone, "/now_playing": self.now,
                     "/volume": self.volume, "/networkInfo": '<networkInfo/>'}[path]
            self.after_read(path)
            return value

        async def post_xml(self, path, xml):
            assert path == "/select"
            with app_db.SessionLocal() as db:
                self.durable_before_write = (db.query(ConfigBackup).count() == 1
                    and db.query(DeviceActionJournal).filter_by(phase="direct_select_intent").count() == 1)
            self.writes.append((path, xml))
            if not self.ignore:
                self.now = (f'<nowPlaying deviceID="{ID}" source="LOCAL_INTERNET_RADIO">{xml}'
                            f'<playStatus>{self.play_status}</playStatus></nowPlaying>')
            self.after_post()
            if self.cancel_write:
                raise asyncio.CancelledError()
            if self.lost_reply:
                raise TimeoutError("simulated lost reply must never be exposed")
            return '<status>OK</status>'

    radio = Radio()
    with app_db.SessionLocal() as db:
        db.add(Device(device_id=ID, ip_address="192.0.2.42", name="Example",
                      model="SoundTouch 30", firmware=BUILD, info_xml=info()))
        db.commit()

    def factory(*args, **kwargs):
        radio.constructed += 1
        return radio

    monkeypatch.setattr(api, "SoundTouchClient", factory)
    with TestClient(create_web_app(background_tasks=False), raise_server_exceptions=False) as client:
        yield radio, client


def select(client, **extra):
    return client.post(f"/api/devices/{ID}/display/direct-select",
                       json={"stream_url": STREAM, "base_label": "Example", "dry_run": False, **extra})


def test_default_preview_does_not_construct_transport_even_if_probe_requested(rig):
    radio, client = rig
    response = client.post(f"/api/devices/{ID}/display/direct-select", json={"stream_url": STREAM, "probe": True})
    assert response.status_code == 200
    assert radio.constructed == 0


def test_changed_identity_blocks_select(rig):
    radio, client = rig
    radio.identity = "BB22CC33DD44"
    assert select(client).status_code == 409
    assert not radio.writes


def test_group_member_is_not_changed_by_a_single_radio_action(rig):
    radio, client = rig
    radio.zone = f'<zone master="BB22CC33DD44"><member ipaddress="192.0.2.42">{ID}</member></zone>'
    assert select(client).status_code == 409
    assert not radio.writes


def test_http_success_without_observed_source_does_not_claim_selected(rig):
    radio, client = rig
    radio.ignore = True
    response = select(client)
    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "DIVERGED"
    assert len(radio.writes) == 1
    with app_db.SessionLocal() as db:
        assert not db.query(RuntimeState).filter_by(key=runtime_state_key(ID)).count()


def test_write_has_durable_start_and_matches_actual_content(rig):
    radio, client = rig
    response = select(client)
    assert response.status_code == 200, response.text
    assert radio.durable_before_write
    result = response.json()
    assert result["status"] == "VERIFIED" and result["audible_audio_verified"] is False
    assert result["readback"]["location"] == STREAM
    assert result["readback"]["play_status"] == "PLAY_STATE"
    assert len(radio.writes) == 1
    assert '/volume' in radio.reads and all(path == '/select' for path, _ in radio.writes)
    with app_db.SessionLocal() as db:
        state = json.loads(db.query(RuntimeState).filter_by(key=runtime_state_key(ID)).one().value)
        assert state['selected_content_item']['location'] == STREAM
        assert state['playback_state'] == 'PLAY_STATE'


def test_lost_post_reply_reconciles_without_replay(rig):
    radio, client = rig
    radio.lost_reply = True
    response = select(client)
    assert response.status_code == 200, response.text
    assert response.json()["transport_uncertain"] is True
    assert len(radio.writes) == 1


@pytest.mark.parametrize('value', [None, 0, 1, 'false', 'true'])
def test_non_boolean_write_intent_is_rejected_without_transport(rig, value):
    radio, client = rig
    assert select(client, dry_run=value).status_code == 400
    assert radio.constructed == 0


@pytest.mark.parametrize('sources', ['<sources/>',
    '<sources><sourceItem source="LOCAL_INTERNET_RADIO" status="UNAVAILABLE"/></sources>',
    '<sources><sourceItem source="LOCAL_INTERNET_RADIO" status="READY" sourceAccount="other"/></sources>',
    '<sources><sourceItem source="LOCAL_INTERNET_RADIO" status="READY"/><sourceItem source="LOCAL_INTERNET_RADIO" status="READY"/></sources>',
    '<sources deviceID="BB22CC33DD44"><sourceItem source="LOCAL_INTERNET_RADIO" status="READY"/></sources>',
    '<sources><sourceItem source="LOCAL_INTERNET_RADIO"/></sources>'])
def test_unavailable_ambiguous_or_other_account_sources_block_write(rig, sources):
    radio, client = rig
    radio.sources = sources
    assert select(client).status_code == 409
    assert not radio.writes


def test_unapproved_firmware_is_not_inferred_from_family(rig):
    radio, client = rig
    radio.firmware = '27.0.7.unknown'
    assert select(client).status_code == 409
    assert not radio.writes


@pytest.mark.parametrize('play_status', ['BUFFERING', 'BUFFERING_STATE'])
def test_buffering_is_pending_not_playback_success(rig, play_status):
    radio, client = rig
    radio.play_status = play_status
    response = select(client)
    assert response.status_code == 202, response.text
    assert response.json()['status'] == 'PENDING_READBACK'
    assert response.json()['verified'] is response.json()['ok'] is False
    with app_db.SessionLocal() as db:
        assert not db.query(DeviceActionJournal).filter_by(verified=True).count()


@pytest.mark.parametrize('status', ['STOP_STATE', 'PAUSE_STATE', 'UNKNOWN', '', 'INVALID_SOURCE'])
def test_selected_content_does_not_prove_started_playback(rig, status):
    radio, client = rig
    radio.play_status = status
    response = select(client)
    assert response.status_code == 409
    assert response.json()['detail']['status'] == 'DIVERGED'
    assert len(radio.writes) == 1


@pytest.mark.parametrize('change', ['identity', 'location', 'account', 'volume', 'zone', 'readback_timeout'])
def test_post_write_divergence_is_not_replayed_or_cached_as_success(rig, change):
    radio, client = rig
    def mutate():
        if change == 'identity': radio.identity = 'BB22CC33DD44'
        if change == 'location': radio.now = radio.now.replace(STREAM, STREAM + '?different')
        if change == 'account': radio.now = radio.now.replace('sourceAccount=""', 'sourceAccount="other"')
        if change == 'volume': radio.volume = radio.volume.replace('<actualvolume>1', '<actualvolume>2')
        if change == 'zone': radio.zone = f'<zone master="{ID}"><member>BB22CC33DD44</member></zone>'
        if change == 'readback_timeout': radio.fail_readback = True
    radio.after_post = mutate
    response = select(client)
    assert response.status_code == 409
    assert response.json()['detail']['status'] == 'DIVERGED'
    assert 'simulated private' not in response.text
    assert len(radio.writes) == 1
    with app_db.SessionLocal() as db:
        assert db.query(DeviceActionJournal).filter_by(phase='direct_select_intent').count() == 1
        assert db.query(DeviceActionJournal).filter_by(phase='direct_select_diverged').count() == 1
        assert not db.query(DeviceActionJournal).filter_by(verified=True).count()


@pytest.mark.parametrize('change', ['target', 'selection', 'volume', 'backup'])
def test_changed_preconditions_abort_before_write(rig, change):
    radio, client = rig
    def mutate(path):
        if path != '/volume' or radio.reads.count('/volume') != 1:
            return
        if change == 'selection': radio.now = radio.now.replace('STANDBY', 'AUX')
        if change == 'volume': radio.volume = radio.volume.replace('<actualvolume>1', '<actualvolume>2')
        if change == 'target':
            with app_db.SessionLocal() as db:
                db.query(Device).filter_by(device_id=ID).one().ip_address = '192.0.2.43'
                db.commit()
        if change == 'backup':
            def tamper(path):
                if path == '/info' and radio.reads.count('/info') == 2:
                    with app_db.SessionLocal() as db:
                        db.query(ConfigBackup).one().content = '{}'
                        db.commit()
            radio.after_read = tamper
    radio.after_read = mutate
    response = select(client)
    assert response.status_code == 409
    assert response.json()['detail']['status'] == 'BLOCKED'
    assert not radio.writes


def test_cancelled_write_keeps_ledger_and_releases_ownership(rig):
    radio, _ = rig
    radio.cancel_write = True
    with app_db.SessionLocal() as db:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(selection.select_direct(db, db.query(Device).one(),
                api._direct_metadata_select_xml('Example', '', STREAM),
                client_factory=api.SoundTouchClient, guard=api.enforce_ip_write_guard))
    assert len(radio.writes) == 1 and not device_lock(ID).locked()
    with app_db.SessionLocal() as db:
        assert db.query(DeviceActionJournal).filter_by(phase='direct_select_diverged').count() == 1


@pytest.mark.parametrize('stage', ['backup', 'result'])
def test_database_commit_failure_cannot_erase_intent_or_claim_success(rig, monkeypatch, stage):
    radio, client = rig
    from sqlalchemy.orm import Session
    original = Session.commit
    def commit(db):
        # record_action flushes, so query on this transaction observes the row.
        phase = 'direct_select_intent' if stage == 'backup' else 'direct_select_readback'
        if db.query(DeviceActionJournal).filter_by(phase=phase).count():
            raise OSError('simulated disk failure with private detail')
        return original(db)
    monkeypatch.setattr(Session, 'commit', commit)
    response = select(client)
    assert response.status_code == 409
    assert 'simulated disk' not in response.text
    assert len(radio.writes) == int(stage == 'result')
    assert response.json()['detail']['status'] == ('DIVERGED' if stage == 'result' else 'BLOCKED')
    with app_db.SessionLocal() as db:
        assert db.query(DeviceActionJournal).filter_by(phase='direct_select_intent').count() == int(stage == 'result')
        assert not db.query(DeviceActionJournal).filter_by(verified=True).count()


@pytest.mark.parametrize('previous', [{}, {'source': 'STANDBY'}, {'source': 'AUX', 'source_account': '', 'location': '', 'play_status': 'STOP_STATE'}])
def test_explicit_expected_previous_is_complete_and_exact(rig, previous):
    radio, client = rig
    assert select(client, expected_previous=previous).status_code == 409
    assert not radio.writes


def test_matching_previous_allows_selection(rig):
    _, client = rig
    assert select(client, expected_previous={'source': 'STANDBY', 'source_account': '', 'location': '',
        'play_status': 'STOP_STATE'}).status_code == 200


@pytest.mark.parametrize('key,value', [('protected_device_ids', ID), ('protected_device_ips', '192.0.2.42')])
def test_protected_device_blocks_before_transport(rig, key, value):
    radio, client = rig
    with app_db.SessionLocal() as db:
        db.add(Setting(key=key, value=value))
        db.commit()
    assert select(client).status_code == 403
    assert radio.constructed == 0


@pytest.mark.parametrize('xml', ['', '<ContentItem/>', '<!DOCTYPE x><ContentItem/>', '<ContentItem>' + 'x' * 131073 + '</ContentItem>',
    '<wrong/>', '<ContentItem source="AIRPLAY" location="http://192.0.2.80/"/>'])
def test_invalid_instruction_cannot_construct_transport(rig, xml):
    radio, _ = rig
    with app_db.SessionLocal() as db:
        with pytest.raises(selection.SelectionError):
            asyncio.run(selection.select_direct(db, db.query(Device).one(), xml,
                client_factory=api.SoundTouchClient, guard=api.enforce_ip_write_guard))
    assert radio.constructed == 0


@pytest.mark.parametrize('xml', ['<nowPlaying source="STANDBY"/>',
    '<nowPlaying source="AUX"><ContentItem source="AIRPLAY"/><playStatus>PLAY_STATE</playStatus></nowPlaying>',
    '<nowPlaying source="AUX"><ContentItem/><ContentItem/><playStatus>PLAY_STATE</playStatus></nowPlaying>'])
def test_ambiguous_prewrite_observation_blocks(rig, xml):
    radio, client = rig
    radio.now = xml
    assert select(client).status_code == 409
    assert not radio.writes


@pytest.mark.parametrize('volume', ['<volume/>', '<volume><targetvolume>1</targetvolume><muteenabled>false</muteenabled></volume>',
    '<volume><actualvolume>101</actualvolume><muteenabled>false</muteenabled></volume>',
    '<volume><actualvolume>1</actualvolume><muteenabled>UNKNOWN</muteenabled></volume>'])
def test_target_volume_is_not_actual_volume(rig, volume):
    radio, client = rig
    radio.volume = volume
    assert select(client).status_code == 409
    assert not radio.writes


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://user:pass@192.0.2.80/', 'http://192.0.2.80:0/'])
def test_disallowed_media_uri_is_not_given_to_radio(rig, url):
    radio, client = rig
    assert select(client, stream_url=url).status_code == 409
    assert radio.constructed == 0


def test_protected_stream_and_artwork_destinations_block_before_transport(rig):
    radio, client = rig
    with app_db.SessionLocal() as db:
        db.add(Setting(key='protected_device_ips', value='192.0.2.80'))
        db.commit()
    assert select(client).status_code == 409
    assert select(client, stream_url='http://192.0.2.81/stream', image_url='http://192.0.2.80/cover').status_code == 409
    assert radio.constructed == 0
