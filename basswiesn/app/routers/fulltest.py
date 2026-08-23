from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.db import get_db
from basswiesn.app.models import Device, DeviceInteraction, Event, SystemBackup, WebhookEndpoint
from basswiesn.app.services import backup_restore
from basswiesn.app.services.announcements import announcements_status, create_announcement_job, preview_announcement
from basswiesn.app.services.diagnostic_export import create_diagnostic_export, diagnostic_preview
from basswiesn.app.services.dlna_experimental import discover_renderers, dlna_status
from basswiesn.app.services.events import create_event, event_to_dict, list_events
from basswiesn.app.services.health_center import latest_healthchecks, run_healthcheck
from basswiesn.app.services.lab_tools import lab_status, probe_port
from basswiesn.app.services.local_media import media_status, scan_media_root, search_media, upsert_media_root, validate_media_root
from basswiesn.app.services.local_updates import inspect_local_release_archive, prepare_local_update
from basswiesn.app.services.model_library import (
    model_definitions,
    reset_capability_override,
    resolve_device_model,
    set_capability_override,
)
from basswiesn.app.services.multiroom_safety import list_multiroom_scenarios_safe, save_multiroom_scenario
from basswiesn.app.services.preset_safety import build_preset_write_plan
from basswiesn.app.services.quick_fixes import execute_quick_fix, list_quick_fixes, preview_quick_fix
from basswiesn.app.services.ssdp_discovery import manual_discovery_test
from basswiesn.app.services.standby_clock_recovery import (
    STANDBY_CLOCK_CONFIRMATION,
    get_standby_clock_job,
    restore_standby_clock,
    standby_clock_status,
)
from basswiesn.app.services.telnet_device_control import (
    REBOOT_CONFIRMATION,
    poll_telnet_job,
    start_telnet_reboot,
    telnet_capabilities,
)
from basswiesn.app.services.webhooks import (
    deliver_webhook,
    endpoint_to_dict,
    upsert_webhook_endpoint,
    validate_webhook_target,
)


router = APIRouter(prefix="/api", tags=["basswiesn-release-candidate"])


class DiscoveryRequest(BaseModel):
    interface: str = ""
    timeout_seconds: int | None = Field(default=None, ge=1, le=15)


class OverrideRequest(BaseModel):
    value: str
    reason: str = ""


class QuickFixRequest(BaseModel):
    device_id: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    confirmation: str = ""


class WebhookRequest(BaseModel):
    name: str = "Webhook"
    url: str
    enabled: bool = False
    event_types: list[str] = Field(default_factory=list)
    allowlist: list[str] = Field(default_factory=list)
    secret: str = ""


class LocalUpdateRequest(BaseModel):
    path: str
    expected_sha256: str = ""
    confirmation: str = ""


class SetupJobRequest(BaseModel):
    job_type: str = "setup"
    device_id: str = ""
    steps: list[dict[str, Any]] = Field(default_factory=list)
    request: dict[str, Any] = Field(default_factory=dict)


class SetupStatusRequest(BaseModel):
    status: str
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class MediaRootRequest(BaseModel):
    path: str
    label: str = ""
    enabled: bool = True


class PresetPlanRequest(BaseModel):
    changes: list[dict[str, Any]]


class MultiroomScenarioRequest(BaseModel):
    name: str
    master_device_id: str
    member_device_ids: list[str] = Field(default_factory=list)
    station_id: int | None = None
    volume: int | None = Field(default=None, ge=0, le=100)


class AnnouncementRequest(BaseModel):
    device_id: str = ""
    text: str = Field(default="", max_length=300)
    language: str = "de"
    volume: int = Field(default=20, ge=0, le=100)
    max_volume: int = Field(default=30, ge=0, le=100)
    confirmation: str = ""


class TelnetRebootRequest(BaseModel):
    confirmation: str = Field(default=REBOOT_CONFIRMATION, max_length=80)


class StandbyClockRestoreRequest(BaseModel):
    confirmation: str = Field(default=STANDBY_CLOCK_CONFIRMATION, max_length=80)
    timezone: str = Field(default="Europe/Berlin", max_length=80)


def _device_or_404(db: Session, device_id: str) -> Device:
    device = db.query(Device).filter(Device.device_id == device_id).one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    return device


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@router.post("/discovery/ssdp")
async def discovery_ssdp(payload: DiscoveryRequest, db: Session = Depends(get_db)) -> dict:
    result = await manual_discovery_test(db, interface=payload.interface, timeout_seconds=payload.timeout_seconds)
    db.commit()
    return result


@router.get("/discovery/test")
async def discovery_test(db: Session = Depends(get_db)) -> dict:
    result = await manual_discovery_test(db)
    db.commit()
    return result


@router.get("/device-models")
def get_device_models(db: Session = Depends(get_db)) -> dict:
    return {"version": get_settings().version, "models": model_definitions(db)}


@router.get("/device-capabilities/{device_id}")
def get_device_capabilities(device_id: str, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    return resolve_device_model(device, db).to_dict()


@router.post("/device-capabilities/{device_id}/overrides/{capability_key}")
def set_device_capability_override(device_id: str, capability_key: str, payload: OverrideRequest, db: Session = Depends(get_db)) -> dict:
    _device_or_404(db, device_id)
    try:
        result = set_capability_override(db, device_id, capability_key, payload.value, payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    create_event(db, "device_state_changed", device_id=device_id, payload={"capability_override": result})
    db.commit()
    return result


@router.delete("/device-capabilities/{device_id}/overrides/{capability_key}")
def delete_device_capability_override(device_id: str, capability_key: str, db: Session = Depends(get_db)) -> dict:
    _device_or_404(db, device_id)
    changed = reset_capability_override(db, device_id, capability_key)
    create_event(db, "device_state_changed", device_id=device_id, payload={"capability_override_reset": capability_key, "changed": changed})
    db.commit()
    return {"changed": changed}


@router.get("/device-interactions")
def list_device_interactions(
    db: Session = Depends(get_db),
    device_id: str = "",
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    query = db.query(DeviceInteraction)
    if device_id:
        query = query.filter(DeviceInteraction.device_id == device_id)
    rows = query.order_by(DeviceInteraction.started_at.desc()).limit(limit).all()
    return {
        "items": [
            {
                "event_id": row.event_id,
                "correlation_id": row.correlation_id,
                "device_id": row.device_id,
                "device_name": row.device_name,
                "device_class": row.device_class,
                "ip_address": row.ip_address,
                "request_purpose": row.request_purpose,
                "requester": row.requester,
                "priority": row.priority,
                "method": row.method,
                "endpoint": row.endpoint,
                "started_at": row.started_at.isoformat() if row.started_at else "",
                "duration_ms": row.duration_ms,
                "timeout_seconds": row.timeout_seconds,
                "attempt": row.attempt,
                "result": row.result,
                "status_code": row.status_code,
                "error_class": row.error_class,
                "polling_profile": row.polling_profile,
                "safe_mode_state": row.safe_mode_state,
                "circuit_breaker_state": row.circuit_breaker_state,
                "lock_wait_ms": row.lock_wait_ms,
                "cache_hit": row.cache_hit,
                "skipped": row.skipped,
                "skip_reason": row.skip_reason,
            }
            for row in rows
        ]
    }


@router.get("/devices/{device_id}/telnet/capabilities")
def device_telnet_capabilities(device_id: str, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    return telnet_capabilities(db, device)


@router.post("/devices/{device_id}/telnet/reboot")
async def device_telnet_reboot_job(device_id: str, payload: TelnetRebootRequest, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    try:
        result = await start_telnet_reboot(db, device, confirmation=payload.confirmation)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return result


@router.get("/devices/{device_id}/telnet/jobs/{job_id}")
def device_telnet_job(device_id: str, job_id: str, db: Session = Depends(get_db)) -> dict:
    _device_or_404(db, device_id)
    try:
        result = poll_telnet_job(db, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result.get("device_id") != device_id:
        raise HTTPException(status_code=404, detail="telnet job not found")
    db.commit()
    return result


@router.get("/devices/{device_id}/standby-clock/status")
def device_standby_clock_status(device_id: str, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    return standby_clock_status(db, device)


@router.post("/devices/{device_id}/standby-clock/restore")
async def device_standby_clock_restore(device_id: str, payload: StandbyClockRestoreRequest, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    try:
        result = await restore_standby_clock(db, device, confirmation=payload.confirmation, timezone=payload.timezone)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return result


@router.get("/devices/{device_id}/standby-clock/jobs/{job_id}")
def device_standby_clock_job(device_id: str, job_id: str, db: Session = Depends(get_db)) -> dict:
    _device_or_404(db, device_id)
    try:
        result = get_standby_clock_job(db, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result.get("device_id") != device_id:
        raise HTTPException(status_code=404, detail="standby clock job not found")
    return result


@router.post("/health/center")
async def run_health_center(include_device_http: bool = False, db: Session = Depends(get_db)) -> dict:
    result = await run_healthcheck(db, include_device_http=include_device_http)
    db.commit()
    return result


@router.get("/health/center/latest")
def health_center_latest(db: Session = Depends(get_db)) -> dict:
    return {"items": latest_healthchecks(db)}


@router.get("/quick-fixes")
def quick_fixes() -> dict:
    return {"items": list_quick_fixes()}


@router.post("/quick-fixes/{quick_fix_id}/preview")
def quick_fix_preview(quick_fix_id: str, payload: QuickFixRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return preview_quick_fix(db, quick_fix_id, device_id=payload.device_id, parameters=payload.parameters)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/quick-fixes/{quick_fix_id}/execute")
def quick_fix_execute(quick_fix_id: str, payload: QuickFixRequest, db: Session = Depends(get_db)) -> dict:
    try:
        result = execute_quick_fix(db, quick_fix_id, confirmation=payload.confirmation, device_id=payload.device_id, parameters=payload.parameters)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


@router.get("/events")
def get_events(db: Session = Depends(get_db), event_type: str = "", device_id: str = "", severity: str = "", limit: int = Query(default=100, ge=1, le=500)) -> dict:
    return {"items": list_events(db, limit=limit, event_type=event_type, device_id=device_id, severity=severity)}


@router.post("/events/test")
def create_test_event(db: Session = Depends(get_db)) -> dict:
    event = create_event(db, "healthcheck_recovered", severity="info", payload={"test": True})
    db.commit()
    return event_to_dict(event)


@router.get("/webhooks/validate")
def webhook_validate(url: str) -> dict:
    return validate_webhook_target(url, set(get_settings().webhook_allowed_hosts) or None)


@router.get("/webhooks")
def list_webhooks(db: Session = Depends(get_db)) -> dict:
    rows = db.query(WebhookEndpoint).order_by(WebhookEndpoint.id).all()
    return {"enabled_globally": get_settings().webhooks_enabled, "items": [endpoint_to_dict(row) for row in rows]}


@router.post("/webhooks")
def create_webhook(payload: WebhookRequest, db: Session = Depends(get_db)) -> dict:
    try:
        endpoint = upsert_webhook_endpoint(db, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return endpoint_to_dict(endpoint)


@router.put("/webhooks/{endpoint_id}")
def update_webhook(endpoint_id: int, payload: WebhookRequest, db: Session = Depends(get_db)) -> dict:
    try:
        endpoint = upsert_webhook_endpoint(db, payload.model_dump(), endpoint_id=endpoint_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return endpoint_to_dict(endpoint)


@router.delete("/webhooks/{endpoint_id}")
def delete_webhook(endpoint_id: int, db: Session = Depends(get_db)) -> dict:
    endpoint = db.query(WebhookEndpoint).filter(WebhookEndpoint.id == endpoint_id).one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="webhook not found")
    endpoint.enabled = False
    db.delete(endpoint)
    db.commit()
    return {"deleted": True}


@router.post("/webhooks/{endpoint_id}/test")
async def test_webhook(endpoint_id: int, db: Session = Depends(get_db)) -> dict:
    endpoint = db.query(WebhookEndpoint).filter(WebhookEndpoint.id == endpoint_id).one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="webhook not found")
    event = create_event(db, "healthcheck_recovered", payload={"test_delivery": True})
    result = await deliver_webhook(db, endpoint, event)
    db.commit()
    return result


@router.get("/backups")
def list_backups(db: Session = Depends(get_db)) -> dict:
    root = backup_restore.backup_root()
    files = []
    for path in sorted(root.glob("*.zip"), key=lambda item: item.stat().st_mtime, reverse=True):
        files.append({"filename": path.name, "path": str(path), "size_bytes": path.stat().st_size, "sha256": _sha256(path)})
    rows = db.query(SystemBackup).order_by(SystemBackup.created_at.desc()).limit(100).all()
    return {"items": files, "journal": [{"backup_id": row.backup_id, "filename": row.filename, "sha256": row.sha256, "created_at": row.created_at.isoformat() if row.created_at else ""} for row in rows]}


@router.post("/backups/create")
def create_backup(db: Session = Depends(get_db)) -> dict:
    result = backup_restore.create_system_backup(db)
    path = Path(result["path"])
    journal = SystemBackup(
        backup_id=path.stem,
        path=str(path),
        filename=path.name,
        version=get_settings().version,
        schema_version="2",
        size_bytes=path.stat().st_size,
        sha256=_sha256(path),
        quick_check=result.get("manifest", {}).get("quick_check", ""),
        manifest_json=json.dumps(result.get("manifest", {}), ensure_ascii=False),
    )
    db.add(journal)
    create_event(db, "backup_created", payload={"filename": path.name, "sha256": journal.sha256})
    db.commit()
    return {**result, "sha256": journal.sha256}


@router.post("/restore/preview")
def restore_preview(payload: dict, db: Session = Depends(get_db)) -> dict:
    try:
        return backup_restore.preview_system_backup(str(payload.get("path") or ""))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/restore/prepare")
def restore_prepare(payload: dict, db: Session = Depends(get_db)) -> dict:
    try:
        result = backup_restore.prepare_system_restore(db, str(payload.get("path") or ""))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    create_event(db, "restore_prepared", payload={"path": result.get("path", ""), "prepared": result.get("prepared")})
    db.commit()
    return result


@router.post("/updates/local/preview")
def update_local_preview(payload: LocalUpdateRequest) -> dict:
    return inspect_local_release_archive(payload.path, expected_sha256=payload.expected_sha256)


@router.post("/updates/local/prepare")
def update_local_prepare(payload: LocalUpdateRequest, db: Session = Depends(get_db)) -> dict:
    result = prepare_local_update(db, payload.path, expected_sha256=payload.expected_sha256, confirmation=payload.confirmation)
    if result.get("ok"):
        create_event(db, "update_started", payload={"local_archive": True, "path": payload.path, "prepared_only": True})
        db.commit()
    return result


@router.get("/setup-jobs")
def setup_jobs(limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db)) -> dict:
    from basswiesn.app.services.setup_jobs import list_setup_jobs

    return {"items": list_setup_jobs(db, limit=limit)}


@router.post("/setup-jobs")
def setup_job_create(payload: SetupJobRequest, db: Session = Depends(get_db)) -> dict:
    from basswiesn.app.services.setup_jobs import create_setup_job

    request = payload.request
    request.update({"job_type": payload.job_type, "device_id": payload.device_id, "steps": payload.steps})
    result = create_setup_job(db, request)
    db.commit()
    return result


@router.post("/setup-jobs/{job_id}/status")
def setup_job_status(job_id: str, payload: SetupStatusRequest, db: Session = Depends(get_db)) -> dict:
    from basswiesn.app.services.setup_jobs import update_setup_job_status

    try:
        result = update_setup_job_status(db, job_id, payload.status, result=payload.result, error=payload.error)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


@router.get("/media/library/status")
def media_library_status(db: Session = Depends(get_db)) -> dict:
    return media_status(db)


@router.post("/media/library/roots")
def media_root_create(payload: MediaRootRequest, db: Session = Depends(get_db)) -> dict:
    try:
        result = upsert_media_root(db, payload.path, label=payload.label, enabled=payload.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


@router.get("/media/library/validate-root")
def media_root_validate(path: str) -> dict:
    return validate_media_root(path).to_dict()


@router.post("/media/library/roots/{root_id}/scan")
def media_root_scan(root_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        result = scan_media_root(db, root_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


@router.get("/media/library/search")
def media_search(q: str = "", limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db)) -> dict:
    return {"items": search_media(db, query=q, limit=limit)}


@router.get("/dlna/status")
def get_dlna_status() -> dict:
    return dlna_status()


@router.post("/dlna/discover")
async def dlna_discover() -> dict:
    return await discover_renderers()


@router.get("/announcements/status")
def get_announcements_status() -> dict:
    return announcements_status()


@router.post("/announcements/preview")
def announcement_preview(payload: AnnouncementRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return preview_announcement(db, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/announcements/jobs")
def announcement_job_create(payload: AnnouncementRequest, db: Session = Depends(get_db)) -> dict:
    try:
        result = create_announcement_job(db, payload.model_dump(), confirmation=payload.confirmation)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


@router.get("/diagnostics/system/preview")
def diagnostics_system_preview(include_logs: bool = True, anonymize: bool = True, db: Session = Depends(get_db)) -> dict:
    return diagnostic_preview(db, include_logs=include_logs, anonymize=anonymize)


@router.post("/diagnostics/system/export")
def diagnostics_system_export(include_logs: bool = True, anonymize: bool = True, db: Session = Depends(get_db)) -> dict:
    return create_diagnostic_export(db, include_logs=include_logs, anonymize=anonymize)


@router.get("/lab/status")
def get_lab_status() -> dict:
    return lab_status()


@router.get("/lab/port-probe")
def lab_port_probe(host: str, port: int = Query(ge=1, le=65535)) -> dict:
    return probe_port(host, port)


@router.post("/presets/{device_id}/plan")
def preset_plan(device_id: str, payload: PresetPlanRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return build_preset_write_plan(db, device_id, payload.changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/multiroom/scenarios-safe")
def multiroom_scenarios_safe(db: Session = Depends(get_db)) -> dict:
    return {"items": list_multiroom_scenarios_safe(db)}


@router.post("/multiroom/scenarios-safe")
def multiroom_scenario_safe_create(payload: MultiroomScenarioRequest, db: Session = Depends(get_db)) -> dict:
    result = save_multiroom_scenario(db, payload.model_dump())
    db.commit()
    return result
