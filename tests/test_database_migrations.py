from sqlalchemy import create_engine, text

from basswiesn.app.db.migrations import (
    SCHEMA_BASELINE,
    ensure_schema_baseline,
    expected_table_names,
    inspect_schema,
)


def test_schema_baseline_detects_and_creates_missing_tables():
    engine = create_engine("sqlite:///:memory:")
    try:
        before = inspect_schema(engine)
        after = ensure_schema_baseline(engine)

        assert before.baseline == SCHEMA_BASELINE == 2
        assert before.existing_tables == ()
        assert before.missing_tables == expected_table_names()
        assert before.ready is False
        assert after.existing_tables == expected_table_names()
        assert after.missing_tables == ()
        assert after.ready is True
    finally:
        engine.dispose()


def test_schema_baseline_is_idempotent_for_initialized_database():
    engine = create_engine("sqlite:///:memory:")
    try:
        first = ensure_schema_baseline(engine)
        second = ensure_schema_baseline(engine)

        assert first == second
        assert second.ready is True
    finally:
        engine.dispose()


def test_schema_baseline_preserves_and_reports_unexpected_tables():
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE external_legacy (id INTEGER PRIMARY KEY)"))

        status = ensure_schema_baseline(engine)

        assert status.ready is True
        assert status.unexpected_tables == ("external_legacy",)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM external_legacy")).scalar_one() == 0
    finally:
        engine.dispose()


def test_schema_baseline_adds_v101_device_and_history_columns():
    engine = create_engine("sqlite:///:memory:")
    try:
        ensure_schema_baseline(engine)
        with engine.connect() as connection:
            device_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(devices)"))}
            history_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(play_history)"))}

        assert {"reachable", "last_failed_at", "failure_count", "offline_reason"} <= device_columns
        assert "internal_event" in history_columns
    finally:
        engine.dispose()


def test_schema_baseline_adds_v110_policy_identity_and_migration_journal():
    engine = create_engine("sqlite:///:memory:")
    try:
        ensure_schema_baseline(engine)
        with engine.connect() as connection:
            device_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(devices)"))}
            history_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(play_history)"))}
            migrations = {row[0] for row in connection.execute(text("SELECT version FROM schema_migrations"))}

        assert {"device_class_override", "safe_mode", "polling_profile_override"} <= device_columns
        assert {"station_display_name", "identity_source", "identity_confidence", "is_confirmed"} <= history_columns
        assert "1.1.0" in migrations
    finally:
        engine.dispose()


def test_schema_baseline_normalizes_numeric_preset_source_aliases():
    engine = create_engine("sqlite:///:memory:")
    try:
        ensure_schema_baseline(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO presets (device_id, button, source, source_account, content_item_xml, location, updated_at) "
                    "VALUES ('RADIO1', 3, '10003', '', "
                    "'<ContentItem source=\"10003\" type=\"stationurl\" location=\"http://local/station\"><itemName>Radio</itemName></ContentItem>', "
                    "'http://local/station', CURRENT_TIMESTAMP)"
                )
            )

        ensure_schema_baseline(engine)
        ensure_schema_baseline(engine)

        with engine.connect() as connection:
            source, xml = connection.execute(text("SELECT source, content_item_xml FROM presets WHERE device_id='RADIO1'")).one()
            migrations = {row[0] for row in connection.execute(text("SELECT version FROM schema_migrations"))}

        assert source == "LOCAL_INTERNET_RADIO"
        assert 'source="LOCAL_INTERNET_RADIO"' in xml
        assert 'source="10003"' not in xml
        assert "1.5.0-preset-source-normalization" in migrations
    finally:
        engine.dispose()
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
