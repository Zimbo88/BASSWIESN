"""In-process ownership for long-lived asyncio tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Awaitable, Callable

from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.core.request_context import correlation_scope, current_context, new_correlation_id


logger = logging.getLogger(__name__)


@dataclass
class TaskHandle:
    name: str
    task: asyncio.Task[None]
    stop_event: asyncio.Event | None = None
    job_id: str = ""
    request_id: str | None = None


_TASKS: dict[str, TaskHandle] = {}


def start_owned_task(name: str, factory: Callable[[], Awaitable[object]], *, stop_event: asyncio.Event | None = None) -> asyncio.Task[None]:
    """Start a named task once per process and return the existing task on reuse."""

    current = _TASKS.get(name)
    if current and not current.task.done():
        return current.task

    job_id = new_correlation_id()
    request_id = current_context().get("request_id")

    async def runner() -> None:
        with correlation_scope(request_id=request_id, job_id=job_id):
            try:
                await factory()
            except asyncio.CancelledError:
                write_masterlog("background_task_cancelled", task=name)
                raise
            except Exception as exc:  # task exceptions must never disappear
                # Raw exception text/tracebacks may contain credentials or URLs.
                logger.error("BASSWIESN background task failed: job=%s type=%s", job_id, type(exc).__name__)
                write_masterlog("background_task_failed", task=name, error_type=type(exc).__name__)
                return
            write_masterlog("background_task_completed", task=name)

    task = asyncio.create_task(runner(), name=f"basswiesn:{name}")
    _TASKS[name] = TaskHandle(name, task, stop_event, job_id, request_id)
    with correlation_scope(request_id=request_id, job_id=job_id):
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
    with correlation_scope(request_id=handle.request_id, job_id=handle.job_id):
        write_masterlog("background_task_stopped", task=name)


async def stop_all_owned_tasks() -> None:
    for name in list(_TASKS):
        await stop_owned_task(name)


def owned_task_status() -> list[dict[str, object]]:
    return [
        {"name": name, "done": handle.task.done(), "cancelled": handle.task.cancelled(),
         "job_id": handle.job_id, "request_id": handle.request_id}
        for name, handle in sorted(_TASKS.items())
    ]


def clear_owned_tasks_for_tests() -> None:
    """Only test cleanup may clear completed registry entries synchronously."""

    _TASKS.clear()
