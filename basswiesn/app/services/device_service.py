"""Business logic for local SoundTouch device records."""

import asyncio
from collections.abc import Callable
import ipaddress
import re
from typing import Protocol
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.config import get_settings, is_safe_radio_host
from basswiesn.app.models import Device, utc_now
from basswiesn.app.db.repositories import DeviceRepository
from basswiesn.app.services.protected_devices import is_protected_device


class DeviceInfoClient(Protocol):
    async def get_xml(self, path: str) -> str: ...


DeviceClientFactory = Callable[[str], DeviceInfoClient]


def classify_marge_url(marge_url: str) -> tuple[str, str]:
    value = (marge_url or "").strip()
    if not value:
        return "unknown", ""
    try:
        parsed = urlparse(value if "://" in value else f"http://{value}")
    except ValueError:
        return "unknown", "unparseable cloud route"
    host = (parsed.hostname or "").strip().lower()
    try:
        port = parsed.port
    except ValueError:
        return "unknown", "unparseable cloud route"
    settings = get_settings()
    local_ports = {1328, int(settings.cloud_port), 1516}
    bose_hosts = {"content.api.bose.io", "streaming.bose.com"}
    if host in bose_hosts:
        if port in local_ports:
            return "mixed", "mixed route / invalid cloud target"
        return "bose", ""
    if host == "basswiesn" and port in local_ports:
        return "basswiesn", ""
    if host and host == (settings.lan_host or "").lower() and port in local_ports:
        return "basswiesn", ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and ip.version == 4 and is_safe_radio_host(str(ip)):
        if port in {int(settings.cloud_port), 1516}:
            return "basswiesn", ""
        return "other", "LAN route points to an unexpected port"
    if "basswiesn" in value.lower() or port in {int(settings.cloud_port), 1516}:
        return "basswiesn", ""
    return "other", ""


def device_summary(device: Device) -> dict:
    """Build the stable API representation of a stored device."""

    root = None
    try:
        root = ET.fromstring(device.info_xml) if device.info_xml else None
    except ET.ParseError:
        pass
    name = root.findtext("name", "") if root is not None else ""
    model = root.findtext("type", "") if root is not None else ""
    firmware_value = root.findtext(".//softwareVersion", "") if root is not None else ""
    firmware_value = firmware_value or device.firmware
    match = re.search(r"(\d+\.\d+\.\d+)", firmware_value or "")
    firmware = match.group(1) if match else firmware_value
    marge_url = root.findtext("margeURL", "") if root is not None else ""
    configured_for, config_status = classify_marge_url(marge_url)
    name = device.name or name
    model = model or device.model
    serial_number = ""
    if root is not None:
        for component in root.findall(".//component"):
            category = component.findtext("componentCategory", "")
            serial = component.findtext("serialNumber", "").strip()
            if category == "PackagedProduct" and serial:
                serial_number = serial
                break
        if not serial_number:
            serial_number = root.findtext(".//serialNumber", "").strip()
    radio_device_id = root.attrib.get("deviceID", "") if root is not None else ""
    reachable_value = getattr(device, "reachable", True)
    reachable = True if reachable_value is None else bool(reachable_value)
    last_seen = getattr(device, "last_seen", None)
    last_failed = getattr(device, "last_failed_at", None)
    protected = is_protected_device(device)
    return {
        "device_id": device.device_id,
        "radio_device_id": radio_device_id or device.device_id,
        "serial_number": serial_number,
        "name": name,
        "model": model,
        "ip_address": device.ip_address,
        "protected": protected,
        "access_protected": protected,
        "protection_scope": "kein automatischer oder manueller Netzwerkzugriff" if protected else "",
        "protection_label": "GESCHUETZT - VOLLSTAENDIG GESPERRT" if protected else "",
        "firmware": firmware,
        "marge_url": marge_url,
        "configured_for": configured_for,
        "config_status": config_status,
        "reachable": reachable,
        "ready": bool(device.ip_address and name and model and reachable),
        "has_info": bool(device.info_xml),
        "has_capabilities": bool(device.capabilities_xml),
        "last_seen_at": last_seen.isoformat() if last_seen else "",
        "last_failed_at": last_failed.isoformat() if last_failed else "",
        "failure_count": int(getattr(device, "failure_count", 0) or 0),
        "offline_reason": getattr(device, "offline_reason", "") or "",
        "maintenance_reboot_enabled": bool(getattr(device, "maintenance_reboot_enabled", False)),
        "maintenance_reboot_interval_hours": int(getattr(device, "maintenance_reboot_interval_hours", 24) or 24),
        "maintenance_last_success_at": device.maintenance_last_success_at.isoformat() if getattr(device, "maintenance_last_success_at", None) else "",
        "maintenance_next_run_at": device.maintenance_next_run_at.isoformat() if getattr(device, "maintenance_next_run_at", None) else "",
        "maintenance_last_attempt_at": device.maintenance_last_attempt_at.isoformat() if getattr(device, "maintenance_last_attempt_at", None) else "",
        "maintenance_last_result": getattr(device, "maintenance_last_result", "") or "",
        "maintenance_phase": getattr(device, "maintenance_phase", "idle") or "idle",
        "maintenance_failure_count": int(getattr(device, "maintenance_failure_count", 0) or 0),
    }


class DeviceService:
    def __init__(
        self,
        repository: DeviceRepository,
        *,
        client_factory: DeviceClientFactory = SoundTouchClient,
    ) -> None:
        self.repository = repository
        self.client_factory = client_factory

    def list_devices(self) -> list[Device]:
        return self.repository.list_by_name()

    def upsert_local_device(
        self,
        *,
        device_id: str,
        ip_address: str,
        name: object | None = None,
        model: object | None = None,
    ) -> Device:
        """Create or update a local record without contacting the device."""

        row = self.repository.get_by_device_id(device_id)
        if row is None and ip_address:
            row = self.repository.get_latest_by_ip(ip_address)
        if row is None:
            row = self.repository.add(Device(device_id=device_id))
        if name is not None:
            row.name = str(name)
        if model is not None:
            row.model = str(model)
        if ip_address:
            row.ip_address = ip_address
        return row

    async def refresh_device(self, device: Device) -> dict[str, object]:
        """Read and apply `/info`, mapping only expected reachability failures."""

        if not device.ip_address:
            return {"ok": False, "device": device, "info_xml": "", "error": "device has no IP address"}
        if is_protected_device(device):
            return {
                "ok": False,
                "device": device,
                "info_xml": "",
                "error": "fully protected device",
                "skipped": True,
                "protected": True,
            }
        try:
            info_xml = await self.client_factory(device.ip_address).get_xml("/info")
            root = ET.fromstring(info_xml)
        except (httpx.HTTPError, ET.ParseError, OSError) as exc:
            failures = int(getattr(device, "failure_count", 0) or 0) + 1
            device.failure_count = failures
            device.last_failed_at = utc_now()
            device.offline_reason = str(exc) or exc.__class__.__name__
            if failures >= 3:
                device.reachable = False
            return {"ok": False, "device": device, "info_xml": "", "error": str(exc)}
        device.info_xml = info_xml
        device.name = root.findtext("name", "") or device.name
        device.model = root.findtext("type", "") or device.model
        device.firmware = root.findtext(".//softwareVersion", "") or device.firmware
        device.last_seen = utc_now()
        device.reachable = True
        device.failure_count = 0
        device.last_failed_at = None
        device.offline_reason = ""
        return {"ok": True, "device": device, "info_xml": info_xml, "error": None}

    async def refresh_devices(self, devices: list[Device]) -> list[dict[str, object]]:
        """Refresh multiple devices while preserving input order."""

        return await asyncio.gather(*(self.refresh_device(device) for device in devices))

    def record_discovery(
        self,
        discovered: list[dict[str, str]],
        *,
        persist: bool,
    ) -> list[dict[str, str]]:
        """Normalize discovery output and optionally persist device records."""

        found: list[dict[str, str]] = []
        for item in discovered:
            summary = self._discovery_summary(item)
            if summary is None:
                continue
            if persist:
                self._upsert_discovered(summary, info_xml=item["raw"])
            found.append(summary)
        if persist:
            self.repository.commit()
        return sorted(found, key=lambda item: item["ip_address"])

    def _discovery_summary(self, item: dict[str, str]) -> dict[str, str] | None:
        ip_address = str(item.get("ip_address") or "").strip()
        info_xml = str(item.get("raw") or "")
        if not ip_address or "<info" not in info_xml:
            return None
        try:
            root = ET.fromstring(info_xml)
        except ET.ParseError:
            return None
        device_id = str(item.get("device_id") or ip_address).strip() or ip_address
        name = str(item.get("name") or root.findtext("name", "") or ip_address)
        model = str(item.get("model") or root.findtext("type", "") or "SoundTouch")
        firmware = root.findtext(".//softwareVersion", "")
        return {
            "device_id": device_id,
            "ip_address": ip_address,
            "name": name,
            "model": model,
            "firmware": firmware,
        }

    def _upsert_discovered(self, summary: dict[str, str], *, info_xml: str) -> Device:
        row = self.repository.get_by_device_id(summary["device_id"])
        if row is None:
            row = self.repository.get_latest_by_ip(summary["ip_address"])
        if row is None:
            row = self.repository.add(Device(device_id=summary["device_id"]))
        elif row.device_id != summary["device_id"]:
            row.device_id = summary["device_id"]
        row.ip_address = summary["ip_address"]
        row.name = summary["name"]
        row.model = summary["model"]
        row.firmware = summary["firmware"]
        row.info_xml = info_xml
        row.last_seen = utc_now()
        row.reachable = True
        row.failure_count = 0
        row.last_failed_at = None
        row.offline_reason = ""
        return row
