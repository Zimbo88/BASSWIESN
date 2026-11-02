"""Process-shared device ingress budgets; no new device or admin authority.

Active slots reuse the existing checked filesystem ownership primitive. Kernel
locks live for the whole request, not a guessed TTL, and release on process exit.
Rate state uses short SQLite transactions, shared by all listeners/workers.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from basswiesn.app.config import get_settings
from basswiesn.app.services.device_ownership import DeviceBusy, DeviceOwnership

PREFIX = "device-ingress-budget:"


class IngressBudgetUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("DEVICE_BUDGET_UNAVAILABLE")


def lock_root() -> Path:
    return get_settings().data_dir / "ingress-locks"


def budget_key(peer: str) -> str:
    return PREFIX + sha256(peer.encode()).hexdigest()


@dataclass
class IngressLease:
    slots: tuple[DeviceOwnership, ...]

    def release(self) -> None:
        for slot in reversed(self.slots):
            slot.release()
        self.slots = ()


class SharedIngressBudget:
    def __init__(self, *, clock: Callable[[], float], burst: float, rate: float,
                 max_active: int, max_total: int) -> None:
        if not (math.isfinite(burst) and math.isfinite(rate) and burst >= 1 and rate > 0
                and 1 <= max_active <= 32 and 1 <= max_total <= 32):
            raise IngressBudgetUnavailable()
        self.clock, self.burst, self.rate = clock, burst, rate
        self.max_active, self.max_total = max_active, max_total

    async def _slot(self, namespace: str, count: int) -> DeviceOwnership | None:
        for index in range(count):
            slot = DeviceOwnership(lock_root(), f"{namespace}:{index}", timeout=0)
            try:
                await slot.acquire()
                return slot
            except DeviceBusy:
                continue
        return None

    def _reserve_rate(self, peer: str) -> bool:
        from basswiesn.app import db as app_db
        from basswiesn.app.models import RuntimeState

        now = self.clock()
        if not math.isfinite(now) or now < 0:
            raise IngressBudgetUnavailable()
        with app_db.SessionLocal() as db:
            if db.get_bind().dialect.name != "sqlite":
                raise IngressBudgetUnavailable()
            # Never hold this database reservation across ASGI/body/network I/O.
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.query(RuntimeState).filter_by(key=budget_key(peer)).one_or_none()
            tokens, observed = self.burst, now
            if row is not None:
                state = json.loads(row.value)
                if (not isinstance(state, dict) or set(state) != {"version", "tokens", "observed"}
                        or type(state["version"]) is not int or state["version"] != 1):
                    raise IngressBudgetUnavailable()
                tokens, observed = state["tokens"], state["observed"]
                if any(type(value) not in (int, float) or not math.isfinite(value) for value in (tokens, observed)):
                    raise IngressBudgetUnavailable()
                if not 0 <= tokens <= self.burst or observed < 0:
                    raise IngressBudgetUnavailable()
                # A backward clock must not refill now OR when it catches up.
                if now < observed:
                    return False
                tokens = min(self.burst, tokens + (now - observed) * self.rate)
            if tokens < 1:
                return False
            value = json.dumps({"version": 1, "tokens": tokens - 1, "observed": now})
            if row is None:
                db.add(RuntimeState(key=budget_key(peer), value=value))
            else:
                row.value = value
            db.commit()
            return True

    async def acquire(self, peer: str) -> IngressLease | None:
        slots: list[DeviceOwnership] = []
        retained = False
        try:
            global_slot = await self._slot("global", self.max_total)
            if global_slot is None:
                return None
            slots.append(global_slot)
            peer_slot = await self._slot("peer:" + sha256(peer.encode()).hexdigest(), self.max_active)
            if peer_slot is None:
                return None
            slots.append(peer_slot)
            if not await run_in_threadpool(self._reserve_rate, peer):
                return None
            retained = True
            return IngressLease(tuple(slots))
        except Exception:
            raise IngressBudgetUnavailable() from None
        finally:
            if not retained:
                for slot in reversed(slots):
                    slot.release()
