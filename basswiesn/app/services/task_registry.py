"""In-process ownership for long-lived asyncio tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Awaitable, Callable

from basswiesn.app.core.masterlog import write_masterlog


logger = logging.getLogger(__name__)


@dataclass
class TaskHandle:
    name: str
    task: asyncio.Task
    stop_event: asyncio.Event | None = None


_TASKS: dict[str, TaskHandle] = {}


def start_owned_task(name: str, factory: Callable[[], Awaitable], *, stop_event: asyncio.Event | None = None) -> asyncio.Task | None:
    """Start a named task once per process and return the existing task on reuse."""

    current = _TASKS.get(name)
    if current and not current.task.done():
        return current.task

    async def runner():
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # task exceptions must never disappear
            logger.exception("BASSWIESN background task failed: %s", name)
            write_masterlog("background_task_failed", task=name, error_type=type(exc).__name__)
            return

    task = asyncio.create_task(runner(), name=f"basswiesn:{name}")
    _TASKS[name] = TaskHandle(name, task, stop_event)
    write_masterlog("background_task_started", task=name)
    return task


async def stop_owned_task(name: str) -> None:
    handle = _TASKS.pop(name, None)
    if handle is None:
        return
    if handle.stop_event is not None:
        handle.stop_event.set()
    if not handle.task.done():
        handle.task.cancel()
    try:
        await handle.task
    except asyncio.CancelledError:
        pass
    write_masterlog("background_task_stopped", task=name)


async def stop_all_owned_tasks() -> None:
    for name in list(_TASKS):
        await stop_owned_task(name)


def owned_task_status() -> list[dict]:
    return [
        {"name": name, "done": handle.task.done(), "cancelled": handle.task.cancelled()}
        for name, handle in sorted(_TASKS.items())
    ]


def clear_owned_tasks_for_tests() -> None:
    """Only test cleanup may clear completed registry entries synchronously."""

    _TASKS.clear()
