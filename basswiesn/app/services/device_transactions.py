"""Transaction ownership shared by HTTP orchestration and background services.

Acquire all known targets before the first action. Nested operations may borrow
only an already-owned subset in the same task. They may not expand the target
set while holding locks; that would invert ordering and permit deadlocks.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from basswiesn.app.models import Device
from basswiesn.app.services.device_ownership import DeviceBusy, DeviceLockUnavailable, DeviceOwnership
from basswiesn.app.services.device_policy import device_lock

_active: ContextVar[tuple[asyncio.Task[Any] | None, frozenset[str]] | None] = ContextVar(
    "device_transaction_targets", default=None
)


def device_fingerprint(device: Device) -> tuple[str, str, str, str, str]:
    """Freeze targeting/profile inputs; a DB refresh must not retarget a write."""
    return (device.device_id, device.ip_address, device.model, device.firmware, device.info_xml)


@asynccontextmanager
async def device_transaction(targets: Iterable[str], *, enabled: bool = True) -> AsyncIterator[None]:
    if not enabled:
        yield
        return
    identities = frozenset(value.strip().upper() for value in targets)
    if not identities or any(not value or len(value) > 128 for value in identities):
        raise DeviceLockUnavailable()
    task = asyncio.current_task()
    current = _active.get()
    if current is not None and current[0] is task and not identities.issubset(current[1]):
        raise DeviceBusy()
    locks = [device_lock(value) for value in sorted(identities)]
    borrowed = [lock.owned_by_current_task() for lock in locks]
    if any(borrowed) and not all(borrowed):
        raise DeviceBusy()
    acquired: list[DeviceOwnership] = []
    token = None
    try:
        for lock, owned in zip(locks, borrowed, strict=True):
            if not owned:
                await lock.acquire()
                acquired.append(lock)
        token = _active.set((task, identities if current is None or current[0] is not task else current[1]))
        yield
    finally:
        if token is not None:
            _active.reset(token)
        for lock in reversed(acquired):
            lock.release()
