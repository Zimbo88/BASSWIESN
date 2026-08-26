import json
import os
import ipaddress
import re
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.adapters.ssh import LEGACY_SSH_ALGORITHM_OPTIONS, build_legacy_ssh_command
from basswiesn.app.core.setup_mode import is_yes_confirmation
from basswiesn.app.db import get_db
from basswiesn.app.models import Device, MediaPlaylist, Preset, ReferenceSetup, Setting, Station, TelemetryEvent, utc_now
from basswiesn.app.routers.shared import (
    battery_percent,
    device_or_404,
    language_codes,
    memory_check_plan,
    preset_slot_dict,
    require_memory_checked,
    setting_rows,
    summarize_payload,
    enforce_ip_write_guard,
)
from basswiesn.app.routers.telemetry import send_cli17000
from basswiesn.app.services.catalogs import DISPLAY_METADATA_MODES, MEDIA_LIBRARY_CAPABILITIES, STOCKHOLM_LANGUAGES, TIME_ZONES
from basswiesn.app.services.battery import battery_state, portable_battery_diagnosis
from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.services.updates import check_update
from basswiesn.app.services.backup_restore import create_system_backup, prepare_system_restore, preview_system_backup
from basswiesn.app.services.offline_mode import allowed_stream_hosts, external_request_decision, offline_status, record_dependency
from basswiesn.app.services.offline_preflight import build_offline_preflight, probe_stream_reference
from basswiesn.app.services.protected_devices import (
    protected_device_ids,
    protected_device_ips,
    require_unprotected_device,
)

router = APIRouter(prefix="/api", tags=["media"])

WEB_LANGUAGES = [
    {"code": "de", "label": "Deutsch"}, {"code": "en", "label": "English"}, {"code": "fr", "label": "Français"}, {"code": "es", "label": "Español"}, {"code": "it", "label": "Italiano"}, {"code": "pt", "label": "Português"}, {"code": "nl", "label": "Nederlands"}, {"code": "da", "label": "Dansk"}, {"code": "sv", "label": "Svenska"}, {"code": "no", "label": "Norsk"}, {"code": "fi", "label": "Suomi"}, {"code": "pl", "label": "Polski"}, {"code": "cs", "label": "Čeština"}, {"code": "sk", "label": "Slovenčina"}, {"code": "hu", "label": "Magyar"}, {"code": "ro", "label": "Română"}, {"code": "bg", "label": "Български"}, {"code": "hr", "label": "Hrvatski"}, {"code": "sl", "label": "Slovenščina"}, {"code": "el", "label": "Ελληνικά"}, {"code": "tr", "label": "Türkçe"}, {"code": "ru", "label": "Русский"}, {"code": "uk", "label": "Українська"}, {"code": "ja", "label": "日本語"}, {"code": "zh", "label": "中文"},
]
WEB_LANGUAGE_CODES = {item["code"] for item in WEB_LANGUAGES}


def _detected_web_language(raspberry_locale: str, browser_locale: str) -> str:
    for locale in (raspberry_locale, browser_locale):
        code = locale.lower().replace("-", "_").split("_", 1)[0]
        code = "no" if code == "nb" else code
        if code in WEB_LANGUAGE_CODES:
            return code
    return "en"

BATTERY_CLI_COMMANDS = ["ba p", "ba 0", "ba 1", "ba 2", "ba 3", "ba 5", "ba 6", "ba 7", "ba 8", "ba 9", "ba c", "ba n"]
BATTERY_MONITOR_PATCH = {
    "file": "/opt/Bose/BatteryMonitor",
    "backup_file": "/mnt/nv/BatteryMonitor.basswiesn-backup",
    "offset_decimal": 322773,
    "offset_hex": "0x4ecd5",
    "expected_original_sha256": "93b23730a3ac66f3331f63711d4a1d1d60704ccb03635b7ab8210c1f084f4fa0",
    "expected_patched_sha256": "4abbb803a20323bf2938e686aa43fe969495e508f4fefe9c6099702f1e7e4e71",
    "known_custom_patch_sha256": "25ed53ef0bb3a8647d6f858ccc4be20dc6292f988acb29059416d5c0591229c5",
    "expected_original_bytes_hex": "53 41 4e 59 4f",
    "patch_bytes_hex": "41 00 00 00 00",
    "meaning": "Patch only the SANYO check bytes in BatteryMonitor; do not replace the whole file.",
}
BATTERY_PATCH_CONFIRMATION = "BASSWIESN BATTERY PATCH"
BATTERY_ROLLBACK_CONFIRMATION = "BASSWIESN BATTERY ROLLBACK"


def _battery_patch_plan(device_ip: str) -> dict:
    offset = BATTERY_MONITOR_PATCH["offset_decimal"]
    return {
        **BATTERY_MONITOR_PATCH,
        "purpose": "Manual LAB patch for third-party or replacement SoundTouch Portable batteries when the original BatteryMonitor rejects a compatible pack.",
        "what_it_changes": "Replaces exactly five bytes at the verified BatteryMonitor offset. The original byte sequence spells SANYO; the patch byte sequence disables that strict vendor check so compatible replacement packs can be accepted.",
        "why": "Some SoundTouch Portable firmware builds accept only a narrow BatteryMonitor profile. Unknown but electrically compatible replacement packs can then be reported as missing or invalid. The patch keeps the original file in place and changes only the known vendor-check bytes.",
        "safety": "Manual LAB only. BASSWIESN checks model, current SHA-256, bytes at the offset, backup, write guard and read-back before reporting success. It is never part of normal setup and never runs as background battery polling.",
        "confirmation_apply": BATTERY_PATCH_CONFIRMATION,
        "confirmation_rollback": BATTERY_ROLLBACK_CONFIRMATION,
        "warnings": [
            "Nur auf eigenen SoundTouch-Portable-Geraeten ausfuehren.",
            "Falsche Firmware oder falsche Checksumme wird blockiert.",
            "Schreibzugriff remountet das Radio-Dateisystem kurzzeitig rw.",
            "Backup und Read-back sind Pflicht; ohne Backup kein Rollback.",
            "Ein Neustart des Radios kann danach manuell erforderlich sein.",
        ],
        "read_verify_commands": [
            "sha256sum /opt/Bose/BatteryMonitor",
            f"dd if=/opt/Bose/BatteryMonitor bs=1 skip={offset} count=5 2>/dev/null | hexdump -v -e '1/1 \"%02x \"'",
        ],
        "byte_patch_commands": [
            "cp /opt/Bose/BatteryMonitor /mnt/nv/BatteryMonitor.basswiesn-backup",
            "mount -o remount,rw /",
            f"printf '\\101\\000\\000\\000\\000' | dd of=/opt/Bose/BatteryMonitor bs=1 seek={offset} conv=notrunc",
            "sync",
            f"dd if=/opt/Bose/BatteryMonitor bs=1 skip={offset} count=5 2>/dev/null | hexdump -v -e '1/1 \"%02x \"'",
            "sha256sum /opt/Bose/BatteryMonitor",
            "mount -o remount,ro /",
        ],
        "target": device_ip,
        "execution": "manual LAB endpoints only: dry-run, apply and rollback with explicit confirmation phrase.",
        "rollback": "Restore /mnt/nv/BatteryMonitor.basswiesn-backup to /opt/Bose/BatteryMonitor, sync, remount read-only, reboot, then verify sha256.",
    }


def _clean_hex(text: str) -> str:
    return " ".join(re.findall(r"[0-9a-fA-F]{2}", text or "")).lower()


def _battery_patch_supported_sha() -> set[str]:
    return {
        BATTERY_MONITOR_PATCH["expected_original_sha256"],
        BATTERY_MONITOR_PATCH["expected_patched_sha256"],
        BATTERY_MONITOR_PATCH["known_custom_patch_sha256"],
    }


def _battery_patchable(sha256: str) -> bool:
    return sha256 == BATTERY_MONITOR_PATCH["expected_original_sha256"]


def _run_battery_ssh(device: Device, remote_command: str, *, username: str = "root", timeout: int = 20) -> str:
    require_unprotected_device(device, action="battery_patch_ssh", requester="battery_monitor_patch", method="SSH", endpoint=BATTERY_MONITOR_PATCH["file"])
    command = build_legacy_ssh_command(device.ip_address, username, remote_command, connect_timeout=5)
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise HTTPException(status_code=502, detail={"error": "SSH command failed", "returncode": result.returncode, "output": output[-1200:]})
    return output


def _parse_battery_monitor_probe(output: str) -> dict:
    sha = ""
    backup_sha = ""
    bytes_hex = ""
    section = ""
    for line in (output or "").splitlines():
        stripped = line.strip()
        if stripped in {"__SHA__", "__BYTES__", "__BACKUP__"}:
            section = stripped
            continue
        if section == "__SHA__" and not sha and re.match(r"^[0-9a-f]{64}\b", stripped):
            sha = stripped.split()[0]
        elif section == "__BACKUP__" and not backup_sha and re.match(r"^[0-9a-f]{64}\b", stripped):
            backup_sha = stripped.split()[0]
        elif section == "__BYTES__" and not bytes_hex:
            candidate = _clean_hex(stripped)
            if candidate:
                bytes_hex = candidate
    return {"battery_monitor_sha256": sha, "battery_monitor_bytes": bytes_hex, "backup_sha256": backup_sha}


async def _battery_monitor_status(device: Device, *, include_http: bool = False) -> dict:
    power_xml = ""
    if include_http:
        try:
            power_xml = await SoundTouchClient(device.ip_address).get_xml("/powerManagement")
        except Exception:
            power_xml = ""
    offset = BATTERY_MONITOR_PATCH["offset_decimal"]
    target = BATTERY_MONITOR_PATCH["file"]
    backup = BATTERY_MONITOR_PATCH["backup_file"]
    remote = (
        "echo __SHA__; sha256sum {target} 2>/dev/null || true; "
        "echo __BYTES__; dd if={target} bs=1 skip={offset} count=5 2>/dev/null | od -An -tx1 -v || true; "
        "echo __BACKUP__; test -s {backup} && sha256sum {backup} || true"
    ).format(target=target, offset=offset, backup=backup)
    ssh_error = ""
    probe = {"battery_monitor_sha256": "", "battery_monitor_bytes": "", "backup_sha256": ""}
    try:
        probe = _parse_battery_monitor_probe(_run_battery_ssh(device, remote, timeout=10))
    except HTTPException as exc:
        ssh_error = str(exc.detail)
    diagnosis = portable_battery_diagnosis(device.model, power_xml, "", probe["battery_monitor_sha256"], probe["battery_monitor_bytes"], probe["backup_sha256"])
    diagnosis.update({
        "device_id": device.device_id,
        "name": device.name,
        "ip": device.ip_address,
        "ssh_status": "available" if not ssh_error else "unavailable",
        "ssh_error": ssh_error,
        "patch_plan": _battery_patch_plan(device.ip_address),
        "manual_lab_only": True,
        "background_polling": False,
        "warning": "Diese Abfrage ist ein manueller LAB-Status fuer den Patchpfad. Sie ist keine regelmaessige Batterieueberwachung.",
    })
    return diagnosis


def _battery_removed(device_id: str = "") -> dict:
    return {
        "ok": False,
        "disabled": True,
        "device_id": device_id,
        "feature": "battery_polling",
        "message": "Regelmaessige Batterieabfragen sind in BASSWIESN 1.5.0 deaktiviert. Der BatteryMonitor-Patch bleibt nur als manuelle LAB-Aktion verfuegbar.",
    }


@router.get("/battery/status/{device_id}")
async def battery_patch_status(device_id: str, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    require_unprotected_device(device, action="battery_patch_status", requester="battery_monitor_patch", method="SSH", endpoint=BATTERY_MONITOR_PATCH["file"])
    return await _battery_monitor_status(device, include_http=False)


@router.post("/battery/patch/{device_id}/dry-run")
async def battery_patch_dry_run(device_id: str, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    require_unprotected_device(device, action="battery_patch_dry_run", requester="battery_monitor_patch", method="SSH", endpoint=BATTERY_MONITOR_PATCH["file"])
    status = await _battery_monitor_status(device, include_http=False)
    return {
        "dry_run": True,
        "will_write": False,
        "device_id": device.device_id,
        "status": status,
        "plan": status.get("patch_plan", _battery_patch_plan(device.ip_address)),
        "required_confirmation": BATTERY_PATCH_CONFIRMATION,
    }


@router.post("/battery/patch/{device_id}/apply")
async def battery_patch_apply(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    require_unprotected_device(device, action="battery_patch_apply", requester="battery_monitor_patch", method="SSH", endpoint=BATTERY_MONITOR_PATCH["file"])
    if str(payload.get("confirmation") or "").strip() != BATTERY_PATCH_CONFIRMATION:
        raise HTTPException(status_code=409, detail={"error": "confirmation phrase required", "expected": BATTERY_PATCH_CONFIRMATION, "why": "BatteryMonitor patch writes to the radio filesystem."})
    require_memory_checked(device, payload)
    enforce_ip_write_guard(db, device)
    status = await _battery_monitor_status(device, include_http=False)
    if not status["supported_portable"]:
        raise HTTPException(status_code=409, detail="unsupported portable model")
    current_sha = status["battery_monitor_sha256"]
    if current_sha not in _battery_patch_supported_sha():
        raise HTTPException(status_code=409, detail={"error": "unsupported checksum", "sha256": current_sha})
    if not _battery_patchable(current_sha):
        return {"changed": False, "already_patched": status["patch_status"] == "patched", "status": status}
    offset = BATTERY_MONITOR_PATCH["offset_decimal"]
    target = BATTERY_MONITOR_PATCH["file"]
    backup = BATTERY_MONITOR_PATCH["backup_file"]
    patch_bytes = "".join(f"\\x{part}" for part in BATTERY_MONITOR_PATCH["patch_bytes_hex"].split())
    original = BATTERY_MONITOR_PATCH["expected_original_sha256"]
    patched = BATTERY_MONITOR_PATCH["expected_patched_sha256"]
    remote = (
        "set -e; trap 'mount -o remount,ro / 2>/dev/null || true' EXIT; "
        "test \"$(sha256sum {target} | awk '{{print $1}}')\" = \"{original}\"; "
        "cp {target} {backup}; sync; test -s {backup}; "
        "mount -o remount,rw /; "
        "printf '{patch_bytes}' | dd of={target} bs=1 seek={offset} conv=notrunc; "
        "sync; mount -o remount,ro /; "
        "sha256sum {target}; sha256sum {backup}"
    ).format(target=target, backup=backup, original=original, patch_bytes=patch_bytes, offset=offset)
    output = _run_battery_ssh(device, remote, timeout=25)
    verify = await _battery_monitor_status(device, include_http=False)
    if verify["battery_monitor_sha256"] not in {patched, BATTERY_MONITOR_PATCH["known_custom_patch_sha256"]} and verify["patch_status"] != "patched":
        raise HTTPException(status_code=502, detail={"error": "patched bytes/checksum verification failed", "verify": verify, "output": output[-1200:]})
    write_masterlog("battery_patch_apply_complete", device_id=device.device_id, radio_ip=device.ip_address, sha256=verify["battery_monitor_sha256"])
    return {"changed": True, "manual_lab_only": True, "output": output[-1200:], "status": verify, "rollback_confirmation": BATTERY_ROLLBACK_CONFIRMATION}


@router.post("/battery/patch/{device_id}/rollback")
async def battery_patch_rollback(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    require_unprotected_device(device, action="battery_patch_rollback", requester="battery_monitor_patch", method="SSH", endpoint=BATTERY_MONITOR_PATCH["file"])
    if str(payload.get("confirmation") or "").strip() != BATTERY_ROLLBACK_CONFIRMATION:
        raise HTTPException(status_code=409, detail={"error": "confirmation phrase required", "expected": BATTERY_ROLLBACK_CONFIRMATION})
    require_memory_checked(device, payload)
    enforce_ip_write_guard(db, device)
    status = await _battery_monitor_status(device, include_http=False)
    if not status["backup_sha256"]:
        raise HTTPException(status_code=409, detail="BatteryMonitor backup missing")
    offset = BATTERY_MONITOR_PATCH["offset_decimal"]
    target = BATTERY_MONITOR_PATCH["file"]
    backup = BATTERY_MONITOR_PATCH["backup_file"]
    original = BATTERY_MONITOR_PATCH["expected_original_sha256"]
    remote = (
        "set -e; trap 'mount -o remount,ro / 2>/dev/null || true' EXIT; test -s {backup}; "
        "test \"$(sha256sum {backup} | awk '{{print $1}}')\" = \"{original}\"; "
        "mount -o remount,rw /; cp {backup} {target}; sync; mount -o remount,ro /; "
        "sha256sum {target}; dd if={target} bs=1 skip={offset} count=5 2>/dev/null | od -An -tx1 -v"
    ).format(target=target, backup=backup, original=original, offset=offset)
    output = _run_battery_ssh(device, remote, timeout=25)
    verify = await _battery_monitor_status(device, include_http=False)
    if verify["battery_monitor_sha256"] != original:
        raise HTTPException(status_code=502, detail={"error": "rollback verification failed", "verify": verify, "output": output[-1200:]})
    write_masterlog("battery_patch_rollback_complete", device_id=device.device_id, radio_ip=device.ip_address, sha256=verify["battery_monitor_sha256"])
    return {"rolled_back": True, "manual_lab_only": True, "output": output[-1200:], "status": verify}


@router.get("/media-playlists")
async def media_playlists(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(MediaPlaylist).order_by(MediaPlaylist.name).all()
    return [{"id": row.id, "name": row.name, "source_type": row.source_type, "uri": row.uri, "items": json.loads(row.items_json or "[]"), "notes": row.notes, "updated_at": row.updated_at.isoformat()} for row in rows]


@router.post("/media-playlists")
async def save_media_playlist(payload: dict, db: Session = Depends(get_db)) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="playlist name is required")
    row = db.query(MediaPlaylist).filter(MediaPlaylist.name == name).one_or_none()
    if row is None:
        row = MediaPlaylist(name=name)
        db.add(row)
    row.source_type = str(payload.get("source_type") or "DLNA")
    row.uri = str(payload.get("uri") or "")
    row.items_json = json.dumps(payload.get("items") or [])
    row.notes = str(payload.get("notes") or "")
    row.updated_at = utc_now()
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name}


@router.delete("/media-playlists")
async def clear_media_playlists(db: Session = Depends(get_db)) -> dict:
    deleted = db.query(MediaPlaylist).delete(synchronize_session=False)
    db.commit()
    return {"deleted": deleted}


@router.post("/devices/{device_id}/media/list-servers")
async def list_media_servers(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    if payload.get("dry_run", True):
        return {"dry_run": True, "device_id": device.device_id, "target": device.ip_address, "endpoint": "/listMediaServers", "capabilities": MEDIA_LIBRARY_CAPABILITIES}
    xml = await SoundTouchClient(device.ip_address).get_xml("/listMediaServers")
    row = TelemetryEvent(device_id=device.device_id, event_type="media_server_probe", endpoint="/listMediaServers", payload=xml, parsed_summary=summarize_payload(xml))
    db.add(row)
    db.commit()
    servers = []
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        for node in root.iter():
            if node is root or not (node.attrib or (node.text or "").strip()):
                continue
            server_id = node.attrib.get("id") or node.attrib.get("serverID") or node.attrib.get("uuid") or node.attrib.get("sourceAccount") or ""
            name = node.attrib.get("friendly_name") or node.attrib.get("name") or node.findtext("name", "") or (node.text or "").strip()
            if server_id or name:
                servers.append({"id": server_id, "name": name, "tag": node.tag, "attributes": dict(node.attrib)})
    except Exception:
        servers = []
    return {"dry_run": False, "device_id": device.device_id, "xml": xml, "servers": servers, "summary": row.parsed_summary, "requirements": {"server_id": "Eindeutige UUID/Source-Account-ID des DLNA-Servers", "container_id": "Objekt-ID des Ordners, Albums oder der Playlist", "item_id": "Objekt-ID des konkreten Titels", "source": "Normalerweise STORED_MUSIC, LOCAL_MUSIC oder UPNP – muss aus der Radioantwort übernommen werden", "playback": "Zuerst /selectLocalSource, dann /navigate und schließlich /playbackRequest mit Container und Track"}}


@router.post("/devices/{device_id}/backup/plan")
async def backup_plan(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    local_dir = get_settings().data_dir / "backups" / device.device_id / stamp
    include_usb = bool(payload.get("include_usb"))
    return {
        "dry_run": True,
        "device_id": device.device_id,
        "target": device.ip_address,
        "local_backup_dir": str(local_dir),
        "raspberry_default": True,
        "usb_optional": include_usb,
        "read_only_http": ["/info", "/capabilities", "/presets", "/sources", "/now_playing", "/getZone", "/supportedURLs"],
        "confirmed_active_layers": {
            "factory_sdk": "/opt/Bose/etc/SoundTouchSdkPrivateCfg.xml",
            "override_sdk": "/mnt/nv/OverrideSdkPrivateCfg.xml",
            "user_state": "/mnt/nv/BoseApp-Persistence/1",
            "legacy_nv_sdk": "/mnt/nv/SoundTouchSdkPrivateCfg.xml (absent on tested Portable/ST20; do not require)",
        },
        "levels": {
            "routing": ["/opt/Bose/etc/SoundTouchSdkPrivateCfg.xml", "/mnt/nv/OverrideSdkPrivateCfg.xml"],
            "user_state": ["/mnt/nv/BoseApp-Persistence"],
            "full_same_device": ["/opt/Bose/etc/SoundTouchSdkPrivateCfg.xml", "/mnt/nv/OverrideSdkPrivateCfg.xml", "/mnt/nv/BoseApp-Persistence", "/mnt/nv/btpm", "/mnt/nv/hosts_backup", "/mnt/nv/remote_services"],
        },
        "important_user_state": ["Presets.xml", "NetworkProfiles.xml (optional/model-state dependent)", "ClockDisplay.xml", "CurrentSource.xml", "CurrentContentItem.xml", "LastNonBluetoothContentItem.xml", "Sources.xml", "Sources.xml.bak", "Recents.xml", "LOCAL_INTERNET_RADIO..history.xml", "AudioVolume.xml", "AudioBass.xml (optional)", "CurrentDisplayLanguage.xml", "StatsDataCaptureConfig.xml", "SystemConfigurationDB.xml", "AirplayConfiguration.xml/AirPlay2_Home.xml (firmware variant)", "LightswitchDataItems.xml (optional)"],
        "ssh_read_plan": [
            "test -r /opt/Bose/etc/SoundTouchSdkPrivateCfg.xml && cat /opt/Bose/etc/SoundTouchSdkPrivateCfg.xml",
            "test -r /mnt/nv/OverrideSdkPrivateCfg.xml && cat /mnt/nv/OverrideSdkPrivateCfg.xml || true",
            "find /mnt/nv/BoseApp-Persistence/1 -maxdepth 2 -type f | sort",
            "tar czf - /opt/Bose/etc/SoundTouchSdkPrivateCfg.xml /mnt/nv/OverrideSdkPrivateCfg.xml /mnt/nv/BoseApp-Persistence /mnt/nv/btpm /mnt/nv/hosts_backup /mnt/nv/remote_services 2>/tmp/basswiesn-backup-errors.txt",
        ],
        "ssh_compatibility_options": list(LEGACY_SSH_ALGORITHM_OPTIONS),
        "network_transfer_plan": f"Stream the tar archive over SSH directly into {local_dir}; do not stage a full archive in /mnt/nv.",
        "mount_rule": "Backups are read-only and should not remount the root filesystem. Remount rw only for explicit writes, then sync and restore/verify.",
        "restore_rule": "Full restore only to the same device/model/firmware. NetworkProfiles.xml, Keys/default.pem, Spotify/AirPlay state and Bluetooth files may be device-bound. Prefer scoped restore of OverrideSdkPrivateCfg.xml or Presets.xml.",
        "sensitive_files": ["NetworkProfiles.xml", "Keys/default.pem", "SPOTIFY*.xml", "AirPlay2_Home.xml", "/mnt/nv/btpm/*"],
        "factory_reset_allowed_after_backup": False,
    }


@router.post("/devices/{device_id}/battery/probe")
async def battery_probe(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device_or_404(db, device_id)
    raise HTTPException(status_code=410, detail=_battery_removed(device_id))


@router.get("/battery/latest")
async def latest_battery_states(db: Session = Depends(get_db)) -> list[dict]:
    raise HTTPException(status_code=410, detail=_battery_removed())


@router.post("/devices/{device_id}/battery/diagnose")
async def battery_diagnose(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    require_unprotected_device(device, action="battery_diagnose", requester="battery_monitor_patch", method="SSH", endpoint=BATTERY_MONITOR_PATCH["file"])
    status = await _battery_monitor_status(device, include_http=False)
    return {"diagnosis": status, "manual_lab_only": True, "background_polling": False}


@router.get("/devices/{device_id}/battery/patch-plan")
async def battery_patch_plan(device_id: str, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    require_unprotected_device(device, action="battery_patch_plan", requester="battery_monitor_patch", method="SSH", endpoint=BATTERY_MONITOR_PATCH["file"])
    return {"device_id": device.device_id, "plan": _battery_patch_plan(device.ip_address), "manual_lab_only": True}


@router.get("/reference-setups")
async def reference_setups(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(ReferenceSetup).order_by(ReferenceSetup.name).all()
    return [{"id": row.id, "name": row.name, "source_device_id": row.source_device_id, "model_family": row.model_family, "settings": json.loads(row.settings_json or "{}"), "presets": json.loads(row.presets_json or "[]"), "notes": row.notes, "updated_at": row.updated_at.isoformat()} for row in rows]


@router.post("/reference-setups/from-device/{device_id}")
async def create_reference_setup(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    name = str(payload.get("name") or f"Reference {device.name or device.device_id}").strip()
    rows = db.query(Preset).filter(Preset.device_id == device_id).order_by(Preset.button).all()
    presets = [preset_slot_dict(row, db.query(Station).filter(Station.id == row.station_id).one_or_none() if row.station_id else None) for row in rows]
    settings_snapshot = {"name": device.name, "model": device.model, "firmware": device.firmware, "device_language_default": setting_rows(db).get("device_language_default", "en"), "timezone": setting_rows(db).get("default_timezone", "Europe/Berlin")}
    row = db.query(ReferenceSetup).filter(ReferenceSetup.name == name).one_or_none()
    if row is None:
        row = ReferenceSetup(name=name)
        db.add(row)
    row.source_device_id = device.device_id
    row.model_family = device.model
    row.settings_json = json.dumps(settings_snapshot)
    row.presets_json = json.dumps(presets)
    row.notes = str(payload.get("notes") or "")
    row.updated_at = utc_now()
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "presets": len(presets), "settings": settings_snapshot}


@router.post("/reference-setups/{setup_id}/apply/{device_id}")
async def apply_reference_setup(setup_id: int, device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    setup = db.query(ReferenceSetup).filter(ReferenceSetup.id == setup_id).one_or_none()
    if setup is None:
        raise HTTPException(status_code=404, detail="reference setup not found")
    device = device_or_404(db, device_id)
    require_memory_checked(device, payload)
    settings_snapshot = json.loads(setup.settings_json or "{}")
    presets = json.loads(setup.presets_json or "[]")
    plan = [
        {"step": "backup", "required": True, "endpoint": f"/api/devices/{device.device_id}/backup/plan"},
        {"step": "rename", "path": "/name", "value": settings_snapshot.get("name", "")},
        {"step": "language", "path": "/language", "value": settings_snapshot.get("device_language_default", "en")},
        {"step": "timezone", "path": "/clockDisplay", "value": settings_snapshot.get("timezone", "Europe/Berlin")},
        {"step": "presets", "slots": [{"button": item.get("button"), "station_name": item.get("station_name"), "source": item.get("source")} for item in presets]},
        {"step": "verify", "reads": ["/info", "/presets", "/sources", "/now_playing"]},
    ]
    return {"dry_run": True, "execution_enabled": False, "reference_setup": setup.name, "target_device_id": device.device_id, "plan": plan, "memory_check": memory_check_plan(device)}


@router.get("/system/settings")
async def system_settings(request: Request, db: Session = Depends(get_db)) -> dict:
    rows = {row.key: row.value for row in db.query(Setting).all()}
    raspberry_locale = (os.getenv("LC_ALL") or os.getenv("LC_MESSAGES") or os.getenv("LANG") or "").lower()
    browser_locale = request.headers.get("accept-language", "").lower()
    detected_language = _detected_web_language(raspberry_locale, browser_locale)
    config = get_settings()
    lan_host = rows.get("lan_host", config.lan_host)
    ui_mode = rows.get("ui_mode", "")
    if ui_mode not in {"easy", "standard", "lab"}:
        # Existing installations already persisted the old lab-mode switch;
        # retain that preference. A truly fresh database starts in Easy Mode.
        ui_mode = (
            "lab"
            if rows.get("lab_mode") == "true"
            else "standard"
            if "lab_mode" in rows
            else "easy"
        )
    return {
        "version": config.version,
        "lan_host": lan_host,
        "local_base_url": f"http://{lan_host}:{config.cloud_port}" if lan_host else config.local_base_url,
        "web_base_url": f"http://{lan_host}:{config.web_port}" if lan_host else config.web_base_url,
        "debug_base_url": f"http://{lan_host}:{config.debug_port}" if lan_host else config.debug_base_url,
        "web_language": rows.get("web_language", detected_language),
        "default_timezone": rows.get("default_timezone", "Europe/Berlin"),
        "device_language_default": rows.get("device_language_default", "en"),
        "battery_polling_removed": True,
        "display_metadata_mode": rows.get("display_metadata_mode", "station_clock"),
        "first_run_warning_required": rows.get("first_run_warning_required", "true"),
        "show_startup_warning": rows.get("show_startup_warning", "true"),
        "lab_mode": rows.get("lab_mode", "false"),
        "ui_mode": ui_mode,
        "guided_hints": rows.get("guided_hints", "true"),
        "safe_startup_volume": int(rows.get("safe_startup_volume", "30") or 30),
        "ip_write_guard": rows.get("ip_write_guard", "false"),
        "ip_write_allowed_ips": rows.get("ip_write_allowed_ips", ""),
        "protected_device_ips": rows.get("protected_device_ips", ",".join(config.protected_device_ips)),
        "effective_protected_device_ips": ",".join(sorted(protected_device_ips())),
        "protected_device_ids": rows.get("protected_device_ids", ",".join(config.protected_device_ids)),
        "effective_protected_device_ids": ",".join(sorted(protected_device_ids())),
        "update_check_enabled": rows.get("update_check_enabled", "true" if config.update_check_enabled else "false"),
        "update_channel": rows.get("update_channel", config.update_channel),
        "update_manifest_url": rows.get("update_manifest_url", config.update_manifest_url),
        "update_repo_url": rows.get("update_repo_url", config.update_repo_url),
        "offline_mode": rows.get("offline_mode", config.offline_mode),
        "offline_allowed_stream_hosts": rows.get("offline_allowed_stream_hosts", ",".join(config.offline_allowed_stream_hosts)),
        "support_latest_firmware_only": rows.get("support_latest_firmware_only", "true"),
        "latest_supported_firmware_family": rows.get("latest_supported_firmware_family", "27.0.x"),
        "languages": STOCKHOLM_LANGUAGES,
        "web_languages": WEB_LANGUAGES,
        "device_languages": STOCKHOLM_LANGUAGES,
        "timezones": TIME_ZONES,
    }


@router.post("/system/settings")
async def save_system_settings(payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    allowed = {"lan_host", "web_language", "default_timezone", "device_language_default", "display_metadata_mode", "first_run_warning_required", "show_startup_warning", "lab_mode", "ui_mode", "guided_hints", "safe_startup_volume", "ip_write_guard", "ip_write_allowed_ips", "protected_device_ips", "protected_device_ids", "support_latest_firmware_only", "latest_supported_firmware_family", "update_check_enabled", "update_channel", "update_manifest_url", "update_repo_url", "offline_mode", "offline_allowed_stream_hosts"}
    values = {key: str(payload.get(key, "")) for key in allowed if key in payload}
    if values.get("web_language") and values["web_language"] not in WEB_LANGUAGE_CODES:
        raise HTTPException(status_code=400, detail="unsupported web language")
    if values.get("default_timezone") and values["default_timezone"] not in TIME_ZONES:
        raise HTTPException(status_code=400, detail="unsupported timezone")
    if values.get("device_language_default") and values["device_language_default"] not in language_codes():
        raise HTTPException(status_code=400, detail="unsupported device language")
    if "lan_host" in values:
        host = values["lan_host"].strip()
        if host:
            try:
                ip = ipaddress.ip_address(host)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="BASSWIESN host must be a LAN IPv4 address") from exc
            from basswiesn.app.config import is_safe_radio_host
            if not is_safe_radio_host(str(ip)):
                raise HTTPException(status_code=400, detail="BASSWIESN host must be a reachable LAN IPv4 address, not localhost or Docker")
        values["lan_host"] = host
    if values.get("safe_startup_volume"):
        volume = int(values["safe_startup_volume"])
        if volume < 0 or volume > 100:
            raise HTTPException(status_code=400, detail="safe startup volume must be 0..100")
        values["safe_startup_volume"] = str(volume)
    if "ip_write_allowed_ips" in values:
        candidates = [item for item in re.split(r"[\s,;]+", values["ip_write_allowed_ips"].strip()) if item]
        try:
            values["ip_write_allowed_ips"] = ",".join(str(ipaddress.ip_address(item)) for item in candidates)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="allowed IPs must contain valid IPv4/IPv6 addresses") from exc
    if "protected_device_ips" in values:
        candidates = [item for item in re.split(r"[\s,;]+", values["protected_device_ips"].strip()) if item]
        try:
            values["protected_device_ips"] = ",".join(sorted({str(ipaddress.ip_address(item)) for item in candidates}))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="protected IPs must contain valid IPv4/IPv6 addresses") from exc
    if "protected_device_ids" in values:
        identifiers = []
        for item in re.split(r"[\s,;]+", values["protected_device_ids"].strip()):
            if not item:
                continue
            normalized = item.strip().upper()
            if not re.fullmatch(r"[A-Z0-9_.:-]{4,128}", normalized):
                raise HTTPException(status_code=400, detail="protected device IDs contain unsupported characters")
            identifiers.append(normalized)
        values["protected_device_ids"] = ",".join(sorted(set(identifiers)))
    if values.get("ui_mode") and values["ui_mode"] not in {"easy", "standard", "lab"}:
        raise HTTPException(status_code=400, detail="ui_mode must be easy, standard or lab")
    if "ui_mode" in values:
        values["lab_mode"] = "true" if values["ui_mode"] == "lab" else "false"
    elif "lab_mode" in values:
        # Backward compatibility for 1.x/2.0 clients that only knew the
        # boolean LAB switch.  A deliberate legacy toggle must not leave the
        # newly introduced Easy Mode active and hide the requested tools.
        values["ui_mode"] = "lab" if values["lab_mode"] in {"true", "on", "1", "yes"} else "standard"
    if values.get("display_metadata_mode") and values["display_metadata_mode"] not in {item["key"] for item in DISPLAY_METADATA_MODES}:
        raise HTTPException(status_code=400, detail="unsupported display metadata mode")
    if values.get("update_channel") and values["update_channel"] not in {"manual", "stable", "beta"}:
        raise HTTPException(status_code=400, detail="update channel must be manual, stable or beta")
    if values.get("offline_mode") and values["offline_mode"] not in {"off", "auto", "strict"}:
        raise HTTPException(status_code=400, detail="offline mode must be off, auto or strict")
    if "offline_allowed_stream_hosts" in values:
        hosts = []
        for item in re.split(r"[\s,;]+", values["offline_allowed_stream_hosts"].strip()):
            if not item:
                continue
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,253}", item):
                raise HTTPException(status_code=400, detail="offline allowed stream hosts must be hostnames")
            hosts.append(item.lower())
        values["offline_allowed_stream_hosts"] = ",".join(sorted(set(hosts)))
    for boolean_key in ("first_run_warning_required", "show_startup_warning", "lab_mode", "guided_hints", "ip_write_guard", "update_check_enabled"):
        if boolean_key in values:
            values[boolean_key] = "true" if values[boolean_key] in {"true", "on", "1", "yes"} else "false"
    for key, value in values.items():
        row = db.query(Setting).filter(Setting.key == key).one_or_none()
        if row is None:
            row = Setting(key=key)
            db.add(row)
        row.value = value
    db.commit()
    return await system_settings(request, db)


@router.get("/offline/status")
async def get_offline_status(db: Session = Depends(get_db)) -> dict:
    return offline_status(db)


@router.post("/offline/preflight")
async def offline_preflight(payload: dict, db: Session = Depends(get_db)) -> dict:
    station = None
    if payload.get("station_id") is not None:
        station = db.query(Station).filter(Station.id == int(payload["station_id"])).one_or_none()
        if station is None:
            raise HTTPException(status_code=404, detail="station not found")
    stream_url = str(payload.get("stream_url") or getattr(station, "stream_url", "") or "").strip()
    location = str(payload.get("location") or "").strip()
    probe_requested = bool(payload.get("probe"))
    result = build_offline_preflight(stream_url=stream_url, location=location, probe_requested=probe_requested)
    if probe_requested:
        decision = external_request_decision(
            db,
            service="offline_preflight",
            url_or_host=stream_url,
            reason="manueller Offline-Preflight",
            required=False,
            stream_target=True,
            manual_action=True,
        )
        if not decision.allowed:
            result["probe"] = {**result["probe"], "status": "blockiert", "reason": "Strict Offline Mode blockiert den externen Stream-Preflight"}
        else:
            result["probe"] = await probe_stream_reference(stream_url, allowed_hosts=allowed_stream_hosts(db))
    return result


def _update_settings(db: Session) -> dict:
    rows = {row.key: row.value for row in db.query(Setting).filter(Setting.key.in_(("update_check_enabled", "update_channel", "update_manifest_url", "update_repo_url"))).all()}
    config = get_settings()
    return {
        "enabled": rows.get("update_check_enabled", "true" if config.update_check_enabled else "false") == "true",
        "channel": rows.get("update_channel", config.update_channel),
        "manifest_url": rows.get("update_manifest_url", config.update_manifest_url),
        "repo_url": rows.get("update_repo_url", config.update_repo_url),
        "local_version": config.version or "dev",
    }


@router.get("/update/status")
async def update_status(db: Session = Depends(get_db)) -> dict:
    settings = _update_settings(db)
    status = "not_configured"
    message = "Updatequelle noch nicht eingerichtet." if not settings["manifest_url"] else "Updateprüfung bereit."
    return {**settings, "status": status, "message": message}


@router.post("/update/check")
async def run_update_check(db: Session = Depends(get_db)) -> dict:
    settings = _update_settings(db)
    decision = external_request_decision(
        db,
        service="update_check",
        url_or_host=settings["manifest_url"],
        reason="manual release manifest check",
        required=False,
        manual_action=True,
    )
    record_dependency(db, decision)
    if not decision.allowed:
        write_masterlog("update_check_blocked_by_offline_mode", mode=decision.mode, host=decision.target_host)
        return {**settings, "status": "blocked_by_offline_mode", "message": "Strict Offline Mode blockiert die externe Update-Pruefung.", "offline": decision.to_dict()}
    write_masterlog("update_check_start", channel=settings["channel"])
    if not settings["manifest_url"]:
        write_masterlog("update_check_not_configured")
        return {**settings, "status": "not_configured", "message": "Updatequelle noch nicht eingerichtet."}
    result = await check_update(settings["local_version"], settings["manifest_url"])
    event = {"up_to_date": "update_check_success", "update_available": "update_check_update_available"}.get(result["status"], "update_check_failed")
    write_masterlog(event, status=result["status"], remote_version=result.get("remote_version", ""))
    return {**settings, **result}


@router.post("/backup/create")
async def system_backup_create(db: Session = Depends(get_db)) -> dict:
    result = create_system_backup(db)
    write_masterlog("system_backup_created", path=result["path"], version=result["manifest"].get("version"))
    return result


@router.post("/backup/preview")
async def system_backup_preview(payload: dict) -> dict:
    return preview_system_backup(str(payload.get("path") or payload.get("filename") or ""))


@router.post("/backup/restore")
async def system_backup_restore(payload: dict, db: Session = Depends(get_db)) -> dict:
    if not is_yes_confirmation(payload.get("confirmation")):
        raise HTTPException(status_code=409, detail="confirmation YES required")
    result = prepare_system_restore(db, str(payload.get("path") or payload.get("filename") or ""))
    write_masterlog("system_restore_prepared", prepared=result.get("prepared", False), pending_archive=result.get("pending_archive", ""))
    return result


@router.post("/system/warnings/ack")
async def acknowledge_first_run_warning(db: Session = Depends(get_db)) -> dict:
    row = db.query(Setting).filter(Setting.key == "first_run_warning_required").one_or_none()
    if row is None:
        row = Setting(key="first_run_warning_required")
        db.add(row)
    row.value = "false"
    db.commit()
    return {"first_run_warning_required": "false"}
