"""Clock metadata is a local projection, never an audio-control action."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.db import get_db
from basswiesn.app.models import Device, MetadataState, RuntimeState, Setting, Station
from basswiesn.app.repositories.research_state_repository import ResearchStateRepository
from basswiesn.app.routers import cloud, research_state
from basswiesn.app.services.clock_metadata import (
    clock_metadata_timezone, is_clock_projection_echo,
    load_clock_metadata_preference, remember_clock_projection,
    save_clock_metadata_preference,
)
from basswiesn.app.services.metadata_engine import (
    ClockMetadataMode, MetadataProvenance, MetadataSnapshot, clock_display_projection,
)
from basswiesn.app.services.research_runtime import ResearchRuntime

pytestmark = pytest.mark.integration
OBSERVED = datetime(2030, 7, 1, 18, 15, tzinfo=UTC)


@pytest.fixture
def seeded():
    factory = app_db.SessionLocal
    with factory() as db:
        db.add(Device(device_id="CLOCK-A", name="Example Radio", ip_address="192.0.2.42"))
        db.add(Station(name="Example Station", stream_url="https://example.invalid/live", provider_station_id="station-a"))
        db.add(Setting(key="default_timezone", value="Europe/Berlin"))
        ResearchStateRepository(db).upsert_metadata("CLOCK-A", MetadataSnapshot(
            station_id="station-a", station_name="Example Station", provider="LOCAL_INTERNET_RADIO",
            source="LOCAL_INTERNET_RADIO", track="Song", artist="Artist", album="Album",
            updated_at=OBSERVED, provenance=MetadataProvenance.PROVIDER, stale=False,
        ))
        db.commit()
    return factory


@pytest.mark.parametrize("instant,expected", [
    (OBSERVED, "20:15"),
    (datetime(2030, 1, 1, 18, 15, tzinfo=UTC), "19:15"),
    (datetime(2030, 7, 1, 22, 0, tzinfo=UTC), "00:00"),
])
def test_clock_timezone_dst_and_midnight_do_not_duplicate_artist(instant, expected):
    original = MetadataSnapshot(track="Song", artist="Artist")
    rendered = clock_display_projection(original, mode=ClockMetadataMode.APPEND, now=instant, timezone=ZoneInfo("Europe/Berlin"))
    assert rendered == f"Song · {expected}"
    assert original.track == "Song"
    assert original.artist == "Artist"


@pytest.mark.parametrize("value", ["{broken", "null", "[]", '{"enabled":false,"mode":"APPEND"}', '{"enabled":"true"}'])
def test_existing_disabled_or_malformed_preference_is_not_enabled(seeded, value):
    with seeded() as db:
        db.add(Setting(key="research.clock_metadata.CLOCK-A", value=value))
        db.commit()
        assert load_clock_metadata_preference(db, "CLOCK-A").enabled is False
        assert load_clock_metadata_preference(db, "CLOCK-B").enabled is True


def test_timezone_invalid_setting_falls_back_to_utc(seeded):
    with seeded() as db:
        row = db.query(Setting).filter(Setting.key == "default_timezone").one()
        row.value = "Invalid/Timezone"
        assert clock_metadata_timezone(db).key == "UTC"


def test_projection_keeps_real_fields_and_reporting_uses_same_payload(seeded, monkeypatch):
    def fixed_projection(snapshot, *, mode, timezone):
        return clock_display_projection(snapshot, mode=mode, timezone=timezone, now=OBSERVED)
    monkeypatch.setattr(cloud, "clock_display_projection", fixed_projection)
    with seeded() as db:
        device = db.query(Device).one()
        payload = cloud._orion_now_playing_payload(db, device, "station-a")
        db.commit()
        raw = db.query(MetadataState).one()
        assert (raw.track, raw.artist, raw.album) == ("Song", "Artist", "Album")
        assert payload["track"] == "Song · 20:15"
        assert payload["artist"] == "Artist"
        assert is_clock_projection_echo(db, "CLOCK-A", "station-a", payload["track"])
        assert not is_clock_projection_echo(db, "CLOCK-A", "station-b", payload["track"])
        assert not is_clock_projection_echo(db, "CLOCK-B", "station-a", payload["track"])
        save_clock_metadata_preference(db, "CLOCK-A", enabled=False, mode="APPEND")
        assert cloud._orion_now_playing_payload(db, device, "station-a")["track"] == "Song"
        # Disabling must still recognize already-sent, late display readbacks.
        assert is_clock_projection_echo(db, "CLOCK-A", "station-a", "Song · 20:14")


def test_projection_memory_is_bounded_and_minute_rollover_does_not_add_rows(seeded):
    with seeded() as db:
        for index in range(20):
            remember_clock_projection(db, "CLOCK-A", "station-a", f"Song {index} · 20:15")
        db.commit()
        row = db.query(RuntimeState).filter(RuntimeState.key.like("%clock_projection")).one()
        original = row.value
        assert len(json.loads(original)["prefixes"]) == 16
        remember_clock_projection(db, "CLOCK-A", "station-a", "Song 19 · 20:16")
        assert row.value == original
        assert is_clock_projection_echo(db, "CLOCK-A", "station-a", "Song 19 · 20:17")
        assert not is_clock_projection_echo(db, "CLOCK-A", "station-a", "Unknown · 20:17")


def test_legacy_off_and_missing_title_modes_keep_their_meaning(seeded):
    with seeded() as db:
        device = db.query(Device).one()
        save_clock_metadata_preference(db, "CLOCK-A", enabled=True, mode="OFF")
        payload = cloud._orion_now_playing_payload(db, device, "station-a")
        assert payload["track"] == "Song"
        assert payload["askAgainAfter"] == 6
        save_clock_metadata_preference(db, "CLOCK-A", enabled=True, mode="MISSING_TITLE")
        assert cloud._orion_now_playing_payload(db, device, "station-a")["track"] == "Song"
        db.query(MetadataState).one().track = None
        payload = cloud._orion_now_playing_payload(db, device, "station-a")
        assert len(payload["track"]) == 5 and payload["track"][2] == ":"


def test_radio_echo_across_minutes_and_runtime_restart_cannot_become_a_title(seeded):
    async def scenario():
        with seeded() as db:
            remember_clock_projection(db, "CLOCK-A", "station-a", "Song · 20:15")
            db.commit()
        # Independent runtime simulates the polling process and its restart.
        for minute in (15, 16, 20):
            runtime = ResearchRuntime(seeded, clock=lambda: OBSERVED, metadata_coalesce_seconds=0)
            try:
                current = await runtime.ingest_metadata(
                    "CLOCK-A", {"track": f"Song · 20:{minute}", "artist": "Artist", "album": "Album"},
                    provenance=MetadataProvenance.RADIO, station_id="station-a",
                    observed_at=OBSERVED + timedelta(minutes=minute),
                    station_name="Example Station", provider="LOCAL_INTERNET_RADIO", source="LOCAL_INTERNET_RADIO",
                )
                assert current.track == "Song"
                assert current.artist == "Artist"
                assert current.updated_at == OBSERVED
                assert current.provenance == MetadataProvenance.PROVIDER
                await asyncio.sleep(0.01)
                with seeded() as db:
                    assert db.query(MetadataState).one().track == "Song"
            finally:
                await runtime.shutdown()
    asyncio.run(scenario())


def test_delayed_echo_does_not_replace_newer_provider_song(seeded):
    async def scenario():
        runtime = ResearchRuntime(seeded, clock=lambda: OBSERVED, metadata_coalesce_seconds=0)
        try:
            # Populate the monitor cache with the old title.
            await runtime.ingest_metadata("CLOCK-A", {"track": "Song"})
            await asyncio.sleep(0.01)
            with seeded() as db:
                remember_clock_projection(db, "CLOCK-A", "station-a", "Song · 20:15")
                db.query(MetadataState).one().track = "New song"
                db.commit()
            observed = await runtime.ingest_metadata(
                "CLOCK-A", {"track": "Song · 20:15"}, provenance=MetadataProvenance.RADIO,
                source="LOCAL_INTERNET_RADIO", station_id="station-a",
            )
            assert observed.track == "New song"
        finally:
            await runtime.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [
    {"enabled": "true"}, {"enabled": 1}, {},
    {"enabled": True, "mode": "BOGUS"},
    {"enabled": True, "interval_seconds": 0},
    {"enabled": True, "interval_seconds": True},
    {"enabled": True, "interval_seconds": 60.5},
])
def test_clock_api_rejects_ambiguous_values_without_settings_write(seeded, body):
    app = FastAPI()
    app.include_router(research_state.router)
    def dependency():
        with seeded() as db:
            yield db
    app.dependency_overrides[get_db] = dependency
    with TestClient(app) as client:
        result = client.put("/api/devices/CLOCK-A/metadata/clock", json=body)
    assert result.status_code == 422
    with seeded() as db:
        assert db.query(Setting).filter(Setting.key.like("research.clock_metadata.%")).count() == 0
