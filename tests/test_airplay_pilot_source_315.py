"""Pilot source/restore uses actual safety services with a simulated radio."""
import asyncio

import pytest

from basswiesn.app.models import ConfigBackup, Setting
from basswiesn.app.routers.shared import enforce_ip_write_guard
from basswiesn.app.services.airplay_bridge.pilot_source import pilot_source_scope
from basswiesn.app.services.zone_changes import ZoneChangeError, ZonePorts
from test_station_playback_safety import rig, LOCATION
from test_maintenance_reboot_safety import ID, now, volume

pytestmark = pytest.mark.integration
XML = f'<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="{LOCATION}"><itemName>AP2 Test</itemName></ContentItem>'


def scope(rig, **kwargs):
    rig.station.internal, rig.station.lab_only, rig.station.purpose = True, True, 'airplay_bridge'
    rig.db.commit()
    return pilot_source_scope(rig.db, [rig.device], master=ID, station=rig.station, xml=XML,
        ports=ZonePorts(client=rig.factory, guard=enforce_ip_write_guard), guard_audio=lambda: None, **kwargs)


def test_one_select_then_verified_standby_before_restoring_comfortable_volume(rig):
    before = dict(rig.raw)
    async def run():
        async with scope(rig) as owner:
            assert owner.before[ID].selection.source == 'STANDBY'
            assert owner.before[ID].volume == (15, False)
            assert (await owner.read(rig.device)).volume == (1, False)
            assert (await owner.read(rig.device)).selection.same_content(owner.selection)
            assert rig.db.query(ConfigBackup).filter_by(path=owner.job+'/before.json').count() == 1
        assert owner.restore_result == 'CAPTURED_STATE_RESTORED'
    asyncio.run(run())
    assert len([path for path, _ in rig.posts if path == '/select']) == 1
    assert rig.posts.index(('/standby','')) < rig.posts.index(('/volume','<volume>15</volume>'))
    assert rig.raw['/volume'] == before['/volume'] and rig.raw['/presets'] == before['/presets']


@pytest.mark.parametrize('failure', ['playing', 'muted', 'foreign_zone', 'protected', 'profile'])
def test_untrusted_pilot_start_never_writes(rig, failure):
    if failure == 'playing': rig.raw['/now_playing'] = now('AUX','PLAY_STATE')
    if failure == 'muted': rig.raw['/volume'] = volume(15,'true')
    if failure == 'foreign_zone': rig.raw['/getZone'] = '<zone master="OTHER" />'
    if failure == 'profile': rig.device.firmware = 'UNVERIFIED'
    if failure == 'protected':
        rig.db.add(Setting(key='protected_device_ids', value=ID))
    async def run():
        with pytest.raises(Exception):
            async with scope(rig):
                pytest.fail('untrusted pilot accepted')
    asyncio.run(run())
    assert not rig.posts


@pytest.mark.parametrize('change', ['source', 'volume', 'preset', 'zone', 'own_address', 'backup'])
def test_cleanup_preserves_newer_user_state_and_reports_reconciliation(rig, change):
    async def run():
        owner = None
        with pytest.raises((ValueError, ZoneChangeError)):
            async with scope(rig) as owner:
                if change == 'source': rig.raw['/now_playing'] = now('AUX','PLAY_STATE')
                if change == 'volume': rig.raw['/volume'] = volume(22)
                if change == 'preset': rig.raw['/presets'] = '<presets><preset id="1" /></presets>'
                if change == 'zone': rig.raw['/getZone'] = '<zone master="OTHER" />'
                if change == 'own_address':
                    rig.raw['/getZone'] = f'<zone master="{ID}"><member ipaddress="192.0.2.99">{ID}</member></zone>'
                if change == 'backup':
                    rig.db.get(ConfigBackup, owner.backup_id).content = '{}'
                    rig.db.commit()
                posts = list(rig.posts)
        assert owner.restore_result == 'MANUAL_REQUIRED' and rig.posts == posts
    asyncio.run(run())


def test_lost_select_response_keeps_existing_fail_closed_policy_and_never_replays(rig):
    import httpx
    original = rig.post_xml
    async def post(path, body):
        reply = await original(path, body)
        if path == '/select':
            raise httpx.ReadTimeout('simulated response loss after applied select')
        return reply
    rig.post_xml = post
    async def run():
        with pytest.raises(ValueError, match='RECONCILIATION'):
            async with scope(rig):
                pytest.fail('uncertain station transaction must not be promoted to success')
    asyncio.run(run())
    assert sum(path == '/select' for path, _ in rig.posts) == 1
    assert '/now_playing' in rig.reads
    assert ('/volume', '<volume>15</volume>') not in rig.posts


def test_pilot_refuses_unacknowledged_audio_gate_before_first_write(rig):
    def no_hold():
        raise ValueError('audio hold has not been acknowledged')
    async def run():
        manager = scope(rig)
        # The public context factory passes the bound callback into its owner;
        # use the same constructor here to exercise preparation under locks.
        from basswiesn.app.services.airplay_bridge.pilot_source import PilotSource
        from basswiesn.app.services.device_transactions import device_transaction
        owner = PilotSource(rig.db, [rig.device], ID, rig.station, XML,
            ZonePorts(client=rig.factory, guard=enforce_ip_write_guard), no_hold, 1)
        async with device_transaction([ID]):
            with pytest.raises(ValueError, match='acknowledged'):
                await owner.prepare()
        del manager
    asyncio.run(run())
    assert not rig.posts
