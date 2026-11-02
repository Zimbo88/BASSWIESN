"""Native provider queries still need a protected, stable target; no real radio."""
import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, Setting
from basswiesn.app.routers import api
from test_settings_write_safety import Radio, DEVICE_ID, BUILD, info

pytestmark = pytest.mark.integration


@pytest.fixture
def search(monkeypatch):
    radio = Radio()
    radio.posts = []
    radio.reply = '<searchResults><stations/></searchResults>'
    radio.failure = False
    radio.after_post = None
    async def post(path, body):
        radio.posts.append((path, body))
        if radio.after_post:
            radio.after_post()
        if radio.failure:
            raise TimeoutError('private provider exception must not escape')
        return radio.reply
    radio.post_xml = post
    with app_db.SessionLocal() as db:
        db.add(Device(device_id=DEVICE_ID, ip_address='192.0.2.42', name='Example',
                      model='SoundTouch 30', firmware=BUILD, info_xml=info()))
        db.commit()
    monkeypatch.setattr(api, 'SoundTouchClient', radio.factory)
    with TestClient(create_web_app(background_tasks=False)) as client:
        yield radio, client


def call(client, **overrides):
    return client.post(f'/api/devices/{DEVICE_ID}/station/search-native', json={
        'source': 'TUNEIN', 'query': 'Jazz', 'dry_run': False, **overrides})


def test_protected_stable_identity_blocks_before_transport(search):
    radio, client = search
    with app_db.SessionLocal() as db:
        db.add(Setting(key='protected_device_ids', value=DEVICE_ID))
        db.commit()
    assert call(client).status_code == 403
    assert not radio.constructors and not radio.posts


def test_exact_profile_is_checked_before_query(search):
    radio, client = search
    radio.build = '27.0.6.UNKNOWN'
    assert call(client).status_code == 409
    assert not radio.posts


@pytest.mark.parametrize('value', ['false', 0, None])
def test_non_boolean_dry_run_never_constructs_transport(search, value):
    radio, client = search
    assert call(client, dry_run=value).status_code == 400
    assert not radio.constructors


def test_preview_is_passive_and_normal_query_is_identity_bound(search):
    radio, client = search
    assert call(client, dry_run=True).status_code == 200
    assert not radio.constructors
    response = call(client)
    assert response.status_code == 200, response.text
    assert radio.calls.count(('GET', '/info')) == 2
    assert len(radio.posts) == 1 and radio.posts[0][0] == '/searchStation'


def test_timeout_does_not_replay_or_echo_internal_error(search):
    radio, client = search
    radio.failure = True
    result = call(client)
    assert result.status_code == 502
    assert len(radio.posts) == 1
    assert 'private provider' not in result.text


def test_target_change_during_query_rejects_projection(search):
    radio, client = search
    def retarget():
        with app_db.SessionLocal() as db:
            db.query(Device).filter_by(device_id=DEVICE_ID).one().ip_address = '192.0.2.99'
            db.commit()
    radio.after_post = retarget
    assert call(client).status_code == 409
    assert len(radio.posts) == 1


@pytest.mark.parametrize('reply', ['not XML', '<!DOCTYPE r><r/>',
    '<searchResults deviceID="OTHER"/>'])
def test_untrusted_response_is_not_a_valid_empty_search(search, reply):
    radio, client = search
    radio.reply = reply
    assert call(client).status_code in {409, 502}
    assert len(radio.posts) == 1


@pytest.mark.parametrize('query', ['bad\x00query', 'bad\ud800query'])
def test_invalid_xml_input_never_constructs_transport(search, query):
    import json
    radio, client = search
    response = client.post(f'/api/devices/{DEVICE_ID}/station/search-native',
        content=json.dumps({'query': query, 'dry_run': False}), headers={'Content-Type': 'application/json'})
    assert response.status_code == 400
    assert not radio.constructors


@pytest.mark.parametrize('reply', ['', '<x>' + 'x' * 1048576 + '</x>'])
def test_empty_or_excessive_response_is_bounded(search, reply):
    radio, client = search
    radio.reply = reply
    assert call(client).status_code == 502


def test_unavailable_initial_identity_prevents_query(search):
    radio, client = search
    async def failed(path):
        raise TimeoutError('private identity error')
    radio.get_xml = failed
    result = call(client)
    assert result.status_code == 502 and 'private identity error' not in result.text
    assert not radio.posts


def test_post_query_identity_change_is_not_a_trusted_result(search):
    radio, client = search
    radio.after_post = lambda: setattr(radio, 'device_id', 'OTHER')
    result = call(client)
    assert result.status_code == 409 and len(radio.posts) == 1
