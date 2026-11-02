#!/usr/bin/env python3
"""Synthetic in-process API/SQLite baseline. No sockets, server or real data.

This measures router handlers and serialization, not network throughput, a Pi's
idle memory, or audible playback. Run from the locked development environment.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
import json
import math
import os
from pathlib import Path
import platform
import resource
import socket
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def denied(*args: object, **kwargs: object) -> None:
    raise RuntimeError("OFFLINE_BENCHMARK_NETWORK_FORBIDDEN")


async def measure(action: Callable[[], Awaitable[object]], samples: int) -> dict[str, float]:
    for _ in range(5):
        json.dumps(await action(), default=str)
    timings = []
    for _ in range(samples):
        start = time.perf_counter_ns()
        json.dumps(await action(), default=str)
        timings.append((time.perf_counter_ns() - start) / 1_000_000)
    timings.sort()
    return {"median_ms": statistics.median(timings), "p95_ms": timings[math.ceil(samples * .95) - 1]}


async def workload(samples: int, rows: int) -> dict[str, object]:
    from sqlalchemy import insert
    from basswiesn.app import db
    from basswiesn.app.db.migrations import inspect_schema
    from basswiesn.app.models import Device, PlayHistory, Preset, Station
    from basswiesn.app.routers.api import play_history
    from basswiesn.app.routers.stations_presets import get_presets, stations

    start = time.perf_counter_ns()
    db.init_db()
    migration_ms = (time.perf_counter_ns() - start) / 1_000_000
    with db.SessionLocal() as session:
        session.add(Device(device_id="AA11BB22CC33", name="Example Radio", ip_address="192.0.2.42"))
        session.add_all([Station(name=f"Example {number}", stream_url=f"https://example.invalid/{number}.mp3")
                         for number in range(100)])
        session.add_all([Preset(device_id="AA11BB22CC33", button=slot, location=f"example:{slot}")
                         for slot in range(1, 7)])
        base = datetime(2026, 1, 1, tzinfo=UTC)
        session.execute(insert(PlayHistory), [
            {"device_id": "AA11BB22CC33", "station_name": "Example station", "station_display_name": "Example station",
             "stream_url": "https://example.invalid/radio.mp3", "source": "LOCAL_INTERNET_RADIO",
             "started_at": base + timedelta(minutes=number), "ended_at": base + timedelta(minutes=number + 1),
             "is_confirmed": True} for number in range(rows)])
        session.commit()

        async def preset_read() -> object:
            return await get_presets("AA11BB22CC33", db=session)

        async def history_read() -> object:
            return await play_history(limit=100, include_internal=False, db=session)

        async def station_read() -> object:
            return await stations(include_internal=False, db=session)

        async def schema_read() -> object:
            status = inspect_schema(db.engine)
            return {"ready": status.ready, "tables": status.existing_tables}

        metrics = {name: await measure(action, samples) for name, action in (
            ("presets_cache_6", preset_read), ("history_100", history_read),
            ("station_catalog_100", station_read), ("schema_inspection", schema_read))}
    db.engine.dispose()
    return {"fixture_schema": 1, "history_rows": rows, "samples": samples, "metrics": metrics,
            "empty_migration_ms_single_sample": migration_ms,
            "peak_process_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "scope": "in-process handlers plus JSON serialization; no HTTP listener",
            "hardware_validation": "NOT_RUN", "network_access": "BLOCKED",
            "pi_performance": "NOT_MEASURED", "python": platform.python_version(),
            "architecture": platform.machine(), "platform": platform.system()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--history-rows", type=int, default=10000)
    args = parser.parse_args(argv)
    if not 10 <= args.samples <= 1000 or not 100 <= args.history_rows <= 100000:
        parser.error("Use 10..1000 samples and 100..100000 synthetic history rows")
    original = Path.cwd()
    try:
        with tempfile.TemporaryDirectory(prefix="basswiesn-offline-benchmark-") as temporary, ExitStack() as stack:
            os.chdir(temporary)
            Path("data").mkdir(mode=0o700)
            stack.enter_context(patch.dict(os.environ, {"BASSWIESN_LAN_HOST": "192.0.2.42"}))
            for method in ("connect", "connect_ex", "sendto"):
                stack.enter_context(patch.object(socket.socket, method, denied))
            stack.enter_context(patch.object(socket, "getaddrinfo", denied))
            result = asyncio.run(workload(args.samples, args.history_rows))
    finally:
        os.chdir(original)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
