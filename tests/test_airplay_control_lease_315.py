"""Local controller expiry and real worker gate wiring, no LAN or radio I/O."""
import asyncio
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import pytest

from basswiesn.app.services.airplay_bridge.control_lease import ControlLease
from basswiesn.app.services.airplay_bridge.http_relay import PCMHTTPRelay
from basswiesn.app.services.airplay_bridge.relay_runtime import ClockedMP3Relay
from test_airplay_receiver_contract_300 import config, contract

NONCE = 'e' * 32


def control(**values):
    data = dict(version=1, nonce=NONCE, sequence=1, observed_monotonic=100,
                allow_audio=False, hold_epoch=0)
    data.update(values)
    return json.dumps(data).encode()


def test_controller_gate_handshake_replay_and_expiry():
    lease = ControlLease(NONCE)
    assert not lease.accept(control(), now=100).allow_audio
    assert lease.accept(control(sequence=2, observed_monotonic=101, allow_audio=True), now=101).allow_audio
    assert not lease.accept(control(sequence=3, observed_monotonic=102, hold_epoch=1), now=102).allow_audio
    assert not lease.accept(control(sequence=3, observed_monotonic=102, hold_epoch=1), now=106).allow_audio
    with pytest.raises(ValueError):
        lease.accept(control(sequence=4, observed_monotonic=107, allow_audio=True, hold_epoch=1), now=107)
    with pytest.raises(ValueError):
        lease.accept(control(sequence=5, observed_monotonic=108), now=108)


@pytest.mark.parametrize('update', [
    {'nonce': 'f'*32}, {'version': True}, {'allow_audio': 1}, {'hold_epoch': True},
    {'hold_epoch': -1}, {'sequence': True}, {'observed_monotonic': float('nan')},
    {'observed_monotonic': 106}, {'sequence': -1}, {'extra': 'unknown'},
])
def test_bad_controller_observation_is_terminal(update):
    lease = ControlLease(NONCE)
    with pytest.raises(ValueError, match='^AIRPLAY_CONTROLLER_UNVERIFIED$'):
        lease.accept(control(**update), now=100)
    with pytest.raises(ValueError):
        lease.accept(control(), now=100)


def test_same_sequence_cannot_change_permission_and_pause_needs_new_epoch():
    for initial, next_record in [(control(), control(allow_audio=True)),
                                (control(allow_audio=True), control(sequence=2, observed_monotonic=101))]:
        lease = ControlLease(NONCE)
        lease.accept(initial, now=100)
        with pytest.raises(ValueError):
            lease.accept(next_record, now=101)


def test_collapsed_hold_release_is_visible_as_new_epoch():
    lease = ControlLease(NONCE)
    lease.accept(control(allow_audio=True), now=100)
    assert lease.accept(control(sequence=3, observed_monotonic=101, allow_audio=True, hold_epoch=1),
                        now=101).hold_epoch == 1


@pytest.mark.parametrize('nonce', [True, '', 'x'*32, 'e'*31, None])
def test_bad_explicit_controller_config_is_not_legacy_fallback(nonce):
    cfg = {**config(), 'relay_target': '192.0.2.43', 'relay_token': 'a'*64, 'controller_nonce': nonce}
    if nonce is None:
        # Explicit null is legacy-disabled; no fabricated owner authority.
        assert contract.validate_config(cfg)['controller_nonce'] is None
    else:
        with pytest.raises(ValueError):
            contract.validate_config(cfg)


def test_control_nonce_without_relay_target_is_invalid():
    with pytest.raises(ValueError):
        contract.validate_config({**config(), 'controller_nonce': NONCE})


def test_hold_discards_old_pcm_without_ending_session_or_reopening_ended_stream():
    relay = PCMHTTPRelay(bind='127.0.0.1', target='127.0.0.1', token='a'*64)
    relay._runtime = ClockedMP3Relay()
    relay.state = 'STREAMING'
    assert relay.release_audio()
    relay.feed(b'\x01'*7680)
    assert relay._runtime.pcm.buffered_bytes == 7680
    relay.hold_audio()
    assert not relay.snapshot()['audio_released']
    assert relay._runtime.pcm.buffered_bytes == 0
    relay.feed(b'\x02'*7680)
    assert relay._runtime.pcm.buffered_bytes == 0
    assert relay.release_audio()
    relay.feed(b'\x03'*7680)
    assert relay._runtime.pcm.frame() == b'\x03'*7680
    relay.end_input()
    relay.hold_audio()
    assert not relay.release_audio()


def test_real_output_worker_ignores_legacy_release_in_controlled_mode_and_stops_on_owner_loss(tmp_path, monkeypatch):
    root = tmp_path / 'ap2'
    root.mkdir(mode=0o700)
    for name in ('http_relay', 'fifo_input', 'lifecycle', 'heartbeat', 'native_group', 'control_lease'):
        monkeypatch.setitem(sys.modules, 'airplay_core.'+name,
                            importlib.import_module('basswiesn.app.services.airplay_bridge.'+name))
    monkeypatch.setitem(sys.modules, 'network_contract', contract)
    path = Path(__file__).resolve().parents[1] / 'modules/airplay-bridge/radio_output.py'
    spec = importlib.util.spec_from_file_location('ap2_worker_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({**config(), 'relay_target': '192.0.2.43',
        'relay_token': 'a'*64, 'controller_nonce': NONCE}))
    monkeypatch.setattr(module, 'Path', lambda value: cfgfile if value == '/config/receiver.json' else Path(value))
    monkeypatch.setattr(module, 'ROOT', root)
    (root/'lease.json').write_text(json.dumps({'address': '192.0.2.42'}))
    os.mkfifo(root/'audio.fifo', 0o600)
    (root/'output-release').touch()
    (root/'output-playback.json').write_text(json.dumps({'state':'PLAYING', 'sequence':1, 'buffer_epoch':0}))
    stamp = time.monotonic()
    (root/'output-heartbeat.json').write_text(json.dumps({'sequence':1, 'observed_monotonic':stamp}))
    (root/'controller.json').write_bytes(control(observed_monotonic=stamp))
    (root/'controller.json').chmod(0o600)
    seen, relays = [], []

    class Relay:
        def __init__(self, **_):
            self.released, self.closed = False, False
            relays.append(self)
        async def start(self):
            pass
        async def close(self):
            self.closed = True
        def update_playback(self, *_):
            pass
        def feed(self, _):
            pytest.fail('no audio fixture is connected')
        def release_audio(self):
            self.released = True
        def hold_audio(self):
            self.released = False
        def snapshot(self):
            return {'audio_released':self.released}

    async def advance(_):
        seen.append(json.loads((root/'output-status.json').read_text()))
        if len(seen) == 3:
            (root/'controller.json').unlink()
        else:
            (root/'controller.json').write_bytes(control(sequence=len(seen)+1,
                observed_monotonic=time.monotonic(), allow_audio=len(seen)==1,
                hold_epoch=1 if len(seen)==2 else 0))
    monkeypatch.setattr(module, 'PCMHTTPRelay', Relay)
    monkeypatch.setattr(module.asyncio, 'sleep', advance)
    with pytest.raises(RuntimeError, match='AIRPLAY_CONTROLLER_LOST'):
        asyncio.run(module.main())
    assert [sample['audio_released'] for sample in seen] == [False, True, False]
    assert [sample['controller_sequence'] for sample in seen] == [1, 2, 3]
    assert relays[0].closed
