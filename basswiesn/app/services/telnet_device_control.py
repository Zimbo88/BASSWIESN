from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import socket
import time
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings, is_safe_radio_host
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.models import Device, DeviceInteraction, TelnetDeviceProfile, TelnetJob, utc_now
from basswiesn.app.services.device_interactions import InteractionPriority
from basswiesn.app.services.action_journal import record_action
from basswiesn.app.services.device_policy import device_lock, policy_for_device
from basswiesn.app.services.events import create_event
from basswiesn.app.services.model_library import resolve_device_model
from basswiesn.app.services.network_security import validate_outbound_host
from basswiesn.app.services.protected_devices import is_protected_device, is_protected_ip, reject_protected_device_access, require_unprotected_device


REBOOT_CONFIRMATION = "BASSWIESN TELNET REBOOT"
ACTIVE_JOB_STATES = {"pending", "connecting", "command_sent", "waiting_offline", "waiting_online", "verifying"}


@dataclass(frozen=True)
class CommandProfile:
    profile_key: str
    model_family: str
    firmware_family: str
    command_port: int
    telnet_reboot_supported: bool
    standby_clock_recovery_supported: bool
    reboot_command_key: str
    commands: dict[str, str]
    evidence: str
    limitations: str


BUILTIN_PROFILES: dict[str, CommandProfile] = {
    "unknown_unsupported": CommandProfile(
        profile_key="unknown_unsupported",
        model_family="unknown",
        firmware_family="unknown",
        command_port=23,
        telnet_reboot_supported=False,
        standby_clock_recovery_supported=False,
        reboot_command_key="",
        commands={},
        evidence="Sicherer Fallback fuer unbekannte Firmware.",
        limitations="Keine Schreibaktion erlaubt.",
    ),
    "wave_soundtouch_iv_fw27_cli17000": CommandProfile(
        profile_key="wave_soundtouch_iv_fw27_cli17000",
        model_family="Wave SoundTouch",
        firmware_family="27.0.x",
        command_port=17000,
        telnet_reboot_supported=True,
        standby_clock_recovery_supported=False,
        reboot_command_key="sys_reboot",
        commands={"sys_reboot": "sys reboot"},
        evidence="Funktional aus lokalen SoundTouch-Firmwarehinweisen und eigenen Toolkit-Notizen abgeleitet.",
        limitations="Unverschluesselt; nur manuell und nur nach Profil-/Allowlist-Pruefung.",
    ),
}


def _json_loads(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _profile_from_row(row: TelnetDeviceProfile | None) -> CommandProfile:
    if row is None:
        return BUILTIN_PROFILES["unknown_unsupported"]
    return CommandProfile(
        profile_key=row.profile_key,
        model_family=row.model_family,
        firmware_family=row.firmware_family,
        command_port=int(row.command_port or 23),
        telnet_reboot_supported=bool(row.telnet_reboot_supported),
        standby_clock_recovery_supported=bool(row.standby_clock_recovery_supported),
        reboot_command_key=row.reboot_command_key or "",
        commands={key: str(value) for key, value in _json_loads(row.commands_json).items()},
        evidence=row.evidence or "",
        limitations=row.limitations or "",
    )


def _model_text(device: Device) -> str:
    return " ".join(
        part for part in (
            getattr(device, "model", ""),
            getattr(device, "name", ""),
            getattr(device, "firmware", ""),
            getattr(device, "info_xml", ""),
        )
        if part
    ).lower()


def _select_profile_key(device: Device) -> str:
    text = _model_text(device)
    firmware = str(getattr(device, "firmware", "") or "").lower()
    if "wave" in text and ("27." in firmware or "27.0" in text):
        return "wave_soundtouch_iv_fw27_cli17000"
    return "unknown_unsupported"


def select_telnet_profile(db: Session, device: Device) -> CommandProfile:
    key = _select_profile_key(device)
    row = db.query(TelnetDeviceProfile).filter(TelnetDeviceProfile.profile_key == key).one_or_none()
    if row is not None:
        return _profile_from_row(row)
    return BUILTIN_PROFILES.get(key, BUILTIN_PROFILES["unknown_unsupported"])


def _password_configured() -> bool:
    path = get_settings().telnet_password_file.strip()
    return bool(path)


def _allowed_by_config(device: Device) -> tuple[bool, str]:
    settings = get_settings()
    if is_protected_device(device):
        return False, "device is protected by PROTECTED_DEVICE_IPS"
    if not settings.telnet_enabled:
        return False, "BASSWIESN_TELNET_ENABLED=false"
    if not device.ip_address or not is_safe_radio_host(device.ip_address):
        return False, "device target is not a validated local radio host"
    allowed_ids = {item.upper() for item in settings.telnet_allowed_device_ids}
    if allowed_ids and device.device_id.upper() not in allowed_ids:
        return False, "device_id is not in BASSWIESN_TELNET_ALLOWED_DEVICE_IDS"
    return True, ""


def telnet_capabilities(db: Session, device: Device) -> dict[str, Any]:
    profile = select_telnet_profile(db, device)
    model = resolve_device_model(device, db)
    config_ok, config_reason = _allowed_by_config(device)
    command = profile.commands.get(profile.reboot_command_key, "")
    profile_supported = bool(profile.telnet_reboot_supported and command)
    return {
        "device_id": device.device_id,
        "device_name": device.name,
        "ip_address": device.ip_address,
        "model": device.model,
        "firmware": device.firmware,
        "model_resolution": model.to_dict(),
        "enabled": get_settings().telnet_enabled,
        "supported": bool(config_ok and profile_supported),
        "profile_supported": profile_supported,
        "profile_key": profile.profile_key,
        "model_family": profile.model_family,
        "firmware_family": profile.firmware_family,
        "command_port": profile.command_port or get_settings().telnet_port,
        "action": "reboot",
        "command_key": profile.reboot_command_key,
        "command_label": "sys reboot" if profile.reboot_command_key else "",
        "requires_confirmation": REBOOT_CONFIRMATION,
        "credentials_configured": _password_configured() or bool(get_settings().telnet_username),
        "reason": "" if config_ok and profile_supported else (config_reason or "firmware profile does not support a validated reboot command"),
        "warnings": [
            "Telnet ist unverschluesselt.",
            "Es gibt keine allgemeine Telnet-Konsole; BASSWIESN sendet nur feste profilbasierte Befehle.",
            "Erfolg wird erst nach Offline-/Online- und Health-Readiness-Pruefung als bestaetigt betrachtet.",
        ],
        "evidence": profile.evidence,
        "limitations": profile.limitations,
    }


def _redact(text: object) -> str:
    value = str(text or "")
    for token in ("password", "passwd", "secret", "token", "authorization", "cookie"):
        value = value.replace(token, f"{token[:2]}***")
    return " ".join(value.split())[:1000]


def _record_interaction(
    db: Session,
    device: Device,
    *,
    correlation_id: str,
    endpoint: str,
    started_at: datetime,
    duration_ms: int,
    result: str,
    error_class: str = "",
    lock_wait_ms: int = 0,
) -> None:
    policy = policy_for_device(device, db)
    event_id = str(uuid4())
    db.add(DeviceInteraction(
        event_id=event_id,
        correlation_id=correlation_id,
        device_id=device.device_id,
        device_name=device.name,
        device_class=policy.device_class.value,
        ip_address=device.ip_address,
        request_purpose="telnet_reboot",
        requester="telnet_device_control",
        priority=int(InteractionPriority.USER_ACTION),
        method="TELNET",
        endpoint=endpoint,
        started_at=started_at,
        ended_at=datetime.now(UTC),
        duration_ms=duration_ms,
        timeout_seconds=get_settings().telnet_timeout_seconds,
        attempt=1,
        result=result,
        status_code=0,
        error_class=error_class,
        polling_profile=policy.polling_profile.value,
        safe_mode_state=str(policy.safe_mode_active).lower(),
        circuit_breaker_state=policy.circuit_state.value,
        lock_wait_ms=lock_wait_ms,
        cache_hit=False,
        skipped=False,
        skip_reason="",
    ))
    write_masterlog(
        "device_interaction_persisted",
        event_id=event_id,
        correlation_id=correlation_id,
        device_id=device.device_id,
        device_name=device.name,
        device_class=policy.device_class.value,
        radio_ip=device.ip_address,
        request_purpose="telnet_reboot",
        requester="telnet_device_control",
        priority=int(InteractionPriority.USER_ACTION),
        method="TELNET",
        endpoint=endpoint,
        started_at=started_at.isoformat(),
        duration_ms=duration_ms,
        timeout=get_settings().telnet_timeout_seconds,
        retry_number=1,
        result=result,
        status_code=0,
        error_class=error_class,
        polling_profile=policy.polling_profile.value,
        safe_mode_active=str(policy.safe_mode_active).lower(),
        circuit_breaker_state=policy.circuit_state.value,
        lock_wait_ms=lock_wait_ms,
        cache_hit=False,
        skipped=False,
        skip_reason="",
    )


async def _send_telnet_command(host: str, port: int, command: str, timeout_seconds: int) -> str:
    validation = validate_outbound_host(host, port=port)
    if not validation.ok:
        raise PermissionError(validation.reason)
    target = validation.addresses[0]
    reject_protected_device_access(target, action="telnet command", requester="telnet_device_control", method="TELNET", endpoint=str(port))
    if any(token in command for token in ("\x00", "\n", "\r", "`", "$(", "&&", "||", ";")):
        raise ValueError("command profile contains unsupported control syntax")
    reader, writer = await asyncio.wait_for(asyncio.open_connection(target, port), timeout=timeout_seconds)
    chunks: list[bytes] = []
    try:
        try:
            chunk = await asyncio.wait_for(reader.read(1024), timeout=0.25)
            if chunk:
                chunks.append(chunk)
        except asyncio.TimeoutError:
            pass
        writer.write((command + "\r\n").encode("ascii"))
        await asyncio.wait_for(writer.drain(), timeout=timeout_seconds)
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=0.75)
            if chunk:
                chunks.append(chunk)
        except asyncio.TimeoutError:
            pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return _redact(b"".join(chunks).decode("utf-8", errors="replace"))


def _port_open(host: str, port: int, timeout: float = 0.75) -> bool:
    if is_protected_ip(host):
        return False
    validation = validate_outbound_host(host, port=port)
    if not validation.ok:
        return False
    try:
        with socket.create_connection((validation.addresses[0], port), timeout=timeout):
            return True
    except OSError:
        return False


def _job_to_dict(job: TelnetJob) -> dict[str, Any]:
    try:
        result = json.loads(job.result_json or "{}")
    except ValueError:
        result = {}
    return {
        "job_id": job.job_id,
        "device_id": job.device_id,
        "action": job.action,
        "status": job.status,
        "profile_key": job.profile_key,
        "command_key": job.command_key,
        "command_port": job.command_port,
        "correlation_id": job.correlation_id,
        "started_at": job.started_at.isoformat() if job.started_at else "",
        "updated_at": job.updated_at.isoformat() if job.updated_at else "",
        "finished_at": job.finished_at.isoformat() if job.finished_at else "",
        "timeout_seconds": job.timeout_seconds,
        "wait_seconds": job.wait_seconds,
        "result": result,
        "error": job.error,
        "warning": "Telnet ist unverschluesselt; keine Zugangsdaten werden im Job gespeichert.",
    }


async def start_telnet_reboot(db: Session, device: Device, *, confirmation: str) -> dict[str, Any]:
    if str(confirmation or "").strip() != REBOOT_CONFIRMATION:
        raise PermissionError(f"confirmation required: {REBOOT_CONFIRMATION}")
    require_unprotected_device(device, action="telnet_reboot", requester="telnet_device_control", method="TELNET", endpoint="reboot")
    active = (
        db.query(TelnetJob)
        .filter(TelnetJob.device_id == device.device_id, TelnetJob.action == "reboot", TelnetJob.status.in_(ACTIVE_JOB_STATES))
        .one_or_none()
    )
    if active is not None:
        raise PermissionError("telnet reboot job is already active for this device")
    caps = telnet_capabilities(db, device)
    if not caps["supported"]:
        raise PermissionError(caps["reason"] or "telnet reboot unsupported")
    profile = select_telnet_profile(db, device)
    command = profile.commands.get(profile.reboot_command_key, "")
    if not command:
        raise PermissionError("no validated reboot command for this profile")
    job = TelnetJob(
        job_id=str(uuid4()),
        device_id=device.device_id,
        action="reboot",
        status="connecting",
        profile_key=profile.profile_key,
        command_key=profile.reboot_command_key,
        command_port=profile.command_port,
        correlation_id=str(uuid4()),
        timeout_seconds=get_settings().telnet_timeout_seconds,
        wait_seconds=get_settings().telnet_reboot_wait_seconds,
        result_json=json.dumps({"command_sent": False}, ensure_ascii=False),
    )
    db.add(job)
    db.flush()
    create_event(db, "telnet_reboot_started", device_id=device.device_id, correlation_id=job.correlation_id, payload={"job_id": job.job_id, "profile_key": profile.profile_key, "port": profile.command_port})
    started_at = datetime.now(UTC)
    started = time.monotonic()
    lock_started = time.monotonic()
    lock = device_lock(device.device_id)
    async with lock:
        lock_wait_ms = int((time.monotonic() - lock_started) * 1000)
        try:
            output = await _send_telnet_command(device.ip_address, profile.command_port, command, get_settings().telnet_timeout_seconds)
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            job.status = "failed"
            job.error = _redact(exc)
            job.updated_at = utc_now()
            job.finished_at = utc_now()
            job.result_json = json.dumps({"command_sent": False}, ensure_ascii=False)
            _record_interaction(db, device, correlation_id=job.correlation_id, endpoint=profile.reboot_command_key, started_at=started_at, duration_ms=duration_ms, result="error", error_class=exc.__class__.__name__, lock_wait_ms=lock_wait_ms)
            record_action(
                db,
                job_id=job.job_id,
                device_id=device.device_id,
                ip_address=device.ip_address,
                action="TELNET reboot",
                trigger="user",
                phase="command_failed",
                requested_state={"operation": profile.reboot_command_key},
                result="failed",
                duration_ms=duration_ms,
                error_category=exc.__class__.__name__,
                verified=False,
            )
            create_event(db, "telnet_reboot_failed", device_id=device.device_id, correlation_id=job.correlation_id, severity="warning", payload={"job_id": job.job_id, "error_class": exc.__class__.__name__})
            return _job_to_dict(job)
    duration_ms = int((time.monotonic() - started) * 1000)
    job.status = "waiting_offline"
    job.updated_at = utc_now()
    job.result_json = json.dumps(
        {
            "command_sent": True,
            "response_preview": output,
            "next_step": "waiting for device to leave port 8090 and return",
            "deadline_at": (datetime.now(UTC) + timedelta(seconds=job.wait_seconds)).isoformat(),
        },
        ensure_ascii=False,
    )
    _record_interaction(db, device, correlation_id=job.correlation_id, endpoint=profile.reboot_command_key, started_at=started_at, duration_ms=duration_ms, result="ok", lock_wait_ms=lock_wait_ms)
    record_action(
        db,
        job_id=job.job_id,
        device_id=device.device_id,
        ip_address=device.ip_address,
        action="TELNET reboot",
        trigger="user",
        phase="command_sent",
        requested_state={"operation": profile.reboot_command_key},
        result="command_sent",
        duration_ms=duration_ms,
        verified=False,
    )
    return _job_to_dict(job)


def poll_telnet_job(db: Session, job_id: str) -> dict[str, Any]:
    job = db.query(TelnetJob).filter(TelnetJob.job_id == job_id).one_or_none()
    if job is None:
        raise ValueError("telnet job not found")
    device = db.query(Device).filter(Device.device_id == job.device_id).one_or_none()
    if device is None:
        return _job_to_dict(job)
    now = datetime.now(UTC)
    deadline = (job.started_at or now) + timedelta(seconds=max(job.wait_seconds, 1))
    if job.status in {"waiting_offline", "waiting_online", "verifying"} and now > deadline:
        job.status = "timed_out"
        job.error = "device did not complete reboot window"
        job.finished_at = utc_now()
        job.updated_at = utc_now()
        record_action(
            db,
            job_id=job.job_id,
            device_id=device.device_id,
            ip_address=device.ip_address,
            action="TELNET reboot readback",
            trigger="job",
            phase="verification_failed",
            requested_state={"operation": job.command_key},
            result="timed_out",
            error_category="VerificationTimeout",
            verified=False,
        )
        create_event(db, "telnet_reboot_failed", device_id=job.device_id, correlation_id=job.correlation_id, severity="warning", payload={"job_id": job.job_id, "reason": "timeout"})
    elif job.status == "waiting_offline":
        if not _port_open(device.ip_address, get_settings().radio_port):
            job.status = "waiting_online"
            job.updated_at = utc_now()
    elif job.status == "waiting_online":
        if _port_open(device.ip_address, get_settings().radio_port):
            job.status = "verifying"
            job.updated_at = utc_now()
    if job.status == "verifying":
        job.status = "succeeded"
        job.finished_at = utc_now()
        job.updated_at = utc_now()
        result = _json_loads(job.result_json)
        result["verified"] = True
        result["verified_at"] = job.updated_at.isoformat()
        job.result_json = json.dumps(result, ensure_ascii=False)
        record_action(
            db,
            job_id=job.job_id,
            device_id=device.device_id,
            ip_address=device.ip_address,
            action="TELNET reboot readback",
            trigger="job",
            phase="verified",
            requested_state={"operation": job.command_key},
            result="device_offline_then_online",
            readback={"radio_port_reachable": True},
            verified=True,
        )
        create_event(db, "telnet_reboot_completed", device_id=job.device_id, correlation_id=job.correlation_id, payload={"job_id": job.job_id, "status": job.status})
    return _job_to_dict(job)
