"""Private native receiver fixture; only run in an isolated disposable container.

Consumes actual decoded audio and metadata, without any radio or LAN interface.
The loopback status server exposes aggregate evidence, never session secrets.
"""
from __future__ import annotations

import asyncio
from array import array
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import secrets
import re
import socket
import subprocess
import sys
import struct
import time


def require_isolation() -> None:
    if os.getuid() == 0 or {name for _, name in socket.if_nameindex()} != {'lo'}:
        raise RuntimeError('UNPRIVILEGED_NETWORK_NONE_REQUIRED')


async def main() -> None:
    bind, interface = '127.0.0.1', 'lo'
    if os.environ.get('BASSWIESN_ISOLATED_SENDER_FIXTURE') == '1':
        from ap2_fixture_network import RECEIVERS, require_bridge
        bind = require_bridge()
        if bind not in RECEIVERS:
            raise RuntimeError('RECEIVER_ADDRESS_REQUIRED')
        interface = 'eth0'
    else:
        require_isolation()
    sys.path.insert(0, '/opt/basswiesn-ap2')
    from metadata_wire import MetadataWireDecoder
    from airplay_core.http_relay import PCMHTTPRelay
    from airplay_core.fifo_input import FIFOInput

    os.umask(0o077)
    root = Path('/run/ap2')
    root.mkdir(mode=0o700)
    Path('/run/dbus').mkdir()
    Path('/run/avahi-daemon').mkdir()
    (root / 'bus.conf').write_text('''<busconfig><type>system</type>
<listen>unix:path=/run/dbus/system_bus_socket</listen><policy context="default">
<allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/>
</policy></busconfig>''')
    (root / 'avahi.conf').write_text('[server]\nuse-ipv4=yes\nuse-ipv6=no\n'
        f'allow-interfaces={interface}\n[publish]\npublish-workstation=no\npublish-hinfo=no\n')
    (root / 'shairport.conf').write_text('''
general = { name="AP2 Isolated Sender Fixture"; interface="lo"; port=7000;
  output_backend="pipe"; ignore_volume_control="yes"; };
pipe = { name="/run/ap2/audio.fifo"; output_rate=48000;
  output_format="S32_LE"; output_channels=2; };
metadata = { enabled="yes"; include_cover_art="no";
  pipe_name="/run/ap2/metadata.fifo"; };
diagnostics = { log_verbosity=2; statistics="no"; };
'''.replace('interface="lo"', f'interface="{interface}"')
        .replace('AP2 Isolated Sender Fixture', 'AP2 Isolated Sender Fixture ' + bind.rsplit('.', 1)[-1]))
    binary = Path('/opt/basswiesn-ap2/shairport-sync-observed')
    manifest = json.loads(Path('/opt/basswiesn-ap2/observer-build.json').read_text())
    if sha256(binary.read_bytes()).hexdigest() != manifest['binary_sha256']:
        raise RuntimeError('RECEIVER_BINARY_MISMATCH')
    processes: list[subprocess.Popen] = []
    fds: list[int] = []
    state: dict = {'fixture': 'basswiesn-isolated-sender-v1', 'ready': False,
        'pcm_bytes': 0, 'nonzero_samples': 0, 'peak_dbfs': -200.0,
        'volume_sequence': 0, 'volume_db': None, 'metadata': {}, 'events': {},
        'native_state': 'STARTING', 'receiver_alive': False, 'diagnostic_counts': {}, 'diagnostic_sites': {},
        'radio_contacts': 0, 'apple_validation': 'NOT_RUN'}
    state.update(relay_decoded_samples=0, relay_peak_dbfs=-200.0)
    # This consumer is a software probe, not a radio impersonation or device
    # readback. It exercises the actual production FIFO/MP3 relay implementation.
    token = secrets.token_hex(32)
    relay = PCMHTTPRelay(bind='127.0.0.1', target='127.0.0.1', token=token, port=0,
                         max_pause_ms=120000)
    relay.update_playback('WAITING', 0)
    pcm_input = None
    decoder_process = None
    consumer_writer = None
    consumer_tasks = []
    decoder = MetadataWireDecoder()
    loop = asyncio.get_running_loop()

    def diagnostics(fd: int) -> None:
        try:
            chunk = os.read(fd, 8192).lower()
        except BlockingIOError:
            return
        for match in re.finditer(rb'([a-z0-9_-]+\.c):([0-9]{1,5})', chunk):
            key = match[0].decode()
            if match[1] not in (b'shairport.c', b'rtsp.c', b'mdns_avahi.c') and (
                    len(state['diagnostic_sites']) < 100 or key in state['diagnostic_sites']):
                state['diagnostic_sites'][key] = state['diagnostic_sites'].get(key, 0) + 1
        # Only fixed classifications survive. Never retain raw RTSP bodies,
        # encryption keys or even unrestricted synthetic-fixture logs.
        for phrase, code in ((b'error decrypting', 'DECRYPT_ERROR'),
                (b'unrecognised ssrc', 'UNKNOWN_ENCODING'),
                (b'preparing initial decoding', 'DECODER_PREPARED'),
                (b'no clock', 'NO_CLOCK'), (b'clock not', 'CLOCK_NOT_READY'),
                (b'ptp', 'PTP_DIAGNOSTIC'), (b'anchor', 'ANCHOR_DIAGNOSTIC'),
                (b'buffered audio port closed', 'AUDIO_PORT_CLOSED'),
                (b'error', 'ERROR_DIAGNOSTIC'), (b'play started', 'PLAY_STARTED')):
            if phrase in chunk:
                state['diagnostic_counts'][code] = state['diagnostic_counts'].get(code, 0) + 1

    class MeasuredInput:
        def input_read_budget(self):
            return relay.input_read_budget()

        def feed(self, data):
            relay.feed(data)
            state['pcm_bytes'] += len(data)
            values = array('i')
            values.frombytes(data[:len(data) // 4 * 4])
            if sys.byteorder != 'little':
                values.byteswap()
            if values:
                peak = max(abs(value) for value in values)
                state['nonzero_samples'] += sum(value != 0 for value in values)
                if peak:
                    state['peak_dbfs'] = max(state['peak_dbfs'], 20 * math.log10(peak / 2**31))

    def consume_metadata(fd: int) -> None:
        try:
            data = os.read(fd, 8192)
        except BlockingIOError:
            return
        for event_type, code, value in decoder.feed(data):
            state['events'][code] = state['events'].get(code, 0) + 1
            if code in ('pbeg', 'prsm'):
                relay.update_playback('PLAYING', state['events'].get('pend', 0))
                relay.release_audio()
            elif code == 'pend':
                # Keep this software measurement stream open across native
                # reconnects. Production session teardown is tested separately.
                relay.update_playback('PAUSED', state['events']['pend'])
            if event_type == 'core' and code in ('minm', 'asar', 'asal'):
                state['metadata'][code] = value.decode('utf8')
            elif code == 'pvol':
                state['volume_db'] = float(value)
                state['volume_sequence'] += 1

    async def consume_mp3(reader):
        while chunk := await reader.read(8192):
            decoder_process.stdin.write(chunk)
            await decoder_process.stdin.drain()
        raise RuntimeError('RELAY_EARLY_EOF')

    async def measure_decoded():
        while True:
            # Exact whole stereo frames avoid interpreting an arbitrary pipe
            # read boundary as a sample boundary. Discard all samples afterward.
            chunk = await decoder_process.stdout.readexactly(3840)
            samples = array('h')
            samples.frombytes(chunk)
            if sys.byteorder != 'little':
                samples.byteswap()
            state['relay_decoded_samples'] += sum(abs(x) > 32 for x in samples)
            peak = max(abs(x) for x in samples)
            if peak:
                state['relay_peak_dbfs'] = max(state['relay_peak_dbfs'], 20 * math.log10(peak / 32768))

    async def status(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
            if not request.startswith(b'GET /status HTTP/1.1\r\n'):
                return
            payload = json.dumps(state).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: application/json\r\n'
                + f'Content-Length: {len(payload)}\r\n\r\n'.encode() + payload)
            await writer.drain()
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        await relay.start()
        decoder_process = await asyncio.create_subprocess_exec('ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-probesize', '8192', '-analyzeduration', '0', '-f', 'mp3', '-i', 'pipe:0',
            '-f', 's16le', '-ar', '48000', '-ac', '2', 'pipe:1',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        consumer_reader, consumer_writer = await asyncio.open_connection('127.0.0.1', relay.port)
        consumer_writer.write(f'GET /relay/{token}.mp3 HTTP/1.1\r\nHost: fixture\r\n\r\n'.encode())
        await consumer_writer.drain()
        header = await asyncio.wait_for(consumer_reader.readuntil(b'\r\n\r\n'), 3)
        if not header.startswith(b'HTTP/1.0 200 OK'):
            raise RuntimeError('LOCAL_RELAY_NOT_READY')
        consumer_tasks = [asyncio.create_task(consume_mp3(consumer_reader)),
                          asyncio.create_task(measure_decoded())]
        for kind in ('audio', 'metadata'):
            path = root / (kind + '.fifo')
            os.mkfifo(path, 0o600)
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            fds.append(fd)
            if kind == 'audio':
                pcm_input = FIFOInput(fd, MeasuredInput())
                pcm_input.start()
            else:
                loop.add_reader(fd, consume_metadata, fd)
        for args in (
            ['dbus-daemon', '--nofork', '--config-file=' + str(root / 'bus.conf')],
            ['avahi-daemon', '--no-chroot', '--no-drop-root', '--no-rlimits', '-f', str(root / 'avahi.conf')],
            ['nqptp'],
            [str(binary), '-c', str(root / 'shairport.conf')],
        ):
            processes.append(subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env={**os.environ, 'LD_LIBRARY_PATH': '/usr/lib',
                     'BASSWIESN_AP2_OBSERVER_NONCE': secrets.token_hex(16)}))
            log_fd = processes[-1].stderr.fileno()
            os.set_blocking(log_fd, False)
            loop.add_reader(log_fd, diagnostics, log_fd)
            await asyncio.sleep(.5)
        server = await asyncio.start_server(status, bind, 8765, limit=2048)
        async with server:
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                state['receiver_alive'] = all(child.poll() is None for child in processes)
                if not state['receiver_alive']:
                    raise RuntimeError('FIXTURE_PROCESS_EXITED')
                state['relay'] = relay.snapshot()
                state['fifo_failure'] = pcm_input.failure
                if pcm_input.failure or state['relay']['failure'] or any(task.done() for task in consumer_tasks):
                    raise RuntimeError('FIXTURE_RELAY_FAILED')
                try:
                    native = json.loads((root / 'native-group.json').read_text())
                    state['native_state'] = native['state']
                    # A native startup record can precede the RTSP listener.
                    # Inspect the local socket table without opening a session.
                    state['ready'] = any(
                        row.split()[1].endswith(':1B58') and row.split()[3] == '0A'
                        for row in Path('/proc/net/tcp').read_text().splitlines()[1:])
                except FileNotFoundError:
                    pass
                try:
                    clock = Path('/dev/shm/nqptp').read_bytes()
                    if len(clock) >= 72 and clock[8:40] == clock[40:72]:
                        version, master = struct.unpack_from('<H6xQ', clock)
                        state['clock_version'] = version
                        state['clock_present'] = master != 0
                except FileNotFoundError:
                    pass
                await asyncio.sleep(.1)
    finally:
        if pcm_input:
            pcm_input.close()
        for task in consumer_tasks:
            task.cancel()
        await asyncio.gather(*consumer_tasks, return_exceptions=True)
        if consumer_writer:
            consumer_writer.close()
            await consumer_writer.wait_closed()
        await relay.close()
        if decoder_process and decoder_process.returncode is None:
            decoder_process.kill()
            await asyncio.wait_for(decoder_process.communicate(), 3)
        for fd in fds:
            loop.remove_reader(fd)
            os.close(fd)
        for child in reversed(processes):
            loop.remove_reader(child.stderr.fileno())
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)


if __name__ == '__main__':
    asyncio.run(main())
