"""Device HTTP routes migrated from the legacy API router."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from collections import Counter
import ipaddress
import xml.etree.ElementTree as ET

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.adapters.discovery import scan_subnet_detailed
from basswiesn.app.config import BASSWIESN_TABOO_HOSTS, _default_lan_host, get_settings, is_safe_radio_host, scan_cidr_for_host
from basswiesn.app.db import get_db
from basswiesn.app.db.repositories import DeviceIdentityRepository, DeviceRepository
from basswiesn.app.models import ConfigBackup, Device, MultiroomScenario, PlayHistory, Preset, ReferenceSetup, ScheduledAction, SetupPlan, Setting, TelemetryEvent
from basswiesn.app.services.device_identity_service import DeviceIdentityService
from basswiesn.app.services.device_discovery_service import DeviceDiscoveryService
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.services.device_service import DeviceService, device_summary
from basswiesn.app.services.device_state import load_runtime_state
from basswiesn.app.services.device_policy import (
    DeviceClass,
    PollingProfile,
    SafeModeSetting,
    policy_for_device,
)
from basswiesn.app.services.protected_devices import require_unprotected_device
from basswiesn.app.core.setup_mode import is_yes_confirmation


router = APIRouter(prefix="/api", tags=["devices"])


def _plain_ipv4(value: object) -> str:
    candidate = str(value or "").strip()
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="valid radio IPv4 address is required") from exc
    if ip.version != 4 or not is_safe_radio_host(str(ip)):
        raise HTTPException(status_code=400, detail="valid radio LAN IPv4 address is required")
    return candidate


def _configured_lan_host(db: Session | None = None) -> str:
    if db is not None:
        row = db.query(Setting).filter(Setting.key == "lan_host").one_or_none()
        if row and row.value.strip():
            return row.value.strip()
    return get_settings().lan_host


def _host_from_request(request: Request) -> str:
    host = request.url.hostname or ""
    if is_safe_radio_host(host):
        return host
    header_host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    return header_host if is_safe_radio_host(header_host) else ""


def _scan_cidr_from_context(payload: dict, request: Request, db: Session) -> str:
    for candidate in (
        payload.get("host"),
        payload.get("ui_host"),
        payload.get("lan_host"),
        _configured_lan_host(db),
        _host_from_request(request),
        _default_lan_host(),
    ):
        cidr = scan_cidr_for_host(str(candidate or "").strip())
        if cidr:
            return cidr
    return ""


def _reject_non_radio_target(ip_address: str, db: Session | None = None) -> None:
    if ip_address in BASSWIESN_TABOO_HOSTS:
        raise HTTPException(status_code=400, detail="blocked host must not be used as radio or setup host")
    if not is_safe_radio_host(ip_address):
        raise HTTPException(status_code=400, detail="invalid host must not be stored as a radio")
    if ip_address == _configured_lan_host(db):
        raise HTTPException(status_code=400, detail="server host must not be stored as a radio")


def _device_or_404(db: Session, device_id: str) -> Device:
    device = db.query(Device).filter(Device.device_id == device_id).one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    return device


def _delete_local_device_data(db: Session, device: Device) -> None:
    device_id = device.device_id
    for model in (Preset, SetupPlan, PlayHistory, TelemetryEvent, ConfigBackup):
        db.query(model).filter(getattr(model, "device_id") == device_id).delete(synchronize_session=False)
    db.query(ReferenceSetup).filter(ReferenceSetup.source_device_id == device_id).delete(synchronize_session=False)
    db.query(MultiroomScenario).filter(
        (MultiroomScenario.master_device_id == device_id) | (MultiroomScenario.trigger_device_id == device_id)
    ).delete(synchronize_session=False)
    for row in db.query(MultiroomScenario).all():
        row.member_device_ids = ",".join(item for item in (row.member_device_ids or "").split(",") if item and item != device_id)
    for row in db.query(ScheduledAction).all():
        row.device_ids = ",".join(item for item in (row.device_ids or "").split(",") if item and item != device_id)
        row.multiroom_member_ids = ",".join(item for item in (row.multiroom_member_ids or "").split(",") if item and item != device_id)
        if row.multiroom_master_id == device_id:
            row.multiroom_master_id = ""
    db.delete(device)


def _device_policy_summary(db: Session, device: Device) -> dict:
    _row, runtime = load_runtime_state(db, device.device_id)
    keepalive = runtime.get("playback_keepalive") or {}
    policy = policy_for_device(device, db, runtime_state=runtime)
    return {
        **policy.to_dict(),
        "device_id": device.device_id,
        "radio_ip": device.ip_address,
        "last_successful_keepalive": keepalive.get("last_keepalive_at", ""),
        "last_successful_lifesign": keepalive.get("last_keepalive_at", "") or (device.last_seen.isoformat() if device.last_seen else ""),
        "last_heartbeat_at": keepalive.get("last_heartbeat_at", ""),
        "last_failed_at": keepalive.get("last_failed_at", "") or (device.last_failed_at.isoformat() if device.last_failed_at else ""),
        "next_planned_poll": keepalive.get("next_retry_at", "") or policy.next_retry_at,
        "current_backoff_seconds": int(keepalive.get("backoff_seconds") or policy.backoff_seconds or 0),
        "last_skip_reason": keepalive.get("skip_reason", "") or policy.skip_reason,
        "reachable": bool(getattr(device, "reachable", True)),
        "offline_reason": getattr(device, "offline_reason", "") or "",
        "runtime_source": runtime.get("current_source", ""),
        "runtime_playback_state": runtime.get("playback_state", ""),
    }


def _enum_value(value: object, enum_type, field: str, *, allow_auto: bool = False) -> str:
    text = str(value or "").strip().lower()
    allowed = {item.value for item in enum_type}
    if allow_auto:
        allowed.add("auto")
    if text not in allowed:
        raise HTTPException(status_code=400, detail=f"invalid {field}: {text}")
    return text


@router.get("/devices")
async def list_devices(live: bool = False, db: Session = Depends(get_db)) -> list[dict]:
    repository = DeviceRepository(db)
    service = DeviceService(repository, client_factory=SoundTouchClient)
    rows = service.list_devices()
    if live:
        await service.refresh_devices(rows)
        repository.commit()
    return [
        {
            **device_summary(device),
            "policy": _device_policy_summary(db, device),
        }
        for device in rows
    ]


@router.get("/devices/health")
async def devices_health(db: Session = Depends(get_db)) -> list[dict]:
    return [_device_policy_summary(db, device) for device in db.query(Device).order_by(Device.name).all()]


@router.get("/devices/{device_id}/policy")
async def get_device_policy(device_id: str, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    return _device_policy_summary(db, device)


@router.put("/devices/{device_id}/policy")
async def update_device_policy(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = _device_or_404(db, device_id)
    require_unprotected_device(device, action="device_policy_update", requester="devices_api", method="PUT", endpoint=f"/api/devices/{device_id}/policy")
    if "device_class_override" in payload:
        device.device_class_override = _enum_value(payload.get("device_class_override"), DeviceClass, "device_class_override", allow_auto=True)
    if "safe_mode" in payload:
        device.safe_mode = _enum_value(payload.get("safe_mode"), SafeModeSetting, "safe_mode")
    if "polling_profile_override" in payload:
        device.polling_profile_override = _enum_value(payload.get("polling_profile_override"), PollingProfile, "polling_profile_override")
    for field in ("auto_restore_allowed", "battery_poll_allowed", "maintenance_actions_allowed"):
        if field in payload:
            setattr(device, field, bool(payload.get(field)))
    db.commit()
    write_masterlog(
        "device_policy_updated",
        device_id=device.device_id,
        radio_ip=device.ip_address,
        device_class_override=device.device_class_override,
        safe_mode=device.safe_mode,
        polling_profile_override=device.polling_profile_override,
        auto_restore_allowed=bool(device.auto_restore_allowed),
        battery_poll_allowed=bool(device.battery_poll_allowed),
        maintenance_actions_allowed=bool(device.maintenance_actions_allowed),
    )
    return _device_policy_summary(db, device)


@router.post("/devices")
async def add_device(payload: dict, db: Session = Depends(get_db)) -> dict:
    if not payload.get("ip_address"):
        raise HTTPException(status_code=400, detail="ip_address is required")
    if "soundtouch test" in str(payload.get("model") or "").lower():
        repository = DeviceRepository(db)
        service = DeviceService(repository, client_factory=SoundTouchClient)
        row = service.upsert_local_device(
            device_id=str(payload.get("device_id") or payload.get("ip_address")),
            ip_address=str(payload.get("ip_address") or "").strip(),
            name=payload.get("name"),
            model=payload.get("model"),
        )
        repository.commit()
        return {"device_id": row.device_id, "identity": {"canonical_id": row.device_id, "probed": False}}
    ip_address = _plain_ipv4(payload.get("ip_address"))
    if ip_address == _configured_lan_host(db):
        raise HTTPException(status_code=400, detail="server host must not be stored as a radio")
    _reject_non_radio_target(ip_address, db)
    repository = DeviceRepository(db)
    service = DeviceService(repository, client_factory=SoundTouchClient)
    probe = Device(device_id=ip_address, ip_address=ip_address)
    probe_result = await service.refresh_device(probe)
    if not probe_result["ok"]:
        write_masterlog("device_add_rejected", ip_address=ip_address, probe_error=str(probe_result["error"]))
        raise HTTPException(status_code=400, detail={"error": "IP is not a reachable SoundTouch radio on port 8090", "ip_address": ip_address, "probe_error": str(probe_result["error"])})
    info_xml = str(probe_result["info_xml"])
    try:
        root = ET.fromstring(info_xml)
    except ET.ParseError as exc:
        raise HTTPException(status_code=400, detail="radio /info returned invalid XML") from exc
    device_id = (root.attrib.get("deviceID") or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="radio /info did not include deviceID")
    row = service.upsert_local_device(
        device_id=device_id,
        ip_address=ip_address,
        name=payload.get("name") or root.findtext("name", ""),
        model=root.findtext("type", "") or payload.get("model") or "SoundTouch",
    )
    row.info_xml = info_xml
    row.firmware = root.findtext(".//softwareVersion", "") or row.firmware
    row, identity = DeviceIdentityService(DeviceIdentityRepository(db)).reconcile(row, info_xml)
    if payload.get("name"):
        row.name = str(payload.get("name"))
    identity["probed"] = True
    repository.commit()
    write_masterlog(
        "device_add",
        device_id=row.device_id,
        ip_address=row.ip_address,
        probed=identity.get("probed", False),
        probe_error=identity.get("probe_error"),
    )
    return {"device_id": row.device_id, "identity": identity}


@router.delete("/devices/{device_id}")
async def remove_device(device_id: str, payload: dict | None = None, db: Session = Depends(get_db)) -> dict:
    if not is_yes_confirmation((payload or {}).get("confirmation")):
        raise HTTPException(status_code=400, detail="confirmation required: YES")
    repository = DeviceRepository(db)
    device = repository.get_by_device_id(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    summary = device_summary(device)
    _delete_local_device_data(db, device)
    repository.commit()
    write_masterlog("device_remove", device_id=device_id, ip_address=summary.get("ip_address"))
    return {"removed": True, "device_id": device_id, "ip_address": summary.get("ip_address"), "radio_write": False}


@router.post("/devices/scan")
async def scan_devices(payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    cidr = payload.get("cidr") or _scan_cidr_from_context(payload, request, db)
    if not cidr:
        raise HTTPException(
            status_code=400,
            detail="A LAN host or explicit Scan CIDR is required. Use the LAN IP of the BASSWIESN host or enter your LAN CIDR.",
        )
    timeout = float(payload.get("timeout") or 0.7)
    limit = int(payload.get("limit") or 254)
    discovery = DeviceDiscoveryService(scanner=scan_subnet_detailed)
    write_masterlog("device_scan_start", cidr=cidr, timeout=timeout, limit=limit)
    try:
        result = await discovery.discover(cidr, timeout=timeout, limit=limit)
    except ValueError as exc:
        write_masterlog("device_scan_error", cidr=cidr, error_reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        write_masterlog(
            "device_scan_error",
            cidr=cidr,
            error_type=type(exc).__name__,
            error_reason=str(exc),
        )
        raise
    service = DeviceService(DeviceRepository(db))
    failure_counts = Counter(failure.code for failure in result.failures)
    safe_devices = []
    for item in result.devices:
        ip_address = str(item.get("ip_address") or "").strip()
        try:
            _reject_non_radio_target(ip_address, db)
        except HTTPException:
            continue
        safe_devices.append(item)
    found = service.record_discovery(safe_devices, persist=payload.get("save", True))
    write_masterlog(
        "device_scan_complete",
        cidr=cidr,
        scanned=result.scanned,
        found=len(found),
        timeout_count=failure_counts.get("timeout", 0),
        unreachable_count=failure_counts.get("unreachable", 0),
        invalid_response_count=failure_counts.get("invalid_response", 0),
        failures=len(result.failures),
    )
    response = {"cidr": cidr, "scanned": result.scanned, "found": found}
    if failure_counts:
        response["failure_summary"] = dict(failure_counts)
    return response
