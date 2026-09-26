"""Loopback-only HTTP/PCM contract tests, never a radio or LAN scan."""
import asyncio
import shutil
import subprocess

import pytest

from basswiesn.app.services.airplay_bridge.http_relay import PCMHTTPRelay

pytestmark = pytest.mark.unit
TOKEN = 'a' * 64
PATH = '/relay/' + TOKEN + '.mp3'


async def request(relay, path=PATH, method='GET', extra=b''):
    reader, writer = await asyncio.open_connection('127.0.0.1', relay.port)
    writer.write(f'{method} {path} HTTP/1.0\r\n'.encode() + extra + b'\r\n')
    await writer.drain()
    return reader, writer


@pytest.mark.parametrize('path,method,status', [
    ('/wrong', 'GET', 404), (PATH, 'POST', 404), (PATH + '?token=x', 'GET', 404),
    (PATH, 'HEAD', 200),
])
def test_exact_path_method_and_head_does_not_start_audio(path, method, status):
    async def check():
        relay = PCMHTTPRelay(bind='127.0.0.1', target='127.0.0.1', token=TOKEN, port=0)
        await relay.start()
        try:
            reader, writer = await request(relay, path, method)
            response = await asyncio.wait_for(reader.read(), timeout=4)
            assert response.startswith(f'HTTP/1.0 {status}'.encode())
            assert not relay._used and relay._runtime is None
            writer.close()
            await writer.wait_closed()
        finally:
            await relay.close()
    asyncio.run(check())


def test_peer_check_cannot_be_overridden_by_forwarded_header():
    async def check():
        relay = PCMHTTPRelay(bind='127.0.0.1', target='192.0.2.42', token=TOKEN, port=0)
        await relay.start()
        try:
            reader, writer = await request(relay, extra=b'X-Forwarded-For: 192.0.2.42\r\n')
            # A rejected peer may receive TCP reset while still sending its
            # request. Neither response nor reset may start the encoder.
            try:
                response = await reader.read()
            except ConnectionResetError:
                response = b''
            assert not response or response.startswith(b'HTTP/1.0 403')
            assert not relay._used
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                pass
        finally:
            await relay.close()
    asyncio.run(check())


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires offline FFmpeg')
def test_stream_is_decodable_input_end_finishes_and_url_cannot_replay():
    async def check():
        relay = PCMHTTPRelay(bind='127.0.0.1', target='127.0.0.1', token=TOKEN, port=0)
        await relay.start()
        try:
            reader, writer = await request(relay)
            headers = await reader.readuntil(b'\r\n\r\n')
            assert b'audio/mpeg' in headers
            await asyncio.sleep(.6)
            relay.end_input()
            body = await asyncio.wait_for(reader.read(), timeout=5)
            assert len(body) > 1000 and relay.sent_bytes == len(body)
            assert relay.state == 'ENDED'
            writer.close()
            await writer.wait_closed()
            reader, writer = await request(relay)
            assert (await reader.read()).startswith(b'HTTP/1.0 410')
            writer.close()
            await writer.wait_closed()
            return body
        finally:
            await relay.close()
    body = asyncio.run(check())
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'mp3', '-i', 'pipe:0',
        '-f', 's16le', 'pipe:1'], input=body, capture_output=True, timeout=5, check=True).stdout
    assert len(decoded) > 40000 and not any(decoded)


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires offline FFmpeg')
def test_disconnect_stops_encoder_and_output():
    async def check():
        relay = PCMHTTPRelay(bind='127.0.0.1', target='127.0.0.1', token=TOKEN, port=0)
        await relay.start()
        try:
            reader, writer = await request(relay)
            await reader.readuntil(b'\r\n\r\n')
            writer.close()
            await writer.wait_closed()
            for _ in range(100):
                if relay._runtime is None:
                    break
                await asyncio.sleep(.02)
            assert relay._runtime is None
            assert relay.state == 'FAILED'
            assert relay.failure == 'RADIO_DISCONNECTED'
        finally:
            await relay.close()
    asyncio.run(check())


@pytest.mark.parametrize('token', ['', 'short', 'x' * 64, 'a' * 65])
def test_invalid_token_rejected(token):
    with pytest.raises(ValueError):
        PCMHTTPRelay(bind='127.0.0.1', target='192.0.2.42', token=token)


def test_input_ended_before_get_cannot_start_or_release_encoder():
    async def check():
        relay = PCMHTTPRelay(bind='127.0.0.1', target='127.0.0.1', token=TOKEN, port=0)
        await relay.start()
        try:
            assert not relay.release_audio()
            relay.end_input()
            relay.feed(b'x' * 7680)
            assert not relay.release_audio() and relay.audio_bytes == 0
            reader, writer = await request(relay)
            assert (await reader.read()).startswith(b'HTTP/1.0 410')
            assert not relay._used
            writer.close()
            await writer.wait_closed()
        finally:
            await relay.close()
    asyncio.run(check())


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires offline FFmpeg')
def test_nonzero_pcm_is_discarded_until_explicit_safety_release():
    async def check():
        relay = PCMHTTPRelay(bind='127.0.0.1', target='127.0.0.1', token=TOKEN, port=0)
        await relay.start()
        try:
            reader, writer = await request(relay)
            await reader.readuntil(b'\r\n\r\n')
            for _ in range(25):
                relay.feed(b'\x00\x00\x00\x08' * 1920)
                await asyncio.sleep(.02)
            assert relay.nonzero_chunks == 25
            assert relay.forwarded_pcm_bytes == 0
            assert relay._runtime.pcm.buffered_bytes == 0
            assert relay.release_audio()
            # Release must not replay any of the discarded preflight input.
            assert relay._runtime.pcm.buffered_bytes == 0
            relay.end_input()
            body = await asyncio.wait_for(reader.read(), timeout=5)
            assert not relay.release_audio()
            writer.close()
            await writer.wait_closed()
            return body
        finally:
            await relay.close()
    body = asyncio.run(check())
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'mp3', '-i', 'pipe:0',
        '-f', 's16le', 'pipe:1'], input=body, capture_output=True, timeout=5, check=True).stdout
    assert len(decoded) > 40000 and not any(decoded)
