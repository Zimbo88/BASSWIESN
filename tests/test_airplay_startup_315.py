"""Simulated radio readback and real loopback encoder; no hardware validation."""
import asyncio
from dataclasses import replace
import math
import shutil
import struct
import subprocess

import pytest

from basswiesn.app.services.airplay_bridge.http_relay import PCMHTTPRelay
from basswiesn.app.services.airplay_bridge.startup import (
    StartupDecision, StartupObservation, StartupReadbackGate,
)

pytestmark = pytest.mark.unit
BUFFERING = StartupObservation(True, True, True, 1, "BUFFERING_STATE", "STREAMING", None, 100000)


def test_pending_selection_extends_reads_without_claiming_success():
    gate = StartupReadbackGate(started=100)
    for moment in (100, 105, 110):
        assert gate.observe(BUFFERING, now=moment) == StartupDecision.WAIT
    assert gate.observe(replace(BUFFERING, play_status="PLAY_STATE"), now=117) == StartupDecision.READY


@pytest.mark.parametrize("change,reason", [
    ({"identity_verified": False}, "TARGET_OR_SOURCE_CHANGED"),
    ({"identity_verified": 1}, "TARGET_OR_SOURCE_CHANGED"),
    ({"owned_source": False}, "TARGET_OR_SOURCE_CHANGED"),
    ({"zone_clear": False}, "TARGET_OR_SOURCE_CHANGED"),
    ({"volume": None}, "SAFE_VOLUME_NOT_CONFIRMED"),
    ({"volume": True}, "SAFE_VOLUME_NOT_CONFIRMED"),
    ({"volume": 2}, "SAFE_VOLUME_NOT_CONFIRMED"),
    ({"volume": -1}, "SAFE_VOLUME_NOT_CONFIRMED"),
    ({"relay_failure": "ENCODER_IO"}, "RELAY_NOT_STREAMING"),
    ({"relay_state": "ENDED"}, "RELAY_NOT_STREAMING"),
    ({"sent_bytes": True}, "RELAY_NOT_STREAMING"),
    ({"sent_bytes": -1}, "RELAY_NOT_STREAMING"),
    ({"play_status": "INVALID_SOURCE"}, "RADIO_STATE_NOT_PLAYABLE"),
    ({"play_status": ""}, "RADIO_STATE_NOT_PLAYABLE"),
])
def test_failure_is_terminal_never_permission_to_retry(change, reason):
    gate = StartupReadbackGate(started=0)
    assert gate.observe(replace(BUFFERING, **change), now=1) == StartupDecision.ABORT
    assert gate.reason == reason
    assert gate.observe(replace(BUFFERING, play_status="PLAY_STATE"), now=2) == StartupDecision.ABORT


@pytest.mark.parametrize("now", [-1, float("nan"), float("inf"), True])
def test_invalid_clock_aborts(now):
    gate = StartupReadbackGate(started=0)
    assert gate.observe(BUFFERING, now=now) == StartupDecision.ABORT


def test_deadline_and_incomplete_transport_are_not_success():
    gate = StartupReadbackGate(started=0)
    assert gate.observe(replace(BUFFERING, play_status="PLAY_STATE", sent_bytes=0), now=1) == StartupDecision.WAIT
    assert gate.observe(replace(BUFFERING, play_status="PLAY_STATE"), now=18) == StartupDecision.ABORT


@pytest.mark.parametrize("args", [{"started": True}, {"started": float("inf")},
                                  {"started": 0, "timeout": 31}, {"started": 0, "timeout": 0},
                                  {"started": 0, "maximum_volume": True}])
def test_invalid_startup_configuration(args):
    with pytest.raises(ValueError):
        StartupReadbackGate(**args)


def tone(frame, frequency):
    return b"".join(struct.pack("<ii", sample, sample) for sample in (
        int(0.1 * (2**31 - 1) * math.sin(2 * math.pi * frequency * (frame * 960 + n) / 48000))
        for n in range(960)))


def frequency_power(samples, frequency):
    """Sum window energies, not phase vectors across disjoint audio segments.

    The clocked relay can insert silence between asynchronously fed frames.
    A single whole-record DFT can cancel an actually present tone across those
    gaps. 50 ms Hann windows retain the original 100x rejection threshold while
    measuring local spectral energy independently of inter-segment phase.
    """
    size = 2400
    weights = [.5 - .5 * math.cos(2 * math.pi * i / (size - 1)) for i in range(size)]
    cosines = [w * math.cos(2 * math.pi * frequency * i / 48000) for i, w in enumerate(weights)]
    sines = [w * math.sin(2 * math.pi * frequency * i / 48000) for i, w in enumerate(weights)]
    total = 0.0
    for start in range(0, len(samples) - size + 1, size // 2):
        window = samples[start:start + size]
        real = sum(x * c for x, c in zip(window, cosines))
        imag = sum(x * s for x, s in zip(window, sines))
        total += real * real + imag * imag
    return total


def test_spectral_measurement_detects_tone_despite_opposite_segment_phase():
    samples = [3000 * math.sin(2 * math.pi * 880 * i / 48000) for i in range(24000)]
    opposite = [-x for x in samples]
    recording = samples + [0] * 24000 + opposite
    assert frequency_power(recording, 880) > 100 * frequency_power(recording, 220)
    forbidden = [3000 * math.sin(2 * math.pi * 220 * i / 48000) for i in range(24000)]
    assert frequency_power(forbidden, 220) > 100 * frequency_power(forbidden, 880)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="offline FFmpeg required")
def test_simulated_buffering_then_real_pcm_mp3_decode_and_pause_resume():
    """One HTTP selection, initially silence, then distinguishable PCM segments.

    The simulated buffer duration is a scenario, not an estimate of Bose DSP
    latency. Actual encoding/decoding runs; nothing is played on a sound device.
    """
    async def run():
        relay = PCMHTTPRelay(bind="127.0.0.1", target="127.0.0.1", token="b" * 64, port=0)
        relay.update_playback("PLAYING", 0)
        await relay.start()
        writer = None
        drain = None
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
            writer.write(b"GET /relay/" + b"b" * 64 + b".mp3 HTTP/1.0\r\n\r\n")
            await writer.drain()
            assert b"200 OK" in await reader.readuntil(b"\r\n\r\n")
            chunks = []

            async def consume():
                while chunk := await reader.read(8192):
                    chunks.append(chunk)

            drain = asyncio.create_task(consume())
            loop = asyncio.get_running_loop()
            gate = StartupReadbackGate(started=loop.time(), timeout=10)
            for frame in range(55):
                relay.feed(tone(frame, 220))
                assert gate.observe(replace(BUFFERING, sent_bytes=relay.sent_bytes), now=loop.time()) == StartupDecision.WAIT
                await asyncio.sleep(.02)
            assert relay.forwarded_pcm_bytes == 0  # No early or replayed PCM.
            assert gate.observe(replace(BUFFERING, play_status="PLAY_STATE", sent_bytes=relay.sent_bytes), now=loop.time()) == StartupDecision.READY
            assert relay.release_audio()
            for frame in range(40):
                relay.feed(tone(frame, 440))
                await asyncio.sleep(.02)
            relay.update_playback("PAUSED", 1)
            before_pause = relay.forwarded_pcm_bytes
            for frame in range(30):
                relay.feed(tone(frame, 220))
                await asyncio.sleep(.02)
            assert relay.forwarded_pcm_bytes == before_pause
            relay.update_playback("PLAYING", 1)
            for frame in range(40):
                relay.feed(tone(frame, 880))
                await asyncio.sleep(.02)
            relay.update_playback("ENDED", 2)
            await asyncio.wait_for(drain, timeout=5)
            assert relay.failure is None and relay.state == "ENDED"
            assert relay.forwarded_pcm_bytes == 80 * 7680
            return b"".join(chunks)
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()
            if drain and not drain.done():
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
            await relay.close()

    encoded = asyncio.run(run())
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-f", "mp3", "-i", "pipe:0",
                          "-ar", "48000", "-ac", "1", "-f", "s16le", "pipe:1"],
                         input=encoded, capture_output=True, check=True, timeout=5).stdout
    samples = [x[0] for x in struct.iter_unpack("<h", pcm)]
    assert len(samples) > 48000 * 2
    assert max(abs(x) for x in samples[:24000]) <= 2
    assert max(abs(x) for x in samples[60000:]) > 1000
    # Frequency power proves both post-release tones survived the encoder,
    # while the pre-release/paused 220 Hz content was never forwarded.
    assert frequency_power(samples, 440) > frequency_power(samples, 220) * 100
    assert frequency_power(samples, 880) > frequency_power(samples, 220) * 100


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="offline FFmpeg required")
def test_slow_simulated_player_fills_256k_buffer_before_release():
    """Exercise a >10-second buffer, not merely a fast mock PLAY_STATE.

    256 KiB is an explicit test scenario, NOT a recovered Bose buffer size.
    The test fails if the owner aborts on its first BUFFERING observation or
    releases music just because HTTP is flowing. Only loopback is contacted.
    """
    async def run():
        relay = PCMHTTPRelay(bind="127.0.0.1", target="127.0.0.1", token="c" * 64, port=0)
        relay.update_playback("PLAYING", 0)
        await relay.start()
        writer = None
        drain = None
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
            writer.write(b"GET /relay/" + b"c" * 64 + b".mp3 HTTP/1.0\r\n\r\n")
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")
            received = 0
            chunks = []

            async def consume():
                nonlocal received
                while chunk := await reader.read(8192):
                    received += len(chunk)
                    chunks.append(chunk)

            drain = asyncio.create_task(consume())
            loop = asyncio.get_running_loop()
            start = loop.time()
            gate = StartupReadbackGate(started=start, timeout=20)
            observations = []
            while gate.decision == StartupDecision.WAIT:
                relay.feed(tone(0, 220))  # Never forwarded during preflight.
                status = "PLAY_STATE" if received >= 256 * 1024 else "BUFFERING_STATE"
                decision = gate.observe(replace(BUFFERING, play_status=status,
                                               sent_bytes=relay.sent_bytes), now=loop.time())
                observations.append(decision)
                assert decision != StartupDecision.ABORT, gate.reason
                assert relay.forwarded_pcm_bytes == 0 and not relay.snapshot()['audio_released']
                await asyncio.sleep(.1)
            assert 10 < loop.time() - start < 20
            assert observations.count(StartupDecision.WAIT) > 90
            assert relay.release_audio()
            for frame in range(50):
                relay.feed(tone(frame, 440))
                await asyncio.sleep(.02)
            relay.end_input()
            await asyncio.wait_for(drain, timeout=5)
            assert relay.failure is None and relay.state == 'ENDED'
            return b''.join(chunks)
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()
            if drain and not drain.done():
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
            await relay.close()

    body = asyncio.run(run())
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'mp3', '-i', 'pipe:0',
                              '-ac', '1', '-ar', '48000', '-f', 's16le', 'pipe:1'],
                             input=body, capture_output=True, check=True, timeout=5).stdout
    samples = [x[0] for x in struct.iter_unpack('<h', decoded)]
    assert not any(samples[:48000 * 9])
    assert max(abs(x) for x in samples[-48000:]) > 1000
