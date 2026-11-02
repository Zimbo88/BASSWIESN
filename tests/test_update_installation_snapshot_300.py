"""Real filesystem/SQLite snapshot tests; no Docker or network access."""
from dataclasses import replace
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

import pytest

from basswiesn.update_helper import installation_snapshot as snapshot
from basswiesn.update_helper import local_docker, host_preflight
from tests.test_update_host_preflight_300 import enrolled  # noqa: F401
from tests.test_update_host_preflight_300 import substitute_child


pytestmark = pytest.mark.unit
REQUEST = "11223344-1234-4234-8234-123456789abc"


@pytest.fixture
def context(enrolled, tmp_path):
    policy = enrolled[0]
    backups, restores = tmp_path / "private-backups", tmp_path / "private-restores"
    backups.mkdir(mode=0o700)
    restores.mkdir(mode=0o700)
    root = Path(policy.root)
    data = root / "data"
    (data / "empty").mkdir(mode=0o750)
    (data / "settings.json").write_text('{"fixture": "original", "volume": 0}')
    (data / "settings.json").chmod(0o640)
    (data / "secrets/setup-rebuild/fixture.key").write_text("synthetic private fixture\n")
    (data / "secrets/setup-rebuild/fixture.key").chmod(0o600)
    with sqlite3.connect(data / "app.db") as database:
        database.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY, value TEXT)")
        database.execute("INSERT INTO fixture VALUES (1, 'old schema')")
    return policy, backups, restores


def capture(context, **kwargs):
    policy, backups, _ = context
    return snapshot.capture(policy, REQUEST, backup_root=str(backups), owner_uid=os.getuid(),
                            assert_stopped=kwargs.pop("assert_stopped", lambda _: True), **kwargs)


def verify(context, receipt):
    policy, backups, _ = context
    return snapshot.verify(receipt, policy, backup_root=str(backups), owner_uid=os.getuid())


def materialize(context, receipt, **kwargs):
    policy, backups, restores = context
    return snapshot.materialize(receipt, policy, backup_root=str(backups), restore_root=str(restores),
        owner_uid=os.getuid(), assert_stopped=kwargs.pop("assert_stopped", lambda _: True), **kwargs)


def tree_signature(root):
    values = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.stat()
        values[str(path.relative_to(root))] = (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid,
            info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
    return values


def test_real_backup_restore_recovers_old_schema_bytes_and_metadata(context):
    policy, backups, restores = context
    data = Path(policy.root) / "data"
    original = tree_signature(data)
    receipt = capture(context)
    contents = verify(context, receipt)
    assert contents["image_id"] == policy.image_id
    assert contents["version"] == "2.6.5"
    assert not any(secret in repr(receipt) for secret in (policy.root, policy.image_id, receipt.manifest_sha256))
    assert "synthetic private fixture" not in (backups / REQUEST / "manifest.json").read_text()
    for path in (backups / REQUEST).rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    with sqlite3.connect(data / "app.db") as database:
        database.execute("ALTER TABLE fixture ADD COLUMN newer TEXT")
        database.execute("UPDATE fixture SET value='candidate state', newer='changed'")
    (data / "settings.json").write_text('{"fixture": "candidate"}')
    changed = tree_signature(data)
    result = materialize(context, receipt)
    assert result == {"state": "RESTORE_TREE_VERIFIED", "live_files_replaced": False,
                      "application_health_verified": False}
    restored = restores / REQUEST
    assert tree_signature(restored / "data") == original
    assert tree_signature(data) == changed  # No overwrite/deletion of current files.
    assert (restored / ".env").read_bytes() == (Path(policy.root) / ".env").read_bytes()
    with sqlite3.connect(f"file:{restored / 'data/app.db'}?mode=ro", uri=True) as database:
        assert database.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert database.execute("SELECT * FROM fixture").fetchall() == [(1, "old schema")]
        assert len(database.execute("PRAGMA table_info(fixture)").fetchall()) == 2
    assert json.loads((restored / "restore-ready.json").read_bytes())["snapshot_sha256"] == receipt.manifest_sha256


def test_wal_database_is_captured_as_one_stopped_state(context):
    # Deliberately retain a committed WAL without automatic final-close
    # checkpoint. No thread writes while the synthetic stop proof holds.
    policy, _, restores = context
    data = Path(policy.root) / "data"
    database = sqlite3.connect(data / "app.db")
    try:
        assert database.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        database.execute("PRAGMA wal_autocheckpoint=0")
        database.execute("INSERT INTO fixture VALUES (2, 'committed WAL')")
        database.commit()
        assert (data / "app.db-wal").stat().st_size > 0
        receipt = capture(context)
    finally:
        database.close()
    materialize(context, receipt)
    with sqlite3.connect(restores / REQUEST / "data/app.db") as restored:
        assert restored.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert restored.execute("SELECT value FROM fixture ORDER BY id").fetchall() == [("old schema",), ("committed WAL",)]


def test_installer_setgid_directories_round_trip_exactly(context):
    data = Path(context[0].root) / "data"
    # install.sh uses group inheritance on runtime directories. It is not an
    # executable privilege bit and must survive a real update/rollback.
    for path in [data, *sorted(p for p in data.rglob("*") if p.is_dir())]:
        # Unprivileged fixture cannot chgrp to the real runtime group. Keep
        # group-write disabled here; its separate APP_GID guard stays enforced.
        path.chmod(0o2750)
    before = tree_signature(data)
    receipt = capture(context)
    materialize(context, receipt)
    assert tree_signature(context[2] / REQUEST / "data") == before


@pytest.mark.parametrize("kind,mode", [("file", 0o2640), ("file", 0o4640),
    ("directory", 0o4750), ("directory", 0o1750), ("directory", 0o2777)])
def test_special_privileges_stay_forbidden_even_with_rehashed_manifest(context, kind, mode):
    receipt = capture(context)
    manifest_path = context[1] / REQUEST / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    key = "data/settings.json" if kind == "file" else "data/empty"
    manifest["entries"][key]["mode"] = mode
    raw = snapshot._json(manifest)
    manifest_path.write_bytes(raw)
    receipt = replace(receipt, manifest_sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(snapshot.SnapshotRejected, match="^BACKUP_INVALID$"):
        materialize(context, receipt)
    assert list(context[2].iterdir()) == []


@pytest.mark.parametrize("return_value", [False, None, 1, "true"])
def test_capture_requires_explicit_quiescence_before_reserving(context, return_value):
    with pytest.raises(snapshot.SnapshotRejected, match="^WRITERS_NOT_STOPPED$"):
        capture(context, assert_stopped=lambda _: return_value)
    assert list(context[1].iterdir()) == []


def test_writer_restarts_during_capture_no_complete_receipt(context):
    calls = iter((True, False))
    with pytest.raises(snapshot.SnapshotRejected, match="^WRITERS_NOT_STOPPED$"):
        capture(context, assert_stopped=lambda _: next(calls))
    assert not (context[1] / REQUEST / "manifest.json").exists()
    with pytest.raises(snapshot.SnapshotRejected, match="^BACKUP_EXISTS$"):
        capture(context)


def test_mutated_file_after_copy_is_detected_and_never_committed(context):
    calls = []
    def stopped(_):
        calls.append(True)
        if len(calls) == 2:
            (Path(context[0].root) / "data/settings.json").write_text("changed during capture")
        return True
    with pytest.raises(snapshot.SnapshotRejected, match="^BACKUP_CHANGED$"):
        capture(context, assert_stopped=stopped)
    assert not (context[1] / REQUEST / "manifest.json").exists()


@pytest.mark.parametrize("kind", ["symlink", "directory-link", "hardlink", "fifo", "world-write", "special-mode", "xattr"])
def test_unsupported_data_entries_fail_closed(context, kind):
    policy, backups, _ = context
    path = Path(policy.root) / "data/unsafe"
    if kind == "symlink":
        path.symlink_to("settings.json")
    elif kind == "directory-link":
        path.symlink_to("empty", target_is_directory=True)
    elif kind == "hardlink":
        os.link(Path(policy.root) / "data/settings.json", path)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_text("fixture")
        path.chmod(0o666 if kind == "world-write" else 0o4600 if kind == "special-mode" else 0o600)
        if kind == "xattr":
            os.setxattr(path, "user.fixture", b"requires preservation")
    with pytest.raises(snapshot.SnapshotRejected):
        capture(context)
    assert list(backups.iterdir()) == []


@pytest.mark.parametrize("limits", [snapshot.Limits(entries=2), snapshot.Limits(total_bytes=100),
                                  snapshot.Limits(file_bytes=100), snapshot.Limits(seconds=0.00000001)])
def test_expansion_and_duration_limits(context, limits):
    with pytest.raises(snapshot.SnapshotRejected, match="^BACKUP_LIMIT$"):
        capture(context, limits=limits)


@pytest.mark.parametrize("kind", ["object-byte", "manifest-byte", "extra-object", "missing-object", "object-link", "object-mode"])
def test_restore_revalidates_all_private_contents_before_output(context, kind):
    receipt = capture(context)
    job = context[1] / REQUEST
    path = job / "objects/00000000"
    if kind == "object-byte":
        path.write_bytes(b"corrupt")
    elif kind == "manifest-byte":
        with (job / "manifest.json").open("ab") as output:
            output.write(b"\n")
    elif kind == "extra-object":
        (job / "objects/extra").write_bytes(b"")
    elif kind == "missing-object":
        path.rename(job / "retained-object")
    elif kind == "object-link":
        path.rename(job / "retained-object")
        path.symlink_to("../retained-object")
    else:
        path.chmod(0o644)
    with pytest.raises(snapshot.SnapshotRejected):
        materialize(context, receipt)
    assert list(context[2].iterdir()) == []


@pytest.mark.parametrize("field,value", [("installation_id", "22223344-1234-4234-8234-123456789abc"),
                                       ("version", "2.6.4"), ("manifest_sha256", "0" * 64)])
def test_receipt_belongs_to_exact_installation_and_version(context, field, value):
    receipt = replace(capture(context), **{field: value})
    with pytest.raises(snapshot.SnapshotRejected):
        materialize(context, receipt)


def test_materialize_refuses_running_writer_and_reuse(context):
    receipt = capture(context)
    with pytest.raises(snapshot.SnapshotRejected, match="^WRITERS_NOT_STOPPED$"):
        materialize(context, receipt, assert_stopped=lambda _: False)
    materialize(context, receipt)
    before = tree_signature(context[2] / REQUEST)
    with pytest.raises(snapshot.SnapshotRejected, match="^RESTORE_EXISTS$"):
        materialize(context, receipt)
    assert tree_signature(context[2] / REQUEST) == before


def test_disk_failure_retains_original_and_partial_reservation(context, monkeypatch):
    original = tree_signature(Path(context[0].root) / "data")
    real = snapshot._hash_file
    def fail(fd, **kwargs):
        if kwargs.get("sink") is not None:
            raise OSError("synthetic fixture, never log raw exception")
        return real(fd, **kwargs)
    monkeypatch.setattr(snapshot, "_hash_file", fail)
    with pytest.raises(snapshot.SnapshotRejected, match="^BACKUP_IO$"):
        capture(context)
    assert tree_signature(Path(context[0].root) / "data") == original
    assert (context[1] / REQUEST / "reservation.json").exists()
    assert not (context[1] / REQUEST / "manifest.json").exists()


def test_backup_cannot_capture_itself_and_restore_cannot_target_live_data(context):
    policy, backups, _ = context
    with pytest.raises(snapshot.SnapshotRejected, match="^BACKUP_UNSAFE$"):
        snapshot.capture(policy, REQUEST, assert_stopped=lambda _: True,
            backup_root=policy.root + "/data/backups", owner_uid=os.getuid())
    receipt = capture(context)
    with pytest.raises(snapshot.SnapshotRejected, match="^BACKUP_UNSAFE$"):
        snapshot.materialize(receipt, policy, assert_stopped=lambda _: True, backup_root=str(backups),
            restore_root=policy.root + "/data", owner_uid=os.getuid())


@pytest.mark.parametrize("kind", ["clean", "running", "wrong-image", "sibling-data", "parent-mount",
    "root-mount", "readonly-data", "unrelated-data", "new-container", "restarted-original"])
def test_real_adapter_checks_exact_stopped_container_and_other_writers(enrolled, monkeypatch, kind):
    policy = enrolled[0]
    original = {"Id": policy.container_id, "Image": policy.image_id,
        "State": {"Running": False, "Restarting": False, "Paused": False, "Dead": False,
                  "Pid": 0, "Status": "exited", "StartedAt": "synthetic-stable-start"}}
    other_id = "e" * 64
    mounts = []
    ids = []
    if kind == "running":
        original["State"].update(Running=True, Pid=123, Status="running")
    if kind == "wrong-image":
        original["Image"] = "sha256:" + "f" * 64
    paths = {"sibling-data": policy.root + "/data", "parent-mount": str(Path(policy.root).parent),
             "root-mount": "/", "readonly-data": policy.root + "/data", "unrelated-data": "/tmp/other-data"}
    if kind in paths:
        ids = [other_id]
        mounts = [{"Source": paths[kind], "Type": "bind", "RW": kind != "readonly-data"}]
    reads = []
    def command(args, deadline):
        reads.append(list(args))
        if args == local_docker.WRITER_LIST:
            return ids + ([other_id] if kind == "new-container" and reads.count(args) == 2 else [])
        assert args[:3] == ["inspect", "--type", "container"]
        if args[3] == policy.container_id:
            item = deepcopy(original)
            if kind == "restarted-original" and reads.count(args) == 2:
                item["State"]["StartedAt"] = "changed-start"
            return [item]
        assert args[3] == other_id
        return [{"Id": other_id, "Mounts": mounts}]
    monkeypatch.setattr(local_docker, "_trusted_tools", lambda: None)
    monkeypatch.setattr(local_docker, "_read_command", command)
    if kind in {"clean", "readonly-data", "unrelated-data"}:
        assert local_docker.confirm_stopped(policy) is True
    else:
        with pytest.raises(host_preflight.HostRejected):
            local_docker.confirm_stopped(policy)
    assert all(args == local_docker.WRITER_LIST or args[:3] == ["inspect", "--type", "container"] for args in reads)


@pytest.mark.parametrize("values,accepted", [([], True), (["a" * 64], True), (["a" * 64, "b" * 64], True),
    (["a" * 64, "a" * 64], False), (["truncated"], False), ([True], False),
    ([f"{i:064x}" for i in range(257)], False)])
def test_writer_list_bounded_real_child_parser(monkeypatch, tmp_path, values, accepted):
    seen = []
    monkeypatch.setattr(local_docker, "CLIENT_CONFIG", str(tmp_path))
    substitute_child(monkeypatch, "import json\nfor item in " + repr(values) + ": print(json.dumps(item))", seen)
    if accepted:
        assert local_docker._read_command(local_docker.WRITER_LIST, time.monotonic() + 2) == values
    else:
        with pytest.raises(host_preflight.HostRejected, match="^LOCAL_DOCKER_UNAVAILABLE$"):
            local_docker._read_command(local_docker.WRITER_LIST, time.monotonic() + 2)
    assert seen[0][0][-4:] == local_docker.WRITER_LIST


def test_zero_length_disk_write_fails_instead_of_spinning(context, monkeypatch):
    original = snapshot.os.write
    # Object copies have a 16-byte synthetic config block; manifest writes use
    # different sizes. Only the byte-copy helper is invoked for this regression.
    source, target = context[1] / "source", context[1] / "target"
    source.write_bytes(b"synthetic bytes!")
    monkeypatch.setattr(snapshot.os, "write", lambda fd, data: 0)
    with source.open("rb") as incoming, target.open("wb") as outgoing:
        with pytest.raises(OSError):
            snapshot._hash_file(incoming.fileno(), limit=1024, deadline=time.monotonic()+1, sink=outgoing.fileno())
    monkeypatch.setattr(snapshot.os, "write", original)
