"""One-session FIFO/HTTP worker. No outbound connections or radio writes."""
import asyncio
import json
import os
from pathlib import Path
import signal
import time

from airplay_core.http_relay import PCMHTTPRelay
from airplay_core.fifo_input import FIFOInput
from airplay_core.lifecycle import PlaybackCursor
from airplay_core.heartbeat import ReceiverHeartbeat
from airplay_core.control_lease import ControlLease
from airplay_core.native_group import read_native_group
from network_contract import validate_config

ROOT = Path('/run/ap2')


async def main():
    os.umask(0o077)
    cfg = validate_config(json.loads(Path('/config/receiver.json').read_text()))
    lease = json.loads((ROOT / 'lease.json').read_text())
    relay = PCMHTTPRelay(bind=lease['address'], target=cfg['relay_target'], token=cfg['relay_token'],
                         max_pause_ms=cfg['max_pause_seconds'] * 1000,
                         end_grace_ms=120000 if cfg.get('controller_nonce') else 0)
    playback = PlaybackCursor()
    heartbeat = ReceiverHeartbeat()
    control = ControlLease(cfg['controller_nonce']) if cfg.get('controller_nonce') else None
    control_started = time.monotonic()
    control_state, hold_epoch, ack_sequence = None, -1, 0
    relay.update_playback('WAITING', 0)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    fd = os.open(ROOT / 'audio.fifo', os.O_RDONLY | os.O_NONBLOCK)

    pcm_input = FIFOInput(fd, relay)
    pcm_input.start()
    deadline = loop.time() + cfg['lifetime_seconds']
    ended = False
    try:
        await relay.start()
        while not stop.is_set() and loop.time() < deadline:
            if pcm_input.failure:
                raise RuntimeError(pcm_input.failure)
            heartbeat.check(json.loads((ROOT / 'output-heartbeat.json').read_text()), now=time.monotonic())
            observation = json.loads((ROOT / 'output-playback.json').read_text())
            if playback.accept(observation):
                relay.update_playback(observation['state'], observation['buffer_epoch'])
            if (ROOT / 'output-end').exists() and not ended:
                ended = True
                relay.end_input()
            if control is not None:
                # A living receiver is not a living controller. Missing/replayed
                # owner input cannot inherit an old persistent release marker.
                try:
                    control_state = control.accept(read_native_group(ROOT / 'controller.json'),
                                                   now=time.monotonic())
                except FileNotFoundError:
                    relay.hold_audio()
                    if control.previous is not None or time.monotonic() - control_started >= 5:
                        raise RuntimeError('AIRPLAY_CONTROLLER_LOST') from None
                if control_state is not None:
                    if control_state.hold_epoch != hold_epoch or not control_state.allow_audio:
                        relay.hold_audio()
                    hold_epoch = control_state.hold_epoch
                    if not ended and observation['state'] == 'PLAYING' and control_state.allow_audio:
                        relay.release_audio()
            elif not ended and observation['state'] == 'PLAYING' and (ROOT / 'output-release').exists():
                relay.release_audio()
            tmp = ROOT / 'output-status.tmp'
            tmp.write_text(json.dumps({**relay.snapshot(), 'updated_epoch': time.time(),
                'input_backpressure_events': pcm_input.backpressure_events,
                'controller_sequence': control_state.sequence if control_state else None,
                'controller_hold_epoch': control_state.hold_epoch if control_state else None}))
            tmp.replace(ROOT / 'output-status.json')
            if control_state is not None:
                # Private exact-command receipt. A public status timestamp or
                # successful file write alone is not an audio-gate handshake.
                ack_sequence += 1
                tmp = ROOT / 'controller-ack.tmp'
                tmp.write_text(json.dumps(dict(version=1, nonce=control.nonce,
                    sequence=ack_sequence, observed_monotonic=time.monotonic(),
                    command_sequence=control_state.sequence, hold_epoch=control_state.hold_epoch,
                    allow_audio=control_state.allow_audio,
                    audio_released=relay.snapshot()['audio_released'])))
                tmp.replace(ROOT / 'controller-ack.json')
            await asyncio.sleep(.1)
    finally:
        pcm_input.close()
        os.close(fd)
        await relay.close()


if __name__ == '__main__':
    asyncio.run(main())
