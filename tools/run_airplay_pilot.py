#!/usr/bin/env python3
"""Explicit private AP2 pilot. No discovery, migration or full app startup."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    from basswiesn.app.services.airplay_bridge.pilot_runner import check_only, load_config, run_pilot, safe_error
    try:
        config = load_config(args.config)
        result = check_only(config) if args.check_only else asyncio.run(run_pilot(config))
    except Exception as exc:
        # Never print exception messages, configuration, credentials or relay URLs.
        print(json.dumps({'status': 'FAILED', **safe_error(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] in {'PREFLIGHT_OK', 'FINISHED'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
