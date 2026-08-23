"""Bounded retention for BASSWIESN 2.0 research-state history.

Current-state snapshots are deliberately never age-purged here. Only timeline
events, terminal scheduler/recovery rows and artwork that has been expired for
the grace period are eligible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from basswiesn.app.models import (
    ArtworkCacheEntry,
    DiagnosticEvent,
    RecoveryOperation,
    ReportingQueueEntry,
)
from basswiesn.app.config import get_settings


DEFAULT_DIAGNOSTIC_RETENTION_DAYS = 30
DEFAULT_REPORTING_QUEUE_RETENTION_DAYS = 7
DEFAULT_RECOVERY_RETENTION_DAYS = 30
DEFAULT_ARTWORK_EXPIRY_GRACE_DAYS = 7
DEFAULT_RETENTION_BATCH_SIZE = 500

TERMINAL_REPORTING_STATES = frozenset({"SUCCESS", "FAILED", "RECOVERED", "CANCELLED"})
TERMINAL_RECOVERY_STATES = frozenset({"SUCCESS", "FAILED", "CANCELLED", "RECOVERED"})


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return current if current.tzinfo else current.replace(tzinfo=UTC)


def research_retention_plan(
    db: Session,
    *,
    now: datetime | None = None,
    diagnostic_days: int = DEFAULT_DIAGNOSTIC_RETENTION_DAYS,
    reporting_queue_days: int = DEFAULT_REPORTING_QUEUE_RETENTION_DAYS,
    recovery_days: int = DEFAULT_RECOVERY_RETENTION_DAYS,
    artwork_grace_days: int = DEFAULT_ARTWORK_EXPIRY_GRACE_DAYS,
    batch_size: int = DEFAULT_RETENTION_BATCH_SIZE,
) -> dict[str, int | bool]:
    """Return a non-mutating retention preview."""

    current = _now(now)
    batch_size = max(1, int(batch_size))
    diagnostic_cutoff = current - timedelta(days=max(0, diagnostic_days))
    reporting_cutoff = current - timedelta(days=max(0, reporting_queue_days))
    recovery_cutoff = current - timedelta(days=max(0, recovery_days))
    artwork_cutoff = current - timedelta(days=max(0, artwork_grace_days))

    diagnostic_events = db.query(DiagnosticEvent).filter(
        DiagnosticEvent.occurred_at < diagnostic_cutoff,
    ).count()
    reporting_entries = db.query(ReportingQueueEntry).filter(
        func.upper(ReportingQueueEntry.status).in_(TERMINAL_REPORTING_STATES),
        ReportingQueueEntry.updated_at < reporting_cutoff,
    ).count()
    recovery_operations = db.query(RecoveryOperation).filter(
        func.upper(RecoveryOperation.status).in_(TERMINAL_RECOVERY_STATES),
        RecoveryOperation.completed_at.is_not(None),
        RecoveryOperation.completed_at < recovery_cutoff,
    ).count()
    artwork_entries = db.query(ArtworkCacheEntry).filter(
        ArtworkCacheEntry.expires_at.is_not(None),
        ArtworkCacheEntry.expires_at < artwork_cutoff,
    ).count()

    return {
        "diagnostic_events": diagnostic_events,
        "reporting_queue_entries": reporting_entries,
        "recovery_operations": recovery_operations,
        "artwork_entries": artwork_entries,
        "eligible_total": diagnostic_events + reporting_entries + recovery_operations + artwork_entries,
        "batch_size": batch_size,
        "dry_run": True,
        "current_snapshots_preserved": True,
    }


def _limited_ids(query: object, model: type, batch_size: int) -> list[int]:
    rows = query.with_entities(model.id).order_by(model.id).limit(batch_size).all()
    return [int(row[0]) for row in rows]


def apply_research_retention(
    db: Session,
    *,
    now: datetime | None = None,
    diagnostic_days: int = DEFAULT_DIAGNOSTIC_RETENTION_DAYS,
    reporting_queue_days: int = DEFAULT_REPORTING_QUEUE_RETENTION_DAYS,
    recovery_days: int = DEFAULT_RECOVERY_RETENTION_DAYS,
    artwork_grace_days: int = DEFAULT_ARTWORK_EXPIRY_GRACE_DAYS,
    batch_size: int = DEFAULT_RETENTION_BATCH_SIZE,
    artwork_cache_root: Path | None = None,
) -> dict[str, int | bool]:
    """Delete one bounded batch and commit it atomically."""

    current = _now(now)
    batch_size = max(1, int(batch_size))
    cutoffs = {
        "diagnostic": current - timedelta(days=max(0, diagnostic_days)),
        "reporting": current - timedelta(days=max(0, reporting_queue_days)),
        "recovery": current - timedelta(days=max(0, recovery_days)),
        "artwork": current - timedelta(days=max(0, artwork_grace_days)),
    }
    queries = (
        (
            "diagnostic_events",
            DiagnosticEvent,
            db.query(DiagnosticEvent).filter(DiagnosticEvent.occurred_at < cutoffs["diagnostic"]),
        ),
        (
            "reporting_queue_entries",
            ReportingQueueEntry,
            db.query(ReportingQueueEntry).filter(
                func.upper(ReportingQueueEntry.status).in_(TERMINAL_REPORTING_STATES),
                ReportingQueueEntry.updated_at < cutoffs["reporting"],
            ),
        ),
        (
            "recovery_operations",
            RecoveryOperation,
            db.query(RecoveryOperation).filter(
                func.upper(RecoveryOperation.status).in_(TERMINAL_RECOVERY_STATES),
                RecoveryOperation.completed_at.is_not(None),
                RecoveryOperation.completed_at < cutoffs["recovery"],
            ),
        ),
    )

    remaining = batch_size
    deleted: dict[str, int | bool] = {}
    for key, model, query in queries:
        ids = _limited_ids(query, model, remaining) if remaining else []
        count = 0
        if ids:
            count = int(db.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False) or 0)
            remaining -= count
        deleted[key] = count

    artwork_query = db.query(ArtworkCacheEntry).filter(
        ArtworkCacheEntry.expires_at.is_not(None),
        ArtworkCacheEntry.expires_at < cutoffs["artwork"],
    )
    artwork_rows = artwork_query.order_by(ArtworkCacheEntry.id).limit(remaining).all() if remaining else []
    root = artwork_cache_root or (get_settings().data_dir / "media" / "artwork-cache")
    artwork_ids: list[int] = []
    files_deleted = 0
    files_missing = 0
    files_blocked = 0
    file_errors = 0
    root_lexical = Path(os.path.abspath(root))
    root_is_symlink = root_lexical.is_symlink()
    root_resolved = root_lexical.resolve(strict=False)
    for row in artwork_rows:
        if not row.cached_path:
            artwork_ids.append(int(row.id))
            continue
        candidate_lexical = Path(os.path.abspath(Path(row.cached_path)))
        try:
            relative = candidate_lexical.relative_to(root_lexical)
        except ValueError:
            files_blocked += 1
            continue
        candidate = root_lexical / relative
        if root_is_symlink:
            files_blocked += 1
            continue
        cursor = root_lexical
        symlinked = False
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                symlinked = True
                break
        if symlinked:
            files_blocked += 1
            continue
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root_resolved)
        except (OSError, ValueError):
            files_blocked += 1
            continue
        if not candidate.exists():
            files_missing += 1
            artwork_ids.append(int(row.id))
            continue
        if not candidate.is_file():
            files_blocked += 1
            continue
        try:
            candidate.unlink()
        except OSError:
            file_errors += 1
            continue
        files_deleted += 1
        artwork_ids.append(int(row.id))

    artwork_count = 0
    if artwork_ids:
        artwork_count = int(
            db.query(ArtworkCacheEntry)
            .filter(ArtworkCacheEntry.id.in_(artwork_ids))
            .delete(synchronize_session=False)
            or 0
        )
        remaining -= artwork_count
    deleted["artwork_entries"] = artwork_count
    deleted["artwork_files_deleted"] = files_deleted
    deleted["artwork_files_missing"] = files_missing
    deleted["artwork_files_blocked"] = files_blocked
    deleted["artwork_file_errors"] = file_errors

    db.commit()
    deleted.update({
        "deleted_total": batch_size - remaining,
        "batch_size": batch_size,
        "dry_run": False,
        "current_snapshots_preserved": True,
    })
    return deleted
