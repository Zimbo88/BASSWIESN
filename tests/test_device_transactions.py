"""Orchestration leases: ordering, nesting and cancellation without transports."""
import asyncio

import pytest

from basswiesn.app.services.device_ownership import DeviceBusy, DeviceLockUnavailable
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.device_transactions import device_transaction

pytestmark = pytest.mark.unit


def test_nested_subset_borrows_ownership_but_does_not_release_parent():
    async def run():
        async with device_transaction(["b", "A", "a"]):
            async with device_transaction(["a"]):
                assert device_lock("A").owned_by_current_task()
            assert device_lock("A").owned_by_current_task()
            with pytest.raises(DeviceBusy):
                async with device_transaction(["A", "C"]):
                    pytest.fail("target expansion")
        assert not device_lock("A").locked()
        assert not device_lock("B").locked()
    asyncio.run(run())


def test_child_task_cannot_borrow_inherited_context():
    async def run():
        lock = device_lock("A")
        lock.timeout = 0.01
        async def child():
            with pytest.raises(DeviceBusy):
                async with device_transaction(["A"]):
                    pytest.fail("child borrowed its parent's lock")
        async with device_transaction(["A"]):
            await asyncio.create_task(child())
            assert lock.owned_by_current_task()
    asyncio.run(run())


def test_partial_acquisition_failure_releases_earlier_targets():
    async def run():
        entered = asyncio.Event()
        finish = asyncio.Event()
        async def owner():
            async with device_transaction(["B"]):
                entered.set()
                await finish.wait()
        task = asyncio.create_task(owner())
        await entered.wait()
        device_lock("B").timeout = 0.01
        with pytest.raises(DeviceBusy):
            async with device_transaction(["B", "A"]):
                pytest.fail("partially acquired transaction ran")
        assert not device_lock("A").locked()
        assert device_lock("B").locked()
        finish.set()
        await task
    asyncio.run(run())


def test_raw_owner_can_borrow_same_target_but_not_add_another():
    async def run():
        async with device_lock("A"):
            async with device_transaction(["A"]):
                pass
            with pytest.raises(DeviceBusy):
                async with device_transaction(["A", "B"]):
                    pytest.fail("unsafe expansion")
            assert device_lock("A").owned_by_current_task()
    asyncio.run(run())


def test_group_targets_acquired_in_canonical_order(monkeypatch):
    from basswiesn.app.services import device_transactions
    original = device_transactions.device_lock
    requests = []
    def traced(identity):
        requests.append(identity)
        return original(identity)
    monkeypatch.setattr(device_transactions, "device_lock", traced)
    async def run():
        async with device_transaction(["z", " a ", "Z"]):
            pass
    asyncio.run(run())
    assert requests == ["A", "Z"]


@pytest.mark.parametrize("targets", [[], [""], ["A", " "], ["x" * 129]])
def test_invalid_targets_never_enter_transaction(targets):
    async def run():
        with pytest.raises(DeviceLockUnavailable):
            async with device_transaction(targets):
                pytest.fail("invalid target")
    asyncio.run(run())


def test_preview_never_opens_ownership_namespace(monkeypatch):
    from basswiesn.app.services import device_transactions
    monkeypatch.setattr(device_transactions, "device_lock", lambda _: pytest.fail("preview acquired lock"))
    async def run():
        async with device_transaction(["A"], enabled=False):
            pass
    asyncio.run(run())
