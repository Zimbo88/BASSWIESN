"""Explicit, one-shot administrator enrollment preparation; no service install.

The default preview is filesystem-only. Applying a matching preview reserves a
new private host directory, observes exactly the approved local container, and
writes a policy plus empty journal. It never executes application configuration,
changes existing permissions, provisions a Web capability or starts a daemon.
Incomplete reservations require operator inspection; there is no erase/retry.
"""
from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import stat
import uuid

from . import local_docker
from .host_preflight import (
    APP_UID, APP_GID, Enrollment, HostRejected, MAX_POLICY_BYTES, RELEASE_FILES,
    absolute_path, capture_configuration, directory, inspect_files,
    inspect_runtime, _regular_read, unique_json,
)
from .journal import Journal
from .protocol import require_digest, UpdateRejected


HOST_ROOT = "/var/lib/basswiesn-update"
# Fixed synthetic v4/variant-1 marker, not a hardware or installation identity.
PREVIEW_ID = str(uuid.UUID(fields=(0, 0, 0x4000, 0x80, 0, 0)))
SUBDIRECTORIES = frozenset({"docker-client", "journal", "staging", "backups", "candidates", "restores"})
ROOT_FILES = frozenset({"reservation.json", "enrollment.json", "peer.json", "prepared.json"})
RESERVATION = {"schema": 1, "purpose": "update-enrollment", "automatic_retry": False}
PEER = {"schema": 1, "host_uid": APP_UID, "host_gid": APP_GID,
        "identity_uid_map": True, "capability_provisioned": False}


class SetupCode(StrEnum):
    ADMIN_REQUIRED = "ADMIN_REQUIRED"
    EXPLICIT_APPROVAL_REQUIRED = "EXPLICIT_APPROVAL_REQUIRED"
    PREVIEW_CHANGED = "PREVIEW_CHANGED"
    TARGET_EXISTS = "TARGET_EXISTS"
    TARGET_UNSAFE = "TARGET_UNSAFE"
    PREPARATION_IO = "PREPARATION_IO"
    PREPARATION_INVALID = "PREPARATION_INVALID"


class SetupRejected(ValueError):
    def __init__(self, code):
        self.code = SetupCode(code)
        super().__init__(self.code.value)


def _json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _policy_value(policy):
    return {"schema": 1, "installation_id": policy.installation_id,
            "root": policy.root, "installation_owner_uid": policy.installation_owner_uid,
            "compose_project": policy.compose_project, "container_id": policy.container_id,
            "image_id": policy.image_id, "version": policy.version,
            "file_sha256": dict(policy.file_sha256), "env_sha256": policy.env_sha256}


def _identity(value):
    return value.st_dev, value.st_ino, value.st_uid, value.st_gid, value.st_mode


def _validate_target(root, installation_root, owner_uid):
    absolute_path(root)
    if type(owner_uid) is not int or owner_uid < 0 or os.geteuid() != owner_uid:
        raise SetupRejected(SetupCode.ADMIN_REQUIRED)
    target, application = PurePosixPath(root), PurePosixPath(installation_root)
    if target.is_relative_to(application) or application.is_relative_to(target):
        raise SetupRejected(SetupCode.TARGET_UNSAFE)
    # In production owner_uid is always zero. Alternate owners and paths exist
    # solely for isolated tests, never as CLI flags or Web-controlled options.
    with directory(str(target.parent), owners={0, owner_uid}, final_owners={owner_uid}) as parent:
        try:
            os.stat(target.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return _identity(os.fstat(parent))
        raise SetupRejected(SetupCode.TARGET_EXISTS)


@dataclass(frozen=True, repr=False)
class Preview:
    policy: Enrollment
    target: str
    owner_uid: int
    parent_identity: tuple

    @property
    def approval_sha256(self):
        return hashlib.sha256(_json({"schema": 1, "purpose": "prepare-only",
            "policy": _policy_value(self.policy), "target": self.target,
            "owner_uid": self.owner_uid, "parent_identity": self.parent_identity})).hexdigest()

    def public(self):
        # An approval digest is not publisher authentication. It binds the exact
        # local preview; do not print private paths, env hashes or container IDs.
        return {"schema": 1, "state": "AWAITING_ADMIN_APPROVAL",
                "approval_sha256": self.approval_sha256, "version": self.policy.version,
                "runtime_observed": False, "local_docker_reads_on_apply": True,
                "creates_private_enrollment": True, "modifies_application": False,
                "installs_service": False, "installation_available": False,
                "onboarding_complete": False}


def preview(*, root, installation_owner_uid, compose_project, container_id, image_id, version,
            target=HOST_ROOT, owner_uid=0):
    """Read-only preview. Identity must be supplied explicitly by an admin."""
    # Validate every supplied identifier BEFORE reading an arbitrary path.
    candidate = Enrollment.parse(_json({"schema": 1, "installation_id": PREVIEW_ID,
        "root": root, "installation_owner_uid": installation_owner_uid,
        "compose_project": compose_project, "container_id": container_id,
        "image_id": image_id, "version": version,
        "file_sha256": {name: "0" * 64 for name in RELEASE_FILES},
        "env_sha256": "0" * 64}))
    parent_identity = _validate_target(target, root, owner_uid)
    hashes, env_hash = capture_configuration(root, installation_owner_uid=installation_owner_uid)
    return Preview(replace(candidate, file_sha256=hashes, env_sha256=env_hash),
                   target, owner_uid, parent_identity)


def _write_new(parent, name, data, owner_uid):
    """Never replace an existing file. Partial files deliberately remain."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise SetupRejected(SetupCode.TARGET_UNSAFE)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError()
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(parent)


def _mkdir_new(parent, name, owner_uid):
    os.mkdir(name, mode=0o700, dir_fd=parent)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise SetupRejected(SetupCode.TARGET_UNSAFE)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(parent)


def _status():
    return {"schema": 1, "state": "ENROLLMENT_PREPARED",
            "enrollment_prepared": True, "installation_available": False,
            "onboarding_complete": False, "backup_restore_verified": False,
            "service_installed": False, "capability_provisioned": False}


def prepare(plan: Preview, *, approve=False, approval_sha256="", observe=None):
    """One reservation; one explicit local runtime read. No automatic retries.

    A failure AFTER mkdir leaves a private reservation for manual inspection.
    There is intentionally no cleanup that could delete a preexisting directory,
    lose evidence, or silently reset a journal. No service/application action occurs.
    """
    if approve is not True:
        raise SetupRejected(SetupCode.EXPLICIT_APPROVAL_REQUIRED)
    try:
        require_digest(approval_sha256)
    except UpdateRejected:
        raise SetupRejected(SetupCode.EXPLICIT_APPROVAL_REQUIRED) from None
    if not hmac.compare_digest(plan.approval_sha256, approval_sha256):
        raise SetupRejected(SetupCode.PREVIEW_CHANGED)
    # Direct in-process users cannot bypass exact enrollment schema validation.
    Enrollment.parse(_json(_policy_value(plan.policy)))
    if plan.policy.installation_id != PREVIEW_ID:
        raise SetupRejected(SetupCode.PREVIEW_CHANGED)
    if _validate_target(plan.target, plan.policy.root, plan.owner_uid) != plan.parent_identity:
        raise SetupRejected(SetupCode.PREVIEW_CHANGED)
    inspect_files(plan.policy)
    target = PurePosixPath(plan.target)
    try:
        with directory(str(target.parent), owners={0, plan.owner_uid}, final_owners={plan.owner_uid}) as parent:
            if _identity(os.fstat(parent)) != plan.parent_identity:
                raise SetupRejected(SetupCode.PREVIEW_CHANGED)
            try:
                _mkdir_new(parent, target.name, plan.owner_uid)
            except FileExistsError:
                raise SetupRejected(SetupCode.TARGET_EXISTS) from None
            with directory(plan.target, owners={0, plan.owner_uid}, final_owners={plan.owner_uid}, private=True) as root:
                root_identity = _identity(os.fstat(root))
                _write_new(root, "reservation.json", _json(RESERVATION), plan.owner_uid)
                for name in sorted(SUBDIRECTORIES):
                    _mkdir_new(root, name, plan.owner_uid)
                # Production collection has a fixed local socket/config dir and
                # checks root prerequisites itself. The callback is tests-only.
                if observe is None:
                    if plan.target != HOST_ROOT or plan.owner_uid != 0:
                        raise SetupRejected(SetupCode.TARGET_UNSAFE)
                    observe = local_docker.collect
                observation = observe(plan.policy)
                inspect_runtime(plan.policy, observation)
                inspect_files(plan.policy)
                policy = replace(plan.policy, installation_id=str(uuid.uuid4()))
                _write_new(root, "enrollment.json", _json(_policy_value(policy)), plan.owner_uid)
                _write_new(root, "peer.json", _json(PEER), plan.owner_uid)
                journal = Journal(Path(plan.target) / "journal", owner_uid=plan.owner_uid)
                try:
                    journal.initialize()
                    with journal.exclusive():
                        if journal.read() != {"schema": 1, "jobs": []}:
                            raise SetupRejected(SetupCode.PREPARATION_INVALID)
                finally:
                    journal.close()
                fingerprints = _fingerprints(plan.target, plan.owner_uid)
                inspect_runtime(policy, observation)  # reject expired observations
                inspect_files(policy)
                if _identity(os.stat(target.name, dir_fd=parent, follow_symlinks=False)) != root_identity:
                    raise SetupRejected(SetupCode.TARGET_UNSAFE)
                # Completion receipt is written LAST. No partial directory is a
                # prepared enrollment, even if it already contains a valid policy.
                _write_new(root, "prepared.json", _json({"schema": 1,
                    "state": "ENROLLMENT_PREPARED", "files": fingerprints}), plan.owner_uid)
        return verify_preparation(plan.target, owner_uid=plan.owner_uid)
    except (SetupRejected, HostRejected):
        raise
    except (OSError, UpdateRejected):
        raise SetupRejected(SetupCode.PREPARATION_IO) from None
    except Exception:
        raise SetupRejected(SetupCode.PREPARATION_INVALID) from None


def _fingerprints(target, owner_uid):
    result = {}
    with directory(target, owners={0, owner_uid}, final_owners={owner_uid}, private=True) as root:
        for name in sorted(ROOT_FILES - {"prepared.json"}):
            data = _regular_read(root, name, owners={owner_uid}, limit=MAX_POLICY_BYTES, private=True)
            result[name] = hashlib.sha256(data).hexdigest()
        for name in sorted(SUBDIRECTORIES):
            with directory(target + "/" + name, owners={0, owner_uid}, final_owners={owner_uid}, private=True) as child:
                expected = {"state.json", "operation.lock"} if name == "journal" else set()
                if set(os.listdir(child)) != expected:
                    raise SetupRejected(SetupCode.PREPARATION_INVALID)
                for member in sorted(expected):
                    data = _regular_read(child, member, owners={owner_uid}, limit=MAX_POLICY_BYTES, private=True)
                    result[name + "/" + member] = hashlib.sha256(data).hexdigest()
    return result


def verify_preparation(target=HOST_ROOT, *, owner_uid=0):
    """Read-only verification of an UNUSED prepared enrollment, not readiness.

    After a future service uses its journal, this initial snapshot is no longer
    an unused enrollment. This function must not reset it or report it as fresh.
    """
    with directory(target, owners={0, owner_uid}, final_owners={owner_uid}, private=True) as root:
        if set(os.listdir(root)) != ROOT_FILES | SUBDIRECTORIES:
            raise SetupRejected(SetupCode.PREPARATION_INVALID)
        def read(name):
            return _regular_read(root, name, owners={owner_uid}, limit=MAX_POLICY_BYTES, private=True)
        policy = Enrollment.parse(read("enrollment.json"))
        if policy.installation_id == PREVIEW_ID or _json(unique_json(read("reservation.json"))) != _json(RESERVATION):
            raise SetupRejected(SetupCode.PREPARATION_INVALID)
        if _json(unique_json(read("peer.json"))) != _json(PEER):
            raise SetupRejected(SetupCode.PREPARATION_INVALID)
        receipt = unique_json(read("prepared.json"))
        expected = {"schema": 1, "state": "ENROLLMENT_PREPARED", "files": _fingerprints(target, owner_uid)}
        if _json(receipt) != _json(expected):
            raise SetupRejected(SetupCode.PREPARATION_INVALID)
    inspect_files(policy)
    journal = Journal(Path(target) / "journal", owner_uid=owner_uid)
    try:
        with journal.exclusive():
            if journal.read() != {"schema": 1, "jobs": []}:
                raise SetupRejected(SetupCode.PREPARATION_INVALID)
    finally:
        journal.close()
    return _status()
