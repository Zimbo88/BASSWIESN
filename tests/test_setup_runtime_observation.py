"""A failed volatile playback observation is not a failed routing backup.

These tests exercise the real backup adapter with offline HTTP fixtures; they
do not equate setup completion with audio validation.
"""
import asyncio
from hashlib import sha256
import json
from types import SimpleNamespace

import httpx
import pytest

from basswiesn.app.services.setup_rebuild import radio_adapter as module

IDENTITY = 'AA11BB22CC33'


def http_error(code, path='/now_playing'):
    request = httpx.Request('GET', 'http://192.0.2.42:8090' + path)
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError('remote status', request=request, response=response)


@pytest.fixture
def capture(monkeypatch, tmp_path):
    calls = []
    failures = {}
    row = SimpleNamespace(device_id=IDENTITY, ip_address='192.0.2.42',
                          expected_model='SoundTouch 30', evidence_json='{}')
    class Client:
        async def get_xml(self, path):
            calls.append(path)
            if path in failures:
                value = failures[path]
                if isinstance(value, Exception): raise value
                return value
            tag = {'/info':'info', '/now_playing':'nowPlaying'}.get(path, path[1:])
            return f'<{tag} deviceID="{IDENTITY}" source="SETUP"/>'

        async def post_xml(self, *args):
            pytest.fail('A baseline capture must not write to a radio')
    adapter = module.RadioSetupAdapter()
    monkeypatch.setattr(adapter, '_client', lambda _: Client())
    monkeypatch.setattr(module, '_baseline_for', lambda *args: tmp_path/'baseline')
    return adapter, row, calls, failures, tmp_path/'baseline'


def test_now_playing_500_retains_required_backup_and_explicit_unknown(capture):
    adapter, row, calls, failures, root = capture
    failures['/now_playing'] = http_error(500)
    result = asyncio.run(adapter.backup(row))
    assert result['verified'] is True
    assert result['rollback_scope'] == 'ROUTING_ONLY'
    observation = result['runtime_observations']['/now_playing']
    assert observation == {'captured': False, 'status': 'UNKNOWN', 'http_status': 500,
                           'error_type': 'HTTPStatusError', 'restorable': False}
    assert calls.count('/now_playing') == 1  # No blind retries or manufactured XML.
    assert not (root/'now_playing.xml').exists()
    for name in ('info.xml', 'networkInfo.xml', 'sources.xml', 'presets.xml', 'volume.xml'):
        assert result['sha256'][name] == sha256((root/name).read_bytes()).hexdigest()
    manifest = json.loads((root/'backup-manifest.json').read_text())
    assert manifest['runtime_observations'] == result['runtime_observations']
    metadata = json.loads((root/'baseline-metadata.json').read_text())['metadata']
    assert '/now_playing' not in metadata['http_endpoints']
    assert metadata['runtime_observations'] == result['runtime_observations']


def test_valid_setup_runtime_xml_is_captured_not_invented_as_standby(capture):
    adapter, row, _, _, root = capture
    result = asyncio.run(adapter.backup(row))
    observation = result['runtime_observations']['/now_playing']
    assert observation['captured'] and observation['restorable'] is False
    xml = (root/'now_playing.xml').read_text()
    assert 'source="SETUP"' in xml and 'STANDBY' not in xml and 'PLAY_STATE' not in xml
    assert observation['sha256'] == sha256(xml.encode()).hexdigest()


@pytest.mark.parametrize('code', [404, 500, 502, 503, 504])
def test_unavailable_runtime_is_explicitly_recorded(capture, code):
    adapter, row, _, failures, _ = capture
    failures['/now_playing'] = http_error(code)
    result = asyncio.run(adapter.backup(row))
    assert result['runtime_observations']['/now_playing']['http_status'] == code


@pytest.mark.parametrize('error', [http_error(401), http_error(403), http_error(302),
                                  httpx.ConnectTimeout(''), httpx.ReadTimeout(''),
                                  PermissionError('protected'), RuntimeError('identity mismatch')])
def test_transport_auth_policy_and_unexpected_failures_still_abort(capture, error):
    adapter, row, _, failures, root = capture
    failures['/now_playing'] = error
    with pytest.raises(type(error)):
        asyncio.run(adapter.backup(row))
    assert not (root/'backup-manifest.json').exists()


@pytest.mark.parametrize('path', ['/info', '/networkInfo', '/sources', '/presets', '/volume'])
def test_required_backup_failure_still_aborts_before_any_write(capture, path):
    adapter, row, _, failures, root = capture
    failures[path] = http_error(500, path)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(adapter.backup(row))
    assert not (root/'backup-manifest.json').exists()


@pytest.mark.parametrize('xml', ['<nowPlaying', '<error/>',
                                '<nowPlaying deviceID="DD44EE55FF66"/>'])
def test_bad_runtime_xml_is_not_marked_as_captured(capture, xml):
    adapter, row, _, failures, root = capture
    failures['/now_playing'] = xml
    with pytest.raises((ValueError, RuntimeError, module.ET.ParseError)):
        asyncio.run(adapter.backup(row))
    assert not (root/'backup-manifest.json').exists()


def test_runtime_unavailable_can_complete_routing_without_claiming_audio(capture):
    from test_setup_rebuild_160 import _session, _device, RecordingAdapter
    from basswiesn.app.services.setup_rebuild.coordinator import SetupCoordinator
    from basswiesn.app.services.setup_rebuild.server_target import ServerTarget
    real, _, _, failures, _ = capture
    failures['/now_playing'] = http_error(500)
    class Adapter(RecordingAdapter):
        async def backup(self, row):
            self.calls.append('backup')
            return await real.backup(row)
    adapter = Adapter()
    session = _session()
    coordinator = SetupCoordinator(adapter=adapter, session_factory=session)
    with session() as db:
        db.add(_device(device_id=IDENTITY))
        db.commit()
        job = coordinator.start(db, device_ids=[IDENTITY],
                                target=ServerTarget('192.0.2.10', 1328, 1516, 1860),
                                options={'ssh_required': False, 'pair_account': True,
                                         'playback_test': False})
    result = asyncio.run(coordinator.execute(job['job_id']))
    assert result['status'] == 'completed'
    evidence = result['devices'][0]['evidence']
    assert evidence['runtime_observations']['/now_playing']['status'] == 'UNKNOWN'
    assert evidence['playback_test'] == 'not_requested'
    assert 'playback_test' not in adapter.calls
    assert adapter.calls.index('backup') < adapter.calls.index('route')


def test_empty_transport_error_remains_visible_and_never_retries_writes():
    from test_setup_rebuild_160 import _session, _device, RecordingAdapter
    from basswiesn.app.services.setup_rebuild.coordinator import SetupCoordinator
    from basswiesn.app.services.setup_rebuild.server_target import ServerTarget
    class Adapter(RecordingAdapter):
        async def identify(self, row):
            self.calls.append('identify')
            raise httpx.ConnectTimeout('')
    adapter = Adapter()
    session = _session()
    coordinator = SetupCoordinator(adapter=adapter, session_factory=session)
    with session() as db:
        db.add(_device(device_id=IDENTITY))
        db.commit()
        job = coordinator.start(db, device_ids=[IDENTITY],
                                target=ServerTarget('192.0.2.10', 1328, 1516, 1860))
    result = asyncio.run(coordinator.execute(job['job_id']))
    assert result['status'] == 'failed'
    assert result['devices'][0]['last_error'] == 'ConnectTimeout'
    assert 'ConnectTimeout' in result['error']
    assert adapter.calls == ['identify']
