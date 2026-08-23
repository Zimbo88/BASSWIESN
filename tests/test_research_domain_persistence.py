from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from basswiesn.app.db.migrations import (
    RESEARCH_DOMAIN_MIGRATION,
    RESEARCH_DOMAIN_TABLES,
    ensure_schema_baseline,
    research_domain_rollback_order,
)
from basswiesn.app.models import (
    AirPlayReadiness,
    AirPlayReadinessState,
    ArtworkCacheEntry,
    DeviceCapabilitiesState,
    DeviceFirmwareProfile,
    DiagnosticEvent,
    FirmwareProfile,
    MetadataState,
    PlaybackHealth,
    PlaybackHealthState,
    PlaybackState,
    ProviderHealthState,
    ProviderLeaseState,
    ProviderState,
    RecoveryOperation,
    ReportingQueueEntry,
    ReportingState,
    RestrictionState,
)
from basswiesn.app.services.research_state_retention import (
    apply_research_retention,
    research_retention_plan,
)


pytestmark = pytest.mark.unit


def _memory_session() -> tuple[object, Session]:
    engine = create_engine("sqlite:///:memory:")
    ensure_schema_baseline(engine)
    return engine, Session(engine)


def test_domain_names_reuse_existing_models_without_duplicate_tables():
    assert FirmwareProfile is DeviceFirmwareProfile
    assert ProviderState is ProviderHealthState
    assert PlaybackHealth is PlaybackHealthState
    assert AirPlayReadiness is AirPlayReadinessState
    assert FirmwareProfile.__tablename__ == "device_firmware_profiles"
    assert ProviderState.__tablename__ == "provider_health_state"


def test_current_schema_upgrade_is_additive_and_preserves_firmware_profile_data():
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE device_firmware_profiles ("
                "id INTEGER PRIMARY KEY, profile_key TEXT NOT NULL UNIQUE, "
                "model_family TEXT NOT NULL, firmware_family TEXT NOT NULL, "
                "platform TEXT NOT NULL, capabilities_json TEXT NOT NULL, "
                "command_profile_key TEXT NOT NULL, evidence TEXT NOT NULL, "
                "limitations TEXT NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            ))
            connection.execute(text(
                "INSERT INTO device_firmware_profiles "
                "(profile_key, model_family, firmware_family, platform, capabilities_json, "
                "command_profile_key, evidence, limitations, created_at, updated_at) VALUES "
                "('legacy-fw', 'SoundTouch', '27.0.x', 'SM2', '{\"known\": true}', "
                "'', 'legacy evidence', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))

        status = ensure_schema_baseline(engine)
        columns = {column["name"] for column in inspect(engine).get_columns("device_firmware_profiles")}
        with engine.connect() as connection:
            row = connection.execute(text(
                "SELECT profile_key, capabilities_json, version, product_id, auth_hardware_expected "
                "FROM device_firmware_profiles WHERE profile_key = 'legacy-fw'"
            )).one()
            migrations = {item[0] for item in connection.execute(text("SELECT version FROM schema_migrations"))}

        assert status.ready is True
        assert {
            "version", "build", "product_id", "variant", "model", "airplay_capability",
            "auth_hardware_expected", "metadata_capability", "artwork_capability",
            "multiroom_capability", "observed_at",
        } <= columns
        assert row == ("legacy-fw", '{"known": true}', None, None, None)
        assert RESEARCH_DOMAIN_MIGRATION in migrations
        assert set(RESEARCH_DOMAIN_TABLES) <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_uint64_provider_values_round_trip_without_sqlite_overflow():
    engine, db = _memory_session()
    maximum = (1 << 64) - 1
    try:
        db.add_all([
            ProviderLeaseState(device_id="radio", provider_id="bmx", inactivity_timeout_s=maximum),
            RestrictionState(
                device_id="radio",
                source_key="bmx:station",
                inactivity_timeout_s=maximum,
                timer_enabled=True,
                origin="STATION",
            ),
            RestrictionState(
                device_id="radio",
                source_key="bmx:zero",
                inactivity_timeout_s=0,
                timer_enabled=False,
                origin="TRACK_LIST",
            ),
            RestrictionState(
                device_id="radio",
                source_key="bmx:absent",
                inactivity_timeout_s=None,
                timer_enabled=False,
                origin="ABSENT",
            ),
        ])
        db.commit()
        db.expire_all()

        restrictions = {
            row.source_key: row.inactivity_timeout_s
            for row in db.query(RestrictionState).order_by(RestrictionState.source_key).all()
        }
        lease = db.query(ProviderLeaseState).one()
        assert lease.inactivity_timeout_s == maximum
        assert restrictions == {
            "bmx:absent": None,
            "bmx:station": maximum,
            "bmx:zero": 0,
        }

        db.add(RestrictionState(
            device_id="radio",
            source_key="bmx:invalid",
            inactivity_timeout_s=maximum + 1,
        ))
        with pytest.raises(StatementError):
            db.flush()
        db.rollback()
    finally:
        db.close()
        engine.dispose()


def test_nullable_airplay_and_playback_evidence_remains_unknown_not_false():
    engine, db = _memory_session()
    try:
        db.add_all([
            DeviceCapabilitiesState(device_id="radio"),
            AirPlayReadinessState(device_id="radio", blocking_stage="UNKNOWN"),
            PlaybackState(device_id="radio", status="STOPPED"),
            PlaybackHealthState(device_id="radio", state="STOPPED"),
            MetadataState(device_id="radio"),
        ])
        db.commit()
        readiness = db.query(AirPlayReadinessState).one()
        health = db.query(PlaybackHealthState).one()
        capabilities = db.query(DeviceCapabilitiesState).one()

        assert readiness.product_allowed is None
        assert readiness.auth_hardware_seen is None
        assert readiness.source_visible is None
        assert health.source_valid is None
        assert health.stream_alive is None
        assert capabilities.supports_airplay is None
    finally:
        db.close()
        engine.dispose()


def test_reporting_contract_constraints_are_persisted():
    engine, db = _memory_session()
    try:
        db.add(ReportingState(device_id="radio", provider_id="bmx", queue_depth=20, retry_count=5))
        db.add(ReportingQueueEntry(
            item_id="report-1",
            device_id="radio",
            provider_id="bmx",
            queue_slot=19,
            retry_count=5,
        ))
        db.commit()

        db.add(ReportingState(device_id="radio-2", provider_id="bmx", queue_depth=21))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

        db.add(ReportingQueueEntry(
            item_id="report-2",
            device_id="radio",
            provider_id="bmx",
            queue_slot=20,
        ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
    finally:
        db.close()
        engine.dispose()


def test_research_retention_is_bounded_and_preserves_current_snapshots():
    engine, db = _memory_session()
    now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    old = now - timedelta(days=40)
    fresh = now - timedelta(days=1)
    try:
        db.add_all([
            DiagnosticEvent(event_id="old-event", occurred_at=old, code="OLD"),
            DiagnosticEvent(event_id="new-event", occurred_at=fresh, code="NEW"),
            ReportingQueueEntry(
                item_id="old-report", device_id="radio", provider_id="bmx",
                queue_slot=0, status="SUCCESS", updated_at=old,
            ),
            ReportingQueueEntry(
                item_id="active-report", device_id="radio", provider_id="bmx",
                queue_slot=1, status="RETRY_WAIT", updated_at=old,
            ),
            RecoveryOperation(
                operation_id="old-recovery", device_id="radio", status="SUCCESS",
                completed_at=old, updated_at=old,
            ),
            RecoveryOperation(
                operation_id="active-recovery", device_id="radio", status="RECOVERING",
                completed_at=None, updated_at=old,
            ),
            ArtworkCacheEntry(cache_key="old-art", expires_at=old),
            ArtworkCacheEntry(cache_key="new-art", expires_at=fresh),
            ProviderHealthState(device_id="radio", provider_id="bmx", state="HEALTHY"),
        ])
        db.commit()

        plan = research_retention_plan(db, now=now)
        assert plan == {
            "diagnostic_events": 1,
            "reporting_queue_entries": 1,
            "recovery_operations": 1,
            "artwork_entries": 1,
            "eligible_total": 4,
            "batch_size": 500,
            "dry_run": True,
            "current_snapshots_preserved": True,
        }

        result = apply_research_retention(db, now=now)
        assert result["deleted_total"] == 4
        assert {row.event_id for row in db.query(DiagnosticEvent).all()} == {"new-event"}
        assert {row.item_id for row in db.query(ReportingQueueEntry).all()} == {"active-report"}
        assert {row.operation_id for row in db.query(RecoveryOperation).all()} == {"active-recovery"}
        assert {row.cache_key for row in db.query(ArtworkCacheEntry).all()} == {"new-art"}
        assert db.query(ProviderHealthState).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_research_indexes_and_non_destructive_rollback_manifest():
    engine = create_engine("sqlite:///:memory:")
    try:
        ensure_schema_baseline(engine)
        reporting_indexes = {item["name"] for item in inspect(engine).get_indexes("reporting_state")}
        diagnostic_indexes = {item["name"] for item in inspect(engine).get_indexes("diagnostic_events")}

        assert "idx_reporting_state_due" in reporting_indexes
        assert "idx_reporting_state_next_due" in reporting_indexes
        assert "idx_diagnostic_events_device_time" in diagnostic_indexes
        assert "idx_diagnostic_events_retention" in diagnostic_indexes
        assert set(research_domain_rollback_order()) == set(RESEARCH_DOMAIN_TABLES)
        assert "device_firmware_profiles" not in research_domain_rollback_order()
    finally:
        engine.dispose()
