"""Root-owned virtual identities, independent of run IDs and radio MACs.

Only an explicitly invoked provisioner calls this. No discovery, subprocess,
network or application startup hook. Retired records remain reserved so a
temporarily missing radio does not later acquire a different Apple identity.
"""
from contextlib import contextmanager
import fcntl
from hashlib import sha256
import os
from pathlib import Path
import re
import secrets
import stat

from pilot_dhcp import read_root_json, root_path, save_json


def virtual_mac(key):
    if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{32}', key):
        raise ValueError('AP2_LOCAL_IDENTITY_KEY_REQUIRED')
    digest = sha256(('basswiesn-ap2-network-v1\0' + key).encode()).hexdigest()
    return '02:' + ':'.join(digest[i:i+2] for i in range(0, 10, 2))


@contextmanager
def registry_lock(root):
    root_path(root, directory=True, private=True)
    fd = os.open(root / 'registry.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError('AP2_IDENTITY_REGISTRY_LOCK_UNSAFE')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def identity_keys(root, device_ids):
    """Allocate only missing keys, with verified persistent readback.

    The caller supplies its explicit approved radio inventory. This does not
    authorize a protected target or create any networking. Changing a radio's
    friendly name, IP or trial ID does not affect its virtual identity.
    """
    root = Path(root)
    if (not isinstance(device_ids, (tuple, list)) or not 1 <= len(device_ids) <= 16
            or any(not isinstance(key, str) or not re.fullmatch('[A-Z0-9_-]{1,128}', key)
                   for key in device_ids) or len(set(device_ids)) != len(device_ids)):
        raise ValueError('AP2_EXPLICIT_IDENTITIES_REQUIRED')
    with registry_lock(root):
        path = root / 'identities.json'
        value = read_root_json(path, private=True) if os.path.lexists(path) else {'version': 1, 'identities': {}}
        if (set(value) != {'version', 'identities'} or type(value['version']) is not int
                or value['version'] != 1 or not isinstance(value['identities'], dict)
                or len(value['identities']) > 128
                or any(not re.fullmatch('[A-Z0-9_-]{1,128}', key) for key in value['identities'])):
            raise ValueError('AP2_IDENTITY_REGISTRY_INVALID')
        entries = value['identities']
        addresses = {virtual_mac(key) for key in entries.values()}
        if len(addresses) != len(entries):
            raise ValueError('AP2_IDENTITY_REGISTRY_COLLISION')
        changed = False
        for device_id in device_ids:
            if device_id in entries:
                continue
            if len(entries) >= 128:
                raise ValueError('AP2_IDENTITY_REGISTRY_FULL')
            for _ in range(16):
                key = secrets.token_hex(16)
                address = virtual_mac(key)
                if address not in addresses:
                    break
            else:
                raise ValueError('AP2_IDENTITY_ALLOCATION_FAILED')
            entries[device_id] = key
            addresses.add(address)
            changed = True
        if changed:
            save_json(path, value)
            # Persist the rename as well as file contents before provisioning
            # any network interface with the new identity.
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            if read_root_json(path, private=True) != value:
                raise ValueError('AP2_IDENTITY_READBACK_MISMATCH')
        return {device_id: entries[device_id] for device_id in device_ids}
