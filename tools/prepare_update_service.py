#!/usr/bin/env python3
"""Build/review or explicitly install sealed update-service files; never enable.

Run build unprivileged from reviewed source. For install, use independently
trusted administrator-reviewed code, not an untrusted mutable checkout as root.
No capability is printed. No systemctl/Docker command, application write or
network action is performed. This is not yet an operational app installer.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from basswiesn.update_helper import deployment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "inspect", "install", "verify-installed"))
    parser.add_argument("--bundle", help="Exact new output for build; existing reviewed input for inspect/install")
    parser.add_argument("--approve-sha256")
    parser.add_argument("--acknowledge-root-service-files", action="store_true")
    args = parser.parse_args(argv)
    if (args.action != "verify-installed") != bool(args.bundle):
        parser.error("bundle is required except for verify-installed")
    if args.action != "install" and (args.approve_sha256 or args.acknowledge_root_service_files):
        parser.error("approval is only valid for install")
    if args.action == "install" and not (args.approve_sha256 and args.acknowledge_root_service_files):
        parser.error("install requires matching hash approval and acknowledgement")
    try:
        if args.action == "build":
            if os.geteuid() == 0:
                raise deployment.DeploymentRejected("ADMIN_REQUIRED")  # do not build from a checkout as root
            data = deployment.build_bundle(Path(__file__).resolve().parents[1])
            fd = os.open(args.bundle, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            result = {"state": "BUNDLE_BUILT", "sha256": deployment.sha(data), "bytes": len(data)}
        elif args.action == "verify-installed":
            deployment.verify_deployment()
            result = {"state": "SERVICE_FILES_VERIFIED"}
        else:
            fd = os.open(args.bundle, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise deployment.DeploymentRejected("BUNDLE_INVALID")
                data = source.read(deployment.MAX_BUNDLE + 1)
            digest = deployment.verify_bundle(data)
            result = {"state": "BUNDLE_VERIFIED", "sha256": digest, "publisher_signature_verified": False}
            if args.action == "install":
                result = deployment.provision(data, approve_sha256=args.approve_sha256, approve=True)
        result["installation_available"] = False
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        code = error.code if isinstance(error, deployment.DeploymentRejected) else "DEPLOYMENT_FAILED"
        print(json.dumps({"code": code, "installation_available": False,
                          "partial_state_may_remain": args.action in {"build", "install"}}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
