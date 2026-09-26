"""Real sourceItem envelopes must not silently disappear from provider evidence."""
import asyncio

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import Device, Preset, Station
from basswiesn.app.routers import stations_presets
from basswiesn.app.services.device_state import parse_sources_xml, save_runtime_state

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('tag', ['sourceItem', 'source'])
def test_observed_source_envelopes(tag):
    rows, providers = parse_sources_xml(
        f'<sources><{tag} source="LOCAL_INTERNET_RADIO" sourceAccount="" status="READY" />'
        f'<{tag} source="AIRPLAY" status="UNAVAILABLE" /></sources>', '2026-01-01T00:00:00Z')
    assert len(rows) == 2
    assert providers['LOCAL_INTERNET_RADIO']['available'] is True
    assert providers['LOCAL_INTERNET_RADIO']['source_observed'] is True
    assert providers['AIRPLAY']['available'] is False
    assert providers['AIRPLAY']['source_observed'] is True


@pytest.mark.parametrize('observation,expected', [('READY', 'VALID'), ('UNAVAILABLE', 'BROKEN'), ('read_failed', 'UNKNOWN')])
def test_explicit_checker_uses_current_evidence_not_stale_provider_cache(monkeypatch, observation, expected):
    location = 'https://example.invalid/station.mp3'
    item = f'<ContentItem source="LOCAL_INTERNET_RADIO" sourceAccount="" type="stationurl" location="{location}"><itemName>Example</itemName></ContentItem>'
    calls = []

    class Client:
        async def get_xml(self, path):
            calls.append(path)
            if path == '/presets':
                return f'<presets><preset id="1">{item}</preset></presets>'
            if path == '/sources' and observation != 'read_failed':
                return f'<sources><sourceItem source="LOCAL_INTERNET_RADIO" status="{observation}" /></sources>'
            raise RuntimeError('test-only route unavailable')

    async def stream_probe(_url):
        return {'status': 'VALID', 'reachable': True, 'reason': 'HTTP response accepted'}

    monkeypatch.setattr(stations_presets, '_soundtouch_client_for', lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(stations_presets, 'probe_stream_reachability', stream_probe)
    with app_db.SessionLocal() as db:
        device = Device(device_id='PRESET-300', ip_address='192.0.2.42')
        station = Station(name='Example', stream_url=location, stream_url_resolved=location)
        db.add_all([device, station])
        db.flush()
        db.add(Preset(device_id=device.device_id, button=1, station_id=station.id,
                      source='LOCAL_INTERNET_RADIO', source_account='', location=location,
                      content_item_xml=item))
        save_runtime_state(db, device.device_id, {'providers': {
            'LOCAL_INTERNET_RADIO': {'available': True, 'source_observed': True, 'source_available': True}
        }})
        db.commit()
        result = asyncio.run(stations_presets.preset_status(device.device_id, probe=True, db=db))
    slot = result['slots'][0]
    provider = next(check for check in slot['checks'] if check['id'] == 'provider_availability')
    physical = next(check for check in slot['checks'] if check['id'] == 'hardware_button_playability')
    assert provider['status'] == expected
    assert slot['verdict'] == expected
    assert physical['status'] == 'UNKNOWN' and physical['affects_verdict'] is False
    assert calls == ['/presets', '/sources', '/serviceAvailability']
