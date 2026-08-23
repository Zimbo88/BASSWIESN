#!/usr/bin/env python3
"""Non-interactive, read-only SSH matrix for approved SoundTouch radios."""

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.adapters.ssh import build_legacy_ssh_command


COMMANDS = {
    "hostname": "hostname",
    "uptime": "uptime",
    "uname": "uname -a",
    "config_files": "for p in /mnt/nv/remote_services /mnt/nv/Settings/SystemConfigurationDB.xml /mnt/nv/Settings/CurrentSystemConfiguration.xml /mnt/nv/SoundTouchSdkPrivateCfg.xml /mnt/nv/OverrideSdkPrivateCfg.xml; do [ -e \"$p\" ] && ls -ld \"$p\"; done",
}


def ssh_command(ip_address: str, username: str, remote_command: str, timeout: int) -> list[str]:
    return build_legacy_ssh_command(
        ip_address,
        username,
        remote_command,
        connect_timeout=timeout,
    )


def check_device(ip_address: str, username: str, timeout: int) -> dict:
    result = {"ip": ip_address, "ssh_reachable": False, "commands": {}, "errors": {}}
    for name, remote_command in COMMANDS.items():
        write_masterlog("live_ssh_read_start", ip_address=ip_address, command=name)
        try:
            completed = subprocess.run(
                ssh_command(ip_address, username, remote_command, timeout),
                capture_output=True,
                text=True,
                timeout=timeout + 2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["errors"][name] = str(exc)
            write_masterlog("live_ssh_read_error", ip_address=ip_address, command=name, error_type=type(exc).__name__, error_reason=str(exc))
            break
        if completed.returncode != 0:
            reason = (completed.stderr or "SSH authentication or connection failed").strip()[:300]
            result["errors"][name] = reason
            write_masterlog("live_ssh_read_error", ip_address=ip_address, command=name, return_code=completed.returncode, error_reason=reason)
            break
        result["ssh_reachable"] = True
        result["commands"][name] = completed.stdout.strip()
        write_masterlog("live_ssh_read_complete", ip_address=ip_address, command=name, bytes=len(completed.stdout.encode()))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ips", nargs="+", help="Explicitly approved radio IP addresses")
    parser.add_argument("--username", default="root")
    parser.add_argument("--timeout", type=int, default=5)
    parser.add_argument("--output-dir", type=Path, default=Path("data/live-tests"))
    args = parser.parse_args()
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "read-only, BatchMode (no interactive password prompt)",
        "devices": [check_device(ip, args.username, args.timeout) for ip in args.ips],
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.output_dir / f"device-ssh-readonly-{stamp}.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(device["ssh_reachable"] for device in payload["devices"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
