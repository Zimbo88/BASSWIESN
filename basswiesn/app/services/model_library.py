from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from basswiesn.app.models import Device, DeviceCapabilityOverride, DeviceModelDefinition


CAPABILITY_KEYS = {
    "battery_supported",
    "display_supported",
    "ethernet_supported",
    "wifi_supported",
    "bluetooth_supported",
    "aux_supported",
    "usb_supported",
    "preset_buttons_supported",
    "preset_write_supported",
    "multiroom_supported",
    "power_key_supported",
    "standby_wakeup_supported",
    "source_query_supported",
    "service_availability_supported",
    "now_playing_supported",
    "volume_supported",
    "zone_supported",
    "local_media_supported",
    "dlna_supported",
    "safe_auto_power",
    "safe_auto_preset_recovery",
    "safe_background_polling",
    "telnet_reboot_supported",
    "standby_clock_recovery_supported",
    "battery_feature_removed",
}


SAFE_UNKNOWN_CAPABILITIES: dict[str, Any] = {
    "battery_supported": None,
    "display_supported": None,
    "ethernet_supported": None,
    "wifi_supported": None,
    "bluetooth_supported": None,
    "aux_supported": None,
    "usb_supported": None,
    "preset_buttons_supported": None,
    "preset_write_supported": None,
    "multiroom_supported": None,
    "power_key_supported": False,
    "standby_wakeup_supported": False,
    "source_query_supported": None,
    "service_availability_supported": None,
    "now_playing_supported": None,
    "volume_supported": None,
    "zone_supported": None,
    "local_media_supported": None,
    "dlna_supported": None,
    "safe_auto_power": False,
    "safe_auto_preset_recovery": False,
    "safe_background_polling": False,
    "telnet_reboot_supported": False,
    "standby_clock_recovery_supported": False,
    "battery_feature_removed": True,
}


@dataclass(frozen=True)
class ResolvedModel:
    model_key: str
    product_name: str
    model_family: str
    device_class: str
    recommended_polling_profile: str
    capabilities: dict[str, Any]
    source: str
    confidence: int
    overrides: dict[str, Any]
    known_limitations: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_key": self.model_key,
            "product_name": self.product_name,
            "model_family": self.model_family,
            "device_class": self.device_class,
            "recommended_polling_profile": self.recommended_polling_profile,
            "capabilities": self.capabilities,
            "source": self.source,
            "confidence": self.confidence,
            "overrides": self.overrides,
            "known_limitations": self.known_limitations,
        }


def normalize_model_name(value: str) -> str:
    text = " ".join((value or "").strip().lower().split())
    text = text.replace("sound touch", "soundtouch")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def combined_device_text(device: Device) -> str:
    return normalize_model_name(" ".join(
        part for part in (
            getattr(device, "model", ""),
            getattr(device, "name", ""),
            getattr(device, "info_xml", ""),
            getattr(device, "capabilities_xml", ""),
        )
        if part
    ))


def _loads_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _boolish(value: str) -> Any:
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on", "supported"}:
        return True
    if text in {"false", "0", "no", "off", "unsupported"}:
        return False
    if text in {"", "none", "null", "unknown", "auto"}:
        return None
    return value


def _definition_to_resolved(definition: DeviceModelDefinition, source: str, confidence: int) -> ResolvedModel:
    capabilities = dict(SAFE_UNKNOWN_CAPABILITIES)
    capabilities.update(_loads_json(definition.capabilities_json, {}))
    return ResolvedModel(
        model_key=definition.model_key,
        product_name=definition.product_name,
        model_family=definition.model_family,
        device_class=definition.device_class or "unknown",
        recommended_polling_profile=definition.recommended_polling_profile or "standby",
        capabilities=capabilities,
        source=source,
        confidence=confidence,
        overrides={},
        known_limitations=definition.known_limitations or "",
    )


def model_definitions(db: Session) -> list[dict[str, Any]]:
    rows = db.query(DeviceModelDefinition).order_by(DeviceModelDefinition.product_name).all()
    return [
        {
            "model_key": row.model_key,
            "product_name": row.product_name,
            "model_family": row.model_family,
            "device_class": row.device_class,
            "aliases": _loads_json(row.aliases_json, []),
            "capabilities": _loads_json(row.capabilities_json, {}),
            "known_limitations": row.known_limitations,
            "recommended_polling_profile": row.recommended_polling_profile,
        }
        for row in rows
    ]


def _match_definition(device: Device, db: Session) -> ResolvedModel:
    text = combined_device_text(device)
    rows = db.query(DeviceModelDefinition).order_by(DeviceModelDefinition.product_name).all()
    best: tuple[int, DeviceModelDefinition] | None = None
    for row in rows:
        aliases = [normalize_model_name(item) for item in _loads_json(row.aliases_json, [])]
        names = [normalize_model_name(row.product_name), normalize_model_name(row.model_family), row.model_key.replace("_", " ")]
        for alias in [*aliases, *names]:
            if alias and alias in text:
                score = 95 if alias == normalize_model_name(getattr(device, "model", "")) else 85
                if best is None or score > best[0]:
                    best = (score, row)
    if best is not None:
        return _definition_to_resolved(best[1], "model_library", best[0])
    fallback = db.query(DeviceModelDefinition).filter(DeviceModelDefinition.model_key == "unknown_soundtouch").one_or_none()
    if fallback is not None:
        return _definition_to_resolved(fallback, "unknown_fallback", 20)
    return ResolvedModel(
        model_key="unknown_soundtouch",
        product_name="Unbekanntes SoundTouch-Gerät",
        model_family="unknown",
        device_class="unknown",
        recommended_polling_profile="standby",
        capabilities=dict(SAFE_UNKNOWN_CAPABILITIES),
        source="built_in_unknown_fallback",
        confidence=10,
        overrides={},
        known_limitations="Unknown models use safe defaults until capabilities are confirmed or overridden.",
    )


def resolve_device_model(device: Device, db: Session | None = None) -> ResolvedModel:
    if db is None:
        capabilities = dict(SAFE_UNKNOWN_CAPABILITIES)
        text = combined_device_text(device)
        if "portable" in text:
            capabilities.update({
                "battery_supported": False,
                "display_supported": True,
                "preset_buttons_supported": True,
                "preset_write_supported": True,
                "multiroom_supported": True,
                "power_key_supported": True,
                "standby_wakeup_supported": True,
                "safe_auto_power": True,
                "safe_auto_preset_recovery": True,
                "safe_background_polling": True,
                "battery_feature_removed": True,
            })
            return ResolvedModel("soundtouch_portable", "SoundTouch Portable", "SoundTouch Portable", "portable", "idle_online", capabilities, "heuristic", 70, {})
        if "soundtouch" in text:
            capabilities.update({"safe_auto_power": False, "safe_auto_preset_recovery": False, "safe_background_polling": False})
        return ResolvedModel("unknown_soundtouch", "Unbekanntes SoundTouch-Gerät", "unknown", "unknown", "standby", capabilities, "unknown_fallback", 20, {})

    resolved = _match_definition(device, db)
    overrides: dict[str, Any] = {}
    for row in db.query(DeviceCapabilityOverride).filter(DeviceCapabilityOverride.device_id == device.device_id).all():
        if row.capability_key not in CAPABILITY_KEYS:
            continue
        value = _boolish(row.override_value)
        resolved.capabilities[row.capability_key] = value
        overrides[row.capability_key] = value
    return ResolvedModel(
        model_key=resolved.model_key,
        product_name=resolved.product_name,
        model_family=resolved.model_family,
        device_class=resolved.device_class,
        recommended_polling_profile=resolved.recommended_polling_profile,
        capabilities=resolved.capabilities,
        source=resolved.source,
        confidence=resolved.confidence,
        overrides=overrides,
        known_limitations=resolved.known_limitations,
    )


def set_capability_override(db: Session, device_id: str, capability_key: str, value: str, reason: str = "") -> dict[str, Any]:
    if capability_key not in CAPABILITY_KEYS:
        raise ValueError(f"Unsupported capability override: {capability_key}")
    row = db.query(DeviceCapabilityOverride).filter(
        DeviceCapabilityOverride.device_id == device_id,
        DeviceCapabilityOverride.capability_key == capability_key,
    ).one_or_none()
    if row is None:
        row = DeviceCapabilityOverride(device_id=device_id, capability_key=capability_key)
        db.add(row)
    row.override_value = str(value)
    row.reason = reason
    db.flush()
    return {"device_id": device_id, "capability_key": capability_key, "override_value": _boolish(value), "reason": reason}


def reset_capability_override(db: Session, device_id: str, capability_key: str) -> bool:
    row = db.query(DeviceCapabilityOverride).filter(
        DeviceCapabilityOverride.device_id == device_id,
        DeviceCapabilityOverride.capability_key == capability_key,
    ).one_or_none()
    if row is None:
        return False
    db.delete(row)
    return True
