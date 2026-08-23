import re
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from basswiesn.app.db import get_db
from basswiesn.app.config import get_settings
from basswiesn.app.core.setup_mode import is_yes_confirmation
from basswiesn.app.models import (
    ConfigBackup,
    Device,
    MultiroomScenario,
    PlayHistory,
    Preset,
    ReferenceSetup,
    ScheduledAction,
    SetupPlan,
    TelemetryEvent,
    RequestLog,
)
from basswiesn.app.routers.shared import device_or_404
from basswiesn.app.db.repositories import DeviceIdentityRepository
from basswiesn.app.services.device_identity_service import DeviceIdentityService
from basswiesn.app.services.maintenance import cleanup_plan, run_cleanup, storage_summary
from basswiesn.app.services.runtime_cleanup import run_runtime_cleanup

router = APIRouter(prefix="/api", tags=["devices"])

WRITE_ENDPOINT_HINTS = {
    "/addStation",
    "/addWirelessProfile",
    "/addGroup",
    "/addZoneSlave",
    "/bookmark",
    "/cancelPairLightswitch",
    "/clearBluetoothPaired",
    "/clearPairedList",
    "/clockDisplay",
    "/criticalError",
    "/enterBluetoothPairing",
    "/enterPairingMode",
    "/factoryDefault",
    "/key",
    "/language",
    "/lowPowerStandby",
    "/marge",
    "/name",
    "/nameSource",
    "/notification",
    "/pairLightswitch",
    "/playNotification",
    "/playbackRequest",
    "/powersaving",
    "/pushCustomerSupportInfoToMarge",
    "/rebroadcastlatencymode",
    "/removeGroup",
    "/removeMusicServiceAccount",
    "/removePreset",
    "/removeStation",
    "/removeZoneSlave",
    "/select",
    "/selectLastSoundTouchSource",
    "/selectLastSource",
    "/selectLastWiFiSource",
    "/selectLocalSource",
    "/selectPreset",
    "/setBCOReset",
    "/setComponentSoftwareVersion",
    "/setMargeAccount",
    "/setMusicServiceAccount",
    "/setMusicServiceOAuthAccount",
    "/setPairedStatus",
    "/setPairingStatus",
    "/setProductSerialNumber",
    "/setProductSoftwareVersion",
    "/setWiFiRadio",
    "/setZone",
    "/setup",
    "/slaveMsg",
    "/standby",
    "/storePreset",
    "/swUpdateAbort",
    "/swUpdateCheck",
    "/swUpdateStart",
    "/systemtimeout",
    "/updateGroup",
    "/userActivity",
    "/userPlayControl",
    "/userRating",
    "/userTrackControl",
}

READ_ENDPOINT_HINTS = {
    "/bass",
    "/bassCapabilities",
    "/bluetoothInfo",
    "/capabilities",
    "/clockTime",
    "/getActiveWirelessProfile",
    "/getBCOReset",
    "/getGroup",
    "/getZone",
    "/genreStations",
    "/info",
    "/introspect",
    "/listMediaServers",
    "/netStats",
    "/networkInfo",
    "/now_playing",
    "/nowPlaying",
    "/nowSelection",
    "/pdo",
    "/presets",
    "/recents",
    "/search",
    "/searchStation",
    "/serviceAvailability",
    "/sourceDiscoveryStatus",
    "/sources",
    "/soundTouchConfigurationStatus",
    "/speaker",
    "/stationInfo",
    "/supportedURLs",
    "/swUpdateQuery",
    "/test",
    "/trackInfo",
    "/volume",
}


def _xml_root(xml: str) -> ET.Element | None:
    try:
        return ET.fromstring(xml or "")
    except ET.ParseError:
        return None


def _xml_text(xml: str, tag: str) -> str:
    match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", xml or "", flags=re.DOTALL)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _supported_urls(xml: str) -> list[str]:
    root = _xml_root(xml)
    if root is None:
        return []
    return sorted({node.attrib.get("location", "") for node in root.findall(".//URL") if node.attrib.get("location")})


def _latest_backup(db: Session, device_id: str, path_suffix: str) -> ConfigBackup | None:
    return (
        db.query(ConfigBackup)
        .filter(ConfigBackup.device_id == device_id, ConfigBackup.path.endswith(path_suffix))
        .order_by(ConfigBackup.created_at.desc())
        .first()
    )


def _replace_csv_id(value: str, old: str, new: str) -> tuple[str, bool]:
    parts = [part.strip() for part in (value or "").split(",") if part.strip()]
    if not parts:
        return value or "", False
    changed = False
    replaced: list[str] = []
    for part in parts:
        if part == old:
            replaced.append(new)
            changed = True
        elif part not in replaced:
            replaced.append(part)
    return ",".join(replaced), changed


def _update_text_id_rows(db: Session, model, column_name: str, old: str, new: str) -> int:
    rows = db.query(model).filter(getattr(model, column_name) == old).all()
    for row in rows:
        setattr(row, column_name, new)
    return len(rows)


def _update_csv_id_rows(db: Session, model, column_name: str, old: str, new: str) -> int:
    changed = 0
    rows = db.query(model).all()
    for row in rows:
        value, did_change = _replace_csv_id(getattr(row, column_name) or "", old, new)
        if did_change:
            setattr(row, column_name, value)
            changed += 1
    return changed


def merge_device_into(db: Session, source: Device, target: Device) -> dict:
    """Merge a temporary/IP based device row into its canonical radio row."""
    return DeviceIdentityRepository(db).merge(source, target)


def reconcile_device_identity(db: Session, device: Device, info_xml: str) -> tuple[Device, dict]:
    return DeviceIdentityService(DeviceIdentityRepository(db)).reconcile(device, info_xml)


def _is_test_device(device: Device) -> bool:
    identifier = (device.device_id or "").upper()
    model = (device.model or "").lower()
    ip = device.ip_address or ""
    prefixes = ("TEST", "UITEST", "PWSETUP", "MRMASTER", "MRMEMBER", "SCHMASTER", "SCHMEMBER")
    return identifier.startswith(prefixes) or "soundtouch test" in model or ip.startswith(("192.0.2.", "127.0.0."))


def _delete_device_data(db: Session, device: Device) -> None:
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


def _cached_endpoint_xml(db: Session, device: Device, endpoint: str) -> tuple[str, str]:
    if endpoint == "/info" and device.info_xml:
        return device.info_xml, "device.info_xml"
    if endpoint == "/capabilities" and device.capabilities_xml:
        return device.capabilities_xml, "device.capabilities_xml"
    row = _latest_backup(db, device.device_id, f"http{endpoint}")
    if row is None:
        return "", ""
    return row.content, row.path


def _device_live_summary(db: Session, device: Device) -> dict:
    info_xml, info_source = _cached_endpoint_xml(db, device, "/info")
    supported_xml, supported_source = _cached_endpoint_xml(db, device, "/supportedURLs")
    urls = _supported_urls(supported_xml)
    radio_id = _xml_root(info_xml).attrib.get("deviceID", "") if _xml_root(info_xml) is not None else ""
    write_candidates = sorted(url for url in urls if url in WRITE_ENDPOINT_HINTS)
    read_candidates = sorted(url for url in urls if url in READ_ENDPOINT_HINTS)
    unknown_candidates = sorted(set(urls) - set(write_candidates) - set(read_candidates))
    return {
        "device_id": device.device_id,
        "radio_device_id": radio_id,
        "device_id_matches_radio": not radio_id or radio_id == device.device_id,
        "name": _xml_text(info_xml, "name") or device.name,
        "model": _xml_text(info_xml, "type") or device.model,
        "ip_address": device.ip_address,
        "firmware": _xml_text(info_xml, "softwareVersion") or device.firmware,
        "marge_url": _xml_text(info_xml, "margeURL"),
        "captures": {
            "info": info_source,
            "supportedURLs": supported_source,
            "http_capture_count": db.query(ConfigBackup).filter(
                ConfigBackup.device_id == device.device_id,
                ConfigBackup.path.like("radio-log/%/http%"),
            ).count(),
            "cli17000_readonly": bool(_latest_backup(db, device.device_id, "cli17000.txt")),
        },
        "endpoint_counts": {
            "total": len(urls),
            "read_or_probe": len(read_candidates),
            "write_or_control": len(write_candidates),
            "unclassified": len(unknown_candidates),
        },
        "endpoints": {
            "read_or_probe": read_candidates,
            "write_or_control": write_candidates,
            "unclassified": unknown_candidates,
        },
        "write_policy": "SupportedURL presence means capability evidence only. basswiesn still requires backup/preflight/confirmation before executing writes.",
    }


@router.post("/devices/{device_id}/migrate-id")
async def migrate_device_id(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    new_device_id = str(payload.get("new_device_id") or "").strip()
    if not new_device_id:
        raise HTTPException(status_code=400, detail="new_device_id is required")
    device = device_or_404(db, device_id)
    if device.device_id == new_device_id:
        return {"changed": False, "device_id": device.device_id, "note": "already migrated"}
    expected = "YES"
    if not is_yes_confirmation(payload.get("confirmation")):
        raise HTTPException(status_code=409, detail={"error": "migration confirmation required", "expected": expected})
    target = db.query(Device).filter(Device.device_id == new_device_id).one_or_none()
    if target is not None and target.id != device.id:
        raise HTTPException(status_code=409, detail="target device_id already exists")

    counts = {
        "presets": _update_text_id_rows(db, Preset, "device_id", device.device_id, new_device_id),
        "setup_plans": _update_text_id_rows(db, SetupPlan, "device_id", device.device_id, new_device_id),
        "play_history": _update_text_id_rows(db, PlayHistory, "device_id", device.device_id, new_device_id),
        "telemetry_events": _update_text_id_rows(db, TelemetryEvent, "device_id", device.device_id, new_device_id),
        "config_backups": _update_text_id_rows(db, ConfigBackup, "device_id", device.device_id, new_device_id),
        "reference_setups": _update_text_id_rows(db, ReferenceSetup, "source_device_id", device.device_id, new_device_id),
        "multiroom_master": _update_text_id_rows(db, MultiroomScenario, "master_device_id", device.device_id, new_device_id),
        "multiroom_trigger": _update_text_id_rows(db, MultiroomScenario, "trigger_device_id", device.device_id, new_device_id),
        "scheduled_multiroom_master": _update_text_id_rows(db, ScheduledAction, "multiroom_master_id", device.device_id, new_device_id),
        "play_history_zone_master": _update_text_id_rows(db, PlayHistory, "zone_master_id", device.device_id, new_device_id),
        "multiroom_members": _update_csv_id_rows(db, MultiroomScenario, "member_device_ids", device.device_id, new_device_id),
        "scheduled_devices": _update_csv_id_rows(db, ScheduledAction, "device_ids", device.device_id, new_device_id),
        "scheduled_multiroom_members": _update_csv_id_rows(db, ScheduledAction, "multiroom_member_ids", device.device_id, new_device_id),
        "play_history_zone_members": _update_csv_id_rows(db, PlayHistory, "zone_member_ids", device.device_id, new_device_id),
    }
    old_device_id = device.device_id
    device.device_id = new_device_id
    db.commit()
    return {"changed": True, "old_device_id": old_device_id, "new_device_id": new_device_id, "updated_rows": counts}


@router.get("/devices/{device_id}/live-summary")
async def device_live_summary(device_id: str, db: Session = Depends(get_db)) -> dict:
    return _device_live_summary(db, device_or_404(db, device_id))


@router.get("/devices/live-comparison")
async def device_live_comparison(
    device_ids: list[str] | None = Query(default=None),
    captured_only: bool = True,
    db: Session = Depends(get_db),
) -> dict:
    devices = db.query(Device).order_by(Device.name).all()
    if device_ids:
        selected = set(device_ids)
        devices = [device for device in devices if device.device_id in selected]
    summaries = [_device_live_summary(db, device) for device in devices]
    if captured_only:
        summaries = [
            item for item in summaries
            if item["captures"]["http_capture_count"] or item["captures"]["cli17000_readonly"]
        ]
    all_read = [set(item["endpoints"]["read_or_probe"]) for item in summaries]
    all_write = [set(item["endpoints"]["write_or_control"]) for item in summaries]
    common_read = sorted(set.intersection(*all_read)) if all_read else []
    common_write = sorted(set.intersection(*all_write)) if all_write else []
    return {
        "devices": summaries,
        "comparison": {
            "common_read_or_probe": common_read,
            "common_write_or_control": common_write,
            "different_endpoint_sets": len({tuple(item["endpoints"]["read_or_probe"] + item["endpoints"]["write_or_control"]) for item in summaries}) > 1,
            "device_id_mismatches": [item for item in summaries if not item["device_id_matches_radio"]],
        },
        "policy": "This endpoint compares observed support and stored captures. It does not perform radio writes.",
    }


@router.get("/maintenance/cleanup-preview")
async def cleanup_preview(db: Session = Depends(get_db)) -> dict:
    test_devices = [row for row in db.query(Device).all() if _is_test_device(row)]
    return {
        "test_devices": [{"device_id": row.device_id, "name": row.name, "ip_address": row.ip_address} for row in test_devices],
        "request_logs": db.query(RequestLog).count(),
        "telemetry_logs": db.query(TelemetryEvent).count(),
    }


@router.get("/maintenance/storage")
async def maintenance_storage(db: Session = Depends(get_db)) -> dict:
    return storage_summary(db)


@router.post("/maintenance/cleanup/dry-run")
async def maintenance_cleanup_dry_run(db: Session = Depends(get_db)) -> dict:
    result = cleanup_plan(db)
    settings = get_settings()
    result["filesystem"] = run_runtime_cleanup(
        settings.data_dir,
        max_bytes_by_area={
            "logs": max(1, settings.masterlog_max_mb) * 1024 * 1024 * max(1, getattr(settings, "masterlog_backup_count", 5)),
            "diagnostics": max(1, settings.diagnostic_max_size_mb) * 1024 * 1024,
            "support": max(1, getattr(settings, "support_bundle_max_mb", 50)) * 1024 * 1024,
        },
        dry_run=True,
    )
    return result


@router.post("/maintenance/cleanup/run")
async def maintenance_cleanup_run(db: Session = Depends(get_db)) -> dict:
    result = run_cleanup(db)
    settings = get_settings()
    result["filesystem"] = run_runtime_cleanup(
        settings.data_dir,
        max_bytes_by_area={
            "logs": max(1, settings.masterlog_max_mb) * 1024 * 1024 * max(1, getattr(settings, "masterlog_backup_count", 5)),
            "diagnostics": max(1, settings.diagnostic_max_size_mb) * 1024 * 1024,
            "support": max(1, getattr(settings, "support_bundle_max_mb", 50)) * 1024 * 1024,
        },
        dry_run=False,
    )
    return result


@router.post("/maintenance/clear-test-devices")
async def clear_test_devices(payload: dict, db: Session = Depends(get_db)) -> dict:
    if not is_yes_confirmation(payload.get("confirmation")):
        raise HTTPException(status_code=409, detail="confirmation YES required")
    rows = [row for row in db.query(Device).all() if _is_test_device(row)]
    removed = [row.device_id for row in rows]
    for row in rows:
        _delete_device_data(db, row)
    db.commit()
    return {"removed_count": len(removed), "removed_device_ids": removed}


@router.post("/maintenance/reconcile-devices")
async def reconcile_devices(payload: dict, db: Session = Depends(get_db)) -> dict:
    if not is_yes_confirmation(payload.get("confirmation")):
        raise HTTPException(status_code=409, detail="confirmation YES required")
    changes = []
    for row in list(db.query(Device).order_by(Device.id).all()):
        if row not in db:
            continue
        canonical, result = reconcile_device_identity(db, row, row.info_xml)
        if result.get("merged") or result.get("migrated"):
            changes.append(result)
    db.commit()
    return {"change_count": len(changes), "changes": changes}


@router.post("/maintenance/clear-logs")
async def clear_logs(payload: dict, db: Session = Depends(get_db)) -> dict:
    if not is_yes_confirmation(payload.get("confirmation")):
        raise HTTPException(status_code=409, detail="confirmation YES required")
    request_logs = db.query(RequestLog).delete(synchronize_session=False)
    telemetry_logs = db.query(TelemetryEvent).delete(synchronize_session=False)
    db.commit()
    return {"request_logs": request_logs, "telemetry_logs": telemetry_logs, "backups_preserved": True}
