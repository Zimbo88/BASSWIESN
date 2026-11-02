"""Automated native AP2/PTP sender, restricted to a disposable internal network.

Uses a separately obtained, hash-pinned external executable, not vendored code.
No discovery, LAN targets, account credentials, radio controller or Apple claim.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
import time
import wave

from ap2_fixture_network import RECEIVERS, SENDER, require_bridge
from ap2_test_sender import synthetic_audio

BINARY = Path('/cliairplay')
BINARY_SHA256 = '73cfae9328d8882114cc788bd15676fc1dec844ff87ca832fb602818cc1b409d'


async def snapshot(target: str) -> dict:
    if target not in RECEIVERS:
        raise RuntimeError('FIXTURE_TARGET_REQUIRED')
    reader, writer = await asyncio.wait_for(asyncio.open_connection(target, 8765), 2)
    try:
        writer.write(b'GET /status HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\n\r\n')
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
        fields = {s.split(b':', 1)[0].lower(): s.split(b':', 1)[1].strip()
            for s in head.split(b'\r\n') if b':' in s}
        length = int(fields.get(b'content-length', b'-1'))
        if not head.startswith(b'HTTP/1.1 200 OK') or not 0 <= length <= 16384:
            raise RuntimeError('FIXTURE_STATUS_REJECTED')
        value = json.loads(await asyncio.wait_for(reader.readexactly(length), 2))
        if value.get('fixture') != 'basswiesn-isolated-sender-v1':
            raise RuntimeError('FIXTURE_IDENTITY_REJECTED')
        return value
    finally:
        writer.close()
        await writer.wait_closed()


async def wait_for(target, predicate, process=None, timeout=15, starting=False):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.returncode is not None:
            raise RuntimeError('SENDER_EXITED')
        try:
            value = await snapshot(target)
        except ConnectionRefusedError:
            if not starting:
                raise
            await asyncio.sleep(.1)
            continue
        if predicate(value):
            return value
        await asyncio.sleep(.1)
    raise TimeoutError('RECEIVER_READBACK_NOT_REACHED')


async def wait_quiet(target, process, timeout=10):
    """Require a full quiet window, not an acknowledgement or a lucky sample."""
    deadline = time.monotonic() + timeout
    previous = None
    quiet_since = time.monotonic()
    while time.monotonic() < deadline:
        if process.returncode is not None:
            raise RuntimeError('SENDER_EXITED_DURING_PAUSE')
        state = await snapshot(target)
        if not state['receiver_alive']:
            raise RuntimeError('RECEIVER_EXITED_DURING_PAUSE')
        samples = (state['nonzero_samples'], state['relay_decoded_samples'])
        if previous != samples:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= 1:
            return state
        previous = samples
        await asyncio.sleep(.1)
    raise TimeoutError('PAUSE_AUDIO_DID_NOT_QUIESCE')


async def main() -> dict:
    if require_bridge() != SENDER:
        raise RuntimeError('SENDER_ADDRESS_REQUIRED')
    if sha256(BINARY.read_bytes()).hexdigest() != BINARY_SHA256:
        raise RuntimeError('SENDER_BINARY_MISMATCH')
    mode = os.environ.get('BASSWIESN_TEST_STREAM', 'buffered')
    if mode not in ('buffered', 'realtime'):
        raise RuntimeError('EXPLICIT_TEST_STREAM_REQUIRED')
    report = {'result': 'FAIL', 'sender_version': 'cliairplay-0.5.8',
        'sender_sha256': BINARY_SHA256, 'transport': 'AP2_' + mode.upper() + '_PTP', 'checks': [],
        'radio_contacts': 0, 'apple_validation': 'NOT_RUN', 'apple_group_selection': 'NOT_TESTED'}
    stage = 'fixture_ready'
    process = None
    tasks = []
    fd = None
    statuses = {}
    try:
        # Two independent native receivers, sequentially. This is NOT an Apple
        # group and does not fabricate groupUUID/receiver ownership evidence.
        for target in RECEIVERS:
            await wait_for(target, lambda s: s['ready'] and s['receiver_alive'], timeout=25, starting=True)
            for cycle in range(2):
                stage = f'{RECEIVERS.index(target)}_{cycle}_connect'
                with tempfile.TemporaryDirectory(prefix='bw-ap2-sender-') as directory:
                    pipe = Path(directory) / 'commands'
                    os.mkfifo(pipe, 0o600)
                    fd = os.open(pipe, os.O_RDWR | os.O_NONBLOCK)
                    statuses = {}
                    process = await asyncio.create_subprocess_exec(str(BINARY), '--protocol', 'airplay2',
                        '--ap2-native', '--ptp', '--if', SENDER,
                        '--port', '7000', '--volume', '50', '--samplerate', '44100',
                        '--bitdepth', '16', '--channels', '2', '--debug', '0', '--cmdpipe', str(pipe), target,
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, limit=16384,
                        env={**os.environ, 'CLIAIRPLAY_BUFFERED': '1' if mode == 'buffered' else '0'})

                    async def logs(reader):
                        while line := await reader.readline():
                            for phrase, code in ((b'HAP transient pair-setup failed', 'PAIRING_FAILED'),
                                    (b'Session SETUP failed', 'SESSION_SETUP_FAILED'),
                                    (b'Stream SETUP failed', 'STREAM_SETUP_FAILED'),
                                    (b'Cannot bind UDP', 'PTP_BIND_FAILED'),
                                    (b'Connection refused', 'CONNECTION_REFUSED'),
                                    (b'not answered our PTP clock', 'PTP_UNANSWERED'),
                                    (b'/info failed', 'INFO_FAILED'),
                                    (b'Events connect failed', 'EVENT_CONNECTION_FAILED')):
                                if phrase in line:
                                    statuses[code] = statuses.get(code, 0) + 1
                            # Keep only fixed event counters, never RTSP bodies,
                            # pairing responses, URLs or uncontrolled log text.
                            match = re.match(rb'^\[STATUS\] ([a-z_]+)(?: |\r?\n)', line)
                            if match and match[1] in (b'connected', b'clock_ready', b'audio', b'started',
                                    b'stopped', b'eof', b'flushed', b'route', b'capabilities', b'latency'):
                                key = match[1].decode()
                                statuses[key] = statuses.get(key, 0) + 1
                                if key == 'clock_ready' and b'state=ready' in line:
                                    statuses['ptp_ready'] = True
                                if key == 'clock_ready':
                                    clock = re.search(rb'\bstate=(cold|probing|ready|stalled)\b', line)
                                    if clock:
                                        statuses['ptp_state'] = clock[1].decode()
                                    mode = re.search(rb'\bmode=(ntp|ptp)\b', line)
                                    if mode:
                                        statuses['timing_mode'] = mode[1].decode()

                    async def audio():
                        with wave.open(synthetic_audio(1), 'rb') as wav:
                            data = wav.readframes(44100)
                        while process.returncode is None:
                            for start in range(0, len(data), 3528):
                                process.stdin.write(data[start:start + 3528])
                                await process.stdin.drain()
                                await asyncio.sleep(.02)

                    tasks = [asyncio.create_task(logs(process.stdout)),
                             asyncio.create_task(logs(process.stderr)), asyncio.create_task(audio())]

                    def command(text):
                        raw = (text + '\n').encode()
                        if os.write(fd, raw) != len(raw):
                            raise RuntimeError('COMMAND_PIPE_PARTIAL_WRITE')

                    before = await snapshot(target)
                    deadline = time.monotonic() + 12
                    while not statuses.get('connected'):
                        if process.returncode is not None or time.monotonic() > deadline:
                            raise RuntimeError('NATIVE_CONNECTION_NOT_REACHED')
                        await asyncio.sleep(.1)
                    command('TITLE=BASSWIESN calibration\nARTIST=Synthetic test signal\nACTION=SENDMETA')
                    command('START_UNIX_MS=0\nACTION=START')
                    stage = f'{RECEIVERS.index(target)}_{cycle}_decoded_audio'
                    state = await wait_for(target, lambda s:
                        s['nonzero_samples'] > before['nonzero_samples'] + 4800
                        and s['relay_decoded_samples'] > before['relay_decoded_samples'] + 4800
                        and -25 < s['relay_peak_dbfs'] < -15
                        and s['metadata'].get('minm') == 'BASSWIESN calibration', process, 20)
                    report['checks'].append({'receiver': RECEIVERS.index(target), 'cycle': cycle,
                        'audio_decoded': True, 'metadata_received': True,
                        'peak_dbfs': round(state['peak_dbfs'], 2),
                        'http_mp3_decoded': True, 'http_mp3_peak_dbfs': round(state['relay_peak_dbfs'], 2)})
                    for percent, expected in ((0, -144), (50, -15), (80, -6), (100, 0), (50, -15)):
                        stage = f'{RECEIVERS.index(target)}_{cycle}_volume_{percent}'
                        previous = (await snapshot(target))['volume_sequence']
                        command(f'VOLUME={percent}')
                        await wait_for(target, lambda s: s['volume_sequence'] > previous
                            and s['volume_db'] is not None and abs(s['volume_db'] - expected) < .01,
                            process, 5)
                        report['checks'].append({'receiver': RECEIVERS.index(target), 'cycle': cycle,
                            'volume_percent': percent, 'native_readback': True})
                    stage = f'{RECEIVERS.index(target)}_{cycle}_pause'
                    command('ACTION=PAUSE')
                    paused = await wait_quiet(target, process)
                    stage = f'{RECEIVERS.index(target)}_{cycle}_resume'
                    command('ACTION=PLAY')
                    await wait_for(target, lambda s:
                        s['nonzero_samples'] > paused['nonzero_samples'] + 4800
                        and s['relay_decoded_samples'] > paused['relay_decoded_samples'] + 4800,
                        process, 15)
                    report['checks'].append({'receiver': RECEIVERS.index(target), 'cycle': cycle,
                        'pause_audio_quiet': True, 'resume_audio_decoded': True})
                    stage = f'{RECEIVERS.index(target)}_{cycle}_stop'
                    previous = (await snapshot(target))['events'].get('pend', 0)
                    command('ACTION=STOP')
                    await asyncio.wait_for(process.wait(), 10)
                    await wait_for(target, lambda s: s['native_state'] == 'IDLE'
                        and s['receiver_alive'] and s['events'].get('pend', 0) > previous, timeout=10)
                    report['checks'].append({'receiver': RECEIVERS.index(target), 'cycle': cycle,
                        'teardown_readback': True, 'sender_statuses': dict(statuses)})
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    tasks = []
                    process = None
                    os.close(fd)
                    fd = None
        report['result'] = 'PASS'
    except Exception as exc:
        report.update(failed_stage=stage, error_class=type(exc).__name__, sender_statuses=statuses)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        if fd is not None:
            os.close(fd)
    return report


if __name__ == '__main__':
    result = asyncio.run(asyncio.wait_for(main(), 140))
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result['result'] == 'PASS' else 1)
