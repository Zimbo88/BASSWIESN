"""Read-only AirPlay 2 readiness projection from firmware/runtime evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import re
from typing import Any


AIRPLAY_AUTOMATIC_PRODUCT_IDS_24 = frozenset(
    {"0X0939", "0X093A", "0X093B", "0X093C", "0X093D", "0X0948", "0X0949"}
)
AIRPLAY_AUTOMATIC_PRODUCT_IDS_25_PLUS = AIRPLAY_AUTOMATIC_PRODUCT_IDS_24 | {"0X094A"}
CONFIRMED_AIRPLAY_ALLOWLIST_MAJORS = frozenset({24, 25, 26, 27})
LEGACY_SCM_PRODUCT_IDS = frozenset({"0X0923", "0X0924", "0X0925"})


class AirPlayBlockingStage(StrEnum):
    PRODUCT_ID = "PRODUCT_ID"
    VARIANT = "VARIANT"
    AUTH_HARDWARE = "AUTH_HARDWARE"
    STS = "STS"
    SOURCE = "SOURCE"
    MDNS = "MDNS"
    PAIRING = "PAIRING"
    PTP = "PTP"
    AUDIO = "AUDIO"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"


class AirPlayReadinessLabel(StrEnum):
    READY = "BEREIT"
    PARTIAL = "TEILWEISE_BEREIT"
    UNSUPPORTED = "NICHT_UNTERSTUETZT"
    BLOCKED = "BLOCKIERT"
    UNKNOWN = "UNBEKANNT"


@dataclass(frozen=True)
class AirPlayReadiness:
    firmware_version: str | None
    product_id: str | None
    variant: str | None
    platform: str | None
    product_allowed: bool | None
    auth_hardware_expected: bool | None
    auth_hardware_detected: bool | None
    sts_registered: bool | None
    source_visible: bool | None
    mdns_visible: bool | None
    pairing_ready: bool | None
    ptp_ready: bool | None
    audio_ready: bool | None
    blocking_stage: AirPlayBlockingStage
    confidence: int
    label: AirPlayReadinessLabel
    evidence: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["blocking_stage"] = self.blocking_stage.value
        value["label"] = self.label.value
        value["evidence"] = list(self.evidence)
        return value

    @property
    def user_visible_status(self) -> str:
        labels = {
            AirPlayReadinessLabel.READY: "Bereit",
            AirPlayReadinessLabel.PARTIAL: "Teilweise bereit",
            AirPlayReadinessLabel.UNSUPPORTED: "Nicht unterstützt",
            AirPlayReadinessLabel.BLOCKED: f"Blockiert bei: {self.blocking_stage.value}",
            AirPlayReadinessLabel.UNKNOWN: "Unbekannt",
        }
        return labels[self.label]


def normalize_product_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    try:
        # Firmware/UI evidence is commonly rendered as either ``0x093B`` or
        # a zero-padded bare hex value such as ``0939``.  Numeric Python input
        # and non-padded digit strings retain their decimal interpretation.
        base = (
            16
            if text.startswith("0X")
            or any(char in "ABCDEF" for char in text)
            or (len(text) == 4 and text.startswith("0"))
            else 10
        )
        number = int(text, base)
    except ValueError:
        return None
    if number < 0 or number > 0xFFFF:
        return None
    return f"0X{number:04X}"


def _firmware_major(value: str | None) -> int | None:
    if not value:
        return None
    match = re.match(r"\s*(\d+)", value)
    return int(match.group(1)) if match else None


def product_allowed_for_firmware(
    firmware_version: str | None, product_id: str | None
) -> bool | None:
    product = normalize_product_id(product_id)
    major = _firmware_major(firmware_version)
    if product is None or major is None:
        return None
    if major not in CONFIRMED_AIRPLAY_ALLOWLIST_MAJORS:
        # Do not project the last researched policy onto future firmware.
        return None
    if major in {25, 26, 27}:
        return product in AIRPLAY_AUTOMATIC_PRODUCT_IDS_25_PLUS
    if major == 24:
        return product in AIRPLAY_AUTOMATIC_PRODUCT_IDS_24
    # Earlier marker-based paths are not equivalent to automatic product
    # allowance and require runtime evidence.
    return None


def platform_for_product(product_id: str | None, variant: str | None = None) -> str | None:
    product = normalize_product_id(product_id)
    if product in LEGACY_SCM_PRODUCT_IDS:
        return "SCM"
    if product in AIRPLAY_AUTOMATIC_PRODUCT_IDS_25_PLUS:
        return "SM2"
    text = (variant or "").upper()
    if "SCM" in text:
        return "SCM"
    if "SM2" in text:
        return "SM2"
    return None


def assess_airplay_readiness(
    *,
    firmware_version: str | None,
    product_id: str | None,
    variant: str | None = None,
    platform: str | None = None,
    auth_hardware_detected: bool | None = None,
    sts_registered: bool | None = None,
    source_visible: bool | None = None,
    mdns_visible: bool | None = None,
    pairing_ready: bool | None = None,
    ptp_ready: bool | None = None,
    audio_ready: bool | None = None,
    evidence: tuple[dict[str, Any], ...] = (),
) -> AirPlayReadiness:
    product = normalize_product_id(product_id)
    resolved_platform = (platform or platform_for_product(product, variant) or "").upper() or None
    allowed = product_allowed_for_firmware(firmware_version, product)
    auth_expected = True if resolved_platform == "SM2" and allowed is True else False if resolved_platform == "SCM" else None

    common = dict(
        firmware_version=firmware_version,
        product_id=product,
        variant=variant,
        platform=resolved_platform,
        product_allowed=allowed,
        auth_hardware_expected=auth_expected,
        auth_hardware_detected=auth_hardware_detected,
        sts_registered=sts_registered,
        source_visible=source_visible,
        mdns_visible=mdns_visible,
        pairing_ready=pairing_ready,
        ptp_ready=ptp_ready,
        audio_ready=audio_ready,
        evidence=evidence,
    )
    if allowed is False:
        return AirPlayReadiness(
            blocking_stage=AirPlayBlockingStage.PRODUCT_ID,
            confidence=99,
            label=AirPlayReadinessLabel.UNSUPPORTED,
            **common,
        )
    if allowed is None:
        return AirPlayReadiness(
            blocking_stage=AirPlayBlockingStage.PRODUCT_ID if product else AirPlayBlockingStage.UNKNOWN,
            confidence=25 if product else 0,
            label=AirPlayReadinessLabel.UNKNOWN,
            **common,
        )
    if resolved_platform is None:
        return AirPlayReadiness(
            blocking_stage=AirPlayBlockingStage.VARIANT,
            confidence=55,
            label=AirPlayReadinessLabel.PARTIAL,
            **common,
        )

    stages = (
        (AirPlayBlockingStage.AUTH_HARDWARE, auth_hardware_detected),
        (AirPlayBlockingStage.STS, sts_registered),
        (AirPlayBlockingStage.SOURCE, source_visible),
        (AirPlayBlockingStage.MDNS, mdns_visible),
        (AirPlayBlockingStage.PAIRING, pairing_ready),
        (AirPlayBlockingStage.PTP, ptp_ready),
        (AirPlayBlockingStage.AUDIO, audio_ready),
    )
    for stage, value in stages:
        if value is False:
            return AirPlayReadiness(
                blocking_stage=stage,
                confidence=90,
                label=AirPlayReadinessLabel.BLOCKED,
                **common,
            )
        if value is None:
            return AirPlayReadiness(
                blocking_stage=stage,
                confidence=60,
                label=AirPlayReadinessLabel.PARTIAL,
                **common,
            )
    return AirPlayReadiness(
        blocking_stage=AirPlayBlockingStage.NONE,
        confidence=95,
        label=AirPlayReadinessLabel.READY,
        **common,
    )
