#!/usr/bin/env python3
"""Explicit administrator onboarding from a trusted release, never a Web route.

The installed daemon executes only the sealed bundle. This one-time administrator
tool must itself be trusted, like install.sh. No shell expansion, secret output,
automatic repair of partial state, remote Docker context or radio access.
"""
import argparse
import grp
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from basswiesn.update_helper import deployment, onboarding, host_preflight as host


def installation_owner(root):
    """Trust the invoking administrator, not arbitrary owners found in a path.

    sudo installations commonly own the final directory as root while the home
    ancestor belongs to the administrator. Both are already trusted by explicit
    local enrollment. Keep rejecting unrelated owners, app UID and symlinks.
    A non-root process cannot choose its identity with an environment variable.
    """
    actor = os.geteuid()
    original = os.environ.get("SUDO_UID") if actor == 0 else None
    if original is not None:
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,9})", original):
            raise ValueError("INVALID_INSTALLATION_ADMINISTRATOR")
        actor = int(original)
    if not 0 <= actor < 2**32 - 1 or actor == host.APP_UID:
        raise ValueError("INVALID_INSTALLATION_ADMINISTRATOR")
    with host.directory(str(root), owners={0, actor}):
        pass
    return actor


def command(args):
    return subprocess.run(args, cwd="/", env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=30, check=True).stdout


def bootstrap():
    if os.geteuid() != 0 or sys.version_info < (3, 11) or not Path("/run/systemd/system").is_dir():
        raise ValueError("SUPPORTED_HOST_REQUIRED")
    if Path(onboarding.HOST_ROOT).exists():
        raise ValueError("EXISTING_OR_PARTIAL_ENROLLMENT")
    for name in deployment.UNITS:
        if (Path(deployment.UNIT_ROOT) / name).exists():
            raise ValueError("EXISTING_OR_PARTIAL_SERVICE")
    # systemd resolves SocketGroup through NSS even for a numeric identifier.
    # Docker's numeric container GID does not create a host group.
    try:
        grp.getgrgid(host.APP_GID)
    except KeyError:
        try:
            grp.getgrnam("basswiesn-update")
        except KeyError:
            command(["/usr/sbin/groupadd", "--system", "--gid", str(host.APP_GID), "basswiesn-update"])
        else:
            raise ValueError("HOST_GROUP_CONFLICT")
    fd = os.open("/run", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("UNSAFE_RUNTIME_DIRECTORY")
        try:
            os.mkdir("basswiesn-update", 0o755, dir_fd=fd)
            child = os.open("basswiesn-update", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                os.fchmod(child, 0o755)  # explicit mode, independent of root umask
            finally:
                os.close(child)
        except FileExistsError:
            pass
    finally:
        os.close(fd)
    with host.directory("/run/basswiesn-update", owners={0}, final_owners={0}) as fd:
        if os.listdir(fd) or stat.S_IMODE(os.fstat(fd).st_mode) != 0o755:
            raise ValueError("CONTROL_DIRECTORY_NOT_EMPTY")


def activate(args):
    if os.geteuid() != 0:
        raise ValueError("ADMIN_REQUIRED")
    # PrivateTmp deliberately hides host temporary paths from the daemon.
    # A persistent updater installation must not live in an ephemeral tree.
    host.absolute_path(args.installation_root)
    if any(Path(args.installation_root).is_relative_to(p) for p in ("/tmp", "/var/tmp", "/run")):
        raise ValueError("PERSISTENT_INSTALLATION_PATH_REQUIRED")
    data = deployment.build_bundle(args.installation_root)
    if not args.approve_sha256 or deployment.sha(data) != args.approve_sha256:
        raise ValueError("BUNDLE_APPROVAL_CHANGED")
    plan = onboarding.preview(root=args.installation_root,
        installation_owner_uid=args.installation_owner_uid, compose_project=args.compose_project,
        container_id=args.container_id, image_id=args.image_id, version=args.version)
    # The administrator supplied exact identities, not an automatically selected
    # arbitrary container. The runtime reader independently validates them.
    onboarding.prepare(plan, approve=True, approval_sha256=plan.approval_sha256)
    policy = host.load_enrollment(onboarding.HOST_ROOT+"/enrollment.json")
    from basswiesn.update_helper.local_docker import collect
    observation = collect(policy)
    host.inspect_runtime(policy, observation)
    mounts = observation.container["Mounts"]
    if not any(m["Source"] == "/run/basswiesn-update" and m["Destination"] == "/run/basswiesn-update"
               and m["RW"] is False for m in mounts):
        raise ValueError("CONTROL_MOUNT_REQUIRED")
    env = observation.container["Config"]["Env"]
    if "BASSWIESN_ENABLE_HTTPS=true" not in env or "BASSWIESN_HTTPS_PORT=1329" not in env:
        raise ValueError("HTTPS_REQUIRED")
    deployment.provision(data, approve=True, approve_sha256=args.approve_sha256)
    deployment.verify_deployment()
    command(["/usr/bin/systemctl", "daemon-reload"])
    command(["/usr/bin/systemctl", "enable", "--now", deployment.SOCKET_NAME])
    command(["/usr/bin/systemctl", "is-active", "--quiet", deployment.SOCKET_NAME])
    # Verify from the application's actual UID/mount, never impersonate a Web
    # credential in the administrator process. This is a STATUS request only.
    probe = "import asyncio; from basswiesn.app.services.update_control import status; v=asyncio.run(status()); assert v['ok'] and v['result']['executor_available']; print('READY')"
    result = command(["/usr/bin/docker", "--host", "unix:///run/docker.sock", "--config",
        onboarding.HOST_ROOT+"/docker-client", "exec", "--user", "10001:10001", args.container_id,
        "python", "-c", probe])
    if result.strip() != b"READY":
        raise ValueError("SERVICE_NOT_READY")
    return {"state": "READY", "installation_available": True, "requires_https": True,
            "administrator_code_required": True, "automatic_installation": False}


def show_code():
    # Deliberate local administrator delivery ONLY to a terminal. Never stdout,
    # journal, subprocess result, Web response or the installation directory.
    deployment.verify_deployment()
    root = onboarding.HOST_ROOT+"/service"
    with host.directory(root, owners={0}, private=True) as fd:
        value = host._regular_read(fd, "administrator.capability", owners={0}, private=True, limit=44)
    with open("/dev/tty", "wb", buffering=0) as tty:
        tty.write(b"\nBASSWIESN administrator code (keep private):\n"+value+b"\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("bootstrap", "activate", "show-code"))
    parser.add_argument("--approve-root-helper", action="store_true")
    parser.add_argument("--installation-root")
    parser.add_argument("--installation-owner-uid", type=int)
    parser.add_argument("--compose-project")
    parser.add_argument("--container-id")
    parser.add_argument("--image-id")
    parser.add_argument("--version")
    parser.add_argument("--approve-sha256")
    args = parser.parse_args(argv)
    if os.geteuid() != 0 or not args.approve_root_helper:
        parser.error("administrator privilege and --approve-root-helper are required")
    if args.action == "activate" and any(getattr(args, n) is None for n in
            ("installation_root", "installation_owner_uid", "compose_project", "container_id", "image_id", "version", "approve_sha256")):
        parser.error("activate requires exact installation identities and reviewed bundle hash")
    try:
        if args.action == "bootstrap":
            bootstrap()
            value = {"state": "CONTROL_DIRECTORY_READY", "installation_available": False}
        elif args.action == "show-code":
            show_code()
            value = {"state": "DELIVERED_TO_LOCAL_TERMINAL"}
        else:
            value = activate(args)
        print(json.dumps(value, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"state": "ONBOARDING_FAILED", "installation_available": False,
                          "partial_state_retained": True, "automatic_retry": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
