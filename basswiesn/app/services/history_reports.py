"""Separate client-reported events from backend-observed listening sessions.

These compatibility calls are database-only. A browser timestamp/status cannot
authorize a new confirmed session or close one opened by actual radio readback.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy.orm import Session

from basswiesn.app.models import Device, PlayHistory, utc_now
from basswiesn.app.services.playback_state import is_confirmed_playing


def observed_session(db: Session, device: Device, *, now: datetime | None = None) -> PlayHistory | None:
    rows = (db.query(PlayHistory).filter(
        PlayHistory.device_id == device.device_id,
        PlayHistory.ended_at.is_(None), PlayHistory.is_confirmed.is_(True),
        PlayHistory.last_confirmed_playing_at.is_not(None),
    ).order_by(PlayHistory.last_confirmed_playing_at.desc(), PlayHistory.id.desc()).limit(2).all())
    if len(rows) != 1:
        return None
    row = rows[0]
    if not is_confirmed_playing(reachable=True, current_source=row.source, playback_state="PLAY_STATE",
                                state_observed_at=row.last_confirmed_playing_at, now=now or utc_now()):
        return None
    return row


def text(payload: Mapping[str, object], field: str, limit: int, default: str = "") -> str:
    value = payload.get(field)
    return value.strip()[:limit] if isinstance(value, str) else default


def integer(payload: Mapping[str, object], field: str, minimum: int, maximum: int) -> int | None:
    value = payload.get(field)
    if value in (None, ""):
        return None
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 3:
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("HISTORY_REPORT_INVALID")
    return value


def record_client_report(db: Session, device: Device, payload: Mapping[str, object]) -> PlayHistory:
    """Record a zero-duration, unverified local event; never modify live history."""
    now = utc_now()
    trigger = text(payload, "trigger", 64, "event") or "event"
    source = text(payload, "source", 64, "REMOTE") or "REMOTE"
    row = PlayHistory(
        device_id=device.device_id, device_name=device.name, device_ip=device.ip_address,
        started_at=now, ended_at=now, station_name=text(payload, "station_name", 255),
        stream_url=text(payload, "stream_url", 1024), source=source,
        source_type=text(payload, "source_type", 64, source), trigger=trigger,
        trigger_type="client_report", preset_button=integer(payload, "preset_button", 1, 6),
        preset_name=text(payload, "preset_name", 255), volume=integer(payload, "volume", 0, 100),
        # The boolean here describes verified operation success, not an HTTP
        # acknowledgment or an untrusted caller's assertion of audible output.
        success=0, is_confirmed=False, internal_event=True, is_internal=True,
        error_message=text(payload, "error_message", 1024),
        last_confirmed_playing_at=None, confirmed_duration_seconds=0,
        end_reason="client_report_unverified",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
