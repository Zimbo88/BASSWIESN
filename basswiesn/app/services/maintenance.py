from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import ConfigBackup, DeviceActionJournal, PresetMutation, RequestLog, TelemetryEvent

# Only an exact terminal verified contract can release transaction evidence.
# Survey completion is not established, so no terminal phase is authorized.
TRANSACTION_BACKUPS = {
    "http8090:station-start": ("station_playback", "STATION_COMPLETED"),
    "http8090:key-start": ("key_command", "KEY_COMPLETED"),
    "http8090:power-start": ("power_action", "POWER_COMPLETED"),
    "http8090:wireless-survey-start": ("wireless_survey", None),
    "http8090:native_station-start": ("native_station", None),
    "http8090:wireless_profile-start": ("wireless_profile", None),
}


def storage_summary(db: Session) -> dict[str, Any]:
    settings = get_settings()
    db_path = str(settings.database_url).replace("sqlite:///", "", 1)
    masterlog = settings.data_dir / "logs" / "master.log"
    try:
        db_size = (settings.data_dir / "basswiesn.db").stat().st_size if db_path.endswith("basswiesn.db") else 0
    except OSError:
        db_size = 0
    try:
        master_size = masterlog.stat().st_size
    except OSError:
        master_size = 0
    return {
        "db_size_mb": round(db_size / 1024 / 1024, 2),
        "masterlog_size_mb": round(master_size / 1024 / 1024, 2),
        "request_log_count": db.query(RequestLog).count(),
        "telemetry_count": db.query(TelemetryEvent).count(),
        "config_backup_count": db.query(ConfigBackup).count(),
        "cleanup_available": True,
        "retention": {
            "request_log_days": settings.request_log_retention_days,
            "telemetry_days": settings.telemetry_retention_days,
            "config_backup_count": settings.config_backup_retention_count,
            "key_backup_count": settings.config_backup_retention_count,
            "unresolved_key_backups": "retained for explicit reconciliation",
            "masterlog_max_mb": settings.masterlog_max_mb,
        },
    }


def _completed_transaction_references(db: Session) -> set[tuple[str, str, str]]:
    """Only a final successful ledger state can release transaction retention.

    Read the latest record per reference, not any historical success. An absent,
    interrupted or contradictory result must preserve recovery evidence.
    """
    latest = (db.query(func.max(DeviceActionJournal.id).label("id"))
              .filter(DeviceActionJournal.action.in_([entry[0] for entry in TRANSACTION_BACKUPS.values()]))
              .group_by(DeviceActionJournal.backup_ref).subquery())
    rows = db.query(DeviceActionJournal).join(latest, DeviceActionJournal.id == latest.c.id).all()
    phases = dict(TRANSACTION_BACKUPS.values())
    return {(row.device_id, row.backup_ref, row.action) for row in rows
            if phases.get(row.action) is not None and row.phase == phases[row.action]
            and row.verified and row.result == "VERIFIED"}


def cleanup_plan(db: Session) -> dict[str, Any]:
    settings = get_settings()
    now = datetime.now(UTC)
    request_cutoff = now - timedelta(days=settings.request_log_retention_days)
    telemetry_cutoff = now - timedelta(days=settings.telemetry_retention_days)
    old_requests = db.query(RequestLog).filter(RequestLog.ts < request_cutoff).count()
    old_telemetry = db.query(TelemetryEvent).filter(TelemetryEvent.ts < telemetry_cutoff).count()
    backup_ids: list[int] = []
    grouped: dict[tuple[str, str], list[int]] = {}
    completed = _completed_transaction_references(db)
    retained_keys = 0
    retained_transactions = 0
    mutations = db.query(PresetMutation).all()
    unresolved = [row for row in mutations if row.state not in {"LOCAL_COMMIT", "ROLLBACK"}]
    pending_refs = {(row.device_id, row.backup_ref) for row in unresolved if row.backup_ref}
    terminal_intents = {(row.device_id, f"preset-intent/{row.mutation_id}.json") for row in mutations
                        if row.state in {"LOCAL_COMMIT", "ROLLBACK"}}
    for row in db.query(ConfigBackup).order_by(ConfigBackup.device_id, ConfigBackup.created_at.desc(), ConfigBackup.id.desc()).all():
        # A count quota must not destroy the only evidence for an uncertain write
        # or the original intent needed to distinguish it from today's catalog.
        if (row.path.startswith("live_radio_reconnect/") or
                (row.device_id, row.path) in pending_refs or
                row.path.startswith("preset-intent/") and (row.device_id, row.path) not in terminal_intents):
            retained_transactions += 1
            continue
        contract = TRANSACTION_BACKUPS.get(row.path)
        if contract is not None:
            reference = f"config-backup:{row.id}:sha256:{sha256(row.content.encode()).hexdigest()}"
            if (row.device_id, reference, contract[0]) not in completed:
                retained_transactions += 1
                retained_keys += int(row.path == "http8090:key-start")
                continue
        # Routine remote-button evidence must never consume the quota for
        # persistent settings/network/rebuild backups. No radio is contacted.
        group = (row.device_id or "unknown", contract[0] if contract else "configuration")
        grouped.setdefault(group, []).append(row.id)
    for ids in grouped.values():
        backup_ids.extend(ids[settings.config_backup_retention_count:])
    return {
        "request_logs": old_requests,
        "telemetry_events": old_telemetry,
        "config_backups": len(backup_ids),
        "config_backup_ids": backup_ids,
        "key_backups_retained_for_reconciliation": retained_keys,
        "transaction_backups_retained_for_reconciliation": retained_transactions,
        "estimated_mb": 0,
        "dry_run": True,
    }


def run_cleanup(db: Session) -> dict[str, Any]:
    # Freeze the retention decision and deletions together. Otherwise a backup
    # selected for cleanup could become an active START between SELECT/DELETE.
    # No radio/network I/O occurs in this short SQLite writer reservation.
    if db.new or db.dirty or db.deleted:
        raise ValueError("cleanup requires a clean database session")
    db.rollback()
    if db.get_bind().dialect.name != "sqlite":
        raise ValueError("cleanup isolation not established for this database")
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    settings = get_settings()
    now = datetime.now(UTC)
    request_cutoff = now - timedelta(days=settings.request_log_retention_days)
    telemetry_cutoff = now - timedelta(days=settings.telemetry_retention_days)
    plan = cleanup_plan(db)
    request_logs = db.query(RequestLog).filter(RequestLog.ts < request_cutoff).delete(synchronize_session=False)
    telemetry_events = db.query(TelemetryEvent).filter(TelemetryEvent.ts < telemetry_cutoff).delete(synchronize_session=False)
    config_backups = 0
    if plan["config_backup_ids"]:
        config_backups = db.query(ConfigBackup).filter(ConfigBackup.id.in_(plan["config_backup_ids"])).delete(synchronize_session=False)
    db.commit()
    return {
        "dry_run": False,
        "request_logs": request_logs,
        "telemetry_events": telemetry_events,
        "config_backups": config_backups,
        "key_backups_retained_for_reconciliation": plan["key_backups_retained_for_reconciliation"],
        "transaction_backups_retained_for_reconciliation": plan["transaction_backups_retained_for_reconciliation"],
        "current_data_preserved": True,
    }
