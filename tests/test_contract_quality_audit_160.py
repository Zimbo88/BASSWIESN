from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from basswiesn.app.db import get_db
from basswiesn.app.db.migrations import (
    RESEARCH_DOMAIN_MIGRATION,
    RESEARCH_DOMAIN_ROLLBACK_ORDER,
    ensure_schema_baseline,
    inspect_research_domain_schema,
    research_domain_rollback_manifest,
    validate_research_domain_rollback_manifest,
)
from basswiesn.app.models import Device, MetadataState
from basswiesn.app.repositories.research_state_repository import ResearchStateRepository
from basswiesn.app.routers import research_state
from basswiesn.app.services.airplay_readiness import (
    normalize_product_id,
    product_allowed_for_firmware,
)
from basswiesn.app.services.health_models import (
    PlaybackHealth,
    PlaybackSignals,
    ProviderHealth,
    ProviderSignals,
    reduce_playback_health,
    reduce_provider_health,
)
from basswiesn.app.services.metadata_engine import (
    MetadataProvenance,
    MetadataSnapshot,
    normalize_metadata,
)


pytestmark = pytest.mark.unit


def test_health_reducers_require_positive_authoritative_evidence() -> None:
    provider = reduce_provider_health(ProviderSignals(service_available=True))
    playback = reduce_playback_health(PlaybackSignals(radio_status=None))
    explicit_stop = reduce_playback_health(
        PlaybackSignals(radio_status="STOPPED", source=None)
    )
    playing_without_source_projection = reduce_playback_health(
        PlaybackSignals(radio_status="PLAY_STATE", source=None)
    )

    assert provider.state == ProviderHealth.DEGRADED
    assert provider.cause == "INSUFFICIENT_OR_PARTIAL_EVIDENCE"
    assert playback.state == PlaybackHealth.FAILED
    assert playback.cause == "NO_AUTHORITATIVE_RADIO_READBACK"
    assert playback.confidence == 0
    assert explicit_stop.state == PlaybackHealth.STOPPED
    assert playing_without_source_projection.state == PlaybackHealth.PLAYING


def test_selection_change_clears_old_runtime_metadata() -> None:
    before = MetadataSnapshot(
        station_name="Old station",
        station_id="old",
        track="Old track",
        artist="Old artist",
        album="Old album",
        image_url="https://art.example/old.jpg",
        provider="ORION",
        source="LOCAL_INTERNET_RADIO",
        display_projection="Old artist – Old track",
    )

    switched = normalize_metadata(
        {"track": "New track"},
        previous=before,
        station_name="New station",
        station_id="new",
        provider="ORION",
        source="LOCAL_INTERNET_RADIO",
    )
    partial = normalize_metadata({"album": "New album"}, previous=switched)

    assert switched.station_name == "New station"
    assert switched.station_id == "new"
    assert switched.track == "New track"
    assert switched.artist is None
    assert switched.album is None
    assert switched.image_url is None
    assert switched.display_projection is None
    assert partial.track == "New track"
    assert partial.album == "New album"


def test_metadata_keeps_safe_operational_artwork_host_without_secret_query() -> None:
    engine = create_engine("sqlite:///:memory:")
    ensure_schema_baseline(engine)
    db = Session(engine)
    try:
        repository = ResearchStateRepository(db)
        repository.upsert_metadata(
            "radio",
            MetadataSnapshot(
                station_id="station",
                image_url="https://192.0.2.44/art.jpg?token=do-not-store#fragment",
                updated_at=datetime(2026, 8, 14, tzinfo=UTC),
                provenance=MetadataProvenance.PROVIDER,
            ),
        )
        db.commit()
        row = db.query(MetadataState).one()
        assert row.artwork_url == "https://192.0.2.44/art.jpg"
        assert "do-not-store" not in row.artwork_url
    finally:
        db.close()
        engine.dispose()


def test_timeline_cursor_uses_time_and_id_keyset_without_gaps(tmp_path) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'timeline.db'}",
        connect_args={"check_same_thread": False},
    )
    ensure_schema_baseline(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = factory()
    db.add(Device(device_id="CURSOR", name="Cursor", ip_address="192.0.2.1"))
    repository = ResearchStateRepository(db)
    base = datetime(2026, 8, 14, 12, tzinfo=UTC)
    # Insertion IDs deliberately disagree with chronological order; two rows
    # also share a timestamp to exercise the ID tie-breaker.
    for code, offset in (
        ("OLDEST_ID1", 0),
        ("NEW_ID2", 20),
        ("MIDDLE_ID3", 10),
        ("NEWEST_ID4", 20),
        ("OLD_ID5", 0),
    ):
        repository.record_event(
            device_id="CURSOR",
            domain="TEST",
            code=code,
            occurred_at=base + timedelta(seconds=offset),
        )
    db.commit()
    db.close()

    app = FastAPI()
    app.include_router(research_state.router)

    def override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            first = client.get(
                "/api/devices/CURSOR/diagnostics/timeline", params={"limit": 2}
            ).json()
            second = client.get(
                "/api/devices/CURSOR/diagnostics/timeline",
                params={"limit": 2, "cursor": first["next_cursor"]},
            ).json()
            third = client.get(
                "/api/devices/CURSOR/diagnostics/timeline",
                params={"limit": 2, "cursor": second["next_cursor"]},
            ).json()
            invalid = client.get(
                "/api/devices/CURSOR/diagnostics/timeline",
                params={"cursor": "not-a-cursor"},
            )
        codes = [
            item["code"]
            for page in (first, second, third)
            for item in page["items"]
        ]
        assert codes == [
            "NEWEST_ID4",
            "NEW_ID2",
            "MIDDLE_ID3",
            "OLD_ID5",
            "OLDEST_ID1",
        ]
        assert len(codes) == len(set(codes)) == 5
        assert invalid.status_code == 400
    finally:
        engine.dispose()


def test_partial_research_table_is_repaired_without_losing_rows(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'partial.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE metadata_state ("
                    "id INTEGER PRIMARY KEY, device_id TEXT)"
                )
            )
            connection.execute(
                text("INSERT INTO metadata_state (device_id) VALUES ('legacy-radio')")
            )

        ensure_schema_baseline(engine)
        status = inspect_research_domain_schema(engine)
        columns = {
            column["name"] for column in inspect(engine).get_columns("metadata_state")
        }
        with engine.connect() as connection:
            preserved = connection.execute(
                text("SELECT device_id FROM metadata_state")
            ).scalar_one()
            migrations = {
                row[0]
                for row in connection.execute(text("SELECT version FROM schema_migrations"))
            }
        assert status.ready is True
        assert {"track", "artist", "album", "artwork_url_redacted", "stale"} <= columns
        assert preserved == "legacy-radio"
        assert RESEARCH_DOMAIN_MIGRATION in migrations
    finally:
        engine.dispose()


def test_unrepairable_partial_primary_key_does_not_claim_160_migration(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'broken.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE diagnostic_events (id INTEGER, event_id TEXT)")
            )
        with pytest.raises(RuntimeError, match="expected primary key"):
            ensure_schema_baseline(engine)
        with engine.connect() as connection:
            migrations = {
                row[0]
                for row in connection.execute(text("SELECT version FROM schema_migrations"))
            }
        assert RESEARCH_DOMAIN_MIGRATION not in migrations
    finally:
        engine.dispose()


def test_partial_rows_with_missing_identity_fail_closed_before_marker(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'missing-identity.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE metadata_state (id INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO metadata_state DEFAULT VALUES"))
        with pytest.raises(RuntimeError, match="null_required_values"):
            ensure_schema_baseline(engine)
        with engine.connect() as connection:
            migrations = {
                row[0]
                for row in connection.execute(text("SELECT version FROM schema_migrations"))
            }
        assert RESEARCH_DOMAIN_MIGRATION not in migrations
    finally:
        engine.dispose()


def test_rollback_manifest_is_explicit_and_rejects_drift() -> None:
    manifest = research_domain_rollback_manifest()
    assert manifest["destructive_sql_executed"] is False
    assert manifest["drop_tables"] == RESEARCH_DOMAIN_ROLLBACK_ORDER
    assert manifest["retained_shared_tables"] == ("device_firmware_profiles",)
    with pytest.raises(ValueError, match="duplicate"):
        validate_research_domain_rollback_manifest(
            RESEARCH_DOMAIN_ROLLBACK_ORDER + (RESEARCH_DOMAIN_ROLLBACK_ORDER[0],)
        )
    with pytest.raises(ValueError, match="mismatch"):
        validate_research_domain_rollback_manifest(
            RESEARCH_DOMAIN_ROLLBACK_ORDER[:-1]
        )


def test_airplay_hex_product_and_firmware_bounds_are_evidence_bounded() -> None:
    assert normalize_product_id("0939") == "0X0939"
    assert normalize_product_id(2361) == "0X0939"
    assert product_allowed_for_firmware("24.0.7", "0939") is True
    assert product_allowed_for_firmware("24.0.7", "094A") is False
    assert product_allowed_for_firmware("25.0.0", "094A") is True
    assert product_allowed_for_firmware("27.0.6", "0939") is True
    assert product_allowed_for_firmware("28.0.0", "0939") is None
    assert product_allowed_for_firmware("99.0.0", "094A") is None
