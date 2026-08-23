"""Model-/firmware-aware SSH profile detection."""

from __future__ import annotations

from dataclasses import dataclass
from .base import DeviceFacts, SshProfile
from .portable import PORTABLE_PROFILE
from .stationary import STATIONARY_PROFILES


@dataclass(frozen=True)
class ProfileMatch:
    profile: SshProfile | None
    reason: str
    confidence: str
    product_id: str = ""

    def public_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile.public_dict() if self.profile else None,
            "reason": self.reason,
            "confidence": self.confidence,
            "product_id": self.product_id,
        }


def _normalized(value: str) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").split())


def _firmware_build(value: str) -> str:
    parts = str(value or "").strip().split()
    return parts[0] if parts else ""


def _product_id(value: str) -> str:
    cleaned = str(value or "").strip().upper()
    if cleaned and not cleaned.startswith("0X"):
        cleaned = f"0X{cleaned}"
    return cleaned


def detect_profile(facts: DeviceFacts) -> ProfileMatch:
    model = _normalized(facts.model)
    firmware = _firmware_build(facts.firmware)
    variant = _normalized(facts.variant)
    platform = _normalized(facts.platform)
    observed_product = _product_id(facts.product_id)
    profiles = (*STATIONARY_PROFILES, PORTABLE_PROFILE)
    model_profiles = [
        item for item in profiles
        if any(pattern in model for pattern in item.supported_model_patterns)
    ]
    if not model_profiles:
        return ProfileMatch(None, "model family is not supported by a setup profile", "none")
    if not firmware:
        return ProfileMatch(None, "exact firmware build is missing", "firmware")
    build_profiles = [item for item in model_profiles if firmware in item.firmware_patterns]
    if not build_profiles:
        return ProfileMatch(None, "exact firmware build is not approved for critical writes", "firmware")
    if not variant:
        return ProfileMatch(None, "live firmware variant is missing", "variant")
    variant_profiles = [item for item in build_profiles if variant in item.variants]
    if not variant_profiles:
        return ProfileMatch(None, "firmware variant does not match the approved model profile", "variant")
    if not platform:
        return ProfileMatch(None, "live platform/moduleType is missing", "platform")
    platform_profiles = [item for item in variant_profiles if platform in item.platforms]
    if len(platform_profiles) != 1:
        return ProfileMatch(None, "platform does not identify one approved write profile", "platform")
    profile = platform_profiles[0]
    expected_product = _product_id(profile.product_id)
    if observed_product and observed_product != expected_product:
        return ProfileMatch(None, "observed product ID conflicts with the approved profile", "product_id")
    if not observed_product:
        return ProfileMatch(
            profile,
            "exact firmware, variant, model and platform profile; product ID derived from the unique approved profile",
            "exact-profile-derived-product",
            expected_product,
        )
    return ProfileMatch(
        profile,
        "exact firmware, product, variant, model and platform profile",
        "exact-research-profile",
        expected_product,
    )


def all_profiles() -> tuple[SshProfile, ...]:
    return *STATIONARY_PROFILES, PORTABLE_PROFILE
