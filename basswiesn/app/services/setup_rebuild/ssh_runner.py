"""Secure SSH runner for allowlisted internal operations.

The runner deliberately does not expose a generic command parameter. Callers
select an operation ID from profiles/activation.py. Credential values are
never returned or logged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Sequence

from basswiesn.app.config import get_settings
from basswiesn.app.services.action_journal import record_transport_attempt
from basswiesn.app.services.network_safety import assert_transport_allowed
from basswiesn.app.services.setup_rebuild.profiles.activation import (
    InternalOperation,
    get_operation,
)


@dataclass(frozen=True)
class SshConfig:
    username: str
    port: int
    timeout_seconds: int
    retry_count: int
    password_file: str
    private_key_file: str
    known_hosts_file: str
    host_key_policy: str

    @classmethod
    def from_settings(cls) -> "SshConfig":
        settings = get_settings()
        known_hosts = settings.ssh_known_hosts_file or str(
            settings.data_dir / "setup-rebuild" / "known_hosts"
        )
        return cls(
            username=settings.ssh_username,
            port=settings.ssh_port,
            timeout_seconds=settings.ssh_timeout_seconds,
            retry_count=settings.ssh_retry_count,
            password_file=settings.ssh_password_file,
            private_key_file=settings.ssh_private_key_file,
            known_hosts_file=known_hosts,
            host_key_policy=settings.ssh_host_key_policy,
        )

    def public_dict(self) -> dict[str, object]:
        if self.private_key_file:
            credential_mode = "private_key"
        elif self.password_file:
            credential_mode = "password_file"
        else:
            credential_mode = "none"
        return {
            "username": self.username,
            "port": self.port,
            "timeout_seconds": self.timeout_seconds,
            "retry_count": self.retry_count,
            "host_key_policy": self.host_key_policy,
            "credential_mode": credential_mode,
            "credential_configured": bool(self.username and credential_mode != "none"),
        }


@dataclass(frozen=True)
class SshResult:
    status: str
    operation_id: str
    return_code: int | None = None
    output: str = ""
    error_class: str = ""
    duration_ms: int = 0

    def public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "operation_id": self.operation_id,
            "return_code": self.return_code,
            "output": self.output,
            "error_class": self.error_class,
            "duration_ms": self.duration_ms,
        }


def _safe_output(value: str, limit: int = 20000) -> str:
    text = " ".join(str(value or "").split())
    for marker in ("password", "passwd", "secret", "token", "authorization", "cookie"):
        text = text.replace(marker, f"{marker[:2]}***")
    return text[:limit]


def _credential_args(config: SshConfig) -> list[str]:
    args: list[str] = []
    if config.private_key_file:
        key = Path(config.private_key_file)
        if not key.is_file():
            raise FileNotFoundError("configured SSH private-key file is unavailable")
        args.extend(["-i", str(key)])
    elif config.password_file:
        password = Path(config.password_file)
        if not password.is_file():
            raise FileNotFoundError("configured SSH password file is unavailable")
        if shutil.which("sshpass") is None:
            raise RuntimeError("sshpass is required for password-file SSH authentication")
        args = ["sshpass", "-f", str(password)]
    return args


def _ssh_options(config: SshConfig) -> list[str]:
    if config.host_key_policy not in {"strict", "accept-new", "off"}:
        raise ValueError("unsupported SSH host-key policy")
    known_hosts = Path(config.known_hosts_file)
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    options = [
        "-p",
        str(config.port),
        "-o",
        "BatchMode=yes" if not config.password_file else "BatchMode=no",
        "-o",
        f"ConnectTimeout={config.timeout_seconds}",
        "-o",
        f"StrictHostKeyChecking={'yes' if config.host_key_policy == 'strict' else 'accept-new' if config.host_key_policy == 'accept-new' else 'no'}",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "LogLevel=ERROR",
    ]
    # Legacy Bose firmware can require these algorithms. The operation is
    # still protected by the managed host-key file and a fixed profile.
    options.extend(
        [
            "-o",
            "HostKeyAlgorithms=+ssh-rsa",
            "-o",
            "PubkeyAcceptedAlgorithms=+ssh-rsa",
            "-o",
            "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
            "-o",
            "Ciphers=+aes256-cbc,aes128-cbc",
        ]
    )
    return options


def build_internal_ssh_command(
    ip_address: str,
    device_id: str,
    operation_id: str,
    *,
    config: SshConfig | None = None,
    approved_only: bool = True,
) -> tuple[list[str], InternalOperation]:
    target = assert_transport_allowed(
        ip_address,
        device_id=device_id,
        transport="SSH",
        approved_only=approved_only,
    )
    config = config or SshConfig.from_settings()
    if not config.username:
        raise ValueError("SSH username is not configured")
    operation = get_operation(operation_id)
    command = _credential_args(config)
    command.append("ssh")
    command.extend(_ssh_options(config))
    command.extend([f"{config.username}@{target}", operation.command])
    return command, operation


async def probe_ssh_port(
    ip_address: str,
    *,
    device_id: str = "",
    port: int | None = None,
    timeout: float | None = None,
    approved_only: bool = True,
) -> bool:
    config = SshConfig.from_settings()
    target_port = port or config.port
    target = assert_transport_allowed(
        ip_address,
        device_id=device_id,
        transport="SSH port probe",
        approved_only=approved_only,
    )
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target, target_port),
            timeout=timeout or config.timeout_seconds,
        )
        del reader
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, TimeoutError, asyncio.TimeoutError):
        return False


async def run_internal_operation(
    ip_address: str,
    device_id: str,
    operation_id: str,
    *,
    config: SshConfig | None = None,
    approved_only: bool = True,
    output_limit: int = 20000,
    runner=subprocess.run,
) -> SshResult:
    config = config or SshConfig.from_settings()
    command, operation = build_internal_ssh_command(
        ip_address,
        device_id,
        operation_id,
        config=config,
        approved_only=approved_only,
    )
    attempts = max(0, config.retry_count) + 1
    last_error: BaseException | None = None
    for attempt in range(attempts):
        started = time.monotonic()
        try:
            completed = await asyncio.to_thread(
                runner,
                command,
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds + 4,
                check=False,
                env={**os.environ, "LC_ALL": "C", "LANG": "C"},
            )
            if completed.returncode == 0:
                duration_ms = int((time.monotonic() - started) * 1000)
                result = SshResult(
                    status="SSH_REACHABLE",
                    operation_id=operation.operation_id,
                    return_code=0,
                    output=_safe_output(completed.stdout, output_limit),
                    duration_ms=duration_ms,
                )
                if not operation.read_only:
                    record_transport_attempt(
                        ip_address=ip_address,
                        device_id=device_id,
                        action=f"SSH {operation.operation_id}",
                        trigger="setup_rebuild",
                        requested_state={
                            "operation_id": operation.operation_id,
                            "purpose": operation.purpose,
                            "attempt": attempt + 1,
                        },
                        result="success",
                        duration_ms=duration_ms,
                    )
                return result
            duration_ms = int((time.monotonic() - started) * 1000)
            result = SshResult(
                status="SSH_AUTH_FAILED" if completed.returncode == 255 else "SSH_SERVICE_FAILED",
                operation_id=operation.operation_id,
                return_code=completed.returncode,
                output=_safe_output(completed.stderr, output_limit),
                duration_ms=duration_ms,
            )
            if not operation.read_only:
                record_transport_attempt(
                    ip_address=ip_address,
                    device_id=device_id,
                    action=f"SSH {operation.operation_id}",
                    trigger="setup_rebuild",
                    requested_state={
                        "operation_id": operation.operation_id,
                        "purpose": operation.purpose,
                        "attempt": attempt + 1,
                    },
                    result="failed",
                    duration_ms=duration_ms,
                    error_category=result.status,
                )
            return result
        except FileNotFoundError as exc:
            last_error = exc
            if not operation.read_only:
                record_transport_attempt(
                    ip_address=ip_address,
                    device_id=device_id,
                    action=f"SSH {operation.operation_id}",
                    trigger="setup_rebuild",
                    requested_state={
                        "operation_id": operation.operation_id,
                        "purpose": operation.purpose,
                        "attempt": attempt + 1,
                    },
                    result="failed",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_category=exc.__class__.__name__,
                )
            break
        except subprocess.TimeoutExpired as exc:
            last_error = exc
            if not operation.read_only:
                record_transport_attempt(
                    ip_address=ip_address,
                    device_id=device_id,
                    action=f"SSH {operation.operation_id}",
                    trigger="setup_rebuild",
                    requested_state={
                        "operation_id": operation.operation_id,
                        "purpose": operation.purpose,
                        "attempt": attempt + 1,
                    },
                    result="failed",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_category=exc.__class__.__name__,
                )
    return SshResult(
        status="SSH_AUTH_FAILED" if isinstance(last_error, FileNotFoundError) else "SSH_SERVICE_FAILED",
        operation_id=operation.operation_id,
        error_class=last_error.__class__.__name__ if last_error else "UnknownError",
    )
