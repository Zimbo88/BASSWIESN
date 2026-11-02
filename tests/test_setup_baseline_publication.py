"""Baseline publication must not truncate, follow links or leak private state."""
from hashlib import sha256
import os
import stat

import pytest

from basswiesn.app.services.setup_rebuild import backup


@pytest.mark.parametrize('mask', [0, 0o002, 0o022, 0o077])
def test_baseline_modes_do_not_inherit_permissive_umask(tmp_path, mask):
    root = tmp_path / 'capture' / 'baseline'
    previous = os.umask(mask)
    try:
        result = backup.write_artifact(root, 'info.xml', '<info/>')
    finally:
        os.umask(previous)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / 'info.xml').stat().st_mode) == 0o600
    assert result.sha256 == sha256((root / 'info.xml').read_bytes()).hexdigest()
    assert result.size == 7


@pytest.mark.parametrize('name', ['../escape', '..', '/absolute', '.hidden', 'a/b', 'x' * 129, 'credentials.json'])
def test_unsafe_artifact_name_rejected_before_file_creation(tmp_path, name):
    with pytest.raises(ValueError):
        backup.write_artifact(tmp_path / 'new', name, 'state')
    assert not (tmp_path / 'new').exists()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'directory'])
def test_existing_special_or_linked_artifact_does_not_touch_other_files(tmp_path, kind):
    outside = tmp_path / 'outside'
    outside.write_bytes(b'unchanged')
    root = tmp_path / 'baseline'
    root.mkdir(mode=0o700)
    leaf = root / 'info.xml'
    if kind == 'symlink': leaf.symlink_to(outside)
    if kind == 'hardlink': os.link(outside, leaf)
    if kind == 'fifo': os.mkfifo(leaf)
    if kind == 'directory': leaf.mkdir()
    with pytest.raises(ValueError):
        backup.write_artifact(root, 'info.xml', '<info/>')
    assert outside.read_bytes() == b'unchanged'


def test_parent_symlink_is_not_followed(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    root = tmp_path / 'baseline'
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        backup.write_artifact(root, 'info.xml', '<info/>')
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('failure', ['write', 'fsync', 'replace', 'zero_progress'])
def test_failed_update_keeps_old_capture_and_cleans_only_owned_temp(tmp_path, monkeypatch, failure):
    root = tmp_path / 'baseline'
    backup.write_artifact(root, 'info.xml', '<info>old</info>')
    def fail(*args, **kwargs):
        raise OSError('simulated failure')
    with monkeypatch.context() as scoped:
        if failure == 'zero_progress': scoped.setattr(backup.os, 'write', lambda *args: 0)
        else: scoped.setattr(backup.os, failure, fail)
        with pytest.raises(OSError):
            backup.write_artifact(root, 'info.xml', '<info>new</info>')
    assert (root / 'info.xml').read_text() == '<info>old</info>'
    assert sorted(path.name for path in root.iterdir()) == ['info.xml']


def test_legal_update_and_short_write_are_complete(tmp_path, monkeypatch):
    root = tmp_path / 'baseline'
    backup.write_artifact(root, 'info.xml', 'old')
    original = os.write
    with monkeypatch.context() as scoped:
        scoped.setattr(backup.os, 'write', lambda fd, data: original(fd, data[:2]))
        receipt = backup.write_artifact(root, 'info.xml', '<info>new</info>')
    assert (root / 'info.xml').read_text() == '<info>new</info>'
    assert receipt.sha256 == sha256((root / 'info.xml').read_bytes()).hexdigest()


def test_writable_root_and_parent_traversal_are_not_accepted(tmp_path):
    root = tmp_path / 'baseline'
    root.mkdir(mode=0o700)
    root.chmod(0o777)
    with pytest.raises(ValueError): backup.write_artifact(root, 'info.xml', 'x')
    with pytest.raises(ValueError): backup.write_artifact(root / '..' / 'escape', 'info.xml', 'x')
    with pytest.raises(ValueError): backup.write_artifact(root, 'info.xml', b'x' * 1048577)


def test_two_adapters_have_distinct_baseline_names():
    from basswiesn.app.services.setup_rebuild.radio_adapter import RadioSetupAdapter
    assert RadioSetupAdapter().timestamp != RadioSetupAdapter().timestamp
