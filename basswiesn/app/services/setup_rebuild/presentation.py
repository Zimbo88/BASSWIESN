"""Durable setup-owned presentation transaction. No warm BoseApp replacement.

A lost SSH reply leaves intent on disk. Resuming never replays a write; the
explicit recovery path inspects the actual device before deciding what to undo.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
from typing import Any

from basswiesn.app.config import get_settings
from basswiesn.app.services import radio_patch_policy as policy
from basswiesn.app.services.protected_devices import protected_device_ids
from .backup import write_artifact, write_json_artifact
from .presentation_model import BASE, FILES, LIMIT, PresentationError, Target, classify, digest, package, parse_snapshot, unpack_backup
from .presentation_transport import PresentationTransport


def read_private(path: Path, limit: int = LIMIT) -> bytes:
    """Reject symlinks in every path component, including backup directories."""
    path = path.absolute()
    if ".." in path.parts:
        raise PresentationError("PRESENTATION_BACKUP_PATH_INVALID")
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit:
            raise PresentationError("PRESENTATION_PRIVATE_FILE_UNSAFE")
        output = bytearray()
        while chunk := os.read(fd, 65536):
            output.extend(chunk)
            if len(output) > limit:
                raise PresentationError("PRESENTATION_PRIVATE_FILE_TOO_LARGE")
        return bytes(output)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


class SetupPresentation:
    def __init__(self, adapter: Any):
        self.adapter = adapter

    async def transport(self, row: Any) -> PresentationTransport:
        facts = await self.adapter.identify(row)
        target = Target(str(row.device_id).upper(), facts["variant"], facts["model"], facts["platform"],
                        facts["firmware"], tuple(sorted(protected_device_ids())))
        target.validate()
        return PresentationTransport(str(row.ip_address), target, self.adapter.ssh_config)

    def root(self, row: Any) -> Path:
        raw = str(getattr(row, "backup_path", "") or "")
        if not raw:
            raise PresentationError("PRESENTATION_BASELINE_REQUIRED")
        path = Path(raw).absolute()
        expected = (get_settings().data_dir / "setup-rebuild").absolute()
        if not path.is_relative_to(expected) or ".." in path.parts:
            raise PresentationError("PRESENTATION_BASELINE_PATH_INVALID")
        return path / "presentation"

    def registry(self, identity: str) -> Path:
        return get_settings().data_dir / "setup-rebuild" / "presentation-receipts" / identity

    def managed(self, target: Target) -> dict[str, Any]:
        try:
            value: dict[str, Any] = json.loads(read_private(self.registry(target.identity) / "receipt.json", 16000))
            return value
        except FileNotFoundError:
            return {}

    def state(self, row: Any) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(read_private(self.root(row) / "transaction.json", 65536))
        if value.get("identity") != str(row.device_id).upper():
            raise PresentationError("PRESENTATION_RECEIPT_IDENTITY_MISMATCH")
        return value

    def save(self, row: Any, value: dict[str, Any], phase: str) -> None:
        value["phase"] = phase
        write_json_artifact(self.root(row), "transaction.json", value)

    def validate_backup(self, row: Any) -> None:
        value = self.state(row)
        if digest(read_private(self.root(row) / "firmware.private.tar")) != value["backup_sha256"]:
            raise PresentationError("PRESENTATION_BACKUP_CORRUPT")

    async def preflight(self, row: Any) -> dict[str, Any]:
        transport = await self.transport(row)
        snap = parse_snapshot(await transport.exchange("inspect"))
        mode = classify(transport.target, snap, self.managed(transport.target))
        return {"presentation_preflight": mode, "presentation_required": ["display", "bootlogo"]}

    async def prepare(self, row: Any) -> dict[str, Any]:
        root = self.root(row)
        if (root / "transaction.json").exists():
            raise PresentationError("PRESENTATION_EXISTING_INTENT_REVIEW_REQUIRED")
        transport = await self.transport(row)
        before = parse_snapshot(await transport.exchange("inspect"))
        mode = classify(transport.target, before, self.managed(transport.target))
        raw = await transport.exchange("backup")
        files = unpack_backup(raw)
        for name, field in zip(FILES, ("app", "battery", "splash_a", "splash_b")):
            if digest(files[name]) != before[field]:
                raise PresentationError("PRESENTATION_BACKUP_DIVERGED")
        after = parse_snapshot(await transport.exchange("inspect"))
        if any(before[key] != after[key] for key in before if key not in {"space", "root_space"}):
            raise PresentationError("PRESENTATION_BASELINE_CHANGED")
        backup = write_artifact(root, "firmware.private.tar", raw, max_bytes=LIMIT)
        if digest(read_private(Path(backup.path))) != backup.sha256:
            raise PresentationError("PRESENTATION_OFFDEVICE_BACKUP_FAILED")
        value = {"identity": transport.target.identity, "mode": mode, "before": before,
                 "backup_sha256": backup.sha256, "hashes": {}, "package_sha256": ""}
        if mode == "install":
            archive, hashes = package(transport.target, before, files)
            artifact = write_artifact(root, "candidate.private.tar", archive, max_bytes=LIMIT)
            value.update(hashes=hashes, package_sha256=artifact.sha256)
        self.save(row, value, "PREPARED")
        return {"presentation_prepared": True, "presentation_mode": mode,
                "presentation_backup_sha256": backup.sha256, "presentation_receipt": str(root / "transaction.json")}

    async def activate(self, row: Any) -> dict[str, Any]:
        value = self.state(row)
        if value["phase"] != "PREPARED":
            raise PresentationError("PRESENTATION_NO_AUTOMATIC_WRITE_REPLAY")
        transport = await self.transport(row)
        current = parse_snapshot(await transport.exchange("inspect"))
        before = value["before"]
        # Space changes and a preceding expert SSH reboot do not change images.
        for key in ("app", "battery", "splash_a", "splash_b", "rc", "base", "control"):
            if current[key] != before[key]:
                raise PresentationError("PRESENTATION_PRECONDITION_CHANGED")
        self.validate_backup(row)
        value["activation_boot"] = current["boot"]
        if value["mode"] == "existing":
            classify(transport.target, current, self.managed(transport.target))
            self.save(row, value, "ADOPTED_REBOOT_REQUIRED")
            return {"presentation_activation": "existing_preserved", "presentation_changed": False}
        archive = read_private(self.root(row) / "candidate.private.tar")
        if digest(archive) != value["package_sha256"]:
            raise PresentationError("PRESENTATION_PACKAGE_CORRUPT")
        self.save(row, value, "STAGE_INTENT")
        receipt = await transport.exchange("stage", before=before, hashes=value["hashes"], data=archive, archive_sha=value["package_sha256"])
        expected = f'{before["splash_a"]}  splash.original\n'
        if before["rc"] != "absent":
            expected += f'{before["rc"]}  rc.original\n'
        if receipt != expected.encode():
            raise PresentationError("PRESENTATION_RADIO_BACKUP_MISMATCH")
        self.save(row, value, "RECEIPT_INTENT")
        await transport.exchange("receipt", before=before, hashes=value["hashes"], archive_sha=digest(receipt))
        self.save(row, value, "ACTIVATION_INTENT")
        await transport.exchange("activate", before=before, hashes=value["hashes"])
        self.save(row, value, "CONFIGURED_REBOOT_REQUIRED")
        return {"presentation_activation": "cold_boot_pending", "presentation_changed": True}

    async def verify(self, row: Any) -> dict[str, Any]:
        value = self.state(row)
        if value["phase"] not in {"CONFIGURED_REBOOT_REQUIRED", "ADOPTED_REBOOT_REQUIRED"}:
            raise PresentationError("PRESENTATION_VERIFY_PHASE_INVALID")
        transport = await self.transport(row)
        before = value["before"]
        deadline = asyncio.get_running_loop().time() + 180
        while True:
            snap = parse_snapshot(await transport.exchange("inspect"))
            if snap["boot"] == value["activation_boot"]:
                raise PresentationError("PRESENTATION_NEW_BOOT_NOT_OBSERVED")
            if snap["pending"] == "no" and snap["healthy"] == snap["boot"]:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise PresentationError("PRESENTATION_BOOT_GUARD_UNCONFIRMED")
            await asyncio.sleep(5)
        expected_app = value["hashes"].get("BoseApp.candidate", before["app"])
        expected_rc = value["hashes"].get("rc.installed", before["rc"])
        expected_control = value["hashes"].get("control.sh", before["control"])
        if (snap["app"] != expected_app or snap["battery"] != before["battery"] or
            snap["splash_a"] != policy.SPLASH_LOGO or snap["splash_b"] != policy.SPLASH_LOGO or
            snap["rc"] != expected_rc or snap["rc_installed"] != expected_rc or
            snap["control"] != expected_control or snap["base"] != (BASE if value["mode"] == "install" else before["base"]) or snap["bind_ro"] != "yes"):
            raise PresentationError("PRESENTATION_RUNTIME_READBACK_MISMATCH")
        await self.adapter.identify(row)
        value["after"] = snap
        self.save(row, value, "VERIFIED")
        if snap["base"] == BASE:
            write_json_artifact(self.registry(transport.target.identity), "receipt.json", {
                "identity": transport.target.identity, "verified": True,
                "control": expected_control, "rc": expected_rc, "app": expected_app})
        return {"presentation_verified": True, "presentation_battery_unchanged": True,
                "presentation_zone_labels": "INSTALLED_VISUAL_ACCEPTANCE_REQUIRED" if expected_app in {
                    policy.APP_DISPLAY, policy.APP_COMBINED} else "NOT_INSTALLED_EXISTING_LAYOUT_PRESERVED",
                "presentation_visual_acceptance": "MANUAL_REQUIRED", "presentation_boot": snap["boot"]}

    async def undo(self, row: Any) -> bool:
        try:
            value = self.state(row)
        except FileNotFoundError:
            return False
        if value["mode"] == "existing" or value["phase"] in {"PREPARED", "ROLLED_BACK"}:
            return False
        if value["phase"] in {"STAGE_INTENT", "RECEIPT_INTENT"}:
            raise PresentationError("PRESENTATION_INTERRUPTED_STAGING_MANUAL_REVIEW_REQUIRED")
        self.validate_backup(row)
        transport = await self.transport(row)
        snap = parse_snapshot(await transport.exchange("inspect"))
        if snap["base"] != BASE or snap["control"] != value["hashes"]["control.sh"]:
            raise PresentationError("PRESENTATION_ROLLBACK_OWNER_MISMATCH")
        if value["phase"] in {"ROLLBACK_INTENT", "ROLLBACK_REBOOT_REQUIRED"}:
            # Explicit recovery may observe a lost reply, but must not blindly
            # issue the same firmware write twice. Only finish an observed undo.
            if any(snap[k] != value["before"][k] for k in ("rc", "splash_a", "splash_b")):
                raise PresentationError("PRESENTATION_ROLLBACK_UNCERTAIN_MANUAL_REVIEW_REQUIRED")
            self.save(row, value, "ROLLBACK_REBOOT_REQUIRED")
            return True
        value["rollback_boot"] = snap["boot"]
        self.save(row, value, "ROLLBACK_INTENT")
        await transport.exchange("undo", before=value["before"], hashes=value["hashes"])
        self.save(row, value, "ROLLBACK_REBOOT_REQUIRED")
        return True

    async def verify_undo(self, row: Any) -> None:
        value = self.state(row)
        transport = await self.transport(row)
        snap = parse_snapshot(await transport.exchange("inspect"))
        if value["phase"] != "ROLLBACK_REBOOT_REQUIRED" or snap["boot"] == value["rollback_boot"]:
            raise PresentationError("PRESENTATION_ROLLBACK_REBOOT_UNCONFIRMED")
        if any(snap[k] != value["before"][k] for k in ("app", "battery", "rc", "splash_a", "splash_b")) or snap["bind_ro"] != "no":
            raise PresentationError("PRESENTATION_ROLLBACK_READBACK_MISMATCH")
        self.save(row, value, "ROLLED_BACK")
        write_json_artifact(self.registry(transport.target.identity), "receipt.json", {
            "identity": transport.target.identity, "verified": False, "state": "REMOVED_BY_SETUP_RECOVERY"})
