"""Bounded local release/data exchange, with originals retained on the same disk.

Only trusted host orchestration may call this module after proving ALL writers
stopped. No Docker, network, shell, deletion or automatic interruption replay.
Linux renameat2 exchanges each top-level entry atomically; a durable immutable
plan records both inode identities before the first exchange. A partial swap
can be rolled back without guessing whether a failed syscall actually happened.

The application cannot access the root-private operation directory. Original
data is never mounted into the candidate; a verified snapshot copy is used.
Code, configuration and data return together, including after schema mutation.
An externally changed original, mount, symlink or unexpected inode fails closed.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import ctypes
import hashlib
import os
import stat
import time

from . import artifact_staging as staging, installation_snapshot as snapshot
from .host_preflight import directory, inspect_files, _regular_read, unique_json
from .onboarding import _json, _write_new
from .protocol import require_request_id, require_version


PREFIX = ".basswiesn-update-"
MAX_PLAN = 16 * 1024 * 1024
BUILD_EXCLUSION = "/.basswiesn-update-*"


class CutoverRejected(ValueError):
    def __init__(self, code="CUTOVER_INVALID"):
        self.code = code if code in {"CUTOVER_INVALID", "CUTOVER_EXISTS", "CUTOVER_CHANGED",
            "CUTOVER_IO", "CUTOVER_UNSUPPORTED", "CUTOVER_SPACE"} else "CUTOVER_INVALID"
        super().__init__(self.code)


@dataclass(frozen=True, repr=False)
class Cutover:
    request_id: str
    installation_id: str
    target_version: str
    plan_sha256: str


def _id(parent, name):
    try:
        value = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not (stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode)):
        raise CutoverRejected("CUTOVER_CHANGED")
    return [value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)]


def _rename(source, destination, name, *, exchange):
    # No replacing rename fallback: it could discard user data on a race or
    # filesystem that cannot meet the exchange/noreplace contract.
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        call = libc.renameat2
    except AttributeError:
        raise CutoverRejected("CUTOVER_UNSUPPORTED") from None
    call.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    call.restype = ctypes.c_int
    encoded = os.fsencode(name)
    if call(source, encoded, destination, encoded, 2 if exchange else 1):
        raise CutoverRejected("CUTOVER_IO")
    os.fsync(source)
    os.fsync(destination)


def _without_objects(entries):
    # Object numbers identify backup blobs, not filesystem content.
    return {p: {k: v for k, v in e.items() if k != "object"} for p, e in entries.items()}


def _inventory(fd, policy, names, limits):
    return snapshot._inventory(fd, policy, limits, time.monotonic() + limits.seconds, paths=list(names))


def _build_exclusion(fd, owners):
    # Both generations must exclude retained private state, including when an
    # operator later builds manually as root. The LAST effective rule wins;
    # reject a Dockerfile-specific override instead of implementing a new parser.
    data = _regular_read(fd, ".dockerignore", owners=owners, limit=1024*1024)
    try:
        lines = [line.strip() for line in data.decode("utf-8").splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
    except UnicodeError:
        raise CutoverRejected("CUTOVER_UNSUPPORTED") from None
    if not lines or lines[-1] != BUILD_EXCLUSION or "Dockerfile.dockerignore" in os.listdir(fd):
        raise CutoverRejected("CUTOVER_UNSUPPORTED")


def _copy(target, entries, open_source, *, limits, release_owner=None):
    """Copy to NEW descriptors only; preserve snapshot metadata, normalize code."""
    deadline = time.monotonic() + limits.seconds
    directories = {p for p, e in entries.items() if e["kind"] == "directory"}
    for path in sorted(directories, key=lambda p: (p.count("/"), p)):
        with snapshot._parent(target, path) as (parent, leaf):
            os.mkdir(leaf, 0o700, dir_fd=parent)
    for path, entry in sorted(entries.items()):
        if entry["kind"] != "file":
            continue
        with snapshot._parent(target, path) as (parent, leaf):
            fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with open_source(path, entry) as source:
                    value = os.fstat(source)
                    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                        raise CutoverRejected("CUTOVER_CHANGED")
                    if snapshot._hash_file(source, limit=limits.file_bytes, deadline=deadline, sink=fd) != (entry["sha256"], entry["size"]):
                        raise CutoverRejected("CUTOVER_CHANGED")
                _metadata(fd, entry, release_owner)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(parent)
    for path in sorted(directories, key=lambda p: (-p.count("/"), p)):
        with snapshot._parent(target, path) as (parent, leaf):
            fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                _metadata(fd, entries[path], release_owner)
                os.fsync(fd)
            finally:
                os.close(fd)
    os.fsync(target)


def _metadata(fd, entry, release_owner):
    if release_owner is not None:
        entry = dict(entry, uid=release_owner[0], gid=release_owner[1],
                     mode=0o755 if entry["kind"] == "directory" or entry["mode"] & 0o100 else 0o644)
    snapshot._restore_metadata(fd, entry)


@contextmanager
def _source(root, path):
    with snapshot._parent(root, path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            yield fd
        finally:
            os.close(fd)


@contextmanager
def _job(policy, request_id, owner_uid):
    require_request_id(request_id)
    with directory(policy.root, owners={0, policy.installation_owner_uid}) as live:
        with directory(policy.root + "/" + PREFIX + request_id, owners={0, policy.installation_owner_uid, owner_uid},
                       final_owners={owner_uid}, private=True) as job:
            pending = os.open("pending", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=job)
            try:
                info = os.fstat(pending)
                if (info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o700
                        or info.st_dev != os.fstat(live).st_dev):
                    raise CutoverRejected("CUTOVER_CHANGED")
                yield live, job, pending
            finally:
                os.close(pending)


def prepare(policy, receipt, version, *, assert_stopped, staging_root=staging.STAGING_ROOT,
            backup_root=snapshot.BACKUP_ROOT, owner_uid=0, limits=snapshot.Limits()):
    """Prepare a replacement beside the enrolled files; do not replace live data."""
    require_version(version)
    snapshot._receipt_check(receipt, policy)
    snapshot._stopped(assert_stopped, policy)
    inspect_files(policy)
    staged = staging.verify_staged_release(receipt.request_id, version, staging_root=staging_root, owner_uid=owner_uid)
    manifest = snapshot.verify(receipt, policy, backup_root=backup_root, owner_uid=owner_uid, limits=limits)
    source_path = staging_root + "/" + receipt.request_id + "/source"
    try:
        with directory(source_path, owners={0, owner_uid}, final_owners={owner_uid}, private=True) as source:
            _build_exclusion(source, {0, owner_uid})
            names = sorted(set(os.listdir(source)) - {"data"})
            if not names or any(n == ".env" or n.startswith(PREFIX) for n in names):
                raise CutoverRejected()
            release, size = _inventory(source, policy, names, limits)
            with directory(policy.root, owners={0, policy.installation_owner_uid}) as live:
                _build_exclusion(live, {0, policy.installation_owner_uid})
                disk = os.fstatvfs(live)
                if disk.f_bavail * disk.f_frsize < size + manifest["total_bytes"] + MAX_PLAN + 1024 * 1024:
                    raise CutoverRejected("CUTOVER_SPACE")
                try:
                    child = snapshot._new_directory(live, PREFIX + receipt.request_id, owner_uid)
                except FileExistsError:
                    raise CutoverRejected("CUTOVER_EXISTS") from None
                try:
                    _write_new(child, "reservation.json", _json({"schema": 1, "request_id": receipt.request_id}), owner_uid)
                    pending = snapshot._new_directory(child, "pending", owner_uid)
                    try:
                        _copy(pending, release, lambda p, e: _source(source, p), limits=limits,
                              release_owner=(policy.installation_owner_uid, os.fstat(live).st_gid))
                        private = {p: e for p, e in manifest["entries"].items() if p == ".env" or p == "data" or p.startswith("data/")}
                        with directory(backup_root + "/" + receipt.request_id + "/objects", owners={0, owner_uid},
                                       final_owners={owner_uid}, private=True) as objects:
                            _copy(pending, private, lambda p, e: _source(objects, e["object"]), limits=limits)
                        names = sorted(names + [".env", "data"])
                        existing = [n for n in names if _id(live, n) is not None]
                        original, _ = _inventory(live, policy, existing, limits)
                        new, _ = _inventory(pending, policy, names, limits)
                        # Reject a changed live data/config state, not just a
                        # matching enrollment. No candidate has run yet.
                        captured, _ = snapshot._inventory(live, policy, limits, time.monotonic() + limits.seconds)
                        if _without_objects(captured) != _without_objects(manifest["entries"]):
                            raise CutoverRejected("CUTOVER_CHANGED")
                        snapshot._stopped(assert_stopped, policy)
                        inspect_files(policy)
                        if staging.verify_staged_release(receipt.request_id, version, staging_root=staging_root,
                                                       owner_uid=owner_uid) != staged:
                            raise CutoverRejected("CUTOVER_CHANGED")
                        plan = {"schema": 1, "request_id": receipt.request_id, "installation_id": policy.installation_id,
                            "version": version, "archive_sha256": staged["archive_sha256"],
                            "snapshot_sha256": receipt.manifest_sha256,
                            "root": [os.fstat(live).st_dev, os.fstat(live).st_ino],
                            "entries": {n: {"old": _id(live, n), "new": _id(pending, n)} for n in names},
                            "old_contents": _without_objects(original), "new_contents": _without_objects(new)}
                        raw = _json(plan)
                        if len(raw) > MAX_PLAN:
                            raise CutoverRejected()
                        _write_new(child, "plan.json", raw, owner_uid)
                    finally:
                        os.close(pending)
                finally:
                    os.close(child)
        return Cutover(receipt.request_id, policy.installation_id, version, hashlib.sha256(raw).hexdigest())
    except OSError:
        raise CutoverRejected("CUTOVER_IO") from None


def _load(live, job, receipt, policy, owner_uid):
    raw = _regular_read(job, "plan.json", owners={owner_uid}, limit=MAX_PLAN, private=True)
    if hashlib.sha256(raw).hexdigest() != receipt.plan_sha256:
        raise CutoverRejected("CUTOVER_CHANGED")
    plan = unique_json(raw)
    if (plan["request_id"] != receipt.request_id or plan["installation_id"] != policy.installation_id
            or receipt.installation_id != policy.installation_id or plan["version"] != receipt.target_version
            or plan["root"] != [os.fstat(live).st_dev, os.fstat(live).st_ino]):
        raise CutoverRejected("CUTOVER_CHANGED")
    return plan


def _position(live, pending, name, identities):
    pair = (_id(live, name), _id(pending, name))
    if pair == (identities["old"], identities["new"]):
        return "original"
    if pair == (identities["new"], identities["old"]):
        return "candidate"
    raise CutoverRejected("CUTOVER_CHANGED")


def _check_content(fd, name, expected, policy, limits):
    actual, _ = _inventory(fd, policy, [name], limits)
    selected = {p: e for p, e in expected.items() if p == name or p.startswith(name + "/")}
    if _without_objects(actual) != selected:
        raise CutoverRejected("CUTOVER_CHANGED")


def exchange(receipt, policy, *, assert_stopped, rollback=False, owner_uid=0, limits=snapshot.Limits()):
    """Exchange/reverse a prepared tree while quiescent; preserve BOTH generations.

    The outer host journal must hold its single-flight lock. After process loss,
    automatic service recovery remains fail-closed; an operator may explicitly
    inspect and reverse this exact receipt. Nothing is inferred from a name.
    """
    snapshot._stopped(assert_stopped, policy)
    try:
        with _job(policy, receipt.request_id, owner_uid) as (live, job, pending):
            plan = _load(live, job, receipt, policy, owner_uid)
            # Once rollback completed this reservation cannot activate again.
            if "restored.json" in os.listdir(job):
                raise CutoverRejected("CUTOVER_EXISTS")
            names = sorted(plan["entries"], reverse=rollback)
            for name in names:
                identities = plan["entries"][name]
                position = _position(live, pending, name, identities)
                if rollback:
                    # Candidate data may legitimately have migrated. Only the
                    # preserved original must still match its exact plan hash.
                    if identities["old"] is not None:
                        _check_content(live if position == "original" else pending, name,
                                       plan["old_contents"], policy, limits)
                else:
                    _check_content(pending if position == "original" else live, name,
                                   plan["new_contents"], policy, limits)
                    if identities["old"] is not None:
                        _check_content(live if position == "original" else pending, name,
                                       plan["old_contents"], policy, limits)
            for name in names:
                identities = plan["entries"][name]
                position = _position(live, pending, name, identities)
                desired = "original" if rollback else "candidate"
                if position == desired:
                    continue
                snapshot._stopped(assert_stopped, policy)
                _rename(live if rollback else pending, pending if rollback else live, name,
                        exchange=identities["old"] is not None)
                if _position(live, pending, name, identities) != desired:
                    raise CutoverRejected("CUTOVER_CHANGED")
            snapshot._stopped(assert_stopped, policy)
            # Full final readback before a host executor may start either image.
            expected = plan["old_contents"] if rollback else plan["new_contents"]
            for name in names:
                if any(p == name or p.startswith(name + "/") for p in expected):
                    _check_content(live, name, expected, policy, limits)
            marker = "restored.json" if rollback else "applied.json"
            _write_new(job, marker, _json({"schema": 1, "plan_sha256": receipt.plan_sha256}), owner_uid)
        return {"state": "ORIGINAL_FILES_RESTORED" if rollback else "CANDIDATE_FILES_INSTALLED",
                "originals_retained": True, "application_health_verified": False}
    except OSError:
        raise CutoverRejected("CUTOVER_IO") from None
