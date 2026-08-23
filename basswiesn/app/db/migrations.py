"""Non-destructive schema baseline and additive release migrations."""

from dataclasses import dataclass
import json

from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from basswiesn.app.db.database import Base
from basswiesn.app.db import models as _models  # noqa: F401


SCHEMA_BASELINE = 2
RESEARCH_DOMAIN_MIGRATION = "1.6.0-research-domain"
RESEARCH_DOMAIN_TABLES = (
    "device_capability_snapshots",
    "provider_health_state",
    "provider_lease_state",
    "reporting_state",
    "reporting_queue",
    "metadata_state",
    "playback_state",
    "playback_health_state",
    "restriction_state",
    "airplay_readiness_state",
    "diagnostic_events",
    "recovery_operations",
    "artwork_cache",
)
RESEARCH_DOMAIN_SHARED_ALTERATIONS = ("device_firmware_profiles",)
RESEARCH_DOMAIN_ROLLBACK_ORDER = (
    "artwork_cache",
    "recovery_operations",
    "diagnostic_events",
    "airplay_readiness_state",
    "restriction_state",
    "playback_health_state",
    "playback_state",
    "metadata_state",
    "reporting_queue",
    "reporting_state",
    "provider_lease_state",
    "provider_health_state",
    "device_capability_snapshots",
)
RESEARCH_DOMAIN_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "device_capability_snapshots": ("device_id",),
    "provider_health_state": ("device_id", "provider_id"),
    "provider_lease_state": ("device_id", "provider_id"),
    "reporting_state": ("device_id", "provider_id"),
    "reporting_queue": ("item_id", "device_id", "provider_id", "queue_slot"),
    "metadata_state": ("device_id",),
    "playback_state": ("device_id",),
    "playback_health_state": ("device_id",),
    "restriction_state": ("device_id", "source_key"),
    "airplay_readiness_state": ("device_id",),
    "diagnostic_events": ("event_id", "code"),
    "recovery_operations": ("operation_id", "device_id"),
    "artwork_cache": ("cache_key",),
}


@dataclass(frozen=True)
class SchemaStatus:
    baseline: int
    expected_tables: tuple[str, ...]
    existing_tables: tuple[str, ...]
    missing_tables: tuple[str, ...]
    unexpected_tables: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.missing_tables


@dataclass(frozen=True)
class ResearchSchemaStatus:
    """Structural status for safely repairable 1.6 state tables."""

    missing_tables: tuple[str, ...]
    missing_columns: tuple[str, ...]
    invalid_primary_keys: tuple[str, ...]
    null_required_values: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not (
            self.missing_tables
            or self.missing_columns
            or self.invalid_primary_keys
            or self.null_required_values
        )


def expected_table_names() -> tuple[str, ...]:
    return tuple(sorted(Base.metadata.tables))


def inspect_schema(bind: Engine) -> SchemaStatus:
    expected = expected_table_names()
    existing = tuple(sorted(inspect(bind).get_table_names()))
    return SchemaStatus(
        baseline=SCHEMA_BASELINE,
        expected_tables=expected,
        existing_tables=existing,
        missing_tables=tuple(sorted(set(expected) - set(existing))),
        unexpected_tables=tuple(sorted(set(existing) - set(expected))),
    )


def ensure_schema_baseline(bind: Engine) -> SchemaStatus:
    """Create missing known tables without altering or dropping existing ones."""

    Base.metadata.create_all(bind=bind)
    _ensure_schema_migration("1.0.x-baseline", "Base tables and 1.0.x additive columns", bind)
    _ensure_device_state_columns(bind)
    _ensure_station_stream_columns(bind)
    _ensure_scheduled_action_columns(bind)
    _ensure_play_history_columns(bind)
    _ensure_maintenance_columns(bind)
    _ensure_policy_columns(bind)
    _ensure_discovery_columns(bind)
    _ensure_play_history_identity_columns(bind)
    _ensure_multiroom_scenario_columns(bind)
    _ensure_firmware_profile_columns(bind)
    _ensure_write_ledger_columns(bind)
    _redact_legacy_write_ledger_secrets(bind)
    _ensure_research_domain_columns(bind)
    _ensure_160_unique_indexes(bind)
    _ensure_indexes(bind)
    _ensure_full_test_indexes(bind)
    _ensure_15_indexes(bind)
    _ensure_160_indexes(bind)
    _seed_device_model_definitions(bind)
    _seed_telnet_device_profiles(bind)
    _backfill_play_history_identity(bind)
    _classify_internal_stations(bind)
    _normalize_preset_source_aliases(bind)
    research_status = inspect_research_domain_schema(bind)
    if not research_status.ready:
        raise RuntimeError(
            "1.6 research schema validation failed: "
            f"missing_tables={research_status.missing_tables}, "
            f"missing_columns={research_status.missing_columns}, "
            f"invalid_primary_keys={research_status.invalid_primary_keys}, "
            f"null_required_values={research_status.null_required_values}"
        )
    _ensure_schema_migration("1.1.0", "Portable Safe Mode, adaptive polling and playback identity snapshots", bind)
    _ensure_schema_migration("1.1.0-device-policy", "Central device policy and safe defaults", bind)
    _ensure_schema_migration("1.1.0-playback-identity", "Stable playback identity snapshots", bind)
    _ensure_schema_migration("1.1.0-discovery", "SSDP discovery metadata and history tables", bind)
    _ensure_schema_migration("1.1.0-device-models", "Curated SoundTouch model definitions and overrides", bind)
    _ensure_schema_migration("1.1.0-health", "Healthcheck and quick-fix journal tables", bind)
    _ensure_schema_migration("1.1.0-events", "Internal event timeline tables", bind)
    _ensure_schema_migration("1.1.0-webhooks", "Disabled-by-default webhook tables", bind)
    _ensure_schema_migration("1.1.0-media", "Experimental local media catalog tables", bind)
    _ensure_schema_migration("1.1.0-setup-jobs", "Persistent setup job tables", bind)
    _ensure_schema_migration("1.1.0-backup-restore", "Backup, restore and local update job tables", bind)
    _ensure_schema_migration("1.5.0-core", "BASSWIESN 1.5.0 Local Test Build additive baseline", bind)
    _ensure_schema_migration("1.5.0-portable-normalization", "Portable devices use the same interaction policy as other SoundTouch devices", bind)
    _ensure_schema_migration("1.5.0-battery-removal", "Battery polling and battery patch APIs disabled without deleting legacy data", bind)
    _ensure_schema_migration("1.5.0-telnet-control", "Manual profile-based Telnet reboot jobs", bind)
    _ensure_schema_migration("1.5.0-standby-clock", "Manual standby-clock recovery jobs", bind)
    _ensure_schema_migration("1.5.0-health-jobs", "Health, restore, update, media, DLNA and webhook history extensions", bind)
    _ensure_schema_migration("1.5.0-final-hardware-gates", "Protected device write guard and Multiroom preserve-volume option", bind)
    _ensure_schema_migration("1.5.0-preset-source-normalization", "Normalize numeric SoundTouch source IDs in local preset ContentItems", bind)
    _ensure_schema_migration("1.5.1-setup-rebuild", "Persistent setup rebuild coordinator, device checkpoints and SSH state", bind)
    _ensure_schema_migration(
        RESEARCH_DOMAIN_MIGRATION,
        "Additive Phase 12 research state, scheduler persistence, diagnostics and recovery schema",
        bind,
    )
    _ensure_schema_migration(
        "2.0.0-write-ledger",
        "Append-only radio write ledger with request, backup, readback and rollback references",
        bind,
    )
    _ensure_schema_migration(
        "2.0.0-airplay-evidence-ttl",
        "Expire transient AirPlay readiness evidence without discarding persistent identity evidence",
        bind,
    )
    return inspect_schema(bind)


def research_domain_rollback_order() -> tuple[str, ...]:
    """Tables that an old-version rollback may remove after writers stop.

    This function is descriptive and deliberately performs no destructive SQL.
    The nullable columns added to ``device_firmware_profiles`` are safe for old
    versions to ignore and therefore are not part of the drop order.
    """

    return validate_research_domain_rollback_manifest(
        RESEARCH_DOMAIN_ROLLBACK_ORDER
    )


def validate_research_domain_rollback_manifest(
    order: tuple[str, ...],
) -> tuple[str, ...]:
    """Validate the documented, non-executing rollback table manifest."""

    if len(order) != len(set(order)):
        raise ValueError("research rollback manifest contains duplicate tables")
    missing = set(RESEARCH_DOMAIN_TABLES) - set(order)
    unknown = set(order) - set(RESEARCH_DOMAIN_TABLES)
    if missing or unknown:
        raise ValueError(
            f"research rollback manifest mismatch: missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    shared = set(order) & set(RESEARCH_DOMAIN_SHARED_ALTERATIONS)
    if shared:
        raise ValueError(
            f"shared additive tables must be retained during rollback: {sorted(shared)}"
        )
    if order.index("reporting_queue") > order.index("reporting_state"):
        raise ValueError("reporting_queue must precede reporting_state in rollback")
    return tuple(order)


def research_domain_rollback_manifest() -> dict[str, object]:
    """Return an explicitly validated, non-destructive rollback description."""

    return {
        "migration": RESEARCH_DOMAIN_MIGRATION,
        "destructive_sql_executed": False,
        "preconditions": (
            "stop 1.6 writers",
            "create and verify a database backup",
            "remove API readers before dropping tables",
        ),
        "drop_tables": research_domain_rollback_order(),
        "retained_shared_tables": RESEARCH_DOMAIN_SHARED_ALTERATIONS,
    }


def inspect_research_domain_schema(bind: Engine) -> ResearchSchemaStatus:
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    missing_tables = tuple(sorted(set(RESEARCH_DOMAIN_TABLES) - existing_tables))
    missing_columns: list[str] = []
    invalid_primary_keys: list[str] = []
    null_required_values: list[str] = []
    quote = bind.dialect.identifier_preparer.quote
    for table_name in RESEARCH_DOMAIN_TABLES:
        if table_name not in existing_tables:
            continue
        expected = {
            column.name for column in Base.metadata.tables[table_name].columns
        }
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        missing_columns.extend(
            f"{table_name}.{name}" for name in sorted(expected - actual)
        )
        primary_key = set(
            inspector.get_pk_constraint(table_name).get("constrained_columns") or ()
        )
        if primary_key != {"id"}:
            invalid_primary_keys.append(table_name)
        required = set(RESEARCH_DOMAIN_REQUIRED_COLUMNS.get(table_name, ())) & actual
        if required:
            with bind.connect() as connection:
                for column_name in sorted(required):
                    count = connection.execute(
                        text(
                            f"SELECT COUNT(*) FROM {quote(table_name)} "
                            f"WHERE {quote(column_name)} IS NULL"
                        )
                    ).scalar_one()
                    if count:
                        null_required_values.append(
                            f"{table_name}.{column_name}:{int(count)}"
                        )
    return ResearchSchemaStatus(
        missing_tables=missing_tables,
        missing_columns=tuple(missing_columns),
        invalid_primary_keys=tuple(sorted(invalid_primary_keys)),
        null_required_values=tuple(null_required_values),
    )


def _ensure_schema_migration(version: str, description: str, bind: Engine) -> None:
    if "schema_migrations" not in inspect(bind).get_table_names():
        return
    with bind.begin() as connection:
        connection.execute(
            text(
                "INSERT OR IGNORE INTO schema_migrations (version, description, applied_at) "
                "VALUES (:version, :description, CURRENT_TIMESTAMP)"
            ),
            {"version": version, "description": description},
        )


def _redact_legacy_write_ledger_secrets(bind: Engine) -> None:
    """One-time security repair for pre-2.0 ledger payload strings.

    Early ledger rows sanitised dictionary keys but could retain a sensitive
    XML element inside a generic ``xml`` value. Security redaction is the one
    intentional exception to journal immutability; the migration is recorded
    and never changes action identity, timestamps or verification results.
    """

    tables = set(inspect(bind).get_table_names())
    if not {"device_action_journal", "schema_migrations"}.issubset(tables):
        return
    migration = "2.0.0-write-ledger-secret-redaction"
    from basswiesn.app.services.support_export import redact_text

    fields = (
        "requested_state", "before_state", "result", "readback",
        "after_state", "backup_ref", "rollback_ref", "error_category",
    )
    with bind.begin() as connection:
        already = connection.execute(
            text("SELECT 1 FROM schema_migrations WHERE version = :version LIMIT 1"),
            {"version": migration},
        ).first()
        if already:
            return
        rows = connection.execute(text(
            "SELECT id, " + ", ".join(fields) + " FROM device_action_journal"
        )).mappings().all()
        for row in rows:
            values = {
                field: redact_text(str(row.get(field) or ""), anonymize_ips=False)
                for field in fields
            }
            if any(values[field] != str(row.get(field) or "") for field in fields):
                values["id"] = row["id"]
                connection.execute(text(
                    "UPDATE device_action_journal SET "
                    + ", ".join(f"{field} = :{field}" for field in fields)
                    + " WHERE id = :id"
                ), values)
        connection.execute(text(
            "INSERT OR IGNORE INTO schema_migrations (version, description, applied_at) "
            "VALUES (:version, :description, CURRENT_TIMESTAMP)"
        ), {
            "version": migration,
            "description": "Redact sensitive XML/JSON values from legacy append-only write ledger payloads",
        })


def _normalize_preset_source_aliases(bind: Engine) -> None:
    if "presets" not in inspect(bind).get_table_names():
        return
    aliases = {
        "10002": "INTERNET_RADIO",
        "10003": "LOCAL_INTERNET_RADIO",
        "10004": "TUNEIN",
        "10005": "RADIO_BROWSER",
        "10006": "WBMX",
    }
    with bind.begin() as connection:
        for old, new in aliases.items():
            connection.execute(
                text(
                    "UPDATE presets SET source = :new "
                    "WHERE upper(coalesce(source, '')) = :old"
                ),
                {"old": old, "new": new},
            )
            connection.execute(
                text(
                    "UPDATE presets SET content_item_xml = replace(content_item_xml, :old_attr, :new_attr) "
                    "WHERE content_item_xml LIKE :needle"
                ),
                {
                    "old_attr": f'source="{old}"',
                    "new_attr": f'source="{new}"',
                    "needle": f'%source="{old}"%',
                },
            )


def _ensure_device_state_columns(bind: Engine) -> None:
    inspector = inspect(bind)
    if "devices" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("devices")}
    columns = {
        "reachable": "BOOLEAN DEFAULT 1",
        "last_failed_at": "DATETIME DEFAULT NULL",
        "failure_count": "INTEGER DEFAULT 0",
        "offline_reason": "TEXT DEFAULT ''",
    }
    with bind.begin() as connection:
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE devices ADD COLUMN {name} {ddl}"))


def _ensure_station_stream_columns(bind: Engine) -> None:
    inspector = inspect(bind)
    if "stations" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("stations")}
    columns = {
        "stream_url_original": "TEXT DEFAULT ''",
        "stream_url_resolved": "TEXT DEFAULT ''",
        "stream_format": "TEXT DEFAULT ''",
        "stream_mime": "TEXT DEFAULT ''",
        "stream_codec": "TEXT DEFAULT ''",
        "compatibility_score": "INTEGER DEFAULT 0",
        "compatibility_warning": "TEXT DEFAULT ''",
        "is_hls": "INTEGER DEFAULT 0",
        "is_direct_audio": "INTEGER DEFAULT 0",
        "internal": "BOOLEAN DEFAULT 0",
        "purpose": "TEXT DEFAULT ''",
        "lab_only": "BOOLEAN DEFAULT 0",
    }
    with bind.begin() as connection:
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE stations ADD COLUMN {name} {ddl}"))


def _ensure_scheduled_action_columns(bind: Engine) -> None:
    inspector = inspect(bind)
    if "scheduled_actions" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("scheduled_actions")}
    columns = {
        "preset_button": "INTEGER DEFAULT NULL",
        "stop_action": "TEXT DEFAULT 'stop_standby'",
    }
    with bind.begin() as connection:
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE scheduled_actions ADD COLUMN {name} {ddl}"))


def _ensure_play_history_columns(bind: Engine) -> None:
    inspector = inspect(bind)
    if "play_history" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("play_history")}
    columns = {
        "device_ip": "TEXT DEFAULT ''",
        "source_type": "TEXT DEFAULT 'LOCAL_INTERNET_RADIO'",
        "trigger_type": "TEXT DEFAULT 'manual'",
        "preset_button": "INTEGER DEFAULT NULL",
        "preset_name": "TEXT DEFAULT ''",
        "volume": "INTEGER DEFAULT NULL",
        "success": "INTEGER DEFAULT 1",
        "error_message": "TEXT DEFAULT ''",
        "internal_event": "BOOLEAN DEFAULT 0",
        "last_confirmed_playing_at": "DATETIME DEFAULT NULL",
        "end_reason": "TEXT DEFAULT ''",
    }
    with bind.begin() as connection:
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE play_history ADD COLUMN {name} {ddl}"))


def _ensure_maintenance_columns(bind: Engine) -> None:
    inspector = inspect(bind)
    if "devices" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("devices")}
    columns = {
        "maintenance_reboot_enabled": "BOOLEAN DEFAULT 0",
        "maintenance_reboot_interval_hours": "INTEGER DEFAULT 24",
        "maintenance_last_success_at": "DATETIME DEFAULT NULL",
        "maintenance_next_run_at": "DATETIME DEFAULT NULL",
        "maintenance_last_attempt_at": "DATETIME DEFAULT NULL",
        "maintenance_last_result": "TEXT DEFAULT ''",
        "maintenance_phase": "TEXT DEFAULT 'idle'",
        "maintenance_failure_count": "INTEGER DEFAULT 0",
    }
    with bind.begin() as connection:
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE devices ADD COLUMN {name} {ddl}"))


def _ensure_policy_columns(bind: Engine) -> None:
    inspector = inspect(bind)
    if "devices" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("devices")}
    columns = {
        "device_class_override": "TEXT DEFAULT 'auto'",
        "safe_mode": "TEXT DEFAULT 'auto'",
        "polling_profile_override": "TEXT DEFAULT 'auto'",
        "auto_restore_allowed": "BOOLEAN DEFAULT 1",
        "battery_poll_allowed": "BOOLEAN DEFAULT 1",
        "maintenance_actions_allowed": "BOOLEAN DEFAULT 0",
    }
    with bind.begin() as connection:
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE devices ADD COLUMN {name} {ddl}"))


def _ensure_discovery_columns(bind: Engine) -> None:
    inspector = inspect(bind)
    if "devices" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("devices")}
    columns = {
        "discovery_method": "TEXT DEFAULT 'unknown'",
        "discovery_confidence": "INTEGER DEFAULT 0",
        "discovery_last_seen": "DATETIME DEFAULT NULL",
        "discovery_location": "TEXT DEFAULT ''",
        "discovered_interface": "TEXT DEFAULT ''",
        "descriptor_url": "TEXT DEFAULT ''",
        "descriptor_validated": "BOOLEAN DEFAULT 0",
        "identity_verified": "BOOLEAN DEFAULT 0",
    }
    with bind.begin() as connection:
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE devices ADD COLUMN {name} {ddl}"))


def _ensure_play_history_identity_columns(bind: Engine) -> None:
    inspector = inspect(bind)
    if "play_history" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("play_history")}
    columns = {
        "station_display_name": "TEXT DEFAULT ''",
        "station_name_normalized": "TEXT DEFAULT ''",
        "source_account": "TEXT DEFAULT ''",
        "content_item_name": "TEXT DEFAULT ''",
        "canonical_stream_id": "TEXT DEFAULT ''",
        "identity_source": "TEXT DEFAULT ''",
        "identity_confidence": "INTEGER DEFAULT 0",
        "source_display_name": "TEXT DEFAULT ''",
        "stream_host": "TEXT DEFAULT ''",
        "is_internal": "BOOLEAN DEFAULT 0",
        "is_confirmed": "BOOLEAN DEFAULT 1",
        "confirmed_duration_seconds": "INTEGER DEFAULT 0",
    }
    with bind.begin() as connection:
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE play_history ADD COLUMN {name} {ddl}"))


def _ensure_multiroom_scenario_columns(bind: Engine) -> None:
    inspector = inspect(bind)
    if "multiroom_scenarios" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("multiroom_scenarios")}
    columns = {
        "preserve_volumes": "BOOLEAN DEFAULT 0",
    }
    with bind.begin() as connection:
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE multiroom_scenarios ADD COLUMN {name} {ddl}"))


def _ensure_firmware_profile_columns(bind: Engine) -> None:
    """Extend the existing firmware profile rather than creating a duplicate."""

    inspector = inspect(bind)
    if "device_firmware_profiles" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("device_firmware_profiles")}
    columns = {
        "version": "TEXT DEFAULT NULL",
        "build": "TEXT DEFAULT NULL",
        "product_id": "TEXT DEFAULT NULL",
        "variant": "TEXT DEFAULT NULL",
        "model": "TEXT DEFAULT NULL",
        "airplay_capability": "TEXT DEFAULT NULL",
        "auth_hardware_expected": "BOOLEAN DEFAULT NULL",
        "metadata_capability": "TEXT DEFAULT NULL",
        "artwork_capability": "TEXT DEFAULT NULL",
        "multiroom_capability": "TEXT DEFAULT NULL",
        "observed_at": "DATETIME DEFAULT NULL",
    }
    with bind.begin() as connection:
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE device_firmware_profiles ADD COLUMN {name} {ddl}"))


def _ensure_write_ledger_columns(bind: Engine) -> None:
    inspector = inspect(bind)
    if "device_action_journal" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("device_action_journal")}
    columns = {
        "requested_state": "TEXT DEFAULT '{}'",
        "backup_ref": "TEXT DEFAULT ''",
        "readback": "TEXT DEFAULT '{}'",
        "rollback_ref": "TEXT DEFAULT ''",
    }
    with bind.begin() as connection:
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE device_action_journal ADD COLUMN {name} {ddl}"))


def _ensure_research_domain_columns(bind: Engine) -> None:
    """Repair safely additive columns in interrupted/partial 1.6 migrations.

    SQLite cannot safely retrofit a primary key.  Such a malformed table is
    rejected with a precise error instead of recording a successful migration
    marker.  All other missing columns are introduced nullable so existing
    rows are preserved; application-level defaults apply to subsequent rows.
    """

    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    quote = bind.dialect.identifier_preparer.quote
    for table_name in RESEARCH_DOMAIN_TABLES:
        if table_name not in existing_tables:
            continue
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        primary_key = set(
            inspector.get_pk_constraint(table_name).get("constrained_columns") or ()
        )
        if "id" not in actual or primary_key != {"id"}:
            raise RuntimeError(
                f"cannot safely repair {table_name}: expected primary key (id)"
            )
        model_table = Base.metadata.tables[table_name]
        missing = [column for column in model_table.columns if column.name not in actual]
        if not missing:
            continue
        with bind.begin() as connection:
            for column in missing:
                type_sql = column.type.compile(dialect=bind.dialect)
                connection.execute(
                    text(
                        f"ALTER TABLE {quote(table_name)} ADD COLUMN "
                        f"{quote(column.name)} {type_sql}"
                    )
                )
                default = column.default
                if default is not None and default.is_scalar:
                    connection.execute(
                        text(
                            f"UPDATE {quote(table_name)} "
                            f"SET {quote(column.name)} = :default_value "
                            f"WHERE {quote(column.name)} IS NULL"
                        ),
                        {"default_value": default.arg},
                    )
                elif not column.nullable and "DATE" in type_sql.upper():
                    connection.execute(
                        text(
                            f"UPDATE {quote(table_name)} "
                            f"SET {quote(column.name)} = CURRENT_TIMESTAMP "
                            f"WHERE {quote(column.name)} IS NULL"
                        )
                    )
        # Refresh dialect inspection before considering the next table.
        inspector = inspect(bind)


def _ensure_160_unique_indexes(bind: Engine) -> None:
    """Restore uniqueness lost when a pre-existing partial table was reused."""

    unique_keys: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
        "device_capability_snapshots": (
            ("uq160_device_capability_snapshot", ("device_id",)),
        ),
        "provider_health_state": (
            ("uq160_provider_health_state", ("device_id", "provider_id")),
        ),
        "provider_lease_state": (
            ("uq160_provider_lease_state", ("device_id", "provider_id")),
        ),
        "reporting_state": (
            ("uq160_reporting_state", ("device_id", "provider_id")),
        ),
        "reporting_queue": (
            ("uq160_reporting_queue_item", ("item_id",)),
            (
                "uq160_reporting_queue_slot",
                ("device_id", "provider_id", "generation", "queue_slot"),
            ),
        ),
        "metadata_state": (("uq160_metadata_state", ("device_id",)),),
        "playback_state": (("uq160_playback_state", ("device_id",)),),
        "playback_health_state": (
            ("uq160_playback_health_state", ("device_id",)),
        ),
        "restriction_state": (
            ("uq160_restriction_state", ("device_id", "source_key")),
        ),
        "airplay_readiness_state": (
            ("uq160_airplay_readiness_state", ("device_id",)),
        ),
        "diagnostic_events": (("uq160_diagnostic_event", ("event_id",)),),
        "recovery_operations": (
            ("uq160_recovery_operation", ("operation_id",)),
        ),
        "artwork_cache": (("uq160_artwork_cache_key", ("cache_key",)),),
    }
    tables = set(inspect(bind).get_table_names())
    quote = bind.dialect.identifier_preparer.quote
    for table_name, definitions in unique_keys.items():
        if table_name not in tables:
            continue
        inspector = inspect(bind)
        present = {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(table_name)
        }
        present.update(
            tuple(item.get("column_names") or ())
            for item in inspector.get_indexes(table_name)
            if item.get("unique")
        )
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        for index_name, key_columns in definitions:
            if key_columns in present:
                continue
            if not set(key_columns).issubset(columns):
                raise RuntimeError(
                    f"cannot create {index_name}: columns missing from {table_name}"
                )
            column_sql = ", ".join(quote(name) for name in key_columns)
            try:
                with bind.begin() as connection:
                    connection.execute(
                        text(
                            f"CREATE UNIQUE INDEX IF NOT EXISTS {quote(index_name)} "
                            f"ON {quote(table_name)} ({column_sql})"
                        )
                    )
            except SQLAlchemyError as exc:
                raise RuntimeError(
                    f"cannot safely enforce {index_name}; duplicate partial data exists"
                ) from exc


def _ensure_indexes(bind: Engine) -> None:
    tables = set(inspect(bind).get_table_names())
    with bind.begin() as connection:
        if "play_history" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_play_history_identity ON play_history(station_display_name)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_play_history_device_started ON play_history(device_id, started_at)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_play_history_stream_host ON play_history(stream_host)"))
        if "runtime_state" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_runtime_state_updated ON runtime_state(updated_at)"))


def _ensure_full_test_indexes(bind: Engine) -> None:
    tables = set(inspect(bind).get_table_names())
    with bind.begin() as connection:
        if "devices" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_devices_discovery ON devices(discovery_method, discovery_last_seen)"))
        if "discovery_events" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_discovery_events_device_ts ON discovery_events(device_id, ts)"))
        if "device_interactions" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_device_interactions_device_started ON device_interactions(device_id, started_at)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_device_interactions_correlation ON device_interactions(correlation_id)"))
        if "events" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(event_type, timestamp)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_events_device_ts ON events(device_id, timestamp)"))
        if "webhook_deliveries" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_endpoint_created ON webhook_deliveries(endpoint_id, created_at)"))
        if "media_items" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_media_items_root_path ON media_items(root_id, relative_path)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_media_items_title ON media_items(title)"))
        if "setup_jobs" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_setup_jobs_status_updated ON setup_jobs(status, updated_at)"))
        if "healthcheck_results" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_healthcheck_results_run_status ON healthcheck_results(run_id, status)"))


def _ensure_15_indexes(bind: Engine) -> None:
    tables = set(inspect(bind).get_table_names())
    with bind.begin() as connection:
        if "telnet_jobs" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_telnet_jobs_device_status ON telnet_jobs(device_id, status)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_telnet_jobs_updated ON telnet_jobs(updated_at)"))
        if "standby_clock_jobs" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_standby_clock_jobs_device_status ON standby_clock_jobs(device_id, status)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_standby_clock_jobs_updated ON standby_clock_jobs(updated_at)"))
        if "device_capability_evidence" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_device_capability_evidence_device ON device_capability_evidence(device_id, observed_at)"))
        if "webhook_delivery_queue" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_webhook_delivery_queue_due ON webhook_delivery_queue(status, next_attempt_at)"))
        if "restore_execution_jobs" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_restore_execution_jobs_status ON restore_execution_jobs(status, created_at)"))
        if "update_execution_jobs" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_update_execution_jobs_status ON update_execution_jobs(status, created_at)"))
        if "media_scan_jobs" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_media_scan_jobs_status ON media_scan_jobs(status, started_at)"))
        if "dlna_renderers" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_dlna_renderers_status ON dlna_renderers(status, last_seen_at)"))
        if "health_status_history" in tables:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_health_status_history_observed ON health_status_history(observed_at)"))


def _ensure_160_indexes(bind: Engine) -> None:
    """Create query-path indexes idempotently for upgraded/partial databases."""

    tables = set(inspect(bind).get_table_names())
    statements = {
        "device_firmware_profiles": (
            "CREATE INDEX IF NOT EXISTS idx_device_firmware_profiles_identity "
            "ON device_firmware_profiles(version, build, product_id, variant)",
        ),
        "device_capability_snapshots": (
            "CREATE INDEX IF NOT EXISTS idx_device_capability_snapshots_observed "
            "ON device_capability_snapshots(observed_at)",
        ),
        "provider_health_state": (
            "CREATE INDEX IF NOT EXISTS idx_provider_health_provider "
            "ON provider_health_state(provider_id)",
        ),
        "provider_lease_state": (
            "CREATE INDEX IF NOT EXISTS idx_provider_lease_report_due "
            "ON provider_lease_state(report_due_at)",
            "CREATE INDEX IF NOT EXISTS idx_provider_lease_metadata_due "
            "ON provider_lease_state(metadata_due_at)",
        ),
        "reporting_state": (
            "CREATE INDEX IF NOT EXISTS idx_reporting_state_next_due "
            "ON reporting_state(next_due_at)",
        ),
        "metadata_state": (
            "CREATE INDEX IF NOT EXISTS idx_metadata_state_stale_updated "
            "ON metadata_state(stale, updated_at)",
        ),
        "restriction_state": (
            "CREATE INDEX IF NOT EXISTS idx_restriction_state_device_source "
            "ON restriction_state(device_id, source_key)",
        ),
        "airplay_readiness_state": (
            "CREATE INDEX IF NOT EXISTS idx_airplay_readiness_expiry "
            "ON airplay_readiness_state(expires_at)",
        ),
        "diagnostic_events": (
            "CREATE INDEX IF NOT EXISTS idx_diagnostic_events_retention "
            "ON diagnostic_events(occurred_at)",
        ),
        "recovery_operations": (
            "CREATE INDEX IF NOT EXISTS idx_recovery_operations_retention "
            "ON recovery_operations(completed_at)",
        ),
    }
    with bind.begin() as connection:
        for table_name, table_statements in statements.items():
            if table_name not in tables:
                continue
            columns = {column["name"] for column in inspect(bind).get_columns(table_name)}
            for statement in table_statements:
                # A partial external schema is reported, not destructively repaired.
                referenced = statement.rsplit("(", 1)[-1].rstrip(")").replace(" ", "").split(",")
                if set(referenced).issubset(columns):
                    connection.execute(text(statement))


def _seed_device_model_definitions(bind: Engine) -> None:
    if "device_model_definitions" not in inspect(bind).get_table_names():
        return
    rows = [
        ("soundtouch_portable", "SoundTouch Portable", "SoundTouch Portable", "portable", "idle_online", ["soundtouch portable", "portable", "taigan"], {
            "battery_supported": False, "display_supported": True, "wifi_supported": True, "preset_buttons_supported": True,
            "preset_write_supported": True, "multiroom_supported": True, "power_key_supported": True,
            "standby_wakeup_supported": True, "source_query_supported": True, "service_availability_supported": True,
            "now_playing_supported": True, "volume_supported": True, "zone_supported": True,
            "safe_auto_power": True, "safe_auto_preset_recovery": True, "safe_background_polling": True,
            "telnet_reboot_supported": None, "standby_clock_recovery_supported": None,
            "battery_feature_removed": True,
        }, "Portable models are recognized, but BASSWIESN 1.5.0 no longer applies Portable-only polling, power or battery restrictions."),
        ("soundtouch_10", "SoundTouch 10", "SoundTouch 10", "stationary", "idle_online", ["soundtouch 10", "st10", "ginger"], {
            "battery_supported": False, "wifi_supported": True, "aux_supported": True, "bluetooth_supported": True,
            "preset_buttons_supported": True, "preset_write_supported": True, "multiroom_supported": True,
            "power_key_supported": True, "standby_wakeup_supported": True, "source_query_supported": True,
            "service_availability_supported": True, "now_playing_supported": True, "volume_supported": True, "zone_supported": True,
            "safe_auto_power": True, "safe_auto_preset_recovery": True, "safe_background_polling": True,
        }, ""),
        ("soundtouch_20", "SoundTouch 20", "SoundTouch 20", "stationary", "idle_online", ["soundtouch 20", "st20", "rhino"], {
            "battery_supported": False, "display_supported": True, "wifi_supported": True, "ethernet_supported": True,
            "preset_buttons_supported": True, "preset_write_supported": True, "multiroom_supported": True,
            "power_key_supported": True, "standby_wakeup_supported": True, "source_query_supported": True,
            "service_availability_supported": True, "now_playing_supported": True, "volume_supported": True, "zone_supported": True,
            "safe_auto_power": True, "safe_auto_preset_recovery": True, "safe_background_polling": True,
        }, ""),
        ("soundtouch_30", "SoundTouch 30", "SoundTouch 30", "stationary", "idle_online", ["soundtouch 30", "st30", "mojo"], {
            "battery_supported": False, "display_supported": True, "wifi_supported": True, "ethernet_supported": True,
            "preset_buttons_supported": True, "preset_write_supported": True, "multiroom_supported": True,
            "power_key_supported": True, "standby_wakeup_supported": True, "source_query_supported": True,
            "service_availability_supported": True, "now_playing_supported": True, "volume_supported": True, "zone_supported": True,
            "safe_auto_power": True, "safe_auto_preset_recovery": True, "safe_background_polling": True,
        }, ""),
        ("soundtouch_300", "SoundTouch 300", "SoundTouch 300", "stationary", "idle_online", ["soundtouch 300", "st300"], {
            "battery_supported": False, "wifi_supported": True, "ethernet_supported": True, "hdmi_supported": True,
            "preset_buttons_supported": True, "preset_write_supported": True, "multiroom_supported": True,
            "power_key_supported": True, "standby_wakeup_supported": True, "source_query_supported": True,
            "service_availability_supported": True, "now_playing_supported": True, "volume_supported": True, "zone_supported": True,
            "safe_auto_power": True, "safe_auto_preset_recovery": True, "safe_background_polling": True,
        }, "Soundbar features vary by firmware; expose writes only after read-back."),
        ("wave_soundtouch", "Wave SoundTouch", "Wave SoundTouch", "stationary", "idle_online", ["wave soundtouch", "wave"], {
            "battery_supported": False, "display_supported": True, "wifi_supported": True, "preset_buttons_supported": True,
            "preset_write_supported": True, "multiroom_supported": True, "power_key_supported": True,
            "standby_wakeup_supported": True, "source_query_supported": True, "service_availability_supported": True,
            "now_playing_supported": True, "volume_supported": True, "zone_supported": True, "safe_auto_power": True,
            "safe_auto_preset_recovery": True, "safe_background_polling": True,
            "telnet_reboot_supported": None, "standby_clock_recovery_supported": True, "battery_feature_removed": True,
        }, ""),
        ("wireless_link", "SoundTouch Wireless Link", "SoundTouch Wireless Link", "stationary", "idle_online", ["wireless link", "soundtouch wireless link"], {
            "battery_supported": False, "display_supported": False, "wifi_supported": True, "ethernet_supported": True,
            "aux_supported": True, "bluetooth_supported": True, "preset_buttons_supported": True, "preset_write_supported": True,
            "multiroom_supported": True, "power_key_supported": True, "standby_wakeup_supported": True,
            "source_query_supported": True, "service_availability_supported": True, "now_playing_supported": True,
            "volume_supported": True, "zone_supported": True, "safe_auto_power": True, "safe_auto_preset_recovery": True,
            "safe_background_polling": True,
        }, ""),
        ("sa4", "SA-4", "SA-4", "stationary", "idle_online", ["sa-4", "sa4"], {
            "battery_supported": False, "wifi_supported": True, "preset_buttons_supported": True, "preset_write_supported": True,
            "multiroom_supported": True, "power_key_supported": True, "standby_wakeup_supported": True,
            "source_query_supported": True, "service_availability_supported": True, "now_playing_supported": True,
            "volume_supported": True, "zone_supported": True, "safe_auto_power": True, "safe_auto_preset_recovery": True,
            "safe_background_polling": True,
        }, ""),
        ("sa5", "SA-5", "SA-5", "stationary", "idle_online", ["sa-5", "sa5"], {
            "battery_supported": False, "wifi_supported": True, "ethernet_supported": True, "preset_buttons_supported": True,
            "preset_write_supported": True, "multiroom_supported": True, "power_key_supported": True, "standby_wakeup_supported": True,
            "source_query_supported": True, "service_availability_supported": True, "now_playing_supported": True,
            "volume_supported": True, "zone_supported": True, "safe_auto_power": True, "safe_auto_preset_recovery": True,
            "safe_background_polling": True,
        }, ""),
        ("lifestyle_soundtouch", "Lifestyle mit SoundTouch", "Lifestyle SoundTouch", "stationary", "idle_online", ["lifestyle", "cinemate"], {
            "battery_supported": False, "wifi_supported": True, "ethernet_supported": True, "multiroom_supported": True,
            "power_key_supported": True, "standby_wakeup_supported": True, "source_query_supported": True,
            "service_availability_supported": True, "now_playing_supported": True, "volume_supported": True,
            "zone_supported": True, "safe_auto_power": True, "safe_auto_preset_recovery": True, "safe_background_polling": True,
        }, "Home-theater capabilities are firmware dependent and require read-back."),
        ("unknown_soundtouch", "Unbekanntes SoundTouch-Gerät", "unknown", "unknown", "standby", ["soundtouch"], {
            "battery_supported": None, "display_supported": None, "wifi_supported": None, "preset_buttons_supported": None,
            "preset_write_supported": None, "multiroom_supported": None, "power_key_supported": False,
            "standby_wakeup_supported": False, "source_query_supported": None, "service_availability_supported": None,
            "now_playing_supported": None, "volume_supported": None, "zone_supported": None, "safe_auto_power": False,
            "safe_auto_preset_recovery": False, "safe_background_polling": False,
            "telnet_reboot_supported": False, "standby_clock_recovery_supported": False,
            "battery_feature_removed": True,
        }, "Unknown models use safe defaults until capabilities are confirmed or overridden."),
    ]
    with bind.begin() as connection:
        for key, product, family, device_class, profile, aliases, capabilities, limitations in rows:
            connection.execute(
                text(
                    "INSERT OR IGNORE INTO device_model_definitions "
                    "(model_key, product_name, model_family, generation, device_class, aliases_json, capabilities_json, "
                    " firmware_notes, known_limitations, recommended_polling_profile, created_at, updated_at) "
                    "VALUES (:key, :product, :family, '', :device_class, :aliases, :capabilities, '', :limitations, :profile, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "key": key,
                    "product": product,
                    "family": family,
                    "device_class": device_class,
                    "aliases": json.dumps(aliases),
                    "capabilities": json.dumps(capabilities),
                    "limitations": limitations,
                    "profile": profile,
                },
            )
            connection.execute(
                text(
                    "UPDATE device_model_definitions "
                    "SET product_name = :product, model_family = :family, device_class = :device_class, "
                    "aliases_json = :aliases, capabilities_json = :capabilities, known_limitations = :limitations, "
                    "recommended_polling_profile = :profile, updated_at = CURRENT_TIMESTAMP "
                    "WHERE model_key = :key"
                ),
                {
                    "key": key,
                    "product": product,
                    "family": family,
                    "device_class": device_class,
                    "aliases": json.dumps(aliases),
                    "capabilities": json.dumps(capabilities),
                    "limitations": limitations,
                    "profile": profile,
                },
            )


def _seed_telnet_device_profiles(bind: Engine) -> None:
    if "telnet_device_profiles" not in inspect(bind).get_table_names():
        return
    rows = [
        {
            "profile_key": "unknown_unsupported",
            "model_family": "unknown",
            "firmware_family": "",
            "command_port": 23,
            "telnet_reachable": False,
            "telnet_reboot_supported": False,
            "standby_clock_recovery_supported": False,
            "reboot_command_key": "",
            "standby_clock_command_key": "",
            "commands": {},
            "evidence": "Safe fallback for unknown SoundTouch models and firmware.",
            "limitations": "No command is sent until a model/firmware profile is verified.",
        },
        {
            "profile_key": "wave_soundtouch_iv_fw27_cli17000",
            "model_family": "Wave SoundTouch",
            "firmware_family": "27.0.x",
            "command_port": 17000,
            "telnet_reachable": True,
            "telnet_reboot_supported": True,
            "standby_clock_recovery_supported": False,
            "reboot_command_key": "sys_reboot",
            "standby_clock_command_key": "",
            "commands": {"sys_reboot": "sys reboot"},
            "evidence": "Local SoundTouch firmware notes document CLI 17000 `sys reboot` with post-reboot verification.",
            "limitations": "Only auto-selected for Wave SoundTouch devices on 27.0.x-like firmware text; all other firmware remains unsupported until verified.",
        },
    ]
    with bind.begin() as connection:
        for row in rows:
            params = {
                **row,
                "commands_json": json.dumps(row["commands"], ensure_ascii=False),
            }
            connection.execute(
                text(
                    "INSERT OR IGNORE INTO telnet_device_profiles "
                    "(profile_key, model_family, firmware_family, command_port, telnet_reachable, "
                    "telnet_reboot_supported, standby_clock_recovery_supported, reboot_command_key, "
                    "standby_clock_command_key, commands_json, evidence, limitations, created_at, updated_at) "
                    "VALUES (:profile_key, :model_family, :firmware_family, :command_port, :telnet_reachable, "
                    ":telnet_reboot_supported, :standby_clock_recovery_supported, :reboot_command_key, "
                    ":standby_clock_command_key, :commands_json, :evidence, :limitations, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                params,
            )
            connection.execute(
                text(
                    "UPDATE telnet_device_profiles "
                    "SET model_family = :model_family, firmware_family = :firmware_family, command_port = :command_port, "
                    "telnet_reachable = :telnet_reachable, telnet_reboot_supported = :telnet_reboot_supported, "
                    "standby_clock_recovery_supported = :standby_clock_recovery_supported, reboot_command_key = :reboot_command_key, "
                    "standby_clock_command_key = :standby_clock_command_key, commands_json = :commands_json, evidence = :evidence, "
                    "limitations = :limitations, updated_at = CURRENT_TIMESTAMP "
                    "WHERE profile_key = :profile_key"
                ),
                params,
            )


def _backfill_play_history_identity(bind: Engine) -> None:
    tables = set(inspect(bind).get_table_names())
    if not {"play_history", "stations"}.issubset(tables):
        return
    with bind.begin() as connection:
        connection.execute(text(
            "UPDATE play_history "
            "SET station_display_name = station_name, "
            "    station_name_normalized = lower(trim(station_name)), "
            "    identity_source = CASE WHEN identity_source = '' THEN 'snapshot' ELSE identity_source END, "
            "    identity_confidence = CASE WHEN identity_confidence = 0 THEN 80 ELSE identity_confidence END "
            "WHERE station_display_name = '' AND station_name IS NOT NULL AND trim(station_name) != ''"
        ))
        connection.execute(text(
            "UPDATE play_history "
            "SET station_display_name = (SELECT stations.name FROM stations WHERE stations.id = play_history.station_id), "
            "    station_name = CASE WHEN station_name = '' THEN (SELECT stations.name FROM stations WHERE stations.id = play_history.station_id) ELSE station_name END, "
            "    station_name_normalized = lower(trim((SELECT stations.name FROM stations WHERE stations.id = play_history.station_id))), "
            "    identity_source = 'station', "
            "    identity_confidence = CASE WHEN identity_confidence < 90 THEN 95 ELSE identity_confidence END "
            "WHERE station_display_name = '' "
            "  AND station_id IS NOT NULL "
            "  AND EXISTS (SELECT 1 FROM stations WHERE stations.id = play_history.station_id AND trim(stations.name) != '')"
        ))
        connection.execute(text(
            "UPDATE play_history "
            "SET is_internal = 1 "
            "WHERE internal_event = 1 OR lower(trigger_type) IN ('keepalive_internal', 'setup_activation') "
            "   OR lower(source_type) IN ('keepalive_internal', 'setup_activation', 'background_probe')"
        ))


def _classify_internal_stations(bind: Engine) -> None:
    """Narrow, repeatable normalization of setup-owned activation records."""
    if "stations" not in inspect(bind).get_table_names():
        return
    with bind.begin() as connection:
        connection.execute(text(
            "UPDATE stations SET internal = 1, lab_only = 1, purpose = 'activation' "
            "WHERE name IN ('BASSWIESN Activation AAC', 'BASSWIESN Activation HLS Fallback', "
            "'BASSWIESN Activation MP3 128', 'BASSWIESN Activation MP3 Backup', "
            "'BASSWIESN Activation OGG', 'BASSWIESN Activation User Station Required')"
        ))
