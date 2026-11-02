"""Run in the receiver image with --network none and the loopback fixture.

Real FIFO, audio worker, FFmpeg, HTTP stream and private owner acknowledgement.
Synthetic PCM only; no Apple session, audible speaker or radio hardware claim.
"""
import asyncio
import json
import math
import os
from pathlib import Path
import selectors
import struct
import sys
import time

sys.path.insert(0, '/opt/basswiesn-ap2')
from airplay_core.control_owner import AudioControl
from airplay_core.private_ipc import FileAudioControlPort
import radio_output
from receiver_probe import start_output_when_authorized


def save(root, name, value):
    tmp = root / (name + '.tmp')
    tmp.write_text(json.dumps(value))
    tmp.replace(root / name)


async def main():
    assert os.getuid() != 0
    assert set(os.listdir('/sys/class/net')) == {'lo'}, 'network-none test only'
    os.umask(0o077)
    root = Path('/run/ap2')
    root.mkdir(mode=0o700)
    cfg = json.loads(Path('/config/receiver.json').read_text())
    assert cfg['relay_target'] == '127.0.0.1' and cfg['interface'] == 'lo'
    save(root, 'lease.json', {'address':'127.0.0.1'})
    save(root, 'output-playback.json', {'state':'PLAYING','sequence':1,'buffer_epoch':0})
    save(root, 'output-heartbeat.json', {'sequence':1, 'observed_monotonic':time.monotonic()})
    os.mkfifo(root/'audio.fifo', 0o600)
    fd = os.open(root/'audio.fifo', os.O_RDWR | os.O_NONBLOCK)
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ, 'audio')
    # Existence of the old marker must not override controlled mode.
    (root/'output-release').touch(mode=0o600)
    tone = b''.join(struct.pack('<ii', value, value) for value in
        (int(math.sin(index * 2 * math.pi * 440 / 48000) * 2**24) for index in range(960)))
    received = 0
    discarded_before_authorization = 0
    writer = None
    tasks = []
    async def producer():
        sequence = 1
        while True:
            sequence += 1
            save(root, 'output-heartbeat.json', {'sequence':sequence,'observed_monotonic':time.monotonic()})
            try:
                os.write(fd, tone)
            except BlockingIOError:
                pass
            await asyncio.sleep(.02)
    async def drain(reader):
        nonlocal received
        while chunk := await reader.read(4096):
            received += len(chunk)
    def status():
        return json.loads((root/'output-status.json').read_text())
    async def start_when_owned():
        nonlocal discarded_before_authorization
        worker = None
        def spawn(name, args):
            nonlocal worker
            assert name == 'radio_output' and worker is None
            assert not selector.get_map(), 'competing FIFO reader'
            worker = asyncio.create_task(radio_output.main())
            tasks.append(worker)
        deadline = time.monotonic() + 10
        while not start_output_when_authorized(cfg, root, time.monotonic(), selector, fd, spawn):
            assert time.monotonic() < deadline, 'first held command missing'
            for event, _ in selector.select(0):
                discarded_before_authorization += len(os.read(event.fd, 8192))
            await asyncio.sleep(.02)
        assert worker is not None
        await worker
    try:
        output = asyncio.create_task(start_when_owned())
        tasks.extend([output, asyncio.create_task(producer())])
        # Provisioning a second receiver must not spend the worker's control
        # lease before an owner exists. Still no output listener before hold.
        await asyncio.sleep(5.1)
        assert not (root/'output-status.json').exists() and not output.done()
        assert discarded_before_authorization > 65536, 'native pipe would stall before authorization'
        try:
            _, unexpected = await asyncio.open_connection('127.0.0.1', 8098)
        except ConnectionRefusedError:
            pass
        else:
            unexpected.close()
            await unexpected.wait_closed()
            raise AssertionError('output opened without first held command')
        owner = AudioControl(cfg['controller_nonce'], FileAudioControlPort(root))
        assert not (await owner.hold()).audio_released
        reader, writer = await asyncio.open_connection('127.0.0.1', 8098)
        writer.write(('GET /relay/' + cfg['relay_token'] + '.mp3 HTTP/1.1\r\nHost: localhost\r\n\r\n').encode())
        await writer.drain()
        header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=4)
        assert header.startswith(b'HTTP/1.0 200 OK\r\n')
        tasks.append(asyncio.create_task(drain(reader)))
        deadline = time.monotonic() + 6
        while received < 4096:
            assert time.monotonic() < deadline and not output.done()
            await owner.pulse()
            await asyncio.sleep(.1)
        assert status()['forwarded_pcm_bytes'] == 0
        assert (await owner.release()).audio_released
        await asyncio.sleep(.4)
        assert status()['forwarded_pcm_bytes'] > 0
        assert not (await owner.hold()).audio_released
        held = status()['forwarded_pcm_bytes']
        await asyncio.sleep(.3)
        assert status()['forwarded_pcm_bytes'] == held
        assert status()['failure'] is None
        # Supervisor continues to run, but missing owner input still kills the
        # worker instead of preserving a previous release or replaying a write.
        (root/'controller.json').unlink()
        try:
            await asyncio.wait_for(output, timeout=6)
        except RuntimeError as exc:
            assert str(exc) == 'AIRPLAY_CONTROLLER_LOST'
        else:
            raise AssertionError('missing controller did not terminate worker')
        print(json.dumps({'result':'PASS', 'worker':'real', 'ffmpeg':'real', 'http':'loopback',
            'no_output_before_first_held_command':True,
            'pending_pcm_drained':discarded_before_authorization,
            'held_pcm_not_forwarded':True, 'released_pcm_forwarded':True,
            'hold_acknowledged':True, 'owner_loss_closes_output':True,
            'radio_contacts':0, 'hardware_audio':'NOT_TESTED'}))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if writer:
            writer.close()
            await writer.wait_closed()
        selector.close()
        os.close(fd)


if __name__ == '__main__':
    asyncio.run(main())
