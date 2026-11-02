"""Real local process/task contention without a radio or network transport."""
import asyncio
import multiprocessing
import os
from pathlib import Path

import pytest

from basswiesn.app.services.device_ownership import DeviceBusy, DeviceLockUnavailable, DeviceOwnership


def process_owner(root, connection, crash):
    async def hold():
        async with DeviceOwnership(Path(root), "EXAMPLE"):
            connection.send("owned")
            connection.recv()
            if crash:
                os._exit(7)
    asyncio.run(hold())
    connection.close()


@pytest.mark.parametrize("crash", [False, True])
def test_separate_process_blocks_then_releases_even_after_crash(tmp_path, crash):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=process_owner, args=(str(tmp_path / "locks"), child, crash))
    process.start()
    child.close()
    try:
        assert parent.poll(10) and parent.recv() == "owned"
        lock = DeviceOwnership(tmp_path / "locks", "example", timeout=0.02)
        assert lock.locked()
        async def blocked():
            with pytest.raises(DeviceBusy):
                async with lock:
                    pytest.fail("overlapping owner")
            # Ownership is per device; another radio is not globally blocked.
            async with DeviceOwnership(tmp_path / "locks", "OTHER"):
                pass
        asyncio.run(blocked())
        parent.send("finish")
        process.join(10)
        assert process.exitcode == (7 if crash else 0)
        assert not lock.locked()
        async def recover():
            async with lock:
                assert lock.locked()
        asyncio.run(recover())
        assert len(list((tmp_path / "locks").glob("*.lock"))) == 2
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(5)


def test_tasks_serialize_and_cancelled_waiter_does_not_release_owner(tmp_path):
    async def run():
        lock = DeviceOwnership(tmp_path / "locks", "EXAMPLE", timeout=1)
        events = []
        async with lock:
            task = asyncio.create_task(lock.acquire())
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert lock.locked()
            other = DeviceOwnership(tmp_path / "locks", "EXAMPLE", timeout=1)
            async def wait():
                async with other:
                    events.append("second")
            waiting = asyncio.create_task(wait())
            await asyncio.sleep(0.02)
            assert events == []
            events.append("first")
        await waiting
        assert events == ["first", "second"]
        assert not lock.locked()
    asyncio.run(run())


def test_cancelled_owner_releases_and_no_reentrant_deadlock(tmp_path):
    async def run():
        lock, entered = DeviceOwnership(tmp_path / "locks", "EXAMPLE"), asyncio.Event()
        async def hold():
            async with lock:
                with pytest.raises(DeviceBusy):
                    await lock.acquire()
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(hold())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with lock:
            pass
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["root-link", "ancestor-link", "leaf-link", "fifo", "hardlink", "permissions", "root-permissions"])
def test_unsafe_lock_namespace_is_closed(tmp_path, kind):
    root = tmp_path / "locks"
    root.mkdir(mode=0o700)
    lock = DeviceOwnership(root, "EXAMPLE", timeout=0)
    leaf = root / lock.leaf
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    target = other / "target"
    target.touch(mode=0o600)
    if kind == "root-link":
        lock = DeviceOwnership(tmp_path / "alias", "EXAMPLE")
        (tmp_path / "alias").symlink_to(root, target_is_directory=True)
    elif kind == "ancestor-link":
        (tmp_path / "alias").symlink_to(other, target_is_directory=True)
        lock = DeviceOwnership(tmp_path / "alias" / "locks", "EXAMPLE")
    elif kind == "leaf-link":
        leaf.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(leaf, 0o600)
    elif kind == "hardlink":
        os.link(target, leaf)
    elif kind == "permissions":
        leaf.touch(mode=0o600)
        leaf.chmod(0o644)
    else:
        root.chmod(0o755)
    async def denied():
        with pytest.raises(DeviceLockUnavailable):
            await lock.acquire()
    asyncio.run(denied())
    assert lock.locked()
    assert target.read_bytes() == b""


def test_replaced_inode_during_acquire_is_not_granted(tmp_path, monkeypatch):
    lock = DeviceOwnership(tmp_path / "locks", "EXAMPLE")
    monkeypatch.setattr(lock, "_same_namespace", lambda _: False)
    async def run():
        with pytest.raises(DeviceLockUnavailable):
            await lock.acquire()
    asyncio.run(run())
    assert lock.locked()


@pytest.mark.parametrize("identity", ["", "  ", "x" * 129])
def test_identity_must_be_stable_nonempty_and_bounded(tmp_path, identity):
    with pytest.raises(DeviceLockUnavailable):
        DeviceOwnership(tmp_path / "locks", identity)


def test_namespace_and_case_are_part_of_shared_factory_key(tmp_path, monkeypatch):
    from basswiesn.app.services import device_policy, device_ownership
    monkeypatch.setattr(device_ownership, "lock_root", lambda: tmp_path / "one")
    one = device_policy.device_lock("example")
    assert device_policy.device_lock(" EXAMPLE ") is one
    monkeypatch.setattr(device_ownership, "lock_root", lambda: tmp_path / "two")
    assert device_policy.device_lock("EXAMPLE") is not one


def test_wrong_task_or_unheld_release_cannot_free_lock(tmp_path):
    async def run():
        lock = DeviceOwnership(tmp_path / "locks", "EXAMPLE")
        with pytest.raises(RuntimeError):
            lock.release()
        async with lock:
            async def attacker():
                with pytest.raises(RuntimeError):
                    lock.release()
            await asyncio.create_task(attacker())
            assert lock.locked()
    asyncio.run(run())
