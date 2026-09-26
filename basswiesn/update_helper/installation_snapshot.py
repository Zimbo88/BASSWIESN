"""Private stopped-writer snapshots and non-destructive restore preparation.

This module never stops/starts Docker, executes code, reads radio endpoints or
replaces live files. The host executor must prove quiescence before AND after
capture, then stop any candidate before using a prepared restore tree. A copied
SQLite DB/WAL pair is consistent only while ALL writers remain stopped.

Receipts and object contents stay in the root-private helper directory. Paths,
configuration hashes and private values are not public status fields. Unsupported
file types, extra mounts, ACL/xattrs and concurrent mutations fail closed rather
than producing a misleadingly complete backup. Originals are never deleted.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import PurePosixPath
import stat
import time

from .host_preflight import (APP_UID, Enrollment, RELEASE_FILES, absolute_path, directory,
                             inspect_files, inspect_space, unique_json)
from .onboarding import _json, _mkdir_new, _write_new
from .protocol import require_digest, require_request_id


BACKUP_ROOT = "/var/lib/basswiesn-update/backups"
CONFIG_FILES = RELEASE_FILES | {".env"}
MAX_MANIFEST = 16 * 1024 * 1024


class SnapshotRejected(ValueError):
    def __init__(self, code):
        self.code = code if code in {
            "BACKUP_EXISTS", "BACKUP_UNSAFE", "BACKUP_CHANGED", "BACKUP_LIMIT",
            "BACKUP_IO", "BACKUP_INVALID", "BACKUP_HASH_MISMATCH", "WRITERS_NOT_STOPPED",
            "RESTORE_EXISTS", "RESTORE_METADATA_UNSUPPORTED",
        } else "BACKUP_INVALID"
        super().__init__(self.code)


@dataclass(frozen=True)
class Limits:
    entries: int = 20000
    total_bytes: int = 4 * 1024**3
    file_bytes: int = 2 * 1024**3
    seconds: float = 300.0

    def __post_init__(self):
        if (type(self.entries) is not int or not 1 <= self.entries <= 20000
                or type(self.file_bytes) is not int or not 0 < self.file_bytes <= 2 * 1024**3
                or type(self.total_bytes) is not int or not 0 < self.total_bytes <= 4 * 1024**3
                or type(self.seconds) not in (int, float) or not 0 < self.seconds <= 1800):
            raise SnapshotRejected("BACKUP_LIMIT")


@dataclass(frozen=True, repr=False)
class Snapshot:
    request_id: str
    installation_id: str
    version: str
    manifest_sha256: str


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _new_directory(parent, name, owner_uid):
    _mkdir_new(parent, name, owner_uid)
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _clock(deadline):
    if time.monotonic() >= deadline:
        raise SnapshotRejected("BACKUP_LIMIT")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_uid, info.st_gid, info.st_mode, info.st_nlink)


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or str(PurePosixPath(value)) != value or value.startswith("/")
            or "\\" in value or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or len(value.split("/")) > 32 or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise SnapshotRejected("BACKUP_UNSAFE")
    return value


@contextmanager
def _parent(root, path):
    parts = _path(path).split("/")
    fd = os.dup(root)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _metadata(fd, *, owners, device):
    info = os.fstat(fd)
    is_file = stat.S_ISREG(info.st_mode)
    if (not (is_file or stat.S_ISDIR(info.st_mode)) or info.st_dev != device
            or info.st_uid not in owners or info.st_mode & 0o5002
            or (is_file and (info.st_nlink != 1 or info.st_mode & stat.S_ISGID))):
        raise SnapshotRejected("BACKUP_UNSAFE")
    # No silent loss of ACLs/security labels/capabilities. Support can be added
    # only together with verified round-trip restoration for that metadata.
    if os.listxattr(fd):
        raise SnapshotRejected("RESTORE_METADATA_UNSUPPORTED")
    return info, {"kind": "file" if is_file else "directory", "uid": info.st_uid,
                  "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode), "mtime_ns": info.st_mtime_ns}


def _hash_file(fd, *, limit, deadline, sink=None):
    digest, size = hashlib.sha256(), 0
    while True:
        _clock(deadline)
        block = os.read(fd, min(65536, limit + 1 - size))
        if not block:
            return digest.hexdigest(), size
        size += len(block)
        if size > limit:
            raise SnapshotRejected("BACKUP_LIMIT")
        digest.update(block)
        if sink is not None:
            view = memoryview(block)
            while view:
                _clock(deadline)
                written = os.write(sink, view)
                if written <= 0:
                    raise OSError()
                view = view[written:]


def _inventory(root, policy, limits, deadline, *, objects=None, owner_uid=0, paths=None):
    entries, total, files = {}, 0, 0
    owners = {0, policy.installation_owner_uid, APP_UID}

    def visit(parent, leaf, relative, device):
        nonlocal total, files
        _clock(deadline)
        _path(relative)
        if len(entries) >= limits.entries:
            raise SnapshotRejected("BACKUP_LIMIT")
        linked = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if not (stat.S_ISREG(linked.st_mode) or stat.S_ISDIR(linked.st_mode)):
            raise SnapshotRejected("BACKUP_UNSAFE")
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before, entry = _metadata(fd, owners=owners, device=device)
            if _identity(linked) != _identity(before):
                raise SnapshotRejected("BACKUP_CHANGED")
            entries[relative] = entry
            if entry["kind"] == "directory":
                names = []
                with os.scandir(fd) as children:
                    for child in children:
                        _clock(deadline)
                        names.append(child.name)
                        if len(names) + len(entries) > limits.entries:
                            raise SnapshotRejected("BACKUP_LIMIT")
                for name in sorted(names):
                    visit(fd, name, relative + "/" + name, device)
            else:
                if before.st_size > limits.file_bytes or total + before.st_size > limits.total_bytes:
                    raise SnapshotRejected("BACKUP_LIMIT")
                entry["object"] = f"{files:08d}"
                files += 1
                sink = None
                try:
                    if objects is not None:
                        sink = os.open(entry["object"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                       0o600, dir_fd=objects)
                        os.fchmod(sink, 0o600)
                        if os.fstat(sink).st_uid != owner_uid:
                            raise SnapshotRejected("BACKUP_UNSAFE")
                    entry["sha256"], entry["size"] = _hash_file(fd, limit=limits.file_bytes, deadline=deadline, sink=sink)
                    if sink is not None:
                        os.fsync(sink)
                finally:
                    if sink is not None:
                        os.close(sink)
                if entry["size"] != before.st_size:
                    raise SnapshotRejected("BACKUP_CHANGED")
                total += entry["size"]
            if (_identity(before) != _identity(os.fstat(fd))
                    or _identity(before) != _identity(os.stat(leaf, dir_fd=parent, follow_symlinks=False))):
                raise SnapshotRejected("BACKUP_CHANGED")
        finally:
            os.close(fd)

    # The trusted cutover module also inventories explicit top-level release
    # entries. This option is not a request parameter and never follows links.
    if paths is not None:
        if (not isinstance(paths, list) or len(paths) != len(set(paths))
                or any(_path(p) != p or "/" in p for p in paths)):
            raise SnapshotRejected("BACKUP_UNSAFE")
        for path in sorted(paths):
            visit(root, path, path, os.fstat(root).st_dev)
        return entries, total
    # Only explicit enrolled release configuration and its complete data mount.
    # No checkout, .git, host home, arbitrary path or unrelated Docker data.
    for path in sorted(CONFIG_FILES):
        with _parent(root, path) as (parent, leaf):
            visit(parent, leaf, path, os.fstat(parent).st_dev)
    data_info = os.stat("data", dir_fd=root, follow_symlinks=False)
    if not stat.S_ISDIR(data_info.st_mode):
        raise SnapshotRejected("BACKUP_UNSAFE")
    visit(root, "data", "data", data_info.st_dev)
    return entries, total


def _stopped(check, policy):
    # Callback is supplied by trusted host code, never deserialized from a request.
    # An explicit True prevents accidentally using an observation that says None.
    if not callable(check) or check(policy) is not True:
        raise SnapshotRejected("WRITERS_NOT_STOPPED")


def _separate_root(path, installation):
    absolute_path(path)
    if path == installation or path.startswith(installation + "/") or installation.startswith(path + "/"):
        raise SnapshotRejected("BACKUP_UNSAFE")


def _receipt_check(receipt, policy):
    if (not isinstance(receipt, Snapshot) or receipt.installation_id != policy.installation_id
            or receipt.version != policy.version):
        raise SnapshotRejected("BACKUP_INVALID")
    require_request_id(receipt.request_id)
    require_digest(receipt.manifest_sha256)


def capture(policy: Enrollment, request_id, *, assert_stopped, backup_root=BACKUP_ROOT,
            owner_uid=0, limits=Limits()):
    """Capture to a new, never reused reservation; return only a private receipt."""
    require_request_id(request_id)
    _separate_root(backup_root, policy.root)
    deadline = time.monotonic() + limits.seconds
    _stopped(assert_stopped, policy)
    inspect_files(policy)
    try:
        with directory(policy.root, owners={0, policy.installation_owner_uid}) as source:
            before, size = _inventory(source, policy, limits, deadline)
            inspect_space(backup_root, owner_uid=owner_uid, required_bytes=size + MAX_MANIFEST + 1024 * 1024)
            with directory(backup_root, owners={0, owner_uid}, final_owners={owner_uid}, private=True) as base:
                try:
                    job = _new_directory(base, request_id, owner_uid)
                except FileExistsError:
                    raise SnapshotRejected("BACKUP_EXISTS") from None
                try:
                    _write_new(job, "reservation.json", _json({"schema": 1, "request_id": request_id}), owner_uid)
                    objects = _new_directory(job, "objects", owner_uid)
                    try:
                        actual, actual_size = _inventory(source, policy, limits, deadline, objects=objects, owner_uid=owner_uid)
                        os.fsync(objects)
                    finally:
                        os.close(objects)
                    _stopped(assert_stopped, policy)
                    inspect_files(policy)
                    final, final_size = _inventory(source, policy, limits, deadline)
                    if actual != before or final != before or size != actual_size or size != final_size:
                        raise SnapshotRejected("BACKUP_CHANGED")
                    manifest = _json({"schema": 1, "request_id": request_id, "installation_id": policy.installation_id,
                        "version": policy.version, "image_id": policy.image_id,
                        "container_id": policy.container_id, "total_bytes": size, "entries": actual})
                    if len(manifest) > MAX_MANIFEST:
                        raise SnapshotRejected("BACKUP_LIMIT")
                    _write_new(job, "manifest.json", manifest, owner_uid)
                    receipt = Snapshot(request_id, policy.installation_id, policy.version, _sha(manifest))
                finally:
                    os.close(job)
        verify(receipt, policy, backup_root=backup_root, owner_uid=owner_uid, limits=limits)
        _stopped(assert_stopped, policy)
        return receipt
    except OSError:
        raise SnapshotRejected("BACKUP_IO") from None


def _manifest(job, receipt, policy, owner_uid, limits):
    from .host_preflight import _regular_read
    if set(os.listdir(job)) != {"reservation.json", "manifest.json", "objects"}:
        raise SnapshotRejected("BACKUP_INVALID")
    reservation = _regular_read(job, "reservation.json", owners={owner_uid}, limit=4096, private=True)
    if reservation != _json({"schema": 1, "request_id": receipt.request_id}):
        raise SnapshotRejected("BACKUP_INVALID")
    raw = _regular_read(job, "manifest.json", owners={owner_uid}, limit=MAX_MANIFEST, private=True)
    if _sha(raw) != receipt.manifest_sha256:
        raise SnapshotRejected("BACKUP_HASH_MISMATCH")
    value = unique_json(raw)
    if (not isinstance(value, dict) or set(value) != {"schema", "request_id", "installation_id", "version",
            "image_id", "container_id", "total_bytes", "entries"} or type(value["schema"]) is not int
            or value["schema"] != 1 or value["request_id"] != receipt.request_id
            or value["installation_id"] != policy.installation_id or value["version"] != policy.version
            or value["image_id"] != policy.image_id or value["container_id"] != policy.container_id
            or not isinstance(value["entries"], dict) or not 1 <= len(value["entries"]) <= limits.entries):
        raise SnapshotRejected("BACKUP_INVALID")
    entries, objects, total = value["entries"], set(), 0
    if not CONFIG_FILES | {"data"} <= entries.keys():
        raise SnapshotRejected("BACKUP_INVALID")
    for path, entry in entries.items():
        _path(path)
        if (path not in CONFIG_FILES and path != "data" and not path.startswith("data/")) or not isinstance(entry, dict):
            raise SnapshotRejected("BACKUP_INVALID")
        kind = entry.get("kind")
        expected = {"kind", "uid", "gid", "mode", "mtime_ns"} | ({"size", "sha256", "object"} if kind == "file" else set())
        if (kind not in {"file", "directory"} or set(entry) != expected
                or any(type(entry[k]) is not int for k in ("uid", "gid", "mode", "mtime_ns"))
                or entry["uid"] not in {0, policy.installation_owner_uid, APP_UID}
                or not 0 <= entry["gid"] < 2**32 - 1 or not 0 <= entry["mode"] <= 0o2777
                or entry["mode"] & 0o5002 or (kind == "file" and entry["mode"] & stat.S_ISGID)
                or not 0 <= entry["mtime_ns"] < 2**63
                or (path in CONFIG_FILES and kind != "file") or (path == "data" and kind != "directory")):
            raise SnapshotRejected("BACKUP_INVALID")
        parent = str(PurePosixPath(path).parent)
        if path.startswith("data/") and (parent not in entries or entries[parent].get("kind") != "directory"):
            raise SnapshotRejected("BACKUP_INVALID")
        if kind == "file":
            number = entry["object"]
            if (not isinstance(number, str) or len(number) != 8 or not number.isascii() or not number.isdigit()
                    or number in objects or type(entry["size"]) is not int or not 0 <= entry["size"] <= limits.file_bytes):
                raise SnapshotRejected("BACKUP_INVALID")
            require_digest(entry["sha256"])
            expected_hash = policy.env_sha256 if path == ".env" else policy.file_sha256.get(path)
            if expected_hash is not None and entry["sha256"] != expected_hash:
                raise SnapshotRejected("BACKUP_INVALID")
            objects.add(number)
            total += entry["size"]
    if (type(value["total_bytes"]) is not int or value["total_bytes"] != total or total > limits.total_bytes
            or objects != {f"{i:08d}" for i in range(len(objects))}):
        raise SnapshotRejected("BACKUP_INVALID")
    return value


def _verify_objects(job, manifest, owner_uid, deadline, limits):
    fd = os.open("objects", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=job)
    try:
        info = os.fstat(fd)
        expected = {e["object"] for e in manifest["entries"].values() if e["kind"] == "file"}
        if info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o700 or set(os.listdir(fd)) != expected:
            raise SnapshotRejected("BACKUP_UNSAFE")
        for entry in manifest["entries"].values():
            if entry["kind"] != "file":
                continue
            raw = os.open(entry["object"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                before = os.fstat(raw)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != owner_uid or before.st_nlink != 1
                        or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size != entry["size"]):
                    raise SnapshotRejected("BACKUP_UNSAFE")
                if _hash_file(raw, limit=limits.file_bytes, deadline=deadline) != (entry["sha256"], entry["size"]):
                    raise SnapshotRejected("BACKUP_HASH_MISMATCH")
                if _identity(before) != _identity(os.fstat(raw)):
                    raise SnapshotRejected("BACKUP_CHANGED")
            finally:
                os.close(raw)
    finally:
        os.close(fd)


def verify(receipt, policy, *, backup_root=BACKUP_ROOT, owner_uid=0, limits=Limits()):
    """Rehash all contents before restore; never trust the manifest alone."""
    _receipt_check(receipt, policy)
    _separate_root(backup_root, policy.root)
    try:
        with directory(backup_root + "/" + receipt.request_id, owners={0, owner_uid},
                       final_owners={owner_uid}, private=True) as job:
            manifest = _manifest(job, receipt, policy, owner_uid, limits)
            _verify_objects(job, manifest, owner_uid, time.monotonic() + limits.seconds, limits)
            return manifest
    except OSError:
        raise SnapshotRejected("BACKUP_IO") from None


def materialize(receipt, policy, *, assert_stopped, restore_root, backup_root=BACKUP_ROOT,
                owner_uid=0, limits=Limits()):
    """Restore into a NEW private sibling tree, not onto a live installation.

    Preserve file/directory owners, modes, mtimes and bytes. Capture original live
    data separately before a future cutover; never delete it or this backup here.
    The executor still owes image/config/data cutover and health verification.
    """
    _stopped(assert_stopped, policy)
    _separate_root(restore_root, policy.root)
    _separate_root(restore_root, backup_root)
    manifest = verify(receipt, policy, backup_root=backup_root, owner_uid=owner_uid, limits=limits)
    deadline = time.monotonic() + limits.seconds
    inspect_space(restore_root, owner_uid=owner_uid, required_bytes=manifest["total_bytes"] + MAX_MANIFEST)
    try:
        with directory(restore_root, owners={0, owner_uid}, final_owners={owner_uid}, private=True) as base:
            try:
                output = _new_directory(base, receipt.request_id, owner_uid)
            except FileExistsError:
                raise SnapshotRejected("RESTORE_EXISTS") from None
            try:
                names = {"basswiesn"} | {p for p, e in manifest["entries"].items() if e["kind"] == "directory"}
                for path in sorted(names, key=lambda p: (p.count("/"), p)):
                    with _parent(output, path) as (parent, leaf):
                        fd = _new_directory(parent, leaf, owner_uid)
                        os.close(fd)
                with directory(backup_root + "/" + receipt.request_id + "/objects", owners={0, owner_uid},
                               final_owners={owner_uid}, private=True) as objects:
                    for path, entry in sorted(manifest["entries"].items()):
                        if entry["kind"] != "file":
                            continue
                        with _parent(output, path) as (parent, leaf):
                            target = os.open(leaf, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                            original = None
                            try:
                                original = os.open(entry["object"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=objects)
                                info = os.fstat(original)
                                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != owner_uid:
                                    raise SnapshotRejected("BACKUP_UNSAFE")
                                if _hash_file(original, limit=limits.file_bytes, deadline=deadline, sink=target) != (entry["sha256"], entry["size"]):
                                    raise SnapshotRejected("BACKUP_HASH_MISMATCH")
                                _restore_metadata(target, entry)
                                os.fsync(target)
                            finally:
                                if original is not None:
                                    os.close(original)
                                os.close(target)
                # Set directory timestamps AFTER creating all children; reverse
                # order also prevents restrictive parent modes blocking creation.
                for path in sorted(names, key=lambda p: (-p.count("/"), p)):
                    with _parent(output, path) as (parent, leaf):
                        fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        try:
                            if path in manifest["entries"]:
                                _restore_metadata(fd, manifest["entries"][path])
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                # This is a complete byte/metadata readback, not just copy success.
                actual, size = _inventory(output, policy, limits, deadline)
                if actual != manifest["entries"] or size != manifest["total_bytes"]:
                    raise SnapshotRejected("BACKUP_CHANGED")
                _stopped(assert_stopped, policy)
                _write_new(output, "restore-ready.json", _json({"schema": 1, "request_id": receipt.request_id,
                           "snapshot_sha256": receipt.manifest_sha256, "image_id": policy.image_id}), owner_uid)
            finally:
                os.close(output)
        return {"state": "RESTORE_TREE_VERIFIED", "live_files_replaced": False,
                "application_health_verified": False}
    except OSError:
        raise SnapshotRejected("BACKUP_IO") from None


def _restore_metadata(fd, entry):
    info = os.fstat(fd)
    if (info.st_uid, info.st_gid) != (entry["uid"], entry["gid"]):
        os.fchown(fd, entry["uid"], entry["gid"])
    os.fchmod(fd, entry["mode"])
    os.utime(fd, ns=(entry["mtime_ns"], entry["mtime_ns"]))
