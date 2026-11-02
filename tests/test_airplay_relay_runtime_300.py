"""Offline encoder and backpressure tests. No sockets, radios or sound output."""
import asyncio
import shutil
import subprocess
import sys

import pytest

from basswiesn.app.services.airplay_bridge.relay_runtime import (
    ClockedMP3Relay, MP3Subscription, RelayFailure,
)
from basswiesn.app.services.airplay_bridge import relay_runtime

pytestmark = pytest.mark.unit


def test_subscriber_drains_then_closes():
    async def check():
        sub = MP3Subscription(2)
        sub.push(b"a")
        sub.push(b"b")
        sub.close(discard=False)
        assert [chunk async for chunk in sub] == [b"a", b"b"]
    asyncio.run(check())


def test_failed_subscriber_discards_queued_audio_and_wakes_waiter():
    async def check():
        sub = MP3Subscription(2)
        sub.push(b"stale")
        sub.close(discard=True)
        assert [chunk async for chunk in sub] == []
        waiting = MP3Subscription(2)
        task = asyncio.create_task(waiting.__anext__())
        await asyncio.sleep(0)
        waiting.close(discard=True)
        with pytest.raises(StopAsyncIteration):
            await task
    asyncio.run(check())


@pytest.mark.parametrize("value", [0, 65, True, 1.5])
def test_bad_output_buffer_bound(value):
    with pytest.raises(ValueError):
        ClockedMP3Relay(subscriber_chunks=value)


def test_subscriber_and_session_bounds():
    runtime = ClockedMP3Relay()
    for _ in range(4):
        runtime.subscribe()
    with pytest.raises(RuntimeError):
        runtime.subscribe()
    assert not runtime.feed(runtime.generation + 1, b"stale")
    runtime.stop()
    assert not runtime.feed(runtime.generation, b"late")


def test_pcm_overflow_stops_session():
    runtime = ClockedMP3Relay()
    with pytest.raises(BufferError):
        runtime.feed(runtime.generation, b"x" * (runtime.pcm.pcm.frame_bytes * 11))
    assert runtime._stop.is_set()
    assert runtime._failure == "PCM_OVERFLOW"


def test_no_subscriber_never_launches_encoder():
    async def check():
        with pytest.raises(RuntimeError):
            await ClockedMP3Relay().run()
    asyncio.run(check())


def test_encoder_missing_is_safe_error(monkeypatch):
    async def missing(*args, **kwargs):
        raise FileNotFoundError("private diagnostic not to publish")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    async def check():
        runtime = ClockedMP3Relay()
        sub = runtime.subscribe()
        with pytest.raises(RelayFailure, match="^ENCODER_IO$"):
            await runtime.run()
        assert [chunk async for chunk in sub] == []
    asyncio.run(check())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires local FFmpeg")
def test_clocked_silence_is_decodable_and_fanout_identical():
    async def collect(sub):
        return b"".join([chunk async for chunk in sub])
    async def check():
        runtime = ClockedMP3Relay(max_silence_ms=500)
        a, b = runtime.subscribe(), runtime.subscribe()
        start = asyncio.get_running_loop().time()
        result, first, second = await asyncio.wait_for(
            asyncio.gather(runtime.run(), collect(a), collect(b)), timeout=10)
        assert .45 < asyncio.get_running_loop().time() - start < 5
        assert result.reason == "SILENCE_EXPIRED"
        assert result.pcm_frames == 25
        assert result.encoder_exit == 0
        assert first == second and len(first) == result.encoded_bytes > 1000
        with pytest.raises(RuntimeError):
            await runtime.run()
        return first
    encoded = asyncio.run(check())
    decoded = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "mp3",
        "-i", "pipe:0", "-f", "s16le", "pipe:1",
    ], input=encoded, capture_output=True, check=True, timeout=5).stdout
    assert len(decoded) >= 48000 * 2 * 2 // 2
    assert not any(decoded)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires local FFmpeg")
def test_slow_consumer_fails_without_unbounded_queue():
    async def check():
        runtime = ClockedMP3Relay(max_silence_ms=1000, subscriber_chunks=1)
        sub = runtime.subscribe()
        with pytest.raises(RelayFailure, match="^SUBSCRIBER_BACKPRESSURE$"):
            await asyncio.wait_for(runtime.run(), timeout=10)
        assert [chunk async for chunk in sub] == []
    asyncio.run(check())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires local FFmpeg")
def test_cancellation_closes_output(monkeypatch):
    created = []
    original = asyncio.create_subprocess_exec
    async def capture(*args, **kwargs):
        process = await original(*args, **kwargs)
        created.append(process)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    async def check():
        runtime = ClockedMP3Relay()
        sub = runtime.subscribe()
        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [chunk async for chunk in sub] == []
        assert runtime.pcm.buffered_bytes == 0
    asyncio.run(check())
    assert len(created) == 1 and created[0].returncode is not None


def test_encoder_early_eof_does_not_look_like_success(monkeypatch):
    monkeypatch.setattr(relay_runtime, "mp3_encoder_command",
                        lambda pcm: [sys.executable, "-c", "pass"])
    async def check():
        runtime = ClockedMP3Relay()
        sub = runtime.subscribe()
        with pytest.raises(RelayFailure) as caught:
            await asyncio.wait_for(runtime.run(), timeout=5)
        assert str(caught.value) in {"ENCODER_EARLY_EOF", "ENCODER_IO"}
        assert [chunk async for chunk in sub] == []
    asyncio.run(check())


def test_full_encoder_pipe_does_not_block_cancellation_cleanup(monkeypatch):
    # A child that never reads PCM and floods stdout. This uses no network,
    # shell, file writes or real audio device; it exercises pipe teardown.
    monkeypatch.setattr(relay_runtime, "mp3_encoder_command", lambda pcm: [
        sys.executable, "-c", "import os\nwhile True: os.write(1, bytes(8192))",
    ])
    async def check():
        runtime = ClockedMP3Relay(subscriber_chunks=1)
        sub = runtime.subscribe()
        with pytest.raises(RelayFailure, match="^SUBSCRIBER_BACKPRESSURE$"):
            await asyncio.wait_for(runtime.run(), timeout=5)
        assert [chunk async for chunk in sub] == []
    asyncio.run(check())
