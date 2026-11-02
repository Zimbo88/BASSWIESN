"""Setup ownership shares the real worker lock namespace; adapter is offline."""
import asyncio

import pytest

from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.device_transactions import device_transaction
from basswiesn.app.services.setup_rebuild.coordinator import SetupCoordinator
from basswiesn.app.services.setup_rebuild.server_target import ServerTarget
from test_setup_rebuild_160 import _session, _device, RecordingAdapter

pytestmark = pytest.mark.integration
IDENTITY = 'SETUP160A'


def prepared(adapter, *, presentation_mode="normal"):
    session = _session()
    with session() as db:
        db.add(_device())
        db.commit()
        coordinator = SetupCoordinator(adapter=adapter, session_factory=session)
        job = coordinator.start(db, device_ids=[IDENTITY],
                                target=ServerTarget('192.0.2.10', 1328, 1516, 1860),
                                options={'ssh_required': False, 'pair_account': True, 'playback_test': True, 'presentation_mode': presentation_mode})
    return coordinator, job['job_id']


class OwnedAdapter(RecordingAdapter):
    async def _result(self, name, value):
        assert device_lock(IDENTITY).owned_by_current_task(), name
        # A different radio does not inherit a global lock.
        assert not device_lock('UNRELATED').locked()
        return await super()._result(name, value)


def test_setup_and_rollback_own_radio_through_all_adapter_steps():
    adapter = OwnedAdapter()
    coordinator, job = prepared(adapter)
    async def run():
        assert (await coordinator.execute(job))['status'] == 'completed'
        assert not device_lock(IDENTITY).locked()
        assert (await coordinator.rollback(job))['status'] == 'rolled_back'
        assert not device_lock(IDENTITY).locked()
    asyncio.run(run())
    assert adapter.calls[-1] == 'rollback'


@pytest.mark.parametrize('operation', ['execute', 'rollback'])
def test_competing_worker_blocks_adapter_and_never_reports_success(operation):
    adapter = RecordingAdapter()
    coordinator, job = prepared(adapter)
    async def run():
        if operation == 'rollback':
            assert (await coordinator.execute(job))['status'] == 'completed'
        adapter.calls.clear()
        entered, release = asyncio.Event(), asyncio.Event()
        async def worker():
            async with device_transaction([IDENTITY]):
                entered.set()
                await release.wait()
        worker_task = asyncio.create_task(worker())
        await entered.wait()
        device_lock(IDENTITY).timeout = 0.01
        try:
            outcome = await getattr(coordinator, operation)(job)
            assert outcome['status'] == ('failed' if operation == 'execute' else 'rollback_failed')
            assert adapter.calls == []
            if operation == 'execute':
                assert outcome['devices'][0]['recovery_status'] == 'NO_WRITE_OWNERSHIP_DENIED'
                assert outcome['devices'][0]['last_error'] == 'DEVICE_BUSY'
        finally:
            release.set()
            await worker_task
        assert not device_lock(IDENTITY).locked()
    asyncio.run(run())


def test_cancelled_execution_releases_ownership_without_replaying_steps():
    started, release = None, None
    class BlockingAdapter(RecordingAdapter):
        async def route(self, row, target):
            assert device_lock(IDENTITY).owned_by_current_task()
            started.set()
            await release.wait()
    adapter = BlockingAdapter()
    coordinator, job = prepared(adapter)
    async def run():
        nonlocal started, release
        started, release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(coordinator.execute(job))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not device_lock(IDENTITY).locked()
        assert 'reboot' not in adapter.calls
        await coordinator.resume_pending_jobs()
        assert 'reboot' not in adapter.calls
    asyncio.run(run())
