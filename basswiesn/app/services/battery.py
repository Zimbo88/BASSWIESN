import re
import xml.etree.ElementTree as ET


def _bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() in {"true", "1", "yes", "on"}


def _number(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text, re.IGNORECASE)
    return float(match.group(1)) if match else None


def battery_bucket(percent: int | None) -> int | None:
    if percent is None:
        return None
    for upper in (20, 40, 60, 75, 100):
        if percent <= upper:
            return upper
    return 100


def parse_power_management(xml_text: str) -> dict:
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError:
        return {"parse_error": "invalid powerManagement XML", "raw": xml_text}
    battery = root.find(".//battery")
    percent_text = battery.findtext("percentCharge", "") if battery is not None else ""
    try:
        percent = int(float(percent_text)) if percent_text else None
    except ValueError:
        percent = None
    return {
        "power_state": root.findtext("powerState", ""),
        "capable": _bool(battery.findtext("capable") if battery is not None else None),
        "present": _bool(battery.findtext("present") if battery is not None else None),
        "running_on_battery": _bool(battery.findtext("runningOnBattery") if battery is not None else None),
        "percent_charge": percent,
        "level_bucket": battery_bucket(percent),
    }


def parse_battery_cli(text: str) -> dict:
    relative = _number(r"relative state of charge:\s*([+-]?[0-9.]+)%", text)
    build = re.search(r"Battery build date:\s*raw=([^,\r\n]+),\s*([^\r\n]+)", text, re.IGNORECASE)
    serial = re.search(r"Battery serial number:\s*([^\r\n]+)", text, re.IGNORECASE)
    version = re.search(r"Battery version:\s*([^\r\n]+)", text, re.IGNORECASE)
    status = re.search(r"Battery status is\s*:\s*([^\r\n]+)", text, re.IGNORECASE)
    fault = re.search(r"Battery Fault Code:\s*([^\s\r\n]+)", text, re.IGNORECASE)
    charger = re.search(r"Battery charger:\s*(on|off)", text, re.IGNORECASE)
    manufacturer = re.search(r"Manufacturer name:\s*([^\r\n]+)", text, re.IGNORECASE)
    dc_present = re.search(r"DC present;\s*(true|false)", text, re.IGNORECASE)
    percent = int(round(relative)) if relative is not None else None
    return {
        "dc_present": _bool(dc_present.group(1)) if dc_present else None,
        "voltage_v": _number(r"Battery voltage:\s*([+-]?[0-9.]+)\s*V", text),
        "temperature_c": _number(r"Battery temperature:\s*([+-]?[0-9.]+)\s*deg\.\s*C", text),
        "current_a": _number(r"Battery current:\s*([+-]?[0-9.]+)\s*A", text),
        "status_raw": status.group(1).strip() if status else "",
        "build_date_raw": build.group(1).strip() if build else "",
        "build_date": build.group(2).strip() if build else "",
        "serial": serial.group(1).strip() if serial else "",
        "version": version.group(1).strip() if version else "",
        "relative_state_of_charge": percent,
        "relative_state_of_charge_exact": relative,
        "level_bucket": battery_bucket(percent),
        "fault_code": fault.group(1) if fault else "",
        "charger_on": charger.group(1).lower() == "on" if charger else None,
        "manufacturer": manufacturer.group(1).strip() if manufacturer else "",
    }


def battery_state(power_xml: str, cli_text: str = "") -> dict:
    http = parse_power_management(power_xml)
    cli = parse_battery_cli(cli_text) if cli_text else {}
    cli_percent = cli.get("relative_state_of_charge")
    http_percent = http.get("percent_charge")
    effective_percent = cli_percent if cli_percent is not None else http_percent
    charger_on = cli.get("charger_on")
    running = http.get("running_on_battery")
    charging = bool(charger_on and running is False and effective_percent is not None and effective_percent < 100)
    return {
        **http,
        "percent_charge": effective_percent,
        "level_bucket": battery_bucket(effective_percent),
        "charging": charging,
        "source_of_truth": "cli17000.ba8" if cli_percent is not None else "http.powerManagement",
        "http": http,
        "cli17000": cli,
        "percent_sources_match": cli_percent is None or http_percent is None or cli_percent == http_percent,
        "safety": {"excluded_command": "ba s", "reason": "ship/storage-mode command; never run automatically"},
    }


KNOWN_BATTERY_PROFILES = {
    "BOSE_A": "factory Bose profile",
    "SANYO": "known compatible OEM profile",
    "ICC": "known compatible replacement-family profile",
}


def portable_battery_diagnosis(device_model: str, power_xml: str = "", cli_text: str = "", monitor_sha256: str = "", monitor_bytes_hex: str = "", backup_sha256: str = "") -> dict:
    state = battery_state(power_xml, cli_text)
    manufacturer = (state.get("cli17000") or {}).get("manufacturer", "").strip().upper()
    profile_known = manufacturer in KNOWN_BATTERY_PROFILES
    supported = "portable" in (device_model or "").lower()
    present = state.get("present")
    if present is None:
        present = state.get("percent_charge") is not None or bool(manufacturer)
    patch_status = "unknown"
    if monitor_sha256:
        patch_status = "unpatched"
    if monitor_sha256 in {
        "4abbb803a20323bf2938e686aa43fe969495e508f4fefe9c6099702f1e7e4e71",
        "25ed53ef0bb3a8647d6f858ccc4be20dc6292f988acb29059416d5c0591229c5",
    } or monitor_bytes_hex.lower().strip() in {"41 00 00 00 00", "4100000000"}:
        patch_status = "patched"
    fix_recommended = bool(supported and present and not profile_known and patch_status != "patched")
    return {
        "portable_model": device_model or "",
        "supported_portable": supported,
        "battery_detected": bool(present),
        "battery_id": manufacturer,
        "battery_profile": KNOWN_BATTERY_PROFILES.get(manufacturer, "unknown"),
        "battery_profile_known": profile_known,
        "battery_values": state,
        "battery_monitor_sha256": monitor_sha256,
        "battery_monitor_bytes": monitor_bytes_hex,
        "patch_status": patch_status,
        "backup_status": "present" if backup_sha256 else "missing",
        "backup_sha256": backup_sha256,
        "fix_recommended": fix_recommended,
        "without_fix": [
            "Drittanbieter-/unbekannte Batterien können nicht korrekt erkannt werden.",
            "Ladezustand kann falsch sein.",
            "Gerät kann Lade-/Akkuverhalten falsch anzeigen.",
            "Gerät kann trotz eingebautem Akku wie ohne gültige Batterie wirken.",
        ],
        "with_fix": [
            "BatteryMonitor akzeptiert bekannte kompatible Ersatzprofile.",
            "Batterieerkennung wird stabiler.",
            "Anzeige/Ladeverhalten kann sich normalisieren.",
            "Patch ist reversibel, wenn Backup vorhanden ist.",
        ],
        "risk_notes": [
            "Nur SoundTouch Portable mit unterstütztem BatteryMonitor-Checksum patchen.",
            "Backup und Byte-Verify sind Pflicht.",
            "Rollback ist nur mit gültigem Backup möglich.",
        ],
    }
