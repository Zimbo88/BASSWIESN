#!/usr/bin/python3
"""Bounded pilot DHCP hook; only the explicit receiver namespace is writable.

The root owner provides BASSWIESN_AP2_CONFIG and BASSWIESN_AP2_NETNS. No
gateway, DNS, server-supplied routes, host addresses or host routes are changed.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

from network_contract import firewall_rules, validate_config, validate_lease


def root_path(path, *, directory=False, private=False):
    """Reject symlinks and writable/non-root ancestors before privileged IO."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute root-owned path required')
    for node in (*reversed(path.parents), path):
        info = node.lstat()
        expected_directory = node != path or directory
        if (info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode)
                or (expected_directory and not stat.S_ISDIR(info.st_mode))):
            raise ValueError('unsafe root-owned path')
        if node == path:
            if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise ValueError('regular root-owned file required')
            if private and info.st_mode & 0o077:
                raise ValueError('private root-owned path required')
    return path


def read_root_json(path, *, private=False):
    path = root_path(path, private=private)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_size > 32768:
            raise ValueError('invalid root configuration')
        value = json.load(stream)
    if type(value) is not dict:
        raise ValueError('configuration object required')
    return value


def save_json(path, value, *, mode=0o600):
    """New temporary inode, never follow/overwrite an existing temporary file."""
    path = Path(path)
    root_path(path.parent, directory=True)
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_lease(action, cfg, state, env, *, run=subprocess.run, now=time.monotonic):
    """Purely scoped command construction, separately exercised offline."""
    if action not in ('bound', 'renew', 'deconfig', 'leasefail', 'nak'):
        return None
    value = {'state': 'UNBOUND'}
    if action in ('bound', 'renew'):
        try:
            lease = validate_lease(cfg, env)
            previous = read_root_json(state) if state.exists() else {}
            if previous.get('address') != lease['address']:
                check = run(['/usr/bin/busybox', 'arping', '-D', '-c', '3', '-w', '4',
                    '-I', 'ap2', lease['address']], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=6)
                if check.returncode:
                    value = {'state': 'ADDRESS_CONFLICT'}
                    raise ValueError('address conflict')
            run(['/usr/sbin/ip', '-4', 'addr', 'replace',
                f"{lease['address']}/{lease['prefix']}", 'dev', 'ap2'],
                check=True, capture_output=True, timeout=5)
            value = {'state': 'BOUND', **lease,
                'expires_monotonic': now() + lease['lease_seconds'],
                'guard_sha256': sha256(firewall_rules(cfg).encode()).hexdigest()}
        except (ValueError, KeyError, subprocess.SubprocessError):
            if value['state'] != 'ADDRESS_CONFLICT':
                value = {'state': 'REJECTED'}
    if value['state'] != 'BOUND':
        run(['/usr/sbin/ip', '-4', 'addr', 'flush', 'dev', 'ap2'],
            check=True, capture_output=True, timeout=5)
    save_json(state, value, mode=0o644)
    return value


def main():
    if os.geteuid() != 0:
        raise ValueError('root namespace owner required')
    current = os.readlink('/proc/self/ns/net')
    if (current == os.readlink('/proc/1/ns/net')
            or current != os.environ.get('BASSWIESN_AP2_NETNS')):
        raise ValueError('explicit private namespace required')
    os.umask(0o077)
    path = Path(os.environ['BASSWIESN_AP2_CONFIG'])
    cfg = validate_config(read_root_json(path))
    if cfg['interface'] != 'ap2' or cfg.get('external_network') is not True:
        raise ValueError('owned receiver interface required')
    root_path(path.parent / 'network', directory=True)
    action = sys.argv[1] if len(sys.argv) == 2 else ''
    apply_lease(action, cfg, path.parent / 'network/lease.json', os.environ)


if __name__ == '__main__':
    main()
