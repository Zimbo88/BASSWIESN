"""Explicit marker transaction, not a firmware modification or reboot test.

The legacy recovery route requires an already working SSH connection. Existing
marker contents/metadata are left untouched; absent markers are the only files
created. No rc.local edit, remote backup overwrite, reboot or automatic rollback
is performed. The caller's SSH host-key policy remains a separate trust boundary.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
import ipaddress
import json
import re
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy.orm import Session

from basswiesn.app.models import ConfigBackup, Device
from basswiesn.app.services.action_journal import record_action, record_transport_attempt
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.device_settings_writer import ClientFactory, IdentityMismatch, verify_settings_identity

# POSIX/BusyBox-compatible, fixed paths only. Reject special files/symlinks;
# neither raw configuration files nor credentials are read by this probe.
_PRECONDITIONS = """test -d /mnt/nv && test ! -L /mnt/nv || exit 41
test -x /etc/init.d/sshd || exit 41
command -v remote_services_enabled >/dev/null 2>&1 || exit 41
for p in /mnt/nv/remote_services /tmp/remote_services; do
 if test -L "$p"; then exit 42; fi
 if test -e "$p" && ! test -f "$p"; then exit 42; fi
done
"""
READ_COMMAND = _PRECONDITIONS + """if test -e /mnt/nv/remote_services; then echo PERSISTENT=1; else echo PERSISTENT=0; fi
if test -e /tmp/remote_services; then echo TEMPORARY=1; else echo TEMPORARY=0; fi
if remote_services_enabled >/dev/null 2>&1; then
 echo ENABLED=1
else
 result=$?
 test "$result" -eq 1 || exit 43
 echo ENABLED=0
fi
echo PROBE_COMPLETE
"""
WRITE_COMMAND = "set -e\n" + _PRECONDITIONS + """set -C
if ! test -e /mnt/nv/remote_services; then (umask 077; : > /mnt/nv/remote_services); fi
if ! test -e /tmp/remote_services; then (umask 077; : > /tmp/remote_services); fi
sync
"""


class SshRunner(Protocol):
    async def __call__(self, ip_address: str, username: str, command: str,
                       timeout: int) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class MarkerState:
    persistent: bool
    temporary: bool
    enabled: bool


def parse_state(reply: Mapping[str, object]) -> MarkerState:
    output = reply.get("stdout")
    if type(reply.get("returncode")) is not int or reply["returncode"] != 0:
        raise ValueError("SSH read did not complete")
    if not isinstance(output, str) or len(output) > 1024:
        raise ValueError("SSH readback is missing or too large")
    lines = output.splitlines()
    if len(lines) != 4 or lines[-1] != "PROBE_COMPLETE":
        raise ValueError("SSH readback framing is incomplete")
    values: list[bool] = []
    for label, line in zip(("PERSISTENT", "TEMPORARY", "ENABLED"), lines[:3], strict=True):
        if line not in {f"{label}=0", f"{label}=1"}:
            raise ValueError("SSH readback contains an unknown or duplicate field")
        values.append(line.endswith("=1"))
    state = MarkerState(*values)
    if (state.persistent or state.temporary) and not state.enabled:
        raise ValueError("marker and service-policy observations contradict")
    return state


class SshPersistenceError(Exception):
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        super().__init__(str(result["code"]))


def _target(device: Device) -> tuple[str, str, str, str, str]:
    return (device.device_id, device.ip_address, device.model, device.firmware, device.info_xml)


async def enable_persistence(
    db: Session, device: Device, *, username: str, client_factory: ClientFactory,
    ssh_runner: SshRunner, guard: Callable[[Session, Device], None],
    expected_previous: object = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "job_id": f"ssh-persistence-{uuid4().hex}", "device_id": device.device_id,
        "target": device.ip_address, "dry_run": False, "persistent_ssh_boot_verified": False,
        "automatic_retry": False, "automatic_rollback": False,
    }
    attempted = False
    backup_ref = ""
    stage = "SSH_PREFLIGHT_FAILED"

    async with device_lock(device.device_id):
        guard(db, device)  # stable-ID/IP protection before either transport
        frozen = _target(device)

        def unchanged_target() -> None:
            db.refresh(device)
            if _target(device) != frozen:
                raise IdentityMismatch("stored target changed during transaction")
            guard(db, device)

        try:
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}", username):
                raise ValueError("invalid SSH username")
            ipaddress.ip_address(frozen[1])  # do not pass an unbound hostname to SSH
            client = client_factory(frozen[1], device_id=frozen[0],
                                    request_purpose="ssh_persistence", trigger="explicit_recovery")
            info = await client.get_xml("/info")
            profile = verify_settings_identity(device, info)
            unchanged_target()
            before = parse_state(await ssh_runner(frozen[1], username, READ_COMMAND, 12))
            unchanged_target()
            if expected_previous is not None and (
                not isinstance(expected_previous, dict)
                or set(expected_previous) != set(asdict(before))
                or any(type(value) is not bool for value in expected_previous.values())
                or expected_previous != asdict(before)
            ):
                stage = "WRITE_PRECONDITION_FAILED"
                raise ValueError("marker state differs from preview")
            if before == MarkerState(True, True, True):
                return {**result, "ok": True, "status": "UNCHANGED", "verified": asdict(before),
                        "note": "Markers observed; reboot persistence was not tested. No write performed."}

            stage = "SSH_BACKUP_FAILED"
            content = json.dumps({"schema": 1, "device_id": frozen[0], "profile": profile,
                                  "info_xml": info, "marker_state": asdict(before),
                                  "scope": "existing markers untouched; absent markers only created"},
                                 sort_keys=True, separators=(",", ":"))
            digest = sha256(content.encode()).hexdigest()
            backup = ConfigBackup(device_id=frozen[0], path="ssh:persistence-marker-state", content=content)
            db.add(backup)
            db.flush()
            backup_id = backup.id
            backup_ref = f"config-backup:{backup_id}:sha256:{digest}"
            result["backup_ref"] = backup_ref
            result["created_markers"] = [name for name in ("persistent", "temporary") if not getattr(before, name)]
            result["rollback_scope"] = "MANUAL_MARKER_RECONCILIATION"
            record_action(db, job_id=result["job_id"], device_id=frozen[0], ip_address=frozen[1],
                          action="persistent_ssh", trigger="explicit_recovery", phase="ssh_persistence_intent",
                          before_state=asdict(before), requested_state=asdict(MarkerState(True, True, True)),
                          backup_ref=backup_ref, rollback_ref=backup_ref, result="PREPARED", verified=False)
            db.commit()  # intent and bounded START snapshot must survive transport loss

            stage = "WRITE_PRECONDITION_FAILED"
            unchanged_target()
            verify_settings_identity(device, await client.get_xml("/info"))
            if parse_state(await ssh_runner(frozen[1], username, READ_COMMAND, 12)) != before:
                raise ValueError("marker state changed after backup")
            unchanged_target()
            db.expire_all()
            stored = db.get(ConfigBackup, backup_id)
            if stored is None or stored.device_id != frozen[0] or sha256(stored.content.encode()).hexdigest() != digest:
                raise ValueError("durable backup is missing or altered")

            stage = "SSH_READBACK_FAILED"
            attempted = True
            uncertain = False
            try:
                reply = await ssh_runner(frozen[1], username, WRITE_COMMAND, 20)
                uncertain = type(reply.get("returncode")) is not int or reply["returncode"] != 0
            except Exception:
                uncertain = True  # never infer that a timeout undid a remote write
            unchanged_target()
            verify_settings_identity(device, await client.get_xml("/info"))
            actual = parse_state(await ssh_runner(frozen[1], username, READ_COMMAND, 12))
            unchanged_target()
            if actual != MarkerState(True, True, True):
                raise ValueError("persistent markers were not read back")
            stage = "SSH_RESULT_COMMIT_FAILED"
            record_action(db, job_id=result["job_id"], device_id=frozen[0], ip_address=frozen[1],
                          action="persistent_ssh", trigger="explicit_recovery", phase="ssh_persistence_verified",
                          before_state=asdict(before), readback=asdict(actual), after_state=asdict(actual),
                          backup_ref=backup_ref, rollback_ref=backup_ref, result="VERIFIED", verified=True)
            db.commit()
            return {**result, "ok": True, "status": "VERIFIED", "verified": asdict(actual),
                    "transport_uncertain": uncertain,
                    "note": "Markers read back over SSH; reboot persistence NOT VERIFIED. No reboot performed."}
        except (Exception, asyncio.CancelledError) as exc:
            db.rollback()
            code = "IDENTITY_MISMATCH" if isinstance(exc, IdentityMismatch) else stage
            status = "DIVERGED" if attempted else "BLOCKED"
            record_transport_attempt(
                device_id=frozen[0], ip_address=frozen[1], action="persistent_ssh",
                trigger="explicit_recovery", phase="ssh_persistence_diverged" if attempted else "ssh_persistence_blocked",
                result=status, job_id=result["job_id"], backup_ref=backup_ref,
                rollback_ref=backup_ref, error_category=code, verified=False,
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise SshPersistenceError({**result, "ok": False, "status": status, "code": code,
                "reconciliation_required": attempted,
                "message": "No automatic retry or rollback. Read state and inspect the saved snapshot before another action."}) from None
