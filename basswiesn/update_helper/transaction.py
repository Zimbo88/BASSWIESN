"""Journalled orchestration, independent of Docker, the Web UI and app imports.

The executor is a trusted, in-process host adapter, NEVER supplied by a request.
The constrained host adapter lives in host_executor; service activation is still
gated separately. Orchestration tests use a synthetic installation.
An archive checksum alone is not release provenance or a verified backup.
"""
from dataclasses import dataclass
from typing import Protocol

from .journal import BEFORE_CUTOVER, Journal
from .protocol import Code, Request, UpdateRejected, newer, require_digest, require_request_id, require_version


@dataclass(frozen=True)
class Plan:
    request_id: str
    target_version: str
    current_version: str

    def __post_init__(self):
        require_request_id(self.request_id)
        require_version(self.target_version)
        require_version(self.current_version)


@dataclass(frozen=True)
class Candidate:
    request_id: str
    version: str
    archive_sha256: str


@dataclass(frozen=True)
class Backup:
    request_id: str
    version: str
    manifest_sha256: str


class HostExecutor(Protocol):
    """Required contract for a fixed-installation host implementation.

    All calls must be bounded and must never activate/poll/discover a radio.
    Preflight validates installation ownership, capacity, container/image/config
    identity, mounts and rollback prerequisites. It is read-only.

    Prepare fetches only the fixed official stable release, verifies source,
    checksums/manifest/version and prepares the candidate without changing the
    active installation or its data. STOP precedes backup: a copied live SQLite
    database plus an unrelated WAL is NOT a consistent restore point.

    Backup retains and verifies original data, config, ownership and image
    identity before any candidate starts. Start must not discard originals.
    Health verification includes target version/readiness, not just HTTP 200.

    Restore with backup=None may ONLY restart the original against untouched
    data. If a candidate may have started, its processes must first be stopped
    and a verified backup plus the original image/config restored. The adapter
    must recheck receipt identity and integrity immediately before using it.
    Verify_original checks original version/readiness and restored state.

    Cleanup is deliberately outside this protocol: no deletion of retained
    backups/images, no automatic factory reset, no arbitrary shell requests.
    """
    def current_version(self) -> str: ...
    def preflight(self, plan: Plan) -> None: ...
    def prepare_candidate(self, plan: Plan) -> Candidate: ...
    def stop_original(self, plan: Plan) -> None: ...
    def backup_original(self, plan: Plan) -> Backup: ...
    def start_candidate(self, plan: Plan, candidate: Candidate, backup: Backup) -> None: ...
    def verify_candidate(self, plan: Plan) -> None: ...
    def restore_original(self, plan: Plan, backup: Backup | None, *, candidate_may_have_started: bool) -> None: ...
    def verify_original(self, plan: Plan) -> None: ...


class _ActionFailed(Exception):
    def __init__(self, code):
        self.code = Code(code)
        super().__init__(self.code.value)


def _action(code, function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception:
        # Deliberately discard adapter exception text, subprocess output and paths.
        # BaseException (process termination) leaves the durable intent for recovery.
        raise _ActionFailed(code) from None


class TransactionRunner:
    def __init__(self, journal: Journal, executor: HostExecutor | None = None):
        self.journal = journal
        self.executor = executor

    def execute(self, request: Request, *, on_reserved=None):
        """Internal entry point; the service must authenticate before calling it."""
        if request.action not in {"install", "submit"}:
            raise UpdateRejected(Code.BAD_REQUEST)
        plan = Plan(request.request_id, request.target_version, request.expected_current_version)
        with self.journal.exclusive():
            # Fail closed: an unaffiliated web process cannot turn a dry model
            # into a real host updater. No fabricated successful job is recorded.
            if self.executor is None:
                raise UpdateRejected(Code.EXECUTOR_UNAVAILABLE)
            job, created = self.journal.begin(request)
            # The acknowledgement follows durable intent, while the same worker
            # retains the exclusive lock until completion. Losing the socket
            # cannot cancel or replay a reserved transaction.
            if on_reserved is not None:
                on_reserved(self.journal.public_job(job))
            if not created:
                return self.journal.public_job(job)  # idempotent, never re-execute
            return self._run(plan)

    def _run(self, plan):
        host, journal = self.executor, self.journal
        backup = None
        candidate_may_have_started = False

        def step(state, **kwargs):
            return journal.transition(plan.request_id, state, **kwargs)

        def prepare():
            receipt = host.prepare_candidate(plan)
            if (not isinstance(receipt, Candidate) or receipt.request_id != plan.request_id
                    or receipt.version != plan.target_version):
                raise UpdateRejected(Code.SOURCE_VERIFICATION_FAILED)
            require_digest(receipt.archive_sha256)
            return receipt

        def capture():
            receipt = host.backup_original(plan)
            if (not isinstance(receipt, Backup) or receipt.request_id != plan.request_id
                    or receipt.version != plan.current_version):
                raise UpdateRejected(Code.BACKUP_FAILED)
            require_digest(receipt.manifest_sha256)
            return receipt

        try:
            if not newer(plan.target_version, plan.current_version):
                raise _ActionFailed(Code.NOT_AN_UPGRADE)
            actual = _action(Code.PREFLIGHT_FAILED, host.current_version)
            if actual != plan.current_version:
                raise _ActionFailed(Code.CURRENT_VERSION_CHANGED)
            _action(Code.PREFLIGHT_FAILED, host.preflight, plan)
            step("VALIDATED")
            step("STAGING")
            candidate = _action(Code.SOURCE_VERIFICATION_FAILED, prepare)
            step("STAGED", artifact_sha256=candidate.archive_sha256)
            # Preparation can take minutes. An external operator may have changed
            # the running version. Check again immediately before downtime.
            actual = _action(Code.PREFLIGHT_FAILED, host.current_version)
            if actual != plan.current_version:
                raise _ActionFailed(Code.CURRENT_VERSION_CHANGED)
            _action(Code.PREFLIGHT_FAILED, host.preflight, plan)
            step("STOP_REQUESTED")
            _action(Code.STOP_FAILED, host.stop_original, plan)
            step("STOPPED")
            step("BACKUP_STARTED")
            backup = _action(Code.BACKUP_FAILED, capture)
            step("BACKED_UP", backup_sha256=backup.manifest_sha256)
            step("START_REQUESTED")
            candidate_may_have_started = True  # set BEFORE a partially failing start
            _action(Code.START_FAILED, host.start_candidate, plan, candidate, backup)
            step("STARTED")
            _action(Code.HEALTH_FAILED, host.verify_candidate, plan)
            step("VERIFIED")
            return journal.public_job(step("COMPLETE"))
        except _ActionFailed as failure:
            job = next(j for j in journal.read()["jobs"] if j["request_id"] == plan.request_id)
            if job["state"] in BEFORE_CUTOVER:
                return journal.public_job(step("FAILED", code=failure.code))
            step("ROLLING_BACK", code=failure.code)
            try:
                _action(Code.RESTORE_FAILED, host.restore_original, plan, backup,
                        candidate_may_have_started=candidate_may_have_started)
                _action(Code.RESTORE_FAILED, host.verify_original, plan)
            except _ActionFailed:
                return journal.public_job(step("MANUAL_ACTION_REQUIRED", code=Code.RESTORE_FAILED))
            return journal.public_job(step("RESTORED", code=failure.code))
        # Journal errors are intentionally NOT caught as adapter failures. If
        # durable intent cannot be recorded, no new host action is safe. Restart
        # reconciliation marks ambiguous cutover states MANUAL_ACTION_REQUIRED.
