"""Bounded, evidence-driven recovery ladder for BASSWIESN 2.5.0."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Any, Awaitable, Callable
from uuid import uuid4


class RecoveryStage(IntEnum):
    READBACK = 0
    METADATA_REFRESH = 1
    PROVIDER_REFRESH = 2
    STREAM_RERESOLVE = 3
    SAME_SOURCE_RESELECT = 4
    CONTROLLED_STOP_PLAY = 5
    LOCAL_SERVICE_RESTART = 6
    MANUAL_LAB_RADIO_REBOOT = 7


class RecoveryReason(StrEnum):
    METADATA_STALE = "METADATA_STALE"
    REPORTING_DEGRADED = "REPORTING_DEGRADED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    STREAM_FAILURE = "STREAM_FAILURE"
    SOURCE_INVALID = "SOURCE_INVALID"
    INTERNAL_SERVICE_FAILURE = "INTERNAL_SERVICE_FAILURE"
    UNKNOWN = "UNKNOWN"


class RecoveryStatus(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    RECOVERED = "RECOVERED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    COOLDOWN = "COOLDOWN"
    DENIED = "DENIED"


@dataclass(frozen=True)
class RecoveryPlan:
    reason: RecoveryReason
    requested_max_stage: RecoveryStage
    effective_max_stage: RecoveryStage
    stages: tuple[RecoveryStage, ...]
    automatic: bool
    lab_mode: bool
    protected_device: bool
    allowed: bool
    blocker: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["reason"] = self.reason.value
        result["requested_max_stage"] = int(self.requested_max_stage)
        result["effective_max_stage"] = int(self.effective_max_stage)
        result["stages"] = [int(stage) for stage in self.stages]
        return result


@dataclass
class RecoveryRun:
    operation_id: str
    key: str
    plan: RecoveryPlan
    status: RecoveryStatus = RecoveryStatus.PLANNED
    current_stage: RecoveryStage = RecoveryStage.READBACK
    started_at: datetime | None = None
    finished_at: datetime | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def _reason_limit(reason: RecoveryReason) -> RecoveryStage:
    return {
        RecoveryReason.METADATA_STALE: RecoveryStage.METADATA_REFRESH,
        RecoveryReason.REPORTING_DEGRADED: RecoveryStage.READBACK,
        RecoveryReason.PROVIDER_UNAVAILABLE: RecoveryStage.PROVIDER_REFRESH,
        RecoveryReason.STREAM_FAILURE: RecoveryStage.STREAM_RERESOLVE,
        RecoveryReason.SOURCE_INVALID: RecoveryStage.CONTROLLED_STOP_PLAY,
        RecoveryReason.INTERNAL_SERVICE_FAILURE: RecoveryStage.LOCAL_SERVICE_RESTART,
        RecoveryReason.UNKNOWN: RecoveryStage.READBACK,
    }[reason]


def plan_recovery(
    *,
    reason: RecoveryReason,
    requested_max_stage: int | RecoveryStage = RecoveryStage.READBACK,
    automatic: bool = False,
    lab_mode: bool = False,
    manual_radio_reboot: bool = False,
    protected_device: bool = False,
) -> RecoveryPlan:
    try:
        requested = RecoveryStage(int(requested_max_stage))
    except (TypeError, ValueError) as exc:
        raise ValueError("recovery stage must be between 0 and 7") from exc

    if protected_device:
        return RecoveryPlan(
            reason,
            requested,
            RecoveryStage.READBACK,
            (),
            automatic,
            lab_mode,
            protected_device,
            False,
            "Das unveränderlich geschützte Gerät darf weder gelesen noch verändert werden.",
        )
    if requested == RecoveryStage.MANUAL_LAB_RADIO_REBOOT and not (
        lab_mode and manual_radio_reboot and not automatic
    ):
        return RecoveryPlan(
            reason,
            requested,
            RecoveryStage.READBACK,
            (RecoveryStage.READBACK,),
            automatic,
            lab_mode,
            protected_device,
            False,
            "Radio-Reboot ist nur manuell im LAB erlaubt.",
        )

    evidence_limit = _reason_limit(reason)
    effective = min(requested, evidence_limit)
    if requested == RecoveryStage.MANUAL_LAB_RADIO_REBOOT:
        effective = requested
    elif automatic and effective >= RecoveryStage.SAME_SOURCE_RESELECT:
        # Stages 4 and 5 mutate the radio and are opt-in/manual according to
        # the confirmed recovery policy.  Automatic recovery may resolve a
        # stream descriptor (stage 3), but cannot silently reselect or stop.
        effective = RecoveryStage.STREAM_RERESOLVE
    stages = tuple(RecoveryStage(value) for value in range(0, int(effective) + 1))
    return RecoveryPlan(
        reason,
        requested,
        effective,
        stages,
        automatic,
        lab_mode,
        protected_device,
        True,
    )


RecoveryAction = Callable[[], Awaitable[dict[str, Any] | None]]
RecoveredCheck = Callable[[RecoveryStage, dict[str, Any] | None], Awaitable[bool]]


class RecoveryCoordinator:
    """Serialize one bounded recovery chain per device/provider generation."""

    def __init__(self, *, cooldown_seconds: int = 300) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_finished: dict[str, datetime] = {}
        self._active: dict[str, RecoveryRun] = {}
        self._cooldown = timedelta(seconds=max(0, cooldown_seconds))

    def active(self, key: str) -> RecoveryRun | None:
        return self._active.get(key)

    async def execute(
        self,
        key: str,
        plan: RecoveryPlan,
        *,
        actions: dict[RecoveryStage, RecoveryAction],
        recovered: RecoveredCheck,
        now: datetime | None = None,
    ) -> RecoveryRun:
        run = RecoveryRun(operation_id=str(uuid4()), key=key, plan=plan)
        if not plan.allowed:
            run.status = RecoveryStatus.DENIED
            run.error = plan.blocker
            return run
        observed = now or datetime.now(UTC)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)

        # A duplicate request joins the already active single flight instead
        # of queuing a second recovery chain behind it.
        active = self._active.get(key)
        if active is not None:
            return active

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Re-check both conditions after lock acquisition.  The previous
            # implementation checked cooldown before waiting, so two callers
            # could both pass the check and execute sequentially.
            active = self._active.get(key)
            if active is not None:
                return active
            last = self._last_finished.get(key)
            if last and observed - last < self._cooldown:
                run.status = RecoveryStatus.COOLDOWN
                run.error = "Recovery-Cooldown aktiv"
                return run

            run.started_at = observed
            run.status = RecoveryStatus.RUNNING
            self._active[key] = run
            try:
                for stage in plan.stages:
                    run.current_stage = stage
                    action = actions.get(stage)
                    if action is None:
                        raise RuntimeError(f"Recovery-Aktion für Stufe {int(stage)} fehlt")
                    result = await action()
                    run.events.append(
                        {
                            "stage": int(stage),
                            "name": stage.name,
                            "occurred_at": datetime.now(UTC).isoformat(),
                            "result": result or {},
                        }
                    )
                    if await recovered(stage, result):
                        run.status = RecoveryStatus.RECOVERED
                        break
                else:
                    run.status = RecoveryStatus.FAILED
                    run.error = "Recovery-Budget ausgeschöpft"
            except asyncio.CancelledError:
                run.status = RecoveryStatus.CANCELLED
                raise
            except Exception as exc:
                run.status = RecoveryStatus.FAILED
                run.error = f"{type(exc).__name__}: {exc}"
            finally:
                run.finished_at = datetime.now(UTC)
                self._last_finished[key] = run.finished_at
                self._active.pop(key, None)
            return run
