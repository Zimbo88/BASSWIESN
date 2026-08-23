from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import ConfigBackup, RequestLog, TelemetryEvent


def storage_summary(db: Session) -> dict:
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
            "masterlog_max_mb": settings.masterlog_max_mb,
        },
    }


def cleanup_plan(db: Session) -> dict:
    settings = get_settings()
    now = datetime.now(UTC)
    request_cutoff = now - timedelta(days=settings.request_log_retention_days)
    telemetry_cutoff = now - timedelta(days=settings.telemetry_retention_days)
    old_requests = db.query(RequestLog).filter(RequestLog.ts < request_cutoff).count()
    old_telemetry = db.query(TelemetryEvent).filter(TelemetryEvent.ts < telemetry_cutoff).count()
    backup_ids = []
    grouped: dict[str, list[int]] = {}
    for row in db.query(ConfigBackup).order_by(ConfigBackup.device_id, ConfigBackup.created_at.desc()).all():
        grouped.setdefault(row.device_id or "unknown", []).append(row.id)
    for ids in grouped.values():
        backup_ids.extend(ids[settings.config_backup_retention_count:])
    return {
        "request_logs": old_requests,
        "telemetry_events": old_telemetry,
        "config_backups": len(backup_ids),
        "config_backup_ids": backup_ids,
        "estimated_mb": 0,
        "dry_run": True,
    }


def run_cleanup(db: Session) -> dict:
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
        "current_data_preserved": True,
    }
