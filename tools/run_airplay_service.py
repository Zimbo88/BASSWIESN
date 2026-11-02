#!/usr/bin/env python3
"""Explicit unprivileged LAB coordinator for already provisioned profiles.

Does not install/start receivers, extend native leases or run on web startup.
The root network owner must provide fresh verified profiles separately.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import signal

from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from basswiesn.app.services.airplay_bridge.pilot_runner import (
    PilotConfig, PilotRunner, check_only, database, load_config,
)
from basswiesn.app.services.airplay_bridge.session_service import SessionService
from basswiesn.app.services.airplay_bridge.service_control import ServiceControl


async def serve(root: Path, profile_args: list[str]) -> None:
    if os.geteuid() == 0:
        raise ValueError('AIRPLAY_SERVICE_NONROOT_REQUIRED')
    profiles: dict[str, PilotConfig] = {}
    for entry in profile_args:
        name, separator, filename = entry.partition('=')
        if not separator or name in profiles:
            raise ValueError('AIRPLAY_PROFILE_INVALID')
        profiles[name] = load_config(Path(filename))
    engines: list[Engine] = []
    def worker(config: PilotConfig) -> PilotRunner:
        engine = database(config, readonly=False)
        engines.append(engine)
        return PilotRunner(config, sessionmaker(bind=engine, expire_on_commit=False), manage_signals=False)
    service = SessionService(root, worker, preflight=check_only)
    control = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        control = ServiceControl(service, profiles)
        await control.start()
        await stop.wait()
    finally:
        try:
            if control:
                await control.close()
            else:
                await service.close()
        finally:
            for engine in engines:
                engine.dispose()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--profile', action='append', required=True, metavar='NAME=PRIVATE_CONFIG')
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.state_dir, args.profile))
    except Exception:
        # Configs may contain capabilities. Never expose parser/SQL/URL details.
        parser.exit(1, 'AIRPLAY_SERVICE_FAILED; inspect private session receipts\n')


if __name__ == '__main__':
    main()
