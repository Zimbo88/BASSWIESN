#!/usr/bin/env python3
"""Explicit host observer installation; preview is the default."""
import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

UNIT = """[Unit]
Description=BASSWIESN bounded host flight recorder
After=docker.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/basswiesn-observer/pi_observer.py --config /opt/basswiesn-observer/config.json
Restart=on-failure
RestartSec=15
UMask=0077
Nice=15
CPUQuota=15%
MemoryMax=128M
TasksMax=32
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/basswiesn-observer
PrivateTmp=true
RestrictAddressFamilies=AF_UNIX
IPAddressDeny=any
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--install", action="store_true")
    args=parser.parse_args()
    data=args.data_dir.resolve(strict=True)
    if not data.is_dir(): parser.error("Expected existing BASSWIESN data directory")
    source=Path(__file__).with_name("pi_observer.py")
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    print(json.dumps({"install":args.install, "observer_sha256":digest, "retention_days":14, "budget_gib":1,
                      "reserve_mib":512, "radio_requests":0}))
    if not args.install: return
    if os.geteuid() != 0: parser.error("Installation requires sudo")
    if not Path("/run/systemd/system").is_dir(): parser.error("systemd required")
    if not Path("/usr/bin/python3").is_file() or not shutil.which("docker"): parser.error("Python3 and Docker required")
    os.umask(0o077)
    base=Path("/opt/basswiesn-observer")
    output=Path("/var/lib/basswiesn-observer")
    unit=Path("/etc/systemd/system/basswiesn-observer.service")
    for target in (base, output, unit):
        if target.is_symlink(): raise PermissionError("Refusing symlink installation target")
    base.mkdir(exist_ok=True, mode=0o700); output.mkdir(exist_ok=True, mode=0o700)
    backup=base / ("install-backup-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    backup.mkdir(mode=0o700)
    for target in (base/"pi_observer.py", base/"config.json", unit):
        if target.is_symlink(): raise PermissionError("Refusing symlink installation file")
        if target.exists(): shutil.copy2(target, backup/target.name)
    (base/"pi_observer.py").write_bytes(source.read_bytes())
    (base/"config.json").write_text(json.dumps({"data_dir":str(data), "output_dir":str(output)}))
    unit.write_text(UNIT); unit.chmod(0o644)
    subprocess.run(["systemd-analyze", "verify", str(unit)], check=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "basswiesn-observer.service"], check=True)
    subprocess.run(["systemctl", "restart", "basswiesn-observer.service"], check=True)
    subprocess.run(["systemctl", "is-active", "--quiet", "basswiesn-observer.service"], check=True)


if __name__ == "__main__": main()
