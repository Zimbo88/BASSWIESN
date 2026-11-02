#!/usr/bin/env python3
"""udhcpc hook: configure only the probe interface, never host routes or DNS."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from network_contract import validate_config, validate_lease

CONFIG = Path('/config/receiver.json')
STATE = Path('/run/ap2/lease.json')


def save(value):
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(STATE)


def main():
    os.umask(0o077)
    cfg = validate_config(json.loads(CONFIG.read_text()))
    action = sys.argv[1] if len(sys.argv) == 2 else 'invalid'
    if action not in ('bound', 'renew', 'deconfig', 'leasefail', 'nak'):
        return
    interface = cfg['interface']
    if action in ('bound', 'renew'):
        try:
            lease = validate_lease(cfg, os.environ)
        except (ValueError, KeyError):
            save({'state': 'REJECTED'})
            subprocess.run(['ip', '-4', 'addr', 'flush', 'dev', interface], check=True)
            return
        # A lease is not proof against manually assigned overlapping devices.
        # Probe ONLY the offered address, after rejecting protected addresses.
        # Busybox arping -D exits nonzero on conflict; no address range scan.
        previous = json.loads(STATE.read_text()) if STATE.exists() else {}
        if previous.get('address') != lease['address']:
            conflict = subprocess.run(['busybox', 'arping', '-D', '-c', '3', '-w', '4',
                '-I', interface, lease['address']], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if conflict.returncode:
                save({'state': 'ADDRESS_CONFLICT'})
                subprocess.run(['ip', '-4', 'addr', 'flush', 'dev', interface], check=True)
                return
        subprocess.run(['ip', '-4', 'addr', 'replace', f"{lease['address']}/{lease['prefix']}",
                        'dev', interface], check=True)
        save({'state': 'BOUND', **lease, 'expires_monotonic': time.monotonic() + lease['lease_seconds']})
    else:
        save({'state': 'UNBOUND'})
        subprocess.run(['ip', '-4', 'addr', 'flush', 'dev', interface], check=True)


if __name__ == '__main__':
    main()
