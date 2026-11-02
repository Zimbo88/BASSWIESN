"""Owned receiver lifecycle, pure state and loopback only; no radio actions."""
import asyncio
import base64
import shutil

import pytest

from basswiesn.app.services.airplay_bridge.http_relay import PCMHTTPRelay
from basswiesn.app.services.airplay_bridge.lifecycle import PilotPlayback, PlaybackCursor, ends_stream
from basswiesn.app.services.airplay_bridge.metadata_wire import MetadataWireDecoder

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('code', ['paus', 'pres', 'pfls', 'prsm', 'pbeg', 'pend'])
def test_lifecycle_payload_is_discarded_at_wire_boundary(code):
    private = b'not-for-state-or-diagnostics'
    wire = (f'<item><type>73736e63</type><code>{code.encode().hex()}</code>'
            f'<length>{len(private)}</length><data encoding="base64">'
            f'{base64.b64encode(private).decode()}</data></item>').encode()
    decoder = MetadataWireDecoder()
    assert decoder.feed(wire) == [('ssnc', code, b'')]
    assert not decoder._buffer


def test_pause_before_player_begin_is_not_overridden_by_player_creation():
    playback = PilotPlayback()
    assert not playback.apply('ssnc', 'abeg')
    assert playback.state == 'WAITING'
    assert playback.apply('ssnc', 'paus')
    assert playback.apply('ssnc', 'pbeg')
    assert playback.state == 'PAUSED'
    assert playback.apply('ssnc', 'pres')
    assert playback.state == 'PLAYING'


@pytest.mark.parametrize('pause,resume', [('paus', 'pres'), ('pfls', 'prsm')])
def test_collapsed_pause_resume_retains_invalidation_epoch(pause, resume):
    playback = PilotPlayback()
    cursor = PlaybackCursor()
    assert cursor.accept(playback.snapshot())
    playback.apply('ssnc', 'pbeg')
    assert cursor.accept(playback.snapshot())
    playback.apply('ssnc', pause)
    playback.apply('ssnc', resume)
    assert playback.state == 'PLAYING'
    assert playback.buffer_epoch == 1
    assert cursor.accept(playback.snapshot())
    assert not cursor.accept(playback.snapshot())


def test_ended_lifetime_cannot_be_rearmed_by_late_metadata():
    playback = PilotPlayback()
    playback.apply('ssnc', 'pbeg')
    playback.apply('ssnc', 'pend')
    before = playback.snapshot()
    for code in ('pbeg', 'pres', 'prsm', 'paus', 'abeg', 'mdst', 'pvol'):
        assert not playback.apply('ssnc', code)
    assert playback.snapshot() == before


@pytest.mark.parametrize('state', ['WAITING', 'PLAYING', 'PAUSED', 'ENDED'])
def test_activity_timeout_is_not_teardown_or_a_new_playback_command(state):
    playback = PilotPlayback(state=state)
    before = playback.snapshot()
    for event in ('aend', 'abeg'):
        assert not ends_stream('ssnc', event)
        assert not playback.apply('ssnc', event)
        assert playback.snapshot() == before
    assert ends_stream('ssnc', 'pend')
    assert not ends_stream('core', 'pend')


def test_real_pause_activity_expiry_resume_sequence_keeps_stream_lifetime():
    playback = PilotPlayback()
    cursor = PlaybackCursor()
    for code in ('pbeg', 'pres', 'prsm', 'paus'):
        playback.apply('ssnc', code)
    assert playback.state == 'PAUSED'
    assert cursor.accept(playback.snapshot())
    paused = playback.snapshot()
    for code in ('aend', 'abeg'):
        assert not playback.apply('ssnc', code)
        assert playback.snapshot() == paused
    assert playback.apply('ssnc', 'pres')
    assert playback.state == 'PLAYING' and playback.buffer_epoch == 1
    assert cursor.accept(playback.snapshot())
    assert playback.apply('ssnc', 'pend')
    assert playback.state == 'ENDED'


@pytest.mark.parametrize('value', [
    {}, {'state': 'PLAYING', 'sequence': 1, 'buffer_epoch': 0, 'secret': 'fixture'},
    {'state': 'anything', 'sequence': 1, 'buffer_epoch': 0},
    {'state': 'PLAYING', 'sequence': True, 'buffer_epoch': 0},
    {'state': 'PLAYING', 'sequence': -1, 'buffer_epoch': 0},
    {'state': 'PLAYING', 'sequence': 1, 'buffer_epoch': 2},
    {'state': 'PLAYING', 'sequence': 1, 'buffer_epoch': False},
])
def test_invalid_private_observation_refused(value):
    with pytest.raises(ValueError):
        PlaybackCursor().accept(value)


def test_cursor_rejects_replay_mutation_epoch_rollback_and_terminal_revival():
    cursor = PlaybackCursor()
    assert cursor.accept({'state': 'PLAYING', 'sequence': 3, 'buffer_epoch': 1})
    for value in (
        {'state': 'PAUSED', 'sequence': 3, 'buffer_epoch': 1},
        {'state': 'PLAYING', 'sequence': 2, 'buffer_epoch': 1},
        {'state': 'PLAYING', 'sequence': 4, 'buffer_epoch': 0},
    ):
        with pytest.raises(ValueError):
            cursor.accept(value)
    assert cursor.accept({'state': 'ENDED', 'sequence': 4, 'buffer_epoch': 2})
    with pytest.raises(ValueError):
        cursor.accept({'state': 'PLAYING', 'sequence': 5, 'buffer_epoch': 2})


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires offline FFmpeg')
def test_real_relay_pause_flush_resume_never_releases_safety_gate_or_replays_pcm():
    async def check():
        relay = PCMHTTPRelay(bind='127.0.0.1', target='127.0.0.1', token='a' * 64, port=0)
        relay.update_playback('WAITING', 0)
        await relay.start()
        writer = None
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', relay.port)
            writer.write(b'GET /relay/' + b'a' * 64 + b'.mp3 HTTP/1.0\r\n\r\n')
            await writer.drain()
            await reader.readuntil(b'\r\n\r\n')
            assert not relay.release_audio()
            relay.update_playback('PLAYING', 0)
            assert not relay.snapshot()['audio_released']
            assert relay.release_audio()
            relay.feed(b'\x01' * 7680)
            assert relay._runtime.pcm.buffered_bytes == 7680
            relay.update_playback('PAUSED', 1)
            assert relay._runtime.pcm.buffered_bytes == 0
            forwarded = relay.forwarded_pcm_bytes
            relay.feed(b'\x02' * 7680)
            assert relay.forwarded_pcm_bytes == forwarded
            assert not relay.release_audio()
            relay.update_playback('PLAYING', 1)
            relay.feed(b'\x03' * 7680)
            # The consumer skipped a complete pause/resume, but must discard
            # queued input because the producer's epoch records that boundary.
            relay.update_playback('PLAYING', 2)
            assert relay._runtime.pcm.buffered_bytes == 0
            relay.update_playback('ENDED', 3)
            assert not relay.update_playback('PLAYING', 3)
            assert not relay.release_audio()
            await asyncio.wait_for(reader.read(), timeout=5)
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            await relay.close()
    asyncio.run(check())
