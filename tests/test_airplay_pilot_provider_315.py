"""Real restricted BMX contracts against an isolated DB, never a LAN peer."""
import asyncio
from urllib.parse import urlsplit

import httpx
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import Device, Station
from basswiesn.app.services.airplay_bridge.display_session import DisplayPublisher
from basswiesn.app.services.airplay_bridge.pilot_provider import PilotProvider
from basswiesn.app.services.device_transactions import device_transaction
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.clock_metadata import save_clock_metadata_preference

pytestmark = pytest.mark.integration
ID, IP, BASE = 'AP2-EXAMPLE', '192.0.2.42', 'http://192.0.2.80:1517'


@pytest.fixture
def provider():
    with app_db.SessionLocal() as db:
        station = Station(name='AP2 Test', stream_url='http://192.0.2.88:8098/relay/'+'a'*64+'.mp3',
            stream_format='mp3', stream_mime='audio/mpeg', internal=True, lab_only=True, purpose='airplay_bridge')
        db.add_all([station, Device(device_id=ID, ip_address=IP, name='Example')])
        db.commit()
        save_clock_metadata_preference(db, ID, enabled=False, mode='APPEND')
        return PilotProvider(app_db.SessionLocal, station=station, peers={IP:ID}, base_url=BASE, guard=lambda:None)


def client(provider, peer=IP):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=provider, client=(peer, 5678)), base_url=BASE)


def test_only_owned_contract_no_relay_probe_and_live_bmx_metadata(provider, monkeypatch):
    from basswiesn.app.services import station_metadata
    async def no_probe(*args, **kwargs):
        pytest.fail('one-use relay must never be probed')
    monkeypatch.setattr(station_metadata, 'probe_station_metadata', no_probe)
    async def run():
        async with device_transaction([ID]):
            with app_db.SessionLocal() as db:
                publisher = DisplayPublisher.begin(db, ID, provider.station_id, device_lock(ID))
                publisher.publish(db, {'track':'Example song', 'artist':'Example artist'})
            async with client(provider) as http:
                station = await http.get(provider.location)
                assert station.status_code == 200
                assert station.json()['audio']['streams'][0]['streamUrl'].endswith('a'*64+'.mp3')
                assert station.json()['_links']['bmx_reporting']['href'] == BASE+provider.report_path
                playing = await http.get(provider.now_path)
                report = await http.post(provider.report_path, json={'eventType':'TIMED'})
                assert playing.status_code == report.status_code == 200
                body = report.json()['_embedded']['bmx_nowplaying']
                assert body['track'] == 'Example song' and body['artist'] == 'Example artist'
                assert playing.json() == body
            with app_db.SessionLocal() as db:
                publisher.end(db)
    asyncio.run(run())


@pytest.mark.parametrize('case', ['peer', 'host', 'identity', 'admin', 'station', 'descriptor', 'duplicate', 'owner', 'changed'])
def test_peer_identity_exact_station_and_owner_cannot_be_bypassed(provider, case):
    async def run():
        path, headers = provider.location, {}
        if case == 'host': headers['host'] = 'example.invalid'
        if case == 'identity': headers['x-device-id'] = 'ANOTHER-RADIO'
        if case == 'admin': path = '/api/devices'
        if case == 'station': path = '/bmx/orion/now-playing/station/999'
        if case == 'descriptor': path = urlsplit(path).path+'?data=e30='
        if case == 'duplicate': path += '&data=e30='
        if case == 'owner':
            def expired(): raise ValueError('expired')
            provider.guard = expired
        if case == 'changed':
            with app_db.SessionLocal() as db:
                db.get(Station, provider.station_id).name = 'Changed'
                db.commit()
        async with client(provider, '192.0.2.99' if case == 'peer' else IP) as http:
            reply = await http.get(path, headers=headers)
            assert reply.status_code in {403,503}
            assert 'streamUrl' not in reply.text
    asyncio.run(run())


def test_reporting_body_budget_and_invalid_json(provider):
    async def run():
        async with client(provider) as http:
            assert (await http.post(provider.report_path, content=b'x'*(1024*1024+1))).status_code == 413
            assert (await http.post(provider.report_path, content=b'not json')).status_code == 400
    asyncio.run(run())
