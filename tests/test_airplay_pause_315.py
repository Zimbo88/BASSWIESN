"""Bounded long pause without extending broken-input or dead-supervisor leases."""
import pytest

from basswiesn.app.services.airplay_bridge.audio import PCMRelay, RelayState
from basswiesn.app.services.airplay_bridge.heartbeat import ReceiverHeartbeat

pytestmark = pytest.mark.unit


def test_explicit_two_minute_pause_can_resume_on_same_generation():
    pcm = PCMRelay(max_pause_ms=300000)
    generation = pcm.begin()
    pcm.feed(generation, b'\x01' * pcm.pcm.frame_bytes)
    assert any(pcm.frame())
    assert pcm.pause(generation)
    for _ in range(120 * 50):
        assert pcm.frame() == bytes(pcm.pcm.frame_bytes)
    assert pcm.resume(generation)
    pcm.feed(generation, b'\x02' * pcm.pcm.frame_bytes)
    assert pcm.frame() == b'\x02' * pcm.pcm.frame_bytes
    assert pcm.state == RelayState.ACTIVE and pcm.generation == generation


def test_long_pause_does_not_extend_unexpected_input_loss():
    pcm = PCMRelay(max_pause_ms=300000)
    pcm.begin()
    for _ in range(1500):
        assert pcm.frame() is not None
    assert pcm.frame() is None and pcm.state == RelayState.EXPIRED


def test_pause_toggles_cannot_renew_either_budget():
    pcm = PCMRelay(max_silence_ms=40, max_pause_ms=60)
    generation = pcm.begin()
    assert pcm.frame() is not None
    for _ in range(3):
        assert pcm.pause(generation)
        assert pcm.frame() is not None
        assert not pcm.pause(generation)
        assert pcm.resume(generation)
    assert pcm.frame() is not None
    assert pcm.frame() is None
    assert pcm.state == RelayState.EXPIRED
    assert not pcm.resume(generation)


def test_pause_budget_expires_even_if_receiver_remains_alive():
    pcm = PCMRelay(max_pause_ms=40)
    generation = pcm.begin()
    pcm.pause(generation)
    assert pcm.frame() is not None
    assert pcm.frame() is not None
    assert pcm.frame() is None and pcm.state == RelayState.EXPIRED
    assert not pcm.feed(generation, bytes(pcm.pcm.frame_bytes))


def test_legacy_pause_budget_is_unchanged_unless_explicitly_selected():
    pcm = PCMRelay(max_silence_ms=40)
    generation = pcm.begin()
    pcm.frame()
    pcm.pause(generation)
    assert pcm.frame() is not None
    assert pcm.frame() is None


@pytest.mark.parametrize('value', [True, 0, -20, 21, 900020, float('inf'), '300000'])
def test_invalid_pause_budget_is_rejected(value):
    with pytest.raises(ValueError):
        PCMRelay(max_pause_ms=value)


def pulse(sequence=1, stamp=10.0):
    return {'sequence': sequence, 'observed_monotonic': stamp}


def test_duplicate_heartbeat_is_fresh_only_until_original_deadline():
    heartbeat = ReceiverHeartbeat()
    heartbeat.check(pulse(), now=10)
    heartbeat.check(pulse(), now=14.99)
    with pytest.raises(ValueError, match='RECEIVER_LIVENESS_UNVERIFIED'):
        heartbeat.check(pulse(), now=15)
    with pytest.raises(ValueError):
        heartbeat.check(pulse(2, 15), now=15)


def test_monotonic_renewal_is_accepted():
    heartbeat = ReceiverHeartbeat()
    for index in range(300):
        heartbeat.check(pulse(index, index + 10.0), now=index + 10.1)


@pytest.mark.parametrize('value,now', [
    ({}, 10), (pulse(True), 10), (pulse(-1), 10), (pulse(2**53), 10),
    (pulse(stamp=True), 10), (pulse(stamp=float('nan')), 10),
    (pulse(stamp=float('inf')), 10), (pulse(stamp=11), 10),
    (pulse(stamp=-1), 10), (pulse(), float('nan')), (pulse(), True),
    ({**pulse(), 'sender_secret': 'discard'}, 10),
])
def test_invalid_heartbeat_fails_closed_without_echo(value, now):
    with pytest.raises(ValueError, match='^RECEIVER_LIVENESS_UNVERIFIED$'):
        ReceiverHeartbeat().check(value, now=now)


@pytest.mark.parametrize('value,now', [
    (pulse(1, 11), 11), (pulse(0, 11), 11),
    (pulse(2, 10), 11), (pulse(2, 9), 11), (pulse(), 9.9),
])
def test_heartbeat_cannot_renew_with_replay_or_clock_regression(value, now):
    heartbeat = ReceiverHeartbeat()
    heartbeat.check(pulse(), now=10)
    with pytest.raises(ValueError):
        heartbeat.check(value, now=now)
