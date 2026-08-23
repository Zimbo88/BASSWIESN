"""Public API adapter for the independent setup rebuild engine."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from types import SimpleNamespace
from typing import Any

from defusedxml import ElementTree as SafeET
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from basswiesn.app import db as app_db
from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.db import get_db
from basswiesn.app.models import Device, SetupRebuildJob, utc_now
from basswiesn.app.config import get_settings
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.services.setup_rebuild.coordinator import get_coordinator
from basswiesn.app.services.setup_rebuild.candidates import (
    selected_setup_devices,
    setup_candidates,
)
from basswiesn.app.services.setup_rebuild.profiles import all_profiles
from basswiesn.app.services.setup_rebuild.profiles.activation import public_operation
from basswiesn.app.services.setup_rebuild.server_target import (
    resolve_server_target,
    server_target_candidates,
)
from basswiesn.app.services.setup_rebuild.states import SshStatus, state_spec
from basswiesn.app.services.setup_rebuild.ssh_runner import SshConfig
from basswiesn.app.services.setup_rebuild.repository import SetupRepository
from basswiesn.app.services.protected_devices import is_device_access_protected
from basswiesn.app.services.ssdp_discovery import manual_discovery_test

router = APIRouter(prefix="/api/setup/rebuild", tags=["setup-rebuild"])


def _explicit_identity_client(ip_address: str, device_id: str) -> SoundTouchClient:
    """Create the guarded client used only after the visible discovery action."""

    return SoundTouchClient(
        ip_address,
        device_id=device_id,
        request_purpose="explicit setup identity discovery",
        trigger="setup_webui_discover",
        get_timeout=5.0,
    )


async def _verify_explicit_discovery_device(
    db: Session,
    discovered: dict[str, Any],
) -> dict[str, Any]:
    """Verify one SSDP result through /info without touching stored bystanders."""

    device_id = str(discovered.get("device_id") or "").strip().upper()
    ip_address = str(discovered.get("ip_address") or "").strip()
    if not device_id:
        raise ValueError("Das Radio hat über SSDP keine überprüfbare Geräte-ID gemeldet.")
    if not ip_address:
        raise ValueError("Das Radio hat über SSDP keine verwendbare LAN-Adresse gemeldet.")
    if is_device_access_protected(ip_address, device_id):
        raise PermissionError("Das gefundene Radio ist vollständig geschützt.")

    row = db.query(Device).filter(Device.device_id == device_id).one_or_none()
    if row is None:
        raise ValueError("Das SSDP-Ergebnis wurde nicht sicher in der Gerätedatenbank verankert.")
    if str(row.ip_address or "").strip() != ip_address:
        raise ValueError("Die SSDP-Adresse stimmt nicht mit der gespeicherten Gerätezuordnung überein.")

    xml_text = await _explicit_identity_client(ip_address, device_id).get_xml("/info")
    try:
        root = SafeET.fromstring(xml_text)
    except Exception as exc:
        raise ValueError("Das Radio hat keine gültigen Geräteinformationen geliefert.") from exc
    observed_id = str(root.attrib.get("deviceID") or "").strip().upper()
    if observed_id != device_id:
        row.identity_verified = False
        raise ValueError("Die Geräte-ID aus /info stimmt nicht mit dem SSDP-Ergebnis überein.")

    observed_at = datetime.now(UTC)
    row.name = str(root.findtext("name", default="") or row.name or device_id).strip()
    row.model = str(root.findtext("type", default="") or row.model or "").strip()
    row.firmware = str(root.findtext(".//softwareVersion", default="") or row.firmware or "").strip()
    row.info_xml = xml_text
    row.ip_address = ip_address
    row.identity_verified = True
    row.reachable = True
    row.last_seen = observed_at
    row.discovery_method = "setup_ssdp_info"
    row.discovery_confidence = 100
    row.discovery_last_seen = observed_at
    return {
        "device_id": device_id,
        "ip_address": ip_address,
        "name": row.name,
        "model": row.model,
        "firmware": row.firmware,
        "identity_verified": True,
        "observed_at": observed_at.isoformat(),
    }


def _ensure_test_mode_simulation_device(db: Session) -> None:
    """Seed a no-network browser target only in explicit test mode."""

    if not get_settings().test_mode:
        return
    device_id = "BASSWIESN-SIM-160"
    row = db.query(Device).filter(Device.device_id == device_id).one_or_none()
    if row is None:
        row = Device(device_id=device_id)
        db.add(row)
    row.name = "Simuliertes Setup-Radio"
    row.ip_address = "192.0.2.160"
    row.model = "SoundTouch 20"
    row.firmware = "27.0.6.46330.5043500 epdbuild.simulation"
    row.info_xml = (
        '<info deviceID="BASSWIESN-SIM-160"><name>Simuliertes Setup-Radio</name>'
        '<type>SoundTouch 20</type><components><component>'
        '<softwareVersion>27.0.6.46330.5043500 epdbuild.simulation</softwareVersion>'
        '</component></components><moduleType>sm2</moduleType><variant>spotty</variant></info>'
    )
    row.identity_verified = True
    row.reachable = True
    db.commit()


async def _execute_job_background(job_id: str, *, dry_run: bool) -> None:
    try:
        await get_coordinator().execute(job_id, dry_run=dry_run)
    except Exception as exc:
        db = app_db.SessionLocal()
        try:
            job = SetupRepository().job(db, job_id)
            if job is not None:
                job.status = "failed"
                job.error = str(exc)[:1000]
                job.ended_at = utc_now()
                db.commit()
        finally:
            db.close()
        write_masterlog("setup_rebuild_background_failed", job_id=job_id, error=str(exc)[:500])


def _device_ids(payload: dict[str, Any]) -> list[str]:
    values = payload.get("device_ids") or payload.get("devices") or []
    if isinstance(values, str):
        values = [item for item in values.replace(";", ",").split(",") if item.strip()]
    result: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("device_id")
        normalized = str(value or "").strip().upper()
        if normalized:
            result.append(normalized)
    return result


def _safe_options(payload: dict[str, Any]) -> dict[str, Any]:
    # The normal UI deliberately exposes no transport credentials or internal
    # operation switches.  SSH is reserved for the explicit expert endpoint.
    return {
        "ssh_required": False,
        "pair_account": bool(payload.get("pair_account", True)),
        "playback_test": bool(payload.get("playback_test", True)),
    }


@router.get("/devices")
def available_devices(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """List local/discovered radios without probing any of them."""

    _ensure_test_mode_simulation_device(db)
    return [item.public_dict() for item in setup_candidates(db)]


@router.post("/discover")
async def discover_connected_radios(
    payload: dict[str, Any] | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Find radios already connected by the user, after a visible UI action.

    This endpoint deliberately performs no Wi-Fi configuration and no subnet
    scan. It sends SSDP multicast only after the user presses the discovery
    button. Only identities returned by that invocation are eligible for the
    subsequent guarded ``/info`` readback; stale database rows are never
    probed as a side effect.
    """

    try:
        requested_timeout = int((payload or {}).get("timeout_seconds") or 3)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Die Suchdauer muss eine ganze Zahl in Sekunden sein.") from exc
    timeout_seconds = max(1, min(requested_timeout, 10))
    discovery = await manual_discovery_test(db, timeout_seconds=timeout_seconds)
    # Discovery deliberately leaves commit ownership to this route. Flush its
    # newly upserted rows so the same non-autoflush SQLAlchemy session can
    # anchor the guarded /info verification immediately.
    db.flush()
    verified: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    blocked = 0

    for item in discovery.get("devices", []):
        device_id = str(item.get("device_id") or "").strip().upper()
        try:
            verified.append(await _verify_explicit_discovery_device(db, item))
        except PermissionError as exc:
            blocked += 1
            failures.append({"device_id": "", "reason": str(exc)})
        except Exception as exc:
            row = db.query(Device).filter(Device.device_id == device_id).one_or_none() if device_id else None
            if row is not None:
                # Descriptor identity alone is not enough for a critical
                # setup write. A failed current /info readback must leave the
                # candidate fail-closed even when historical profile data is
                # still present in the database.
                row.identity_verified = False
                row.reachable = False
                row.discovery_method = "setup_ssdp_info_failed"
                row.last_failed_at = datetime.now(UTC)
                row.failure_count = int(row.failure_count or 0) + 1
                row.offline_reason = "explicit setup identity readback failed"
            failures.append({
                "device_id": device_id,
                "reason": str(exc)[:500] or "Identitätsprüfung fehlgeschlagen.",
            })

    db.commit()
    candidates_by_id = {item.device_id: item.public_dict() for item in setup_candidates(db)}
    verified_candidates = [
        candidates_by_id[item["device_id"]]
        for item in verified
        if item["device_id"] in candidates_by_id
    ]
    result = {
        "triggered_by_user": True,
        "network_configuration_changed": False,
        "method": "SSDP multicast followed by guarded /info readback",
        "found": len(discovery.get("devices", [])),
        "verified": len(verified),
        "failed": len(failures),
        "blocked": blocked,
        "devices": verified_candidates,
        "failures": failures,
        "descriptor_failures": len(discovery.get("errors", [])),
    }
    write_masterlog(
        "setup_explicit_discovery_completed",
        found=result["found"],
        verified=result["verified"],
        failed=result["failed"],
        blocked=result["blocked"],
        timeout_seconds=timeout_seconds,
        network_configuration_changed=False,
    )
    return result


@router.get("/server-targets")
def available_server_targets() -> dict[str, Any]:
    candidates = [item.to_public_dict() for item in server_target_candidates()]
    return {
        "candidates": candidates,
        "recommended_host": candidates[0]["host"] if candidates else "",
        "blocked_kinds": ["loopback", "unspecified", "link-local", "container bridge"],
    }


@router.get("/profiles")
def profiles() -> dict[str, Any]:
    return {
        "ssh": [profile.public_dict() for profile in all_profiles()],
        "operations": [
            public_operation("common.read_ssh_state"),
            public_operation("common.reboot"),
            public_operation("common.rollback_ssh"),
        ],
        "credentials": SshConfig.from_settings().public_dict(),
    }


@router.get("/state-machine")
def machine() -> list[dict[str, object]]:
    return state_spec()


@router.post("/preview")
def preview(payload: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        target = resolve_server_target(payload)
        _ensure_test_mode_simulation_device(db)
        selected = selected_setup_devices(db, _device_ids(payload), require_eligible=False)
        options = _safe_options(payload)
        options["simulation"] = any(candidate.simulated for _row, candidate in selected)
        result = get_coordinator().preview(
            device_ids=[row.device_id for row, _candidate in selected],
            target=target,
            options=options,
        )
        result["devices"] = [candidate.public_dict() for _row, candidate in selected]
        audio_blocked = bool(options["playback_test"] and any(candidate.audio_safety_locked for _row, candidate in selected))
        result["audio_safety_blocked"] = audio_blocked
        result["ready_for_start"] = all(candidate.eligible for _row, candidate in selected) and not audio_blocked
        return result
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/start")
async def start(payload: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        target = resolve_server_target(payload)
        _ensure_test_mode_simulation_device(db)
        selected = selected_setup_devices(db, _device_ids(payload), require_eligible=True)
        simulated = any(candidate.simulated for _row, candidate in selected)
        if simulated and not get_settings().test_mode:
            raise ValueError("Der Simulationspfad ist außerhalb des Testmodus gesperrt.")
        dry_run = True if simulated else bool(payload.get("dry_run", False))
        options = _safe_options(payload)
        options["simulation"] = simulated
        if options["playback_test"] and any(candidate.audio_safety_locked for _row, candidate in selected):
            raise ValueError(
                "Die Wiedergabeprüfung ist für dieses Radio gesperrt. Bitte zuerst die sichtbare Audio-Sicherheitsprüfung ausführen oder den Audiotest abwählen."
            )
        result = get_coordinator().start(
            db,
            device_ids=[row.device_id for row, _candidate in selected],
            target=target,
            dry_run=dry_run,
            options=options,
        )
        asyncio.create_task(_execute_job_background(result["job_id"], dry_run=dry_run))
        return result
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409 if "active" in str(exc) else 400, detail=str(exc)) from exc


@router.post("/devices/{device_id}/audio-safety/verify")
async def verify_audio_safety(
    device_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Verify identity/volume/standby after an explicit human UI action."""

    if payload.get("confirm_stop_and_volume_one") is not True:
        raise HTTPException(status_code=400, detail="Die Audio-Sicherheitsprüfung muss ausdrücklich bestätigt werden.")
    try:
        row, candidate = selected_setup_devices(db, [device_id], require_eligible=True)[0]
        from basswiesn.app.services.setup_rebuild.radio_adapter import RadioSetupAdapter

        adapter_row = SimpleNamespace(
            device_id=row.device_id,
            ip_address=candidate.ip_address,
            expected_model=candidate.model,
            evidence_json="{}",
            backup_path="",
        )
        result = await RadioSetupAdapter().verify_audio_safety(adapter_row)
        write_masterlog(
            "setup_audio_safety_verified",
            device_id=row.device_id,
            radio_ip=candidate.ip_address,
            volume_before=result["volume_before"],
            volume_readback=result["final_volume"],
        )
        return result
    except (ValueError, RuntimeError, OSError) as exc:
        write_masterlog(
            "setup_audio_safety_verification_failed",
            device_id=str(device_id or "").strip().upper(),
            error=str(exc)[:500],
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/jobs/latest")
def latest_job(db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.query(SetupRebuildJob).order_by(SetupRebuildJob.created_at.desc()).first()
    if row is None:
        return {"job_id": "", "status": "none", "devices": []}
    return SetupRepository().public_job(db, row)


@router.get("/jobs/{job_id}")
def job_status(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = SetupRepository().job(db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="setup rebuild job not found")
    return SetupRepository().public_job(db, row)


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    repository = SetupRepository()
    row = repository.job(db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="setup rebuild job not found")
    row.cancel_requested = True
    db.commit()
    return repository.public_job(db, row)


@router.post("/jobs/{job_id}/rollback")
async def rollback(job_id: str) -> dict[str, Any]:
    try:
        return await get_coordinator().rollback(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/devices/{device_id}/ssh/status")
def ssh_status_preview(device_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    expected = _setup_ssh_device(db, device_id)
    return {
        "device_id": expected["device_id"],
        "ip_address": expected["ip_address"],
        "status": SshStatus.SSH_UNKNOWN.value,
        "action": "status is collected by the coordinator; preview performs no network access",
        "profile_required": True,
    }


def _setup_ssh_device(db: Session, device_id: str) -> dict[str, Any]:
    try:
        row, candidate = selected_setup_devices(db, [device_id], require_eligible=True)[0]
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "device_id": row.device_id,
        "ip_address": candidate.ip_address,
        "name": candidate.name,
        "model": candidate.model,
        "firmware": candidate.firmware,
    }


@router.get("/devices/{device_id}/ssh/status")
async def ssh_status_live(device_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Read live SSH status for one exact approved radio."""

    expected = _setup_ssh_device(db, device_id)
    from basswiesn.app.services.setup_rebuild.radio_adapter import RadioSetupAdapter

    row = SimpleNamespace(
        device_id=expected["device_id"],
        ip_address=expected["ip_address"],
        expected_model=expected["model"],
        evidence_json="{}",
    )
    try:
        adapter = RadioSetupAdapter()
        identity = await adapter.identify(row)
        row.evidence_json = json.dumps(identity, ensure_ascii=False)
        result = await adapter.ssh_status(row)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="SSH status read failed") from exc
    return {"device": identity, "ssh": result}


@router.post("/devices/{device_id}/ssh/activation/preview")
def ssh_activation_preview(device_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return a model/profile plan without contacting the radio."""

    expected = _setup_ssh_device(db, device_id)
    family = "portable" if "portable" in expected["model"].lower() else "stationary"
    profiles = [item.public_dict() for item in all_profiles() if item.model_family == family]
    return {
        "device_id": expected["device_id"],
        "ip_address": expected["ip_address"],
        "model": expected["model"],
        "profile_candidates": profiles,
        "execution": "coordinator_only",
        "required_before_write": ["identity", "backup", "profile_detection", "temporary_status", "persistence_readback"],
        "usb_required": False,
    }


@router.post("/devices/{device_id}/ssh/activation/start")
async def ssh_activation_start(
    device_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Start the complete safe one-device coordinator workflow.

    SSH activation is never exposed as an isolated write: the coordinator
    must retain the backup, routing and rollback checkpoints around it.
    """

    expected = _setup_ssh_device(db, device_id)
    scoped = dict(payload or {})
    scoped["device_ids"] = [expected["device_id"]]
    scoped["dry_run"] = bool(scoped.get("dry_run", False))
    try:
        target = resolve_server_target(scoped)
        result = get_coordinator().start(
            db,
            device_ids=[expected["device_id"]],
            target=target,
            dry_run=bool(scoped["dry_run"]),
            options={
                "ssh_required": True,
                "pair_account": bool(scoped.get("pair_account", True)),
                "playback_test": bool(scoped.get("playback_test", True)),
            },
        )
        asyncio.create_task(
            _execute_job_background(result["job_id"], dry_run=bool(scoped["dry_run"]))
        )
        return result
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
