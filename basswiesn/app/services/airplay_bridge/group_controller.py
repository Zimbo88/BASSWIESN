"""Explicit bounded LAB loop connecting receiver IPC, audio gate and zones.

The outer playback owner must already own a fresh relay selection, all target
locks and a durable restore snapshot. Receiver provisioning/source selection
and post-session restoration are NOT inferred from input metadata. No automatic
startup, browser endpoint, discovery, or unverified master handoff is provided.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
import time
from typing import Protocol

from sqlalchemy.orm import Session

from basswiesn.app.models import Device
from basswiesn.app.services.direct_selection import Selection
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.zone_changes import ZonePorts
from .control_owner import AudioControl
from .group_session import GroupSession
from .group_zone import GroupZone, GroupZoneRejected, GroupZoneResult, group_zone_scope
from .receiver_port import ReceiverFrame, ReceiverPort


FrameSupplier = Callable[[], Mapping[str, ReceiverFrame]]


class _PlanChangedDuringReadback(RuntimeError):
    """No write outcome is implied; the next loop must verify the new plan."""


class OutputOwner(Protocol):
    @property
    def selection(self) -> Selection: ...

    async def validate_output(self, zone: GroupZone, frames: FrameSupplier) -> None: ...


class GroupController:
    """No writes in the liveness task; one foreground task owns the radios.

    The readback callback must check actual volume/mute and publish metadata
    only for its verified source. It must throw on drift, never silently fix a
    newer user's state. A hung foreground cannot keep audio alive indefinitely.
    """
    def __init__(self, session: GroupSession, receivers: Mapping[str, ReceiverPort],
                 audio: AudioControl, *, clock: Callable[[], float] = time.monotonic,
                 interval: float = .5) -> None:
        if (session.plan is not None or set(receivers) != session.device_ids or not .05 <= interval <= 1
                or any(port.binding is None or port.binding.device_id != key
                       for key, port in receivers.items())):
            raise ValueError('AIRPLAY_CONTROLLER_BINDINGS_REQUIRED')
        self.session, self.receivers, self.audio = session, dict(receivers), audio
        self.clock, self.interval = clock, interval
        self.frames: dict[str, ReceiverFrame] = {}
        self._ready = asyncio.Event()
        self._failure: BaseException | None = None
        self._progress = clock()
        self._owner_sequence = 0
        self._running = False
        self._stop_requested = False

    def request_stop(self) -> None:
        """Schedule held, foreground-owned teardown, never cancel a radio write."""
        self._stop_requested = True

    def stopped_result(self) -> GroupZoneResult:
        return GroupZoneResult('STOPPED', self.session.master, (), '', (), ())

    def check(self) -> None:
        if self._failure is not None:
            raise RuntimeError('AIRPLAY_CONTROLLER_OBSERVATION_LOST') from self._failure
        self.session.require_live(now=self.clock())

    async def _monitor(self) -> None:
        try:
            while True:
                # Thirty seconds bounds one in-flight verified foreground
                # step, not a licence for unattended retry or self-renewal.
                if self.clock() - self._progress >= 30:
                    raise TimeoutError('AIRPLAY_FOREGROUND_STALLED')
                async with asyncio.timeout(2):
                    values = await asyncio.gather(*(port.read() for port in self.receivers.values()))
                frames = dict(zip(self.receivers, values, strict=True))
                now = self.clock()
                self._owner_sequence += 1
                self.session.renew_owner(sequence=self._owner_sequence, observed_monotonic=now, now=now)
                old = self.session.plan
                plan = self.session.refresh({key: value.sample for key, value in frames.items()}, now=now)
                if plan is not old or self._stop_requested:
                    await self.audio.hold()
                else:
                    await self.audio.pulse()
                self.frames = frames
                self._ready.set()
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._failure = exc
            # A failed observation never keeps the most recent release alive.
            try:
                await self.audio.hold()
            except Exception:
                pass  # Worker has an independent five-second fail-closed lease.
            self.audio.revoke()
            self._ready.set()

    @asynccontextmanager
    async def _supervised(self) -> AsyncIterator[None]:
        if self._running:
            raise RuntimeError('AIRPLAY_CONTROLLER_ONE_SHOT')
        self._running = True
        monitor = asyncio.create_task(self._monitor(), name='ap2-private-input-monitor')
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=4)
            self.check()
            yield
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            try:
                await self.audio.hold()
            except Exception:
                pass
            self.audio.revoke()

    async def run(self, db: Session, devices: Sequence[Device], *, ports: ZonePorts,
                  selection: Selection,
                  validate_output: Callable[[GroupZone, FrameSupplier], Awaitable[None]],
                  on_result: Callable[[GroupZoneResult], None] = lambda _: None,
                  ) -> GroupZoneResult:
        async with self._supervised():
            return await self._operate(db, devices, ports, selection, validate_output, on_result)

    async def run_owned(self, db: Session, devices: Sequence[Device], *, ports: ZonePorts,
                        prepare: Callable[[Callable[[], None]], AbstractAsyncContextManager[OutputOwner]],
                        wait_seconds: float = 120,
                        initial_members: frozenset[str] | None = None,
                        on_result: Callable[[GroupZoneResult], None] = lambda _: None,
                        ) -> GroupZoneResult:
        """Keep input/held-audio supervision alive during source preparation.

        No radio is touched while waiting for the explicit master's input. The
        supplied context must own source backup, selection and conditional
        restoration in this same foreground task. A changed native plan aborts
        preparation; the helper never elects another radio behind the user's back.
        """
        if type(wait_seconds) not in (int, float) or not 0 < wait_seconds <= 300:
            raise ValueError('AIRPLAY_BOUNDED_WAIT_REQUIRED')
        if initial_members is not None and (
                not isinstance(initial_members, frozenset)
                or not initial_members <= self.session.device_ids - {self.session.master}):
            raise ValueError('AIRPLAY_EXPLICIT_INITIAL_MEMBERS_REQUIRED')
        async with self._supervised():
            deadline = self.clock() + wait_seconds
            while True:
                self.check()
                if self._stop_requested:
                    await self.audio.hold()
                    return self.stopped_result()
                plan = self.session.plan
                if plan is None or plan.status not in {'WAITING', 'ACTIVE'}:
                    raise RuntimeError('AIRPLAY_ACTIVE_SELECTION_REQUIRED')
                if plan.status == 'ACTIVE' and (initial_members is None
                        or frozenset(plan.members) == initial_members):
                    break
                if self.clock() >= deadline:
                    raise TimeoutError('AIRPLAY_INPUT_WAIT_EXPIRED')
                self._progress = self.clock()
                await asyncio.sleep(self.interval)
            self.check()

            def guard_preparation() -> None:
                self.check()
                self.audio.require_held()
                if self.session.plan is not plan:
                    raise RuntimeError('AIRPLAY_PLAN_CHANGED_DURING_PREPARATION')
                now = self.clock()
                if now - self._progress >= 30:
                    raise TimeoutError('AIRPLAY_FOREGROUND_STALLED')
                # Only the foreground advances this, after rechecking the live
                # session, acknowledged hold and exact native plan. A sequence
                # of bounded verified source reads can legitimately take over
                # thirty seconds; an individual stalled step still cannot.
                self._progress = now

            guard_preparation()
            async with prepare(guard_preparation) as source:
                guard_preparation()
                if self._stop_requested:
                    await self.audio.hold()
                    return self.stopped_result()
                return await self._operate(db, devices, ports, source.selection,
                                           source.validate_output, on_result)

    async def _operate(self, db: Session, devices: Sequence[Device], ports: ZonePorts,
                       selection: Selection,
                       validate_output: Callable[[GroupZone, FrameSupplier], Awaitable[None]],
                       on_result: Callable[[GroupZoneResult], None]) -> GroupZoneResult:
        if self.session.plan is None or self.session.plan.status != 'ACTIVE':
            raise RuntimeError('AIRPLAY_ACTIVE_SELECTION_REQUIRED')
        foreground = asyncio.current_task()
        def verified_progress() -> None:
            # GroupZone calls only after its full live-plan/identity/profile
            # guard. Neither the observer task nor an unlocked caller can
            # turn receiver activity into progress of the radio transaction.
            if (asyncio.current_task() is not foreground or any(
                    not device_lock(identity).owned_by_current_task() for identity in self.session.device_ids)):
                raise RuntimeError('AIRPLAY_FOREGROUND_OWNER_REQUIRED')
            self.check()
            now = self.clock()
            if now - self._progress >= 30:
                raise TimeoutError('AIRPLAY_FOREGROUND_STALLED')
            self._progress = now
        async with group_zone_scope(db, devices, self.session, ports=ports, selection=selection,
                hold_audio=self.audio.revoke, hold_acknowledged=self.audio.hold,
                clock=self.clock, foreground_progress=verified_progress) as zone:
            previous = None
            next_readback = self.clock()
            while True:
                self.check()
                if self._stop_requested:
                    await self.audio.hold()
                    self.session.request_stop(now=self.clock())
                plan = self.session.plan
                if plan is not previous:
                    result = await zone.apply_current()
                    self.check()
                    on_result(result)
                    if result.status != 'ACTIVE':
                        await self.audio.hold()
                        return result
                    previous = plan
                    next_readback = self.clock()
                if self.clock() >= next_readback:
                    def current_frames() -> Mapping[str, ReceiverFrame]:
                        # Radio readback may outlast one receiver heartbeat.
                        # Sample only at the consumer's final synchronous use,
                        # after checking the still-owned native plan/liveness.
                        self.check()
                        if self.session.plan is not plan:
                            raise _PlanChangedDuringReadback('AIRPLAY_PLAN_CHANGED_BEFORE_RELEASE')
                        return dict(self.frames)

                    try:
                        await zone.check_expected()
                        await zone.check_source()
                        await validate_output(zone, current_frames)
                    except (_PlanChangedDuringReadback, GroupZoneRejected) as exc:
                        # Re-evaluate only a live native-plan change during the
                        # readback phase. Never retry an interrupted zone write
                        # or swallow identity/source/volume/transport failures.
                        self.check()
                        if (self.session.plan is plan or zone.failed or
                                (isinstance(exc, GroupZoneRejected) and
                                 exc.code != 'AIRPLAY_GROUP_CHANGED_DURING_WRITE')):
                            raise
                        await self.audio.hold()
                        continue
                    self.check()
                    if self.session.plan is not plan or self._stop_requested:
                        await self.audio.hold()
                        continue
                    await self.audio.release()
                    self.check()
                    self._progress = self.clock()
                    next_readback = self.clock() + 2
                await asyncio.sleep(self.interval)
