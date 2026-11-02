"""Real isolated SQLite sessions, never hardware or network operations."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import SetupRebuildCoordinatorLease as Lease, SetupRebuildJob
from basswiesn.app.services.setup_rebuild.coordinator import SetupCoordinator
from basswiesn.app.services.setup_rebuild.repository import SetupRepository
from basswiesn.app.services.setup_rebuild.lease import LeaseWatch, SetupLeaseLost
from basswiesn.app.services.setup_rebuild.states import SetupState
from test_setup_transaction_ownership import prepared
from test_setup_rebuild_160 import RecordingAdapter

pytestmark = pytest.mark.integration


def seed(owner="worker-a", job="job-a", status="running", expired=False):
    with app_db.SessionLocal() as db:
        if status is not None:
            db.add(SetupRebuildJob(job_id=job, status=status, owner_id=owner))
        db.add(Lease(lease_key="global", owner_id=owner, job_id=job,
                     expires_at=datetime.now(UTC) + timedelta(seconds=-1 if expired else 90)))
        db.commit()


def test_startup_does_not_fail_another_workers_live_job():
    seed()
    coordinator = SetupCoordinator(session_factory=app_db.SessionLocal)
    asyncio.run(coordinator.resume_pending_jobs())
    with app_db.SessionLocal() as db:
        assert db.query(SetupRebuildJob).one().status == "running"
        assert db.query(Lease).one().owner_id == "worker-a"


def test_same_worker_cannot_replace_its_other_active_job():
    seed()
    with app_db.SessionLocal() as db:
        assert not SetupRepository().acquire_lease(db, owner_id="worker-a", job_id="job-b")
        assert db.query(Lease).one().job_id == "job-a"


def test_unknown_unexpired_owner_is_not_assumed_dead():
    seed(owner="recovery-worker", status=None)
    with app_db.SessionLocal() as db:
        assert not SetupRepository().acquire_lease(db, owner_id="worker-b", job_id="job-b")
        assert db.query(Lease).one().owner_id == "recovery-worker"


def test_expired_ownership_cannot_be_revived_by_heartbeat():
    seed(expired=True)
    with app_db.SessionLocal() as db:
        assert not SetupRepository().renew_lease(db, owner_id="worker-a", job_id="job-a")


def test_stale_session_cannot_overwrite_a_new_owner():
    seed(owner="", job="", status=None, expired=True)
    with app_db.SessionLocal() as stale:
        observed = stale.query(Lease).one()
        assert observed.owner_id == ""
        with app_db.SessionLocal() as current:
            current.add(SetupRebuildJob(job_id="job-a", status="running", owner_id="worker-a"))
            current.commit()
            assert SetupRepository().acquire_lease(current, owner_id="worker-a", job_id="job-a")
        assert not SetupRepository().acquire_lease(stale, owner_id="worker-b", job_id="job-b")


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "rolled_back", None])
def test_status_alone_does_not_release_a_live_lease(status):
    seed(status=status)
    with app_db.SessionLocal() as db:
        assert not SetupRepository().acquire_lease(db, owner_id="worker-b", job_id="job-b")


def test_conditional_release_cannot_clear_replacement_owner():
    seed()
    with app_db.SessionLocal() as stale:
        previous = stale.query(Lease).one()
        assert previous.owner_id == "worker-a"
        with app_db.SessionLocal() as current:
            current.query(Lease).update({"owner_id": "worker-b", "job_id": "job-b"})
            current.commit()
        SetupRepository().release_lease(stale, owner_id="worker-a", job_id="job-a")
        assert stale.query(Lease).one().owner_id == "worker-b"


def test_release_and_reacquire_and_exact_job_renewal():
    seed()
    repo = SetupRepository()
    with app_db.SessionLocal() as db:
        assert repo.acquire_lease(db, owner_id="worker-a", job_id="job-a")
        assert not repo.renew_lease(db, owner_id="worker-a", job_id="wrong-job")
        assert repo.renew_lease(db, owner_id="worker-a", job_id="job-a")
        repo.release_lease(db, owner_id="worker-a", job_id="job-a")
        assert repo.acquire_lease(db, owner_id="worker-b", job_id="job-b")


@pytest.mark.parametrize("owner,job", [("", "a"), ("a", "")])
def test_empty_ownership_is_rejected(owner, job):
    with app_db.SessionLocal() as db, pytest.raises(ValueError):
        SetupRepository().acquire_lease(db, owner_id=owner, job_id=job)


def test_null_expiry_with_owner_is_not_a_free_lease():
    seed(status=None)
    with app_db.SessionLocal() as db:
        db.query(Lease).update({"expires_at": None})
        db.commit()
        assert not SetupRepository().acquire_lease(db, owner_id="worker-b", job_id="job-b")


def test_recovery_after_expiry_is_local_and_preserves_live_work_before_expiry():
    seed()
    coordinator = SetupCoordinator(session_factory=app_db.SessionLocal)
    asyncio.run(coordinator.resume_pending_jobs())
    with app_db.SessionLocal() as db:
        assert db.query(SetupRebuildJob).one().status == "running"
        db.query(Lease).update({"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
        db.commit()
    asyncio.run(coordinator.resume_pending_jobs())
    with app_db.SessionLocal() as db:
        assert db.query(SetupRebuildJob).one().status == "failed"
        assert db.query(Lease).one().owner_id == ""


def test_bounded_startup_waits_for_expiry_without_replaying(monkeypatch):
    seed()
    sleeps = []
    async def expire(seconds):
        sleeps.append(seconds)
        with app_db.SessionLocal() as db:
            db.query(Lease).update({"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
            db.commit()
    monkeypatch.setattr(asyncio, "sleep", expire)
    coordinator = SetupCoordinator(session_factory=app_db.SessionLocal)
    asyncio.run(coordinator.resume_pending_jobs(attempts=3))
    assert sleeps == [5.0]
    with app_db.SessionLocal() as db:
        assert db.query(SetupRebuildJob).one().status == "failed"


def test_bounded_startup_does_not_time_out_a_live_worker(monkeypatch):
    seed()
    sleeps = []
    async def renew(seconds):
        sleeps.append(seconds)
        with app_db.SessionLocal() as db:
            assert SetupRepository().renew_lease(db, owner_id="worker-a", job_id="job-a")
    monkeypatch.setattr(asyncio, "sleep", renew)
    coordinator = SetupCoordinator(session_factory=app_db.SessionLocal)
    asyncio.run(coordinator.resume_pending_jobs(attempts=3))
    assert len(sleeps) == 2
    with app_db.SessionLocal() as db:
        assert db.query(SetupRebuildJob).one().status == "running"


def test_heartbeat_stops_on_lost_lease():
    seed(expired=True)
    watch = LeaseWatch(app_db.SessionLocal, "worker-a", "job-a")
    async def bounded_heartbeat():
        task = asyncio.create_task(watch.heartbeat(interval=0))
        for _ in range(4):
            await asyncio.sleep(0)
        finished = task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert finished, "heartbeat continued after ownership expired"
    asyncio.run(bounded_heartbeat())
    assert watch.lost
    with pytest.raises(SetupLeaseLost):
        watch.checkpoint()


def test_failed_heartbeat_is_not_cleared_by_a_later_read():
    seed()
    watch = LeaseWatch(app_db.SessionLocal, "worker-a", "job-a")
    watch.checkpoint()
    watch.lost = True
    with pytest.raises(SetupLeaseLost):
        watch.checkpoint()


def test_control_database_error_fails_closed():
    def unavailable():
        raise RuntimeError("simulated unavailable database")
    watch = LeaseWatch(unavailable, "worker-a", "job-a")
    with pytest.raises(SetupLeaseLost):
        watch.checkpoint()
    watch = LeaseWatch(unavailable, "worker-a", "job-a")
    asyncio.run(watch.heartbeat(interval=0))
    assert watch.lost


def test_terminal_job_cannot_replay_adapter():
    adapter = RecordingAdapter()
    coordinator, job_id = prepared(adapter)
    async def run():
        assert (await coordinator.execute(job_id))["status"] == "completed"
        previous = list(adapter.calls)
        assert (await coordinator.execute(job_id))["status"] == "completed"
        assert adapter.calls == previous
    asyncio.run(run())


def test_interrupted_checkpoint_is_not_replayed():
    adapter = RecordingAdapter()
    coordinator, job_id = prepared(adapter)
    with coordinator.session_factory() as db:
        coordinator.repository.states(db, job_id)[0].state = SetupState.BASSWIESN_ROUTE_ACTIVE.value
        db.commit()
    outcome = asyncio.run(coordinator.execute(job_id))
    assert outcome["status"] == "failed"
    assert "SETUP_REPLAY_DENIED" in outcome["error"]
    assert adapter.calls == []


def test_duplicate_execute_and_rollback_do_not_release_running_lease():
    class BlockingAdapter(RecordingAdapter):
        async def route(self, row, target):
            entered.set()
            await release.wait()
            return await super().route(row, target)
    adapter = BlockingAdapter()
    coordinator, job_id = prepared(adapter)
    async def run():
        nonlocal entered, release
        entered, release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(coordinator.execute(job_id))
        await entered.wait()
        for operation in [coordinator.execute, coordinator.rollback]:
            with pytest.raises(RuntimeError, match="already active"):
                await operation(job_id)
        with coordinator.session_factory() as db:
            assert db.query(Lease).one().owner_id == coordinator.owner_id
        release.set()
        assert (await task)["status"] == "completed"
    entered, release = None, None
    asyncio.run(run())


def test_lost_lease_after_route_preserves_evidence_and_sends_no_reboot():
    class LosingAdapter(RecordingAdapter):
        async def route(self, row, target):
            result = await super().route(row, target)
            with coordinator.session_factory() as db:
                db.query(Lease).update({"owner_id": "replacement-worker", "job_id": "replacement"})
                db.commit()
            return result
    adapter = LosingAdapter()
    coordinator, job_id = prepared(adapter)
    outcome = asyncio.run(coordinator.execute(job_id))
    assert outcome["status"] == "failed"
    assert outcome["devices"][0]["recovery_status"] == "LEASE_LOST_REVIEW_REQUIRED"
    assert outcome["devices"][0]["evidence"]["routing_status"] == "active"
    assert "reboot" not in adapter.calls
    with coordinator.session_factory() as db:
        assert db.query(Lease).one().owner_id == "replacement-worker"


def test_two_sessions_claiming_empty_database_have_exactly_one_owner():
    barrier = Barrier(2)
    def claim(index):
        with app_db.SessionLocal() as db:
            barrier.wait(timeout=5)
            return SetupRepository().acquire_lease(db, owner_id=f"worker-{index}", job_id=f"job-{index}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, [0, 1])) == [False, True]
    with app_db.SessionLocal() as db:
        assert db.query(Lease).count() == 1


def test_heartbeat_does_not_commit_executors_staged_job_state(monkeypatch):
    seed()
    iterations = 0
    async def next_tick(_seconds):
        nonlocal iterations
        iterations += 1
        if iterations > 1:
            raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, "sleep", next_tick)
    with app_db.SessionLocal() as executor:
        job = executor.query(SetupRebuildJob).one()
        job.status = "uncommitted-test-state"
        watch = LeaseWatch(app_db.SessionLocal, "worker-a", "job-a")
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(watch.heartbeat())
        with app_db.SessionLocal() as observer:
            assert observer.query(SetupRebuildJob).one().status == "running"
        executor.rollback()


def test_rollback_lease_loss_does_not_claim_whole_job_restored():
    class LosingRollback(RecordingAdapter):
        async def rollback(self, row):
            result = await super().rollback(row)
            with coordinator.session_factory() as db:
                db.query(Lease).update({"owner_id": "replacement-worker", "job_id": "replacement"})
                db.commit()
            return result
    adapter = LosingRollback()
    coordinator, job_id = prepared(adapter)
    async def run():
        assert (await coordinator.execute(job_id))["status"] == "completed"
        result = await coordinator.rollback(job_id)
        assert result["status"] == "rollback_failed"
        assert result["devices"][0]["recovery_status"] == "LEASE_LOST_REVIEW_REQUIRED"
        assert result["devices"][0]["evidence"]["rollback"]["rolled_back"]
    asyncio.run(run())
