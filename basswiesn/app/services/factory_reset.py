"""Explicit LAB reset: durable evidence before one irreversible command.

A send receipt cannot prove factory state or even loss of reachability. There
is no automatic retry, follow-up discovery, account restoration or rollback.
The existing baseline is redacted and cannot restore authentication secrets.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from types import SimpleNamespace
from typing import Any, Protocol
from uuid import uuid4

from defusedxml import ElementTree as SafeXML  # type: ignore[import-untyped]
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import Device, DiagnosticEvent
from basswiesn.app.services.action_journal import record_action, record_transport_attempt
from basswiesn.app.services.device_settings_writer import ClientFactory, verify_settings_identity
from basswiesn.app.services.device_transactions import device_fingerprint, device_transaction
from basswiesn.app.services.setup_rebuild.candidates import SetupCandidate
from basswiesn.app.services.setup_rebuild.profiles import DeviceFacts, detect_profile
from basswiesn.app.services.zone_changes import Zone


class ResetAdapter(Protocol):
    async def identify(self, row: Any) -> dict[str, Any]: ...
    async def backup(self, row: Any) -> dict[str, Any]: ...


class ResetReply(Protocol):
    def public_dict(self) -> dict[str, object]: ...


class FactoryResetError(Exception):
    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(str(detail["error"]))


_REQUIRED = {"info.xml", "networkInfo.xml", "sources.xml", "presets.xml",
             "now_playing.xml", "volume.xml", "getZone.xml", "backup-manifest.json"}


def verify_reset_baseline(backup: Mapping[str, Any], device: Device) -> dict[str, str]:
    """Verify the actual bounded files, not the adapter's boolean declaration.

    Open every directory/leaf without symlink traversal and hash opened files.
    Restrict the baseline to the configured data tree, reject links/special files
    and group/world-writable leaves. This is not a defence against the same UID.
    """
    if backup.get("verified") is not True:
        raise ValueError("baseline not captured")
    # The existing adapter supports a relative configured data directory.
    # Preserve lexical '..' for rejection; do not resolve through symlinks.
    path = Path(str(backup.get("backup_path", ""))).absolute()
    allowed = get_settings().data_dir.absolute() / "setup-rebuild"
    relative = path.relative_to(allowed)
    hashes = backup.get("sha256")
    if not path.is_absolute() or ".." in path.parts or len(relative.parts) != 3 or relative.parts[-2:] != (device.device_id.upper(), "baseline"):
        raise ValueError("unexpected baseline location")
    if not isinstance(hashes, dict) or not _REQUIRED.issubset(hashes) or len(hashes) > 32:
        raise ValueError("incomplete baseline manifest")
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.(?:xml|json)", name)
           or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
           for name, digest in hashes.items()):
        raise ValueError("unsafe baseline manifest")
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    raw: dict[str, str] = {}
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        for name, digest in hashes.items():
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o022 or not 0 < info.st_size <= 1048576:
                    raise ValueError("unsafe baseline file")
                data = bytearray()
                while len(data) <= 1048576:
                    chunk = os.read(fd, min(65536, 1048577 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                if len(data) != info.st_size or sha256(data).hexdigest() != digest:
                    raise ValueError("baseline hash mismatch")
                raw[name] = data.decode("utf-8")
            finally:
                os.close(fd)
    finally:
        os.close(directory)
    manifest = json.loads(raw["backup-manifest.json"])
    if not isinstance(manifest, dict) or manifest.get("artifacts") != {key: value for key, value in hashes.items() if key != "backup-manifest.json"}:
        raise ValueError("baseline manifest differs from captured receipt")
    for name, tag in {"info.xml": "info", "networkInfo.xml": "networkInfo", "sources.xml": "sources",
                      "presets.xml": "presets", "now_playing.xml": "nowPlaying", "volume.xml": "volume", "getZone.xml": "zone"}.items():
        root = SafeXML.fromstring(raw[name], forbid_dtd=True)
        if root.tag != tag or (root.get("deviceID") is not None and root.get("deviceID", "").upper() != device.device_id.upper()):
            raise ValueError("backup XML has an unexpected root or identity")
    verify_settings_identity(device, raw["info.xml"])
    if not Zone.parse(raw["getZone.xml"], device_id=device.device_id).standalone(device.device_id):
        raise ValueError("reset target still belongs to a zone")
    return raw


def _identity(identity: Mapping[str, Any], candidate: SetupCandidate) -> None:
    for field in ("device_id", "model", "firmware", "variant", "platform"):
        if str(identity.get(field, "")).strip().casefold() != str(getattr(candidate, field)).strip().casefold():
            raise ValueError("reset hardware identity changed")
    facts = DeviceFacts(candidate.device_id, candidate.ip_address, str(identity["model"]),
                        str(identity["firmware"]), product_id=str(identity.get("product_id", "")),
                        variant=str(identity["variant"]), platform=str(identity["platform"]))
    match = detect_profile(facts)
    if match.profile is None or match.profile.key != candidate.profile_key:
        raise ValueError("reset profile changed")


async def request_factory_reset(
    db: Session, device: Device, candidate: SetupCandidate, *, adapter: ResetAdapter,
    send: Callable[[str, str], Awaitable[ResetReply]], client_factory: ClientFactory,
    guard: Callable[[Session, Device], None],
) -> dict[str, Any]:
    """Preserve command uncertainty and leave actual reachability unmodified."""
    job = f"factory-reset-{uuid4().hex}"
    attempted, backup_ref = False, ""
    frozen = device_fingerprint(device)

    def unchanged() -> None:
        db.refresh(device)
        if device_fingerprint(device) != frozen:
            raise ValueError("stored reset target changed")
        guard(db, device)

    async with device_transaction([frozen[0]]):
        try:
            if not candidate.eligible or (candidate.device_id, candidate.ip_address) != (frozen[0], frozen[1]):
                raise ValueError("reset candidate no longer applies")
            unchanged()  # before adapter/client creates any transport
            row = SimpleNamespace(device_id=frozen[0], ip_address=frozen[1], name=device.name,
                                  expected_model=device.model, evidence_json="{}")
            identity = await adapter.identify(row)
            _identity(identity, candidate)
            unchanged()
            row.evidence_json = json.dumps(identity)
            backup = await adapter.backup(row)
            unchanged()
            raw = verify_reset_baseline(backup, device)
            backup_ref = str(backup["backup_path"])
            before_zone = Zone.parse(raw["getZone.xml"], device_id=frozen[0])
            record_action(db, job_id=job, device_id=frozen[0], ip_address=frozen[1],
                          action="factory_reset", trigger="explicit_lab_reset", phase="factory_reset_intent",
                          before_state={"sha256": backup["sha256"]}, backup_ref=backup_ref,
                          requested_state={"operation": "factory_default"}, result="PREPARED", verified=False)
            db.commit()  # failure here must prevent the destructive command
            unchanged()
            _identity(await adapter.identify(row), candidate)
            unchanged()
            client = client_factory(frozen[1], device_id=frozen[0], request_purpose="factory_reset_preflight",
                                    trigger="explicit_lab_reset")
            zone = Zone.parse(await client.get_xml("/getZone"), device_id=frozen[0])
            if zone != before_zone:
                raise ValueError("reset topology changed after backup")
            unchanged()
            verify_reset_baseline(backup, device)
            attempted = True
            await send(frozen[1], frozen[0])
            # No raw CLI output is sent to the browser or used as reset evidence.
            record_action(db, job_id=job, device_id=frozen[0], ip_address=frozen[1],
                          action="factory_reset", trigger="explicit_lab_reset", phase="factory_reset_sent",
                          backup_ref=backup_ref, result="PENDING_MANUAL_READBACK", verified=False)
            db.add(DiagnosticEvent(event_id=str(uuid4()), device_id=frozen[0], domain="SESSION", severity="WARNING",
                code="FACTORY_RESET_SENT", message="LAB reset command sent; factory state and reachability are unverified.",
                evidence_json=json.dumps([{"job_id": job, "profile": candidate.profile_key}]), redacted=True))
            db.commit()
            return {"job_id": job, "identity": identity, "backup": backup,
                    "cli": {"operation": "factory_reset", "output": "not exposed; not state verification"},
                    "status": "FACTORY RESET SENT", "next_status": "WAITING FOR MANUAL READBACK",
                    "reachability": "NOT OBSERVED", "verified": False, "reconciliation_required": True,
                    "automatic_follow_up_probe": False, "automatic_retry": False, "automatic_rollback": False,
                    "restore_scope": "PARTIAL: redacted settings baseline; account/session secrets not restorable"}
        except (Exception, asyncio.CancelledError) as exc:
            db.rollback()
            code = "FACTORY_RESET_OUTCOME_UNKNOWN" if attempted else "FACTORY_RESET_NOT_SENT"
            message = ("Command outcome unknown. Do not retry; manually verify device identity and state."
                       if attempted else "Reset blocked before the command. Review identity, profile, topology and backup.")
            record_transport_attempt(device_id=frozen[0], ip_address=frozen[1], action="factory_reset",
                trigger="explicit_lab_reset", phase="factory_reset_diverged" if attempted else "factory_reset_blocked",
                result="DIVERGED" if attempted else "BLOCKED", job_id=job, backup_ref=backup_ref,
                error_category=code, verified=False)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise FactoryResetError({"error": code, "message": message, "job_id": job, "backup_ref": backup_ref,
                "verified": False, "reconciliation_required": attempted, "automatic_retry": False}) from None
