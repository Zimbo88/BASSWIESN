"""Sender cannot be aimed at a household device, even by command line."""
import asyncio
import importlib.util
from pathlib import Path
import wave
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(filename):
    spec = importlib.util.spec_from_file_location(filename, ROOT / 'tools' / (filename + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('filename', ['ap2_test_sender', 'ap2_sender_receiver_fixture'])
@pytest.mark.parametrize('uid,interfaces', [(0, [(1, 'lo')]), (1000, [(1, 'lo'), (2, 'eth0')]),
    (1000, [(1, 'lo'), (2, 'wlan0')]), (1000, [])])
def test_no_host_network_or_root_sender(monkeypatch, filename, uid, interfaces):
    module = load(filename)
    monkeypatch.setattr(module.os, 'getuid', lambda: uid)
    monkeypatch.setattr(module.socket, 'if_nameindex', lambda: interfaces)
    with pytest.raises(RuntimeError, match='UNPRIVILEGED_NETWORK_NONE_REQUIRED'):
        module.require_isolation()


@pytest.mark.parametrize('filename', ['ap2_test_sender', 'ap2_sender_receiver_fixture'])
def test_nonroot_loopback_fixture_allowed(monkeypatch, filename):
    module = load(filename)
    monkeypatch.setattr(module.os, 'getuid', lambda: 1000)
    monkeypatch.setattr(module.socket, 'if_nameindex', lambda: [(1, 'lo')])
    module.require_isolation()


def test_synthetic_wav_bounded_quiet_and_decodable():
    module = load('ap2_test_sender')
    with wave.open(module.synthetic_audio(1), 'rb') as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (2, 2, 44100)
        assert audio.getnframes() == 44100
        data = audio.readframes(44100)
        assert len(data) == 176400 and any(data)
    for value in (0, 61, True, 1.1):
        with pytest.raises(ValueError):
            module.synthetic_audio(value)


def test_sender_refuses_lan_before_loading_optional_dependency(monkeypatch):
    module = load('ap2_test_sender')
    monkeypatch.setattr(module.socket, 'if_nameindex', lambda: [(1, 'lo'), (2, 'eth0')])
    with pytest.raises(RuntimeError, match='UNPRIVILEGED_NETWORK_NONE_REQUIRED'):
        asyncio.run(module.main())


def test_stream_failure_cannot_be_reported_as_readback_success(monkeypatch):
    module = load('ap2_test_sender')

    async def scenario():
        async def failed():
            raise ValueError('sender failed')
        task = asyncio.create_task(failed())
        await asyncio.sleep(0)
        with pytest.raises(ValueError, match='sender failed'):
            await module.wait_for(lambda state: True, task)
    asyncio.run(scenario())


def test_internal_bridge_is_exact_not_any_private_lan():
    module = load('ap2_fixture_network')
    routes = [{'dst': '198.18.42.0/29', 'dev': 'eth0', 'scope': 'link'}]
    assert module.validate_bridge(['lo', 'eth0'], ['198.18.42.3'], routes, 1000) == '198.18.42.3'
    for addresses, bad_routes, uid in [
        (['192.168.50.25'], routes, 1000),
        (['198.18.42.1'], routes, 1000),
        (['198.18.42.3', '198.18.42.2'], routes, 1000),
        (['198.18.42.3'], routes, 0),
        (['198.18.42.3'], [], 1000),
        (['198.18.42.3'], routes + [{'dst': 'default', 'gateway': '198.18.42.1'}], 1000),
        (['198.18.42.3'], routes + [{'dst': '192.168.50.0/24', 'dev': 'eth0'}], 1000),
        (['198.18.42.3'], [{'dst': '198.18.42.0/29', 'dev': 'eth0', 'gateway': '198.18.42.1'}], 1000),
    ]:
        with pytest.raises(RuntimeError):
            module.validate_bridge(['lo', 'eth0'], addresses, bad_routes, uid)


@pytest.mark.parametrize('failure', ['sender_exit', 'receiver_exit', 'output_keeps_playing'])
def test_pause_requires_live_receiver_and_quiet_decoded_output(monkeypatch, failure):
    monkeypatch.syspath_prepend(str(ROOT / 'tools'))
    module = load('ap2_buffered_test_sender')
    clock = [0.0]
    process = SimpleNamespace(returncode=1 if failure == 'sender_exit' else None)

    async def snapshot(target):
        return {'receiver_alive': failure != 'receiver_exit', 'nonzero_samples': 100,
                'relay_decoded_samples': int(clock[0] * 48000)}

    async def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(module, 'snapshot', snapshot)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    expected = TimeoutError if failure == 'output_keeps_playing' else RuntimeError
    with pytest.raises(expected):
        asyncio.run(module.wait_quiet('198.18.42.2', process, timeout=2))
