"""Every field subset and order, atomic preferences, no hardware traffic."""
from datetime import UTC, datetime
from itertools import permutations
import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import Device, MetadataState, RuntimeState, Setting, Station
from basswiesn.app.routers import cloud, research_state
from basswiesn.app.services import station_metadata as metadata
from basswiesn.app.services.clock_metadata import save_clock_metadata_preference
from basswiesn.app.services.metadata_engine import clock_display_projection

pytestmark = pytest.mark.integration
FIELDS = metadata.DISPLAY_FIELDS
SONG = {"track": "Title", "artist": "Artist", "description": "Programme"}
VALUES = dict(station="Station", artist="Artist", title="Title", clock="20:15", other="Programme")


def expected(order, fields, values=VALUES):
    text = ""
    for field in order:
        if field in fields and values.get(field):
            text += ((" " if field == "clock" else " — ") if text else "") + values[field]
    return text


def test_every_subset_and_order_is_rendered_without_implicit_fields():
    count = 0
    for order in permutations(FIELDS):
        for mask in range(32):
            fields = tuple(field for i, field in enumerate(FIELDS) if mask & (1 << i))
            preference = metadata.DisplayPreference("CUSTOM", "other" in fields, fields, order)
            metadata.validate_display_preference(preference)
            result = metadata.display_fields(preference, "Station", SONG, clock_text="20:15")
            assert result == {"track": expected(order, fields), "artist": ""}
            assert preference.needs_metadata == bool(set(fields) & {"artist", "title", "other"})
            count += 1
    assert count == 3840


def test_missing_values_do_not_invent_hidden_station_or_separators():
    for mask in range(32):
        fields = tuple(field for i, field in enumerate(FIELDS) if mask & (1 << i))
        result = metadata.display_fields(metadata.DisplayPreference("CUSTOM", fields=fields), "Station", {}, clock_text="20:15")
        assert result["track"] == expected(FIELDS, fields, {"station": "Station", "clock": "20:15"})


def test_long_fields_preserve_clock_in_every_position():
    for order in permutations(FIELDS):
        result = metadata.display_fields(metadata.DisplayPreference("CUSTOM", True, FIELDS, order), "S" * 256,
                                         {"artist": "A" * 256, "track": "T" * 256, "other_info": "O" * 256}, clock_text="20:15")
        assert len(result["track"]) <= 256
        assert all(value in result["track"] for value in ("SS", "AA", "TT", "OO", "20:15"))
        assert "·" not in result["track"]


@pytest.fixture
def seeded(monkeypatch):
    def fixed(snapshot, *, mode, timezone):
        return clock_display_projection(snapshot, mode=mode, timezone=timezone, now=datetime(2030, 7, 1, 18, 15, tzinfo=UTC))
    monkeypatch.setattr(cloud, "clock_display_projection", fixed)
    with app_db.SessionLocal() as db:
        device = Device(device_id="DISPLAY-A", name="Example", ip_address="192.0.2.42")
        station = Station(name="Station", stream_url="https://example.invalid/live", provider_station_id="example-station")
        db.add_all([device, station, Device(device_id="DISPLAY-B", name="Other", ip_address="192.0.2.43"),
                    Setting(key="default_timezone", value="Europe/Berlin"),
                    RuntimeState(key=metadata.cache_key(station.stream_url), value=json.dumps({**SONG, "observed_timestamp": time.time()})),
                    MetadataState(device_id=device.device_id, station_id="example-station", track="Old echo 09:00", artist="Old artist", album="Old album")])
        db.commit()
    return "DISPLAY-A"


@pytest.mark.parametrize("legacy_clock", [False, True])
def test_all_custom_choices_override_legacy_clock_and_isolate_device(seeded, legacy_clock):
    app = FastAPI()
    app.include_router(research_state.router)
    app.include_router(cloud.router)
    with app_db.SessionLocal() as db:
        save_clock_metadata_preference(db, seeded, enabled=legacy_clock, mode="APPEND")
    with TestClient(app) as client:
        for mask in range(32):
            order = list(reversed(FIELDS))
            fields = [field for field in order if mask & (1 << FIELDS.index(field))]
            body = {"mode": "CUSTOM", "fields": fields, "field_order": order}
            response = client.put(f"/api/devices/{seeded}/metadata/display", json=body)
            assert response.status_code == 200
            saved = client.get(f"/api/devices/{seeded}/metadata/display").json()
            assert all(saved[key] == value for key, value in body.items())
            assert saved["radio_write"] is False
            assert saved["presentation"] == "COMPOSED_TITLE_LINE"
            clock = client.get(f"/api/devices/{seeded}/metadata/clock").json()
            assert clock["enabled"] == ("clock" in fields)
            assert clock["managed_by_display_layout"] is True
            assert client.get("/api/devices/DISPLAY-B/metadata/display").json()["mode"] == "STATION"
            headers = {"x-basswiesn-device-id": seeded}
            payload = client.get("/bmx/orion/now-playing/station/example-station", headers=headers).json()
            reported = client.post("/bmx/orion/reporting/station/example-station", headers=headers,
                                   json={"eventType": "TIMED", "timeIntoTrack": 120}).json()
            assert payload["track"] == expected(order, fields)
            assert payload["artist"] == payload["album"] == ""
            assert payload == reported["_embedded"]["bmx_nowplaying"]
        assert client.put(f"/api/devices/{seeded}/metadata/clock", json={"enabled": True, "mode": "APPEND"}).status_code == 409
        with app_db.SessionLocal() as db:
            value = json.loads(db.query(Setting).filter_by(key=f"research.clock_metadata.{seeded}").one().value)
            assert value["enabled"] == legacy_clock, "CUSTOM is one setting, not a second clock write"


@pytest.mark.parametrize("body", [
    {"mode": "CUSTOM"},
    {"mode": "CUSTOM", "fields": ["title", "title"], "field_order": list(FIELDS)},
    {"mode": "CUSTOM", "fields": ["unsafe"], "field_order": list(FIELDS)},
    {"mode": "CUSTOM", "fields": [True], "field_order": list(FIELDS)},
    {"mode": "CUSTOM", "fields": "title", "field_order": list(FIELDS)},
    {"mode": "CUSTOM", "fields": ["title"], "field_order": ["title"] * 5},
    {"mode": "CUSTOM", "fields": ["title"], "field_order": list(FIELDS[:-1])},
    {"mode": "CUSTOM", "fields": ["other"], "field_order": list(FIELDS), "show_other_info": False},
    {"mode": "STATION", "fields": [], "field_order": list(FIELDS)},
    {"mode": "CUSTOM", "fields": [], "field_order": list(FIELDS), "typo": True},
])
def test_invalid_preferences_are_rejected_without_partial_setting(seeded, body):
    app = FastAPI()
    app.include_router(research_state.router)
    with TestClient(app) as client:
        before = client.get(f"/api/devices/{seeded}/metadata/display").json()
        assert client.put(f"/api/devices/{seeded}/metadata/display", json=body).status_code == 422
        assert client.get(f"/api/devices/{seeded}/metadata/display").json() == before


def test_legacy_mapping_preserves_read_only_load_and_corrupt_custom_fails_closed(seeded):
    with app_db.SessionLocal() as db:
        pref = metadata.DisplayPreference("TRACK_ARTIST", True)
        assert pref.as_public_dict(legacy_clock_enabled=False)["fields"] == ["artist", "title", "other"]
        assert pref.as_public_dict(legacy_clock_enabled=True)["fields"] == ["artist", "title", "clock", "other"]
        db.add(Setting(key=metadata.PREF_PREFIX + seeded, value='{"mode":"CUSTOM","fields":["bad"],"field_order":[]}'))
        db.commit()
        loaded = metadata.load_display_preference(db, seeded)
        assert loaded.mode == "CUSTOM" and not loaded.needs_metadata
        assert loaded.fields == ()
