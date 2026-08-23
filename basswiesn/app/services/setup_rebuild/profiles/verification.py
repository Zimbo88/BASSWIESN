"""SSH status parsing without exposing credentials or shell details."""

from __future__ import annotations

from dataclasses import dataclass

from .base import DeviceFacts
from ..states import SshStatus


@dataclass(frozen=True)
class VerificationResult:
    status: SshStatus
    daemon_detected: bool
    persistence_detected: bool
    read_only_command_ok: bool
    reason: str = ""

    def public_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "daemon_detected": self.daemon_detected,
            "persistence_detected": self.persistence_detected,
            "read_only_command_ok": self.read_only_command_ok,
            "reason": self.reason,
        }


def parse_ssh_state(
    facts: DeviceFacts,
    *,
    port_reachable: bool,
    operation_ok: bool,
    output: str = "",
    after_reboot: bool = False,
) -> VerificationResult:
    if not port_reachable:
        return VerificationResult(SshStatus.SSH_DISABLED, False, False, False, "SSH port is closed")
    if not operation_ok:
        return VerificationResult(SshStatus.SSH_AUTH_FAILED, False, False, False, "SSH authentication or command failed")
    normalized = str(output or "").lower()
    daemon = "sshd" in normalized or "dropbear" in normalized
    persistence = "/mnt/nv/remote_services" in normalized or "/mnt/nv/rc.local" in normalized
    if after_reboot:
        return VerificationResult(
            SshStatus.SSH_VERIFIED_AFTER_REBOOT if daemon else SshStatus.SSH_SERVICE_FAILED,
            daemon,
            persistence,
            True,
            "read-only SSH command completed after reboot",
        )
    if daemon and persistence:
        status = SshStatus.SSH_PERSISTENTLY_ENABLED
    elif daemon:
        status = SshStatus.SSH_TEMPORARILY_ENABLED
    else:
        status = SshStatus.SSH_REACHABLE
    return VerificationResult(status, daemon, persistence, True, "read-only SSH command completed")
