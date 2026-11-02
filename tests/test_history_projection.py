"""Request-local identity reuse, with unchanged historical field precedence."""

import asyncio

import pytest
from sqlalchemy import event

from basswiesn.app import db as database
from basswiesn.app.models import PlayHistory, Station
from basswiesn.app.routers.api import play_history

pytestmark = pytest.mark.unit


def test_repeated_history_identity_has_bounded_database_queries():
    with database.SessionLocal() as db:
        db.add_all([PlayHistory(device_id="EXAMPLE", station_name="Example station",
            station_display_name="Example station", stream_url="https://example.invalid/radio.mp3",
            source="LOCAL_INTERNET_RADIO", is_confirmed=True) for _ in range(100)])
        db.commit()
        queries = []
        def observe(_conn, _cursor, statement, *_):
            queries.append(statement)
        event.listen(database.engine, "before_cursor_execute", observe)
        try:
            result = asyncio.run(play_history(limit=100, include_internal=True, db=db))
        finally:
            event.remove(database.engine, "before_cursor_execute", observe)
        assert len(result) == 100
        assert all(row["station_display_name"] == "Example station" for row in result)
        assert len(queries) <= 8


def test_new_request_does_not_reuse_stale_station_identity():
    with database.SessionLocal() as db:
        station = Station(name="Before", stream_url="https://example.invalid/live.mp3")
        db.add(station)
        db.flush()
        db.add(PlayHistory(device_id="EXAMPLE", station_id=station.id, station_name="Snapshot",
                           stream_url=station.stream_url))
        db.commit()
        first = asyncio.run(play_history(limit=100, include_internal=True, db=db))
        station.name = "After"
        db.commit()
        second = asyncio.run(play_history(limit=100, include_internal=True, db=db))
    assert first[0]["station_display_name"] == "Before"
    assert second[0]["station_display_name"] == "After"


@pytest.mark.parametrize("field,value", [
    ("station_id", 5), ("station_display_name", "Different snapshot"), ("stream_url", "https://example.invalid/other"),
    ("source", "AUX"), ("source_account", "other-account"), ("content_item_name", "Other item"),
    ("device_id", "OTHER"), ("preset_button", 2), ("internal_event", True), ("is_confirmed", False),
])
def test_every_identity_input_participates_in_request_cache(field, value, monkeypatch):
    from basswiesn.app.services import history_projection as projection
    from basswiesn.app.services.playback_identity import PlaybackIdentity
    calls = []
    def resolve(_db, **kwargs):
        calls.append(kwargs)
        return PlaybackIdentity("Example", None, "snapshot", 92, "radio", "example.invalid", False, True, "example")
    monkeypatch.setattr(projection, "resolve_playback_identity", resolve)
    with database.SessionLocal() as db:
        row = PlayHistory(device_id="EXAMPLE", station_display_name="Original", is_confirmed=True)
        view = projection.HistoryIdentityProjection(db)
        view.resolve(row)
        view.resolve(row)
        assert len(calls) == 1
        setattr(row, field, value)
        view.resolve(row)
        assert len(calls) == 2


def test_projection_is_bounded_without_losing_rows(monkeypatch):
    from basswiesn.app.services import history_projection as projection
    from basswiesn.app.services.playback_identity import PlaybackIdentity
    calls = []
    def resolve(_db, **kwargs):
        calls.append(kwargs)
        return PlaybackIdentity(kwargs["station_name"], None, "snapshot", 92, "radio", "", False, True, "example")
    monkeypatch.setattr(projection, "resolve_playback_identity", resolve)
    with database.SessionLocal() as db:
        view = projection.HistoryIdentityProjection(db)
        for index in range(514):
            row = PlayHistory(device_id="EXAMPLE", station_display_name=f"Station {index}")
            assert view.resolve(row).station_display_name == f"Station {index}"
        assert len(view._resolved) == 512
        assert len(calls) == 514
