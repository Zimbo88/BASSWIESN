"""Incomplete and changing state is not permission to unmute or replay a key."""
from xml.etree import ElementTree as ET

import pytest

from basswiesn.app.services.playback_safety_gate import load_playback_safety_gate
from test_key_action_safety import ID, ITEM, keys, now, run, volume  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('recovers', [True, False])
def test_missing_state_after_initial_volume_is_bounded_read_only(keys, recovers):
    keys.raw['/volume'] = volume(2)
    pending, reads = False, 0

    def changed(path):
        nonlocal pending
        if path == '/volume' and not pending:
            pending = True
            keys.raw['/now_playing'] = f'<nowPlaying source="LOCAL_INTERNET_RADIO">{ITEM}</nowPlaying>'

    def observed(path):
        nonlocal reads
        if pending and path == '/now_playing':
            reads += 1
            if recovers and reads == 2:
                keys.raw[path] = now('LOCAL_INTERNET_RADIO', 'PLAY_STATE')

    keys.after_write, keys.after_read = changed, observed
    result = run(keys, 'PRESET_1', safe_volume=1)
    assert result.get('verified', False) is recovers
    assert sum(path == '/volume' for path, _ in keys.writes) == 1
    presses = [body for path, body in keys.writes if path == '/key' and 'state="press"' in body]
    assert sum('>PRESET_1<' in body for body in presses) == int(recovers)
    if not recovers:
        assert not presses and reads <= 22


@pytest.mark.parametrize('fault', ['volume', 'source', 'stopped', 'stalled'])
def test_changed_state_while_unmuting_aborts_without_replay(keys, fault):
    changed = False

    def observed(path):
        nonlocal changed
        if path != '/now_playing' or changed or load_playback_safety_gate(keys.db, ID).get('state') != 'VERIFIED':
            return
        changed = True
        if fault == 'volume':
            keys.raw['/volume'] = volume(2, 'true')
        elif fault == 'source':
            keys.raw[path] = now('LOCAL_INTERNET_RADIO', 'PLAY_STATE', 'http://example.invalid/other.mp3')
        else:
            keys.raw[path] = now('LOCAL_INTERNET_RADIO', 'STOP_STATE' if fault == 'stopped' else 'BUFFERING_STATE')

    keys.after_read = observed
    result = run(keys, 'PRESET_1', safe_volume=1)
    assert changed and not result.get('verified', False)
    assert ET.fromstring(keys.raw['/volume']).findtext('muteenabled') == 'true'
    presses = [ET.fromstring(body).text for path, body in keys.writes if path == '/key' and 'state="press"' in body]
    assert presses.count('PRESET_1') == 1
    assert presses.count('MUTE') == 1  # The initial protective mute, never a retry/unmute.


def test_acknowledged_preset_that_never_leaves_standby_keeps_provider_blocked(keys):
    standby = '<nowPlaying source="STANDBY"><ContentItem source="STANDBY"/></nowPlaying>'
    keys.raw['/now_playing'] = standby

    def never_activated(path):
        if path == '/key' and '>PRESET_1<' in keys.writes[-1][1]:
            keys.raw['/now_playing'] = standby

    keys.after_write = never_activated
    result = run(keys, 'PRESET_1', safe_volume=1)
    assert not result.get('verified', False)
    assert load_playback_safety_gate(keys.db, ID).get('state') != 'VERIFIED'
    assert ET.fromstring(keys.raw['/volume']).findtext('muteenabled') == 'true'
    assert sum('>PRESET_1<' in body and 'state="press"' in body for _, body in keys.writes) == 1
