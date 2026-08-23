"""Canonical confirmed-playback rule and conservative history lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from basswiesn.app.models import Device, PlayHistory, RuntimeState, Station, utc_now
from basswiesn.app.services.playback_identity import apply_identity, clean_station_name, resolve_playback_identity


INACTIVE_SOURCES = {"", "STANDBY", "INVALID_SOURCE", "SOURCE_DISCONNECTED"}
INACTIVE_STATES = {
    "", "STOP_STATE", "PAUSE_STATE", "STANDBY", "INVALID_SOURCE",
    "SOURCE_DISCONNECTED", "BUFFERING_STATE", "ERROR_STATE",
}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def is_confirmed_playing(
    *,
    reachable: bool,
    current_source: str | None,
    playback_state: str | None,
    play_status: str | None = None,
    state_observed_at: datetime | None,
    now: datetime | None = None,
    stale_after_seconds: int = 360,
    manual_stop: bool = False,
    timed_out: bool = False,
    zone_removed: bool = False,
) -> bool:
    """Return true only for a fresh, successful Bose PLAY_STATE observation."""

    now = _aware(now or utc_now())
    observed = _aware(state_observed_at)
    source = str(current_source or "").strip().upper()
    status = str(play_status or playback_state or "").strip().upper()
    if not reachable or manual_stop or timed_out or zone_removed:
        return False
    if observed is None or now is None or observed > now + timedelta(seconds=5):
        return False
    if (now - observed).total_seconds() > max(1, stale_after_seconds):
        return False
    if source in INACTIVE_SOURCES or status in INACTIVE_STATES:
        return False
    return status == "PLAY_STATE"


def safe_session_end(
    row: PlayHistory,
    *,
    transition_at: datetime | None = None,
    device_last_seen: datetime | None = None,
) -> datetime:
    """Choose the last defensible playback instant, never the late query time."""

    started = _aware(row.started_at) or utc_now()
    candidates = (
        _aware(getattr(row, "last_confirmed_playing_at", None)),
        _aware(transition_at),
        _aware(device_last_seen),
        started,
    )
    chosen = next((item for item in candidates if item is not None and item >= started), started)
    return chosen


def close_open_sessions(
    db: Session,
    device_id: str,
    *,
    reason: str,
    transition_at: datetime | None = None,
    device_last_seen: datetime | None = None,
) -> int:
    rows = (
        db.query(PlayHistory)
        .filter(PlayHistory.device_id == device_id, PlayHistory.ended_at.is_(None))
        .order_by(PlayHistory.started_at)
        .all()
    )
    for row in rows:
        row.ended_at = safe_session_end(row, transition_at=transition_at, device_last_seen=device_last_seen)
        row.end_reason = reason[:64]
    return len(rows)


def confirm_playback_session(
    db: Session,
    device: Device,
    *,
    observed_at: datetime,
    source: str,
    station_id: int | None = None,
    station_name: str = "",
    stream_url: str = "",
    trigger: str = "live_state",
    trigger_type: str = "station",
    internal_event: bool = False,
    source_type: str | None = None,
    zone_master_id: str = "",
    zone_member_ids: str = "",
    preset_button: int | None = None,
    preset_name: str = "",
    volume: int | None = None,
    source_account: str = "",
    content_item_name: str = "",
) -> PlayHistory:
    """Refresh one open session or atomically replace stale/duplicate sessions."""

    observed_at = _aware(observed_at) or utc_now()
    open_rows = (
        db.query(PlayHistory)
        .filter(PlayHistory.device_id == device.device_id, PlayHistory.ended_at.is_(None))
        .order_by(PlayHistory.started_at.desc())
        .all()
    )
    current = open_rows[0] if open_rows else None
    for duplicate in open_rows[1:]:
        duplicate.ended_at = safe_session_end(duplicate, device_last_seen=device.last_seen)
        duplicate.end_reason = "duplicate_open_session"
    if current is not None and str(current.source or "").upper() != str(source or "").upper():
        current.ended_at = safe_session_end(current, transition_at=observed_at, device_last_seen=device.last_seen)
        current.end_reason = "source_changed"
        current = None
    if current is None:
        station = db.query(Station).filter(Station.id == station_id).one_or_none() if station_id else None
        identity = resolve_playback_identity(
            db,
            station_id=station_id,
            station_name=station_name or (station.name if station else ""),
            stream_url=stream_url or (station.stream_url if station else ""),
            source=source,
            source_account=source_account,
            content_item_name=content_item_name,
            device_id=device.device_id,
            preset_button=preset_button,
            internal_event=internal_event or bool(getattr(station, "internal", False)),
            is_confirmed=True,
        )
        current = PlayHistory(
            device_id=device.device_id, device_name=device.name, device_ip=device.ip_address,
            station_id=station_id, station_name=station_name or (station.name if station else ""),
            stream_url=stream_url or (station.stream_url if station else ""), source=source,
            source_account=source_account, content_item_name=content_item_name,
            source_type=source_type or source, trigger=trigger, trigger_type=trigger_type,
            zone_master_id=zone_master_id, zone_member_ids=zone_member_ids,
            preset_button=preset_button, preset_name=preset_name, volume=volume,
            internal_event=internal_event or bool(getattr(station, "internal", False)),
            started_at=observed_at, last_confirmed_playing_at=observed_at,
            is_confirmed=True,
        )
        apply_identity(current, identity)
        db.add(current)
        db.flush()
    else:
        current.last_confirmed_playing_at = observed_at
        if station_id and not current.station_id:
            current.station_id = station_id
        if station_name and not clean_station_name(current.station_name):
            current.station_name = station_name
        if stream_url and not current.stream_url:
            current.stream_url = stream_url
        if source_account and not getattr(current, "source_account", ""):
            current.source_account = source_account
        if content_item_name and not getattr(current, "content_item_name", ""):
            current.content_item_name = content_item_name
        if (
            not clean_station_name(getattr(current, "station_display_name", ""))
            or int(getattr(current, "identity_confidence", 0) or 0) < 50
        ):
            apply_identity(current, resolve_playback_identity(
                db,
                station_id=current.station_id,
                station_name=current.station_name,
                stream_url=current.stream_url,
                source=current.source,
                source_account=getattr(current, "source_account", ""),
                content_item_name=getattr(current, "content_item_name", ""),
                device_id=current.device_id,
                preset_button=getattr(current, "preset_button", None),
                internal_event=bool(getattr(current, "internal_event", False)),
                is_confirmed=True,
            ))
    return current


def conservative_duration_seconds(row: PlayHistory, *, now: datetime | None = None, poll_tolerance_seconds: int = 360) -> int:
    start = _aware(row.started_at) or _aware(row.ended_at) or utc_now()
    if row.ended_at is not None:
        end = _aware(row.ended_at) or start
    else:
        confirmed = _aware(getattr(row, "last_confirmed_playing_at", None)) or start
        end = min(_aware(now or utc_now()) or confirmed, confirmed + timedelta(seconds=max(0, poll_tolerance_seconds)))
    return max(0, int((end - start).total_seconds()))


def reconcile_open_play_history(db: Session) -> int:
    """Idempotently close every pre-boot open row; new live polls may reopen."""

    repaired = 0
    devices = {item.device_id: item for item in db.query(Device).all()}
    for row in db.query(PlayHistory).filter(PlayHistory.ended_at.is_(None)).all():
        device = devices.get(row.device_id)
        row.ended_at = safe_session_end(row, device_last_seen=device.last_seen if device else None)
        row.end_reason = "startup_reconciliation"
        repaired += 1
    if repaired:
        db.commit()
    return repaired
