"""Exact-image composition and archive validation, with no radio IO.

Only authored payloads and short owned patch ranges ship in the recipe. Whole
firmware images are acquired from the explicitly selected radio and stay private.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import tarfile
from typing import Any

from basswiesn.app.services import radio_patch_policy as policy

ASSETS = Path(__file__).with_name("presentation_assets")
BASE = "/mnt/nv/basswiesn-standard-presentation"
OLD_BASES = ("/mnt/nv/basswiesn-radio-patches", "/mnt/nv/basswiesn-display-installed")
FILES = ("opt/Bose/BoseApp", "opt/Bose/BatteryMonitor", "usr/bin/splash-write", "opt/Bose/splash-write")
LIMIT = 20 * 1048576
HASH = re.compile(r"[a-f0-9]{64}")


class PresentationError(RuntimeError):
    """Stable, non-secret error code; no remote stderr forwarded to the browser."""


def digest(raw: bytes) -> str:
    return sha256(raw).hexdigest()


def recipe() -> dict[str, Any]:
    value: dict[str, Any] = json.loads((ASSETS / "recipe.json").read_text())
    return value


def compose(raw: bytes, kind: str) -> bytes:
    spec = recipe()[kind]
    target = spec["results"].get(digest(raw))
    if target is None or len(raw) != spec["original_size"]:
        raise PresentationError("PRESENTATION_UNKNOWN_BASE")
    payload = bytes.fromhex(spec["payload_hex"])
    if digest(payload) != spec["payload_sha256"]:
        raise PresentationError("PRESENTATION_PAYLOAD_CORRUPT")
    return policy.compose_image(raw, expected=digest(raw), result=target,
        changes=tuple(policy.ImageChange(p["offset"], bytes.fromhex(p["before"]), bytes.fromhex(p["after"])) for p in spec["changes"]),
        tail=bytes(spec["padding"]) + payload)


@dataclass(frozen=True)
class Target:
    identity: str
    variant: str
    model: str
    platform: str
    firmware: str
    protected: tuple[str, ...] = ()

    def validate(self) -> None:
        if not re.fullmatch(r"[A-F0-9]{12}", self.identity) or self.identity in self.protected:
            raise PresentationError("PRESENTATION_IDENTITY_REFUSED")
        if (self.model, self.variant, self.platform) not in policy.PROFILES or not self.firmware.split() or self.firmware.split()[0] != policy.FIRMWARE:
            raise PresentationError("PRESENTATION_PROFILE_UNSUPPORTED")
        if any(not re.fullmatch(r"[A-F0-9]{12}", item) for item in self.protected):
            raise PresentationError("PRESENTATION_PROTECTION_CONFIGURATION_INVALID")


def unpack_backup(raw: bytes) -> dict[str, bytes]:
    if len(raw) > LIMIT:
        raise PresentationError("PRESENTATION_BACKUP_TOO_LARGE")
    allowed = set(FILES) | {"mnt/nv/rc.local"}
    for base in (BASE, *OLD_BASES):
        allowed.update(base.lstrip("/") + "/" + part for part in (
            "control.sh", "rc.installed", "SHA256SUMS", "backup/splash.original", "backup/rc.original", "backup/SHA256SUMS", "backup.receipt"))
    files: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for item in archive:
            if not item.isfile() or item.name not in allowed or item.name in files or not 0 <= item.size <= LIMIT:
                raise PresentationError("PRESENTATION_BACKUP_MEMBER_INVALID")
            total += item.size
            if total > LIMIT:
                raise PresentationError("PRESENTATION_BACKUP_TOO_LARGE")
            stream = archive.extractfile(item)
            if stream is None:
                raise PresentationError("PRESENTATION_BACKUP_UNREADABLE")
            files[item.name] = stream.read()
    if not set(FILES) <= files.keys():
        raise PresentationError("PRESENTATION_BACKUP_INCOMPLETE")
    return files


def parse_snapshot(raw: bytes) -> dict[str, str]:
    if len(raw) > 16000:
        raise PresentationError("PRESENTATION_OBSERVATION_TOO_LARGE")
    result: dict[str, str] = {}
    for line in raw.decode("ascii").splitlines():
        key, sep, value = line.partition("=")
        if not sep or key in result or not re.fullmatch(r"[a-z_]+", key):
            raise PresentationError("PRESENTATION_OBSERVATION_INVALID")
        result[key] = value
    required = {"boot", "app", "battery", "splash_a", "splash_b", "rc", "root_ro", "base", "control", "rc_installed", "healthy", "pending", "bind_ro", "space", "root_space"}
    if not required <= result.keys() or result["root_ro"] != "yes":
        raise PresentationError("PRESENTATION_OBSERVATION_INCOMPLETE")
    for name in ("app", "battery", "splash_a", "splash_b"):
        if not HASH.fullmatch(result[name]):
            raise PresentationError("PRESENTATION_HASH_INVALID")
    return result


def classify(target: Target, snap: dict[str, str], managed: dict[str, Any] | None = None) -> str:
    target.validate()
    check = policy.compatibility(model=target.model, variant=target.variant, platform=target.platform,
        firmware=target.firmware, app=snap["app"], battery=snap["battery"],
        splash=(snap["splash_a"], snap["splash_b"]), framebuffer=(128, 100, 8, 128))
    if not check["eligible_for_private_trial"]:
        raise PresentationError("PRESENTATION_INCOMPATIBLE_IMAGE")
    spec = recipe()
    if snap["app"] in policy.ADOPTABLE_DISPLAY_IMAGES:
        approved = any(item["base"] == snap["base"] and item["control"] == snap["control"]
                       and item["rc"] == snap["rc"] for item in spec["legacy_installations"])
        if managed and managed.get("identity") == target.identity and managed.get("verified") is True and snap["base"] == BASE:
            approved = managed.get("control") == snap["control"] and managed.get("rc") == snap["rc"] and managed.get("app") == snap["app"]
        # New-controller approval is against the off-device job receipt instead,
        # never merely an arbitrary on-radio manifest or hash-shaped string.
        if not approved:
            raise PresentationError("PRESENTATION_EXISTING_INSTALL_REVIEW_REQUIRED")
        if snap["healthy"] != snap["boot"] or snap["pending"] != "no" or snap["bind_ro"] != "yes" or snap["rc"] != snap["rc_installed"] or snap["splash_a"] != policy.SPLASH_LOGO or snap["splash_b"] != policy.SPLASH_LOGO:
            raise PresentationError("PRESENTATION_EXISTING_INSTALL_UNVERIFIED")
        return "existing"
    if snap["base"] != "none" or snap["app"] not in spec["app"]["results"] or snap["splash_a"] != policy.SPLASH_STOCK or snap["splash_b"] != policy.SPLASH_STOCK:
        raise PresentationError("PRESENTATION_UPGRADE_REVIEW_REQUIRED")
    if snap["rc"] not in {"absent", *spec["reviewed_startups"]}:
        raise PresentationError("PRESENTATION_STARTUP_REVIEW_REQUIRED")
    if int(snap["space"]) < 24000 or int(snap["root_space"]) < 1024:
        raise PresentationError("PRESENTATION_INSUFFICIENT_SPACE")
    return "install"


def package(target: Target, snap: dict[str, str], files: dict[str, bytes]) -> tuple[bytes, dict[str, str]]:
    if classify(target, snap) != "install":
        raise PresentationError("PRESENTATION_NOT_A_FRESH_INSTALL")
    for name, field in zip(FILES, ("app", "battery", "splash_a", "splash_b")):
        if digest(files[name]) != snap[field]:
            raise PresentationError("PRESENTATION_BACKUP_DIVERGED")
    if digest(files.get("mnt/nv/rc.local", b"")) != (snap["rc"] if snap["rc"] != "absent" else digest(b"")):
        raise PresentationError("PRESENTATION_STARTUP_BACKUP_DIVERGED")
    app, splash = compose(files[FILES[0]], "app"), compose(files[FILES[2]], "splash")
    values = {"APP": snap["app"], "PATCH": digest(app), "BATTERY": snap["battery"], "RC_BEFORE": snap["rc"],
              "IDENTITY": target.identity, "VARIANT": target.variant, "PROTECTED": " ".join(target.protected)}
    control = (ASSETS / "control.sh.in").read_text()
    for name, value in values.items():
        control = control.replace("@" + name + "@", value)
    if re.search(r"@[A-Z_]+@", control):
        raise PresentationError("PRESENTATION_TEMPLATE_INVALID")
    rc = f'#!/bin/sh\n/bin/sh {BASE}/control.sh cold >> {BASE}/boot.log 2>&1 || :\n'
    if snap["rc"] != "absent":
        rc += f'if [ "$(sha256sum {BASE}/backup/rc.original | cut -d\' \' -f1)" = {snap["rc"]} ]; then\n /bin/sh {BASE}/backup/rc.original\nfi\n'
    data = {"control.sh": control.encode(), "rc.installed": (rc + "exit 0\n").encode(),
            "BoseApp.candidate": app, "splash.candidate": splash}
    hashes = {name: digest(value) for name, value in data.items()}
    data["SHA256SUMS"] = "".join(f"{value}  {name}\n" for name, value in sorted(hashes.items())).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, raw_value in sorted(data.items()):
            member = tarfile.TarInfo(name); member.size = len(raw_value); member.mode = 0o700
            archive.addfile(member, io.BytesIO(raw_value))
    return output.getvalue(), hashes
