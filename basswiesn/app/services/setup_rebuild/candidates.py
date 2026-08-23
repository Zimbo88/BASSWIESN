"""Read-only setup candidates derived from the local device database.

The normal setup UI must never expose a development-time hardware list.  A
device becomes a setup candidate only after BASSWIESN has a local record with
an exact IP/device-id mapping.  Protected devices are removed before the
result leaves the server.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from typing import Any
from xml.etree import ElementTree as ET

from sqlalchemy.orm import Session

from basswiesn.app.models import Device
from basswiesn.app.config import get_settings
from basswiesn.app.services.protected_devices import is_protected_device
from basswiesn.app.services.setup_rebuild.profiles import DeviceFacts, detect_profile
from basswiesn.app.services.setup_rebuild.audio_safety import load_audio_safety


def _literal_lan_ipv4(value: object) -> str:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return ""
    if (
        address.version != 4
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or not (address.is_private or address in ipaddress.ip_network("192.0.2.0/24"))
    ):
        return ""
    return str(address)


def _identity_matches(row: Device) -> bool:
    if str(getattr(row, "discovery_method", "") or "") == "setup_ssdp_info_failed":
        return False
    if bool(getattr(row, "identity_verified", False)):
        return True
    try:
        root = ET.fromstring(str(getattr(row, "info_xml", "") or ""))
    except ET.ParseError:
        return False
    return root.attrib.get("deviceID", "").strip().upper() == str(row.device_id or "").strip().upper()


def _info_facts(row: Device) -> tuple[str, str, str]:
    try:
        root = ET.fromstring(str(getattr(row, "info_xml", "") or ""))
    except ET.ParseError:
        return "", "", ""
    product_id = next(
        (
            str(root.findtext(tag, "") or "").strip()
            for tag in ("productID", "productId", "product_id", "productCode")
            if str(root.findtext(tag, "") or "").strip()
        ),
        "",
    )
    return (
        root.findtext("variant", "").strip(),
        root.findtext("moduleType", "").strip(),
        product_id,
    )


@dataclass(frozen=True)
class SetupCandidate:
    device_id: str
    name: str
    ip_address: str
    model: str
    firmware: str
    product_id: str
    product_id_provenance: str
    variant: str
    platform: str
    identity_verified: bool
    profile_key: str
    profile_confidence: str
    eligible: bool
    blocking_reason: str
    audio_safety_locked: bool = False
    audio_safety_reason: str = ""
    audio_safety_source: str = "none"
    simulated: bool = False
    reachable: bool = False
    last_seen_at: str = ""
    discovery_method: str = "unknown"

    def public_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "ip_address": self.ip_address,
            "model": self.model,
            "firmware": self.firmware,
            "product_id": self.product_id,
            "product_id_provenance": self.product_id_provenance,
            "variant": self.variant,
            "platform": self.platform,
            "identity_verified": self.identity_verified,
            "profile_key": self.profile_key,
            "profile_confidence": self.profile_confidence,
            "eligible": self.eligible,
            "blocking_reason": self.blocking_reason,
            "protected": False,
            "setup_state": "UNKNOWN",
            "transport_path": ["HTTP 8090", "CLI 17000"],
            "ssh_required": False,
            "audio_safety_locked": self.audio_safety_locked,
            "audio_safety_reason": self.audio_safety_reason,
            "audio_safety_source": self.audio_safety_source,
            "simulated": self.simulated,
            "reachable": self.reachable,
            "last_seen_at": self.last_seen_at,
            "discovery_method": self.discovery_method,
        }


def candidate_from_device(row: Device, db: Session | None = None) -> SetupCandidate | None:
    """Build one candidate without opening a socket or performing discovery."""

    if is_protected_device(row):
        return None
    device_id = str(row.device_id or "").strip().upper()
    simulated = device_id == "BASSWIESN-SIM-160"
    if simulated and not get_settings().test_mode:
        return None
    ip_address = _literal_lan_ipv4(row.ip_address)
    model = str(row.model or "").strip()
    firmware = str(row.firmware or "").strip()
    variant, platform, observed_product_id = _info_facts(row)
    identity_verified = _identity_matches(row)
    match = detect_profile(
        DeviceFacts(
            device_id,
            ip_address,
            model,
            firmware,
            product_id=observed_product_id,
            variant=variant,
            platform=platform,
        )
    ) if model else None
    profile = match.profile if match is not None and firmware else None
    audio_safety = load_audio_safety(db, device_id) if db is not None else None

    reasons: list[str] = []
    if not device_id or device_id.startswith("UNVERIFIED-"):
        reasons.append("Geräte-ID wurde noch nicht am Radio bestätigt")
    if not ip_address:
        reasons.append("keine geeignete LAN-IP gespeichert")
    if not identity_verified:
        reasons.append("Identität muss über die Radio-Informationen bestätigt werden")
    if not bool(row.reachable):
        reasons.append("Radio ist nach der letzten Prüfung nicht erreichbar")
    if not firmware:
        reasons.append("Firmwareversion fehlt")
    if profile is None:
        reasons.append(match.reason if match is not None else "Gerätemodell ist keinem bestätigten Profil zugeordnet")

    return SetupCandidate(
        device_id=device_id,
        name=str(row.name or device_id or ip_address or "Unbenanntes Radio"),
        ip_address=ip_address,
        model=model,
        firmware=firmware,
        product_id=match.product_id if match is not None else "",
        product_id_provenance="RADIO_INFO" if observed_product_id else "PROFILE_DERIVED" if match is not None and match.product_id else "UNKNOWN",
        variant=variant,
        platform=platform,
        identity_verified=identity_verified,
        profile_key=profile.key if profile is not None else "",
        profile_confidence=match.confidence if match is not None else "none",
        eligible=not reasons,
        blocking_reason="; ".join(dict.fromkeys(reasons)),
        audio_safety_locked=bool(audio_safety.locked) if audio_safety is not None else False,
        audio_safety_reason=audio_safety.reason if audio_safety is not None else "",
        audio_safety_source=audio_safety.source if audio_safety is not None else "none",
        simulated=simulated,
        reachable=bool(row.reachable),
        last_seen_at=row.last_seen.isoformat() if row.last_seen is not None else "",
        discovery_method=str(row.discovery_method or "unknown"),
    )


def setup_candidates(db: Session) -> list[SetupCandidate]:
    result = []
    for row in db.query(Device).order_by(Device.name, Device.device_id).all():
        candidate = candidate_from_device(row, db)
        if candidate is not None:
            result.append(candidate)
    return result


def selected_setup_devices(
    db: Session,
    device_ids: list[str],
    *,
    require_eligible: bool,
) -> list[tuple[Device, SetupCandidate]]:
    normalized = [str(item or "").strip().upper() for item in device_ids]
    if not normalized or len(normalized) != len(set(normalized)):
        raise ValueError("Bitte genau einmal mindestens ein Radio auswählen.")
    rows = {
        str(row.device_id or "").strip().upper(): row
        for row in db.query(Device).filter(Device.device_id.in_(normalized)).all()
    }
    if len(rows) != len(normalized):
        raise ValueError("Mindestens ein ausgewähltes Radio ist nicht mehr in BASSWIESN vorhanden.")
    selected: list[tuple[Device, SetupCandidate]] = []
    for device_id in normalized:
        row = rows[device_id]
        candidate = candidate_from_device(row, db)
        if candidate is None:
            raise ValueError("Ein vollständig geschütztes Radio darf nicht im Setup verwendet werden.")
        if require_eligible and not candidate.eligible:
            raise ValueError(f"{candidate.name}: {candidate.blocking_reason}")
        selected.append((row, candidate))
    return selected
