"""Bounded pyatv sender against the isolated native fixture, never LAN radios.

No discovery, account, stored pairing credentials or production dependencies.
Run inside the fixture's network namespace, unprivileged, with pyatv 0.18.0.
Its realtime AP2/NTP stream is not Apple's buffered AAC/PTP multiroom stream.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import socket
import struct
import time
import traceback
import wave


def require_isolation() -> None:
    if os.getuid() == 0 or {name for _, name in socket.if_nameindex()} != {'lo'}:
        raise RuntimeError('UNPRIVILEGED_NETWORK_NONE_REQUIRED')


def synthetic_audio(seconds: int = 35) -> io.BytesIO:
    """Own quiet stereo calibration tone, not captured/copyrighted music."""
    if type(seconds) is not int or not 1 <= seconds <= 60:
        raise ValueError('bounded audio duration required')
    result = io.BytesIO()
    frame = b''.join(struct.pack('<hh', int(3276 * math.sin(2 * math.pi * 440 * n / 44100)),
        int(3276 * math.sin(2 * math.pi * 660 * n / 44100))) for n in range(44100))
    with wave.open(result, 'wb') as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(44100)
        for _ in range(seconds):
            output.writeframes(frame)
    result.seek(0)
    return result


async def snapshot() -> dict:
    reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', 8765), 2)
    try:
        writer.write(b'GET /status HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        await writer.drain()
        header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
        if not header.startswith(b'HTTP/1.1 200 OK'):
            raise RuntimeError('FIXTURE_STATUS_FAILED')
        fields = {line.split(b':', 1)[0].lower(): line.split(b':', 1)[1].strip()
            for line in header.split(b'\r\n')[1:] if b':' in line}
        length = int(fields.get(b'content-length', b'-1'))
        if not 0 <= length <= 16384:
            raise RuntimeError('FIXTURE_STATUS_TOO_LARGE')
        payload = await asyncio.wait_for(reader.readexactly(length), 2)
        result = json.loads(payload)
        if result.get('fixture') != 'basswiesn-isolated-sender-v1':
            raise RuntimeError('FIXTURE_IDENTITY_MISMATCH')
        return result
    finally:
        writer.close()
        await writer.wait_closed()


async def wait_for(predicate, stream=None, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stream is not None and stream.done():
            stream.result()
            raise RuntimeError('SENDER_STREAM_ENDED_EARLY')
        state = await snapshot()
        if predicate(state):
            return state
        await asyncio.sleep(.1)
    raise TimeoutError('RECEIVER_READBACK_NOT_REACHED')


async def main() -> dict:
    require_isolation()
    # Imported only after isolation checks, not on ordinary unit-test import.
    from importlib.metadata import version
    from ipaddress import IPv4Address
    import pyatv
    from pyatv.conf import AppleTV, ManualService
    from pyatv.const import Protocol
    from pyatv.interface import MediaMetadata
    from pyatv.settings import AirPlayVersion
    from pyatv.storage.memory_storage import MemoryStorage

    if version('pyatv') != '0.18.0':
        raise RuntimeError('SENDER_VERSION_MISMATCH')
    logging.disable(logging.CRITICAL)  # Never persist pairing keys/RTSP debug bodies.
    report = {'sender': 'pyatv-0.18.0', 'result': 'FAIL', 'checks': [],
        'transport': 'AP2_REALTIME_NTP', 'radio_contacts': 0,
        'apple_validation': 'NOT_RUN', 'apple_grouping': 'NOT_TESTED',
        'buffered_aac_ptp': 'NOT_TESTED', 'pause_resume': 'NOT_TESTED'}
    stage = 'fixture_ready'
    client = None
    stream = None
    try:
        await wait_for(lambda s: s['ready'] and s['receiver_alive'], timeout=25)
        for cycle in range(2):
            stage = f'cycle_{cycle}_connect'
            cfg = AppleTV(IPv4Address('127.0.0.1'), 'AP2 Isolated Sender Fixture')
            cfg.add_service(ManualService('basswiesn-offline-fixture', Protocol.RAOP, 7000,
                {'ft': '0x0,0x800', 'am': 'Shairport Sync', 'md': '0,1,2'}))
            storage = MemoryStorage()
            settings = await storage.get_settings(cfg)
            settings.protocols.raop.protocol_version = AirPlayVersion.V2
            client = await pyatv.connect(cfg, asyncio.get_running_loop(), storage=storage)
            await client.audio.set_volume(50)
            before = await snapshot()
            stage = f'cycle_{cycle}_audio_and_metadata'
            stream = asyncio.create_task(client.stream.stream_file(synthetic_audio(),
                metadata=MediaMetadata(title='BASSWIESN calibration', artist='Synthetic test signal')))
            playing = await wait_for(lambda s: s['nonzero_samples'] > before['nonzero_samples'] + 4800
                and s['metadata'].get('minm') == 'BASSWIESN calibration', stream)
            report['checks'].append({'cycle': cycle, 'audio_decoded': True,
                'metadata_received': True, 'peak_dbfs': round(playing['peak_dbfs'], 2)})
            # ACK alone is not success: require a fresh native metadata readback.
            for percent, expected in ((0, -144), (50, -15), (80, -6), (100, 0), (50, -15)):
                stage = f'cycle_{cycle}_volume_{percent}'
                previous = (await snapshot())['volume_sequence']
                await client.audio.set_volume(percent)
                await wait_for(lambda s: s['volume_sequence'] > previous
                    and s['volume_db'] is not None and abs(s['volume_db'] - expected) < .01, stream, 5)
                report['checks'].append({'cycle': cycle, 'volume_percent': percent, 'native_readback': True})
            stage = f'cycle_{cycle}_stop'
            await client.remote_control.stop()
            await asyncio.wait_for(stream, 12)
            stream = None
            pending = client.close()
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)
            client = None
            await wait_for(lambda s: s['native_state'] == 'IDLE' and s['receiver_alive'], timeout=10)
            report['checks'].append({'cycle': cycle, 'teardown_readback': True})
        report['result'] = 'PASS'
    except Exception as exc:
        report.update(failed_stage=stage, error_class=type(exc).__name__)
        report['error_location'] = [
            {'module': frame.filename.rsplit('/', 1)[-1], 'line': frame.lineno, 'function': frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)[-6:]]
    finally:
        if stream is not None and not stream.done():
            stream.cancel()
            await asyncio.gather(stream, return_exceptions=True)
        if client is not None:
            pending = client.close()
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)
    return report


if __name__ == '__main__':
    result = asyncio.run(asyncio.wait_for(main(), 120))
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result['result'] == 'PASS' else 1)
