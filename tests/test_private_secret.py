"""Private service-credential handling is tested without disclosing its bytes."""
import fcntl
import os
from pathlib import Path
import stat

import pytest

from basswiesn.app.core import private_secret as secret

pytestmark = pytest.mark.unit


@pytest.fixture
def target(tmp_path):
    return tmp_path / 'private' / 'token'


def test_create_once_private_and_never_rotate(target):
    first = secret.load_or_create(target)
    assert len(first) >= 40 and first == secret.load_or_create(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert not list(target.parent.glob('.secret-*'))


@pytest.mark.parametrize('kind', ['symlink', 'ancestor', 'hardlink', 'fifo', 'permissions', 'directory',
                                   'parent_permissions', 'lock_symlink', 'lock_permissions'])
def test_unsafe_path_is_not_read_repaired_or_replaced(target, kind):
    target.parent.mkdir(mode=0o700)
    outside = target.parent.parent / 'retained'
    outside.write_text('private fixture not to be returned')
    outside.chmod(0o600)
    if kind == 'symlink':
        target.symlink_to(outside)
    elif kind == 'ancestor':
        target.parent.rename(target.parent.with_name('retained-directory'))
        target.parent.symlink_to('retained-directory', target_is_directory=True)
    elif kind == 'hardlink':
        os.link(outside, target)
    elif kind == 'fifo':
        os.mkfifo(target, 0o600)
    elif kind == 'permissions':
        target.write_text('existing fixture')
        target.chmod(0o644)
    elif kind == 'directory':
        target.mkdir(mode=0o700)
    elif kind == 'parent_permissions':
        target.parent.chmod(0o777)
    elif kind == 'lock_symlink':
        Path(str(target) + '.lock').symlink_to(outside)
    else:
        lock = Path(str(target) + '.lock')
        lock.write_text('')
        lock.chmod(0o644)
    before = outside.read_bytes()
    with pytest.raises(secret.SecretFileUnavailable, match='^LOCAL_SECRET_UNAVAILABLE$'):
        secret.load_or_create(target)
    assert outside.read_bytes() == before


@pytest.mark.parametrize('data', [b'', b' ', b'x\ny', b'x\ry', b'x\0y', b'x' * 4097, b'\xff'])
def test_malformed_secret_remains_unchanged_and_is_not_echoed(target, data):
    target.parent.mkdir(mode=0o700)
    target.write_bytes(data)
    target.chmod(0o600)
    with pytest.raises(secret.SecretFileUnavailable) as failure:
        secret.load_or_create(target)
    assert str(failure.value) == 'LOCAL_SECRET_UNAVAILABLE'
    assert target.read_bytes() == data


def test_busy_creation_is_fail_closed_and_retry_after_owner_finishes_is_safe(target):
    first = secret.load_or_create(target)
    fd = os.open(str(target) + '.lock', os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(secret.SecretFileUnavailable):
            secret.load_or_create(target)
    finally:
        os.close(fd)
    assert secret.load_or_create(target) == first


@pytest.mark.parametrize('fault', ['short_write', 'fsync', 'concurrent_file'])
def test_creation_fault_never_publishes_partial_or_overwrites_existing_secret(target, monkeypatch, fault):
    if fault == 'short_write':
        monkeypatch.setattr(os, 'write', lambda *args: 0)
    elif fault == 'fsync':
        monkeypatch.setattr(os, 'fsync', lambda *_: (_ for _ in ()).throw(OSError('disk full')))
    else:
        real_link = os.link
        def other_writer(*args, **kwargs):
            target.write_text('preserve concurrent credential')
            target.chmod(0o600)
            return real_link(*args, **kwargs)
        monkeypatch.setattr(os, 'link', other_writer)
    with pytest.raises(secret.SecretFileUnavailable):
        secret.load_or_create(target)
    assert not list(target.parent.glob('.secret-*'))
    if fault == 'concurrent_file':
        assert target.read_text() == 'preserve concurrent credential'
    else:
        assert not target.exists()


def test_setup_adapter_uses_same_file_policy(target, monkeypatch):
    from basswiesn.app.config import get_settings
    from basswiesn.app.services.setup_rebuild.radio_adapter import _read_or_create_marge_auth_token
    monkeypatch.setattr(get_settings(), 'marge_auth_token_file', str(target))
    assert _read_or_create_marge_auth_token() == secret.load_or_create(target)
