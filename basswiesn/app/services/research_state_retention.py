"""Bounded retention for BASSWIESN 2.0 research-state history.

Current-state snapshots are deliberately never age-purged here. Only timeline
events, terminal scheduler/recovery rows and artwork that has been expired for
the grace period are eligible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import errno
import os
from pathlib import Path
import stat
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Query, Session

from basswiesn.app.models import (
    ArtworkCacheEntry,
    DiagnosticEvent,
    DeviceInteraction,
    RecoveryOperation,
    ReportingQueueEntry,
)
from basswiesn.app.config import get_settings
from basswiesn.app.services.artwork_cache_lock import ArtworkCacheBusy, artwork_cache_lock


DEFAULT_DIAGNOSTIC_RETENTION_DAYS = 30
DEFAULT_REPORTING_QUEUE_RETENTION_DAYS = 7
DEFAULT_RECOVERY_RETENTION_DAYS = 30
DEFAULT_ARTWORK_EXPIRY_GRACE_DAYS = 7
DEFAULT_RETENTION_BATCH_SIZE = 500
DEFAULT_INTERACTION_RETENTION_DAYS = 14

# A GET method alone does not prove a read-only device protocol operation.
# Unknown endpoints are deliberately retained along with every mutation.
RETENTION_READ_ENDPOINTS = frozenset({
    "/info", "/capabilities", "/sources", "/presets", "/now_playing", "/nowPlaying",
    "/volume", "/bass", "/bassCapabilities", "/clockTime", "/networkInfo", "/netStats",
    "/getZone", "/getGroup", "/serviceAvailability",
})
TERMINAL_INTERACTION_RESULTS = frozenset({"ok", "error", "cache", "blocked", "skipped"})

TERMINAL_REPORTING_STATES = frozenset({"SUCCESS", "FAILED", "RECOVERED", "CANCELLED"})
TERMINAL_RECOVERY_STATES = frozenset({"SUCCESS", "FAILED", "CANCELLED", "RECOVERED"})


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return current if current.tzinfo else current.replace(tzinfo=UTC)


def _retained_read_query(db: Session, cutoff: datetime) -> Query[DeviceInteraction]:
    return db.query(DeviceInteraction).filter(
        func.upper(DeviceInteraction.method).in_({"GET", "HEAD"}),
        DeviceInteraction.endpoint.in_(RETENTION_READ_ENDPOINTS),
        func.lower(DeviceInteraction.result).in_(TERMINAL_INTERACTION_RESULTS),
        DeviceInteraction.ended_at.is_not(None),
        DeviceInteraction.ended_at < cutoff,
    )


def research_retention_plan(
    db: Session,
    *,
    now: datetime | None = None,
    diagnostic_days: int = DEFAULT_DIAGNOSTIC_RETENTION_DAYS,
    interaction_days: int = DEFAULT_INTERACTION_RETENTION_DAYS,
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
    device_interactions = _retained_read_query(db, current - timedelta(days=max(0, interaction_days))).count()

    return {
        "diagnostic_events": diagnostic_events,
        "reporting_queue_entries": reporting_entries,
        "recovery_operations": recovery_operations,
        "artwork_entries": artwork_entries,
        "device_interactions": device_interactions,
        "eligible_total": diagnostic_events + reporting_entries + recovery_operations + artwork_entries + device_interactions,
        "batch_size": batch_size,
        "dry_run": True,
        "current_snapshots_preserved": True,
    }


def _limited_ids(query: Query[Any], model: type[Any], batch_size: int) -> list[int]:
    rows = query.with_entities(model.id).order_by(model.id).limit(batch_size).all()
    return [int(row[0]) for row in rows]


class UnsafeArtworkPath(ValueError):
    """An expired cache row does not authorize this filesystem target."""


def _unlink_cached_file(root: Path, candidate: str) -> bool:
    """Unlink one regular leaf via pinned, no-follow directory descriptors.

    Return False for an already missing file. Opening every path component
    prevents an ancestor or cache subdirectory symlink from redirecting cleanup.
    No recursive deletion is performed.
    """
    root_path = Path(os.path.abspath(root))
    candidate_path = Path(os.path.abspath(candidate))
    try:
        relative = candidate_path.relative_to(root_path)
    except ValueError:
        raise UnsafeArtworkPath("artwork is outside its cache root") from None
    if not relative.parts:
        raise UnsafeArtworkPath("artwork must be a regular cache file")
    if any(part.startswith(".") for part in relative.parts):
        # In particular, never unlink the live .retention.lock inode: recreating
        # its name would allow another process to enter a different lock.
        raise UnsafeArtworkPath("artwork must not target cache control or temporary files")
    directory = os.open(root_path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in (*root_path.parts[1:], *relative.parts[:-1]):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        info = os.stat(relative.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeArtworkPath("artwork must be a regular cache file")
        os.unlink(relative.name, dir_fd=directory)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafeArtworkPath("artwork path contains a symlink or non-directory") from None
        raise
    finally:
        os.close(directory)


def _retain_artwork(db: Session, root: Path, cutoff: datetime, limit: int) -> dict[str, int]:
    result = {"artwork_entries": 0, "artwork_files_deleted": 0, "artwork_files_missing": 0,
              "artwork_files_blocked": 0, "artwork_file_errors": 0}
    if not limit:
        return result
    try:
        with artwork_cache_lock(root):
            return _retain_locked_artwork(db, root, cutoff, limit, result)
    except ArtworkCacheBusy:
        # Busy/unsafe/unavailable cache ownership leaves all rows/files intact.
        # History rows can still be committed by the caller; a later pass retries.
        result["artwork_file_errors"] += 1
        return result


def _retain_locked_artwork(
    db: Session, root: Path, cutoff: datetime, limit: int, result: dict[str, int],
) -> dict[str, int]:
    rows = db.query(ArtworkCacheEntry).filter(
        ArtworkCacheEntry.expires_at.is_not(None),
        ArtworkCacheEntry.expires_at < cutoff,
    ).order_by(ArtworkCacheEntry.id).limit(limit).all()
    # Producer os.replace + row commit uses this same process-shared lock.
    # No refresh can replace an expired leaf while it is selected/unlinked.
    pending = [(int(row.id), row.cached_path) for row in rows]
    for row in rows:
        row.failure_status = "RETENTION_PENDING"
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    artwork_ids: list[int] = []
    for row_id, cached_path in pending:
        pending_row = db.query(ArtworkCacheEntry).filter(
            ArtworkCacheEntry.id == row_id,
            ArtworkCacheEntry.failure_status == "RETENTION_PENDING",
            ArtworkCacheEntry.cached_path == cached_path,
            ArtworkCacheEntry.expires_at < cutoff,
        ).populate_existing().one_or_none()
        if pending_row is None:
            continue
        if not cached_path:
            artwork_ids.append(row_id)
            continue
        try:
            removed = _unlink_cached_file(root, cached_path)
        except UnsafeArtworkPath:
            result["artwork_files_blocked"] += 1
            continue
        except OSError:
            result["artwork_file_errors"] += 1
            continue
        result["artwork_files_deleted" if removed else "artwork_files_missing"] += 1
        artwork_ids.append(row_id)
    if artwork_ids:
        result["artwork_entries"] = int(db.query(ArtworkCacheEntry).filter(
            ArtworkCacheEntry.id.in_(artwork_ids),
            ArtworkCacheEntry.failure_status == "RETENTION_PENDING",
            ArtworkCacheEntry.expires_at < cutoff,
        ).delete(synchronize_session=False) or 0)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


def apply_research_retention(
    db: Session,
    *,
    now: datetime | None = None,
    diagnostic_days: int = DEFAULT_DIAGNOSTIC_RETENTION_DAYS,
    interaction_days: int = DEFAULT_INTERACTION_RETENTION_DAYS,
    reporting_queue_days: int = DEFAULT_REPORTING_QUEUE_RETENTION_DAYS,
    recovery_days: int = DEFAULT_RECOVERY_RETENTION_DAYS,
    artwork_grace_days: int = DEFAULT_ARTWORK_EXPIRY_GRACE_DAYS,
    batch_size: int = DEFAULT_RETENTION_BATCH_SIZE,
    artwork_cache_root: Path | None = None,
) -> dict[str, int | bool]:
    """Delete one bounded batch, with recoverable expired-artwork cleanup.

    History deletion and artwork tombstones are committed before unlinking any
    file. Failed unlinks keep their tombstone for retry. A crash/DB failure after
    unlink may leave an expired row pointing to an absent cache file; the next
    pass completes deletion. SQLite and filesystem changes are NOT one atomic
    transaction, and current snapshots/active operations are never selected.
    """

    current = _now(now)
    batch_size = max(1, int(batch_size))
    cutoffs = {
        "diagnostic": current - timedelta(days=max(0, diagnostic_days)),
        "reporting": current - timedelta(days=max(0, reporting_queue_days)),
        "recovery": current - timedelta(days=max(0, recovery_days)),
        "artwork": current - timedelta(days=max(0, artwork_grace_days)),
        "interaction": current - timedelta(days=max(0, interaction_days)),
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
        ("device_interactions", DeviceInteraction, _retained_read_query(db, cutoffs["interaction"])),
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

    root = artwork_cache_root or (get_settings().data_dir / "media" / "artwork-cache")
    artwork_result = _retain_artwork(db, root, cutoffs["artwork"], remaining)
    remaining -= artwork_result["artwork_entries"]
    deleted.update(artwork_result)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    deleted.update({
        "deleted_total": batch_size - remaining,
        "batch_size": batch_size,
        "dry_run": False,
        "current_snapshots_preserved": True,
    })
    return deleted
