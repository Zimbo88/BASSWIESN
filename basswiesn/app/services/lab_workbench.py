"""Bounded, local-only projections for the LAB workbench.

No adapters, HTTP clients, background workers or arbitrary log payloads belong
here. A stored observation is neither a current radio readback nor a cause.
"""
from datetime import UTC, datetime, timedelta
import csv
import hashlib
import io
import json
import re
from types import SimpleNamespace
from sqlalchemy import func

from basswiesn.app.models import (
    DiagnosticEvent, MetadataState, PlaybackHealthState, PlaybackState,
    PlayHistory, ProviderHealthState, ReportingState, RestrictionState, RuntimeState, Station,
)
from basswiesn.app.services.listening_stats import aware, observed_interval, overlap_seconds
from basswiesn.app.services.support_export import redact_text

HISTORY_LIMIT = 2000
EVENT_LIMIT = 100
OBSERVATION_TTL = 600


def iso(value):
    return aware(value).isoformat() if value else None


def age(value, now):
    if not value:
        return None
    seconds = int((now - aware(value)).total_seconds())
    return seconds if seconds >= 0 else None


def text(value, limit=200):
    # These are UI summaries, not a facility for exporting raw evidence. URLs,
    # household identifiers and assignment-style secrets have no place here.
    value = redact_text(str(value or "")[:4096])
    value = re.sub(r"https?://[^\s<>]+", "[URL]", value, flags=re.I)
    value = re.sub(r"(?i)\b[0-9a-f]{12}\b|\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", "[ID]", value)
    value = re.sub(r"/(?:home|Users)/[^\s<>]+", "[PATH]", value)
    return " ".join(value.split())[:limit]


def code(value):
    value = str(value or "UNKNOWN")
    return value if text(value) == value and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", value) else "UNKNOWN"


def stamp(value, now):
    seconds = age(value, now)
    return {"observed_at": iso(value), "age_seconds": seconds,
            "freshness": "UNKNOWN" if seconds is None else "RECENT" if seconds <= OBSERVATION_TTL else "STALE"}


def history_query(db, device_id):
    internal = {"standby", "keepalive_internal", "maintenance_internal", "setup_activation",
                "six_hour_refresh", "background_probe", "background_maintenance"}
    return db.query(PlayHistory).filter(
        PlayHistory.device_id == device_id, PlayHistory.success == 1,
        PlayHistory.is_confirmed.is_(True), PlayHistory.is_internal.is_(False),
        PlayHistory.internal_event.is_(False),
        func.lower(PlayHistory.source).not_in(internal),
        func.lower(PlayHistory.source_type).not_in(internal),
        func.lower(PlayHistory.trigger).not_in(internal | {"stop", "pause", "stop_pause"}),
        func.lower(PlayHistory.trigger_type).not_in(internal),
    ).order_by(PlayHistory.started_at.desc(), PlayHistory.id.desc())


def station_revision(station):
    """Bind replay confirmation to today's mapping, never a historical URL."""
    values = [station.id, station.name, station.stream_url, station.stream_url_original,
              station.stream_url_resolved, station.provider, station.provider_station_id,
              station.stream_format, station.internal, station.lab_only]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def recent_stations(db, device_id, now):
    rows = history_query(db, device_id).limit(HISTORY_LIMIT + 1).all()
    stations = {s.id: s for s in db.query(Station).filter(
        Station.id.in_({r.station_id for r in rows[:HISTORY_LIMIT] if r.station_id}))}
    result, seen = [], set()
    for row in rows[:HISTORY_LIMIT]:
        if aware(row.started_at) > now:
            continue
        key = row.station_id if row.station_id else (row.source, row.station_name)
        if key in seen:
            continue
        seen.add(key)
        station = stations.get(row.station_id)
        replayable = bool(station and not station.internal and station.stream_url
                          and station.provider == "LOCAL_INTERNET_RADIO")
        result.append({"history_id": row.id, "station_id": row.station_id,
                       "name": text(station.name if station else row.station_name) or "—",
                       "source": code(row.source), "last_played_at": iso(row.started_at),
                       "replayable": replayable,
                       "revision": station_revision(station) if replayable else None})
        if len(result) == 40:
            break
    return {"items": result, "history_limit": HISTORY_LIMIT,
            "truncated": len(rows) > HISTORY_LIMIT, "uses_current_station_mapping": True}


def metadata_summary(row, playback, now):
    if row is None:
        return {"state": "NOT_OBSERVED", "fields": {}, "provenance": "UNKNOWN",
                "age_seconds": None, "observed_at": None, "selection_match": "UNKNOWN"}
    seconds = age(row.updated_at, now)
    mismatch = bool(playback and row.source and playback.source and row.source != playback.source)
    state = ("SELECTION_MISMATCH" if mismatch else "UNKNOWN" if seconds is None
             else "STALE" if row.stale or seconds > 300 else "RECENT")
    return {"state": state, "provenance": code(row.provenance),
            "age_seconds": seconds, "observed_at": iso(row.updated_at),
            # Matching source alone does not establish matching station/session.
            "selection_match": "MISMATCH" if mismatch else "UNKNOWN",
            "source": code(row.source), "confidence": min(100, max(0, row.confidence or 0)),
            "fields": {key: text(getattr(row, key)) for key in ("station_name", "track", "artist", "album", "genre")},
            "artwork_present": bool(row.artwork_url), "artwork_provenance": code(row.artwork_provenance),
            "audible_output_verified": False}


def cached_snapshot(db, device, *, now=None):
    now = aware(now or datetime.now(UTC))
    identity = device.device_id
    playback = db.query(PlaybackState).filter_by(device_id=identity).one_or_none()
    runtime = db.query(RuntimeState).filter_by(key=f"device:{identity}:runtime_state").one_or_none()
    if runtime and (playback is None or aware(runtime.updated_at) > aware(playback.updated_at)):
        try:
            payload = json.loads(runtime.value)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and (payload.get("current_source") or payload.get("playback_state")):
            # Legacy cache lacks an independently dated volume observation.
            playback = SimpleNamespace(source=payload.get("current_source"), status=payload.get("playback_state"),
                                       volume=None, mute=None, updated_at=runtime.updated_at)
    health = db.query(PlaybackHealthState).filter_by(device_id=identity).one_or_none()
    metadata = db.query(MetadataState).filter_by(device_id=identity).one_or_none()
    # Stable database identifiers are used only in queries, never raw URLs,
    # account identifiers, evidence_json or unrestricted message bodies.
    providers = db.query(ProviderHealthState).filter_by(device_id=identity).order_by(ProviderHealthState.id).limit(30).all()
    reports = db.query(ReportingState).filter_by(device_id=identity).order_by(ReportingState.id).limit(30).all()
    restrictions = db.query(RestrictionState).filter_by(device_id=identity).order_by(RestrictionState.id).limit(30).all()
    return {
        "schema": 1, "captured_at": now.isoformat(), "origin": "DATABASE_ONLY",
        "radio_contacted": False, "audible_output_verified": False,
        "device": {"model": text(device.model), "firmware": text(device.firmware),
                   "cached_reachable": device.reachable,
                   **stamp(device.last_seen if device.reachable else device.last_failed_at, now)},
        "playback": ({"source": code(playback.source), "status": code(playback.status),
                      "volume": playback.volume, "mute": playback.mute, **stamp(playback.updated_at, now)} if playback else None),
        "health": ({"state": code(health.state), "source_valid": health.source_valid,
                    "stream_alive": health.stream_alive, "position_advancing": health.position_advancing,
                    "recovery_stage": health.recovery_stage, **stamp(health.observed_at, now)} if health else None),
        "metadata": metadata_summary(metadata, playback, now),
        "providers": [{"slot": i + 1, "source": code(r.source), "state": code(r.state),
                       "availability": code(r.availability), **stamp(r.updated_at, now)} for i, r in enumerate(providers)],
        "reporting": [{"slot": i + 1, "state": code(r.state), "queue_depth": r.queue_depth,
                       "retry_count": r.retry_count, "http_status": r.last_http_status,
                       **stamp(r.updated_at, now)} for i, r in enumerate(reports)],
        "restrictions": [{"slot": i + 1, "timer_enabled": r.timer_enabled,
                          "inactivity_timeout_s": r.inactivity_timeout_s,
                          "effective_until": iso(r.effective_until), **stamp(r.updated_at, now)} for i, r in enumerate(restrictions)],
    }


def diagnose(snapshot):
    observations = []
    def add(key, layer, observed):
        observations.append({"code": key, "layer": layer,
                             "freshness": observed.get("freshness", "UNKNOWN"),
                             "observed_at": observed.get("observed_at")})
    device, health, playback = snapshot["device"], snapshot["health"], snapshot["playback"]
    if not device["cached_reachable"]:
        add("CACHED_OFFLINE", "CONTROL", device)
    if health:
        if health["source_valid"] is False:
            add("SOURCE_INVALID", "PLAYBACK", health)
        if health["stream_alive"] is False:
            add("STREAM_NOT_ALIVE", "PLAYBACK", health)
        if health["state"] in {"STALLED", "RECOVERING", "FAILED", "INVALID_SOURCE"}:
            add("PLAYBACK_ATTENTION", "PLAYBACK", health)
    if playback and playback["status"] in {"STANDBY", "STOPPED", "STOP_STATE"}:
        add("STOP_OBSERVED", "PLAYBACK", playback)
    for provider in snapshot["providers"]:
        if provider["state"] in {"DEGRADED", "UNAVAILABLE", "FAILED", "ERROR", "SERVICE_UNAVAILABLE", "SOURCE_INVALID", "AUTH_REFRESH_REQUIRED", "RECOVERING"}:
            add("PROVIDER_ATTENTION", "PROVIDER", provider)
    for report in snapshot["reporting"]:
        if report["retry_count"] or report["state"] in {"FAILED", "ERROR", "BACKOFF"}:
            add("REPORTING_ONLY", "REPORTING", report)
    for restriction in snapshot["restrictions"]:
        if restriction["timer_enabled"] and restriction["effective_until"]:
            add("TIMER_CONFIGURED", "RESTRICTIONS", restriction)
    if snapshot["metadata"]["state"] in {"STALE", "SELECTION_MISMATCH"}:
        add("METADATA_ONLY", "METADATA", {})
    return {"root_cause": "NOT_ESTABLISHED", "automatic_action": False,
            "observations": observations, "no_observed_issue_is_health_proof": False}


def timeline(db, device_id, now, hours):
    rows = db.query(DiagnosticEvent).filter(
        DiagnosticEvent.device_id == device_id,
        DiagnosticEvent.occurred_at >= now - timedelta(hours=hours),
        DiagnosticEvent.occurred_at <= now,
    ).order_by(DiagnosticEvent.occurred_at.desc(), DiagnosticEvent.id.desc()).limit(EVENT_LIMIT + 1).all()
    # Show the newest bounded window chronologically. No arbitrary log message
    # or header payload reaches the browser or a snapshot.
    return {"items": [{"at": iso(r.occurred_at), "domain": code(r.domain),
                       "code": code(r.code), "severity": code(r.severity)}
                      for r in reversed(rows[:EVENT_LIMIT])],
            "truncated": len(rows) > EVENT_LIMIT, "limit": EVENT_LIMIT, "hours": hours}


def compare_snapshots(before, after):
    changes = []
    ignored = {"captured_at", "observed_at", "age_seconds", "freshness"}
    def walk(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                if key not in ignored:
                    walk(a.get(key), b.get(key), f"{path}.{key}" if path else key)
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            for i, (old, new) in enumerate(zip(a, b)):
                walk(old, new, f"{path}[{i}]")
        elif a != b:
            changes.append({"field": path, "before": a, "after": b,
                            "state_class": "VOLATILE"})
    # Lists keep their recorded order; no inference about provider identities.
    # A no-change result is not an EXACT radio restore certificate.
    walk(before, after, "")
    return {"changes": changes, "comparison": "CACHED_STATE_ONLY",
            "restore_verified": False, "observation_timestamps_ignored": True}


def csv_cell(value):
    value = str(value if value is not None else "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def listening_csv(db, device_id, *, now, days):
    boundary = now - timedelta(days=days)
    query = history_query(db, device_id).filter(
        PlayHistory.started_at <= now,
        (PlayHistory.ended_at.is_(None) | (PlayHistory.ended_at > boundary)),
    )
    rows = query.limit(HISTORY_LIMIT + 1).all()
    truncated = len(rows) > HISTORY_LIMIT
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["station", "source", "start_utc", "observed_end_utc", "estimated_seconds_in_window",
                     "end_reason", "audible_output_verified", "window_days", "export_truncated"])
    for row in rows[:HISTORY_LIMIT]:
        start, end = observed_interval(row, now=now, tolerance=360)
        seconds = overlap_seconds(start, end, boundary)
        if not seconds:
            continue
        writer.writerow([csv_cell(text(row.station_display_name or row.station_name)), code(row.source),
                         iso(max(start, boundary)), iso(end), seconds, code(row.end_reason),
                         "false", days, str(truncated).lower()])
    return output.getvalue(), truncated


def format_inventory(db):
    rows = db.query(Station).filter(Station.internal.is_(False)).order_by(Station.name, Station.id).limit(501).all()
    result = []
    for station in rows[:500]:
        fmt = (station.stream_format or station.stream_codec or "").lower()
        decision = "HLS_REQUIRES_ADAPTATION" if station.is_hls or fmt in {"hls", "m3u8"} else (
            "DIRECT_CANDIDATE" if fmt in {"mp3", "aac", "aac+", "aacp"} else "FORMAT_UNVERIFIED")
        result.append({"id": station.id, "name": text(station.name), "format": code(fmt),
                       "assessment": decision, "hardware_verified": False})
    return {"items": result, "truncated": len(rows) > 500, "origin": "SAVED_STATION_HINTS_ONLY",
            "network_probed": False, "transcoding_available": False}
