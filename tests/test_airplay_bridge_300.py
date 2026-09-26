"""AirPlay bridge offline foundations: never discover or contact a radio."""
from datetime import UTC, datetime
import math
import shutil
import struct
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import Device, Setting
from basswiesn.app.routers.airplay_bridge import router
from basswiesn.app.services.airplay_bridge.audio import PCMFormat, PCMRelay, RelayState, mp3_encoder_command
from basswiesn.app.services.airplay_bridge.metadata import BridgeMetadata
from basswiesn.app.services.airplay_bridge.planning import receiver_name
from basswiesn.app.services.station_metadata import DisplayPreference

pytestmark = pytest.mark.unit


def batch(metadata, generation, fields, start=0):
    metadata.apply(generation, start, "ssnc", "mdst")
    for offset, (code, value) in enumerate(fields.items(), start=1):
        metadata.apply(generation, start + offset, "core", code, value)
    return metadata.apply(generation, start + len(fields) + 1, "ssnc", "mden",
                          observed_at=datetime(2030, 1, 1, tzinfo=UTC))


def test_status_uses_database_only_filters_protection_and_never_enables():
    with app_db.SessionLocal() as db:
        db.add_all([
            Device(device_id="AP2-FIXTURE-1", name="Desk", ip_address="192.0.2.42"),
            Device(device_id="AP2-FIXTURE-2", name="Kitchen", ip_address="192.0.2.43"),
            Device(device_id="AP2-EXCLUDED", name="Excluded", ip_address="192.0.2.44"),
            Setting(key="protected_device_ids", value="AP2-EXCLUDED"),
        ])
        db.commit()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/api/airplay-bridge/status")
        assert response.status_code == 200
        value = response.json()
        assert value["enabled"] is False
        assert value["receiver_backend_running"] is False
        assert [item["name"] for item in value["receivers"]] == ["AP2 Desk", "AP2 Kitchen"]
        assert all(not item["advertised"] for item in value["receivers"])
        assert value["capabilities"]["apple_multi_select"] == "NOT_TESTED"
        assert client.post("/api/airplay-bridge/status").status_code == 405
        assert client.post("/api/airplay-bridge/enable").status_code == 404


def test_planning_fails_closed_on_protection_lookup_error(monkeypatch):
    from basswiesn.app.services import protected_devices
    from basswiesn.app.services.airplay_bridge.planning import preview_receivers
    monkeypatch.setattr(protected_devices, "_device_row_by_id", lambda _: (None, True))
    assert preview_receivers([Device(device_id="AP2-FIXTURE", name="Desk", ip_address="192.0.2.42")]) == []


def test_duplicate_names_are_not_silently_advertised():
    from basswiesn.app.services.airplay_bridge.planning import preview_receivers
    devices = [Device(device_id=f"AP2-{i}", name="Desk", ip_address=f"192.0.2.{42+i}") for i in range(2)]
    assert all(row["state"] == "NAME_CONFLICT" for row in preview_receivers(devices))


@pytest.mark.parametrize("name", ["Room", "", "🎵" * 100, "Küche\n\x00", "x" * 200])
def test_names_are_printable_bounded_utf8(name):
    value = receiver_name(name)
    assert value.startswith("AP2 ")
    assert len(value.encode()) <= 63
    assert all(c.isprintable() for c in value)


def test_metadata_is_committed_atomically_and_uses_ordered_display():
    metadata = BridgeMetadata("AP2 Desk")
    generation = metadata.begin()
    metadata.apply(generation, 0, "ssnc", "mdst")
    metadata.apply(generation, 1, "core", "minm", b"Song")
    assert metadata.snapshot().track is None
    metadata.apply(generation, 2, "core", "asar", b"Artist")
    metadata.apply(generation, 3, "core", "asal", b"Album")
    assert metadata.apply(generation, 4, "ssnc", "mden")
    snapshot = metadata.snapshot()
    assert (snapshot.track, snapshot.artist, snapshot.album) == ("Song", "Artist", "Album")
    assert snapshot.source == "LOCAL_INTERNET_RADIO"
    assert snapshot.provider == "AIRPLAY_BRIDGE"
    preference = DisplayPreference("CUSTOM", fields=("title", "artist", "clock"),
        field_order=("title", "artist", "clock", "station", "other"))
    assert metadata.display(preference, clock_text="20:15") == {
        "track": "Song — Artist 20:15", "artist": "", "album": "Album"}


def test_new_song_does_not_retain_missing_artist_album_or_genre():
    metadata = BridgeMetadata("AP2 Desk")
    generation = metadata.begin()
    assert batch(metadata, generation, {"minm": b"First", "asar": b"Old Artist", "asal": b"Old Album", "asgn": b"Jazz"})
    assert batch(metadata, generation, {"minm": b"Second"}, start=10)
    assert metadata.snapshot().artist is None
    assert metadata.snapshot().album is None
    assert metadata.snapshot().track == "Second"


def test_old_session_events_cannot_overwrite_or_clear_new_session():
    metadata = BridgeMetadata("AP2 Desk")
    old = metadata.begin()
    assert batch(metadata, old, {"minm": b"Old"})
    new = metadata.begin()
    assert not batch(metadata, old, {"minm": b"Late old"}, start=10)
    assert metadata.snapshot().track is None
    assert batch(metadata, new, {"minm": b"New"})
    assert not metadata.end(old)
    assert metadata.snapshot().track == "New"
    assert metadata.end(new)
    assert metadata.snapshot().track is None
    assert not batch(metadata, new, {"minm": b"Late new"}, start=20)


@pytest.mark.parametrize("code", ["acre", "daid", "clip", "cmac", "snam", "PICT", "secret", "unknown"])
def test_non_display_payloads_not_retained(code):
    metadata = BridgeMetadata("AP2 Desk")
    generation = metadata.begin()
    metadata.apply(generation, 0, "ssnc", "mdst")
    metadata.apply(generation, 1, "ssnc", code, b"sensitive-marker")
    metadata.apply(generation, 2, "core", code, b"sensitive-marker")
    assert metadata.apply(generation, 3, "ssnc", "mden")
    assert "sensitive-marker" not in str(metadata.snapshot().as_dict())
    assert "sensitive-marker" not in repr(metadata.__dict__)


@pytest.mark.parametrize("bad", [b"x" * 4097, b"\xff\xfe"])
def test_invalid_text_aborts_batch_but_keeps_previous_complete_snapshot(bad):
    metadata = BridgeMetadata("AP2 Desk")
    generation = metadata.begin()
    assert batch(metadata, generation, {"minm": b"Before"})
    assert not batch(metadata, generation, {"minm": bad}, start=10)
    assert metadata.snapshot().track == "Before"


def test_metadata_sequence_replay_and_unbatched_updates_ignored():
    metadata = BridgeMetadata("AP2 Desk")
    generation = metadata.begin()
    assert not metadata.apply(generation, 0, "core", "minm", b"Unbatched")
    assert metadata.snapshot().track is None
    assert batch(metadata, generation, {"minm": b"Track"}, start=2)
    assert not metadata.apply(generation, 2, "ssnc", "mdst")
    assert not metadata.apply(generation, 3, "core", "minm", b"Replay")
    assert not metadata.apply(generation, 4, "ssnc", "mden")
    assert metadata.snapshot().track == "Track"


@pytest.mark.parametrize("rate,size", [(44100, 2), (44100, 4), (48000, 2), (48000, 4)])
def test_pcm_fragment_silence_audio_pause_resume_and_stop(rate, size):
    relay = PCMRelay(PCMFormat(rate, size))
    assert relay.frame() is None
    session = relay.begin()
    count = relay.pcm.frame_bytes
    assert relay.frame() == bytes(count)
    audio = b"\x01" * count
    assert relay.feed(session, audio[:3])
    assert relay.frame() == bytes(count)
    assert relay.feed(session, audio[3:])
    assert relay.frame() == audio
    assert relay.feed(session, audio)
    assert relay.pause(session)
    assert relay.buffered_bytes == 0
    assert relay.frame() == bytes(count)
    assert not relay.feed(session, audio)
    assert relay.resume(session)
    assert relay.feed(session, audio)
    assert relay.frame() == audio
    assert relay.end(session)
    assert relay.frame() is None


def test_pcm_old_generation_rejected_and_new_session_starts_clean():
    relay = PCMRelay()
    old = relay.begin()
    relay.feed(old, b"\x01" * relay.pcm.frame_bytes)
    new = relay.begin()
    assert new != old
    assert relay.buffered_bytes == 0
    assert not relay.feed(old, b"\x01" * relay.pcm.frame_bytes)
    assert not relay.end(old)
    assert not relay.pause(old)
    assert relay.frame() == bytes(relay.pcm.frame_bytes)


def test_pcm_overflow_fails_without_playing_stale_buffer():
    relay = PCMRelay(max_buffer_ms=20)
    session = relay.begin()
    relay.feed(session, bytes(relay.pcm.frame_bytes))
    with pytest.raises(BufferError):
        relay.feed(session, b"x")
    assert relay.state == RelayState.FAILED
    assert relay.buffered_bytes == 0
    assert relay.frame() is None


def test_no_infinite_silence_and_pause_resume_does_not_renew_lease():
    relay = PCMRelay(max_silence_ms=40)
    session = relay.begin()
    assert relay.frame() is not None
    relay.pause(session)
    relay.resume(session)
    assert relay.frame() is not None
    assert relay.frame() is None
    assert relay.state == RelayState.EXPIRED
    assert not relay.feed(session, b"x")


@pytest.mark.parametrize("kwargs", [{"max_buffer_ms": 0}, {"max_buffer_ms": 21},
    {"max_buffer_ms": 3000}, {"max_silence_ms": 30020}, {"max_silence_ms": True}])
def test_pcm_limits(kwargs):
    with pytest.raises(ValueError):
        PCMRelay(**kwargs)


@pytest.mark.parametrize("pcm", [PCMFormat(44100, 2), PCMFormat()])
def test_mp3_silence_audio_silence_stays_one_decodable_stream(pcm):
    """Local in-memory encoding/decoding only. No speaker/audio output device."""
    if not shutil.which("ffmpeg"):
        pytest.skip("optional offline ffmpeg proof requires ffmpeg with libmp3lame")
    relay = PCMRelay(pcm)
    generation = relay.begin()
    parts = [relay.frame() for _ in range(20)]
    samples_per_frame = pcm.sample_rate // 50
    amplitude = 3000 * (65536 if pcm.sample_bytes == 4 else 1)
    sample_format = "<ii" if pcm.sample_bytes == 4 else "<hh"
    for frame in range(50):
        tone = b"".join(struct.pack(sample_format, *(2 * [int(amplitude * math.sin(
            2 * math.pi * 440 * (frame * samples_per_frame + n) / pcm.sample_rate))]))
            for n in range(samples_per_frame))
        assert relay.feed(generation, tone)
        parts.append(relay.frame())
    relay.pause(generation)
    parts.extend(relay.frame() for _ in range(20))
    encoded = subprocess.run(mp3_encoder_command(pcm), input=b"".join(parts), capture_output=True, check=True, timeout=15).stdout
    assert not encoded.startswith(b"ID3")
    decoded = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "mp3", "-i", "pipe:0", "-f", "s16le", "-ac", "2", "-ar", "44100", "pipe:1"],
        input=encoded, capture_output=True, check=True, timeout=15).stdout
    samples = struct.unpack('<' + 'h' * (len(decoded) // 2), decoded)
    assert len(samples) >= int(1.8 * 44100 * 2)
    assert max(abs(v) for v in samples[:4410]) <= 2
    assert max(abs(v) for v in samples[44100:88200]) > 1000
    assert max(abs(v) for v in samples[-4410:]) <= 2
