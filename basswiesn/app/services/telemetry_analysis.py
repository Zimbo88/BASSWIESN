from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
import csv
from io import StringIO
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from basswiesn.app.models import Device, RequestLog, RuntimeState, TelemetryEvent
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.services.diagnostics import redact_support_text
from basswiesn.app.services.provider_registry import SERVICE_MANIFEST


SECRET_KEYS = ("authorization", "cookie", "token", "password", "secret", "credential")


def redact_mapping(values: dict[str, Any]) -> dict[str, Any]:
    clean = {}
    for key, value in values.items():
        if any(marker in str(key).lower() for marker in SECRET_KEYS):
            clean[key] = "***REDACTED***"
        elif isinstance(value, dict):
            clean[key] = redact_mapping(value)
        else:
            clean[key] = redact_support_text(str(value)) if isinstance(value, str) else value
    return clean


def since_for_range(value: str) -> datetime | None:
    now = datetime.now(UTC)
    key = (value or "24h").lower()
    if key in {"1h", "hour", "last_hour"}:
        return now - timedelta(hours=1)
    if key in {"24h", "day", "last_day"}:
        return now - timedelta(hours=24)
    if key in {"7d", "week", "last_week"}:
        return now - timedelta(days=7)
    return None


def _request_rows(db: Session, range_value: str) -> list[RequestLog]:
    query = db.query(RequestLog)
    since = since_for_range(range_value)
    if since is not None:
        query = query.filter(RequestLog.ts >= since)
    return query.order_by(RequestLog.ts.desc()).all()


def _telemetry_rows(db: Session, range_value: str) -> list[TelemetryEvent]:
    query = db.query(TelemetryEvent)
    since = since_for_range(range_value)
    if since is not None:
        query = query.filter(TelemetryEvent.ts >= since)
    return query.order_by(TelemetryEvent.ts.desc()).all()


def _masterlog_events(db: Session, range_value: str) -> list[dict[str, Any]]:
    from basswiesn.app.config import get_settings

    path = get_settings().data_dir / "logs" / "master.log"
    since = since_for_range(range_value)
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return events
    for line in lines[-5000:]:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        ts_text = str(item.get("ts") or "")
        try:
            ts = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
        except ValueError:
            ts = None
        if since is not None and ts is not None and ts < since:
            continue
        events.append(item)
    return events


def _event_counter(events: list[dict[str, Any]], telemetry: list[TelemetryEvent]) -> Counter:
    counts = Counter(str(item.get("event") or "unknown") for item in events)
    counts.update(row.event_type or "unknown" for row in telemetry)
    return counts


def _volume_summary(events: list[dict[str, Any]], telemetry: list[TelemetryEvent]) -> list[dict[str, Any]]:
    by_device: dict[str, dict[str, Any]] = defaultdict(lambda: {"device_id": "unknown", "last_volume": None, "max_volume": None, "violations": 0, "events": 0, "recommendation": "unknown"})
    for item in events:
        event = str(item.get("event") or "")
        if not event.startswith("volume_safety"):
            continue
        device_id = str(item.get("device_id") or item.get("radio_ip") or "unknown")
        row = by_device[device_id]
        row["device_id"] = device_id
        row["events"] += 1
        value = item.get("volume", item.get("requested"))
        if isinstance(value, (int, float)):
            row["last_volume"] = int(value)
            row["max_volume"] = int(value) if row["max_volume"] is None else max(row["max_volume"], int(value))
            if int(value) > 10:
                row["violations"] += 1
        if event == "volume_safety_failed":
            row["violations"] += 1
            row["recommendation"] = "Radio blockiert Volume Write oder antwortet verzögert."
    for item in by_device.values():
        if item["recommendation"] != "unknown":
            continue
        if item["max_volume"] is None:
            item["recommendation"] = "Keine Volume-Daten vorhanden."
        elif item["max_volume"] > 10:
            item["recommendation"] = "Safe Volume niedriger konfigurieren."
        elif item["max_volume"] <= 5:
            item["recommendation"] = "Volume Safety stabil."
        else:
            item["recommendation"] = "Volume beobachten."
    return sorted(by_device.values(), key=lambda item: item["device_id"])


def _heartbeat_summary(requests: list[RequestLog]) -> dict[str, Any]:
    successful = sorted([row for row in requests if 200 <= row.status_code < 400], key=lambda row: row.ts)
    longest_gap_seconds = 0
    longest_gap = None
    previous = None
    for row in successful:
        if previous is not None:
            gap = (row.ts - previous.ts).total_seconds()
            if gap > longest_gap_seconds:
                longest_gap_seconds = int(gap)
                longest_gap = {"from": previous.ts.isoformat(), "to": row.ts.isoformat(), "seconds": int(gap)}
        previous = row
    last_success = successful[-1].ts.isoformat() if successful else ""
    power_on = [row for row in requests if row.path == "/streaming/support/power_on"]
    account_full = [row for row in requests if re.match(r"/streaming/account/[^/]+/full$", row.path)]
    provider_settings = [row for row in requests if re.match(r"/streaming/account/[^/]+/provider_settings$", row.path)]
    heartbeat_paths = [row for row in requests if any(token in row.path.lower() for token in ("power_on", "heartbeat", "provider_settings", "/full"))]
    candidate = longest_gap_seconds >= 6 * 60 * 60
    if longest_gap_seconds >= 60 * 60:
        write_masterlog("heartbeat_gap_detected", longest_gap_seconds=longest_gap_seconds, last_success=last_success)
    if candidate:
        write_masterlog("six_hour_gap_candidate", longest_gap_seconds=longest_gap_seconds, last_success=last_success)
    return {
        "longest_gap_seconds": longest_gap_seconds,
        "longest_gap": longest_gap,
        "last_successful_response": last_success,
        "power_on_events": len(power_on),
        "account_sync_events": len(account_full),
        "provider_settings_requests": len(provider_settings),
        "heartbeat_requests": len(heartbeat_paths),
        "six_hour_gap_candidate": candidate,
        "recommendation": "Möglicher 6-Stunden-Timeout. Heartbeat / Noop Route prüfen." if candidate else "",
    }


def _playback_protection(db: Session) -> list[dict[str, Any]]:
    devices = {row.device_id: row for row in db.query(Device).all()}
    rows = []
    for row in db.query(RuntimeState).filter(RuntimeState.key.like("device:%:runtime_state")).all():
        device_id = row.key.removeprefix("device:").removesuffix(":runtime_state")
        try:
            payload = json.loads(row.value or "{}")
        except ValueError:
            payload = {}
        keepalive = payload.get("playback_keepalive") or {}
        if not keepalive:
            continue
        device = devices.get(device_id)
        status = keepalive.get("playback_observation_status", "unknown")
        if status == "invalid_source_diagnosis_required":
            recommendation = "INVALID_SOURCE-Evidenz prüfen; keine automatische Quellenwahl ausgeführt."
        elif status == "restriction_expired_observed":
            recommendation = "Provider-Restriction prüfen; keine automatische Wiedergabeaktion ausgeführt."
        elif keepalive.get("playing") and status == "playing_observed":
            recommendation = "Autoritativer Radio-Readback meldet laufende Wiedergabe."
        elif keepalive.get("consecutive_failures"):
            recommendation = "Radio-Erreichbarkeit prüfen; kein Auto-Reboot."
        else:
            recommendation = "Weiter beobachten."
        rows.append({
            "device_id": device_id,
            "name": device.name if device else "",
            "ip": device.ip_address if device else "",
            "currently_playing": bool(keepalive.get("playing")),
            "last_seen_playback": keepalive.get("last_seen_playback", ""),
            "longest_playback_seconds": int(keepalive.get("longest_playback_seconds") or 0),
            "last_keepalive": keepalive.get("last_keepalive_at", ""),
            "playback_observation_status": status,
            "restriction": keepalive.get("restriction") or {},
            "last_stop_detected": keepalive.get("last_stop_detected", ""),
            "recommendation": recommendation,
        })
    return sorted(rows, key=lambda item: item["device_id"])


def telemetry_summary(db: Session, range_value: str = "24h") -> dict[str, Any]:
    requests = _request_rows(db, range_value)
    telemetry = _telemetry_rows(db, range_value)
    master_events = _masterlog_events(db, range_value)
    event_counts = _event_counter(master_events, telemetry)
    path_counts = Counter(row.path for row in requests)
    method_counts = Counter(row.method for row in requests)
    status_counts = Counter(str(row.status_code) for row in requests)
    unknown_requests = [row for row in requests if "unknown_cloud_request" in (row.body or "") or row.service == "cloud-catchall"]
    error_requests = [row for row in requests if row.status_code in {404} or row.status_code >= 500]
    by_device: dict[str, dict[str, Any]] = {}
    devices = {row.device_id: row for row in db.query(Device).all()}
    for row in telemetry:
        item = by_device.setdefault(row.device_id or "unknown", {"device_id": row.device_id or "unknown", "name": devices.get(row.device_id).name if devices.get(row.device_id) else "", "ip": devices.get(row.device_id).ip_address if devices.get(row.device_id) else "", "last_request": "", "last_preset_action": "", "last_setup_action": "", "last_error": "", "volume_events": 0})
        if not item["last_request"]:
            item["last_request"] = row.ts.isoformat()
        event_type = row.event_type or ""
        if "preset" in event_type and not item["last_preset_action"]:
            item["last_preset_action"] = row.ts.isoformat()
        if "setup" in event_type and not item["last_setup_action"]:
            item["last_setup_action"] = row.ts.isoformat()
        if "failed" in event_type or "error" in event_type:
            item["last_error"] = row.parsed_summary or event_type
        if "volume_safety" in event_type:
            item["volume_events"] += 1
    recommendations = []
    if len(unknown_requests) >= 3:
        recommendations.append("Cloud Catch-All prüfen / neue Route eventuell emulieren.")
    if any(row.status_code == 404 for row in error_requests):
        recommendations.append("Route sollte als Noop oder Stub ergänzt werden.")
    if event_counts.get("runtime_state_parse_warning", 0):
        recommendations.append("Runtime-State Parser mit aktuellen Radio-Captures prüfen.")
    if event_counts.get("volume_safety_failed", 0):
        recommendations.append("Playback Safety / Startup Volume prüfen.")
    heartbeat = _heartbeat_summary(requests)
    if heartbeat["six_hour_gap_candidate"]:
        recommendations.append("Möglicher 6-Stunden-Timeout. Heartbeat / Noop Route prüfen.")
    protection = _playback_protection(db)
    return {
        "range": range_value,
        "generated_at": datetime.now(UTC).isoformat(),
        "service_health": {
            "webgui_online_percent": 100,
            "cloud_online_percent": 100 if requests else 0,
            "debug_online_percent": 100,
            "last_errors": [item for item in master_events if str(item.get("event", "")).endswith("failed") or "error" in str(item.get("event", ""))][-10:],
            "last_restarts": [item for item in master_events if item.get("event") == "app_start"][-10:],
        },
        "cloud_requests": {
            "total": len(requests),
            "top_paths": path_counts.most_common(20),
            "top_methods": method_counts.most_common(10),
            "status_codes": status_counts.most_common(),
            "unknown_requests": len(unknown_requests),
            "error_requests": len(error_requests),
            "bose_hosts": sorted({row.host for row in requests if "bose" in (row.host or "").lower()}),
            "unknown_hosts": sorted({str(item.get("host")) for item in master_events if item.get("event") == "unknown_host_detected" and item.get("host")}),
        },
        "radio_activity": sorted(by_device.values(), key=lambda item: item["device_id"]),
        "heartbeat_analysis": heartbeat,
        "playback_protection": protection,
        "error_groups": {key: event_counts.get(key, 0) for key in ("setup_failed", "preset_apply_failed", "cloud_request_error", "unknown_cloud_request", "runtime_state_parse_warning", "service_status_check offline", "volume_safety_failed")},
        "volume_safety": _volume_summary(master_events, telemetry),
        "recommendations": recommendations,
    }


def telemetry_export_json(db: Session, range_value: str = "24h") -> str:
    return json.dumps(redact_mapping(telemetry_summary(db, range_value)), ensure_ascii=False, indent=2)


def telemetry_export_csv(db: Session, range_value: str = "24h") -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["section", "key", "value"])
    summary = telemetry_summary(db, range_value)
    writer.writerow(["cloud_requests", "total", summary["cloud_requests"]["total"]])
    for path, count in summary["cloud_requests"]["top_paths"]:
        writer.writerow(["top_path", path, count])
    for key, count in summary["error_groups"].items():
        writer.writerow(["error_group", key, count])
    for item in summary["radio_activity"]:
        writer.writerow(["radio", item["device_id"], json.dumps(redact_mapping(item), ensure_ascii=False)])
    return output.getvalue()


def telemetry_report_html(db: Session, range_value: str = "24h") -> str:
    summary = telemetry_summary(db, range_value)
    body = json.dumps(redact_mapping(summary), ensure_ascii=False, indent=2)
    return f"<!doctype html><html><head><meta charset=\"utf-8\"><title>BASSWIESN Telemetry Report</title><style>body{{font-family:system-ui;margin:24px;max-width:1100px}}pre{{white-space:pre-wrap;background:#f3f5f7;padding:16px}}</style></head><body><h1>BASSWIESN Telemetry Report</h1><p>Zeitraum: {range_value}</p><pre>{body}</pre></body></html>"


def emulation_gaps(db: Session) -> dict[str, Any]:
    requests = db.query(RequestLog).order_by(RequestLog.ts.desc()).limit(5000).all()
    unknown = [row for row in requests if row.service == "cloud-catchall" or "unknown_cloud_request" in (row.body or "")]
    frequent_404 = [item for item in Counter(row.path for row in requests if row.status_code == 404).most_common() if item[1] >= 1]
    recommendations = []
    for path, _count in Counter(row.path for row in unknown).most_common(20):
        if re.search(r"/streaming/account/.+/device/.+", path) and any(row.method == "DELETE" and row.path == path for row in unknown):
            recommendations.append({"path": path, "recommendation": "Als Noop-Route dauerhaft emulieren."})
        elif re.search(r"/streaming/account/.+/device/.+", path) and any(row.method == "PUT" and row.path == path for row in unknown):
            recommendations.append({"path": path, "recommendation": "Als Device-Binding-Accept Route emulieren."})
        else:
            recommendations.append({"path": path, "recommendation": "Route beobachten und bei Wiederholung als Stub ergänzen."})
    runtime_rows = db.query(RuntimeState).all()
    provider_gaps = []
    known = set(SERVICE_MANIFEST)
    for row in runtime_rows:
        try:
            payload = json.loads(row.value or "{}")
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        for name in (payload.get("providers") or {}):
            if name not in known and name != "unknown_provider":
                provider_gaps.append(name)
    for item in _playback_protection(db):
        if item["playback_observation_status"] == "invalid_source_diagnosis_required":
            recommendations.append({"path": "playback_keepalive", "recommendation": "INVALID_SOURCE-Evidenz prüfen; keine automatische Quellenwahl"})
        elif item["playback_observation_status"] == "restriction_expired_observed":
            recommendations.append({"path": "playback_keepalive", "recommendation": "Provider-Restriction prüfen; Wiedergabe nicht automatisch ändern"})
    status = "problem" if frequent_404 else "attention" if unknown or provider_gaps else "ok"
    return {
        "status": status,
        "unknown_routes": [{"method": row.method, "path": row.path, "host": row.host, "ts": row.ts.isoformat()} for row in unknown[:100]],
        "frequent_404": [{"path": path, "count": count} for path, count in frequent_404],
        "provider_gaps": sorted(set(provider_gaps)),
        "host_gaps": [],
        "recommendations": recommendations,
    }
