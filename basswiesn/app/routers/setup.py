import asyncio
import base64
from datetime import UTC, datetime
from html import escape as html_escape
import json
from pathlib import Path
import re
import secrets
from types import SimpleNamespace
from uuid import uuid4
import zlib
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings, is_safe_radio_host, scan_cidr_for_host
from basswiesn.app.core.setup_mode import setup_confirmation_allowed
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app import db as app_db
from basswiesn.app.db import SessionLocal, get_db
from basswiesn.app.models import ConfigBackup, Device, RuntimeState, SetupPlan, Station, TelemetryEvent, Setting
from basswiesn.app.routers import api as api_core
from basswiesn.app.routers.stations_presets import import_presets_from_radio_backup, play_station_on_device, preset_status
from basswiesn.app.services.orion import OrionLocationError, StationDescriptor, station_location
from basswiesn.app.services.xml import content_item_xml
from basswiesn.app.services.provider_registry import RECOMMENDED_SOURCE_TYPES, persistence_sources_xml
from basswiesn.app.services.network_security import (
    pinned_http_target,
    validate_outbound_http_url,
)
from basswiesn.app.services.protected_devices import is_protected_ip
from basswiesn.app.routers.shared import enforce_ip_write_guard

router = APIRouter(prefix="/api", tags=["setup"])

SETUP_JOB_STEPS = [
    "volume_safety",
    "ssh_preflight",
    "factory_fix",
    "cloud_route",
    "source_bootstrap",
    "host_redirect",
    "reboot",
    "verify",
    "volume_safety_verify",
    "preset_checker",
    "activation_playback",
    "done",
]

_SETUP_JOBS: dict[str, dict] = {}
_SETUP_LATEST_JOB_ID = ""


def _legacy_setup_retired() -> None:
    """Disable the pre-rebuild write runtime and point to the new engine."""

    raise HTTPException(
        status_code=410,
        detail={
            "error": "legacy_setup_retired",
            "message": "Der alte Setup-Runtimepfad ist deaktiviert.",
            "replacement": "/api/setup/rebuild",
        },
    )


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()

FACTORY_SOURCES_XML = persistence_sources_xml()
SOURCE_BOOTSTRAP_RETRY_DELAYS = (5, 10, 15, 20, 30)
LEGACY_SOURCE_BOOTSTRAP_RETRY_DELAYS = (10, 20, 30, 45, 60, 90)
FACTORY_REBOOT_SETTLE_SECONDS = 45
SETUP_ACTIVATION_SECONDS = 30
ACTIVATION_CANDIDATE_POOL = [
    {"name": "BASSWIESN Activation MP3 128", "stream_url": "https://dispatcher.rndfnk.com/br/br1/obb/mp3/mid", "format": "mp3", "priority": 10},
    {"name": "BASSWIESN Activation MP3 Backup", "stream_url": "https://stream.live.vc.bbcmedia.co.uk/bbc_world_service", "format": "mp3", "priority": 20},
    {"name": "BASSWIESN Activation AAC", "stream_url": "https://dispatcher.rndfnk.com/br/br1/obb/aac/low", "format": "aac", "priority": 30},
    {"name": "BASSWIESN Activation OGG", "stream_url": "https://streams.radiobob.de/bob-live/mp3-192/mediaplayer", "format": "ogg", "priority": 40},
    {"name": "BASSWIESN Activation HLS Fallback", "stream_url": "https://example.com/live.m3u8", "format": "hls", "priority": 90},
]


def _saved_lan_host(db: Session | None = None) -> str:
    owns_session = db is None
    session = db or app_db.SessionLocal()
    try:
        row = session.query(Setting).filter(Setting.key == "lan_host").one_or_none()
        return (row.value if row else "").strip()
    finally:
        if owns_session:
            session.close()


def _setup_target_host(payload: dict, request: Request) -> str:
    settings = get_settings()
    requested = str(payload.get("host") or "").strip()
    if requested:
        return api_core._validated_setup_host(requested)
    saved_host = _saved_lan_host()
    if saved_host:
        return api_core._validated_setup_host(saved_host)
    if settings.lan_host_configured:
        return api_core._validated_setup_host(settings.lan_host)
    if settings.local_base_url_configured:
        configured_host = urlparse(settings.local_base_url).hostname or ""
        if configured_host:
            return api_core._validated_setup_host(configured_host)
    browser_host = request.url.hostname or ""
    if is_safe_radio_host(browser_host):
        return api_core._validated_setup_host(browser_host)
    return api_core._validated_setup_host(api_core._outbound_lan_ip())


def _require_setup_write_allowed(device, db: Session | None = None) -> None:
    if db is not None:
        enforce_ip_write_guard(db, device)
        return
    allowed = get_settings().setup_write_radio_ips
    if not allowed or device.ip_address not in allowed:
        raise HTTPException(
            status_code=403,
            detail={"error": "radio is not allowed for live setup writes", "radio_ip": device.ip_address, "configured_allowlist": list(allowed)},
        )


def _setup_device_status(device: Device) -> dict:
    """Return the retired batch-setup snapshot without network side effects.

    This compatibility endpoint used to probe ports 22 and 17000 for every
    stored device. A passive browser request must never open radio transports;
    explicit Setup 2.0 preflight routes perform guarded probes instead.
    """

    probe_message = "not probed; use an explicit Setup 2.0 preflight"
    return {
        "device_id": device.device_id,
        "name": device.name,
        "ip": device.ip_address,
        "model": device.model,
        "configured_for": api_core.device_summary(device).get("configured_for", "unknown"),
        "ssh_ready": False,
        "ssh_status": "not_probed",
        "ssh_message": probe_message,
        "port_17000_available": False,
        "port_17000_status": "not_probed",
        "port_17000_message": probe_message,
        "remote_services_needed": None,
        "ready_status": "not_probed",
        "network_probe": False,
        "retired": True,
    }


@router.get("/setup/devices")
async def setup_devices(db: Session = Depends(get_db)) -> list[dict]:
    return [_setup_device_status(device) for device in db.query(Device).order_by(Device.name).all()]


def _job_public(job: dict) -> dict:
    return json.loads(json.dumps(job, ensure_ascii=False))


def _setup_job_is_stale_success(job: dict, *, now: datetime | None = None) -> bool:
    if job.get("running"):
        return False
    summary = job.get("summary") or {}
    if int(summary.get("failed") or 0) or int(summary.get("cancelled") or 0) or job.get("error"):
        return False
    finished_at = job.get("finished_at")
    if not finished_at:
        return False
    try:
        finished = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - finished).total_seconds() >= 20 * 60


def _persist_setup_job(job: dict) -> None:
    db = SessionLocal()
    try:
        key = f"setup_job:{job['job_id']}"
        row = db.query(RuntimeState).filter(RuntimeState.key == key).one_or_none()
        if row is None:
            row = RuntimeState(key=key, value="")
            db.add(row)
        row.value = json.dumps(_job_public(job), ensure_ascii=False)
        latest = db.query(RuntimeState).filter(RuntimeState.key == "setup_job:latest").one_or_none()
        if latest is None:
            latest = RuntimeState(key="setup_job:latest", value="")
            db.add(latest)
        latest.value = job["job_id"]
        db.commit()
    finally:
        db.close()


def _load_persisted_setup_job(job_id: str) -> dict | None:
    db = SessionLocal()
    try:
        row = db.query(RuntimeState).filter(RuntimeState.key == f"setup_job:{job_id}").one_or_none()
        return json.loads(row.value) if row and row.value else None
    finally:
        db.close()


def _latest_persisted_setup_job_id() -> str:
    db = SessionLocal()
    try:
        row = db.query(RuntimeState).filter(RuntimeState.key == "setup_job:latest").one_or_none()
        return row.value if row and row.value else ""
    finally:
        db.close()


def _new_job_device(device: Device) -> dict:
    return {
        "device_id": device.device_id,
        "ip": device.ip_address,
        "name": device.name,
        "status": "queued",
        "step": "",
        "step_label": "",
        "started_at": None,
        "finished_at": None,
        "estimated_seconds": 390,
        "error": None,
        "ssh_ready": None,
        "port_17000_available": None,
        "remote_services_needed": None,
    }


def _set_job_device(job: dict, device_id: str, **changes) -> None:
    for item in job["devices"]:
        if item["device_id"] == device_id:
            item.update(changes)
            _persist_setup_job(job)
            return


async def _job_step(job: dict, item: dict, step: str, label: str, delay: float = 0.02) -> None:
    if job.get("cancel_requested"):
        raise asyncio.CancelledError()
    job["current_device_id"] = item["device_id"]
    _set_job_device(job, item["device_id"], status="running", step=step, step_label=label)
    write_masterlog("setup_device_step", job_id=job["job_id"], device_id=item["device_id"], step=step)
    await asyncio.sleep(delay)


def _exception_message(exc: BaseException, fallback: str = "unknown error") -> str:
    text = str(exc).strip()
    return text or f"{exc.__class__.__name__}: {fallback}"


def _legacy_readiness_profile(device: Device) -> bool:
    model = (device.model or "").lower()
    firmware = (device.firmware or "").lower()
    return any(name in model for name in ("soundtouch 20", "soundtouch 30", "portable")) or firmware.startswith("27.")


def _http_error_snapshot(exc: BaseException, endpoint: str, attempt: int) -> dict:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    body = getattr(response, "text", "") if response is not None else ""
    return {
        "endpoint": endpoint,
        "attempt": attempt,
        "status_code": status_code,
        "response_body_preview": str(body or "")[:500],
        "exception_class": exc.__class__.__name__,
        "message": _exception_message(exc, "source bootstrap failed"),
    }


async def _source_bootstrap_readiness(device, host: str, port: int, attempt: int) -> dict:
    if is_protected_ip(host):
        raise HTTPException(
            status_code=403,
            detail="BASSWIESN server target is a protected device address",
        )
    registry_url = f"http://{host}:{port}/bmx/registry/v1/services"
    registry_validation = validate_outbound_http_url(registry_url)
    if not registry_validation.ok:
        raise HTTPException(status_code=400, detail=registry_validation.reason)
    pinned_registry_url, registry_headers, registry_extensions = pinned_http_target(
        registry_url, registry_validation
    )
    result = {"attempt": attempt, "info": False, "sources": False, "now_playing": False, "presets": None, "serviceAvailability": None, "bmx_registry": False}
    client = api_core.SoundTouchClient(device.ip_address)
    try:
        result["info"] = bool((await client.get_xml("/info")).strip())
    except Exception as exc:
        result["info_error"] = _exception_message(exc, "/info failed")
    try:
        result["sources"] = bool((await client.get_xml("/sources")).strip())
    except Exception as exc:
        result["sources_error"] = _exception_message(exc, "/sources failed")
    try:
        result["now_playing"] = bool((await client.get_xml("/now_playing")).strip())
    except Exception as exc:
        result["now_playing_error"] = _exception_message(exc, "/now_playing failed")
    try:
        result["presets"] = bool((await client.get_xml("/presets")).strip())
    except Exception as exc:
        result["presets_error"] = _exception_message(exc, "/presets unavailable")
    try:
        result["serviceAvailability"] = bool((await client.get_xml("/serviceAvailability")).strip())
    except Exception as exc:
        result["serviceAvailability_error"] = _exception_message(exc, "/serviceAvailability unavailable")
    try:
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=False, trust_env=False) as http:
            response = await http.get(
                pinned_registry_url,
                headers=registry_headers,
                extensions=registry_extensions,
            )
            result["bmx_registry"] = response.status_code == 200
            result["bmx_status_code"] = response.status_code
    except Exception as exc:
        result["bmx_error"] = _exception_message(exc, "BMX registry failed")
    write_masterlog("source_bootstrap_readiness", device_id=device.device_id, radio_ip=device.ip_address, **result)
    return result


async def _source_bootstrap_attempt(device, account_id: str, db: Session) -> dict:
    pairing = await _pair_local_account(device, account_id, db)
    defaults = await _apply_setup_defaults(device, db)
    db.add(TelemetryEvent(
        device_id=device.device_id,
        event_type="setup_source_bootstrap",
        endpoint="basswiesn",
        payload=json.dumps({"account_pairing": pairing, "defaults": defaults}, ensure_ascii=False),
        parsed_summary="source bootstrap completed",
    ))
    db.commit()
    return {"account_pairing": pairing, "defaults": defaults}


async def _source_bootstrap_with_retries(job: dict, item: dict, device, account_id: str, db: Session, host: str, port: int, cli_open: bool, ssh_open: bool, delays: tuple[int, ...] = SOURCE_BOOTSTRAP_RETRY_DELAYS) -> dict:
    endpoint = "/setMargeAccount"
    last_error: dict = {}
    if delays == SOURCE_BOOTSTRAP_RETRY_DELAYS and _legacy_readiness_profile(device):
        delays = LEGACY_SOURCE_BOOTSTRAP_RETRY_DELAYS
    write_masterlog("source_bootstrap_start", job_id=job["job_id"], device_id=device.device_id, radio_ip=device.ip_address, account_id=account_id)
    for cycle in (1, 2):
        for index, delay in enumerate(delays, start=1):
            attempt = ((cycle - 1) * len(delays)) + index
            try:
                readiness = await _source_bootstrap_readiness(device, host, port, attempt)
                required = {"info": readiness.get("info"), "sources": readiness.get("sources"), "now_playing": readiness.get("now_playing", True)}
                if not all(required.values()):
                    missing = ", ".join(name for name, ok in required.items() if not ok)
                    raise OSError(f"radio not ready for source bootstrap: {missing}")
                result = await _source_bootstrap_attempt(device, account_id, db)
                write_masterlog("source_bootstrap_complete", job_id=job["job_id"], device_id=device.device_id, attempts=attempt)
                return result
            except Exception as exc:
                db.rollback()
                last_error = _http_error_snapshot(exc, endpoint, attempt)
                write_masterlog("source_bootstrap_retry", job_id=job["job_id"], device_id=device.device_id, **last_error)
                item.update({"step_label": f"Source Bootstrap retry {index}/{len(delays)}"})
                _persist_setup_job(job)
                if index < len(delays):
                    await asyncio.sleep(delay)
        if cycle == 1 and (cli_open or ssh_open):
            write_masterlog("source_bootstrap_reboot_retry", job_id=job["job_id"], device_id=device.device_id, last_error=last_error)
            try:
                if cli_open:
                    await api_core._send_cli17000(device.ip_address, ["sys reboot"])
                else:
                    await _factory_ssh(device, "reboot", 10)
            except Exception as exc:
                write_masterlog("source_bootstrap_reboot_retry", job_id=job["job_id"], device_id=device.device_id, reboot_error=_exception_message(exc, "reboot failed"))
            await api_core._wait_for_radio_http(device, initial_delay=75, attempts=4, interval=5)
            continue
        break
    status = last_error.get("status_code")
    message = last_error.get("message") or "source bootstrap did not complete"
    attempts = last_error.get("attempt", len(delays))
    final = f"source_bootstrap failed after {attempts} attempts: endpoint={endpoint} status={status or 'n/a'} error={message}"
    write_masterlog("source_bootstrap_failed", job_id=job["job_id"], device_id=device.device_id, endpoint=endpoint, status_code=status, attempts=attempts, error=final)
    raise OSError(final)


def _now_playing_active(xml_text: str) -> bool:
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError:
        return False
    source = (root.attrib.get("source") or "").upper()
    if source in {"STANDBY", "INVALID_SOURCE"}:
        return False
    status = " ".join((root.findtext(path, "") or "") for path in ("playStatus", "playbackStatus")).upper()
    return "PLAY" in status or root.find(".//ContentItem") is not None


def _activation_candidates(db: Session) -> list[Station]:
    rows = []
    for row in db.query(Station).order_by(Station.compatibility_score.desc(), Station.id).all():
        url = (row.stream_url or "").lower()
        if not url.startswith(("http://", "https://")):
            continue
        rows.append(row)
    for candidate in sorted(ACTIVATION_CANDIDATE_POOL, key=lambda item: item["priority"]):
        row = db.query(Station).filter(Station.stream_url == candidate["stream_url"]).one_or_none()
        if row is None:
            row = Station(name=candidate["name"], stream_url=candidate["stream_url"], provider="LOCAL_INTERNET_RADIO", stream_format=candidate["format"], stream_mime="audio/mpeg" if candidate["format"] == "mp3" else "", compatibility_score=max(0, 100 - int(candidate["priority"])), is_hls=1 if candidate["format"] == "hls" else 0, is_direct_audio=0 if candidate["format"] == "hls" else 1, internal=True, purpose="activation", lab_only=True)
            db.add(row)
            db.commit()
            db.refresh(row)
        if row not in rows:
            rows.append(row)
    return rows


def _activation_station(db: Session) -> Station:
    candidates = _activation_candidates(db)
    if candidates:
        return candidates[0]
    row = Station(name="BASSWIESN Activation User Station Required", stream_url="", provider="LOCAL_INTERNET_RADIO", internal=True, purpose="activation", lab_only=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


async def _run_setup_activation_playback(device: Device, db: Session, *, duration_seconds: int = SETUP_ACTIVATION_SECONDS) -> dict:
    client = api_core.SoundTouchClient(device.ip_address)
    failures = []
    for station in _activation_candidates(db):
        write_masterlog("setup_activation_candidate_test", device_id=device.device_id, radio_ip=device.ip_address, station_id=station.id, stream_url=station.stream_url, stream_format=station.stream_format)
        try:
            write_masterlog("setup_activation_playback_start", device_id=device.device_id, radio_ip=device.ip_address, station_id=station.id)
            result = await play_station_on_device(
                device.device_id,
                station.id,
                {"dry_run": False, "safe_volume": 3, "trigger": "setup_activation", "trigger_type": "setup_activation", "source_type": "setup_activation", "internal_event": True},
                db,
                db,
            )
            deadline = datetime.now(UTC).timestamp() + max(0, duration_seconds)
            last_now_playing = ""
            active_seen = False
            while True:
                last_now_playing = await client.get_xml("/now_playing")
                active_seen = _now_playing_active(last_now_playing)
                write_masterlog("setup_activation_playback_now_playing", device_id=device.device_id, radio_ip=device.ip_address, active=active_seen, station_id=station.id)
                if not active_seen:
                    break
                if datetime.now(UTC).timestamp() >= deadline:
                    break
                await asyncio.sleep(1)
            if not active_seen:
                raise OSError(f"activation candidate did not stay active: {station.name}")
            now = _iso_now()
            for key, value in {
                f"device:{device.device_id}:first_playback_activation_done": "true",
                f"device:{device.device_id}:first_playback_activation_at": now,
            }.items():
                row = db.query(Setting).filter(Setting.key == key).one_or_none()
                if row is None:
                    row = Setting(key=key)
                    db.add(row)
                row.value = value
            db.commit()
            write_masterlog("setup_activation_playback_complete", device_id=device.device_id, radio_ip=device.ip_address, station_id=station.id, duration_seconds=duration_seconds)
            write_masterlog("setup_activation_marked_complete", device_id=device.device_id, radio_ip=device.ip_address, activated_at=now)
            return {"ok": True, "station_id": station.id, "duration_seconds": duration_seconds, "playback": result, "candidate_failures": failures}
        except Exception as exc:
            db.rollback()
            failures.append({"station_id": station.id, "name": station.name, "error": str(exc) or exc.__class__.__name__})
            write_masterlog("setup_activation_failed", device_id=device.device_id, radio_ip=device.ip_address, station_id=station.id, error=str(exc) or exc.__class__.__name__)
            continue
    raise OSError("Setup written, but no activation playback candidate succeeded. Add a known working MP3/AAC station and retry activation playback.")


async def _run_setup_device(job: dict, device_id: str, dry_run: bool, host: str, port: int) -> None:
    db = SessionLocal()
    item = next(row for row in job["devices"] if row["device_id"] == device_id)
    try:
        device = db.query(Device).filter(Device.device_id == device_id).one()
        item.update({"status": "running", "started_at": _iso_now()})
        write_masterlog("setup_device_start", job_id=job["job_id"], device_id=device.device_id, radio_ip=device.ip_address)

        await _job_step(job, item, "volume_safety", "volume safety")
        if not dry_run:
            _require_setup_write_allowed(device, db)
            await api_core.SoundTouchClient(device.ip_address).post_xml("/volume", "<volume>5</volume>")
            write_masterlog("volume_safety_set", device_id=device.device_id, radio_ip=device.ip_address, requested=5)

        await _job_step(job, item, "ssh_preflight", "ssh preflight")
        ssh_open, _ssh_message = api_core._tcp_port_open(device.ip_address, 22, timeout=1.0)
        cli_open, _cli_message = api_core._tcp_port_open(device.ip_address, 17000, timeout=1.0)
        item.update({"ssh_ready": ssh_open, "port_17000_available": cli_open, "remote_services_needed": not ssh_open and not cli_open})
        if not ssh_open and not cli_open:
            raise OSError("ssh unavailable; port 17000 unavailable; USB remote_services needed")

        await _job_step(job, item, "factory_fix", "factory fix")
        if not dry_run:
            if not ssh_open:
                raise OSError("factory fix failed: ssh unavailable")
            await factory_fix(device.device_id, {"dry_run": False}, None, db)

        await _job_step(job, item, "cloud_route", "cloud route")
        if not dry_run:
            if not cli_open:
                raise OSError("port 17000 unavailable")
            targets = api_core._cloud_route_targets(host, port)
            await api_core._send_cli17000(device.ip_address, api_core._setup_cli17000_commands(targets, reboot=False))

        await _job_step(job, item, "source_bootstrap", "source bootstrap")
        if not dry_run:
            account_id = _local_account_id(device.device_id)
            await _source_bootstrap_with_retries(job, item, device, account_id, db, host, port, cli_open, ssh_open)

        await _job_step(job, item, "host_redirect", "host redirect")
        if not dry_run and ssh_open:
            ssh_hosts = await api_core._read_ssh_hosts(device.ip_address)
            if ssh_hosts.get("available"):
                rewritten = api_core.rewrite_hosts(ssh_hosts.get("content", ""), host)
                await api_core._write_ssh_hosts(device.ip_address, rewritten, host)
                write_masterlog("hosts_redirect_complete", device_id=device.device_id, target_host=host)
            else:
                raise OSError("host redirect failed")

        await _job_step(job, item, "reboot", "reboot")
        if not dry_run:
            await api_core._send_cli17000(device.ip_address, ["sys reboot"])

        await _job_step(job, item, "verify", "verify")
        if not dry_run:
            legacy_profile = _legacy_readiness_profile(device)
            result = await api_core._wait_for_radio_http(device, initial_delay=90 if legacy_profile else 60, attempts=18 if legacy_profile else 12, interval=5)
            if not result.get("ok"):
                write_masterlog("setup_verify_retry", device_id=device.device_id, radio_ip=device.ip_address, first_error=result.get("last_error", "not reachable"))
                result = await api_core._wait_for_radio_http(device, initial_delay=30 if legacy_profile else 15, attempts=18 if legacy_profile else 12, interval=5)
            if not result.get("ok"):
                raise OSError(result.get("last_error") or "verify failed")
            write_masterlog("setup_verify_complete", device_id=device.device_id, status="ready")

        await _job_step(job, item, "volume_safety_verify", "volume safety verify")
        if not dry_run:
            volume_xml = await api_core.SoundTouchClient(device.ip_address).get_xml("/volume")
            verified = api_core._xml_text(volume_xml, "actualvolume")
            write_masterlog("volume_safety_verified", device_id=device.device_id, radio_ip=device.ip_address, volume=verified)

        await _job_step(job, item, "preset_checker", "preset checker refresh")
        if not dry_run:
            await preset_status(device.device_id, db)

        await _job_step(job, item, "activation_playback", "activation playback")
        if not dry_run:
            item["activation_playback"] = await _run_setup_activation_playback(device, db)

        item.update({"status": "ready", "step": "done", "step_label": "READY FOR BASSWIESN", "finished_at": _iso_now(), "error": None})
        write_masterlog("setup_device_complete", job_id=job["job_id"], device_id=device.device_id)
    except asyncio.CancelledError:
        db.rollback()
        item.update({"status": "cancelled", "finished_at": _iso_now(), "error": "cancelled"})
        write_masterlog("setup_device_failed", job_id=job["job_id"], device_id=device_id, error="cancelled")
    except Exception as exc:
        db.rollback()
        error_text = _exception_message(exc, "setup_device_failed")
        item.update({"status": "failed", "finished_at": _iso_now(), "error": error_text})
        write_masterlog("setup_device_failed", job_id=job["job_id"], device_id=device_id, error=error_text)
    finally:
        _persist_setup_job(job)
        db.close()


async def _run_setup_job(job_id: str, dry_run: bool, host: str, port: int) -> None:
    job = _SETUP_JOBS[job_id]
    try:
        for item in job["devices"]:
            if job.get("cancel_requested"):
                break
            await _run_setup_device(job, item["device_id"], dry_run, host, port)
        if job.get("cancel_requested"):
            for item in job["devices"]:
                if item["status"] == "queued":
                    item.update({"status": "cancelled", "finished_at": _iso_now(), "error": "cancelled"})
            job.update({"running": False, "finished_at": _iso_now(), "current_device_id": None, "summary": {"successful": sum(1 for item in job["devices"] if item["status"] == "ready"), "failed": sum(1 for item in job["devices"] if item["status"] == "failed"), "cancelled": sum(1 for item in job["devices"] if item["status"] == "cancelled")}})
            _persist_setup_job(job)
            write_masterlog("setup_batch_cancelled", job_id=job_id)
            return
        success = sum(1 for item in job["devices"] if item["status"] == "ready")
        failed = sum(1 for item in job["devices"] if item["status"] == "failed")
        job.update({"running": False, "finished_at": _iso_now(), "current_device_id": None, "summary": {"successful": success, "failed": failed, "cancelled": sum(1 for item in job["devices"] if item["status"] == "cancelled")}})
        _persist_setup_job(job)
        write_masterlog("setup_batch_complete", job_id=job_id, successful=success, failed=failed, failed_count=failed)
    except Exception as exc:
        job.update({"running": False, "finished_at": _iso_now(), "error": str(exc)})
        _persist_setup_job(job)


@router.post("/setup/jobs/start")
async def start_setup_job(payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    global _SETUP_LATEST_JOB_ID
    device_ids = [str(item).strip() for item in (payload.get("device_ids") or []) if str(item).strip()]
    if not device_ids:
        raise HTTPException(status_code=400, detail="device_ids is required")
    devices = [api_core._device_or_404(db, device_id) for device_id in device_ids]
    host = _setup_target_host(payload, request)
    port = int(payload.get("port") or get_settings().cloud_port)
    dry_run = bool(payload.get("dry_run", True))
    if not dry_run:
        _legacy_setup_retired()
    job_id = uuid4().hex
    job = {
        "job_id": job_id,
        "running": True,
        "dry_run": dry_run,
        "started_at": _iso_now(),
        "finished_at": None,
        "current_device_id": None,
        "cancel_requested": False,
        "devices": [_new_job_device(device) for device in devices],
        "summary": {"successful": 0, "failed": 0, "cancelled": 0},
    }
    _SETUP_JOBS[job_id] = job
    _SETUP_LATEST_JOB_ID = job_id
    _persist_setup_job(job)
    write_masterlog("setup_batch_start", job_id=job_id, devices=device_ids, dry_run=dry_run)
    asyncio.create_task(_run_setup_job(job_id, dry_run, host, port))
    return _job_public(job)


@router.get("/setup/jobs/latest")
async def latest_setup_job() -> dict:
    latest_job_id = _SETUP_LATEST_JOB_ID or _latest_persisted_setup_job_id()
    if not latest_job_id:
        raise HTTPException(status_code=404, detail="no setup job")
    job = _SETUP_JOBS.get(latest_job_id) or _load_persisted_setup_job(latest_job_id)
    if not job:
        raise HTTPException(status_code=404, detail="no setup job")
    if _setup_job_is_stale_success(job):
        raise HTTPException(status_code=404, detail="no active setup job")
    return _job_public(job)


@router.get("/setup/jobs/{job_id}")
async def get_setup_job(job_id: str) -> dict:
    job = _SETUP_JOBS.get(job_id) or _load_persisted_setup_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="setup job not found")
    return _job_public(job)


@router.post("/setup/jobs/{job_id}/cancel")
async def cancel_setup_job(job_id: str) -> dict:
    job = _SETUP_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="setup job not found")
    job["cancel_requested"] = True
    for item in job["devices"]:
        if item["status"] == "queued":
            item.update({"status": "cancelled", "finished_at": _iso_now(), "error": "cancelled"})
    _persist_setup_job(job)
    return _job_public(job)


@router.get("/ssh/remote-services-file")
async def download_remote_services_file() -> Response:
    return Response(
        content=b"",
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="remote_services"'},
    )


def _local_account_id(device_id: str) -> str:
    return str(1_000_000 + (zlib.crc32(device_id.encode("utf-8")) % 9_000_000))


async def _pair_local_account(device, account_id: str, db: Session) -> dict:
    client = api_core.SoundTouchClient(device.ip_address)
    before = await client.get_xml("/info")
    db.add(ConfigBackup(device_id=device.device_id, path="setup-account/before-info.xml", content=before))
    current = ET.fromstring(before).findtext("margeAccountUUID", "").strip()
    if current == account_id:
        return {"ok": True, "changed": False, "account_id": account_id, "message": "Account already paired"}
    info_root = ET.fromstring(before)
    base_url = info_root.findtext("margeURL", "").strip()
    if not base_url:
        raise HTTPException(status_code=409, detail="set the BASSWIESN cloud route before pairing the account")
    # FW 27.0.6 copies updateServer into its runtime margeURL during pairing.
    # Send the local base for both fields, then restore the real update URL via
    # the confirmed CLI fields below.
    token_file = get_settings().marge_auth_token_file.strip()
    if not token_file:
        raise HTTPException(status_code=503, detail="Marge auth token secret file is not configured")
    try:
        auth_token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Marge auth token secret file is unavailable") from exc
    if not auth_token or "\n" in auth_token or "\r" in auth_token or len(auth_token) > 4096:
        raise HTTPException(status_code=503, detail="Marge auth token secret file is invalid")
    xml = (
        f"<PairDeviceWithAccount><accountId>{account_id}</accountId>"
        f"<userAuthToken>{html_escape(auth_token)}</userAuthToken>"
        f"<boseServer>{base_url}</boseServer><updateServer>{base_url}</updateServer>"
        "<accountEmail>local@basswiesn.invalid</accountEmail></PairDeviceWithAccount>"
    )
    response = await client.post_xml("/setMargeAccount", xml)
    await api_core._send_cli17000(device.ip_address, [
        f'envswitch boseurls set "{base_url}" "{base_url.rstrip("/")}/updates/soundtouch"',
        f'sys configuration bmxRegistryUrl {base_url.rstrip("/")}/bmx/registry/v1/services',
        f'sys configuration statsServerUrl {base_url}',
    ])
    last_info = ""
    for _ in range(10):
        await asyncio.sleep(0.5)
        last_info = await client.get_xml("/info")
        if ET.fromstring(last_info).findtext("margeAccountUUID", "").strip() == account_id:
            device.info_xml = last_info
            db.commit()
            return {"ok": True, "changed": True, "account_id": account_id, "response": response}
    raise HTTPException(status_code=502, detail={"error": "radio did not persist local account", "account_id": account_id, "last_info": last_info})


async def _apply_setup_defaults(device, db: Session) -> dict:
    rows = {row.key: row.value for row in db.query(Setting).all()}
    timezone = rows.get("default_timezone", "Europe/Berlin")
    language = rows.get("device_language_default", "de")
    clock_path, clock_xml = api_core._setting_payload("clockConfig", {
        "timezoneInfo": timezone,
        "userEnable": True,
        "timeFormat": "TIME_FORMAT_24HOUR_ID",
        "userOffsetMinute": 0,
        "brightnessLevel": 70,
        "userUtcTime": 0,
    })
    language_path, language_xml = api_core._setting_payload("language", language)
    client = api_core.SoundTouchClient(device.ip_address)
    clock_response = await client.post_xml(clock_path, clock_xml)
    language_response = await client.post_xml(language_path, language_xml)
    # Original Stockholm setup flow explicitly leaves SetupAP after network
    # configuration.  Without this message a fully paired radio can remain on
    # source=SETUP and physical presets are ignored.
    setup_leave_response = await client.post_xml("/setup", '<setupState state="SETUP_WIFI_LEAVE" />')
    clock_verify = await client.get_xml(clock_path)
    language_verify = await client.get_xml(language_path)
    clock_root = ET.fromstring(clock_verify).find("clockConfig")
    language_id = int((ET.fromstring(language_verify).text or "0").strip() or 0)
    ok = bool(
        clock_root is not None
        and clock_root.attrib.get("timezoneInfo") == timezone
        and clock_root.attrib.get("userEnable", "false").lower() == "true"
        and api_core.LANGUAGE_CODES_BY_ID.get(language_id) in {language, "no" if language == "nb" else language}
    )
    if not ok:
        raise HTTPException(status_code=502, detail={"error": "setup defaults were not confirmed", "clock": clock_verify, "language": language_verify})
    return {"ok": True, "timezone": timezone, "standby_clock": True, "language": language, "clock_response": clock_response, "language_response": language_response, "setup_state": "SETUP_WIFI_LEAVE", "setup_leave_response": setup_leave_response}


@router.get("/setup/wizard/server-info")
async def setup_wizard_server_info(request: Request) -> dict:
    settings = get_settings()
    saved_host = _saved_lan_host()
    configured_host = saved_host or settings.lan_host
    configured = bool(saved_host or settings.lan_host_configured)
    lan_candidates = api_core._lan_ip_candidates()
    docker_candidate_ips = {
        str(candidate.get("ip", ""))
        for candidate in lan_candidates
        if any(token in str(candidate.get("source", "")).lower() for token in ("docker", "veth", "br-"))
    }
    candidates = [
        candidate
        for candidate in lan_candidates
        if candidate.get("ip") != configured_host
        and is_safe_radio_host(str(candidate.get("ip", "")))
        and str(candidate.get("ip", "")) not in docker_candidate_ips
    ]
    if is_safe_radio_host(configured_host):
        network = scan_cidr_for_host(configured_host)
        candidates.insert(0, {"ip": configured_host, "suggested_cidr": network, "source": "ui" if saved_host else "configuration" if settings.lan_host_configured else "auto-detection"})
    lan_ip = candidates[0]["ip"] if candidates else ""
    browser_host = request.url.hostname or ""
    browser_host_safe = is_safe_radio_host(browser_host) and browser_host not in docker_candidate_ips
    if browser_host_safe:
        candidates = [candidate for candidate in candidates if candidate.get("ip") != browser_host]
        candidates.insert(0, {"ip": browser_host, "suggested_cidr": scan_cidr_for_host(browser_host), "source": "browser"})
    if browser_host_safe:
        # The address used by the browser is direct reachability evidence for
        # this running instance.  It must win over a stale host saved on a
        # previous LAN; the saved address remains available as a candidate.
        recommended_host = browser_host
        host_source = "browser"
    elif configured:
        recommended_host = configured_host
        host_source = "ui" if saved_host else "environment"
    elif lan_ip and is_safe_radio_host(lan_ip):
        recommended_host = lan_ip
        host_source = "auto-detection"
    else:
        recommended_host = ""
        host_source = "unknown"
    host_safe = is_safe_radio_host(recommended_host)
    host_warning = None if host_safe else "Bitte LAN-IP des BASSWIESN Hosts eintragen; localhost, Docker-IPs und öffentliche Hosts sind für Radios nicht erreichbar."
    cloud_port = settings.cloud_port
    web_port = settings.web_port
    debug_port = settings.debug_port
    cloud_open, cloud_message = api_core._tcp_port_open("127.0.0.1", cloud_port, timeout=0.5)
    debug_open, debug_message = api_core._tcp_port_open("127.0.0.1", debug_port, timeout=0.5)
    external_cloud_open, external_cloud_message = api_core._tcp_port_open(recommended_host, cloud_port, timeout=0.5) if host_safe and recommended_host else (False, "no safe LAN host")
    url_host = recommended_host if host_safe else ""
    web_url = f"http://{url_host}:{web_port}" if url_host else ""
    cloud_base_url = f"http://{url_host}:{cloud_port}" if url_host else ""
    debug_base_url = f"http://{url_host}:{debug_port}" if url_host else ""
    result = {
        "recommended_host": recommended_host,
        "host_source": host_source,
        "host_safe": host_safe,
        "host_warning": host_warning,
        "detected_lan_ip": lan_ip,
        "browser_host": browser_host,
        "ip_candidates": candidates,
        "suggested_scan_cidr": scan_cidr_for_host(recommended_host) if host_safe else "",
        "web_url": web_url,
        "cloud_base_url": cloud_base_url,
        "debug_base_url": debug_base_url,
        "cloud_port": cloud_port,
        "debug_port": debug_port,
        "strategy": "cli17000_cloud_route",
        "fallback_strategy": "ssh_config_hosts_plan",
        "steps": api_core._setup_wizard_steps(),
        "local_services": {
            "cloud": {"ok": cloud_open, "message": cloud_message, "url": f"http://127.0.0.1:{cloud_port}/bmx/registry/v1/services"},
            "cloud_from_lan_host": {"ok": external_cloud_open, "message": external_cloud_message, "url": f"http://{recommended_host}:{cloud_port}/bmx/registry/v1/services"},
            "debug": {"ok": debug_open, "message": debug_message, "url": f"http://127.0.0.1:{debug_port}/health"},
        },
        "source_basis": [
            "BASSWIESN wizard pattern: server-info, access check, backup, redirect, reboot, verify",
            "Local SoundTouch validation: CLI 17000 can set bmxRegistryUrl, margeServerUrl, statsServerUrl and boseurls",
            "OverrideSdkPrivateCfg.xml is firmware-dependent and must not be mandatory",
        ],
    }
    write_masterlog(
        "server_info",
        recommended_host=recommended_host,
        suggested_scan_cidr=result["suggested_scan_cidr"],
        cloud_ok=cloud_open,
        debug_ok=debug_open,
        cloud_error=None if cloud_open else cloud_message,
        debug_error=None if debug_open else debug_message,
    )
    return result


@router.post("/setup/wizard/service-status")
async def record_browser_service_status(payload: dict) -> dict:
    service = str(payload.get("service") or "").strip().lower()
    if service not in {"cloud", "debug"}:
        raise HTTPException(status_code=400, detail="service must be cloud or debug")
    online = bool(payload.get("online"))
    reason = str(payload.get("reason") or "")[:300]
    write_masterlog(
        "service_status_check",
        service=service,
        online=online,
        error_reason=None if online else reason or "request failed",
    )
    return {"ok": True}


@router.post("/setup/wizard/preflight/{device_id}")
async def setup_wizard_preflight(device_id: str, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    write_masterlog("setup_start", endpoint="setup_wizard_preflight", device_id=device_id)
    device = api_core._device_or_404(db, device_id)
    host = _setup_target_host(payload, request)
    port = int(payload.get("port") or get_settings().cloud_port)
    targets = api_core._cloud_route_targets(host, port)
    checks: list[dict] = []

    def add(name: str, ok: bool, message: str, details: dict | None = None) -> None:
        checks.append({"name": name, "ok": ok, "message": message, "details": details or {}})

    add("basswiesn host", host not in {"127.0.0.1", "localhost"}, f"Target host: {host}", {"host": host, "cloud_port": port})
    cloud_open, cloud_message = api_core._tcp_port_open("127.0.0.1", port, timeout=0.5)
    add("local cloud service", cloud_open, cloud_message, {"url": f"http://127.0.0.1:{port}/bmx/registry/v1/services"})
    lan_cloud_open, lan_cloud_message = api_core._tcp_port_open(host, port, timeout=0.8)
    add("radio reachable cloud URL", lan_cloud_open, lan_cloud_message, {"url": f"http://{host}:{port}/bmx/registry/v1/services"})
    if device.ip_address:
        radio_http_open, radio_http_message = api_core._tcp_port_open(device.ip_address, 8090, timeout=float(payload.get("timeout") or 1.2))
        add("radio HTTP 8090", radio_http_open, radio_http_message, {"radio_ip": device.ip_address})
        cli_open, cli_message = api_core._tcp_port_open(device.ip_address, 17000, timeout=float(payload.get("timeout") or 1.2))
        add("radio CLI 17000", cli_open, cli_message, {"radio_ip": device.ip_address})
    else:
        add("radio IP", False, "Device has no IP address", {})

    current_route = await api_core._read_current_cloud_route(device) if payload.get("probe_current", True) else {"values": {}, "sources": {}}
    diff = api_core._cloud_route_diff(current_route.get("values", {}), targets)
    route_ready = all(not row["changed"] for row in diff)
    add("cloud route values", route_ready, "Already points to basswiesn" if route_ready else "Route differs and can be written by the wizard", {"diff_text": api_core._cloud_route_diff_text(diff)})
    backup_preview = await api_core._safe_http_setup_backups(device, db) if payload.get("capture_backup", False) else {"saved": [], "failed": [], "preview": True}
    if payload.get("capture_backup", False):
        db.commit()
    backup_status = api_core._setup_backup_status(device, db)
    cli_ok = next((item["ok"] for item in checks if item["name"] == "radio CLI 17000"), False)
    return {
        "device_id": device.device_id,
        "radio_ip": device.ip_address,
        "host": host,
        "targets": targets,
        "current_radio_settings": current_route,
        "diff": diff,
        "diff_text": api_core._cloud_route_diff_text(diff),
        "checks": checks,
        "backup": backup_preview,
        "backup_status": backup_status,
        "commands": api_core._setup_cli17000_commands(targets, reboot=bool(payload.get("reboot", True))),
        "ssh_fallback_plan": api_core._ssh_fallback_plan(host, port) if not cli_ok else None,
        "confirmation_required_for_write": "yes",
        "ready_for_apply": all(item["ok"] for item in checks if item["name"] in {"basswiesn host", "local cloud service", "radio reachable cloud URL", "radio HTTP 8090", "radio CLI 17000"}),
        "steps": api_core._setup_wizard_steps(),
    }


@router.post("/setup/wizard/apply/{device_id}")
async def setup_wizard_apply(device_id: str, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    host = _setup_target_host(payload, request)
    body = {**payload, "host": host, "port": int(payload.get("port") or get_settings().cloud_port), "reboot": payload.get("reboot", True)}
    if payload.get("dry_run", True):
        preflight = await setup_wizard_preflight(device_id, {**body, "capture_backup": False}, request, db)
        return {"dry_run": True, "mode": "guided_setup", "preflight": preflight, "apply_endpoint": f"/api/setup/wizard/apply/{device_id}", "note": "Type yes and disable dry_run to write."}
    _legacy_setup_retired()
    preflight = await setup_wizard_preflight(device_id, {**body, "capture_backup": True}, request, db)
    if not preflight.get("ready_for_apply") and not payload.get("force"):
        raise HTTPException(status_code=409, detail={"error": "wizard preflight is not ready for apply", "preflight": preflight, "override": "Set force=true only after manually confirming host, cloud reachability, HTTP backup and CLI 17000 access."})
    result = await apply_setup_cloud_route(device_id, {**body, "dry_run": False}, request, db)
    device = api_core._device_or_404(db, device_id)
    account_id = str(payload.get("account_id") or _local_account_id(device.device_id))
    result["account_pairing"] = await _pair_local_account(device, account_id, db)
    result["device_defaults"] = await _apply_setup_defaults(device, db)
    result["post_setup_verify"] = await verify_setup_cloud_route(device_id, {"host": host, "port": body["port"]}, request, db)
    result["preflight"] = preflight
    result["mode"] = "guided_setup"
    result["wizard_steps"] = api_core._setup_wizard_steps()
    return result


@router.post("/setup/account/{device_id}")
async def setup_local_account(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = api_core._device_or_404(db, device_id)
    account_id = str(payload.get("account_id") or _local_account_id(device.device_id))
    if not account_id.isdigit() or not (1 <= len(account_id) <= 10):
        raise HTTPException(status_code=400, detail="account_id must be numeric")
    if payload.get("dry_run", False):
        return {"dry_run": True, "device_id": device.device_id, "account_id": account_id, "method": "minimal /setMargeAccount", "confirmation_required": "YES"}
    _legacy_setup_retired()
    _require_setup_write_allowed(device, db)
    if not setup_confirmation_allowed(
        payload.get("confirmation"),
        "YES",
        endpoint=f"/api/setup/account/{device_id}",
        action="pair local BASSWIESN account",
    ):
        raise HTTPException(status_code=409, detail={"error": "pair confirmation required", "expected": "YES"})
    return await _pair_local_account(device, account_id, db)


@router.post("/setup/activation-playback/{device_id}")
async def retry_setup_activation_playback(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = api_core._device_or_404(db, device_id)
    if payload.get("dry_run", True):
        station = _activation_station(db)
        return {
            "dry_run": True,
            "device_id": device.device_id,
            "station_id": station.id,
            "duration_seconds": SETUP_ACTIVATION_SECONDS,
            "message": "Activation playback will use the normal station playback route.",
        }
    _legacy_setup_retired()
    _require_setup_write_allowed(device, db)
    try:
        return {"dry_run": False, **await _run_setup_activation_playback(device, db)}
    except Exception as exc:
        write_masterlog("setup_activation_playback_failed", device_id=device.device_id, radio_ip=device.ip_address, error=str(exc))
        raise HTTPException(status_code=502, detail={"error": "activation playback failed", "message": str(exc)}) from exc


@router.post("/setup/cloud-route/{device_id}")
async def setup_cloud_route(device_id: str, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    device = api_core._device_or_404(db, device_id)
    host = _setup_target_host(payload, request)
    port = int(payload.get("port") or get_settings().cloud_port)
    targets = api_core._cloud_route_targets(host, port)
    current_route = await api_core._read_current_cloud_route(device) if payload.get("probe_current", True) else {"values": {}, "sources": {}}
    diff = api_core._cloud_route_diff(current_route.get("values", {}), targets)
    write_masterlog("setup_route_preview", device_id=device.device_id, radio_ip=device.ip_address, target_host=host, changed=sum(1 for row in diff if row["changed"]))
    return {
        "dry_run": True,
        "device_id": device.device_id,
        "radio_ip": device.ip_address,
        "basswiesn_host": host,
        "cloud_port": port,
        "current_radio_settings": current_route,
        "targets": targets,
        "diff": diff,
        "diff_text": api_core._cloud_route_diff_text(diff),
        "cli17000_commands": api_core._setup_cli17000_commands(targets, reboot=bool(payload.get("reboot"))),
        "confirmation_required_for_write": "yes",
        "host_file_visibility": "The device /etc/hosts file is only readable after SSH/read-only access. This setup path reads runtime values via /info and CLI 17000 first.",
        "note": "Preview only. Apply will save HTTP backups, then set boseurls/marge/stats/BMX to basswiesn.",
    }


@router.post("/setup/cloud-route/{device_id}/apply")
async def apply_setup_cloud_route(device_id: str, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    device = api_core._device_or_404(db, device_id)
    host = _setup_target_host(payload, request)
    port = int(payload.get("port") or get_settings().cloud_port)
    targets = api_core._cloud_route_targets(host, port)
    reboot = bool(payload.get("reboot"))
    commands = api_core._setup_cli17000_commands(targets, reboot=reboot)
    write_commands = api_core._setup_cli17000_commands(targets, reboot=False)
    if payload.get("dry_run", False):
        current_route = await api_core._read_current_cloud_route(device) if payload.get("probe_current", True) else {"values": {}, "sources": {}}
        diff = api_core._cloud_route_diff(current_route.get("values", {}), targets)
        return {"dry_run": True, "device_id": device.device_id, "radio_ip": device.ip_address, "current_radio_settings": current_route, "targets": targets, "diff": diff, "diff_text": api_core._cloud_route_diff_text(diff), "cli17000_commands": commands, "confirmation_required_for_write": "yes"}
    _legacy_setup_retired()
    _require_setup_write_allowed(device, db)
    write_masterlog("setup_apply_start", device_id=device.device_id, radio_ip=device.ip_address, reboot=reboot)
    if not device.ip_address:
        raise HTTPException(status_code=400, detail="device has no IP address")
    before_route = await api_core._read_current_cloud_route(device)
    db.add(ConfigBackup(device_id=device.device_id, path="setup-route/before.json", content=json.dumps(before_route, ensure_ascii=False)))
    try:
        before_presets_xml = await api_core.SoundTouchClient(device.ip_address).get_xml("/presets")
    except Exception as exc:
        write_masterlog("setup_presets_backup_failed", device_id=device.device_id, radio_ip=device.ip_address, error=str(exc))
        raise HTTPException(status_code=502, detail={"error": "preset backup failed before setup route write", "message": str(exc), "radio_ip": device.ip_address}) from exc
    db.add(ConfigBackup(device_id=device.device_id, path="setup-route/before-presets.xml", content=before_presets_xml))
    preset_import_result = import_presets_from_radio_backup(db, device.device_id, before_presets_xml, request_host=request.headers.get("host", ""))
    if preset_import_result["source_count"] and not preset_import_result["imported_count"]:
        write_masterlog("setup_presets_import_failed", device_id=device.device_id, radio_ip=device.ip_address, result=preset_import_result)
        raise HTTPException(status_code=409, detail={"error": "preset import failed before setup route write", "result": preset_import_result})
    pre_setup_logs = await api_core._capture_radio_logs(device, db, reason="setup-before", include_cli=True)
    backup_result = await api_core._safe_http_setup_backups(device, db)
    # The rollback record must survive SSH detection, write or reboot failures.
    db.commit()
    write_masterlog("hosts_redirect_start", device_id=device.device_id, target_host=host)
    ssh_hosts = await api_core._read_ssh_hosts(device.ip_address)
    ssh_override = await api_core._read_ssh_setup_override(device.ip_address) if ssh_hosts.get("available") else {"present": False, "error": ssh_hosts.get("error")}
    ssh_write_result = {"used": False, "present": ssh_override.get("present", False)}
    ssh_hosts_result = {"used": False, "available": ssh_hosts.get("available", False), "message": ssh_hosts.get("message")}
    rewritten_hosts = ""
    if ssh_hosts.get("available"):
        original_hosts = ssh_hosts["content"]
        db.add(ConfigBackup(device_id=device.device_id, path="setup-route/etc-hosts", content=original_hosts))
        write_masterlog("hosts_redirect_backup", device_id=device.device_id, path="/mnt/nv/etc-hosts.basswiesn-backup")
        rewritten_hosts = api_core.rewrite_hosts(original_hosts, host)
    if ssh_override.get("content"):
        original_xml = ssh_override["content"]
        db.add(ConfigBackup(device_id=device.device_id, path="setup-route/OverrideSdkPrivateCfg.xml", content=original_xml))
        rewritten_xml = api_core.rewrite_sdk_config(original_xml, targets["margeServerUrl"])
    else:
        rewritten_xml = ""
    if rewritten_xml or rewritten_hosts:
        db.commit()
    try:
        output = await api_core._send_cli17000(device.ip_address, write_commands)
        if rewritten_xml:
            ssh_write_result = await api_core._write_ssh_setup_override(device.ip_address, rewritten_xml)
            ssh_write_result["used"] = True
        if rewritten_hosts:
            ssh_hosts_result = await api_core._write_ssh_hosts(device.ip_address, rewritten_hosts, host)
            ssh_hosts_result["used"] = True
            write_masterlog("hosts_redirect_write", device_id=device.device_id, target_host=host)
            write_masterlog("hosts_redirect_verify", device_id=device.device_id, **ssh_hosts_result["verification"])
            write_masterlog("hosts_redirect_complete", device_id=device.device_id, target_host=host)
        if reboot:
            output += await api_core._send_cli17000(device.ip_address, ["sys reboot"])
    except (OSError, ET.ParseError) as exc:
        db.rollback()
        if rewritten_hosts:
            write_masterlog("hosts_redirect_failed", device_id=device.device_id, target_host=host, error=str(exc))
        write_masterlog("setup_failed", device_id=device.device_id, stage="apply", error=str(exc))
        raise HTTPException(status_code=502, detail={"error": "setup route write failed", "radio_ip": device.ip_address, "message": str(exc), "backup_result": backup_result, "pre_setup_logs": pre_setup_logs}) from exc
    post_reboot_check = api_core._wait_for_radio_http(device, initial_delay=60) if reboot else asyncio.sleep(0, result={"ok": True, "initial_delay_seconds": 0, "summary": "no reboot requested"})
    post_reboot_result = await post_reboot_check
    if reboot and not post_reboot_result.get("ok"):
        write_masterlog("setup_post_reboot_retry", device_id=device.device_id, radio_ip=device.ip_address, first_error=post_reboot_result.get("last_error", "not reachable"))
        post_reboot_result = await api_core._wait_for_radio_http(device, initial_delay=15, attempts=12, interval=5)
    post_setup_logs = await api_core._capture_radio_logs(device, db, reason="setup-after", include_cli=True) if post_reboot_result.get("ok") else {"reason": "setup-after", "captured": [], "failed": [{"source": "post-reboot", "error": post_reboot_result.get("last_error", "radio not reachable")}]}
    post_setup_verify = await verify_setup_cloud_route(device_id, {"host": host, "port": port}, request, db) if post_reboot_result.get("ok") else {"status": "needs-attention", "checks": [{"name": "post reboot radio wait", "ok": False, "result": post_reboot_result}]}
    device.last_seen = api_core.utc_now()
    event = TelemetryEvent(
        device_id=device.device_id,
        event_type="setup_cloud_route_apply",
        endpoint="cli17000",
        payload=json.dumps({"before_route": before_route, "targets": targets, "commands": commands, "output": output, "backup_result": backup_result}, ensure_ascii=False),
        parsed_summary=api_core._summarize_payload(output or "setup command sent"),
    )
    db.add(event)
    db.commit()
    result = {
        "dry_run": False,
        "device_id": device.device_id,
        "radio_ip": device.ip_address,
        "before_route": before_route,
        "targets": targets,
        "backup_result": backup_result,
        "preset_import_result": preset_import_result,
        "ssh_override_write": ssh_write_result,
        "ssh_hosts_write": ssh_hosts_result,
        "pre_setup_logs": pre_setup_logs,
        "post_reboot_wait": post_reboot_result,
        "post_setup_logs": post_setup_logs,
        "post_setup_verify": post_setup_verify,
        "cli17000_commands": commands,
        "cli17000_output": output,
        "reboot_requested": reboot,
        "next_steps": ["If reboot was requested, basswiesn already waited 60 seconds and ran post-reboot verify.", "Open Debug/Requests and play a station to verify basswiesn receives cloud/BMX calls.", "If /etc/hosts must be inspected, use SSH read-only log capture."],
    }
    if post_setup_verify.get("status") != "ready":
        write_masterlog("setup_failed", device_id=device.device_id, stage="verify", error="setup route verification failed")
        raise HTTPException(status_code=502, detail={"error": "setup route verification failed", "result": result})
    write_masterlog("setup_apply_complete", device_id=device.device_id, radio_ip=device.ip_address, verified=True)
    return result


@router.post("/setup/cloud-route/{device_id}/verify")
async def verify_setup_cloud_route(device_id: str, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    write_masterlog("setup_verify_start", device_id=device_id)
    device = api_core._device_or_404(db, device_id)
    host = _setup_target_host(payload, request)
    port = int(payload.get("port") or get_settings().cloud_port)
    targets = api_core._cloud_route_targets(host, port)
    current_route = await api_core._read_current_cloud_route(device)
    diff = api_core._cloud_route_diff(current_route.get("values", {}), targets)
    client = api_core.SoundTouchClient(device.ip_address)
    checks: list[dict] = []
    for endpoint in ["/info", "/sources", "/presets", "/now_playing", "/getZone"]:
        try:
            xml = await client.get_xml(endpoint)
            checks.append({"name": f"radio {endpoint}", "ok": True, "summary": api_core._summarize_payload(xml)})
        except Exception as exc:
            checks.append({"name": f"radio {endpoint}", "ok": False, "error": str(exc)})
    try:
        import httpx

        async with httpx.AsyncClient(timeout=4.0) as http:
            response = await http.get(f"http://127.0.0.1:{port}/bmx/registry/v1/services")
        checks.append({"name": "basswiesn BMX registry", "ok": response.status_code == 200, "status_code": response.status_code})
    except Exception as exc:
        checks.append({"name": "basswiesn BMX registry", "ok": False, "error": str(exc)})
    route_ok = all((not row["changed"]) for row in diff)
    checks.insert(0, {"name": "cloud route matches basswiesn target", "ok": route_ok, "diff_text": api_core._cloud_route_diff_text(diff)})
    ssh_hosts = await api_core._read_ssh_hosts(device.ip_address)
    if ssh_hosts.get("available"):
        hosts_verify = api_core.verify_hosts_redirect(ssh_hosts.get("content", ""), host)
        checks.append({"name": "SSH /etc/hosts full redirect", **hosts_verify})
    else:
        checks.append({"name": "SSH /etc/hosts full redirect", "ok": True, "mode": "cli-only", "message": ssh_hosts.get("message"), "error": ssh_hosts.get("error")})
    overall = "ready" if all(item.get("ok") for item in checks) else "needs-attention"
    write_masterlog("setup_verify_complete", device_id=device.device_id, status=overall)
    return {"device_id": device.device_id, "radio_ip": device.ip_address, "status": overall, "checks": checks, "current_radio_settings": current_route, "targets": targets, "diff": diff, "diff_text": api_core._cloud_route_diff_text(diff)}


@router.post("/setup/cloud-route/{device_id}/rollback")
async def rollback_setup_cloud_route(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    _legacy_setup_retired()
    device = api_core._device_or_404(db, device_id)
    backup = db.query(ConfigBackup).filter(ConfigBackup.device_id == device.device_id, ConfigBackup.path == "setup-route/before.json").order_by(ConfigBackup.created_at.desc()).first()
    if backup is None:
        raise HTTPException(status_code=404, detail="no setup route backup found")
    before = json.loads(backup.content or "{}")
    values = before.get("values") or {}
    missing = [tag for tag in api_core.CLOUD_ROUTE_TAGS if not values.get(tag)]
    if missing:
        raise HTTPException(status_code=409, detail={"error": "rollback backup is incomplete", "missing": missing, "backup": before})
    reboot = bool(payload.get("reboot", True))
    commands = api_core._setup_cli17000_commands_from_values(values, reboot=reboot)
    if payload.get("dry_run", True):
        return {"dry_run": True, "device_id": device.device_id, "radio_ip": device.ip_address, "rollback_values": values, "cli17000_commands": commands, "confirmation_required_for_write": "YES"}
    _require_setup_write_allowed(device, db)
    write_masterlog("setup_rollback", device_id=device.device_id, radio_ip=device.ip_address)
    expected = "YES"
    if not setup_confirmation_allowed(
        payload.get("confirmation"),
        expected,
        endpoint=f"/api/setup/cloud-route/{device_id}/rollback",
        action="rollback BASSWIESN cloud route",
    ):
        raise HTTPException(status_code=409, detail={"error": "rollback confirmation required", "expected": expected})
    pre_logs = await api_core._capture_radio_logs(device, db, reason="rollback-before", include_cli=True)
    output = await api_core._send_cli17000(device.ip_address, commands)
    post_logs = await api_core._capture_radio_logs(device, db, reason="rollback-after", include_cli=True)
    db.add(TelemetryEvent(device_id=device.device_id, event_type="setup_cloud_route_rollback", endpoint="cli17000", payload=json.dumps({"commands": commands, "output": output}, ensure_ascii=False), parsed_summary=api_core._summarize_payload(output)))
    db.commit()
    return {"dry_run": False, "device_id": device.device_id, "rollback_values": values, "cli17000_commands": commands, "cli17000_output": output, "pre_logs": pre_logs, "post_logs": post_logs, "reboot_requested": reboot}


async def _factory_ssh(device, command: str, timeout: int = 25) -> dict:
    result = await api_core._run_ssh_readonly_command(device.ip_address, "root", command, timeout)
    if result["returncode"] != 0:
        raise OSError(result["stderr"].strip() or "Factory-Fix SSH command failed")
    return result


async def _ensure_persistence_file(device, filename: str, xml_text: str, marker: str) -> dict:
    encoded = base64.b64encode(xml_text.encode()).decode()
    target = f"/mnt/nv/BoseApp-Persistence/1/{filename}"
    tmp = f"/tmp/{filename}.basswiesn"
    command = (
        "mkdir -p /mnt/nv/BoseApp-Persistence/1; "
        f"if [ -s {target} ]; then echo SKIPPED; "
        f"else printf %s {encoded} | base64 -d > {tmp} && test -s {tmp} && mv {tmp} {target} && sync && echo CREATED; fi; "
        f"test -s {target}; grep -q {marker} {target}"
    )
    result = await _factory_ssh(device, command, 25)
    stdout = result.get("stdout", "")
    return {"created": "CREATED" in stdout, "skipped": "SKIPPED" in stdout, "path": target, "stdout": stdout.strip()}


async def _ensure_sources_persistence_file(device) -> dict:
    encoded = base64.b64encode(FACTORY_SOURCES_XML.encode()).decode()
    target = "/mnt/nv/BoseApp-Persistence/1/Sources.xml"
    tmp = "/tmp/Sources.xml.basswiesn"
    required = " ".join(RECOMMENDED_SOURCE_TYPES)
    command = (
        "mkdir -p /mnt/nv/BoseApp-Persistence/1 /mnt/nv/factory-fix-backups; "
        f"target={target}; tmp={tmp}; missing=0; "
        "if [ ! -s \"$target\" ]; then missing=1; else "
        f"for source in {required}; do grep -q \"sourceKey type=\\\"$source\\\"\" \"$target\" || missing=1; done; "
        "fi; "
        "if [ \"$missing\" = 0 ]; then echo SKIPPED; "
        "else "
        "if [ -s \"$target\" ]; then cp \"$target\" /mnt/nv/factory-fix-backups/Sources.xml.basswiesn-pre-augment-$(date +%Y%m%d-%H%M%S) 2>/dev/null || true; fi; "
        f"printf %s {encoded} | base64 -d > \"$tmp\" && test -s \"$tmp\" && mv \"$tmp\" \"$target\" && sync && echo AUGMENTED; "
        "fi; "
        "test -s \"$target\"; "
        f"for source in {required}; do grep -q \"sourceKey type=\\\"$source\\\"\" \"$target\"; done"
    )
    result = await _factory_ssh(device, command, 35)
    stdout = result.get("stdout", "")
    return {"created": "AUGMENTED" in stdout, "skipped": "SKIPPED" in stdout, "path": target, "stdout": stdout.strip()}


@router.post("/devices/{device_id}/factory-fix")
async def factory_fix(device_id: str, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    """Repair factory-reset persistence without performing another reset."""
    _legacy_setup_retired()
    device = api_core._device_or_404(db, device_id)
    if not payload.get("dry_run", False):
        enforce_ip_write_guard(db, device)
    write_masterlog("factory_fix_start", device_id=device.device_id, radio_ip=device.ip_address)
    try:
        info_xml = await api_core.SoundTouchClient(device.ip_address).get_xml("/info")
        if not info_xml.strip():
            raise OSError("/info ist leer")
        info = ET.fromstring(info_xml)
        device_name = (info.findtext("name") or "SoundTouch").strip() or "SoundTouch"
        account_uuid = (info.findtext("margeAccountUUID") or "").strip()
        write_masterlog("factory_fix_info_read", device_id=device.device_id, device_name=device_name, account_present=bool(account_uuid))

        ssh = await api_core._read_ssh_hosts(device.ip_address)
        if not ssh.get("available"):
            write_masterlog("factory_fix_requires_ssh", device_id=device.device_id, error=ssh.get("error"))
            raise HTTPException(status_code=409, detail="Factory-Fix benötigt SSH. Bitte remote_services USB-Stick aktivieren.")
        variant = await _factory_ssh(device, "cat /proc/variant 2>/dev/null || true; cat /proc/variant_mode 2>/dev/null || true")

        account_was_missing = not account_uuid
        if account_was_missing:
            account_uuid = str(secrets.randbelow(9_000_000) + 1_000_000)

        system_xml = f'''<?xml version="1.0" encoding="UTF-8" ?>

<SystemConfiguration>
  <Password />
  <DeviceName>{device_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")}</DeviceName>
  <AccountAssociatedEMail />
  <AccountUUID>{account_uuid}</AccountUUID>
  <Locale />
  <acctMode>global</acctMode>
  <isMultiDeviceAccount>true</isMultiDeviceAccount>
  <margeAuthServerToken />
  <powerSavingSettings powersaving_en="true" />
</SystemConfiguration>
'''
        if payload.get("dry_run", False):
            return {"dry_run": True, "device_id": device.device_id, "radio_ip": device.ip_address, "device_name": device_name, "account_uuid": account_uuid, "account_id_would_be_created": account_was_missing, "factory_state": variant["stdout"].strip(), "requires_ssh": True}

        if account_was_missing:
            await api_core._send_cli17000(device.ip_address, [f"envswitch accountid set {account_uuid}", "exit"])
            write_masterlog("factory_fix_account_id_set", device_id=device.device_id, account_id=account_uuid)

        await _factory_ssh(device, "mount -o remount,rw / || true")
        write_masterlog("factory_fix_rootfs_rw", device_id=device.device_id)

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup = await api_core._run_ssh_readonly_command(device.ip_address, "root", "mkdir -p /mnt/nv/factory-fix-backups; tar czf /mnt/nv/factory-fix-backups/factory-sources-prep-" + stamp + ".tgz /etc/local_services /etc/remote_services /mnt/nv/local_services /mnt/nv/remote_services /mnt/nv/BoseApp-Persistence/1/SystemConfigurationDB.xml /mnt/nv/BoseApp-Persistence/1/Sources.xml 2>/dev/null || true", 30)
        write_masterlog("factory_fix_backup", device_id=device.device_id, ok=backup["returncode"] == 0)

        await _factory_ssh(device, "touch /etc/local_services /etc/remote_services /mnt/nv/local_services /mnt/nv/remote_services /tmp/local_services /tmp/remote_services || true")
        write_masterlog("factory_fix_services_enabled", device_id=device.device_id)

        system_init = await _ensure_persistence_file(device, "SystemConfigurationDB.xml", system_xml, "AccountUUID")
        write_masterlog("persistence_system_created" if system_init["created"] else "persistence_system_skipped", device_id=device.device_id, path=system_init["path"])
        sources_init = await _ensure_sources_persistence_file(device)
        write_masterlog("persistence_sources_created" if sources_init["created"] else "persistence_sources_skipped", device_id=device.device_id, path=sources_init["path"])

        redirect_result = {"used": False, "message": "redirect optional"}
        if payload.get("redirect"):
            target_host = _setup_target_host(payload, request)
            rewritten = api_core.rewrite_hosts(ssh["content"], target_host)
            redirect_result = await api_core._write_ssh_hosts(device.ip_address, rewritten, target_host)
            redirect_result["used"] = True

        await _factory_ssh(device, "sync; mount -o remount,ro / || true; reboot", 15)
        write_masterlog("factory_fix_reboot", device_id=device.device_id)
        reachable = await api_core._wait_for_radio_http(device, initial_delay=60, attempts=13, interval=5)
        if not reachable.get("ok"):
            raise OSError(reachable.get("last_error") or "Radio nach Reboot nicht erreichbar")
        write_masterlog("factory_fix_settle_wait", device_id=device.device_id, seconds=FACTORY_REBOOT_SETTLE_SECONDS)
        await asyncio.sleep(FACTORY_REBOOT_SETTLE_SECONDS)

        client = api_core.SoundTouchClient(device.ip_address)
        endpoints = ["/info", "/sources", "/capabilities", "/presets", "/serviceAvailability", "/soundTouchConfigurationStatus"]
        checks = {}
        for endpoint in endpoints:
            try:
                checks[endpoint] = {"ok": True, "xml": await client.get_xml(endpoint)}
            except Exception as exc:
                checks[endpoint] = {"ok": False, "error": str(exc)}
        sources_text = checks.get("/sources", {}).get("xml", "")
        source_types = re.findall(r'(?:sourceKey type|source)="([A-Z0-9_]+)"', sources_text)
        service_check = await _factory_ssh(device, "test -e /etc/local_services -o -e /mnt/nv/local_services -o -e /tmp/local_services; test -e /etc/remote_services -o -e /mnt/nv/remote_services -o -e /tmp/remote_services; test -s /mnt/nv/BoseApp-Persistence/1/SystemConfigurationDB.xml; test -s /mnt/nv/BoseApp-Persistence/1/Sources.xml; cat /mnt/nv/BoseApp-Persistence/1/Sources.xml")
        persisted_source_types = re.findall(r'sourceKey type="([A-Z0-9_]+)"', service_check.get("stdout", ""))
        effective_source_types = set(source_types) | set(persisted_source_types)
        missing_recommended = [source for source in RECOMMENDED_SOURCE_TYPES if source not in set(persisted_source_types)]
        if missing_recommended:
            write_masterlog("persistence_sources_missing_recommended", device_id=device.device_id, missing=missing_recommended, persistence_sources_complete=False)
        # Some firmware hides provider-backed sources from /sources until the
        # first successful cloud registry bootstrap. Persistence is still the
        # ground truth written by the known-good factory repair script.
        source_ok = len(set(source_types)) > 1 and "LOCAL_INTERNET_RADIO" in effective_source_types and ("TUNEIN" in effective_source_types or "RADIO_BROWSER" in effective_source_types)
        verified = all(item["ok"] for item in checks.values()) and source_ok and service_check["returncode"] == 0
        write_masterlog("factory_fix_verify", device_id=device.device_id, verified=verified, source_types=source_types, persisted_source_types=persisted_source_types, persistence_sources_complete=not missing_recommended)
        if not verified:
            raise OSError("Factory-Fix-Verifikation fehlgeschlagen")
        write_masterlog("factory_fix_complete", device_id=device.device_id, radio_ip=device.ip_address)
        return {"dry_run": False, "device_id": device.device_id, "radio_ip": device.ip_address, "factory_state": variant["stdout"].strip(), "verified": True, "source_types": source_types, "persisted_source_types": persisted_source_types, "missing_recommended_sources": missing_recommended, "persistence_sources_complete": not missing_recommended, "redirect": redirect_result, "checks": {key: {"ok": value["ok"], "error": value.get("error")} for key, value in checks.items()}}
    except HTTPException:
        raise
    except Exception as exc:
        write_masterlog("factory_fix_failed", device_id=device.device_id, radio_ip=device.ip_address, error=str(exc))
        raise HTTPException(status_code=502, detail={"error": "factory_fix_failed", "message": str(exc)}) from exc


@router.post("/devices/{device_id}/setup/live-test")
async def setup_live_test(device_id: str, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    device = api_core._device_or_404(db, device_id)
    host = api_core._validated_setup_host(payload.get("host") or request.url.hostname)
    port = int(payload.get("port") or get_settings().cloud_port)
    dry_run = bool(payload.get("dry_run", True))
    result: dict = {"dry_run": dry_run, "device_id": device.device_id, "radio_ip": device.ip_address, "steps": []}
    if dry_run:
        station_id = payload.get("station_id")
        result["steps"] = [
            {"name": "capture before", "ok": True, "dry_run": True, "plan": "Would capture HTTP/XML and CLI17000 readouts."},
            {"name": "verify setup", "ok": True, "dry_run": True, "plan": "Would compare cloud route and read /info, /sources, /presets, /now_playing, /getZone."},
            {"name": "preset status", "ok": True, "dry_run": True, "plan": "Would compare local preset slots with radio /presets."},
        ]
        if station_id:
            station = db.query(Station).filter(Station.id == int(station_id)).one_or_none()
            if station is None:
                result["steps"].append({"name": "play test station", "ok": False, "error": "station not found"})
            else:
                descriptor = StationDescriptor(station.name, station.stream_url, station.image_url, station.provider_station_id)
                try:
                    location = station_location(descriptor, db=db, request_host=request.headers.get("host", ""))
                except OrionLocationError as exc:
                    raise HTTPException(status_code=409, detail={"error": str(exc), "hint": "BASSWIESN Host IP setzen"}) from exc
                result["steps"].append({"name": "play test station", "ok": True, "dry_run": True, "path": "/select", "xml": content_item_xml(station, location)})
        result["steps"].append({"name": "capture after", "ok": True, "dry_run": True, "plan": "Would capture post-test logs."})
        result["status"] = "ready" if all(step.get("ok") for step in result["steps"]) else "needs-attention"
        return result
    _legacy_setup_retired()
    log_before = await api_core._capture_radio_logs(device, db, reason="live-test-before", include_cli=True)
    result["steps"].append({"name": "capture before", "ok": True, "result": log_before})
    verify = await verify_setup_cloud_route(device_id, {"host": host, "port": port}, request, db)
    result["steps"].append({"name": "verify setup", "ok": verify.get("status") == "ready", "result": verify})
    presets = await preset_status(device_id, db)
    result["steps"].append({"name": "preset status", "ok": not presets.get("radio_error"), "result": presets})
    station_id = payload.get("station_id")
    if station_id:
        station = db.query(Station).filter(Station.id == int(station_id)).one_or_none()
        if station is None:
            result["steps"].append({"name": "play test station", "ok": False, "error": "station not found"})
        else:
            try:
                play = await play_station_on_device(device_id, int(station_id), {"dry_run": False}, db)
                result["steps"].append({"name": "play test station", "ok": True, "result": play})
            except Exception as exc:
                result["steps"].append({"name": "play test station", "ok": False, "error": str(exc)})
    log_after = await api_core._capture_radio_logs(device, db, reason="live-test-after", include_cli=True)
    result["steps"].append({"name": "capture after", "ok": True, "result": log_after})
    result["status"] = "ready" if all(step.get("ok") for step in result["steps"]) else "needs-attention"
    db.add(TelemetryEvent(device_id=device.device_id, event_type="setup_live_test", endpoint="basswiesn", payload=json.dumps(result, ensure_ascii=False), parsed_summary=result["status"]))
    db.commit()
    return result


@router.get("/setup/plans/{device_id}")
async def setup_plan(device_id: str, db: Session = Depends(get_db)) -> dict:
    device = api_core._device_or_404(db, device_id)
    plan = db.query(SetupPlan).filter(SetupPlan.device_id == device_id).order_by(SetupPlan.updated_at.desc()).first()
    steps = json.loads(plan.steps_json) if plan else api_core._guided_setup_steps(device)
    return {"device": api_core._device_summary(device), "plan_id": plan.id if plan else None, "steps": steps}


@router.post("/setup/plans/{device_id}")
async def save_setup_plan(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    # Persisting a user-visible plan is a compatibility operation, not a
    # hardware setup runtime. Execution still belongs exclusively to
    # /api/setup/rebuild.
    device = api_core._device_or_404(db, device_id)
    plan = db.query(SetupPlan).filter(SetupPlan.device_id == device_id).order_by(SetupPlan.updated_at.desc()).first()
    if plan is None:
        plan = SetupPlan(device_id=device_id, name=payload.get("name") or f"Setup {device.name or device.device_id}")
        db.add(plan)
    plan.steps_json = json.dumps(payload.get("steps") or api_core._guided_setup_steps(device))
    plan.status = payload.get("status", plan.status)
    plan.updated_at = api_core.utc_now()
    db.commit()
    db.refresh(plan)
    return {"id": plan.id, "device_id": plan.device_id, "status": plan.status}
