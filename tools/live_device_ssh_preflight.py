#!/usr/bin/env python3
"""Safe SSH preflight for explicitly approved SoundTouch radios."""

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import shlex
import socket
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from basswiesn.app.adapters.ssh import (
    build_legacy_ssh_command,
    legacy_ssh_options,
)
from basswiesn.app.core.masterlog import write_masterlog


PREFLIGHT_COMMAND = """echo BASSWIESN_SSH_OK
echo __BASSWIESN_HOSTNAME__; hostname
echo __BASSWIESN_DATE__; date
echo __BASSWIESN_UPTIME__; uptime
echo __BASSWIESN_UNAME__; uname -a
echo __BASSWIESN_INTERFACES__; ifconfig 2>/dev/null || ip addr 2>/dev/null || true
echo __BASSWIESN_REMOTE_SERVICES__; test -e /mnt/nv/remote_services && echo true || echo false
echo __BASSWIESN_SETTINGS_DIR__; test -d /mnt/nv/Settings && echo true || echo false
echo __BASSWIESN_PATHS__; ls -ld /mnt/nv /mnt/nv/Settings 2>/dev/null || true"""

FIELD_MARKERS = {
    "__BASSWIESN_HOSTNAME__": "hostname",
    "__BASSWIESN_DATE__": "date",
    "__BASSWIESN_UPTIME__": "uptime",
    "__BASSWIESN_UNAME__": "uname",
    "__BASSWIESN_INTERFACES__": "interfaces",
    "__BASSWIESN_REMOTE_SERVICES__": "remote_services_present",
    "__BASSWIESN_SETTINGS_DIR__": "settings_dir_present",
    "__BASSWIESN_PATHS__": None,
}


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise argparse.ArgumentTypeError("expected true or false")
    return normalized == "true"


def empty_result(ip_address: str, timeout: int) -> dict:
    return {
        "ip": ip_address,
        "tcp_22_open": False,
        "ssh_login_ok": False,
        "legacy_options_used": list(
            legacy_ssh_options(connect_timeout=timeout, batch_mode=True)
        ),
        "hostname": "",
        "date": "",
        "uptime": "",
        "uname": "",
        "interfaces": "",
        "remote_services_present": "unknown",
        "settings_dir_present": "unknown",
        "errors": [],
    }


def tcp_port_open(
    ip_address: str,
    timeout: int,
    *,
    connection_factory=socket.create_connection,
) -> tuple[bool, str]:
    try:
        connection = connection_factory((ip_address, 22), timeout=timeout)
        connection.close()
        return True, ""
    except OSError as exc:
        return False, str(exc)


def parse_preflight_output(output: str) -> dict:
    parsed = {
        "ssh_login_ok": "BASSWIESN_SSH_OK" in output.splitlines(),
        "hostname": "",
        "date": "",
        "uptime": "",
        "uname": "",
        "interfaces": "",
        "remote_services_present": "unknown",
        "settings_dir_present": "unknown",
    }
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in output.splitlines():
        marker_field = FIELD_MARKERS.get(line)
        if line in FIELD_MARKERS:
            current = marker_field
            if current:
                sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    for field, lines in sections.items():
        value = "\n".join(lines).strip()
        if field in {"remote_services_present", "settings_dir_present"}:
            value = value if value in {"true", "false"} else "unknown"
        parsed[field] = value
    return parsed


def manual_command(ip_address: str, username: str, timeout: int) -> list[str]:
    return build_legacy_ssh_command(
        ip_address,
        username,
        "echo BASSWIESN_SSH_OK",
        connect_timeout=timeout,
        batch_mode=False,
    )


def check_device(
    ip_address: str,
    username: str,
    timeout: int,
    *,
    interactive: bool = False,
    connection_factory=socket.create_connection,
    runner=subprocess.run,
) -> dict:
    result = empty_result(ip_address, timeout)
    write_masterlog("live_ssh_preflight_start", ip_address=ip_address)
    tcp_ok, tcp_error = tcp_port_open(
        ip_address, timeout, connection_factory=connection_factory
    )
    result["tcp_22_open"] = tcp_ok
    write_masterlog(
        "live_ssh_preflight_tcp",
        ip_address=ip_address,
        open=tcp_ok,
        error_reason=tcp_error or None,
    )
    if not tcp_ok:
        result["errors"].append(f"TCP 22: {tcp_error or 'unreachable'}")
        write_masterlog(
            "live_ssh_preflight_complete",
            ip_address=ip_address,
            tcp_22_open=False,
            ssh_login_ok=False,
        )
        return result

    command = build_legacy_ssh_command(
        ip_address,
        username,
        PREFLIGHT_COMMAND,
        connect_timeout=timeout,
    )
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout + 4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["errors"].append(f"SSH: {exc}")
        completed = None

    if completed is not None and completed.returncode == 0:
        parsed = parse_preflight_output(completed.stdout)
        result.update(parsed)
        if result["ssh_login_ok"]:
            write_masterlog("live_ssh_preflight_login_ok", ip_address=ip_address)
        else:
            result["errors"].append("SSH output did not contain BASSWIESN_SSH_OK")
    else:
        reason = (
            (completed.stderr or "SSH BatchMode login failed").strip()[:300]
            if completed is not None
            else result["errors"][-1]
        )
        result["errors"].append(reason) if reason not in result["errors"] else None
        write_masterlog(
            "live_ssh_preflight_login_failed",
            ip_address=ip_address,
            error_reason=reason,
        )
        suggested = manual_command(ip_address, username, timeout)
        print(f"{ip_address}: BatchMode failed. Try manual command: {shlex.join(suggested)}")
        if interactive:
            try:
                manual = runner(suggested, timeout=timeout + 60, check=False)
                if manual.returncode != 0:
                    result["errors"].append(
                        f"interactive SSH attempt exited {manual.returncode}"
                    )
            except (OSError, subprocess.TimeoutExpired) as exc:
                result["errors"].append(f"interactive SSH: {exc}")

    if not result["ssh_login_ok"] and completed is not None and completed.returncode == 0:
        write_masterlog(
            "live_ssh_preflight_login_failed",
            ip_address=ip_address,
            error_reason="success marker missing",
        )
    write_masterlog(
        "live_ssh_preflight_complete",
        ip_address=ip_address,
        tcp_22_open=result["tcp_22_open"],
        ssh_login_ok=result["ssh_login_ok"],
        remote_services_present=result["remote_services_present"],
        settings_dir_present=result["settings_dir_present"],
    )
    return result


def build_payload(devices: list[dict]) -> dict:
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "read-only SSH preflight",
        "devices": devices,
    }


def write_output(payload: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = output_dir / f"device-ssh-preflight-{stamp}.json"
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ips", nargs="+", help="Explicitly approved radio IPs")
    parser.add_argument("--username", default="root")
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--interactive", type=parse_bool, default=False)
    parser.add_argument("--output-dir", type=Path, default=Path("data/live-tests"))
    args = parser.parse_args()
    devices = [
        check_device(
            ip,
            args.username,
            args.timeout,
            interactive=args.interactive,
        )
        for ip in args.ips
    ]
    payload = build_payload(devices)
    output = write_output(payload, args.output_dir)
    print(output)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(device["ssh_login_ok"] for device in devices) else 2


if __name__ == "__main__":
    raise SystemExit(main())
