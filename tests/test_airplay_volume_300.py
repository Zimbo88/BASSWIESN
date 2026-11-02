"""Offline-only slider/control contract. No radio or receiver connection."""
import base64
import pytest

from basswiesn.app.services.airplay_bridge.volume import project_volume, SessionVolumeCursor
from basswiesn.app.services.airplay_bridge.metadata_wire import MetadataWireDecoder

pytestmark = pytest.mark.unit


def event(value):
    return (b'<item><type>73736e63</type><code>70766f6c</code><length>' + str(len(value)).encode()
            + b'</length><data encoding="base64">' + base64.b64encode(value) + b'</data></item>')


@pytest.mark.parametrize('db,requested,muted', [(-30, 0, False), (-15, 50, False),
                                            (0, 100, False), (-144, 0, True)])
def test_explicit_test_cap_never_exceeds_one(db, requested, muted):
    value = project_volume(db, maximum=1)
    assert value.requested == requested
    assert value.applied == min(requested, 1)
    assert value.muted is muted
    assert value.limited == (requested > 1)


@pytest.mark.parametrize('value', [1, -31, -143, float('nan'), float('inf'), True, '-15'])
def test_invalid_source_value_rejected(value):
    with pytest.raises(ValueError):
        project_volume(value, maximum=1)


@pytest.mark.parametrize('maximum', [0, 101, True, None])
def test_maximum_must_be_explicit_valid_integer(maximum):
    with pytest.raises(ValueError):
        project_volume(-15, maximum=maximum)


def test_session_sequence_and_close_reject_stale_volume_commands():
    state = SessionVolumeCursor(7, maximum=30)
    assert state.consume(6, 1000, 0) is None
    assert state.consume(7, 1, -15).applied == 30
    assert state.consume(7, 1, -20) is None
    assert state.consume(7, 0, -20) is None
    assert state.consume(7, 2, -24).applied == 20
    state.close()
    assert state.consume(7, 3, 0) is None


def test_bounded_five_volume_down_mute_resume_and_duplicate_events():
    """A test cap is a ceiling, not rescaling the whole Apple slider to 0..5.

    Values below the cap must still decrease the physical control position;
    returning from mute cannot inherit mute or exceed the explicit ceiling.
    """
    cursor = SessionVolumeCursor(8, maximum=5)
    steps = [(-24, 20, 5, False), (-29.1, 3, 3, False),
             (-30, 0, 0, False), (-144, 0, 0, True), (-24, 20, 5, False)]
    for sequence, (db, requested, applied, muted) in enumerate(steps):
        intent = cursor.consume(8, sequence, db)
        assert (intent.requested, intent.applied, intent.muted) == (requested, applied, muted)
        assert cursor.consume(8, sequence, db) is None
        assert cursor.consume(7, sequence + 100, 0) is None
    cursor.close()
    assert cursor.consume(8, 100, 0) is None


def test_decoder_returns_only_normalized_source_slider_value():
    decoder = MetadataWireDecoder()
    assert decoder.feed(event(b'-15.00,-20.00,-96.00,0.00')) == [('ssnc', 'pvol', b'-15.000000')]
    assert decoder.feed(event(b'-144.00,0.00,0.00,0.00')) == [('ssnc', 'pvol', b'-144.000000')]


@pytest.mark.parametrize('raw', [b'nan,0,0,0', b'-31,0,0,0', b'1,0,0,0', b'-15,0,0',
                              b'-15,0,0,0,0', b'-15,secret,0,0', b'-15,0,0,inf',
                              b'-15,0,0,999', b'-15,0,0,0\x00'])
def test_decoder_discards_malformed_volume_without_retaining_values(raw):
    decoder = MetadataWireDecoder()
    assert decoder.feed(event(raw)) == []
    assert decoder.discarded == 1 and not decoder._buffer
