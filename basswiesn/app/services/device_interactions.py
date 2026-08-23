from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum
import json
import time
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.models import Device, DeviceInteraction, RuntimeState, utc_now
from basswiesn.app.services.device_policy import device_lock, policy_for_device
from basswiesn.app.services.protected_devices import is_protected_device, log_protected_block


class InteractionPriority(IntEnum):
    USER_ACTION = 10
    SAFETY = 20
    MULTIROOM = 30
    SETUP = 40
    PRESET_READBACK = 50
    HEALTHCHECK = 60
    KEEPALIVE = 70
    STATISTICS = 80
    BACKGROUND_DISCOVERY = 90


SECRET_KEYS = ("password", "token", "secret", "authorization", "cookie", "private_key", "ssh_key", "api_key")


@dataclass(frozen=True)
class InteractionResult:
    ok: bool
    event_id: str
    correlation_id: str
    device_id: str
    endpoint: str
    method: str
    status_code: int
    duration_ms: int
    payload: str = ""
    error_class: str = ""
    error: str = ""
    skipped: bool = False
    skip_reason: str = ""
    cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "event_id": self.event_id,
            "correlation_id": self.correlation_id,
            "device_id": self.device_id,
            "endpoint": self.endpoint,
            "method": self.method,
            "status_code": self.status_code,
            "duration_ms": self.duration_ms,
            "payload": self.payload,
            "error_class": self.error_class,
            "error": self.error,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "cache_hit": self.cache_hit,
        }


def _redact(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    for key in SECRET_KEYS:
        text = text.replace(key, f"{key[:2]}***")
    return " ".join(text.split())[:limit]


def _runtime_row(db: Session, key: str) -> RuntimeState:
    row = db.query(RuntimeState).filter(RuntimeState.key == key).one_or_none()
    if row is None:
        row = RuntimeState(key=key, value="")
        db.add(row)
        db.flush()
    return row


def _cache_get(db: Session, key: str) -> str | None:
    row = db.query(RuntimeState).filter(RuntimeState.key == key).one_or_none()
    if row is None or not row.value:
        return None
    try:
        payload = json.loads(row.value)
        expires_at = datetime.fromisoformat(payload.get("expires_at", ""))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        return None
    return str(payload.get("payload", ""))


def _cache_set(db: Session, key: str, payload: str, ttl_seconds: int) -> None:
    row = _runtime_row(db, key)
    row.value = json.dumps({
        "payload": payload,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(),
    })
    row.updated_at = utc_now()


def _record(
    db: Session,
    *,
    event_id: str,
    correlation_id: str,
    device: Device,
    request_purpose: str,
    requester: str,
    priority: InteractionPriority,
    method: str,
    endpoint: str,
    started_at: datetime,
    ended_at: datetime,
    duration_ms: int,
    timeout_seconds: int,
    attempt: int,
    result: str,
    status_code: int = 0,
    error_class: str = "",
    polling_profile: str = "",
    device_class: str = "",
    safe_mode_state: str = "",
    circuit_breaker_state: str = "",
    lock_wait_ms: int = 0,
    cache_hit: bool = False,
    skipped: bool = False,
    skip_reason: str = "",
) -> None:
    row = DeviceInteraction(
        event_id=event_id,
        correlation_id=correlation_id,
        device_id=device.device_id,
        device_name=device.name,
        device_class=device_class,
        ip_address=device.ip_address,
        request_purpose=request_purpose,
        requester=requester,
        priority=int(priority),
        method=method,
        endpoint=endpoint,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        timeout_seconds=int(timeout_seconds),
        attempt=attempt,
        result=result,
        status_code=status_code,
        error_class=error_class,
        polling_profile=polling_profile,
        safe_mode_state=safe_mode_state,
        circuit_breaker_state=circuit_breaker_state,
        lock_wait_ms=lock_wait_ms,
        cache_hit=cache_hit,
        skipped=skipped,
        skip_reason=skip_reason,
    )
    db.add(row)
    write_masterlog(
        "device_interaction_persisted",
        event_id=event_id,
        correlation_id=correlation_id,
        device_id=device.device_id,
        device_name=device.name,
        device_class=device_class,
        radio_ip=device.ip_address,
        request_purpose=request_purpose,
        requester=requester,
        priority=int(priority),
        method=method,
        endpoint=endpoint,
        started_at=started_at.isoformat(),
        duration_ms=duration_ms,
        timeout=timeout_seconds,
        retry_number=attempt,
        result=result,
        status_code=status_code,
        error_class=error_class,
        polling_profile=polling_profile,
        safe_mode_active=safe_mode_state,
        circuit_breaker_state=circuit_breaker_state,
        lock_wait_ms=lock_wait_ms,
        cache_hit=cache_hit,
        skipped=skipped,
        skip_reason=skip_reason,
    )


class DeviceInteractionCoordinator:
    async def request_xml(
        self,
        db: Session,
        device: Device,
        endpoint: str,
        *,
        method: str = "GET",
        body: str = "",
        request_purpose: str,
        requester: str,
        priority: InteractionPriority = InteractionPriority.USER_ACTION,
        timeout_seconds: int = 5,
        retry_budget: int = 0,
        cache_ttl_seconds: int = 0,
        correlation_id: str | None = None,
        allow_safe_mode_skip: bool = False,
    ) -> InteractionResult:
        event_id = str(uuid4())
        correlation = correlation_id or str(uuid4())
        started_at = datetime.now(UTC)
        started = time.monotonic()
        policy = policy_for_device(device, db)
        method = method.upper()
        if is_protected_device(device):
            ended = datetime.now(UTC)
            duration_ms = int((time.monotonic() - started) * 1000)
            log_protected_block(
                device,
                action=request_purpose,
                requester=requester,
                method=method,
                endpoint=endpoint,
            )
            _record(
                db,
                event_id=event_id,
                correlation_id=correlation,
                device=device,
                request_purpose=request_purpose,
                requester=requester,
                priority=priority,
                method=method,
                endpoint=endpoint,
                started_at=started_at,
                ended_at=ended,
                duration_ms=duration_ms,
                timeout_seconds=timeout_seconds,
                attempt=0,
                result="blocked",
                status_code=403,
                error_class="ProtectedDevice",
                polling_profile=policy.polling_profile.value,
                device_class=policy.device_class.value,
                safe_mode_state=str(policy.safe_mode_active).lower(),
                circuit_breaker_state=policy.circuit_state.value,
                skipped=True,
                skip_reason="fully protected device access blocked",
            )
            return InteractionResult(
                False,
                event_id,
                correlation,
                device.device_id,
                endpoint,
                method,
                403,
                duration_ms,
                error_class="ProtectedDevice",
                error="fully protected device access blocked",
                skipped=True,
                skip_reason="fully protected device access blocked",
            )
        safe_block = bool(allow_safe_mode_skip and policy.safe_mode_active)
        if safe_block:
            ended = datetime.now(UTC)
            duration_ms = int((time.monotonic() - started) * 1000)
            _record(
                db,
                event_id=event_id,
                correlation_id=correlation,
                device=device,
                request_purpose=request_purpose,
                requester=requester,
                priority=priority,
                method=method,
                endpoint=endpoint,
                started_at=started_at,
                ended_at=ended,
                duration_ms=duration_ms,
                timeout_seconds=timeout_seconds,
                attempt=0,
                result="skipped",
                polling_profile=policy.polling_profile.value,
                device_class=policy.device_class.value,
                safe_mode_state=str(policy.safe_mode_active).lower(),
                circuit_breaker_state=policy.circuit_state.value,
                skipped=True,
                skip_reason="device safe mode blocks automatic diagnostic access",
            )
            return InteractionResult(False, event_id, correlation, device.device_id, endpoint, method, 0, duration_ms, skipped=True, skip_reason="device safe mode blocks automatic diagnostic access")

        cache_key = f"device_interaction_cache:{device.device_id}:{method}:{endpoint}"
        if method == "GET" and cache_ttl_seconds > 0:
            cached = _cache_get(db, cache_key)
            if cached is not None:
                ended = datetime.now(UTC)
                duration_ms = int((time.monotonic() - started) * 1000)
                _record(
                    db,
                    event_id=event_id,
                    correlation_id=correlation,
                    device=device,
                    request_purpose=request_purpose,
                    requester=requester,
                    priority=priority,
                    method=method,
                    endpoint=endpoint,
                    started_at=started_at,
                    ended_at=ended,
                    duration_ms=duration_ms,
                    timeout_seconds=timeout_seconds,
                    attempt=0,
                    result="cache",
                    polling_profile=policy.polling_profile.value,
                    device_class=policy.device_class.value,
                    safe_mode_state=str(policy.safe_mode_active).lower(),
                    circuit_breaker_state=policy.circuit_state.value,
                    cache_hit=True,
                )
                return InteractionResult(True, event_id, correlation, device.device_id, endpoint, method, 200, duration_ms, cached, cache_hit=True)

        lock_wait_started = time.monotonic()
        lock = device_lock(device.device_id)
        async with lock:
            lock_wait_ms = int((time.monotonic() - lock_wait_started) * 1000)
            attempts = max(1, retry_budget + 1)
            last_error = ""
            last_error_class = ""
            last_status = 0
            for attempt in range(1, attempts + 1):
                request_started_at = datetime.now(UTC)
                request_started = time.monotonic()
                try:
                    client = SoundTouchClient(
                        device.ip_address,
                        get_timeout=float(timeout_seconds),
                        post_timeout=float(timeout_seconds),
                        device_id=device.device_id,
                        request_purpose=request_purpose,
                        trigger=requester,
                        retry_number=attempt,
                        policy_context=policy.to_dict(),
                    )
                    if method == "GET":
                        payload = await client.get_xml(endpoint)
                    elif method == "POST":
                        payload = await client.post_xml(endpoint, body)
                    else:
                        raise ValueError(f"Unsupported SoundTouch method: {method}")
                    duration_ms = int((time.monotonic() - request_started) * 1000)
                    if method == "GET" and cache_ttl_seconds > 0:
                        _cache_set(db, cache_key, payload, cache_ttl_seconds)
                    _record(
                        db,
                        event_id=event_id,
                        correlation_id=correlation,
                        device=device,
                        request_purpose=request_purpose,
                        requester=requester,
                        priority=priority,
                        method=method,
                        endpoint=endpoint,
                        started_at=request_started_at,
                        ended_at=datetime.now(UTC),
                        duration_ms=duration_ms,
                        timeout_seconds=timeout_seconds,
                        attempt=attempt,
                        result="ok",
                        status_code=200,
                        polling_profile=policy.polling_profile.value,
                        device_class=policy.device_class.value,
                        safe_mode_state=str(policy.safe_mode_active).lower(),
                        circuit_breaker_state=policy.circuit_state.value,
                        lock_wait_ms=lock_wait_ms,
                    )
                    return InteractionResult(True, event_id, correlation, device.device_id, endpoint, method, 200, duration_ms, payload)
                except httpx.HTTPStatusError as exc:
                    last_status = exc.response.status_code
                    last_error_class = exc.__class__.__name__
                    last_error = _redact(exc)
                except Exception as exc:
                    last_error_class = exc.__class__.__name__
                    last_error = _redact(exc)
                if attempt < attempts:
                    await asyncio.sleep(min(0.25 * attempt, 1.0))

            ended = datetime.now(UTC)
            duration_ms = int((time.monotonic() - started) * 1000)
            _record(
                db,
                event_id=event_id,
                correlation_id=correlation,
                device=device,
                request_purpose=request_purpose,
                requester=requester,
                priority=priority,
                method=method,
                endpoint=endpoint,
                started_at=started_at,
                ended_at=ended,
                duration_ms=duration_ms,
                timeout_seconds=timeout_seconds,
                attempt=attempts,
                result="error",
                status_code=last_status,
                error_class=last_error_class,
                polling_profile=policy.polling_profile.value,
                device_class=policy.device_class.value,
                safe_mode_state=str(policy.safe_mode_active).lower(),
                circuit_breaker_state=policy.circuit_state.value,
                lock_wait_ms=lock_wait_ms,
            )
            return InteractionResult(False, event_id, correlation, device.device_id, endpoint, method, last_status, duration_ms, error_class=last_error_class, error=last_error)


coordinator = DeviceInteractionCoordinator()
