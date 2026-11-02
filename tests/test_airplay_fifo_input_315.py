"""Real local pipes and encoder; never discovery, radios or sound output."""
import asyncio
import os
import shutil

import pytest

from basswiesn.app.services.airplay_bridge.audio import PCMRelay
from basswiesn.app.services.airplay_bridge.fifo_input import FIFOInput
from basswiesn.app.services.airplay_bridge.http_relay import PCMHTTPRelay
from basswiesn.app.services.airplay_bridge.relay_runtime import ClockedMP3Relay

pytestmark = pytest.mark.unit


async def produce(fd, payload):
    offset = 0
    while offset < len(payload):
        try:
            offset += os.write(fd, payload[offset:offset + 8192])
        except BlockingIOError:
            await asyncio.sleep(.001)


def test_burst_is_backpressured_without_loss_or_queue_growth():
    async def check():
        # One second of input arriving immediately used to overflow 200 ms.
        pcm = PCMRelay()
        generation = pcm.begin()
        expected = bytes(range(256)) * 1500
        received = bytearray()
        class Sink:
            def input_read_budget(self):
                return min(8192, pcm.available_bytes)
            def feed(self, chunk):
                pcm.feed(generation, chunk)
        read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
        reader = FIFOInput(read_fd, Sink())
        reader.start()
        task = asyncio.create_task(produce(write_fd, expected))
        try:
            await asyncio.sleep(.05)
            assert not task.done() and pcm.buffered_bytes == 76800
            assert reader.backpressure_events > 0
            for _ in range(150):
                if pcm.buffered_bytes >= pcm.pcm.frame_bytes:
                    received.extend(pcm.frame())
                assert pcm.buffered_bytes <= 76800
                if len(received) == len(expected):
                    break
                await asyncio.sleep(.005)
            await asyncio.wait_for(task, 1)
            assert received == expected and reader.failure is None
        finally:
            reader.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            os.close(read_fd)
            os.close(write_fd)
    asyncio.run(check())


def test_hold_drains_full_pipe_without_retaining_stale_pcm():
    async def check():
        relay = PCMHTTPRelay(bind='127.0.0.1', target='127.0.0.1', token='a' * 64)
        relay._runtime = ClockedMP3Relay()
        relay.state = 'STREAMING'
        relay.release_audio()
        read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
        reader = FIFOInput(read_fd, relay)
        reader.start()
        payload = b'x' * 384000
        task = asyncio.create_task(produce(write_fd, payload))
        try:
            await asyncio.sleep(.05)
            assert relay.input_read_budget() == 0 and not task.done()
            relay.hold_audio()
            await asyncio.wait_for(task, 1)
            for _ in range(100):
                if relay.audio_bytes == len(payload):
                    break
                await asyncio.sleep(.005)
            assert relay.audio_bytes == len(payload)
            assert relay.forwarded_pcm_bytes == 76800
            assert relay._runtime.pcm.buffered_bytes == 0
            assert relay.peak_pcm_bytes == 76800 and relay.failure is None
            assert relay.release_audio()
            assert relay._runtime.pcm.buffered_bytes == 0
        finally:
            reader.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            os.close(read_fd)
            os.close(write_fd)
    asyncio.run(check())


@pytest.mark.parametrize('budget', [-1, True, 8193])
def test_invalid_budget_fails_closed_and_close_cancels_retry(budget):
    async def check():
        class Sink:
            def input_read_budget(self):
                return budget
            def feed(self, chunk):
                pytest.fail('invalid budget consumed bytes')
        read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
        reader = FIFOInput(read_fd, Sink())
        try:
            reader.start()
            os.write(write_fd, b'x')
            await asyncio.sleep(.02)
            assert reader.failure == 'PCM_INPUT_FAILED'
            assert not reader._registered and reader._timer is None
        finally:
            reader.close()
            os.close(read_fd)
            os.close(write_fd)
    asyncio.run(check())


def test_full_buffer_close_and_eof_do_not_spin_or_rearm():
    async def check():
        class Sink:
            budget = 0
            def input_read_budget(self):
                return self.budget
            def feed(self, chunk):
                pytest.fail('must not read when full')
        sink = Sink()
        read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
        reader = FIFOInput(read_fd, sink)
        try:
            reader.start()
            os.write(write_fd, b'x')
            await asyncio.sleep(.02)
            assert 1 <= reader.backpressure_events <= 10
            reader.close()
            await asyncio.sleep(.02)
            assert not reader._registered and reader._timer is None
            with pytest.raises(RuntimeError):
                reader.start()
            os.read(read_fd, 1)
            os.close(write_fd)
            write_fd = None
            sink.budget = 8192
            reader = FIFOInput(read_fd, sink)
            reader.start()
            await asyncio.sleep(.01)
            assert reader._timer is not None and reader.failure is None
        finally:
            reader.close()
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
    asyncio.run(check())


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires offline FFmpeg')
def test_unpaced_pipe_burst_survives_real_clocked_encoder():
    async def check():
        relay = PCMHTTPRelay(bind='127.0.0.1', target='127.0.0.1', token='b' * 64, port=0)
        read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
        reader = FIFOInput(read_fd, relay)
        writer = None
        producing = collecting = None
        await relay.start()
        try:
            stream, writer = await asyncio.open_connection('127.0.0.1', relay.port)
            writer.write(b'GET /relay/' + b'b' * 64 + b'.mp3 HTTP/1.0\r\n\r\n')
            await writer.drain()
            await stream.readuntil(b'\r\n\r\n')
            collecting = asyncio.create_task(stream.read())
            assert relay.release_audio()
            reader.start()
            payload = b'\x00\x00\x00\x08' * 192000  # two seconds, no pacing
            producing = asyncio.create_task(produce(write_fd, payload))
            await asyncio.wait_for(producing, 4)
            for _ in range(100):
                if relay.forwarded_pcm_bytes == len(payload) and relay._runtime.pcm.buffered_bytes == 0:
                    break
                await asyncio.sleep(.02)
            assert relay.forwarded_pcm_bytes == len(payload)
            assert relay._runtime.pcm.audio_frames == 100
            assert relay.failure is None and reader.failure is None
            assert reader.backpressure_events > 0
            assert relay.peak_pcm_bytes <= 76800
            relay.end_input()
            encoded = await asyncio.wait_for(collecting, 5)
            assert len(encoded) > 40000
        finally:
            reader.close()
            for task in (producing, collecting):
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if writer:
                writer.close()
                await writer.wait_closed()
            await relay.close()
            os.close(read_fd)
            os.close(write_fd)
    asyncio.run(check())
