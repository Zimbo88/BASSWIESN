"""Acknowledged, bounded audio control over private local IPC.

The port belongs to an explicitly provisioned receiver, not an HTTP caller.
Closing future PCM is NOT a physical mute: encoded/radio buffers may still
contain audio. No task, namespace, radio transport or network is created here.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import json
import math
from typing import Protocol
import time

from .control_lease import ControlLease
from .heartbeat import ReceiverHeartbeat


class AudioControlPort(Protocol):
    async def publish(self, raw: bytes) -> None: ...
    async def acknowledgement(self) -> bytes: ...


@dataclass(frozen=True)
class AudioAcknowledgement:
    sequence: int
    command_sequence: int
    hold_epoch: int
    audio_released: bool
    allow_audio: bool


class AudioControlRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("AIRPLAY_AUDIO_CONTROL_UNVERIFIED")


class AudioControl:
    """An owner must pulse explicitly; receiver activity cannot renew it.

    Each hold waits for a fresh worker acknowledgement of this exact command.
    A failed/cancelled exchange retires the owner. Revocation is synchronous
    and prevents later release; worker-side expiry independently closes audio.
    """

    def __init__(self, nonce: str, port: AudioControlPort, *,
                 clock: Callable[[], float] = time.monotonic, timeout: float = 2) -> None:
        self._lease = ControlLease(nonce)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout < 5:
            raise ValueError("invalid acknowledgement timeout")
        self.nonce, self.port, self.clock, self.timeout = nonce, port, clock, timeout
        self._lock = asyncio.Lock()
        self._sequence, self._epoch = 0, 0
        self._allowed, self._failed = False, False
        self._revision = 0
        self._ack_heartbeat = ReceiverHeartbeat()
        self._previous_ack: dict[str, object] | None = None

    def revoke(self) -> None:
        self._failed, self._allowed = True, False
        self._revision += 1

    def require_held(self) -> None:
        """Synchronous pre-write check of the already acknowledged held gate."""
        self._check(self._revision)
        previous, ack = self._lease.previous, self._previous_ack
        if (previous is None or previous.allow_audio or self._allowed or ack is None
                or ack['allow_audio'] is not False or ack['audio_released'] is not False):
            raise AudioControlRejected()
        try:
            self._lease.heartbeat.check({'sequence':previous.sequence,
                'observed_monotonic':previous.observed_monotonic}, now=self.clock())
            self._ack_heartbeat.check({'sequence':ack['sequence'],
                'observed_monotonic':ack['observed_monotonic']}, now=self.clock())
        except ValueError:
            self.revoke()
            raise AudioControlRejected() from None

    def _check(self, revision: int) -> None:
        if self._failed or revision != self._revision:
            raise AudioControlRejected()

    def _parse(self, raw: bytes) -> AudioAcknowledgement:
        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            value: dict[str, object] = {}
            for key, item in items:
                if key in value:
                    raise ValueError
                value[key] = item
            return value

        if len(raw) > 2048:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=pairs)
        if (type(value) is not dict or set(value) != {'version', 'nonce', 'sequence',
                'observed_monotonic', 'command_sequence', 'hold_epoch', 'allow_audio', 'audio_released'}
                or type(value['version']) is not int or value['version'] != 1
                or value['nonce'] != self.nonce
                or any(type(value[key]) is not int or not 0 <= value[key] <= 2**53-1
                       for key in ('command_sequence', 'hold_epoch'))
                or any(type(value[key]) is not bool for key in ('allow_audio', 'audio_released'))):
            raise ValueError
        self._ack_heartbeat.check({'sequence': value['sequence'],
            'observed_monotonic': value['observed_monotonic']}, now=self.clock())
        old = self._previous_ack
        if old is not None and (
                (old['sequence'] == value['sequence'] and old != value)
                or value['command_sequence'] < old['command_sequence']
                or value['hold_epoch'] < old['hold_epoch']):
            raise ValueError
        self._previous_ack = value
        return AudioAcknowledgement(value['sequence'], value['command_sequence'], value['hold_epoch'],
                                    value['audio_released'], value['allow_audio'])

    async def _exchange(self, allowed: bool | None) -> AudioAcknowledgement:
        async with self._lock:
            revision = self._revision
            self._check(revision)
            try:
                async with asyncio.timeout(self.timeout):
                    if allowed is not None:
                        if not allowed:
                            self._epoch += 1
                        self._allowed = allowed
                    self._sequence += 1
                    now = self.clock()
                    raw = json.dumps(dict(version=1, nonce=self.nonce, sequence=self._sequence,
                        observed_monotonic=now, allow_audio=self._allowed, hold_epoch=self._epoch)).encode()
                    # A late pulse cannot revive an expired owner, even if the
                    # receiver has not noticed the missing lease yet.
                    self._lease.accept(raw, now=now)
                    await self.port.publish(raw)
                    self._check(revision)
                    while True:
                        try:
                            ack = self._parse(await self.port.acknowledgement())
                        except FileNotFoundError:
                            ack = None
                        self._check(revision)
                        if ack is not None:
                            if ack.command_sequence > self._sequence or ack.hold_epoch > self._epoch:
                                raise ValueError
                            if ack.command_sequence == self._sequence:
                                if (ack.hold_epoch != self._epoch or ack.allow_audio != self._allowed
                                        or (not self._allowed and ack.audio_released)):
                                    raise ValueError
                                return ack
                        await asyncio.sleep(.025)
            except BaseException as exc:
                self.revoke()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise AudioControlRejected() from None

    async def hold(self) -> AudioAcknowledgement:
        return await self._exchange(False)

    async def release(self) -> AudioAcknowledgement:
        return await self._exchange(True)

    async def pulse(self) -> AudioAcknowledgement:
        return await self._exchange(None)
