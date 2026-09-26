"""One-session FIFO/HTTP worker. No outbound connections or radio writes."""
import asyncio
import json
import os
from pathlib import Path
import signal
import time

from airplay_core.http_relay import PCMHTTPRelay
from network_contract import validate_config

ROOT = Path('/run/ap2')


async def main():
    os.umask(0o077)
    cfg = validate_config(json.loads(Path('/config/receiver.json').read_text()))
    lease = json.loads((ROOT / 'lease.json').read_text())
    relay = PCMHTTPRelay(bind=lease['address'], target=cfg['relay_target'], token=cfg['relay_token'])
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    fd = os.open(ROOT / 'audio.fifo', os.O_RDONLY | os.O_NONBLOCK)

    def consume():
        try:
            chunk = os.read(fd, 8192)
        except BlockingIOError:
            return
        if chunk:
            relay.feed(chunk)

    loop.add_reader(fd, consume)
    deadline = loop.time() + cfg['lifetime_seconds']
    ended = False
    try:
        await relay.start()
        while not stop.is_set() and loop.time() < deadline:
            if (ROOT / 'output-end').exists() and not ended:
                ended = True
                relay.end_input()
            if not ended and (ROOT / 'output-release').exists():
                relay.release_audio()
            tmp = ROOT / 'output-status.tmp'
            tmp.write_text(json.dumps({**relay.snapshot(), 'updated_epoch': time.time()}))
            tmp.replace(ROOT / 'output-status.json')
            await asyncio.sleep(.1)
    finally:
        loop.remove_reader(fd)
        os.close(fd)
        await relay.close()


if __name__ == '__main__':
    asyncio.run(main())
