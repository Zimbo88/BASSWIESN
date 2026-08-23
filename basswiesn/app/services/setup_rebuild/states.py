"""Persistent setup and SSH state definitions.

This module contains only domain state and transition validation. Network
adapters are intentionally kept elsewhere so tests can exercise the state
machine without contacting hardware.
"""

from __future__ import annotations

from enum import StrEnum


class SetupState(StrEnum):
    UNKNOWN = "UNKNOWN"
    DISCOVERED = "DISCOVERED"
    IDENTIFIED = "IDENTIFIED"
    BACKUP_PENDING = "BACKUP_PENDING"
    BACKUP_COMPLETE = "BACKUP_COMPLETE"
    SSH_STATUS_PENDING = "SSH_STATUS_PENDING"
    SSH_ALREADY_ACTIVE = "SSH_ALREADY_ACTIVE"
    SSH_ACTIVATION_PENDING = "SSH_ACTIVATION_PENDING"
    SSH_TEMPORARY_ACTIVE = "SSH_TEMPORARY_ACTIVE"
    SSH_PERSISTENCE_PENDING = "SSH_PERSISTENCE_PENDING"
    SSH_REBOOT_PENDING = "SSH_REBOOT_PENDING"
    SSH_VERIFIED = "SSH_VERIFIED"
    ROUTING_BACKUP_COMPLETE = "ROUTING_BACKUP_COMPLETE"
    BASSWIESN_ROUTE_PENDING = "BASSWIESN_ROUTE_PENDING"
    BASSWIESN_ROUTE_ACTIVE = "BASSWIESN_ROUTE_ACTIVE"
    RADIO_REBOOT_PENDING = "RADIO_REBOOT_PENDING"
    RADIO_REBOOTED = "RADIO_REBOOTED"
    RADIO_RECONNECT_PENDING = "RADIO_RECONNECT_PENDING"
    RADIO_REACHABLE = "RADIO_REACHABLE"
    MARGE_ACCOUNT_PENDING = "MARGE_ACCOUNT_PENDING"
    MARGE_ACCOUNT_ACTIVE = "MARGE_ACCOUNT_ACTIVE"
    PRESETS_READABLE = "PRESETS_READABLE"
    PLAYBACK_READY = "PLAYBACK_READY"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    RECOVERY_PENDING = "RECOVERY_PENDING"
    ROLLBACK_PENDING = "ROLLBACK_PENDING"
    ROLLED_BACK = "ROLLED_BACK"


class MultiDeviceSetupPhase(StrEnum):
    QUEUED = "QUEUED"
    PREFLIGHT = "PREFLIGHT"
    BACKUP = "BACKUP"
    ROUTING = "ROUTING"
    ACCOUNT = "ACCOUNT"
    REBOOT = "REBOOT"
    RECONNECT = "RECONNECT"
    READBACK = "READBACK"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    ROLLBACK = "ROLLBACK"
    COMPLETE = "COMPLETE"


class SshStatus(StrEnum):
    SSH_UNKNOWN = "SSH_UNKNOWN"
    SSH_DISABLED = "SSH_DISABLED"
    SSH_TEMPORARILY_ENABLED = "SSH_TEMPORARILY_ENABLED"
    SSH_PERSISTENTLY_ENABLED = "SSH_PERSISTENTLY_ENABLED"
    SSH_REACHABLE = "SSH_REACHABLE"
    SSH_AUTH_FAILED = "SSH_AUTH_FAILED"
    SSH_SERVICE_FAILED = "SSH_SERVICE_FAILED"
    SSH_REBOOT_VERIFICATION_PENDING = "SSH_REBOOT_VERIFICATION_PENDING"
    SSH_VERIFIED_AFTER_REBOOT = "SSH_VERIFIED_AFTER_REBOOT"


TERMINAL_STATES = frozenset(
    {SetupState.VERIFIED, SetupState.FAILED, SetupState.ROLLED_BACK}
)

_ALLOWED_TRANSITIONS: dict[SetupState, frozenset[SetupState]] = {
    SetupState.UNKNOWN: frozenset({SetupState.DISCOVERED, SetupState.FAILED}),
    SetupState.DISCOVERED: frozenset({SetupState.IDENTIFIED, SetupState.FAILED}),
    SetupState.IDENTIFIED: frozenset({SetupState.BACKUP_PENDING, SetupState.FAILED}),
    SetupState.BACKUP_PENDING: frozenset({SetupState.BACKUP_COMPLETE, SetupState.FAILED}),
    SetupState.BACKUP_COMPLETE: frozenset(
        {
            SetupState.SSH_STATUS_PENDING,
            SetupState.ROUTING_BACKUP_COMPLETE,
            SetupState.FAILED,
        }
    ),
    SetupState.SSH_STATUS_PENDING: frozenset(
        {SetupState.SSH_ALREADY_ACTIVE, SetupState.SSH_ACTIVATION_PENDING, SetupState.FAILED}
    ),
    SetupState.SSH_ALREADY_ACTIVE: frozenset({SetupState.SSH_PERSISTENCE_PENDING, SetupState.FAILED}),
    SetupState.SSH_ACTIVATION_PENDING: frozenset({SetupState.SSH_TEMPORARY_ACTIVE, SetupState.FAILED}),
    SetupState.SSH_TEMPORARY_ACTIVE: frozenset({SetupState.SSH_PERSISTENCE_PENDING, SetupState.FAILED}),
    SetupState.SSH_PERSISTENCE_PENDING: frozenset({SetupState.SSH_REBOOT_PENDING, SetupState.FAILED}),
    SetupState.SSH_REBOOT_PENDING: frozenset({SetupState.SSH_VERIFIED, SetupState.FAILED}),
    SetupState.SSH_VERIFIED: frozenset({SetupState.ROUTING_BACKUP_COMPLETE, SetupState.FAILED}),
    SetupState.ROUTING_BACKUP_COMPLETE: frozenset({SetupState.BASSWIESN_ROUTE_PENDING, SetupState.FAILED}),
    SetupState.BASSWIESN_ROUTE_PENDING: frozenset({SetupState.BASSWIESN_ROUTE_ACTIVE, SetupState.FAILED}),
    SetupState.BASSWIESN_ROUTE_ACTIVE: frozenset({SetupState.RADIO_REBOOT_PENDING, SetupState.FAILED}),
    SetupState.RADIO_REBOOT_PENDING: frozenset({SetupState.RADIO_REBOOTED, SetupState.FAILED}),
    SetupState.RADIO_REBOOTED: frozenset({SetupState.RADIO_RECONNECT_PENDING, SetupState.FAILED}),
    SetupState.RADIO_RECONNECT_PENDING: frozenset({SetupState.RADIO_REACHABLE, SetupState.FAILED}),
    SetupState.RADIO_REACHABLE: frozenset(
        {SetupState.MARGE_ACCOUNT_PENDING, SetupState.PRESETS_READABLE, SetupState.FAILED}
    ),
    SetupState.MARGE_ACCOUNT_PENDING: frozenset({SetupState.MARGE_ACCOUNT_ACTIVE, SetupState.FAILED}),
    SetupState.MARGE_ACCOUNT_ACTIVE: frozenset({SetupState.PRESETS_READABLE, SetupState.FAILED}),
    SetupState.PRESETS_READABLE: frozenset(
        {SetupState.PLAYBACK_READY, SetupState.VERIFIED, SetupState.FAILED}
    ),
    SetupState.PLAYBACK_READY: frozenset({SetupState.VERIFIED, SetupState.FAILED}),
    SetupState.VERIFIED: frozenset({SetupState.ROLLBACK_PENDING}),
    SetupState.FAILED: frozenset(
        {SetupState.RECOVERY_PENDING, SetupState.ROLLBACK_PENDING}
    ),
    SetupState.RECOVERY_PENDING: frozenset(
        {SetupState.SSH_STATUS_PENDING, SetupState.ROLLBACK_PENDING, SetupState.FAILED}
    ),
    SetupState.ROLLBACK_PENDING: frozenset({SetupState.ROLLED_BACK, SetupState.FAILED}),
    SetupState.ROLLED_BACK: frozenset(),
}


def allowed_next_states(state: SetupState | str) -> tuple[SetupState, ...]:
    current = SetupState(state)
    return tuple(sorted(_ALLOWED_TRANSITIONS.get(current, ()), key=lambda item: item.value))


def transition(current: SetupState | str, target: SetupState | str) -> SetupState:
    current_state = SetupState(current)
    target_state = SetupState(target)
    if target_state == current_state:
        return target_state
    if target_state not in _ALLOWED_TRANSITIONS.get(current_state, frozenset()):
        raise ValueError(f"invalid setup transition {current_state} -> {target_state}")
    return target_state


def state_spec() -> list[dict[str, object]]:
    """Return the documented machine in a JSON-serializable form."""

    return [
        {
            "state": state.value,
            "allowed_next": [item.value for item in allowed_next_states(state)],
            "terminal": state in TERMINAL_STATES,
        }
        for state in SetupState
    ]


def multi_device_phase(state: SetupState | str) -> MultiDeviceSetupPhase:
    current = SetupState(state)
    if current is SetupState.UNKNOWN:
        return MultiDeviceSetupPhase.QUEUED
    if current in {
        SetupState.DISCOVERED,
        SetupState.IDENTIFIED,
        SetupState.SSH_STATUS_PENDING,
        SetupState.SSH_ALREADY_ACTIVE,
        SetupState.SSH_ACTIVATION_PENDING,
        SetupState.SSH_TEMPORARY_ACTIVE,
        SetupState.SSH_PERSISTENCE_PENDING,
        SetupState.SSH_REBOOT_PENDING,
        SetupState.SSH_VERIFIED,
    }:
        return MultiDeviceSetupPhase.PREFLIGHT
    if current in {SetupState.BACKUP_PENDING, SetupState.BACKUP_COMPLETE}:
        return MultiDeviceSetupPhase.BACKUP
    if current in {
        SetupState.ROUTING_BACKUP_COMPLETE,
        SetupState.BASSWIESN_ROUTE_PENDING,
        SetupState.BASSWIESN_ROUTE_ACTIVE,
    }:
        return MultiDeviceSetupPhase.ROUTING
    if current in {SetupState.RADIO_REBOOT_PENDING, SetupState.RADIO_REBOOTED}:
        return MultiDeviceSetupPhase.REBOOT
    if current in {SetupState.RADIO_RECONNECT_PENDING, SetupState.RADIO_REACHABLE}:
        return MultiDeviceSetupPhase.RECONNECT
    if current in {SetupState.MARGE_ACCOUNT_PENDING, SetupState.MARGE_ACCOUNT_ACTIVE}:
        return MultiDeviceSetupPhase.ACCOUNT
    if current in {SetupState.PRESETS_READABLE, SetupState.PLAYBACK_READY}:
        return MultiDeviceSetupPhase.READBACK
    if current is SetupState.VERIFIED:
        return MultiDeviceSetupPhase.VERIFIED
    if current is SetupState.FAILED:
        return MultiDeviceSetupPhase.FAILED
    return MultiDeviceSetupPhase.ROLLBACK
