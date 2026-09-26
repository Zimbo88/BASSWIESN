#!/usr/bin/env python3
"""Local administrator preflight. No install, enrollment writes or radio probes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from basswiesn.update_helper.host_preflight import HostRejected, evaluate, load_enrollment
from basswiesn.update_helper.local_docker import collect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, help="Root-owned private enrollment policy (no environment secrets in output)")
    parser.add_argument("--inspect-local-docker", action="store_true",
                        help="Explicitly inspect only the fixed local Docker socket; requires provisioned root-owned prerequisites")
    args = parser.parse_args()
    try:
        enrollment = load_enrollment(args.policy)
        observation = collect(enrollment) if args.inspect_local_docker else None
        result = evaluate(enrollment, observation=observation)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["layout_checks_passed"] else 2
    except HostRejected as error:
        print(json.dumps({"installation_available": False, "code": error.code.value}))
        return 2
    except Exception:
        print(json.dumps({"installation_available": False, "code": "PREFLIGHT_FAILED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
