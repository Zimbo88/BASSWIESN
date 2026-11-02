"""Persistent virtual identities: local files only, no Docker or network."""
import importlib
import json
import os
from pathlib import Path
import stat
import sys

import pytest

MODULES = Path(__file__).resolve().parents[1] / 'modules/airplay-bridge'
sys.path.insert(0, str(MODULES))
identity = importlib.import_module('receiver_identity')
dhcp = importlib.import_module('pilot_dhcp')
sys.path.remove(str(MODULES))
pytestmark = pytest.mark.unit


@pytest.fixture
def registry(tmp_path, monkeypatch):
    # Exercise real files, atomic replacement and flock, without requiring root.
    # Privileged ancestor validation is tested separately in namespace tests.
    original_stat = os.fstat
    def root_stat(fd):
        value = list(original_stat(fd))
        value[4] = 0
        return os.stat_result(value)
    def private_path(path, *, directory=False, private=False):
        path = Path(path)
        info = path.lstat()
        assert not stat.S_ISLNK(info.st_mode)
        assert stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        assert directory or info.st_nlink == 1
        assert not private or not info.st_mode & 0o077
        return path
    monkeypatch.setattr(identity.os, 'fstat', root_stat)
    monkeypatch.setattr(identity, 'root_path', private_path)
    monkeypatch.setattr(dhcp, 'root_path', private_path)
    root = tmp_path / 'registry'
    root.mkdir(mode=0o700)
    return root


def test_keys_survive_restart_reordering_and_temporary_absence(registry):
    initial = identity.identity_keys(registry, ['EXAMPLE_A', 'EXAMPLE_B'])
    assert initial['EXAMPLE_A'] != initial['EXAMPLE_B']
    path = registry / 'identities.json'
    first = path.read_bytes()
    inode = path.stat().st_ino
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert identity.identity_keys(registry, ['EXAMPLE_B']) == {'EXAMPLE_B': initial['EXAMPLE_B']}
    assert path.read_bytes() == first and path.stat().st_ino == inode
    expanded = identity.identity_keys(registry, ['EXAMPLE_C', 'EXAMPLE_A'])
    assert expanded['EXAMPLE_A'] == initial['EXAMPLE_A']
    assert identity.identity_keys(registry, ['EXAMPLE_B', 'EXAMPLE_A']) == initial
    assert len({identity.virtual_mac(k) for k in (*initial.values(), expanded['EXAMPLE_C'])}) == 3


def test_registry_lock_blocks_other_writer_but_recovers_after_release(registry):
    with identity.registry_lock(registry):
        with pytest.raises(BlockingIOError):
            identity.identity_keys(registry, ['EXAMPLE_A'])
    assert not (registry / 'identities.json').exists()
    assert identity.identity_keys(registry, ['EXAMPLE_A'])


@pytest.mark.parametrize('value', [[], {}, ['A', 'A'], ['../A'], ['a'], [None], ['A'] * 17])
def test_bad_inventory_has_no_disk_side_effect(registry, value):
    with pytest.raises(ValueError):
        identity.identity_keys(registry, value)
    assert not list(registry.iterdir())


@pytest.mark.parametrize('value', [
    {'version': True, 'identities': {}}, {'version': 2, 'identities': {}},
    {'version': 1, 'identities': [], 'extra': 0},
    {'version': 1, 'identities': {'A': 'not-a-key'}},
    {'version': 1, 'identities': {'A': 'a' * 32, 'B': 'a' * 32}},
])
def test_invalid_existing_registry_never_silently_rekeys(registry, value):
    path = registry / 'identities.json'
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        identity.identity_keys(registry, ['EXAMPLE_A'])
    assert path.read_bytes() == before


@pytest.mark.parametrize('unsafe', ['symlink', 'hardlink', 'permissions'])
def test_unsafe_registry_never_overwritten(registry, unsafe):
    other = registry / 'other'
    other.write_text('{"version":1,"identities":{}}')
    other.chmod(0o600)
    path = registry / 'identities.json'
    if unsafe == 'symlink':
        path.symlink_to(other)
    elif unsafe == 'hardlink':
        os.link(other, path)
    else:
        path.write_bytes(other.read_bytes())
        path.chmod(0o644)
    before = other.read_bytes()
    with pytest.raises(AssertionError):
        identity.identity_keys(registry, ['EXAMPLE_A'])
    assert other.read_bytes() == before


def test_mac_collision_during_allocation_keeps_previous_registry(registry, monkeypatch):
    initial = identity.identity_keys(registry, ['EXAMPLE_A'])
    before = (registry / 'identities.json').read_bytes()
    monkeypatch.setattr(identity.secrets, 'token_hex', lambda size: initial['EXAMPLE_A'])
    with pytest.raises(ValueError, match='ALLOCATION_FAILED'):
        identity.identity_keys(registry, ['EXAMPLE_B'])
    assert (registry / 'identities.json').read_bytes() == before


def test_save_failure_does_not_return_unpersisted_identity(registry, monkeypatch):
    def failed(*args, **kwargs):
        raise OSError('simulated disk full')
    monkeypatch.setattr(identity, 'save_json', failed)
    with pytest.raises(OSError):
        identity.identity_keys(registry, ['EXAMPLE_A'])
    assert not (registry / 'identities.json').exists()


def test_readback_mismatch_does_not_authorize_provisioning(registry, monkeypatch):
    monkeypatch.setattr(identity, 'read_root_json', lambda *a, **k: {})
    with pytest.raises(ValueError, match='READBACK_MISMATCH'):
        identity.identity_keys(registry, ['EXAMPLE_A'])
