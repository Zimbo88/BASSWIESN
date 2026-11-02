"""Business rules for reconciling local and radio device identities."""

from xml.etree import ElementTree as ET

from basswiesn.app.models import Device
from basswiesn.app.db.repositories import DeviceIdentityRepository


class DeviceIdentityService:
    def __init__(self, repository: DeviceIdentityRepository) -> None:
        self.repository = repository

    def reconcile(self, device: Device, info_xml: str) -> tuple[Device, dict]:
        try:
            root = ET.fromstring(info_xml or "")
        except ET.ParseError:
            root = None
        radio_id = root.attrib.get("deviceID", "").strip() if root is not None else ""
        if not radio_id or radio_id == device.device_id:
            return device, {"merged": False, "canonical_id": device.device_id}
        canonical = self.repository.get_by_device_id(radio_id)
        if canonical is not None:
            result = self.repository.merge(device, canonical)
            return canonical, {**result, "canonical_id": radio_id}
        old_id = device.device_id
        device.device_id = radio_id
        return device, {
            "merged": False,
            "migrated": True,
            "old_device_id": old_id,
            "canonical_id": radio_id,
        }
