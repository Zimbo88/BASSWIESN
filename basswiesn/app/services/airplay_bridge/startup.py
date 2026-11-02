"""Pure, bounded readback after one radio selection; never issues a write.

An observed BUFFERING_STATE is not success and not permission to repeat select.
The caller supplies actual identity/source/volume observations, keeps the audio
gate closed, and restores/reconciles on failure. Simulation is not audibility.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math


class StartupDecision(StrEnum):
    WAIT = "WAIT_READBACK"
    READY = "RADIO_READBACK_CONFIRMED"
    ABORT = "RECONCILIATION_REQUIRED"


@dataclass(frozen=True)
class StartupObservation:
    identity_verified: bool
    owned_source: bool
    zone_clear: bool
    volume: int | None
    play_status: str
    relay_state: str
    relay_failure: str | None
    sent_bytes: int


class StartupReadbackGate:
    """One-shot readback window. Terminal results cannot be revived.

    This is a startup observation, not continuing permission to play. The owner
    still rechecks volume/mute immediately before release and throughout output.
    Times must come from the same monotonic clock; no device timestamps trusted.
    """

    def __init__(self, *, started: float, timeout: float = 18, maximum_volume: int = 1):
        if (type(started) not in (int, float) or not math.isfinite(started)
                or type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 30):
            raise ValueError("finite bounded startup window required")
        if type(maximum_volume) is not int or not 1 <= maximum_volume <= 100:
            raise ValueError("explicit safe volume bound required")
        self.deadline = started + timeout
        self.maximum_volume = maximum_volume
        self.last_time = started
        self.decision = StartupDecision.WAIT
        self.reason = "BUFFERING_NOT_YET_CONFIRMED"

    def _abort(self, reason: str) -> StartupDecision:
        self.reason = reason
        self.decision = StartupDecision.ABORT
        return self.decision

    def observe(self, value: StartupObservation, *, now: float) -> StartupDecision:
        if self.decision != StartupDecision.WAIT:
            return self.decision
        if (type(now) not in (int, float) or not math.isfinite(now)
                or now < self.last_time):
            return self._abort("INVALID_OBSERVATION_CLOCK")
        self.last_time = now
        if now >= self.deadline:
            return self._abort("READBACK_DEADLINE_EXCEEDED")
        if (value.identity_verified is not True or value.owned_source is not True
                or value.zone_clear is not True):
            return self._abort("TARGET_OR_SOURCE_CHANGED")
        if type(value.volume) is not int or not 0 <= value.volume <= self.maximum_volume:
            return self._abort("SAFE_VOLUME_NOT_CONFIRMED")
        if (value.relay_failure is not None or value.relay_state != "STREAMING"
                or type(value.sent_bytes) is not int or value.sent_bytes < 0):
            return self._abort("RELAY_NOT_STREAMING")
        if value.play_status not in ("BUFFERING_STATE", "PLAY_STATE"):
            return self._abort("RADIO_STATE_NOT_PLAYABLE")
        if value.play_status == "PLAY_STATE" and value.sent_bytes >= 4096:
            self.decision = StartupDecision.READY
            self.reason = "OWNED_SOURCE_PLAYING_AT_SAFE_VOLUME"
        return self.decision
