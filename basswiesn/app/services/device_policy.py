from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import Device
from basswiesn.app.services.device_state import load_runtime_state


class DeviceClass(str, Enum):
    PORTABLE = "portable"
    STATIONARY = "stationary"
    UNKNOWN = "unknown"


class SafeModeSetting(str, Enum):
    AUTO = "auto"
    ALWAYS = "always"
    DISABLED = "disabled"


class PollingProfile(str, Enum):
    AUTO = "auto"
    ACTIVE_PLAYBACK = "active_playback"
    IDLE_ONLINE = "idle_online"
    STANDBY = "standby"
    PORTABLE_BATTERY = "portable_battery"
    OFFLINE_BACKOFF = "offline_backoff"
    SETUP = "setup"
    MANUAL_DIAGNOSTICS = "manual_diagnostics"


class CircuitState(str, Enum):
    CLOSED = "closed"
    HALF_OPEN = "half_open"
    OPEN = "open"


@dataclass(frozen=True)
class DeviceCapabilities:
    battery: bool | None
    multiroom: bool | None
    clock_display: bool | None
    source: str


@dataclass(frozen=True)
class DeviceInteractionPolicy:
    device_id: str
    device_class: DeviceClass
    safe_mode_active: bool
    safe_mode_setting: SafeModeSetting
    polling_profile: PollingProfile
    circuit_state: CircuitState
    failure_count: int
    backoff_seconds: int
    next_retry_at: str
    allow_auto_wakeup: bool
    allow_automatic_power_key: bool
    allow_preset_restore: bool
    allow_invalid_source_recovery: bool
    allow_battery_poll: bool
    allow_maintenance_actions: bool
    low_risk_probe_interval_seconds: int
    skip_reason: str
    suspected_state: str
    capabilities: DeviceCapabilities

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["device_class"] = self.device_class.value
        data["safe_mode_setting"] = self.safe_mode_setting.value
        data["polling_profile"] = self.polling_profile.value
        data["circuit_state"] = self.circuit_state.value
        return data


@dataclass(frozen=True)
class PollDecision:
    allowed: bool
    profile: PollingProfile
    reason: str
    low_risk_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "profile": self.profile.value,
            "reason": self.reason,
            "low_risk_only": self.low_risk_only,
        }


_locks: dict[str, asyncio.Lock] = {}


def _clean(value: object, default: str = "") -> str:
    text = str(value or "").strip().lower()
    return text or default


def _parse_safe_mode(value: object) -> SafeModeSetting:
    text = _clean(value, SafeModeSetting.AUTO.value)
    aliases = {
        "true": SafeModeSetting.ALWAYS,
        "on": SafeModeSetting.ALWAYS,
        "always_on": SafeModeSetting.ALWAYS,
        "forced": SafeModeSetting.ALWAYS,
        "false": SafeModeSetting.DISABLED,
        "off": SafeModeSetting.DISABLED,
        "never": SafeModeSetting.DISABLED,
    }
    if text in aliases:
        return aliases[text]
    try:
        return SafeModeSetting(text)
    except ValueError:
        return SafeModeSetting.AUTO


def _parse_profile(value: object) -> PollingProfile:
    text = _clean(value, PollingProfile.AUTO.value)
    try:
        return PollingProfile(text)
    except ValueError:
        return PollingProfile.AUTO


def _combined_device_text(device: Device) -> str:
    return " ".join(
        part for part in (
            getattr(device, "model", ""),
            getattr(device, "name", ""),
            getattr(device, "info_xml", ""),
            getattr(device, "capabilities_xml", ""),
        )
        if part
    ).lower()


def classify_device(device: Device) -> DeviceClass:
    override = _clean(getattr(device, "device_class_override", "auto"), "auto")
    if override in {DeviceClass.PORTABLE.value, "portable"}:
        return DeviceClass.PORTABLE
    if override in {DeviceClass.STATIONARY.value, "stationary", "fixed"}:
        return DeviceClass.STATIONARY
    if override in {DeviceClass.UNKNOWN.value, "unknown"}:
        return DeviceClass.UNKNOWN

    text = _combined_device_text(device)
    if any(token in text for token in ("soundtouch portable", "portable", "taigan")):
        return DeviceClass.PORTABLE
    if any(token in text for token in (
        "soundtouch 10",
        "soundtouch 20",
        "soundtouch 30",
        "soundtouch 300",
        "wave soundtouch",
        "soundtouch wireless link",
        "soundtouch sa-",
        "cinemate",
        "lifestyle",
        "st10",
        "st20",
        "st30",
        "rhino",
        "spotty",
        "mojo",
        "ginger",
        "burns",
        "lisa",
    )):
        return DeviceClass.STATIONARY
    return DeviceClass.UNKNOWN


def capabilities_for_device(device: Device, device_class: DeviceClass | None = None) -> DeviceCapabilities:
    device_class = device_class or classify_device(device)
    text = _combined_device_text(device)
    battery = False
    multiroom = None if device_class == DeviceClass.UNKNOWN else True
    clock_display = True if any(token in text for token in ("portable", "soundtouch 20", "soundtouch 30", "wave")) else None
    return DeviceCapabilities(
        battery=battery,
        multiroom=multiroom,
        clock_display=clock_display,
        source="model_library" if device_class != DeviceClass.UNKNOWN else "unknown",
    )


def _failures(device: Device, runtime_state: dict[str, Any]) -> int:
    keepalive = runtime_state.get("playback_keepalive") or {}
    values = (
        keepalive.get("consecutive_failures"),
        keepalive.get("failure_count"),
        getattr(device, "failure_count", 0),
    )
    parsed = []
    for value in values:
        try:
            parsed.append(int(value or 0))
        except (TypeError, ValueError):
            parsed.append(0)
    return max(parsed or [0])


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def recommended_backoff_seconds(failures: int, device_class: DeviceClass) -> int:
    if failures <= 0:
        return 0
    buckets = (0, 0, 5 * 60, 15 * 60, 30 * 60, 60 * 60)
    return buckets[min(failures - 1, len(buckets) - 1)]


def _circuit_state(failures: int, device_class: DeviceClass, next_retry_at: datetime | None, now: datetime) -> CircuitState:
    threshold = 5
    if failures >= threshold:
        if next_retry_at and next_retry_at <= now:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN
    if failures > 0:
        return CircuitState.HALF_OPEN
    return CircuitState.CLOSED


def _runtime_profile(device: Device, runtime_state: dict[str, Any], circuit: CircuitState) -> PollingProfile:
    override = _parse_profile(getattr(device, "polling_profile_override", "auto"))
    if override != PollingProfile.AUTO:
        return override
    if circuit == CircuitState.OPEN:
        return PollingProfile.OFFLINE_BACKOFF
    source = str(runtime_state.get("current_source") or "").upper()
    playback_state = str(runtime_state.get("playback_state") or runtime_state.get("play_status") or "").upper()
    if source == "STANDBY":
        return PollingProfile.STANDBY
    if playback_state == "PLAY_STATE" and source not in {"", "STANDBY", "INVALID_SOURCE"}:
        return PollingProfile.ACTIVE_PLAYBACK
    if bool(getattr(device, "reachable", True)):
        return PollingProfile.IDLE_ONLINE
    return PollingProfile.OFFLINE_BACKOFF


def policy_for_device(
    device: Device,
    db: Session | None = None,
    *,
    runtime_state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> DeviceInteractionPolicy:
    now = now or datetime.now(UTC)
    if runtime_state is None and db is not None:
        _row, runtime_state = load_runtime_state(db, device.device_id)
    runtime_state = runtime_state or {}
    device_class = classify_device(device)
    safe_mode_setting = _parse_safe_mode(getattr(device, "safe_mode", "auto"))
    keepalive = runtime_state.get("playback_keepalive") or {}
    failures = _failures(device, runtime_state)
    next_retry_at = _parse_time(keepalive.get("next_retry_at"))
    circuit = _circuit_state(failures, device_class, next_retry_at, now)
    safe_mode_active = (
        safe_mode_setting == SafeModeSetting.ALWAYS
        or (
            safe_mode_setting == SafeModeSetting.AUTO
            and circuit == CircuitState.OPEN
        )
    )
    profile = _runtime_profile(device, runtime_state, circuit)
    settings = get_settings()
    low_risk_interval = settings.stationary_low_risk_interval_seconds
    if circuit == CircuitState.OPEN and not next_retry_at:
        next_retry_at = now + timedelta(seconds=recommended_backoff_seconds(max(failures, 1), device_class))
    allow_auto_restore_setting = getattr(device, "auto_restore_allowed", True) is not False
    allow_maintenance_setting = bool(getattr(device, "maintenance_actions_allowed", False))
    capabilities = capabilities_for_device(device, device_class)
    allow_auto_wakeup = not safe_mode_active and circuit != CircuitState.OPEN
    # INVALID_SOURCE requires evidence classification and an explicit bounded
    # recovery plan.  A background device policy must never replay a preset.
    allow_invalid_source_recovery = (
        not safe_mode_active and circuit != CircuitState.OPEN
    )
    allow_preset_restore = allow_auto_restore_setting and not safe_mode_active
    suspected_state = "online"
    skip_reason = ""
    if circuit == CircuitState.OPEN:
        suspected_state = "offline_or_unreachable"
        skip_reason = "circuit breaker open"
    elif not bool(getattr(device, "reachable", True)):
        suspected_state = "offline"
        skip_reason = "device currently marked unreachable"
    return DeviceInteractionPolicy(
        device_id=device.device_id,
        device_class=device_class,
        safe_mode_active=safe_mode_active,
        safe_mode_setting=safe_mode_setting,
        polling_profile=profile,
        circuit_state=circuit,
        failure_count=failures,
        backoff_seconds=recommended_backoff_seconds(failures, device_class),
        next_retry_at=next_retry_at.isoformat() if next_retry_at else "",
        allow_auto_wakeup=allow_auto_wakeup,
        allow_automatic_power_key=allow_auto_wakeup,
        allow_preset_restore=allow_preset_restore,
        allow_invalid_source_recovery=allow_invalid_source_recovery,
        allow_battery_poll=bool(
            getattr(device, "battery_poll_allowed", False)
            and capabilities.battery is True
            and not safe_mode_active
        ),
        allow_maintenance_actions=allow_maintenance_setting and not safe_mode_active,
        low_risk_probe_interval_seconds=low_risk_interval,
        skip_reason=skip_reason,
        suspected_state=suspected_state,
        capabilities=capabilities,
    )


def should_poll(
    policy: DeviceInteractionPolicy,
    *,
    now: datetime | None = None,
    last_probe_at: datetime | None = None,
) -> PollDecision:
    now = now or datetime.now(UTC)
    next_retry_at = _parse_time(policy.next_retry_at)
    if next_retry_at and now < next_retry_at:
        return PollDecision(False, policy.polling_profile, "backoff active", low_risk_only=True)
    if policy.safe_mode_active:
        return PollDecision(
            False,
            policy.polling_profile,
            "safe mode active",
            low_risk_only=True,
        )
    return PollDecision(True, policy.polling_profile, policy.skip_reason or "poll allowed", low_risk_only=policy.circuit_state == CircuitState.HALF_OPEN)


def device_lock(device_id: str) -> asyncio.Lock:
    if device_id not in _locks:
        _locks[device_id] = asyncio.Lock()
    return _locks[device_id]
