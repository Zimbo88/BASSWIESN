"""Fail-closed private IPC liveness, not sender authentication or group evidence.

The receiver supervisor emits this only after checking its children and lease.
The separate audio worker must stop if the supervisor stalls, even while a long
explicit pause is allowed. Nothing here starts a task or contacts a device.
"""
from __future__ import annotations

import math


class ReceiverHeartbeat:
    def __init__(self, *, max_age: float = 5.0) -> None:
        if type(max_age) not in (float, int) or not math.isfinite(max_age) or not 0 < max_age <= 5:
            raise ValueError("invalid heartbeat bound")
        self.max_age = max_age
        self._sequence = -1
        self._stamp = -1.0
        self._now = -1.0
        self._failed = False

    def check(self, observation: object, *, now: float) -> None:
        """Duplicate observations cannot renew the lease; expiry is terminal."""
        try:
            if self._failed or type(now) not in (float, int) or not math.isfinite(now) or now < self._now:
                raise ValueError
            self._now = now
            if type(observation) is not dict or set(observation) != {"sequence", "observed_monotonic"}:
                raise ValueError
            sequence, stamp = observation["sequence"], observation["observed_monotonic"]
            if (type(sequence) is not int or not 0 <= sequence <= 2**53 - 1
                    or type(stamp) not in (float, int) or not math.isfinite(stamp)
                    or not 0 <= stamp <= now or now - stamp >= self.max_age
                    or sequence < self._sequence or stamp < self._stamp
                    or (sequence == self._sequence and stamp != self._stamp)
                    or (sequence > self._sequence and self._sequence >= 0 and stamp <= self._stamp)):
                raise ValueError
            self._sequence, self._stamp = sequence, stamp
        except (ValueError, TypeError):
            self._failed = True
            raise ValueError("RECEIVER_LIVENESS_UNVERIFIED") from None
