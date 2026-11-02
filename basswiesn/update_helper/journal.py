"""Private, fsync-backed host journal with a single-flight process lock.

No application database and no migrations run here. Opening does not create a
state directory or repair permissions. Onboarding must create that directory
explicitly; a malformed/untrusted journal is never silently reset. Request IDs
are retained until an operator performs future explicit offline maintenance.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import threading
import time

from .protocol import Code, UpdateRejected, require_digest, require_request_id, require_version


MAX_BYTES = 512 * 1024
MAX_JOBS = 128
MAX_EVENTS = 32
STATES = {
    "REQUESTED", "VALIDATED", "STAGING", "STAGED", "STOP_REQUESTED", "STOPPED",
    "BACKUP_STARTED", "BACKED_UP", "START_REQUESTED", "STARTED", "VERIFIED",
    "COMPLETE", "ROLLING_BACK", "RESTORED", "FAILED", "MANUAL_ACTION_REQUIRED",
}
TERMINAL = {"COMPLETE", "RESTORED", "FAILED", "MANUAL_ACTION_REQUIRED"}
BEFORE_CUTOVER = {"REQUESTED", "VALIDATED", "STAGING", "STAGED"}
ORDER = ["REQUESTED", "VALIDATED", "STAGING", "STAGED", "STOP_REQUESTED", "STOPPED",
         "BACKUP_STARTED", "BACKED_UP", "START_REQUESTED", "STARTED", "VERIFIED", "COMPLETE"]
NEXT = {old: {new} for old, new in zip(ORDER, ORDER[1:])}
for state in BEFORE_CUTOVER:
    NEXT[state].add("FAILED")
for state in STATES - TERMINAL - BEFORE_CUTOVER:
    NEXT.setdefault(state, set()).update({"ROLLING_BACK", "MANUAL_ACTION_REQUIRED"})
NEXT["ROLLING_BACK"] = {"RESTORED", "MANUAL_ACTION_REQUIRED"}


def _duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateRejected(Code.JOURNAL_CORRUPT)
        result[key] = value
    return result


def _validate_state(payload):
    if (not isinstance(payload, dict) or set(payload) != {"schema", "jobs"}
            or type(payload["schema"]) is not int or payload["schema"] != 1
            or not isinstance(payload["jobs"], list) or len(payload["jobs"]) > MAX_JOBS):
        raise UpdateRejected(Code.JOURNAL_CORRUPT)
    ids, active = set(), 0
    for position, job in enumerate(payload["jobs"]):
        if not isinstance(job, dict) or set(job) != {
            "request_id", "target_version", "expected_current_version", "state", "events", "backup_sha256", "artifact_sha256"
        }:
            raise UpdateRejected(Code.JOURNAL_CORRUPT)
        try:
            require_request_id(job["request_id"])
            require_version(job["target_version"])
            require_version(job["expected_current_version"])
            if job["backup_sha256"] is not None:
                require_digest(job["backup_sha256"])
            if job["artifact_sha256"] is not None:
                require_digest(job["artifact_sha256"])
        except UpdateRejected:
            raise UpdateRejected(Code.JOURNAL_CORRUPT) from None
        if job["request_id"] in ids or not isinstance(job["state"], str) or job["state"] not in STATES:
            raise UpdateRejected(Code.JOURNAL_CORRUPT)
        ids.add(job["request_id"])
        active += job["state"] not in TERMINAL or job["state"] == "MANUAL_ACTION_REQUIRED"
        if active and position != len(payload["jobs"]) - 1:
            raise UpdateRejected(Code.JOURNAL_CORRUPT)
        events = job["events"]
        if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
            raise UpdateRejected(Code.JOURNAL_CORRUPT)
        previous = None
        for index, event in enumerate(events):
            if (not isinstance(event, dict) or set(event) != {"sequence", "state", "time", "code"}
                    or type(event["sequence"]) is not int or event["sequence"] != index
                    or type(event["time"]) is not int or not 0 <= event["time"] <= 253_402_300_799
                    or not isinstance(event["state"], str) or event["state"] not in STATES
                    or (event["code"] is not None and (
                        not isinstance(event["code"], str) or event["code"] not in set(Code)))):
                raise UpdateRejected(Code.JOURNAL_CORRUPT)
            if previous is None:
                if event["state"] != "REQUESTED":
                    raise UpdateRejected(Code.JOURNAL_CORRUPT)
            elif event["state"] not in NEXT.get(previous, set()):
                raise UpdateRejected(Code.JOURNAL_CORRUPT)
            previous = event["state"]
        if previous != job["state"]:
            raise UpdateRejected(Code.JOURNAL_CORRUPT)
        if any(e["state"] == "BACKED_UP" for e in events) != (job["backup_sha256"] is not None):
            raise UpdateRejected(Code.JOURNAL_CORRUPT)
        if any(e["state"] == "STAGED" for e in events) != (job["artifact_sha256"] is not None):
            raise UpdateRejected(Code.JOURNAL_CORRUPT)
    if active > 1:
        raise UpdateRejected(Code.JOURNAL_CORRUPT)


def _open_directory(path: Path, owner_uid: int) -> int:
    """Walk without symlinks; pin the private final directory via a descriptor."""
    path = Path(path)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts) or len(path.parts) < 3:
        raise UpdateRejected(Code.JOURNAL_UNSAFE)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            value = os.fstat(fd)
            # Root-owned sticky /tmp supports private test/staging directories.
            # Arbitrary shared-writable parents are not a trusted host location.
            sticky_root = value.st_uid == 0 and bool(value.st_mode & stat.S_ISVTX)
            if value.st_uid not in {0, owner_uid} or (value.st_mode & 0o022 and not sticky_root):
                raise UpdateRejected(Code.JOURNAL_UNSAFE)
        value = os.fstat(fd)
        if value.st_uid != owner_uid or stat.S_IMODE(value.st_mode) != 0o700:
            raise UpdateRejected(Code.JOURNAL_UNSAFE)
        return fd
    except BaseException:
        os.close(fd)
        raise


class Journal:
    def __init__(self, directory: Path, *, owner_uid: int = 0):
        self.owner_uid = owner_uid
        try:
            self.fd = _open_directory(directory, owner_uid)
        except (OSError, ValueError):
            raise UpdateRejected(Code.JOURNAL_UNSAFE) from None
        self.lock_fd = None
        self.lock_owner = None
        self.local_lock = threading.Lock()
        self.io_failed = False

    def close(self):
        if not self.local_lock.acquire(blocking=False):
            raise UpdateRejected(Code.BUSY)
        try:
            os.close(self.fd)
        finally:
            self.local_lock.release()

    def initialize(self):
        """Explicit one-time onboarding ONLY. Never repair/reset an old journal.

        The exclusive-created lock is also a permanent initialization marker.
        A crash during initialization requires operator inspection, not retry.
        """
        if os.listdir(self.fd):
            raise UpdateRejected(Code.JOURNAL_UNSAFE)
        with self._exclusive(create=True):
            if set(os.listdir(self.fd)) != {"operation.lock"}:
                raise UpdateRejected(Code.JOURNAL_UNSAFE)
            os.fsync(self.lock_fd)
            os.fsync(self.fd)
            self.save({"schema": 1, "jobs": []})

    def _check_file(self, fd):
        value = os.fstat(fd)
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != self.owner_uid
                or stat.S_IMODE(value.st_mode) != 0o600 or value.st_nlink != 1):
            raise UpdateRejected(Code.JOURNAL_UNSAFE)

    def _open_file(self, name, flags):
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.fd)
        try:
            self._check_file(fd)
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def exclusive(self):
        with self._exclusive():
            yield self

    @contextmanager
    def _exclusive(self, *, create=False):
        if not self.local_lock.acquire(blocking=False):
            raise UpdateRejected(Code.BUSY)
        lock = None
        try:
            if self.io_failed:
                raise UpdateRejected(Code.JOURNAL_IO)
            flags = os.O_RDWR | (os.O_CREAT | os.O_EXCL if create else 0)
            lock = self._open_file("operation.lock", flags)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_fd = lock
            self.lock_owner = threading.get_ident()
            yield self
        except BlockingIOError:
            raise UpdateRejected(Code.BUSY) from None
        except OSError:
            self.io_failed = True
            raise UpdateRejected(Code.JOURNAL_IO) from None
        finally:
            self.lock_fd = None
            self.lock_owner = None
            try:
                if lock is not None:
                    os.close(lock)
            finally:
                self.local_lock.release()

    def read(self):
        try:
            fd = self._open_file("state.json", os.O_RDONLY)
        except FileNotFoundError:
            raise UpdateRejected(Code.JOURNAL_CORRUPT) from None
        except OSError:
            raise UpdateRejected(Code.JOURNAL_UNSAFE) from None
        try:
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise UpdateRejected(Code.JOURNAL_CORRUPT)
            value = json.loads(data, object_pairs_hook=_duplicate_keys)
            _validate_state(value)
            return value
        except (ValueError, TypeError, RecursionError, OSError):
            raise UpdateRejected(Code.JOURNAL_CORRUPT) from None

    def save(self, value):
        if self.lock_fd is None or self.lock_owner != threading.get_ident():
            raise UpdateRejected(Code.BUSY)
        if self.io_failed:
            raise UpdateRejected(Code.JOURNAL_IO)
        _validate_state(value)
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        if len(data) > MAX_BYTES:
            raise UpdateRejected(Code.JOURNAL_CAPACITY)
        name = ".state-" + secrets.token_hex(16) + ".tmp"
        try:
            fd = self._open_file(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, "state.json", src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        except OSError:
            self.io_failed = True
            raise UpdateRejected(Code.JOURNAL_IO) from None
        finally:
            try:
                os.unlink(name, dir_fd=self.fd)  # only our exact generated temp file
            except FileNotFoundError:
                pass

    def begin(self, request):
        value = self.read()
        for job in value["jobs"]:
            if job["request_id"] == request.request_id:
                if (job["target_version"] != request.target_version
                        or job["expected_current_version"] != request.expected_current_version):
                    raise UpdateRejected(Code.REQUEST_ID_REUSED)
                return job, False
        if any(j["state"] == "MANUAL_ACTION_REQUIRED" or j["state"] not in TERMINAL for j in value["jobs"]):
            raise UpdateRejected(Code.RECOVERY_REQUIRED)
        if len(value["jobs"]) >= MAX_JOBS:
            raise UpdateRejected(Code.JOURNAL_CAPACITY)
        job = {"request_id": request.request_id, "target_version": request.target_version,
               "expected_current_version": request.expected_current_version, "state": "REQUESTED",
            "backup_sha256": None, "artifact_sha256": None,
            "events": [{"sequence": 0, "state": "REQUESTED", "time": int(time.time()), "code": None}]}
        value["jobs"].append(job)
        self.save(value)
        return job, True

    def transition(self, request_id, state, *, code: Code | None = None, backup_sha256=None, artifact_sha256=None):
        value = self.read()
        job = next((j for j in value["jobs"] if j["request_id"] == request_id), None)
        if not job or state not in NEXT.get(job["state"], set()):
            raise UpdateRejected(Code.INVALID_TRANSITION)
        if state == "BACKED_UP":
            require_digest(backup_sha256)
            job["backup_sha256"] = backup_sha256
        elif backup_sha256 is not None:
            raise UpdateRejected(Code.INVALID_TRANSITION)
        if state == "STAGED":
            require_digest(artifact_sha256)
            job["artifact_sha256"] = artifact_sha256
        elif artifact_sha256 is not None:
            raise UpdateRejected(Code.INVALID_TRANSITION)
        job["state"] = state
        job["events"].append({"sequence": len(job["events"]), "state": state,
                              "time": int(time.time()), "code": Code(code).value if code is not None else None})
        self.save(value)
        return job

    def recover_interrupted(self):
        """Reconcile under the process lock; NEVER replay a host action."""
        with self.exclusive():
            for job in self.read()["jobs"]:
                if job["state"] in TERMINAL:
                    continue
                before = job["state"] in BEFORE_CUTOVER
                self.transition(job["request_id"], "FAILED" if before else "MANUAL_ACTION_REQUIRED",
                                code=Code.INTERRUPTED_BEFORE_CUTOVER if before else Code.RECOVERY_REQUIRED)

    def public_status(self):
        jobs = self.read()["jobs"]
        if not jobs:
            return {"state": "IDLE", "recovery_required": False}
        return self.public_job(jobs[-1])

    @staticmethod
    def public_job(job):
        return {"state": job["state"], "request_id": job["request_id"],
                "target_version": job["target_version"], "current_version_before": job["expected_current_version"],
                "code": job["events"][-1]["code"], "events": job["events"],
                "recovery_required": job["state"] == "MANUAL_ACTION_REQUIRED"}
