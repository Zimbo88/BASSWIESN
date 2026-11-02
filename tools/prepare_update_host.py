#!/usr/bin/env python3
"""Development/admin enrollment preparation, NOT an operational updater.

Use only from administrator-reviewed, trusted release code. Do not run a mutable
or untrusted checkout as root. This tool does not install itself as a service.
Default: filesystem-only preview. --apply requires a matching preview digest
AND explicit consent. No application write, Docker mutation or radio request.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from basswiesn.update_helper.host_preflight import HostRejected
from basswiesn.update_helper.onboarding import (
    HOST_ROOT, SetupRejected, prepare, preview, verify_preparation,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="Reserve private host state and inspect the approved local container")
    action.add_argument("--verify-prepared", action="store_true", help="Only verify an unused prepared enrollment; no Docker access")
    parser.add_argument("--installation-root", help="Exact existing installation, not a URL")
    parser.add_argument("--installation-owner-uid", type=int)
    parser.add_argument("--compose-project")
    parser.add_argument("--container-id", help="Exact full container ID; no listing/discovery is performed")
    parser.add_argument("--image-id", help="Exact sha256 image ID, not a tag")
    parser.add_argument("--version", help="Current stable installed version")
    parser.add_argument("--approve-preview", help="SHA256 returned by the preceding local preview")
    parser.add_argument("--acknowledge-private-host-state", action="store_true",
                        help="Explicitly approve new root-private state and read-only Docker inspection; no service is enabled")
    args = parser.parse_args(argv)
    values = (args.installation_root, args.installation_owner_uid, args.compose_project,
              args.container_id, args.image_id, args.version)
    if args.verify_prepared:
        if any(value is not None for value in values) or args.approve_preview or args.acknowledge_private_host_state:
            parser.error("verification does not accept preparation parameters")
    elif any(value is None for value in values):
        parser.error("preparation requires all six installation identity parameters")
    if args.apply and (not args.approve_preview or not args.acknowledge_private_host_state):
        parser.error("apply requires both preview approval and explicit acknowledgment")
    if not args.apply and (args.approve_preview or args.acknowledge_private_host_state):
        parser.error("approval parameters are only valid with --apply")
    try:
        if args.verify_prepared:
            result = verify_preparation(HOST_ROOT)
        else:
            plan = preview(root=args.installation_root, installation_owner_uid=args.installation_owner_uid,
                compose_project=args.compose_project, container_id=args.container_id,
                image_id=args.image_id, version=args.version, target=HOST_ROOT)
            result = prepare(plan, approve=True, approval_sha256=args.approve_preview) if args.apply else plan.public()
        print(json.dumps(result, sort_keys=True))
        return 0
    except (HostRejected, SetupRejected) as error:
        code = error.code.value
    except Exception:
        code = "PREPARATION_FAILED"
    print(json.dumps({"installation_available": False, "onboarding_complete": False,
                      "preparation_may_be_incomplete": args.apply, "code": code}, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
