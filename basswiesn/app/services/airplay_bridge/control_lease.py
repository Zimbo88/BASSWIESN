"""Private owner's audio gate, independent of receiver/sender liveness.

Only the explicitly bound local controller publishes this atomic file. No
network endpoint accepts it. The nonce binds a one-shot relay, never an Apple
sender; a group label cannot renew this authority. Failure is terminal.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from .heartbeat import ReceiverHeartbeat


@dataclass(frozen=True)
class ControlState:
    sequence: int
    observed_monotonic: float
    allow_audio: bool
    hold_epoch: int


class ControlLease:
    def __init__(self, nonce: str) -> None:
        if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{32}", nonce):
            raise ValueError("AIRPLAY_CONTROLLER_UNVERIFIED")
        self.nonce = nonce
        self.previous: ControlState | None = None
        self.failed = False
        self.heartbeat = ReceiverHeartbeat()

    def accept(self, raw: bytes, *, now: float) -> ControlState:
        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            data: dict[str, object] = {}
            for key, value in items:
                if key in data:
                    raise ValueError
                data[key] = value
            return data

        try:
            if self.failed or len(raw) > 2048:
                raise ValueError
            value = json.loads(raw, object_pairs_hook=pairs)
            if (type(value) is not dict or set(value) != {'version', 'nonce', 'sequence',
                    'observed_monotonic', 'allow_audio', 'hold_epoch'}
                    or type(value['version']) is not int or value['version'] != 1
                    or value['nonce'] != self.nonce or type(value['allow_audio']) is not bool
                    or type(value['hold_epoch']) is not int
                    or not 0 <= value['hold_epoch'] <= 2**53-1):
                raise ValueError
            if self.previous is not None:
                self.heartbeat.check({'sequence': self.previous.sequence,
                    'observed_monotonic': self.previous.observed_monotonic}, now=now)
            self.heartbeat.check({'sequence': value['sequence'],
                'observed_monotonic': value['observed_monotonic']}, now=now)
            record = ControlState(value['sequence'], value['observed_monotonic'],
                                  value['allow_audio'], value['hold_epoch'])
            old = self.previous
            if old is not None and (
                (record.sequence == old.sequence and record != old)
                or record.hold_epoch < old.hold_epoch
                or (old.allow_audio and not record.allow_audio and record.hold_epoch == old.hold_epoch)
            ):
                raise ValueError
            self.previous = record
            return record
        except (ValueError, TypeError, UnicodeError):
            self.failed = True
            raise ValueError("AIRPLAY_CONTROLLER_UNVERIFIED") from None
