import ipaddress
import re

from fastapi import HTTPException
from sqlalchemy.orm import Session

from basswiesn.app.models import Device, Preset, Setting, Station
from basswiesn.app.config import get_settings
from basswiesn.app.services.catalogs import STOCKHOLM_LANGUAGES
from basswiesn.app.services.protected_devices import require_unprotected_device


def enforce_ip_write_guard(db: Session, device: Device) -> None:
    require_unprotected_device(device, action="radio_write", requester="ip_write_guard")
    rows = setting_rows(db)
    if rows.get("ip_write_guard", "false").lower() not in {"true", "1", "yes", "on"}:
        return
    configured = re.split(r"[\s,;]+", rows.get("ip_write_allowed_ips", "").strip())
    allowed = {value for value in configured if value}
    allowed.update(get_settings().setup_write_radio_ips)
    allowed = {value for value in allowed if _valid_ip(value)}
    if device.ip_address not in allowed:
        raise HTTPException(status_code=403, detail=f"IP Write Guard blockiert Schreibzugriff auf {device.ip_address}. In Einstellungen freigeben oder Guard deaktivieren.")


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def memory_check_plan(device: Device) -> dict:
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


def require_memory_checked(device: Device, payload: dict) -> None:
    if payload.get("dry_run", True):
        return
    if not payload.get("memory_checked"):
        raise HTTPException(status_code=409, detail={"error": "memory check required before radio write", "memory_check": memory_check_plan(device)})


def language_codes() -> set[str]:
    return {item["code"] for item in STOCKHOLM_LANGUAGES}


def summarize_payload(payload: str) -> str:
    text = re.sub(r"\s+", " ", payload or "").strip()
    return text[:240]


def preset_slot_dict(row: Preset, station: Station | None = None) -> dict:
    return {
        "button": row.button,
        "station_id": row.station_id,
        "station_name": station.name if station else "",
        "stream_url": station.stream_url if station else "",
        "image_url": station.image_url if station else "",
        "source": row.source,
        "location": row.location,
        "content_item_xml": row.content_item_xml,
    }


def device_or_404(db: Session, device_id: str) -> Device:
    device = db.query(Device).filter(Device.device_id == device_id).one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    return device


def setting_rows(db: Session) -> dict[str, str]:
    return {row.key: row.value for row in db.query(Setting).all()}


def xml_text(xml: str, path: str) -> str:
    match = re.search(rf"<{path}[^>]*>(.*?)</{path}>", xml or "", flags=re.DOTALL)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def battery_percent(xml: str) -> str:
    match = re.search(r'percentCharge="([^"<]+)"', xml or "")
    if match:
        return match.group(1)
    return xml_text(xml or "", "percentCharge")
