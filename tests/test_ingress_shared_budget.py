"""Multiple listener/worker instances must not multiply ingress authority."""
import asyncio
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from basswiesn.app.core import device_ingress as ingress
from basswiesn.app.core.device_ingress import DeviceIngress
from basswiesn.app.core import ingress_budget
from basswiesn.app import db as app_db
from basswiesn.app.models import RuntimeState
from test_device_ingress import PEER, invoke

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("limit", ["peer", "global"])
def test_active_limits_are_shared_by_independent_listener_instances(monkeypatch, limit):
    monkeypatch.setattr(ingress, "MAX_ACTIVE" if limit == "peer" else "MAX_TOTAL_ACTIVE", 1)

    async def run():
        first = DeviceIngress((PEER, "192.0.2.43"))
        second = DeviceIngress((PEER, "192.0.2.43"))
        entered, finish = asyncio.Event(), asyncio.Event()

        async def hold():
            entered.set()
            await finish.wait()

        task = asyncio.create_task(invoke(first, handler=hold))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            sent, called = await invoke(second, peer=PEER if limit == "peer" else "192.0.2.43")
            assert sent[0]["status"] == 429 and called == []
        finally:
            finish.set()
            await task
        assert (await invoke(second))[0][0]["status"] == 200

    asyncio.run(run())


def test_restart_cannot_replenish_burst(monkeypatch):
    monkeypatch.setattr(ingress, "BURST", 2.0)
    monkeypatch.setattr(ingress, "RATE", 1.0)

    async def run():
        for _ in range(2):
            assert (await invoke(DeviceIngress((PEER,), clock=lambda: 100.0)))[0][0]["status"] == 200
        sent, called = await invoke(DeviceIngress((PEER,), clock=lambda: 100.0))
        assert sent[0]["status"] == 429 and called == []
        assert (await invoke(DeviceIngress((PEER,), clock=lambda: 101.0)))[0][0]["status"] == 200

    asyncio.run(run())


def test_clock_rollback_cannot_create_extra_tokens(monkeypatch):
    monkeypatch.setattr(ingress, "BURST", 1.0)
    monkeypatch.setattr(ingress, "RATE", 1.0)
    now = [100.0]

    async def run():
        policy = DeviceIngress((PEER,), clock=lambda: now[0])
        assert (await invoke(policy))[0][0]["status"] == 200
        now[0] = 99.0
        assert (await invoke(policy))[0][0]["status"] == 429
        now[0] = 100.0
        assert (await invoke(policy))[0][0]["status"] == 429
        now[0] = 101.0
        assert (await invoke(policy))[0][0]["status"] == 200

    asyncio.run(run())


def process_request_owner(root, database_url, connection, crash):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(database_url)
    app_db.SessionLocal = sessionmaker(bind=engine)
    ingress_budget.lock_root = lambda: Path(root)

    async def hold():
        limits = ingress_budget.SharedIngressBudget(clock=lambda: 100.0, burst=2.0, rate=1.0,
                                                    max_active=1, max_total=32)
        lease = await limits.acquire(PEER)
        assert lease is not None
        connection.send("owned")
        connection.recv()
        if crash:
            os._exit(7)
        lease.release()

    asyncio.run(hold())
    connection.close()
    engine.dispose()


@pytest.mark.parametrize("crash", [False, True])
def test_process_exit_releases_capacity_but_does_not_reset_rate(tmp_path, monkeypatch, crash):
    monkeypatch.setattr(ingress, "MAX_ACTIVE", 1)
    monkeypatch.setattr(ingress, "BURST", 2.0)
    monkeypatch.setattr(ingress, "RATE", 1.0)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=process_request_owner, args=(
        str(tmp_path / "ingress-locks"), str(app_db.engine.url), child, crash))
    process.start()
    child.close()
    try:
        assert parent.poll(10) and parent.recv() == "owned"

        async def request():
            return await invoke(DeviceIngress((PEER,), clock=lambda: 100.0))

        assert asyncio.run(request())[0][0]["status"] == 429
        parent.send("finish")
        process.join(10)
        assert process.exitcode == (7 if crash else 0)
        assert asyncio.run(request())[0][0]["status"] == 200
        assert asyncio.run(request())[0][0]["status"] == 429
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(5)


@pytest.mark.parametrize("state", [
    "not json", "[]", "null",
    json.dumps({"version": True, "tokens": 1, "observed": 100}),
    json.dumps({"version": 2, "tokens": 1, "observed": 100}),
    json.dumps({"version": 1, "tokens": -1, "observed": 100}),
    json.dumps({"version": 1, "tokens": 121, "observed": 100}),
    json.dumps({"version": 1, "tokens": True, "observed": 100}),
    json.dumps({"version": 1, "tokens": 1, "observed": -1}),
    json.dumps({"version": 1, "tokens": 1, "observed": float("nan")}),
])
def test_corrupt_rate_state_fails_closed_without_reset(monkeypatch, state):
    with app_db.SessionLocal() as db:
        db.add(RuntimeState(key=ingress_budget.budget_key(PEER), value=state))
        db.commit()
    sent, called = asyncio.run(invoke(DeviceIngress((PEER,), clock=lambda: 100.0)))
    assert sent[0]["status"] == 503 and called == []
    assert b"DEVICE_BUDGET_UNAVAILABLE" in sent[-1]["body"]
    with app_db.SessionLocal() as db:
        row = db.query(RuntimeState).filter_by(key=ingress_budget.budget_key(PEER)).one()
        assert row.value == state


def test_failed_storage_reservation_releases_slots_and_redacts_error(monkeypatch):
    monkeypatch.setattr(ingress, "MAX_TOTAL_ACTIVE", 1)
    original = ingress_budget.SharedIngressBudget._reserve_rate

    def unavailable(self, peer):
        raise OSError("private storage canary")

    monkeypatch.setattr(ingress_budget.SharedIngressBudget, "_reserve_rate", unavailable)
    sent, called = asyncio.run(invoke(DeviceIngress((PEER,))))
    assert sent[0]["status"] == 503 and called == []
    assert "private storage canary" not in str(sent)
    monkeypatch.setattr(ingress_budget.SharedIngressBudget, "_reserve_rate", original)
    assert asyncio.run(invoke(DeviceIngress((PEER,))))[0][0]["status"] == 200


def test_unsafe_shared_lock_directory_never_reaches_handler(tmp_path):
    directory = tmp_path / "ingress-locks"
    directory.mkdir()
    # A restrictive developer umask must not silently make this unsafe fixture
    # safe; production correctly accepts 0700 and rejects this explicit 0755.
    directory.chmod(0o755)
    sent, called = asyncio.run(invoke(DeviceIngress((PEER,))))
    assert sent[0]["status"] == 503 and called == []


def test_backward_clock_denies_even_a_nonempty_bucket(monkeypatch):
    monkeypatch.setattr(ingress, "BURST", 3.0)
    monkeypatch.setattr(ingress, "RATE", 1.0)
    assert asyncio.run(invoke(DeviceIngress((PEER,), clock=lambda: 100.0)))[0][0]["status"] == 200
    assert asyncio.run(invoke(DeviceIngress((PEER,), clock=lambda: 99.5)))[0][0]["status"] == 429


def test_concurrent_reservations_cannot_double_spend_tokens(monkeypatch):
    monkeypatch.setattr(ingress, "BURST", 2.0)
    monkeypatch.setattr(ingress, "RATE", 1.0)

    async def run():
        results = await asyncio.gather(*[
            invoke(DeviceIngress((PEER,), clock=lambda: 100.0)) for _ in range(12)
        ])
        assert sum(result[0][0]["status"] == 200 for result in results) == 2
        assert all(result[0][0]["status"] in (200, 429) for result in results)

    asyncio.run(run())
