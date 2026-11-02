from datetime import UTC, datetime, timedelta
from html import escape as html_escape
import asyncio
import base64
import ipaddress
import json
import re
import socket
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urlparse
from uuid import uuid4
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from basswiesn.app.db import get_db
from basswiesn.app.api_models import HealthCheckResponse, ReadinessResponse, StatusResponse, VersionResponse
from basswiesn.app.config import BASSWIESN_TABOO_HOSTS, get_settings, is_safe_radio_host
from basswiesn.app.core.setup_mode import is_yes_confirmation
from basswiesn.app.adapters.ssh import build_legacy_ssh_command
from basswiesn.app.models import ConfigBackup, Device, DeviceActionJournal, DiagnosticEvent, PlayHistory, Preset, PresetProfile, RequestLog, RuntimeState, SetupPlan, Station, TelemetryEvent, Setting, utc_now
from basswiesn.app.services.catalogs import (
    DISPLAY_METADATA_MODES,
    KEY_COMMANDS,
    RADIO_LOG_CLI17000_COMMANDS,
    RADIO_LOG_HTTP_ENDPOINTS,
    RADIO_LOG_SSH_PLAN,
    SETTINGS_CATALOG,
    STOCKHOLM_LANGUAGES,
    TELNET_COMMANDS,
    TIME_ZONES,
)
from basswiesn.app.services.orion import OrionLocationError, StationDescriptor, station_location
from basswiesn.app.services.xml import content_item_xml
from basswiesn.app.services.device_service import device_summary
from basswiesn.app.services.device_state import read_device_state, runtime_from_now_playing, update_runtime_state
from basswiesn.app.services.diagnostics import build_support_bundle, research_diagnostics_snapshot
from basswiesn.app.services.telemetry_analysis import emulation_gaps, redact_mapping, telemetry_export_csv, telemetry_export_json, telemetry_report_html, telemetry_summary
from basswiesn.app.services.capabilities import capability_flags
from basswiesn.app.services.provider_registry import STREAM_SOURCE_PRIORITY, provider_rows, normalize_source_name
from basswiesn.app.services.device_identity_service import DeviceIdentityService
from basswiesn.app.services.config_rewrite import rewrite_hosts, rewrite_sdk_config, verify_hosts_redirect
from basswiesn.app.services.stream_compat import analyze_stream_url
from basswiesn.app.services.tls import ensure_tls_files
from basswiesn.app.services.device_policy import policy_for_device
from basswiesn.app.services.playback_identity import apply_identity, clean_station_name, identity_for_history
from basswiesn.app.services.history_projection import HistoryIdentityProjection
from basswiesn.app.services.history_reports import observed_session, record_client_report
from basswiesn.app.services.offline_mode import external_request_decision, record_dependency
from basswiesn.app.services.network_security import (
    pinned_http_target,
    validate_outbound_host,
    validate_outbound_http_url,
)
from basswiesn.app.services.protected_devices import is_device_access_protected, is_protected_ip, reject_protected_device_access, reject_protected_write_ip, require_unprotected_device
from basswiesn.app.services.feature_status import build_feature_status
from basswiesn.app.services.release_publication import publication_info, refresh_publication
from basswiesn.app.services.device_readiness import cached_readiness, probe_readiness
from basswiesn.app.services.action_journal import record_transport_attempt
from basswiesn.app.services.support_export import SupportBundleTooLarge, build_support_bundle as build_deterministic_support_bundle, redact_payload, redact_text, tail_text
from basswiesn.app.services.filesystem_contract import filesystem_status
from basswiesn.app.services.task_registry import owned_task_status
from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.db.repositories import DeviceIdentityRepository
from basswiesn.app.repositories.research_state_repository import ResearchStateRepository
from basswiesn.app.services.playback_state import is_confirmed_playing
from basswiesn.app.services.artwork import cache_artwork, choose_artwork
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.routers.shared import enforce_ip_write_guard
from basswiesn.app.services.ssh_persistence import WRITE_COMMAND as SSH_PERSISTENCE_COMMAND
from basswiesn.app.services.ssh_persistence import SshPersistenceError, enable_persistence
from basswiesn.app.services.direct_selection import SelectionError, select_direct
from basswiesn.app.services.key_actions import KeyActionError, execute_key
from basswiesn.app.services.device_power import POWER_PATHS, PowerActionError, execute_power
from basswiesn.app.services.wireless_diagnostics import WirelessDiagnosticError, wireless_diagnostics
from basswiesn.app.services.manual_radio_commands import ManualCommand, ManualCommandError, execute_manual_command
from basswiesn.app.services.native_station_search import NativeSearchError, search_native
from basswiesn.app.services.factory_reset import FactoryResetError, request_factory_reset
from basswiesn.app.services.setup_rebuild.candidates import candidate_from_device
from basswiesn.app.services.setup_rebuild.cli17000 import factory_reset as cli_factory_reset
from basswiesn.app.services.setup_rebuild.radio_adapter import RadioSetupAdapter
from basswiesn.app.services.settings_contract import (
    SettingPlan, plan_setting, read_setting, device_name,
)
from basswiesn.app.services.device_settings_writer import apply_settings, SettingsOperationError
from basswiesn.app.services.listener_health import http_service_status as _http_service_status

router = APIRouter(prefix="/api", tags=["api"])
from basswiesn.app.routers import system_api
from basswiesn.app.routers.system_api import api_version, health, system_service_health, feature_status, readiness, system_healthcheck
router.include_router(system_api.router)

_ONLINE_ARTWORK_LOCKS = tuple(asyncio.Lock() for _ in range(32))


def _station_location_or_409(descriptor: StationDescriptor, db: Session, request: Request | None = None) -> str:
    try:
        return station_location(descriptor, db=db, request_host=request.headers.get("host", "") if request else "")
    except OrionLocationError as exc:
        raise HTTPException(status_code=409, detail={"error": str(exc), "hint": "BASSWIESN Host IP setzen"}) from exc
















@router.get("/devices/status-badges")
async def device_status_badges(db: Session = Depends(get_db)) -> list[dict]:
    """DB-only: opening the dashboard never contacts a radio."""
    return [cached_readiness(db, device.device_id) for device in db.query(Device).order_by(Device.name).all()]


@router.post("/devices/{device_id}/diagnostics/readiness")
async def device_readiness_probe(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    if payload.get("confirm_read_only") is not True:
        raise HTTPException(status_code=400, detail={"error": "explicit_read_only_confirmation_required"})
    return await probe_readiness(_device_or_404(db, device_id), db,
                                client_factory=SoundTouchClient, ssh_runner=_run_ssh_readonly_command)


@router.get("/devices/ui-capabilities")
async def device_ui_capabilities(db: Session = Depends(get_db)) -> list[dict]:
    result = []
    for device in db.query(Device).order_by(Device.name).all():
        flags, known = capability_flags(device.capabilities_xml)
        result.append({"device_id": device.device_id, "features": flags, "has_capability_data": known})
    return result


@router.get("/devices/{device_id}/state")
async def device_state(device_id: str, db: Session = Depends(get_db)) -> dict:
    return await read_device_state(_device_or_404(db, device_id), db)


@router.get("/devices/{device_id}/provider-status")
async def device_provider_status(device_id: str, db: Session = Depends(get_db)) -> dict:
    state = await read_device_state(_device_or_404(db, device_id), db)
    runtime = state["runtime_state"]
    sources = {str(item.get("source", "")).upper(): item for item in runtime.get("provider_state", [])}
    availability = {str(item.get("service", "")).upper(): item for item in runtime.get("service_availability", [])}
    parsed_providers = runtime.get("providers") or {}
    providers = []
    for service in provider_rows():
        name = service["name"]
        source = sources.get(name)
        available = availability.get(name)
        status = str((available or source or {}).get("status", "")).upper()
        parsed = parsed_providers.get(name, {})
        providers.append({"name": name, "provider_id": service["provider_id"], "registered": source is not None or bool(parsed), "available": bool(parsed.get("available")) or (available is not None and status not in {"UNAVAILABLE", "ERROR"}), "ready": bool(parsed.get("ready")) or status == "READY", "oauth_dummy_injected": bool(source and source.get("credential_present") and service["auth_model"] == "oauth"), "visible_in_sources": bool(parsed.get("visible_in_sources")) or source is not None, "auth_model": service["auth_model"], "can_add": parsed.get("can_add", service["can_add"]), "can_remove": parsed.get("can_remove", service["can_remove"]), "source_status": parsed.get("source_status", status)})
    return {"device_id": device_id, "providers": providers, "runtime_state": runtime}


@router.get("/diagnostics/telemetry/summary")
async def diagnostics_telemetry_summary(range: str = "24h", db: Session = Depends(get_db)) -> dict:
    return telemetry_summary(db, range)


@router.get("/diagnostics/telemetry/export")
async def diagnostics_telemetry_export(format: str = "json", range: str = "24h", db: Session = Depends(get_db)) -> Response:
    if format == "csv":
        return Response(telemetry_export_csv(db, range), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="basswiesn-telemetry.csv"'})
    return Response(telemetry_export_json(db, range), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="basswiesn-telemetry.json"'})


@router.get("/diagnostics/telemetry/report", response_class=HTMLResponse)
async def diagnostics_telemetry_report(range: str = "24h", db: Session = Depends(get_db)) -> str:
    return telemetry_report_html(db, range)


@router.get("/diagnostics/emulation-gaps")
async def diagnostics_emulation_gaps(db: Session = Depends(get_db)) -> dict:
    return emulation_gaps(db)


def _json_bytes(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _redact_payload(data: object) -> object:
    if isinstance(data, dict):
        return redact_mapping(data)
    if isinstance(data, list):
        return [_redact_payload(item) for item in data]
    if isinstance(data, str):
        return re.sub(r"(?i)(password|token|credential|secret|authorization)([\"'=:\\s]+)([^\\s\"',}]+)", r"\1\2***REDACTED***", data)
    return data


def _setup_job_snapshots(db: Session) -> list[dict]:
    rows = db.query(RuntimeState).filter(RuntimeState.key.like("setup_job:%")).all()
    jobs = []
    for row in rows:
        if row.key == "setup_job:latest":
            continue
        try:
            jobs.append(json.loads(row.value or "{}"))
        except ValueError:
            jobs.append({"key": row.key, "parse_error": True})
    return jobs


def _battery_snapshot(db: Session) -> list[dict]:
    return [{
        "feature": "battery_polling",
        "status": "removed",
        "version": get_settings().version,
        "message": "Regelmaessige Batterieabfragen sind im aktuellen BASSWIESN-Release deaktiviert. Historische Telemetrie bleibt in der Datenbank, wird aber nicht aktiv aktualisiert.",
    }]


# Compatibility aliases; new code uses the dedicated runtime health service.
from basswiesn.app.services.runtime_health import (
    manifest_candidates as _manifest_candidates, validate_manifest_data as _validate_manifest_data,
    release_healthcheck as _release_healthcheck,
)




def _manifest_status(*, required: bool | None = None) -> dict:
    from basswiesn.app.services import runtime_health
    return runtime_health.manifest_status(required=required, candidates=_manifest_candidates())








@router.get("/support-bundle")
async def global_support_bundle(db: Session = Depends(get_db)) -> StreamingResponse:
    settings = get_settings()
    masterlog_path = settings.data_dir / "logs" / "master.log"
    devices = [_redact_payload(device_summary(device)) for device in db.query(Device).order_by(Device.name).all()]
    files = {
        "version.json": {"version": settings.version, "update_channel": settings.update_channel},
        "manifest.json": _manifest_status(),
        "healthcheck.json": _release_healthcheck(db),
        "devices.json": devices,
        "emulator_gaps.json": emulation_gaps(db),
        "setup_jobs.json": _setup_job_snapshots(db),
        "battery_polling_removed.json": _battery_snapshot(db),
        "research_diagnostics.json": research_diagnostics_snapshot(db),
    }
    try:
        masterlog = tail_text(masterlog_path, max_lines=1000) or "master.log not available\n"
        entries = {filename: _json_bytes(_redact_payload(payload)) for filename, payload in files.items()}
        entries["masterlog.txt"] = redact_text(str(_redact_payload(masterlog)))
        bundle = build_deterministic_support_bundle(
            entries,
            max_bytes=max(1, int(getattr(settings, "support_bundle_max_mb", 50))) * 1024 * 1024,
            metadata={"kind": "global-support", "version": settings.version},
        )
    except SupportBundleTooLarge as exc:
        raise HTTPException(status_code=413, detail="support bundle exceeds the configured size limit") from exc
    return StreamingResponse(bundle, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="support_bundle.zip"'})


@router.get("/devices/{device_id}/support-bundle")
async def support_bundle(device_id: str, db: Session = Depends(get_db)) -> StreamingResponse:
    device = _device_or_404(db, device_id)
    try:
        bundle = await build_support_bundle(device, db)
    except SupportBundleTooLarge as exc:
        raise HTTPException(status_code=413, detail="support bundle exceeds the configured size limit") from exc
    filename = f"basswiesn-support-{re.sub(r'[^A-Za-z0-9_.-]', '_', device.device_id)}.zip"
    return StreamingResponse(bundle, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/diagnostics/support-bundle")
async def diagnostics_support_bundle(device_id: str = "", db: Session = Depends(get_db)) -> StreamingResponse:
    query = db.query(Device)
    device = query.filter(Device.device_id == device_id).one_or_none() if device_id else query.order_by(Device.last_seen.desc()).first()
    if device is None:
        raise HTTPException(status_code=404, detail="no device available for support bundle")
    return await support_bundle(device.device_id, db)


def _memory_check_plan(device: Device) -> dict:
    return {
        "required_before_write": True,
        "device_id": device.device_id,
        "ip_address": device.ip_address,
        "checks": [
            "GET /info reachable on port 8090",
            "GET /supportedURLs contains target endpoint",
            "backup of current /presets and relevant settings exists",
            "for SSH/config writes: read /mnt/nv state with old POSIX-safe commands only",
        ],
        "legacy_shell_note": "Radios are 2013-era embedded Linux. Prefer sh, cat, ls, df, du, grep, sed, awk, curl/wget; avoid modern bashisms and GNU-only options.",
    }


def _require_memory_checked(device: Device, payload: dict) -> None:
    if payload.get("dry_run", True):
        return
    if not payload.get("memory_checked"):
        raise HTTPException(status_code=409, detail={"error": "memory check required before radio write", "memory_check": _memory_check_plan(device)})


SETUP_HOST_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _validated_setup_host(host: object) -> str:
    value = str(host or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="basswiesn LAN host is required")
    forbidden = [';', ' ', '\t', '\n', '\r', '"', "'", '$', '`', '\\', '/', '<', '>', '&', '|']
    if any(ch in value for ch in forbidden) or not SETUP_HOST_RE.match(value):
        raise HTTPException(status_code=400, detail="host must be a plain LAN hostname or IPv4 address")
    if value in {"127.0.0.1", "localhost"}:
        raise HTTPException(status_code=400, detail="use the LAN IP of the computer/Raspberry, not localhost")
    if value == "content.api.bose.io":
        raise HTTPException(status_code=400, detail="content.api.bose.io is the Bose cloud host, not the BASSWIESN LAN host")
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="host must be a reachable LAN IPv4 address") from exc
    if not is_safe_radio_host(str(parsed)) or is_protected_ip(str(parsed)):
        raise HTTPException(status_code=400, detail="host must be a reachable LAN IPv4 address, not localhost or Docker")
    return value


def _cloud_route_targets(host: str, port: int) -> dict:
    base = f"http://{host}:{port}"
    return {
        "margeServerUrl": base,
        "statsServerUrl": base,
        "swUpdateUrl": f"{base}/updates/soundtouch",
        "bmxRegistryUrl": f"{base}/bmx/registry/v1/services",
        "dns_override": {"content.api.bose.io": host, "streaming.bose.com": host},
        "device_hosts_candidates": [
            f"{host} content.api.bose.io",
            f"{host} streaming.bose.com",
            f"{host} downloads.bose.com",
        ],
    }


def _outbound_lan_ip() -> str:
    candidates = _lan_ip_candidates()
    return candidates[0]["ip"] if candidates else ""


def _lan_ip_candidates() -> list[dict]:
    candidates: list[dict] = []

    def add(ip: str, source: str) -> None:
        if not is_safe_radio_host(ip) or is_protected_ip(ip):
            return
        if Path("/.dockerenv").exists() and ipaddress.ip_address(ip) in ipaddress.ip_network("172.16.0.0/12"):
            return
        if any(token in source.lower() for token in ("docker", "veth", "br-")):
            return
        parsed = ipaddress.ip_address(ip)
        if any(item["ip"] == ip for item in candidates):
            return
        octets = ip.split(".")
        cidr = ".".join(octets[:3]) + ".0/24" if len(octets) == 4 else ""
        private_rank = 0 if parsed.is_private else 1
        vpn_rank = 1 if ip.startswith(("10.", "172.")) else 0
        candidates.append({"ip": ip, "source": source, "suggested_cidr": cidr, "rank": private_rank + vpn_rank})

    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(ip, "hostname")
    except OSError:
        pass
    try:
        output = subprocess.run(["ip", "-o", "-4", "addr", "show", "scope", "global"], capture_output=True, text=True, timeout=2).stdout
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 4 and "/" in parts[3]:
                add(parts[3].split("/", 1)[0], f"interface:{parts[1]}")
    except Exception:
        pass
    return sorted(candidates, key=lambda item: (item["rank"], item["ip"]))


def _setup_backup_status(device: Device, db: Session) -> dict:
    paths = [row.path for row in db.query(ConfigBackup).filter(ConfigBackup.device_id == device.device_id).all()]
    required_any = ["setup-route/before.json", "http8090:/info", "http8090:/presets", "http8090:/sources", "http8090:/supportedURLs"]
    found = {path: any(path == item or item.endswith(path.replace("http8090:", "")) for item in paths) for path in required_any}
    enough = found.get("setup-route/before.json", False) or sum(1 for ok in found.values() if ok) >= 3
    return {"ready": enough, "found": found, "saved_paths": sorted(paths)[-20:], "rule": "setup-route/before.json or at least three HTTP setup backups must exist before treating memory as checked"}


def _ssh_fallback_plan(host: str, port: int) -> dict:
    targets = _cloud_route_targets(host, port)
    b64_note = "Use SSH only if CLI 17000 is unavailable and a full backup exists. Write /mnt/nv/SoundTouchSdkPrivateCfg.xml or firmware-supported override, never require OverrideSdkPrivateCfg.xml to exist."
    hosts = "\n".join(f"{host} {domain}" for domain in ["content.api.bose.io", "streaming.bose.com", "bmx.bose.com", "events.api.bosecm.com", "worldwide.bose.com", "bose.vtuner.com", "bose2.vtuner.com"])
    return {
        "enabled": False,
        "reason": "Fallback plan only until SSH backup/restore is validated on this model.",
        "note": b64_note,
        "targets": targets,
        "read_first": ["cat /opt/Bose/etc/SoundTouchSdkPrivateCfg.xml 2>/dev/null", "cat /mnt/nv/SoundTouchSdkPrivateCfg.xml 2>/dev/null", "cat /mnt/nv/OverrideSdkPrivateCfg.xml 2>/dev/null", "cat /etc/hosts 2>/dev/null", "getpdo CurrentSystemConfiguration 2>/dev/null || true"],
        "write_plan": [
            "cp existing SDK config to /mnt/nv/SoundTouchSdkPrivateCfg.xml.basswiesn-backup before editing",
            "replace margeServerUrl/statsServerUrl/swUpdateUrl/bmxRegistryUrl with basswiesn HTTP URLs",
            "update /etc/hosts with a marked basswiesn block if DNS override is needed",
            "sync; reboot",
        ],
        "hosts_block_preview": hosts,
    }


def _tcp_port_open(host: str, port: int, timeout: float = 1.0) -> tuple[bool, str]:
    if is_protected_ip(host):
        return False, "protected device access blocked"
    validation = validate_outbound_host(host, port=port)
    if not validation.ok:
        return False, validation.reason
    target = validation.addresses[0]
    try:
        reject_protected_device_access(target, action="TCP port probe", requester="api", method="TCP", endpoint=str(port))
    except Exception:
        return False, "protected device access blocked"
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return True, "open"
    except OSError as exc:
        return False, f"Connection failed ({type(exc).__name__})"


def _setup_wizard_steps() -> list[dict]:
    return [
        {"key": "server", "title": "basswiesn server", "required": True, "detail": "Use the LAN IP reachable by the radio, never 127.0.0.1."},
        {"key": "radio", "title": "Find radio", "required": True, "detail": "Read /info on port 8090 and check CLI 17000 before writing."},
        {"key": "backup", "title": "Capture backup", "required": True, "detail": "Save /info, /supportedURLs, /presets, /sources, /now_playing and current route before changes."},
        {"key": "route", "title": "Write cloud route", "required": True, "detail": "Use the confirmed two-URL envswitch command; it writes the persistent SystemConfiguration PB store."},
        {"key": "reboot", "title": "Reboot and wait", "required": True, "detail": "envswitch persists the route but does not activate it immediately; an explicit reboot is required."},
        {"key": "verify", "title": "Verify", "required": True, "detail": "Read current values, radio endpoints and local BMX registry after reboot."},
        {"key": "rollback", "title": "Rollback ready", "required": True, "detail": "Keep setup-route/before.json so the previous route can be restored."},
    ]



def _setup_cli17000_commands(targets: dict, reboot: bool = False) -> list[str]:
    # boseurls persists Marge + update. BMX and stats are independent fields in
    # CurrentSystemConfiguration and must be written explicitly after a factory
    # reset; otherwise they keep pointing at the previous server.
    commands = [
        f'envswitch boseurls set "{targets["margeServerUrl"]}" "{targets["swUpdateUrl"]}"',
        f'sys configuration bmxRegistryUrl {targets["bmxRegistryUrl"]}',
        f'sys configuration margeServerUrl {targets["margeServerUrl"]}',
        f'sys configuration swUpdateUrl {targets["swUpdateUrl"]}',
        f'sys configuration statsServerUrl {targets["statsServerUrl"]}',
    ]
    if reboot:
        # Live test 2026-06-20: envswitch returns OK but does not reboot by
        # itself. The new route becomes visible only after an explicit reboot.
        commands.append("sys reboot")
    return commands


def _setup_cli17000_commands_from_values(values: dict, reboot: bool = False) -> list[str]:
    required = {tag: values.get(tag, "") for tag in CLOUD_ROUTE_TAGS}
    commands = [
        f'envswitch boseurls set "{required["margeServerUrl"]}" "{required["swUpdateUrl"]}"',
        f'sys configuration bmxRegistryUrl {required["bmxRegistryUrl"]}',
        f'sys configuration statsServerUrl {required["statsServerUrl"]}',
    ]
    if reboot:
        commands.append("sys reboot")
    return commands


async def _send_cli17000(ip_address: str, commands: list[str], timeout: float = 8.0) -> str:
    reject_protected_write_ip(
        ip_address,
        action="cli17000",
        requester="api",
        method="TELNET",
        endpoint="cli17000",
    )
    validation = validate_outbound_host(ip_address, port=17000)
    if not validation.ok:
        raise PermissionError(validation.reason)
    target = validation.addresses[0]

    def run() -> str:
        chunks: list[bytes] = []
        with socket.create_connection((target, 17000), timeout=timeout) as sock:
            sock.settimeout(0.8)
            time.sleep(0.25)
            for command in commands:
                sock.sendall((command + "\n").encode("utf-8"))
                time.sleep(0.75)
                while True:
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        break
                    if not data:
                        break
                    chunks.append(data)
        return b"".join(chunks).decode("utf-8", errors="replace")
    def is_write(command: str) -> bool:
        normalized = " ".join(str(command or "").strip().lower().split())
        return not (
            normalized.startswith("getpdo ")
            or normalized.endswith(" get")
            or normalized == "sys configuration"
        )

    writes = [command for command in commands if is_write(command)]
    started = time.monotonic()
    try:
        output = await asyncio.to_thread(run)
        if writes:
            record_transport_attempt(
                ip_address=ip_address,
                action="CLI17000 legacy batch",
                trigger="api",
                requested_state={"commands": writes},
                result="command_sent",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        return output
    except Exception as exc:
        if writes:
            record_transport_attempt(
                ip_address=ip_address,
                action="CLI17000 legacy batch",
                trigger="api",
                requested_state={"commands": writes},
                result="failed",
                duration_ms=int((time.monotonic() - started) * 1000),
                error_category=exc.__class__.__name__,
            )
        raise


CLOUD_ROUTE_TAGS = ("bmxRegistryUrl", "margeServerUrl", "swUpdateUrl", "statsServerUrl")


def _extract_cloud_route_values(payload: str) -> dict:
    values: dict[str, str] = {}
    if not payload:
        return values
    for tag in CLOUD_ROUTE_TAGS:
        tag_match = re.search(rf"<{tag}>\s*([^<]+?)\s*</{tag}>", payload, re.IGNORECASE)
        if tag_match:
            values[tag] = tag_match.group(1).strip()
            continue
        key_match = re.search(rf"\b{tag}\b\s*[:=]\s*([^\s<]+)", payload, re.IGNORECASE)
        if key_match:
            values[tag] = key_match.group(1).strip()
            continue
        cli_block_match = re.search(rf"\b{tag}\b\s*\{{\s*text:\s*\"([^\"]+)\"", payload, re.IGNORECASE)
        if cli_block_match:
            values[tag] = cli_block_match.group(1).strip()
    return values


def _cloud_route_diff(current: dict, targets: dict) -> list[dict]:
    return [
        {"tag": tag, "current": current.get(tag, ""), "target": targets.get(tag, ""), "changed": current.get(tag, "") != targets.get(tag, "")}
        for tag in CLOUD_ROUTE_TAGS
    ]


def _cloud_route_diff_text(diff: list[dict]) -> str:
    lines: list[str] = []
    for row in diff:
        lines.append(f"<{row['tag']}>")
        lines.append(f"  - {row['current'] or '(not found)'}")
        lines.append(f"  + {row['target'] or '(not set)'}")
    return "\n".join(lines)


async def _read_current_cloud_route(device: Device) -> dict:
    current: dict[str, str] = {}
    sources: dict[str, object] = {}
    if device.info_xml:
        cached_values = _extract_cloud_route_values(device.info_xml)
        cached_marge = _xml_text(device.info_xml, "margeURL")
        if cached_marge and "margeServerUrl" not in cached_values:
            cached_values["margeServerUrl"] = cached_marge
        current.update(cached_values)
        sources["cached_info"] = {"ok": True, "values": cached_values}
    try:
        info_xml = await SoundTouchClient(device.ip_address).get_xml("/info")
        info_values = _extract_cloud_route_values(info_xml)
        marge = _xml_text(info_xml, "margeURL")
        if marge and "margeServerUrl" not in info_values:
            info_values["margeServerUrl"] = marge
        current.update(info_values)
        sources["http_info"] = {"ok": True, "values": info_values}
    except Exception as exc:
        sources["http_info"] = {"ok": False, "error": str(exc)}
    try:
        cli_output = await _send_cli17000(device.ip_address, ["getpdo CurrentSystemConfiguration"], timeout=5.0)
        cli_values = _extract_cloud_route_values(cli_output)
        current.update(cli_values)
        sources["cli17000_getpdo"] = {"ok": True, "values": cli_values, "raw": cli_output}
    except Exception as exc:
        sources["cli17000_getpdo"] = {"ok": False, "error": str(exc)}
    return {"values": current, "sources": sources}


async def _run_ssh_readonly_command(ip_address: str, username: str, command: str, timeout: int = 12) -> dict:
    if is_protected_ip(ip_address):
        write_masterlog(
            "protected_device_write_blocked",
            radio_ip=ip_address,
            action="ssh_readonly",
            requester="api",
            method="SSH",
            endpoint="ssh",
        )
        return {
            "returncode": 126,
            "stdout": "",
            "stderr": "protected device: SSH is blocked by PROTECTED_DEVICE_IPS",
        }
    validation = validate_outbound_host(ip_address, port=22)
    if not validation.ok:
        return {"returncode": 126, "stdout": "", "stderr": validation.reason}
    target = validation.addresses[0]

    def run() -> dict:
        proc = subprocess.run(
            build_legacy_ssh_command(target, username, command),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    return await asyncio.to_thread(run)


async def _read_ssh_setup_override(ip_address: str, username: str = "root") -> dict:
    path = "/mnt/nv/OverrideSdkPrivateCfg.xml"
    stock_path = "/opt/Bose/etc/SoundTouchSdkPrivateCfg.xml"
    result = await _run_ssh_readonly_command(
        ip_address,
        username,
        f'if test -s {path}; then echo __OVERRIDE__; cat {path}; elif test -s {stock_path}; then echo __STOCK__; cat {stock_path}; fi',
    )
    if result["returncode"] != 0:
        return {"present": False, "path": path, "error": result["stderr"].strip()}
    output = result["stdout"]
    source = "override" if output.startswith("__OVERRIDE__\n") else "stock" if output.startswith("__STOCK__\n") else ""
    content = output.split("\n", 1)[1] if source else ""
    return {"present": source == "override", "template_present": bool(source), "source": source, "path": path, "content": content}


async def _read_ssh_hosts(ip_address: str, username: str = "root") -> dict:
    path = "/etc/hosts"
    try:
        result = await _run_ssh_readonly_command(ip_address, username, f"cat {path}")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "path": path, "error": f"SSH_READ_FAILED ({type(exc).__name__})", "message": "CLI-only ist möglich. Full redirect benötigt SSH/remote_services."}
    if result["returncode"] != 0:
        return {"available": False, "path": path, "error": "SSH_READ_FAILED", "message": "CLI-only ist möglich. Full redirect benötigt SSH/remote_services."}
    return {"available": True, "path": path, "content": result["stdout"]}


async def _write_ssh_hosts(ip_address: str, hosts_text: str, target_host: str, username: str = "root") -> dict:
    reject_protected_write_ip(
        ip_address,
        action="ssh_write_hosts",
        requester="api",
        method="SSH",
        endpoint="/etc/hosts",
    )
    validation = validate_outbound_host(ip_address, port=22)
    if not validation.ok:
        raise PermissionError(validation.reason)
    target = validation.addresses[0]
    path = "/etc/hosts"
    backup_path = "/mnt/nv/etc-hosts.basswiesn-backup"
    encoded = base64.b64encode(hosts_text.encode("utf-8")).decode("ascii")
    command = (
        f"set -e; test -f {path}; test -f {backup_path} || cp {path} {backup_path}; "
        "trap 'mount -o remount,ro / >/dev/null 2>&1 || true' 0; mount -o remount,rw /; "
        f"printf %s {encoded} | base64 -d > {path}.basswiesn-new; "
        f"chown 0:0 {path}.basswiesn-new; chmod 644 {path}.basswiesn-new; "
        f"mv {path}.basswiesn-new {path}; sync; mount -o remount,ro /"
    )

    def run() -> dict:
        proc = subprocess.run(
            build_legacy_ssh_command(target, username, command),
            capture_output=True,
            text=True,
            timeout=20,
        )
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}

    result = await asyncio.to_thread(run)
    if result["returncode"] != 0:
        raise OSError(result["stderr"].strip() or "SSH /etc/hosts write failed")
    verify = await _read_ssh_hosts(ip_address, username)
    verification = verify_hosts_redirect(verify.get("content", ""), target_host)
    if not verification["ok"]:
        raise OSError(f"SSH /etc/hosts verification failed: {verification['missing_domains']}")
    return {"ok": True, "path": path, "backup_path": backup_path, "verification": verification}


async def _write_ssh_setup_override(ip_address: str, xml_text: str, username: str = "root") -> dict:
    reject_protected_write_ip(
        ip_address,
        action="ssh_write_setup_override",
        requester="api",
        method="SSH",
        endpoint="/mnt/nv/OverrideSdkPrivateCfg.xml",
    )
    validation = validate_outbound_host(ip_address, port=22)
    if not validation.ok:
        raise PermissionError(validation.reason)
    target = validation.addresses[0]
    path = "/mnt/nv/OverrideSdkPrivateCfg.xml"
    encoded = base64.b64encode(xml_text.encode("utf-8")).decode("ascii")
    command = (
        f"set -e; test -f {path}.basswiesn-backup || cp {path} {path}.basswiesn-backup 2>/dev/null || cp /opt/Bose/etc/SoundTouchSdkPrivateCfg.xml {path}.basswiesn-backup; "
        f"printf %s {encoded} | base64 -d > {path}.basswiesn-new; "
        f"chown 0:0 {path}.basswiesn-new; chmod 644 {path}.basswiesn-new; "
        f"mv {path}.basswiesn-new {path}; sync"
    )

    def run() -> dict:
        proc = subprocess.run(
            build_legacy_ssh_command(target, username, command),
            capture_output=True,
            text=True,
            timeout=20,
        )
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}

    result = await asyncio.to_thread(run)
    if result["returncode"] != 0:
        raise OSError(result["stderr"].strip() or "SSH override write failed")
    verify = await _read_ssh_setup_override(ip_address, username)
    if verify.get("content", "").strip() != xml_text.strip():
        raise OSError("SSH override write verification failed")
    return {"ok": True, "path": path, "backup_path": f"{path}.basswiesn-backup"}


async def _wait_for_radio_http(device: Device, initial_delay: int = 60, attempts: int = 12, interval: int = 5) -> dict:
    if initial_delay > 0:
        await asyncio.sleep(initial_delay)
    client = SoundTouchClient(device.ip_address)
    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            xml = await client.get_xml("/info")
            return {"ok": True, "attempt": attempt, "initial_delay_seconds": initial_delay, "summary": _summarize_payload(xml)}
        except Exception as exc:
            errors.append(str(exc))
            if attempt < attempts:
                await asyncio.sleep(interval)
    return {"ok": False, "attempts": attempts, "initial_delay_seconds": initial_delay, "last_error": errors[-1] if errors else "unknown"}


async def _capture_radio_logs(device: Device, db: Session, reason: str = "manual", include_cli: bool = True) -> dict:
    client = SoundTouchClient(device.ip_address)
    captured: list[dict] = []
    failed: list[dict] = []
    for endpoint in RADIO_LOG_HTTP_ENDPOINTS:
        try:
            payload = await client.get_xml(endpoint)
        except Exception as exc:
            failed.append({"source": "http", "endpoint": endpoint, "error": str(exc)})
            continue
        event = TelemetryEvent(
            device_id=device.device_id,
            event_type=f"radio_log_http:{reason}",
            endpoint=endpoint,
            payload=payload,
            parsed_summary=_summarize_payload(payload),
        )
        db.add(event)
        db.add(ConfigBackup(device_id=device.device_id, path=f"radio-log/{reason}/http{endpoint}", content=payload))
        captured.append({"source": "http", "endpoint": endpoint, "bytes": len(payload)})
        if endpoint == "/info":
            device.info_xml = payload
        elif endpoint in {"/supportedURLs", "/capabilities"} and not device.capabilities_xml:
            device.capabilities_xml = payload
    if include_cli:
        try:
            cli_output = await _send_cli17000(device.ip_address, RADIO_LOG_CLI17000_COMMANDS, timeout=5.0)
        except Exception as exc:
            failed.append({"source": "cli17000", "endpoint": "commands", "error": str(exc), "commands": RADIO_LOG_CLI17000_COMMANDS})
        else:
            event = TelemetryEvent(
                device_id=device.device_id,
                event_type=f"radio_log_cli17000:{reason}",
                endpoint="cli17000",
                payload=cli_output,
                parsed_summary=_summarize_payload(cli_output),
            )
            db.add(event)
            db.add(ConfigBackup(device_id=device.device_id, path=f"radio-log/{reason}/cli17000.txt", content=cli_output))
            captured.append({"source": "cli17000", "endpoint": "commands", "bytes": len(cli_output), "commands": RADIO_LOG_CLI17000_COMMANDS})
    device.last_seen = utc_now()
    return {"reason": reason, "captured": captured, "failed": failed, "ssh_readonly_plan": RADIO_LOG_SSH_PLAN}


async def _safe_http_setup_backups(device: Device, db: Session) -> dict:
    client = SoundTouchClient(device.ip_address)
    endpoints = ["/info", "/supportedURLs", "/presets", "/sources", "/now_playing", "/getZone"]
    saved: list[str] = []
    failed: list[dict] = []
    for endpoint in endpoints:
        try:
            xml = await client.get_xml(endpoint)
        except Exception as exc:
            failed.append({"path": endpoint, "error": str(exc)})
            continue
        db.add(ConfigBackup(device_id=device.device_id, path=f"http8090:{endpoint}", content=xml))
        saved.append(endpoint)
        if endpoint == "/info":
            device.info_xml = xml
        elif endpoint == "/supportedURLs":
            device.capabilities_xml = xml
    return {"saved": saved, "failed": failed}


def _validate_device_name(name: object) -> str:
    try:
        return device_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _language_codes() -> set[str]:
    return {item["code"] for item in STOCKHOLM_LANGUAGES}


def _summarize_payload(payload: str) -> str:
    compact = " ".join(payload.replace("\n", " ").split())
    return compact[:240]



def _xml_text(xml: str, path: str) -> str:
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    found = root.find(path)
    return found.text.strip() if found is not None and found.text else ""


def _short_firmware(value: str) -> str:
    match = re.search(r"(\d+\.\d+\.\d+)", value or "")
    return match.group(1) if match else value


def _device_summary(device: Device) -> dict:
    return device_summary(device)


def _guided_setup_steps(device: Device) -> list[dict]:
    summary = _device_summary(device)
    return [
        {"key": "identify", "title": "Gerät identifizieren", "status": "ready" if summary["has_info"] else "pending", "action": "GET /info und /capabilities abrufen"},
        {"key": "backup", "title": "Backup erstellen", "status": "pending", "action": "Config und aktuelle Presets sichern, vor Writes Pflicht"},
        {"key": "cloud_route", "title": "Cloud-Route setzen", "status": "pending", "action": "Bose Hostnames auf basswiesn :1516 routen"},
        {"key": "settings", "title": "Geräteeinstellungen", "status": "pending", "action": "Name, Sprache, Bass, Clock, Power Saving setzen"},
        {"key": "presets", "title": "Preset-Profil anwenden", "status": "pending", "action": "Benanntes 6er-Profil auf Slots 1-6 schreiben"},
        {"key": "verify", "title": "Verify", "status": "pending", "action": "/info, /sources, /presets, /now_playing, /getZone prüfen"},
    ]


PERSISTENT_SSH_PLAN = [
    "Firmware remote_services_enabled returns true when /etc/remote_services, /mnt/nv/remote_services or /tmp/remote_services exists.",
    "Temporary SSH bootstrap uses /tmp/remote_services plus /etc/init.d/sshd start.",
    "Persistent SSH marker is /mnt/nv/remote_services; this survives reboot on the device persistence partition.",
    "Mathias toolkit also has an rc.local fallback that touches /tmp/remote_services and starts sshd at boot; basswiesn keeps that as manual lab information only.",
    "Before enabling: verify live /info against the exact stored hardware profile and read the fixed marker state over existing SSH.",
    "Persist a hashed START snapshot and intent, recheck preconditions, then write once and independently read back. Reboot persistence remains unverified; no reboot is performed.",
]


def _persistent_ssh_command() -> str:
    return SSH_PERSISTENCE_COMMAND

def _device_or_404(db: Session, device_id: str) -> Device:
    device = db.query(Device).filter(Device.device_id == device_id).one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    return device





def _soundtouch_client_for(device: Device, *, purpose: str, trigger: str = "", policy=None) -> SoundTouchClient:
    try:
        return SoundTouchClient(
            device.ip_address,
            device_id=device.device_id,
            request_purpose=purpose,
            trigger=trigger,
            policy_context=policy.to_dict() if policy is not None else None,
        )
    except TypeError:
        return SoundTouchClient(device.ip_address)


def _safe_source_token(value: object, default: str = "TUNEIN") -> str:
    token = str(value or default).strip().upper()
    if not re.fullmatch(r"[A-Z0-9_:-]{1,64}", token):
        raise HTTPException(status_code=400, detail="invalid source token")
    return token


def _safe_source_account(value: object) -> str:
    account = str(value or "").strip()
    if len(account) > 128 or any(ch in account for ch in "<>\"&"):
        raise HTTPException(status_code=400, detail="invalid source account")
    return account


def _search_station_xml(source: object, source_account: object, query: object) -> str:
    query_text = str(query or "").strip()
    if not query_text:
        raise HTTPException(status_code=400, detail="search query is required")
    if len(query_text) > 120:
        raise HTTPException(status_code=400, detail="search query is too long")
    source_text = _safe_source_token(source)
    account = _safe_source_account(source_account)
    account_attr = f' sourceAccount="{html_escape(account, quote=True)}"' if account else ""
    return f'<search source="{source_text}"{account_attr}>{html_escape(query_text)}</search>'


def _add_station_xml(source: object, source_account: object, token: object, name: object) -> str:
    token_text = str(token or "").strip()
    station_name = str(name or "").strip()
    if not token_text or len(token_text) > 256 or any(ch in token_text for ch in "<>\"&"):
        raise HTTPException(status_code=400, detail="valid station token is required")
    if not station_name or len(station_name) > 128:
        raise HTTPException(status_code=400, detail="station name is required")
    source_text = _safe_source_token(source)
    account = _safe_source_account(source_account)
    account_attr = f' sourceAccount="{html_escape(account, quote=True)}"' if account else ""
    return f'<addStation source="{source_text}"{account_attr} token="{html_escape(token_text, quote=True)}"><name>{html_escape(station_name)}</name></addStation>'


def _parse_search_station_results(xml: str) -> dict:
    root = _display_xml_root(xml)
    if root is None:
        return {"songs": [], "artists": [], "stations": [], "raw_summary": _summarize_payload(xml)}
    result: dict[str, list[dict]] = {"songs": [], "artists": [], "stations": []}
    for group in result:
        for node in root.findall(f".//{group}/searchResult"):
            result[group].append({
                "source": node.attrib.get("source", root.attrib.get("source", "")),
                "sourceAccount": node.attrib.get("sourceAccount", root.attrib.get("sourceAccount", "")),
                "token": node.attrib.get("token", ""),
                "name": node.findtext("name", ""),
                "artist": node.findtext("artist", ""),
                "album": node.findtext("album", ""),
                "logo": node.findtext("logo", ""),
                "description": node.findtext("description", ""),
            })
    result["raw_summary"] = _summarize_payload(xml)
    return result


def _parse_bass_capabilities(xml: str) -> dict:
    return {
        "deviceID": _display_xml_root(xml).attrib.get("deviceID", "") if _display_xml_root(xml) is not None else "",
        "bassAvailable": (_xml_text(xml, "bassAvailable") or "").lower() == "true",
        "bassMin": int(_xml_text(xml, "bassMin") or -9),
        "bassMax": int(_xml_text(xml, "bassMax") or 0),
        "bassDefault": int(_xml_text(xml, "bassDefault") or 0),
        "raw_summary": _summarize_payload(xml),
    }


def _parse_zone_summary(xml: str) -> dict:
    root = _display_xml_root(xml)
    if root is None:
        return {"master": "", "members": [], "raw_summary": _summarize_payload(xml)}
    members = []
    for node in root.findall(".//member"):
        members.append({"deviceID": (node.text or "").strip(), "ipaddress": node.attrib.get("ipaddress", "")})
    return {"master": root.attrib.get("master", ""), "members": members, "raw_summary": _summarize_payload(xml)}


def _name_source_plan_xml(source: object, name: object, source_account: object = "", raw_xml: object = "") -> str:
    custom = str(raw_xml or "").strip()
    if custom:
        if "<nameSource" not in custom or len(custom) > 1000:
            raise HTTPException(status_code=400, detail="custom nameSource XML must contain <nameSource and be short")
        return custom
    source_text = _safe_source_token(source)
    account = _safe_source_account(source_account)
    display_name = str(name or "").strip()
    if not display_name or len(display_name) > 63 or any(ch in display_name for ch in "<>\"&"):
        raise HTTPException(status_code=400, detail="source label is required and must be XML-safe")
    account_attr = f' sourceAccount="{html_escape(account, quote=True)}"' if account else ""
    return f'<nameSource source="{source_text}"{account_attr}><name>{html_escape(display_name)}</name></nameSource>'

def _setting_plan(setting: str, value: object) -> SettingPlan:
    try:
        return plan_setting(setting, value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _setting_payload(setting: str, value: object) -> tuple[str, str]:
    plan = _setting_plan(setting, value)
    return plan.path, plan.xml


def _setting_value(setting: str, xml_text: str) -> object:
    return read_setting(setting, xml_text)


async def _apply_settings_transaction(db: Session, device: Device, plans: list[SettingPlan],
                                      payload: dict, art_mode: str | None = None) -> dict:
    previous = payload.get("expected_previous")
    previous_keys = {p.setting for p in plans} | ({"station_art_mode"} if art_mode is not None else set())
    if previous is not None and (not isinstance(previous, dict) or set(previous) - previous_keys):
        raise HTTPException(status_code=400, detail="expected_previous must map requested settings")
    try:
        return await apply_settings(db, device, plans, client_factory=SoundTouchClient,
                                    guard=enforce_ip_write_guard, expected_previous=previous, art_mode=art_mode)
    except SettingsOperationError as exc:
        raise HTTPException(status_code=409, detail=exc.result) from None


@router.post("/devices/{device_id}/rename")
async def rename_device(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    plan = _setting_plan("name", payload.get("name"))
    preview = {"dry_run": True, "device_id": device.device_id, "target": device.ip_address,
               "path": plan.path, "xml": plan.xml}
    if payload.get("dry_run", True):
        return preview
    result = await _apply_settings_transaction(db, device, [plan], payload)
    change = next(iter(result["changed"]), {})
    return {**preview, **result, "dry_run": False, "verified": plan.expected,
            "response": change.get("response", ""), "transport_uncertain": change.get("transport_uncertain", False),
            "persistence": change.get("persistence", {"updated": False, "reason": "Already current; reboot persistence not tested"})}


@router.get("/devices/{device_id}/settings")
async def device_settings(device_id: str, probe: bool = False, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    specs = [
        {"key": "volume", "label": "Volume", "kind": "range", "min": 0, "max": 100, "path": "/volume"},
        {"key": "bass", "label": "Bass", "kind": "range", "min": -9, "max": 0, "path": "/bass"},
        {"key": "clockDisplay", "label": "Clock display", "kind": "choice", "values": ["true", "false"], "path": "/clockDisplay"},
        {"key": "clockConfig", "label": "Timezone / time format", "kind": "clockConfig", "values": TIME_ZONES, "path": "/clockDisplay"},
        {"key": "language", "label": "Language", "kind": "choice", "values": [item["code"] for item in STOCKHOLM_LANGUAGES], "path": "/language"},
        {"key": "systemtimeout", "label": "System timeout", "kind": "number", "path": "/systemtimeout"},
        {"key": "auto_standby_minutes", "label": "Auto standby minutes", "kind": "number", "path": "/systemtimeout"},
        {"key": "powersaving", "label": "Power saving", "kind": "choice", "values": ["true", "false"], "path": "/systemtimeout"},
        {"key": "rebroadcastlatencymode", "label": "Rebroadcast latency", "kind": "choice", "values": ["SYNC_TO_ZONE", "SYNC_TO_ROOM"], "path": "/rebroadcastlatencymode"},
    ]
    current: dict[str, object] = {}
    raw: dict[str, str] = {}
    now_playing: dict[str, object] = {}
    if probe:
        require_unprotected_device(device, action="settings_read", requester="settings_probe")
        client = SoundTouchClient(device.ip_address, device_id=device.device_id,
                                  request_purpose="settings_read", trigger="explicit_settings_probe")
        info_xml = await client.get_xml("/info")
        info = ET.fromstring(info_xml)
        if info.tag != "info" or info.get("deviceID", "").strip().upper() != device.device_id.strip().upper():
            raise HTTPException(status_code=409, detail="IDENTITY_MISMATCH: settings probe stopped")
        raw["name"] = info_xml
        current["name"] = info.findtext("name", "")
        # Several UI fields share one device endpoint. Read each path once.
        by_path: dict[str, str] = {}
        failed_paths: dict[str, Exception] = {}
        for spec in specs:
            try:
                if spec["path"] in failed_paths:
                    raise failed_paths[spec["path"]]
                if spec["path"] not in by_path:
                    try:
                        by_path[spec["path"]] = await client.get_xml(spec["path"])
                    except Exception as exc:
                        # One unavailable route must not cause a timeout for
                        # each field mapped to it. Do not manufacture defaults.
                        failed_paths[spec["path"]] = exc
                        raise
                raw[spec["key"]] = by_path[spec["path"]]
                current[spec["key"]] = _setting_value(spec["key"], by_path[spec["path"]])
            except Exception as exc:
                current[spec["key"]] = f"ERROR: {type(exc).__name__}"
        try:
            now_xml = await client.get_xml("/now_playing")
            raw["now_playing"] = now_xml
            now_playing = _now_playing_metadata(now_xml)
            current["source"] = now_playing.get("source") or ""
            current["playback_state"] = now_playing.get("playback_state") or ""
            update_runtime_state(db, device.device_id, current_source=str(now_playing.get("source") or ""), playback_state=str(now_playing.get("playback_state") or ""), current_preset=int(now_playing["preset"]) if str(now_playing.get("preset") or "").isdigit() else None)
        except Exception as exc:
            raw["now_playing"] = f"ERROR: {type(exc).__name__}"
        art_setting = db.query(Setting).filter(Setting.key == f"station_art_mode:{device.device_id}").one_or_none()
        current["station_art_mode"] = art_setting.value if art_setting else "radio_symbol"
        db.commit()
    return {"device_id": device.device_id, "ip_address": device.ip_address, "radio_ip": device.ip_address, "firmware": device.firmware, "settings": specs, "current": current, "raw": raw, "now_playing": now_playing}


@router.post("/devices/{device_id}/settings/{setting}")
async def set_device_setting(device_id: str, setting: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    if setting == "name":
        raise HTTPException(status_code=400, detail="use the rename route")
    plan = _setting_plan(setting, payload.get("value"))
    preview = {"dry_run": True, "device_id": device.device_id, "target": device.ip_address,
               "path": plan.path, "xml": plan.xml, "memory_check": _memory_check_plan(device)}
    if payload.get("dry_run", True):
        return preview
    result = await _apply_settings_transaction(db, device, [plan], payload)
    change = next(iter(result["changed"]), {})
    return {**preview, **result, "dry_run": False, "response": change.get("response", ""),
            "verified": change.get("after", plan.expected),
            "transport_uncertain": change.get("transport_uncertain", False)}


@router.post("/devices/{device_id}/settings-apply")
async def apply_changed_device_settings(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """Validate the entire batch, then delegate all writes and readbacks."""
    device = _device_or_404(db, device_id)
    requested = payload.get("values", {})
    if not isinstance(requested, dict):
        raise HTTPException(status_code=400, detail="values must be an object")
    allowed = {"name", "volume", "bass", "clockDisplay", "clockConfig", "language", "powersaving",
               "rebroadcastlatencymode", "station_art_mode"}
    unknown = sorted(set(requested) - allowed)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unsupported settings: {', '.join(unknown)}")
    plans = []
    art_mode = None
    for setting, value in requested.items():
        if setting == "station_art_mode":
            if value not in ("radio_symbol", "station_logo", "no_station_logo"):
                raise HTTPException(status_code=400, detail="station_art_mode must be radio_symbol, station_logo or no_station_logo")
            art_mode = value
        else:
            plans.append(_setting_plan(setting, value))
    clock = next((p for p in plans if p.setting == "clockConfig"), None)
    enabled = next((p for p in plans if p.setting == "clockDisplay"), None)
    if clock and enabled and "userEnable" in clock.expected and clock.expected["userEnable"] != str(enabled.expected).lower():
        raise HTTPException(status_code=400, detail="conflicting clock display settings")
    if payload.get("dry_run", False):
        return {"dry_run": True, "device_id": device.device_id,
                "plans": [{"setting": p.setting, "path": p.path, "xml": p.xml} for p in plans],
                "station_art_mode": art_mode}
    return await _apply_settings_transaction(db, device, plans, payload, art_mode)


def _split_csv(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]








def _now_playing_has_audio(xml: str, preset: Preset | None = None) -> bool:
    try:
        root = ET.fromstring(xml or "")
    except ET.ParseError:
        return False
    if (root.attrib.get("source") or "").upper() == "STANDBY":
        return False
    play_status = " ".join((root.findtext(path, "") or "") for path in ("playStatus", "playbackStatus")).strip().upper()
    if "PLAY" in play_status:
        return True
    if play_status:
        return False
    content = root.find(".//ContentItem")
    location = content.attrib.get("location", "") if content is not None else ""
    return bool(preset and preset.location and location and location == preset.location)








def _trigger_type(trigger: object) -> str:
    value = str(trigger or "manual").lower()
    if value.startswith("preset"):
        return "preset"
    if value in {"scheduler", "timer"} or "timer" in value:
        return "timer"
    if value.startswith("multiroom"):
        return "multiroom"
    return "manual"


def _is_user_playback_row(row: PlayHistory, *, allow_unresolved_active: bool = False) -> bool:
    if bool(getattr(row, "internal_event", False)):
        return False
    if bool(getattr(row, "is_internal", False)):
        return False
    if not bool(getattr(row, "is_confirmed", True)):
        return False
    source = str(getattr(row, "source", "") or "").strip().lower()
    source_type = str(getattr(row, "source_type", "") or source).strip().lower()
    trigger = str(getattr(row, "trigger", "") or "").strip().lower()
    trigger_type = str(getattr(row, "trigger_type", "") or _trigger_type(trigger)).strip().lower()
    internal_values = {
        "standby",
        "keepalive_internal",
        "maintenance_internal",
        "setup_activation",
        "six_hour_refresh",
        "background_probe",
        "background_maintenance",
    }
    if source in internal_values or source_type in internal_values or trigger in internal_values or trigger_type in internal_values:
        return False
    if source == "standby" or trigger in {"stop", "pause", "stop_pause"}:
        return False
    if not bool(getattr(row, "success", 1)):
        return False
    has_identity_signal = bool(
        clean_station_name(getattr(row, "station_display_name", ""))
        or clean_station_name(getattr(row, "station_name", ""))
        or getattr(row, "station_id", None)
        or str(getattr(row, "stream_url", "") or "").strip()
        or getattr(row, "preset_button", None)
    )
    has_playback_context = bool(
        allow_unresolved_active
        and (
            clean_station_name(getattr(row, "device_name", ""))
            or str(getattr(row, "device_ip", "") or "").strip()
            or getattr(row, "last_confirmed_playing_at", None)
            or int(getattr(row, "confirmed_duration_seconds", 0) or 0) > 0
        )
    )
    if (
        not has_identity_signal
        and not has_playback_context
    ):
        return False
    return trigger_type in {"manual", "preset", "station", "timer", "multiroom"}




def _duration_seconds(row: PlayHistory) -> int:
    from basswiesn.app.services.playback_state import conservative_duration_seconds
    return conservative_duration_seconds(row, poll_tolerance_seconds=get_settings().playback_keepalive_interval_seconds + 60)


def _iso_or_empty(value) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _history_text(value, fallback: str = "") -> str:
    return str(value) if value is not None else fallback


def _station_summary(db: Session, station_id: int | None) -> tuple[str, str]:
    if station_id is None:
        return "", ""
    station = db.query(Station).filter(Station.id == station_id).one_or_none()
    if station is None:
        return "", ""
    return station.name, station.stream_url

@router.post("/devices/{device_id}/station/search-native")
async def native_station_search(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    if type(payload.get("dry_run", True)) is not bool:
        raise HTTPException(status_code=400, detail="dry_run must be a JSON boolean")
    xml = _search_station_xml(payload.get("source") or "TUNEIN", payload.get("sourceAccount"), payload.get("query"))
    if payload.get("dry_run", True):
        return {"dry_run": True, "device_id": device.device_id, "target": device.ip_address, "path": "/searchStation", "xml": xml, "note": "Native radio search; confirmed XML shape, real execution depends on configured service/account."}
    try:
        response = await search_native(db, device, xml, client_factory=SoundTouchClient,
                                       guard=enforce_ip_write_guard)
    except NativeSearchError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code,
            "automatic_retry": False, "message": "Native query not confirmed; no automatic retry."}) from None
    return {"dry_run": False, "device_id": device.device_id, "target": device.ip_address, "path": "/searchStation", "xml": xml, "results": _parse_search_station_results(response), "response": response}


@router.post("/devices/{device_id}/station/add-native")
async def native_station_add(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    xml = _add_station_xml(payload.get("source") or "TUNEIN", payload.get("sourceAccount"), payload.get("token"), payload.get("name"))
    try:
        command = ManualCommand.station(xml)
    except (ValueError, ET.ParseError):
        raise HTTPException(status_code=400, detail="Invalid native station request") from None
    return await _manual_command_response(db, device, command, payload)


async def _manual_command_response(db: Session, device: Device, command: ManualCommand, payload: dict):
    if type(payload.get("dry_run", True)) is not bool:
        raise HTTPException(status_code=400, detail="dry_run must be a JSON boolean")
    if payload.get("dry_run", True):
        return {**command.preview(), "device_id": device.device_id, "target": device.ip_address}
    if not _lab_mode_enabled(db):
        raise HTTPException(status_code=403, detail="This unverified radio operation requires LAB mode")
    if not is_yes_confirmation(payload.get("confirmation")) or payload.get("acknowledge_manual_recovery") is not True:
        raise HTTPException(status_code=409, detail="Explicit YES and manual-recovery acknowledgement required")
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", operation_id):
        raise HTTPException(status_code=400, detail="An explicit unique operation_id (32 lowercase hex characters) is required")
    enforce_ip_write_guard(db, device)
    try:
        result = await execute_manual_command(db, device, command, client_factory=SoundTouchClient,
            guard=enforce_ip_write_guard, operation_id=operation_id)
    except ManualCommandError as exc:
        raise HTTPException(status_code=502 if exc.detail["reconciliation_required"] else 409, detail=exc.detail) from None
    return JSONResponse(result, status_code=202)


@router.post("/devices/{device_id}/sources/name-plan")
async def source_name_plan(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    xml = _name_source_plan_xml(payload.get("source") or "AUX", payload.get("name"), payload.get("sourceAccount"), payload.get("xml"))
    return {"dry_run": True, "execution_enabled": False, "device_id": device.device_id, "target": device.ip_address, "path": "/nameSource", "xml": xml, "note": "Only planned because local sources confirm /nameSource exists but not enough model-safe write behavior. Execute manually only after real capture confirms payload."}


@router.post("/devices/{device_id}/bass-capabilities")
async def bass_capabilities_probe(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    if payload.get("dry_run", True):
        return {"dry_run": True, "device_id": device.device_id, "target": device.ip_address, "path": "/bassCapabilities", "note": "Read-only GET; use result to clamp bass slider per model."}
    xml = await SoundTouchClient(device.ip_address).get_xml("/bassCapabilities")
    return {"dry_run": False, "device_id": device.device_id, "target": device.ip_address, "path": "/bassCapabilities", "capabilities": _parse_bass_capabilities(xml), "xml": xml}


@router.get("/devices/{device_id}/wireless-profiles")
async def wireless_profiles(device_id: str, survey: bool = False, db: Session = Depends(get_db)) -> dict:
    if survey:
        # GET must not mutate a radio, including for authenticated sessions.
        raise HTTPException(status_code=409, detail={"code": "EXPLICIT_SURVEY_REQUIRED",
            "message": "Use POST /api/devices/{device_id}/wireless-survey with explicit YES confirmation."})
    device = _device_or_404(db, device_id)
    try:
        return await wireless_diagnostics(db, device, survey=False, client_factory=SoundTouchClient, guard=enforce_ip_write_guard)
    except WirelessDiagnosticError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from None


@router.post("/devices/{device_id}/wireless-survey")
async def wireless_survey(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    if type(payload.get("dry_run", True)) is not bool:
        raise HTTPException(status_code=400, detail="dry_run must be a JSON boolean")
    if payload.get("dry_run", True):
        return {"dry_run": True, "device_id": device.device_id, "path": "/performWirelessSiteSurvey",
                "http_method": "POST", "confirmation_required": "YES", "completion_verified": False}
    if not is_yes_confirmation(payload.get("confirmation")):
        raise HTTPException(status_code=409, detail="Wireless survey confirmation required")
    if not _lab_mode_enabled(db):
        raise HTTPException(status_code=403, detail="Wireless survey completion is unverified; LAB mode required")
    try:
        result = await wireless_diagnostics(db, device, survey=True, client_factory=SoundTouchClient, guard=enforce_ip_write_guard)
    except WirelessDiagnosticError as exc:
        raise HTTPException(status_code=502 if exc.detail["reconciliation_required"] else 409, detail=exc.detail) from None
    return JSONResponse(result, status_code=202)


@router.post("/devices/{device_id}/wireless-profiles")
async def add_wireless_profile(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    try:
        command = ManualCommand.wireless(payload)
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=400, detail="Invalid SSID, password or wireless security contract") from None
    return await _manual_command_response(db, device, command, payload)


@router.post("/devices/{device_id}/zone/status")
async def zone_status_probe(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    if payload.get("dry_run", True):
        return {"dry_run": True, "device_id": device.device_id, "target": device.ip_address, "path": "/getZone", "note": "Read-only current multiroom zone status."}
    xml = await SoundTouchClient(device.ip_address).get_xml("/getZone")
    return {"dry_run": False, "device_id": device.device_id, "target": device.ip_address, "path": "/getZone", "zone": _parse_zone_summary(xml), "xml": xml}



@router.get("/play-history")
async def play_history(limit: int = 100, include_internal: bool = False, db: Session = Depends(get_db)) -> list[dict]:
    max_limit = min(limit, 500)
    raw_limit = max_limit if include_internal else min(max_limit * 5, 1000)
    rows = db.query(PlayHistory).order_by(PlayHistory.started_at.desc()).limit(raw_limit).all()
    if not include_internal:
        rows = [row for row in rows if _is_user_playback_row(row, allow_unresolved_active=True)][:max_limit]
    device_ips = {row.device_id: row.ip_address for row in db.query(Device).all()}
    result = []
    identities = HistoryIdentityProjection(db)
    for row in rows:
        identity = identities.resolve(row)
        result.append({
            "id": row.id,
            "started_at": _iso_or_empty(row.started_at),
            "ended_at": _iso_or_empty(row.ended_at) or None,
            "duration_seconds": _duration_seconds(row),
            "device_id": _history_text(row.device_id),
            "device_name": _history_text(row.device_name),
            "device_ip": _history_text(getattr(row, "device_ip", "")) or device_ips.get(row.device_id or "", ""),
            "ip_address": _history_text(getattr(row, "device_ip", "")) or device_ips.get(row.device_id or "", ""),
            "station_id": identity.station_id,
            "station_name": _history_text(row.station_name),
            "station_display_name": identity.station_display_name,
            "stream_url": _history_text(row.stream_url),
            "source": _history_text(row.source),
            "source_type": _history_text(getattr(row, "source_type", ""), _history_text(row.source)),
            "source_display_name": identity.source_display_name,
            "identity_source": identity.identity_source,
            "identity_confidence": identity.identity_confidence,
            "stream_host": identity.stream_host,
            "is_internal": identity.is_internal,
            "is_confirmed": identity.is_confirmed,
            "zone_master_id": _history_text(row.zone_master_id),
            "zone_member_ids": _split_csv(row.zone_member_ids),
            "trigger": _history_text(row.trigger),
            "trigger_type": _history_text(getattr(row, "trigger_type", ""), _trigger_type(row.trigger)),
            "preset_button": getattr(row, "preset_button", None),
            "preset_name": _history_text(getattr(row, "preset_name", "")),
            "volume": getattr(row, "volume", None),
            "success": bool(getattr(row, "success", 1)),
            "error_message": _history_text(getattr(row, "error_message", "")),
            "internal_event": bool(getattr(row, "internal_event", False)),
            "last_confirmed_playing_at": _iso_or_empty(getattr(row, "last_confirmed_playing_at", None)) or None,
            "end_reason": _history_text(getattr(row, "end_reason", "")),
        })
    return result


@router.post("/play-history/start")
async def start_play_history(payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, payload.get("device_id", ""))
    # Keep the URL, but browser-provided PLAY_STATE/timestamps are assertions,
    # not radio readback. Only the existing backend observation is authoritative.
    row = observed_session(db, device)
    if row is None:
        return {"pending": True, "opened": False, "playback_verified": False,
                "reason": "awaiting_confirmed_play_state"}
    return {"id": row.id, "started_at": row.started_at.isoformat(), "opened": True,
            "playback_verified": True, "state_observed_at": row.last_confirmed_playing_at.isoformat()}


@router.post("/play-history/{history_id}/stop")
async def stop_play_history(history_id: int, db: Session = Depends(get_db)) -> dict:
    row = db.query(PlayHistory).filter(PlayHistory.id == history_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="play history entry not found")
    if row.ended_at is None:
        raise HTTPException(status_code=409, detail={"code": "HISTORY_OBSERVATION_REQUIRED"})
    return {"id": row.id, "ended_at": row.ended_at.isoformat(), "duration_seconds": _duration_seconds(row)}


@router.post("/play-history/event")
async def record_play_history_event(payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, payload.get("device_id", ""))
    try:
        row = record_client_report(db, device, payload)
    except ValueError:
        raise HTTPException(400, detail={"code": "HISTORY_REPORT_INVALID"}) from None
    write_masterlog("playback_client_report", history_id=row.id, device_id=device.device_id,
                    playback_verified=False)
    return {"id": row.id, "trigger": row.trigger, "timestamp": row.started_at.isoformat(),
            "recorded": True, "playback_verified": False}


@router.get("/stats/playback")
async def playback_stats(db: Session = Depends(get_db), timezone: str | None = Query(default=None, max_length=80)) -> dict:
    from basswiesn.app.services.listening_stats import listening_summary, observed_interval, overlap_seconds
    from basswiesn.app.services.clock_metadata import clock_metadata_timezone
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        zone = ZoneInfo(timezone) if timezone is not None else clock_metadata_timezone(db)
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, detail={"code": "INVALID_TIMEZONE"})
    all_rows = db.query(PlayHistory).order_by(PlayHistory.started_at.desc()).all()
    rows = [row for row in all_rows if _is_user_playback_row(row)]
    devices = {row.device_id: row for row in db.query(Device).all()}
    runtime_rows = {row.key.removeprefix("device:").removesuffix(":runtime_state"): row for row in db.query(RuntimeState).filter(RuntimeState.key.like("device:%:runtime_state")).all()}
    by_device: dict[str, dict] = {}
    by_station: dict[str, dict] = {}
    by_preset: dict[int, dict] = {}
    by_trigger: dict[str, dict] = {}
    yearly: dict[int, dict] = {}
    device_history: dict[str, dict] = {}
    active = []
    now = utc_now()
    today = now.astimezone(zone).date()
    week_start = today.fromisocalendar(today.isocalendar().year, today.isocalendar().week, 1)
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    decade_start = today.replace(year=(today.year // 10) * 10, month=1, day=1)
    today_stats = {"starts": 0, "stops": 0, "errors": 0}
    aggregate_seconds = {"today": 0, "week": 0, "month": 0, "year": 0, "decade": 0, "lifetime": 0}
    timer = {"plays": 0, "successes": 0, "seconds": 0}
    station_names = {}
    tolerance = get_settings().playback_keepalive_interval_seconds + 60
    period_boundaries = {key: datetime.combine(value, datetime.min.time(), tzinfo=zone).astimezone(UTC) for key, value in
                         (("today", today), ("week", week_start), ("month", month_start), ("year", year_start), ("decade", decade_start))}
    for row in rows:
        interval_start, interval_end = observed_interval(row, now=now, tolerance=tolerance)
        seconds = max(0, int((interval_end - interval_start).total_seconds()))
        started_at = (row.started_at or utc_now())
        started_at = (started_at.replace(tzinfo=UTC) if started_at.tzinfo is None else started_at).astimezone(zone)
        started_date = started_at.date()
        device_key = row.device_id or "unknown"
        identity = identity_for_history(db, row)
        station_key = identity.station_display_name
        station_names[row.id] = station_key
        trigger_type = getattr(row, "trigger_type", "") or _trigger_type(row.trigger)
        success = bool(getattr(row, "success", 1))
        year_bucket = yearly.setdefault(started_at.year, {"year": started_at.year, "plays": 0, "seconds": 0})
        year_bucket["plays"] += 1
        year_bucket["seconds"] += seconds
        aggregate_seconds["lifetime"] += seconds
        for period, boundary in period_boundaries.items():
            aggregate_seconds[period] += overlap_seconds(interval_start, interval_end, boundary)
        if started_date == today:
            if row.trigger == "stop":
                today_stats["stops"] += 1
            elif not success or "failed" in (row.trigger or "") or "error" in (row.trigger or ""):
                today_stats["errors"] += 1
            else:
                today_stats["starts"] += 1
        current_device = devices.get(device_key)
        current_name = current_device.name if current_device else (row.device_name or "")
        device_bucket = by_device.setdefault(device_key, {"device_id": device_key, "device_name": current_name, "current_device_name": current_name, "device_name_snapshot": row.device_name or "", "device_ip": (current_device.ip_address if current_device else "") or getattr(row, "device_ip", "") or "", "plays": 0, "seconds": 0, "first_usage": "", "last_played_at": "", "last_source": "", "failures": 0, "volume_last": None})
        if current_name:
            device_bucket["device_name"] = current_name
            device_bucket["current_device_name"] = current_name
        if row.device_name and not device_bucket.get("device_name_snapshot"):
            device_bucket["device_name_snapshot"] = row.device_name
        if getattr(row, "volume", None) is not None:
            device_bucket["volume_last"] = row.volume
        device_bucket["plays"] += 1
        device_bucket["seconds"] += seconds
        if not success:
            device_bucket["failures"] += 1
        if not device_bucket["device_ip"] and getattr(row, "device_ip", ""):
            device_bucket["device_ip"] = row.device_ip
        # Calendar grouping is local; sortable API/event timestamps stay UTC,
        # including the repeated hour when daylight saving ends.
        started_iso = _iso_or_empty(started_at.astimezone(UTC))
        if not device_bucket["first_usage"] or started_iso < device_bucket["first_usage"]:
            device_bucket["first_usage"] = started_iso
        if not device_bucket["last_played_at"] or started_iso > device_bucket["last_played_at"]:
            device_bucket["last_played_at"] = started_iso
            device_bucket["last_source"] = station_key
        history_bucket = device_history.setdefault(device_key, {"device_id": device_key, "current_name": current_name, "current_ip": (current_device.ip_address if current_device else ""), "known_names": set(), "known_ips": set(), "linked": current_device is not None, "first_seen": "", "last_seen": ""})
        if current_name:
            history_bucket["current_name"] = current_name
        if current_device and current_device.ip_address:
            history_bucket["current_ip"] = current_device.ip_address
        if row.device_name:
            history_bucket["known_names"].add(row.device_name)
        if getattr(row, "device_ip", ""):
            history_bucket["known_ips"].add(row.device_ip)
        if not history_bucket["first_seen"] or started_iso < history_bucket["first_seen"]:
            history_bucket["first_seen"] = started_iso
        if not history_bucket["last_seen"] or started_iso > history_bucket["last_seen"]:
            history_bucket["last_seen"] = started_iso
        station_bucket = by_station.setdefault(station_key, {"station_id": identity.station_id, "station": station_key, "station_display_name": station_key, "identity_source": identity.identity_source, "identity_confidence": identity.identity_confidence, "stream_host": identity.stream_host, "plays": 0, "seconds": 0, "last_played_at": ""})
        station_bucket["plays"] += 1
        station_bucket["seconds"] += seconds
        if not station_bucket["last_played_at"] or started_iso > station_bucket["last_played_at"]:
            station_bucket["last_played_at"] = started_iso
        if getattr(row, "preset_button", None):
            preset_bucket = by_preset.setdefault(int(row.preset_button), {"preset_button": int(row.preset_button), "preset_name": getattr(row, "preset_name", "") or f"Preset {int(row.preset_button)}", "plays": 0, "seconds": 0, "last_played_at": ""})
            preset_bucket["plays"] += 1
            preset_bucket["seconds"] += seconds
            if not preset_bucket["last_played_at"] or started_iso > preset_bucket["last_played_at"]:
                preset_bucket["last_played_at"] = started_iso
        trigger_bucket = by_trigger.setdefault(trigger_type, {"trigger_type": trigger_type, "plays": 0, "seconds": 0})
        trigger_bucket["plays"] += 1
        trigger_bucket["seconds"] += seconds
        if trigger_type == "timer":
            timer["plays"] += 1
            timer["successes"] += 1 if success else 0
            timer["seconds"] += seconds
        if row.ended_at is None:
            from basswiesn.app.services.playback_state import is_confirmed_playing
            runtime_row = runtime_rows.get(device_key)
            try:
                runtime_payload = json.loads(runtime_row.value or "{}") if runtime_row else {}
            except ValueError:
                runtime_payload = {}
            live = is_confirmed_playing(reachable=bool(current_device and current_device.reachable), current_source=runtime_payload.get("current_source"), playback_state=runtime_payload.get("playback_state"), state_observed_at=runtime_row.updated_at if runtime_row else None, now=now, stale_after_seconds=get_settings().playback_state_stale_after_seconds)
            if live:
                active.append({"device_id": device_key, "device_name": current_name, "device_name_snapshot": row.device_name or "", "station": station_key, "station_display_name": station_key, "identity_source": identity.identity_source, "identity_confidence": identity.identity_confidence, "seconds": seconds, "volume": getattr(row, "volume", None), "zone_member_ids": _split_csv(row.zone_member_ids), "status": "playing"})
    for bucket in by_device.values():
        bucket["avg_session_seconds"] = int(bucket["seconds"] / bucket["plays"]) if bucket["plays"] else 0
    linked_devices = []
    removed_devices = []
    for bucket in device_history.values():
        item = {
            **bucket,
            "known_names": sorted(bucket["known_names"]),
            "known_ips": sorted(bucket["known_ips"]),
        }
        if item["linked"]:
            linked_devices.append(item)
        else:
            removed_devices.append(item)
    server_rows = {row.key: row.value for row in db.query(RuntimeState).filter(RuntimeState.key.like("server:%")).all()}
    last_boot = server_rows.get("server:last_boot", "")
    current_uptime = 0
    if last_boot:
        try:
            boot_dt = datetime.fromisoformat(last_boot)
            if boot_dt.tzinfo is None:
                boot_dt = boot_dt.replace(tzinfo=UTC)
            current_uptime = max(0, int((utc_now() - boot_dt).total_seconds()))
        except ValueError:
            current_uptime = 0
    total_runtime = int(server_rows.get("server:total_runtime_seconds") or 0) + current_uptime
    return {
        "listening": listening_summary(rows, now=now, tolerance=tolerance,
                                       device_names={key: device.name for key, device in devices.items()}, station_names=station_names,
                                       timezone_name=zone.key),
        "timezone": zone.key,
        "today": today_stats,
        "lifetime": {"total_plays": len(rows), "total_seconds": aggregate_seconds["lifetime"], "internal_events_excluded": len(all_rows) - len(rows)},
        "aggregate": {f"{key}_hours": round(value / 3600, 2) for key, value in aggregate_seconds.items()},
        "yearly": sorted(yearly.values(), key=lambda item: item["year"], reverse=True)[:20],
        "device_history": {
            "linked": sorted(linked_devices, key=lambda item: item["last_seen"], reverse=True),
            "removed": sorted(removed_devices, key=lambda item: item["last_seen"], reverse=True),
        },
        "active": active,
        "by_device": sorted(by_device.values(), key=lambda item: item["seconds"], reverse=True),
        "by_station": sorted(by_station.values(), key=lambda item: item["seconds"], reverse=True),
        "top_presets": sorted(by_preset.values(), key=lambda item: item["plays"], reverse=True),
        "top_triggers": sorted(by_trigger.values(), key=lambda item: item["plays"], reverse=True),
        "timer": {**timer, "success_rate_percent": round((timer["successes"] / timer["plays"]) * 100, 1) if timer["plays"] else 0},
        "server": {
            "first_boot": server_rows.get("server:first_boot", ""),
            "last_boot": last_boot,
            "restart_count": int(server_rows.get("server:restart_count") or 0),
            "current_uptime_seconds": current_uptime,
            "total_runtime_seconds": total_runtime,
            "total_runtime_hours": round(total_runtime / 3600, 2),
        },
    }


@router.get("/stations/search-online")
async def search_online_stations(q: str, limit: int = 20, db: Session = Depends(get_db)) -> list[dict]:
    if not q.strip():
        return []
    decision = external_request_decision(
        db,
        service="radio_browser",
        url_or_host="all.api.radio-browser.info",
        reason="optionale Online-Sendersuche",
        required=False,
    )
    record_dependency(db, decision)
    if not decision.allowed:
        write_masterlog("radio_browser_search_blocked_by_offline_mode", mode=decision.mode, host=decision.target_host)
        raise HTTPException(status_code=409, detail={"error": "Strict Offline Mode blockiert die optionale Online-Sendersuche.", "offline": decision.to_dict()})
    hosts = (
        "de1.api.radio-browser.info",
        "de2.api.radio-browser.info",
        "at1.api.radio-browser.info",
        "nl1.api.radio-browser.info",
        "all.api.radio-browser.info",
    )
    requested_limit = min(limit, 50)
    params = {"hidebroken": "true", "limit": min(max(requested_limit * 3, requested_limit), 50), "order": "votes", "reverse": "true"}
    write_masterlog("radio_browser_search_start", query=q, limit=requested_limit, upstream_limit=params["limit"])
    stations = None
    failures = []
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False, trust_env=False) as client:
        for host in hosts:
            target_url = f"https://{host}/json/stations/byname/{quote(q)}"
            validation = validate_outbound_http_url(target_url)
            if not validation.ok:
                failures.append({"host": host, "error": validation.reason, "status_code": None})
                write_masterlog("radio_browser_host_blocked", host=host, reason=validation.reason)
                continue
            pinned_url, pinned_headers, extensions = pinned_http_target(
                target_url, validation
            )
            try:
                response = await client.get(
                    pinned_url,
                    params=params,
                    headers={**pinned_headers, "User-Agent": "basswiesn/1.0"},
                    extensions=extensions,
                )
                response.raise_for_status()
                stations = response.json()
                write_masterlog("radio_browser_host_success", host=host, status_code=response.status_code)
                break
            except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as exc:
                status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                if status_code is not None and status_code < 500:
                    raise HTTPException(status_code=status_code, detail="Online-Sendersuche aktuell nicht erreichbar. Bitte später erneut versuchen.") from exc
                failures.append({"host": host, "error": str(exc), "status_code": status_code})
                write_masterlog("radio_browser_host_failed", host=host, error=str(exc), status_code=status_code)
    if stations is None:
        write_masterlog("radio_browser_search_failed", failures=failures)
        raise HTTPException(status_code=503, detail="Online-Sendersuche aktuell nicht erreichbar. Bitte später erneut versuchen.")
    rows = []
    for item in stations:
        original = item.get("url") or item.get("url_resolved") or ""
        resolved = item.get("url_resolved") or item.get("url") or ""
        selected = original or resolved
        if not selected:
            continue
        hls_mime = "application/vnd.apple.mpegurl" if item.get("hls") else ""
        analysis = analyze_stream_url(original, mime=hls_mime, resolved_url=resolved, bitrate=item.get("bitrate"))
        write_masterlog("stream_type_detected", stream_url_original=analysis.stream_url_original, stream_url_resolved=analysis.stream_url_resolved, codec=analysis.stream_codec, mime=analysis.stream_mime, is_hls=analysis.is_hls, is_direct_audio=analysis.is_direct_audio, compatibility_score=analysis.compatibility_score, compatibility_warning=analysis.compatibility_warning)
        rows.append({
            "name": item.get("name", ""),
            "stream_url": selected,
            "stream_url_original": analysis.stream_url_original,
            "stream_url_resolved": analysis.stream_url_resolved,
            "stream_format": analysis.stream_format,
            "stream_mime": analysis.stream_mime,
            "stream_codec": analysis.stream_codec,
            "stream_bitrate": analysis.stream_bitrate,
            "bitrate": item.get("bitrate") or analysis.stream_bitrate,
            "compatibility_score": analysis.compatibility_score,
            "compatibility_warning": analysis.compatibility_warning,
            "is_hls": analysis.is_hls,
            "is_direct_audio": analysis.is_direct_audio,
            "image_url": item.get("favicon", ""),
            "country": item.get("country", ""),
            "tags": item.get("tags", ""),
            "media": _radio_browser_media_summary(selected),
        })
    rows.sort(key=lambda item: (-int(item.get("compatibility_score") or 0), item["stream_url"]))
    return rows[:requested_limit]


@router.get("/stations/online-artwork")
async def online_station_artwork(url: str, db: Session = Depends(get_db)) -> RedirectResponse:
    """Fetch a Radio Browser logo through the guarded raster cache.

    Search results are controlled by an external catalogue.  Returning their
    favicon URL directly to the WebUI would let the browser bypass the central
    protected/private-target policy.  This same-origin endpoint validates and
    DNS-pins the target before a bounded fetch and never exposes failed URLs to
    the browser.
    """

    candidate = str(url or "").strip()
    if not candidate or len(candidate) > 4096:
        raise HTTPException(status_code=404, detail="station artwork not available")
    validation = validate_outbound_http_url(candidate, public_only=True)
    if not validation.ok:
        raise HTTPException(status_code=404, detail="station artwork not available")
    decision = external_request_decision(
        db,
        service="station_artwork",
        url_or_host=candidate,
        reason="optional online station logo",
        required=False,
    )
    if not decision.allowed:
        raise HTTPException(status_code=404, detail="station artwork not available")
    lock = _ONLINE_ARTWORK_LOCKS[sum(candidate.encode("utf-8")) % len(_ONLINE_ARTWORK_LOCKS)]
    async with lock:
        result = await cache_artwork(
            db,
            choose_artwork(station_logo_url=candidate),
            media_dir=get_settings().data_dir / "media",
            provider_id="RADIO_BROWSER",
        )
    if result.status == "FAILED" or not result.cache_key:
        raise HTTPException(status_code=404, detail="station artwork not available")
    return RedirectResponse(
        f"/api/artwork-cache/{result.cache_key}",
        status_code=307,
        headers={"Cache-Control": "private, max-age=3600"},
    )


def _radio_browser_media_summary(url: str) -> dict:
    analysis = analyze_stream_url(url)
    if analysis.stream_format == "mp3":
        return {"status": "confirmed", "type": "direct_mp3", **analysis.to_dict()}
    if analysis.stream_format in {"aac", "ogg"}:
        return {"status": "candidate", "type": analysis.stream_format, **analysis.to_dict()}
    if analysis.is_hls:
        return {"status": "candidate", "type": "hls", **analysis.to_dict()}
    value = (url or "").lower().split("?", 1)[0]
    if value.startswith("http://"):
        return {"status": "candidate", "type": "http_live_radio", **analysis.to_dict()}
    return {"status": "candidate", "type": "https_live_radio", **analysis.to_dict()}


def _radio_browser_stream_rank(url: str) -> tuple[int, str]:
    return (-analyze_stream_url(url).compatibility_score, url)



@router.post("/devices/{device_id}/probe-info")
async def probe_device_info(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    endpoints = ["/info", "/capabilities", "/networkInfo", "/supportedURLs"]
    if payload.get("dry_run", True):
        return {"dry_run": True, "device_id": device.device_id, "target": device.ip_address, "endpoints": endpoints}
    write_masterlog("device_probe", device_id=device.device_id, radio_ip=device.ip_address)
    client = SoundTouchClient(device.ip_address)
    results = {}
    for endpoint in endpoints:
        try:
            results[endpoint] = await client.get_xml(endpoint)
        except Exception as exc:
            results[endpoint] = f"ERROR: {exc}"
    if results.get("/info") and not results["/info"].startswith("ERROR"):
        device.info_xml = results["/info"]
        device.name = _xml_text(device.info_xml, "name") or device.name
        device.model = _xml_text(device.info_xml, "type") or device.model
        device.firmware = _xml_text(device.info_xml, ".//softwareVersion") or device.firmware
        device, identity = DeviceIdentityService(DeviceIdentityRepository(db)).reconcile(
            device, device.info_xml
        )
    else:
        identity = {"merged": False, "canonical_id": device.device_id}
    if results.get("/capabilities") and not results["/capabilities"].startswith("ERROR"):
        device.capabilities_xml = results["/capabilities"]
    device.last_seen = utc_now()
    db.commit()
    write_masterlog(
        "device_probe_complete",
        device_id=device.device_id,
        failed_endpoints=sum(1 for value in results.values() if str(value).startswith("ERROR")),
    )
    return {"dry_run": False, "device": _device_summary(device), "identity": identity, "results": results}


@router.get("/devices/{device_id}/host-config")
async def device_host_config(device_id: str, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    return {
        "device_id": device.device_id,
        "ip_address": device.ip_address,
        "non_ssh": {
            "info_cached": device.info_xml,
            "capabilities_cached": device.capabilities_xml,
            "target_cloud_host": "content.api.bose.io",
            "target_cloud_port": 1516,
            "expected_bmx_registry": "http://content.api.bose.io:1516/bmx/registry/v1/services",
        },
        "ssh_plan": {
            "read_hosts": "cat /etc/hosts; hostname; cat /mnt/nv/BoseApp-Persistence/1/SystemConfigurationDB.xml 2>/dev/null || true",
            "read_bose_urls": "getpdo CurrentSystemConfiguration 2>/dev/null || true; envswitch boseurls get 2>/dev/null || true",
            "note": "SSH-Ausführung ist absichtlich noch nicht automatisch aktiv. Erst Live-Testphase.",
        },
    }


@router.post("/devices/{device_id}/power/{action}")
async def device_power_action(device_id: str, action: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    if type(payload.get("dry_run", True)) is not bool:
        raise HTTPException(status_code=400, detail="dry_run must be a JSON boolean")
    if action == "power_management":
        raise HTTPException(status_code=410, detail="powerManagement/battery probing is not part of the regular BASSWIESN runtime")
    if action not in POWER_PATHS:
        raise HTTPException(status_code=400, detail="unsupported power action")
    path = POWER_PATHS[action]
    state_changing = action in {"standby", "low_power_standby"}
    confirmation_required = "YES" if state_changing else ""
    if payload.get("dry_run", True):
        return {
            "dry_run": True,
            "device_id": device.device_id,
            "target": device.ip_address,
            "path": path,
            "http_method": "GET",
            "confirmation_required": confirmation_required,
            "danger": "lowPowerStandby can drop the network and require hardware wake" if action == "low_power_standby" else "standby stops playback" if action == "standby" else "read-only status",
        }
    if state_changing and not is_yes_confirmation(payload.get("confirmation")):
        raise HTTPException(status_code=409, detail={"error": "power action confirmation required", "expected": confirmation_required})
    try:
        result = await execute_power(db, device, action, client_factory=SoundTouchClient, guard=enforce_ip_write_guard)
    except PowerActionError as exc:
        raise HTTPException(status_code=502 if exc.detail["reconciliation_required"] else 409, detail=exc.detail) from None
    if result["status"] == "PENDING_READBACK":
        return JSONResponse(result, status_code=202)
    return result


def _lab_mode_enabled(db: Session) -> bool:
    row = db.query(Setting).filter(Setting.key == "ui_mode").one_or_none()
    return bool(row and row.value == "lab")


@router.post("/lab/devices/{device_id}/factory-reset")
async def lab_factory_reset(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """Profile-bound, backed-up GUI factory reset for the explicit LAB path."""

    if type(payload.get("dry_run", True)) is not bool:
        raise HTTPException(status_code=400, detail="dry_run must be a JSON boolean")
    if not _lab_mode_enabled(db):
        raise HTTPException(status_code=403, detail="Factory Reset Radio is available only while LAB mode is active.")
    device = _device_or_404(db, device_id)
    if is_device_access_protected(device.ip_address, device.device_id):
        raise HTTPException(status_code=403, detail="Protected devices cannot be selected for factory reset.")
    candidate = candidate_from_device(device, db)
    if candidate is None:
        raise HTTPException(status_code=409, detail="No confirmed model/build profile is available for this radio.")
    preview = {
        "dry_run": True,
        "device": {
            "device_id": candidate.device_id,
            "ip_address": candidate.ip_address,
            "name": candidate.name,
            "model": candidate.model,
            "firmware": candidate.firmware,
            "product_id": candidate.product_id,
            "variant": candidate.variant,
            "platform": candidate.platform,
        },
        "profile": candidate.profile_key,
        "eligible": candidate.eligible,
        "blocking_reason": candidate.blocking_reason,
        "confirmation_required": "FACTORY RESET RADIO",
        "backup_required": True,
        "operation": "sys factorydefault",
        "expected_statuses": [
            "FACTORY RESET SENT",
            "WAITING FOR DEVICE RESTART",
            "DEVICE NO LONGER REACHABLE / RESET EXPECTED",
        ],
        "warning": "This erases radio configuration. Redacted backups cannot restore account/session secrets. Manual re-enrollment may be required.",
        "readback_required": "MANUAL: send/disconnect is not proof of factory state",
        "standalone_required": True,
    }
    if payload.get("dry_run", True):
        return preview
    if not candidate.eligible:
        raise HTTPException(status_code=409, detail={"error": "FACTORY_RESET_PROFILE_BLOCKED", "reason": candidate.blocking_reason})
    if payload.get("acknowledged") is not True:
        raise HTTPException(status_code=400, detail="You must acknowledge that this resets the radio.")
    if str(payload.get("confirmation") or "").strip() != "FACTORY RESET RADIO":
        raise HTTPException(status_code=400, detail="confirmation required: FACTORY RESET RADIO")
    enforce_ip_write_guard(db, device)

    try:
        result = await request_factory_reset(
            db, device, candidate, adapter=RadioSetupAdapter(), send=cli_factory_reset,
            client_factory=SoundTouchClient, guard=enforce_ip_write_guard,
        )
    except FactoryResetError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from None
    return {**preview, **result, "dry_run": False, "eligible": True}


@router.post("/devices/{device_id}/recovery/{action}")
async def device_recovery_action(device_id: str, action: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    if action == "persistent_ssh" and type(payload.get("dry_run", True)) is not bool:
        raise HTTPException(status_code=422, detail="dry_run must be a JSON boolean")
    if action in {"factory_default", "factory_reset_fix_plan", "nuclear_reset_plan"}:
        raise HTTPException(
            status_code=410,
            detail={
                "error": "FACTORY_RESET_RETIRED",
                "message": "BASSWIESN 2.0 führt keinen Factory Reset aus und erzeugt keinen Löschbefehl.",
                "recovery": "Nutze die begrenzte Diagnose- und Recovery-Leiter; ein Werksreset gehört nicht dazu.",
            },
        )
    if action == "persistent_ssh" and not payload.get("dry_run", True):
        enforce_ip_write_guard(db, device)
    confirmation = payload.get("confirmation", "")
    safe_startup_volume = int(payload.get("safe_startup_volume", 30))
    if safe_startup_volume < 0 or safe_startup_volume > 100:
        raise HTTPException(status_code=400, detail="safe_startup_volume must be 0..100")
    if action == "persistent_ssh":
        required = "YES"
        command = _persistent_ssh_command()
        if payload.get("dry_run", True):
            return {
                "dry_run": True,
                "device_id": device.device_id,
                "target": device.ip_address,
                "confirmation_required": required,
                "plan": PERSISTENT_SSH_PLAN,
                "ssh_command": command,
                "rc_local_fallback_manual_only": "cat > /mnt/nv/rc.local; touch /tmp/remote_services; /etc/init.d/sshd start; chmod 755 /mnt/nv/rc.local; sync",
                "source_evidence": ["firmware /usr/bin/remote_services_enabled", "firmware /etc/init.d/sshd", "firmware /etc/udev/scripts/mount.sh", "Mathias toolkit Install-PersistentSSH"],
            }
        if not is_yes_confirmation(confirmation):
            raise HTTPException(status_code=400, detail=f"confirmation required: {required}")
        username = str(payload.get("username") or "root")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", username):
            raise HTTPException(status_code=400, detail="invalid SSH username")
        try:
            return await enable_persistence(
                db, device, username=username, client_factory=SoundTouchClient,
                ssh_runner=_run_ssh_readonly_command, guard=enforce_ip_write_guard,
                expected_previous=payload.get("expected_previous"),
            )
        except SshPersistenceError as exc:
            raise HTTPException(status_code=409, detail=exc.result) from None
    raise HTTPException(status_code=400, detail="unsupported recovery action")




def _setting_rows(db: Session) -> dict[str, str]:
    return {row.key: row.value for row in db.query(Setting).all()}


def _battery_percent(xml: str) -> str:
    match = re.search(r'percentCharge="([^"<]+)"', xml or "")
    if match:
        return match.group(1)
    return _xml_text(xml or "", "percentCharge")


def _display_xml_root(xml: str) -> ET.Element | None:
    try:
        return ET.fromstring(xml or "")
    except ET.ParseError:
        return None


def _now_playing_metadata(xml: str) -> dict:
    root = _display_xml_root(xml)
    source = root.attrib.get("source", "") if root is not None else ""
    preset = root.attrib.get("preset", "") if root is not None else ""
    content = root.find(".//ContentItem") if root is not None else None
    item_name = content.findtext("itemName", "") if content is not None else ""
    station = _xml_text(xml, "stationName") or item_name
    track = _xml_text(xml, "track")
    artist = _xml_text(xml, "artist")
    album = _xml_text(xml, "album")
    image_url = _xml_text(xml, "art")
    play_status = _xml_text(xml, "playStatus") or _xml_text(xml, "playbackStatus")
    status_text = play_status.lower()
    playback_state = "playing" if "play" in status_text else "paused" if "pause" in status_text else "stopped" if source.upper() == "STANDBY" or "stop" in status_text else "unknown"
    label = station or track or artist or source or "SoundTouch"
    return {
        "source": source,
        "station": station,
        "item_name": item_name,
        "track": track,
        "artist": artist,
        "album": album,
        "image_url": image_url,
        "preset": preset,
        "play_status": play_status,
        "playback_state": playback_state,
        "label": label,
    }


def _network_signal_metadata(xml: str) -> dict:
    root = _display_xml_root(xml)
    if root is None:
        return {"kind": "unknown", "label": "NET ?", "percent": None, "reliability": "no networkInfo XML"}
    signal_map = {"EXCELLENT_SIGNAL": 100, "GOOD_SIGNAL": 80, "FAIR_SIGNAL": 55, "MARGINAL_SIGNAL": 45, "POOR_SIGNAL": 20}
    interfaces = root.findall(".//interface")
    wifi = next((node for node in interfaces if "WIFI" in (node.attrib.get("type", "") + node.attrib.get("state", "")).upper()), None)
    if wifi is not None:
        signal = wifi.attrib.get("signal", "")
        percent = signal_map.get(signal)
        label = f"WiFi {percent}%" if percent is not None else f"WiFi {signal or '?'}"
        return {
            "kind": "wifi",
            "state": wifi.attrib.get("state", ""),
            "ssid": wifi.attrib.get("ssid", ""),
            "signal": signal,
            "percent": percent,
            "label": label,
            "reliability": "preferred source: /networkInfo WiFi interface signal",
        }
    ethernet = next((node for node in interfaces if "ETHERNET" in (node.attrib.get("type", "") + node.attrib.get("state", "")).upper()), None)
    if ethernet is not None:
        return {"kind": "ethernet", "state": ethernet.attrib.get("state", ""), "label": "LAN", "percent": None, "reliability": "wired interface; no WiFi level"}
    return {"kind": "unknown", "label": "NET ?", "percent": None, "reliability": "no WiFi/Ethernet interface in /networkInfo"}


def _battery_metadata(xml: str) -> dict:
    percent = _battery_percent(xml)
    running = _xml_text(xml, "runningOnBattery")
    if not running:
        match = re.search(r'runningOnBattery="([^"<]+)"', xml or "")
        running = match.group(1) if match else ""
    label = f"Bat {percent}%" if percent else "Bat ?"
    try:
        numeric_percent = int(float(percent)) if percent else None
    except ValueError:
        numeric_percent = None
    if numeric_percent is None:
        bucket = None
    else:
        bucket = next(upper for upper in (20, 40, 60, 75, 100) if numeric_percent <= upper)
    return {
        "percent": percent,
        "running_on_battery": running,
        "level_bucket": bucket,
        "label": label,
        "reliability": "HTTP mirror; use safe CLI17000 ba 8 in battery diagnostics as the hardware-near source of truth",
    }


def _display_clock_label(include_date: bool) -> str:
    return datetime.now().strftime("%d.%m %H:%M" if include_date else "%H:%M")


def _display_metadata_fields(mode: str) -> list[str]:
    selected = next((item for item in DISPLAY_METADATA_MODES if item["key"] == mode), None)
    if selected is None:
        raise HTTPException(status_code=400, detail="unsupported display metadata mode")
    fields = list(selected.get("fields", []))
    if "battery" in fields:
        raise HTTPException(status_code=410, detail="display battery metadata was removed in BASSWIESN 1.5.0")
    return fields


def _build_display_status_label(mode: str, now: dict, network: dict, battery: dict, include_date: bool) -> str:
    fields = _display_metadata_fields(mode)
    parts = []
    if any(field in fields for field in ["station", "artist", "track"]):
        parts.append(str(now.get("label") or "SoundTouch"))
    if "clock" in fields:
        parts.append(_display_clock_label(include_date))
    if "wifi" in fields:
        parts.append(str(network.get("label") or "WiFi ?"))
    text = " | ".join(part for part in parts if part).strip()
    return text[:120] if text else "SoundTouch"


def _direct_metadata_select_xml(base_label: str, status_label: str, stream_url: str, image_url: str = "") -> str:
    # /select is a playback/source action. It may carry normal station metadata,
    # but arbitrary clock/battery/WiFi text is not a reliable native overlay.
    display_name = base_label or "Basswiesn"
    return (
        f'<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" '
        f'location="{html_escape(stream_url, quote=True)}" sourceAccount="" isPresetable="true">'
        f"<itemName>{html_escape(display_name)}</itemName>"
        f"<containerArt>{html_escape(image_url or '')}</containerArt>"
        f"</ContentItem>"
    )


async def _display_source_snapshot(device: Device, probe: bool) -> dict:
    results = {"/now_playing": "", "/networkInfo": ""}
    if not probe:
        return results
    client = SoundTouchClient(device.ip_address)
    for endpoint in list(results):
        try:
            results[endpoint] = await client.get_xml(endpoint)
        except Exception as exc:
            results[endpoint] = f"ERROR: {exc}"
    return results


@router.post("/devices/{device_id}/display/metadata-preview")
async def display_metadata_preview(device_id: str, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    mode = str(payload.get("mode") or "station_clock_wifi")
    _display_metadata_fields(mode)
    include_date = bool(payload.get("include_date", True))
    probe = bool(payload.get("probe", False))
    source_xml = await _display_source_snapshot(device, probe)
    now = _now_playing_metadata(source_xml.get("/now_playing", ""))
    network = _network_signal_metadata(source_xml.get("/networkInfo", ""))
    battery = {"supported": False, "status": "removed", "label": "Batterieabfrage deaktiviert"}
    status_label = _build_display_status_label(mode, now, network, battery, include_date)
    station_id = payload.get("station_id")
    direct_xml = ""
    direct_path = "/select"
    if station_id:
        station = db.query(Station).filter(Station.id == int(station_id)).one_or_none()
        if station is None:
            raise HTTPException(status_code=404, detail="station not found")
        location = _station_location_or_409(StationDescriptor(station.name, station.stream_url, station.image_url, station.provider_station_id), db, request)
        direct_xml = _direct_metadata_select_xml(station.name, status_label, location, station.image_url)
    stream_url = str(payload.get("stream_url") or "").strip()
    if stream_url and not direct_xml:
        direct_xml = _direct_metadata_select_xml(str(payload.get("base_label") or now.get("label") or "Basswiesn"), status_label, stream_url, str(payload.get("image_url") or ""))
    return {
        "device_id": device.device_id,
        "target": device.ip_address,
        "mode": mode,
        "probe": probe,
        "sources": {key: ("ok" if value and not value.startswith("ERROR") else value or "not probed") for key, value in source_xml.items()},
        "parsed": {"now_playing": now, "network": network, "battery": battery, "clock": _display_clock_label(include_date)},
        "display_state": {
            "preferences": {"is_clock_displayed": "clock" in _display_metadata_fields(mode)},
            "clock": {"label": _display_clock_label(include_date), "maximum_update_rate": "once per minute"},
            "metadata": now,
            "battery_overlay": {**battery, "update_rule": "removed from regular runtime"},
            "network_overlay": {**network, "update_rule": "redraw only when connection, SSID or signal changes"},
            "power_state": "",
        },
        "status_label": status_label,
        "direct_without_overlay": {
            "possible": bool(direct_xml),
            "path": direct_path,
            "xml": direct_xml,
            "rule": "/select starts playback and carries normal station metadata only. It is not used to fake clock or WiFi overlays.",
        },
        "overlay_fallback": {
            "possible": False,
                "cli17000_command": f'vm btesttext "{status_label.replace(chr(34), chr(39))}"',
            "rule": "vm btesttext is debug-only and disabled for productive overlays because repeated CLI wake/redraw activity is unreliable.",
        },
        "native_protocol_status": {"OledUI": "architecture confirmed; field numbers not byte-exact enough for writes", "ClockDisplay": "local HTTP clockConfig is implemented; arbitrary OledUI protobuf injection remains disabled"},
        "evidence": ["/now_playing stationName/itemName/track/artist drives normal metadata", "/networkInfo WiFi interface signal gives EXCELLENT/GOOD/FAIR/POOR", "Batterie-Metadaten werden im regulären Lauf nicht aktiv abgefragt", "vm btesttext is a CLI17000 debug path, not the normal metadata path"],
    }


@router.post("/devices/{device_id}/display/direct-select")
async def display_direct_select(device_id: str, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    dry_run = payload.get("dry_run", True)
    if type(dry_run) is not bool:
        raise HTTPException(status_code=400, detail="dry_run must be a JSON boolean")
    # Preview is pure/cached. The live service owns and identifies all actual
    # reads; metadata preview must not create a transport ahead of that gate.
    preview = await display_metadata_preview(device_id, {**payload, "probe": False}, request, db)
    xml = preview["direct_without_overlay"].get("xml", "")
    if not xml:
        raise HTTPException(status_code=400, detail="station_id or stream_url required for direct metadata select")
    if dry_run:
        return {"dry_run": True, "device_id": device.device_id, "target": device.ip_address, "path": "/select", "xml": xml, "status_label": preview["status_label"], "note": preview["direct_without_overlay"]["rule"]}
    try:
        result = await select_direct(db, device, xml, client_factory=SoundTouchClient,
                                     guard=enforce_ip_write_guard, expected_previous=payload.get("expected_previous"))
    except SelectionError as exc:
        raise HTTPException(status_code=409, detail=exc.result) from None
    result["status_label"] = preview["status_label"]
    if result["status"] == "PENDING_READBACK":
        return JSONResponse(result, status_code=202)
    return result


@router.post("/devices/{device_id}/key")
async def send_key_command(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    return await _execute_key_command(device_id, payload, db)


async def _execute_key_command(device_id: str, payload: dict, db: Session, *, participants=None) -> dict:
    """HTTP-compatible adapter; participant scope is supplied by server code only."""
    device = _device_or_404(db, device_id)
    key = str(payload.get("key") or "").strip().upper()
    if key not in {item["key"] for item in KEY_COMMANDS}:
        raise HTTPException(status_code=400, detail="unsupported key command")
    safe_volume = payload.get("safe_volume")
    if safe_volume is not None:
        if type(safe_volume) is not int or not 0 <= safe_volume <= 100:
            raise HTTPException(status_code=400, detail="safe_volume must be an integer 0..100")
    if payload.get("dry_run", False):
        return {"dry_run": True, "device_id": device.device_id, "target": device.ip_address, "path": "/key",
                "sequence": [f'<key state="{state}" sender="Gabbo">{key}</key>' for state in ("press", "release")]}
    enforce_ip_write_guard(db, device)
    policy = policy_for_device(device, db)
    if key == "POWER" and not policy.allow_automatic_power_key and not is_yes_confirmation(payload.get("confirmation")):
        raise HTTPException(status_code=409, detail={"error": "device policy confirmation required",
                                                    "device_id": device.device_id, "expected": "YES"})
    try:
        return await execute_key(
            db, device, key, safe_volume=safe_volume, participants=participants,
            trigger=str(payload.get("trigger") or "api"),
            client_factory=lambda item: _soundtouch_client_for(item, purpose="key_command",
                trigger=str(payload.get("trigger") or "api"), policy=policy_for_device(item, db)),
            guard=enforce_ip_write_guard,
        )
    except KeyActionError as exc:
        code = exc.detail["code"]
        status = 502 if code in {"KEY_READBACK_MISMATCH", "KEY_OUTCOME_UNKNOWN", "KEY_RESULT_COMMIT_FAILED"} else 409
        raise HTTPException(status_code=status, detail=exc.detail) from None

@router.post("/devices/{device_id}/display/settings")
async def save_device_display_settings(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    mode = str(payload.get("mode") or "station_clock")
    if mode not in {item["key"] for item in DISPLAY_METADATA_MODES}:
        raise HTTPException(status_code=400, detail="unsupported display metadata mode")
    _display_metadata_fields(mode)
    for key, value in {f"display_mode_{device.device_id}": mode}.items():
        row = db.query(Setting).filter(Setting.key == key).one_or_none()
        if row is None:
            row = Setting(key=key)
            db.add(row)
        row.value = value
    db.commit()
    selected = next(item for item in DISPLAY_METADATA_MODES if item["key"] == mode)
    return {"device_id": device.device_id, "mode": selected, "battery_polling_removed": True, "note": "Display plan saved. Direct mode uses /select ContentItem metadata; regular battery polling is disabled."}


@router.post("/devices/{device_id}/display-recovery/plan")
async def display_recovery_plan(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    mode = str(payload.get("mode") or "pixel_wash")
    if mode not in {"pixel_wash", "inverse_scroll", "black_white_cycle"}:
        raise HTTPException(status_code=400, detail="unsupported recovery mode")
    minutes = max(1, min(60, int(payload.get("minutes") or 10)))
    temp_dir = f"/tmp/basswiesn-display-recovery-{device.device_id}"
    return {
        "dry_run": True,
        "device_id": device.device_id,
        "target": device.ip_address,
        "mode": mode,
        "minutes": minutes,
        "writes_radio": False,
        "preferred_runtime": "HTTP /key + display text/runtime hooks where available; avoid persistent files",
        "temp_dir": temp_dir,
        "cleanup_plan": [
            f"rm -rf {temp_dir}",
            "find /tmp -maxdepth 1 -name 'basswiesn-display-recovery-*' -type d -exec rm -rf {} \\;",
            "sync",
        ],
        "memory_rule": "No 30 MB persistent recovery assets on the radio. If a future method must transfer data, write only to /tmp and delete it at stop.",
    }


@router.post("/devices/{device_id}/telnet/plan")
async def device_telnet_plan(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    from basswiesn.app.services.telnet_device_control import REBOOT_CONFIRMATION, telnet_capabilities

    command_key = payload.get("command_key", "")
    selected = next((item for item in TELNET_COMMANDS if item["key"] == command_key), None)
    if selected is None:
        raise HTTPException(status_code=400, detail="validated command_key is required")
    if selected["key"] != "sys_reboot":
        return {
            "dry_run": True,
            "execution_enabled": False,
            "device_id": device.device_id,
            "target": device.ip_address,
            "port": 17000,
            "command_key": selected["key"],
            "mode": selected["mode"],
            "note": "BASSWIESN plant Telnet nur fuer feste, profilbasierte Aktionen. Freie Kommandos werden nicht ausgefuehrt.",
        }
    capabilities = telnet_capabilities(db, device)
    return {
        "dry_run": True,
        "execution_enabled": bool(capabilities.get("supported")),
        "device_id": device.device_id,
        "target": device.ip_address,
        "port": capabilities.get("command_port", 17000),
        "command_key": selected["key"],
        "mode": selected["mode"],
        "confirmation": REBOOT_CONFIRMATION,
        "capabilities": capabilities,
        "note": "Telnet-Reboot ist nur manuell, profilbasiert und mit persistierendem Job erlaubt.",
    }


@router.put("/devices/{device_id}/maintenance-reboot")
async def configure_maintenance_reboot(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    settings = get_settings()
    interval = int(payload.get("interval_hours", device.maintenance_reboot_interval_hours or settings.maintenance_reboot_default_interval_hours))
    if not settings.maintenance_reboot_min_interval_hours <= interval <= settings.maintenance_reboot_max_interval_hours:
        raise HTTPException(status_code=422, detail={"error": "interval_out_of_range", "minimum": settings.maintenance_reboot_min_interval_hours, "maximum": settings.maintenance_reboot_max_interval_hours})
    enabled = bool(payload.get("enabled", False))
    if enabled:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "automatic_radio_reboot_disabled",
                "message": "Automatische Radio-Reboots sind deaktiviert. Ein Reboot ist nur manuell im LAB erlaubt.",
            },
        )
    device.maintenance_reboot_enabled = False
    device.maintenance_reboot_interval_hours = interval
    device.maintenance_next_run_at = None
    device.maintenance_phase = "idle"
    db.commit()
    return {"device_id": device.device_id, "enabled": False, "automatic_reboot": False, "manual_lab_only": True, "interval_hours": interval, "next_run_at": None}


@router.post("/devices/{device_id}/maintenance-reboot/run")
async def run_safe_maintenance_reboot(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    lab = db.query(Setting).filter(Setting.key == "lab_mode").one_or_none()
    if not get_settings().lab_mode and (lab is None or str(lab.value).strip().lower() != "true"):
        raise HTTPException(status_code=403, detail={"error": "experimental_lab_only", "message": "Radio-Reboot ist nur im aktivierten LAB-Modus erlaubt."})
    if str(payload.get("confirmation") or "").strip() != "REBOOT RADIO":
        raise HTTPException(status_code=409, detail={"error": "confirmation_required", "confirmation": "REBOOT RADIO"})
    enforce_ip_write_guard(db, device)
    from basswiesn.app.services.maintenance_reboot import run_maintenance_reboot
    result = await run_maintenance_reboot(device, db, trigger="manual_lab")
    if not result.get("ok") and result.get("code") == "DEVICE_UNREACHABLE":
        return JSONResponse(status_code=503, content={**result, "device_id": device.device_id, "ip_address": device.ip_address})
    return result


@router.get("/devices/{device_id}/action-journal")
async def device_action_journal(device_id: str, limit: int = 100, db: Session = Depends(get_db)) -> list[dict]:
    _device_or_404(db, device_id)
    rows = db.query(DeviceActionJournal).filter(DeviceActionJournal.device_id == device_id).order_by(DeviceActionJournal.ts.desc()).limit(min(max(limit, 1), 500)).all()
    def safe_json(value: str) -> object:
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError):
            parsed = {"unparsed": str(value or "")}
        return redact_payload(parsed, anonymize_ips=False)
    return [{"ts": row.ts.isoformat(), "job_id": row.job_id, "device_id": row.device_id, "ip_address": row.ip_address, "action": row.action, "trigger": row.trigger, "phase": row.phase, "requested_state": safe_json(getattr(row, "requested_state", "{}")), "backup_ref": redact_text(getattr(row, "backup_ref", ""), anonymize_ips=False), "before_state": safe_json(row.before_state), "result": redact_text(row.result, anonymize_ips=False), "readback": safe_json(getattr(row, "readback", "{}")), "rollback_ref": redact_text(getattr(row, "rollback_ref", ""), anonymize_ips=False), "after_state": safe_json(row.after_state), "duration_ms": row.duration_ms, "error_category": redact_text(row.error_category, anonymize_ips=False), "verified": row.verified} for row in rows]


@router.get("/preset-profiles")
async def preset_profiles(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(PresetProfile).order_by(PresetProfile.name).all()
    return [{"id": row.id, "name": row.name, "description": row.description, "slots": json.loads(row.slots_json or "[]"), "updated_at": row.updated_at.isoformat()} for row in rows]


@router.post("/preset-profiles")
async def create_preset_profile(payload: dict, db: Session = Depends(get_db)) -> dict:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="profile name is required")
    slots = payload.get("slots") or []
    if len(slots) > 6:
        raise HTTPException(status_code=400, detail="preset profile supports max 6 slots")
    normalized = []
    for idx, slot in enumerate(slots, start=1):
        normalized.append({"button": int(slot.get("button") or idx), "station_id": int(slot["station_id"]) if slot.get("station_id") else None, "label": slot.get("label", "")})
    row = db.query(PresetProfile).filter(PresetProfile.name == name).one_or_none()
    if row is None:
        row = PresetProfile(name=name)
        db.add(row)
    row.description = payload.get("description", "")
    row.slots_json = json.dumps(normalized)
    row.updated_at = utc_now()
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "slots": normalized}


@router.post("/preset-profiles/{profile_id}/apply/{device_id}")
async def apply_preset_profile(profile_id: int, device_id: str, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    dry_run = payload.get("dry_run", True)
    if type(dry_run) is not bool or (not dry_run and payload.get("memory_checked") is not True):
        raise HTTPException(status_code=409, detail="explicit boolean preview/write and memory confirmation required")
    profile = db.query(PresetProfile).filter(PresetProfile.id == profile_id).one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail="preset profile not found")
    device = _device_or_404(db, device_id)
    _require_memory_checked(device, payload)
    if not dry_run:
        enforce_ip_write_guard(db, device)
    slots = json.loads(profile.slots_json or "[]")
    preview = []
    prepared = {}
    for slot in slots:
        station_id = slot.get("station_id")
        if not station_id:
            continue
        button = slot.get("button")
        if type(button) is not int or button not in range(1, 7) or button in prepared:
            raise HTTPException(status_code=409, detail="preset profile contains invalid or duplicate slots")
        station = db.query(Station).filter(Station.id == station_id).one_or_none()
        if station is None:
            raise HTTPException(status_code=409, detail="preset profile refers to a missing station")
        descriptor = StationDescriptor(station.name, station.stream_url, station.image_url, station.provider_station_id)
        location = _station_location_or_409(descriptor, db, request)
        preview.append({"button": button, "station_id": station.id, "station_name": station.name, "location": location})
        prepared[button] = {"button": button, "station_id": station.id,
            "source": "LOCAL_INTERNET_RADIO", "source_account": "", "location": location,
            "content_item_xml": content_item_xml(station, location)}
    if not dry_run:
        from basswiesn.app.routers.stations_presets import preset_write_ports
        from basswiesn.app.services.preset_writer import apply_prepared_presets, PresetWriteFailure
        try:
            radio_rows = await apply_prepared_presets(device, prepared, db,
                ports=preset_write_ports(), backup_label=f"preset-profile/{profile.id}")
        except PresetWriteFailure as exc:
            raise HTTPException(status_code=502, detail=exc.detail) from exc
        return {"dry_run": False, "verified": True, "profile_id": profile.id, "device_id": device_id, "slots": preview, "radio_slots": radio_rows, "memory_check": _memory_check_plan(device)}
    return {"dry_run": True, "profile_id": profile.id, "device_id": device_id, "slots": preview, "memory_check": _memory_check_plan(device)}
