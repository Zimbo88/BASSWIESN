"""Offline patch compatibility. A plan is never an installation receipt.

Display, splash writer and battery policy own different files. Whole-image
stock replacement is explicitly not a supported way to combine patches.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

APP_STOCK = "8b2b7d19dff149894369b72e106bb8a75d79a68424b7aa2f53bd673a0fce831b"
APP_LEGACY = "a7b7a1f2a792a0f2650d1bcb5c2281f1f7715907151604a1d4670ec6a58bf08d"
APP_DISPLAY_PREVIOUS = "cac446a300bc1e7022a1dd16493b2c84c5c76072925a053ca05f93da58e13918"
APP_COMBINED_PREVIOUS = "be33021e87b9374ed4bff0df992d368f42804c95766eca3b3364bf294c88e37f"
APP_DISPLAY_ACCEPTED = "e1395d4e90de748af0af1960a40d883feeefe66b01f4f6e10f53c702120d6154"
APP_COMBINED_ACCEPTED = "cec0697eaf37a981ce4fdd7514d73c9f595d886ebb0faf8396c14bc34c328086"
# New local-zone labels are ARM-emulator tested, not yet hardware accepted.
# Keep accepted installations identifiable; an app update never replaces them.
APP_DISPLAY = "8e715d3b48f15101e0344a25a71ade58bab6d2c81735b50d71f5c9cdb9ac29fa"
APP_COMBINED = "77300b927e615c68c18a766ab200f7ed8ded4e1b4cb5b92fec8bcad46a723690"
ADOPTABLE_DISPLAY_IMAGES = frozenset({APP_DISPLAY, APP_COMBINED,
                                    APP_DISPLAY_ACCEPTED, APP_COMBINED_ACCEPTED})
BATTERY_STOCK = "93b23730a3ac66f3331f63711d4a1d1d60704ccb03635b7ab8210c1f084f4fa0"
BATTERY_PATCHED = "4abbb803a20323bf2938e686aa43fe969495e508f4fefe9c6099702f1e7e4e71"
BATTERY_LEGACY = "25ed53ef0bb3a8647d6f858ccc4be20dc6292f988acb29059416d5c0591229c5"
SPLASH_STOCK = "49d4432df819c91ac06a7180b4d814cffaa6380450b2206316040401719ee54b"
SPLASH_LOGO = "47a142fa24a0d6cb4b13a138e03140a68bd01bd6ec323fbdb4c469fd063caf1c"
FIRMWARE = "27.0.6.46330.5043500"
PROFILES = frozenset({("SoundTouch 30", "mojo", "scm"),
                      ("SoundTouch Portable", "taigan", "scm"),
                      ("SoundTouch 20", "spotty", "sm2")})
PatchName = Literal["display", "bootlogo", "battery", "wifi", "long_press_reboot"]
REQUIRED_SETUP_PATCHES: tuple[PatchName, ...] = ()


def default_selection() -> dict[str, str]:
    """New plans preserve firmware; enabling a patch needs explicit selection."""
    return {"display": "keep", "bootlogo": "keep", "battery": "keep", "battery_kind": "unknown"}


def presentation_supported(model: str, variant: str, platform: str, firmware: str) -> bool:
    """Catalogue eligibility only; actual hashes and identity still need readback."""
    return (model, variant, platform) in PROFILES and firmware.split(" ", 1)[0] == FIRMWARE


def setup_presentation_contract() -> dict[str, object]:
    """Optional installer contract; only a job readback proves installation."""
    return {
        "required_components": list(REQUIRED_SETUP_PATCHES),
        "optional_components": ["display", "bootlogo"],
        "default_mode": "normal",
        "battery_action": "keep",
        "unknown_profile_action": "BLOCK_PATCH_INSTALLATION",
        "exact_image_and_offdevice_backup_required": True,
        "cold_boot_and_actual_readback_required": True,
        "guided_installer_available": True,
        "setup_integration_status": "IMPLEMENTED_HARDWARE_ACCEPTANCE_REQUIRED",
        "pinned_root_ssh_required": True,
        "hardware_setup_validation": "MANUAL_REQUIRED",
        "may_claim_standard_presentation_installed": False,
        "recovery_removal_allowed": True,
    }


@dataclass(frozen=True)
class ImageChange:
    offset: int
    before: bytes
    after: bytes


def compose_image(original: bytes, *, expected: str, result: str,
                  changes: tuple[ImageChange, ...], tail: bytes) -> bytes:
    """Apply exact, nonoverlapping owned ranges; retain all other bytes.

    This helper has no filesystem/network access. The caller supplies a sealed
    recipe for one known input, not arbitrary user-provided patch instructions.
    In particular a pre-existing modified image is never normalized to stock.
    """
    if sha256(original).hexdigest() != expected:
        raise ValueError("PATCH_BASE_UNKNOWN")
    output = bytearray(original)
    previous_end = 0
    for change in sorted(changes, key=lambda item: item.offset):
        start, end = change.offset, change.offset + len(change.before)
        if (not change.before or start < previous_end or start < 0 or end > len(original)
                or len(change.before) != len(change.after)
                or original[start:end] != change.before):
            raise ValueError("PATCH_OWNERSHIP_CONFLICT")
        output[start:end] = change.after
        previous_end = end
    output.extend(tail)
    if sha256(output).hexdigest() != result:
        raise ValueError("PATCH_RESULT_MISMATCH")
    return bytes(output)


def compatibility(*, model: str, variant: str, platform: str, firmware: str,
                  app: str, battery: str, splash: tuple[str, str],
                  framebuffer: tuple[int, int, int, int]) -> dict[str, object]:
    """Exact observed-image eligibility, NOT permission to write or hardware proof."""
    reasons = []
    if (model, variant, platform) not in PROFILES:
        reasons.append("UNQUALIFIED_MODEL_VARIANT_PLATFORM")
    if firmware.split(" ", 1)[0] != FIRMWARE:
        reasons.append("UNQUALIFIED_FIRMWARE")
    if app not in {APP_STOCK, APP_LEGACY, *ADOPTABLE_DISPLAY_IMAGES,
                   APP_DISPLAY_PREVIOUS, APP_COMBINED_PREVIOUS}:
        reasons.append("UNKNOWN_BOSEAPP")
    if battery not in {BATTERY_STOCK, BATTERY_PATCHED, BATTERY_LEGACY}:
        reasons.append("UNKNOWN_BATTERYMONITOR")
    if len(splash) != 2 or any(value not in {SPLASH_STOCK, SPLASH_LOGO} for value in splash):
        reasons.append("UNKNOWN_BOOT_WRITER")
    if len(set(splash)) != 1:
        reasons.append("PARTIAL_BOOT_WRITER_STATE")
    if framebuffer != (128, 100, 8, 128):
        reasons.append("UNQUALIFIED_FRAMEBUFFER")
    return {"eligible_for_private_trial": not reasons, "reasons": reasons,
            "battery_must_remain_unchanged": True, "existing_app_changes_must_be_preserved": True,
            "candidate_app_sha256": APP_COMBINED if app in {
                APP_LEGACY, APP_COMBINED, APP_COMBINED_ACCEPTED, APP_COMBINED_PREVIOUS} else APP_DISPLAY,
            "hardware_acceptance_required": True, "write_authorized": False}


def catalogue() -> list[dict[str, object]]:
    return [
        {"id": "display", "maturity": "LAB", "selectable": True,
         "owns": ["BoseApp display hooks", "cold-start binding"],
         "description": "Fixed station, title/artist and centred clock layout. Local Bose Zone prefixes [M]/[G] in the new revision. Cold boot only.",
         "rollback": "Disable owned binding and reboot; restore the exact pre-patch application.",
         "evidence": "Earlier layout user-accepted on SoundTouch 30 SCM; new [M]/[G] revision ARM-emulator tested only. Hardware acceptance required."},
        {"id": "bootlogo", "maturity": "LAB", "selectable": True,
         "owns": ["/usr/bin/splash-write", "/opt/Bose/splash-write"],
         "description": "BASSWIESN above the native progress field. No bootloader/kernel patch.",
         "rollback": "Restore both verified original writer files; leave the display choice unchanged.",
         "evidence": "Lettering observed during boot, not proven on the very first frame."},
        {"id": "battery", "maturity": "LAB", "selectable": True,
         "owns": ["/opt/Bose/BatteryMonitor"],
         "description": "Separate third-party battery option for exact Portable profiles; unnecessary for original batteries.",
         "rollback": "Restore only the verified BatteryMonitor backup; preserve display and logo.",
         "evidence": "Existing original and two known modified hashes recognized; never auto-apply."},
        {"id": "wifi", "maturity": "EXPERIMENTAL", "selectable": False,
         "owns": [], "description": "Channel-change recovery is not hardware-qualified.",
         "rollback": "No production installer enabled.", "evidence": "BLOCKED pending controlled recovery evidence."},
        {"id": "long_press_reboot", "maturity": "EXPERIMENTAL", "selectable": False,
         "owns": [], "description": "Long-press reboot is not qualified for installation.",
         "rollback": "No production installer enabled.", "evidence": "NOT HARDWARE VERIFIED"},
    ]
