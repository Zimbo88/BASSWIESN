"""Bounded cached-state projections; no payload/log/credential collection."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import sqlite3
from typing import cast

from basswiesn.app.services.provider_registry import SERVICE_MANIFEST
from basswiesn.cli_checks import OfflineConfig, protected_policy, timestamp
from basswiesn.cli_contract import Check, JsonValue, OfflineError, Report
from basswiesn.cli_storage import offline_database, quick_check, require_core, table_columns

PROVIDER_STATES = frozenset({"HEALTHY", "DEGRADED", "REPORTING_DEGRADED", "METADATA_STALE", "AUTH_REFRESH_REQUIRED", "SERVICE_UNAVAILABLE", "SOURCE_INVALID", "RECOVERING", "FAILED"})
RUN_STATES = frozenset({"running", "completed", "ok", "passed", "failed", "warning", "cancelled", "partial", "success", "error"})


def _device_rows(config: OfflineConfig, db: sqlite3.Connection, limit: int, *, identifiers: bool) -> list[JsonValue]:
    ips, protected_ids = protected_policy(config, db)
    columns = table_columns(db)["devices"]
    fields = "device_id, ip_address, reachable"
    if "last_seen" in columns:
        fields += ", last_seen"
    if "failure_count" in columns:
        fields += ", failure_count"
    rows: list[JsonValue] = []
    for row in db.execute(f"SELECT {fields} FROM devices ORDER BY device_id LIMIT ?", (limit,)):
        identifier = str(row["device_id"] or "")
        address = str(row["ip_address"] or "")
        try:
            parsed = ipaddress.ip_address(address)
            normalized_ip = str(parsed.ipv4_mapped if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped else parsed)
        except ValueError:
            normalized_ip = ""
        entry: dict[str, JsonValue] = {
            "device_ref": hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:12],
            "recorded_reachable": bool(row["reachable"]),
            "protected": normalized_ip in ips or identifier.upper() in protected_ids,
            "live_reachability": "NOT_PROBED",
        }
        if "last_seen" in columns:
            entry["last_seen"] = timestamp(row["last_seen"])
        if "failure_count" in columns:
            value = row["failure_count"]
            entry["recorded_failure_count"] = value if type(value) is int and 0 <= value <= 2**31 - 1 else None
        if identifiers:
            # Never echo free-text names, arbitrary identifiers, URLs or tokens.
            entry["device_id"] = identifier.upper() if re.fullmatch(r"[A-Fa-f0-9]{12}", identifier) else "UNAVAILABLE"
            entry["ip_address"] = normalized_ip or "UNAVAILABLE"
        rows.append(entry)
    return rows


def devices(config: OfflineConfig, *, limit: int, identifiers: bool) -> Report:
    report = Report("devices")
    with offline_database(config.database) as db:
        require_core(db)
        total = int(db.execute("SELECT COUNT(*) FROM devices").fetchone()[0])
        rows = _device_rows(config, db, limit, identifiers=identifiers)
    report.data = {"evidence": "cached_only", "devices": rows, "total": total, "truncated": total > limit, "identifiers_included": identifiers}
    report.checks.append(Check("devices.cached", "PASS", "Cached device records read; no discovery, resolution or contact occurred."))
    return report


def status(config: OfflineConfig) -> Report:
    report = Report("status")
    with offline_database(config.database) as db:
        quick_check(db)
        require_core(db)
        total = int(db.execute("SELECT COUNT(*) FROM devices").fetchone()[0])
        recorded_reachable = int(db.execute("SELECT COUNT(*) FROM devices WHERE reachable=1").fetchone()[0])
        ips, identifiers = protected_policy(config, db)
    report.data = {
        "database": "readable_snapshot",
        "devices": total,
        "recorded_reachable": recorded_reachable,
        "protected_ip_entries": len(ips),
        "protected_identifier_entries": len(identifiers),
        "live_runtime": "NOT_PROBED",
        "evidence": "offline_snapshot_only",
    }
    report.checks.append(Check("status.snapshot", "PASS", "Local snapshot is readable; this is not a running-service health check."))
    return report


def health(config: OfflineConfig, *, limit: int) -> Report:
    report = Report("health")
    runs: list[JsonValue] = []
    states: dict[str, JsonValue] = {}
    with offline_database(config.database) as db:
        require_core(db)
        columns = table_columns(db)
        if {"id", "status", "started_at", "ended_at", "duration_ms"}.issubset(columns.get("healthcheck_runs", set())):
            for row in db.execute("SELECT status, started_at, ended_at, duration_ms FROM healthcheck_runs ORDER BY id DESC LIMIT ?", (limit,)):
                duration = row[3]
                runs.append({"status": str(row[0]) if row[0] in RUN_STATES else "unknown", "started_at": timestamp(row[1]), "ended_at": timestamp(row[2]), "duration_ms": duration if type(duration) is int and duration >= 0 else None})
        if {"state"}.issubset(columns.get("provider_health_state", set())):
            for row in db.execute("SELECT state, COUNT(*) FROM provider_health_state GROUP BY state LIMIT 100"):
                state = str(row[0]) if row[0] in PROVIDER_STATES else "UNKNOWN"
                states[state] = int(cast(int, states.get(state, 0))) + int(row[1])
    report.data = {"evidence": "cached_only", "runs": runs, "provider_state_counts": states, "live_health": "NOT_PROBED"}
    report.checks.append(Check("health.cached", "PASS" if runs else "WARN", "Stored health history read without running checks.", "No cached health runs are available; run checks only through a separately authorized workflow." if not runs else ""))
    return report


def providers() -> Report:
    report = Report("providers")
    rows: list[JsonValue] = []
    # Registry metadata is static and contains legacy descriptor credentials:
    # copy only named public fields, never serialize the registry wholesale.
    manifest = SERVICE_MANIFEST
    for name, data in sorted(manifest.items()):
        contract = data.get("contract_status")
        contract_state = str(contract) if contract in {"CONFIRMED", "UNSUPPORTED"} else "UNCONFIRMED"
        rows.append({
            "source": name,
            "contract_status": contract_state,
            "registry_visible": data.get("visible") is True,
            "experimental": data.get("experimental") is True or name in {"AIRPLAY", "UPNP"},
            "hardware_acceptance": "MANUAL_REQUIRED" if name in {"AIRPLAY", "UPNP"} else "NOT_ASSESSED",
            "live_provider_health": "NOT_PROBED",
        })
    report.data = {"providers": rows, "evidence": "static_registry_only", "capability_warning": "Registry presence is not implemented playback support, authentication or hardware acceptance."}
    report.checks.append(Check("providers.registry", "PASS", "Static provider contracts read without credentials, adapters or external services."))
    return report
