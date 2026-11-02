"""Untrusted device requests stop before handler/state changes on every failure."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from basswiesn.app.core import device_ingress as ingress
from basswiesn.app.core.device_ingress import DeviceIngress, canonical_peer
from basswiesn.app.main import create_cloud_app, create_web_app
from test_admin_boundary import secured  # noqa: F401

pytestmark = pytest.mark.integration
PEER = "192.0.2.42"


async def invoke(policy, *, peer=PEER, chunks=(b"",), headers=(), device_id=None,
                 handler=None, receive_override=None, path=b"/protocol", query=b"",
                 account_id=None, registration=False):
    sent, called = [], []
    messages = [{"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
                for index, chunk in enumerate(chunks)]
    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}
    async def send(message):
        sent.append(message)
    async def app(scope, incoming, outgoing):
        called.append(await incoming())
        if handler:
            await handler()
        await outgoing({"type": "http.response.start", "status": 200, "headers": []})
        await outgoing({"type": "http.response.body", "body": b"ok"})
    scope = {"type": "http", "method": "POST", "path": "/protocol", "raw_path": path,
             "query_string": query, "headers": list(headers), "client": (peer, 41000)}
    await policy(app, scope, receive_override or receive, send, device_id=device_id,
                 account_id=account_id, registration=registration)
    return sent, called


@pytest.mark.parametrize("peer,expected", [
    (PEER, PEER), ("::ffff:192.0.2.42", PEER), ("2001:db8::1", "2001:db8::1"),
    ("::", ""), ("0.0.0.0", ""), ("ff02::1", ""), ("224.0.0.1", ""),
    ("device.invalid", ""), ("192.0.2.0/24", ""), ("fe80::1%eth0", ""),
])
def test_canonical_peer_never_resolves_or_accepts_ranges(peer, expected):
    assert canonical_peer(peer) == expected


@pytest.mark.parametrize("peers,peer,status", [
    ((), PEER, 503), ((PEER, "bad"), PEER, 503), ((PEER,) * 257, PEER, 503),
    ((PEER,), "192.0.2.43", 403), ((PEER,), "::ffff:192.0.2.42", 200),
])
def test_exact_peer_policy_fails_closed(peers, peer, status):
    sent, called = asyncio.run(invoke(DeviceIngress(peers), peer=peer))
    assert sent[0]["status"] == status
    assert bool(called) == (status == 200)


@pytest.mark.parametrize("headers,chunks,status", [
    (((b"content-length", b"1048577"),), (b"",), 413),
    (((b"content-length", b"-1"),), (b"",), 400),
    (((b"content-length", b"9" * 5000),), (b"",), 400),
    (((b"content-length", b"1"), (b"content-length", b"1")), (b"x",), 400),
    (((b"content-encoding", b"gzip"),), (b"",), 400),
    (((b"content-length", b"1"),), (b"xx",), 400),
    ((), (b"x" * ingress.MAX_BODY, b"x"), 413),
    (((b"large", b"x" * ingress.MAX_HEADERS),), (b"",), 413),
])
def test_body_and_framing_limits_precede_handler(headers, chunks, status):
    policy = DeviceIngress((PEER,))
    sent, called = asyncio.run(invoke(policy, headers=headers, chunks=chunks))
    assert sent[0]["status"] == status and called == []
    assert all(b.active == 0 for b in policy.budgets.values())


@pytest.mark.parametrize("part", ["path", "query"])
def test_target_limit(part):
    sent, called = asyncio.run(invoke(DeviceIngress((PEER,)), **{part: b"x" * (ingress.MAX_TARGET + 1)}))
    assert sent[0]["status"] == 413 and not called


def test_timeout_disconnect_and_cancellation_release_ownership(monkeypatch):
    monkeypatch.setattr(ingress, "BODY_TIMEOUT", 0.01)
    async def run():
        policy = DeviceIngress((PEER,))
        async def wait():
            await asyncio.sleep(10)
        sent, called = await invoke(policy, receive_override=wait)
        assert sent[0]["status"] == 408 and not called
        async def disconnect():
            return {"type": "http.disconnect"}
        assert await invoke(policy, receive_override=disconnect) == ([], [])
        async def cancel():
            raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await invoke(policy, receive_override=cancel)
        assert policy.budgets[PEER].active == 0
    asyncio.run(run())


def test_buffered_body_is_replayed_exactly_once():
    policy = DeviceIngress((PEER,), identity_peer=lambda device: PEER if device == "EXAMPLE" else None)
    sent, called = asyncio.run(invoke(policy, chunks=(b"<a>", b"x</a>"), device_id="EXAMPLE"))
    assert sent[0]["status"] == 200
    assert called == [{"type": "http.request", "body": b"<a>x</a>", "more_body": False}]


def test_handler_can_observe_disconnect_without_replaying_buffered_body():
    async def run():
        policy = DeviceIngress((PEER,))
        received = []
        incoming = iter([
            {"type": "http.request", "body": b"<a/>", "more_body": False},
            {"type": "http.disconnect"},
        ])
        async def receive():
            return next(incoming)
        async def send(message):
            pytest.fail("test handler does not send a response")
        async def handler(scope, receive, send):
            received.extend([await receive(), await receive()])
        scope = {"type": "http", "client": (PEER, 41000), "headers": []}
        await policy(handler, scope, receive, send)
        assert received == [
            {"type": "http.request", "body": b"<a/>", "more_body": False},
            {"type": "http.disconnect"},
        ]
        assert policy.budgets[PEER].active == 0
    asyncio.run(run())


@pytest.mark.parametrize("known,identifier,code", [
    (None, "UNKNOWN", "DEVICE_IDENTITY_DENIED"),
    ("192.0.2.43", "EXAMPLE", "DEVICE_IDENTITY_DENIED"),
    (PEER, "x" * 129, "DEVICE_IDENTITY_DENIED"),
    (PEER, "", "DEVICE_IDENTITY_DENIED"),
])
def test_identity_binding_uses_inventory_not_request(known, identifier, code):
    sent, called = asyncio.run(invoke(DeviceIngress((PEER,), identity_peer=lambda _: known), device_id=identifier))
    assert json.loads(sent[1]["body"])["error"]["code"] == code
    assert not called


def test_inventory_failure_is_closed_and_not_echoed():
    def broken(_):
        raise RuntimeError("private database failure canary")
    sent, called = asyncio.run(invoke(DeviceIngress((PEER,), identity_peer=broken), device_id="EXAMPLE"))
    assert sent[0]["status"] == 503 and not called
    assert b"canary" not in sent[1]["body"]


def test_path_identity_cannot_authorize_ambiguous_peer_inventory():
    from basswiesn.app import db as app_db
    from basswiesn.app.models import Device
    with app_db.SessionLocal() as db:
        db.add(Device(device_id="EXAMPLE", ip_address=PEER, name="First"))
        db.add(Device(device_id="OTHER", ip_address="::ffff:" + PEER, name="Stale"))
        db.commit()
    sent, called = asyncio.run(invoke(DeviceIngress((PEER,)), device_id="EXAMPLE"))
    assert sent[0]["status"] == 403 and not called


def test_invalid_cached_address_is_not_an_identity_binding():
    from basswiesn.app import db as app_db
    from basswiesn.app.models import Device
    from basswiesn.app.core.device_ingress import cached_device_peer
    with app_db.SessionLocal() as db:
        db.add(Device(device_id="EXAMPLE", ip_address="invalid.invalid", name="Example"))
        db.commit()
    assert cached_device_peer("EXAMPLE") is None


def test_bounded_rate_budget_and_refill():
    now = [0.0]
    policy = DeviceIngress((PEER,), clock=lambda: now[0])
    async def run():
        for _ in range(120):
            sent, called = await invoke(policy)
            assert sent[0]["status"] == 200 and called
        sent, called = await invoke(policy)
        assert sent[0]["status"] == 429 and not called
        now[0] = 1
        assert (await invoke(policy))[0][0]["status"] == 200
        for i in range(300):
            await invoke(policy, peer=f"host-{i}.invalid")
        assert len(policy.budgets) == 1
    asyncio.run(run())


def test_parallel_budget_recovers_after_handler_failure(monkeypatch):
    monkeypatch.setattr(ingress, "MAX_ACTIVE", 1)
    async def run():
        policy, started, release = DeviceIngress((PEER,)), asyncio.Event(), asyncio.Event()
        async def handler():
            started.set()
            await release.wait()
            raise ValueError("simulated handler failure")
        first = asyncio.create_task(invoke(policy, handler=handler))
        await started.wait()
        sent, called = await invoke(policy)
        assert sent[0]["status"] == 429 and not called
        release.set()
        with pytest.raises(ValueError):
            await first
        assert (await invoke(policy))[0][0]["status"] == 200
    asyncio.run(run())


@pytest.mark.parametrize("factory", [create_web_app, create_cloud_app])
def test_real_device_route_checks_actual_peer_not_forwarding_headers(secured, factory):
    client = TestClient(factory(), base_url="http://device-service.invalid", client=("192.0.2.43", 41000))
    response = client.get("/streaming/sourceproviders", headers={"X-Forwarded-For": PEER, "X-Real-IP": PEER})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "DEVICE_PEER_DENIED"


def test_inventory_lookup_accepts_exact_record_and_rejects_unknown():
    from basswiesn.app import db as app_db
    from basswiesn.app.models import Device
    with app_db.SessionLocal() as db:
        db.add(Device(device_id="EXAMPLE", ip_address=PEER, name="Example Radio"))
        db.commit()
    assert ingress.cached_device_peer("example") == PEER
    assert ingress.cached_device_peer("another") is None


def test_global_active_budget_is_not_multiplied_by_allowed_peer_count(monkeypatch):
    monkeypatch.setattr(ingress, "MAX_TOTAL_ACTIVE", 1)
    async def run():
        policy = DeviceIngress((PEER, "192.0.2.43"))
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler():
            entered.set()
            await release.wait()
        first = asyncio.create_task(invoke(policy, handler=handler))
        await entered.wait()
        sent, called = await invoke(policy, peer="192.0.2.43")
        assert sent[0]["status"] == 429 and not called
        release.set()
        await first
        assert (await invoke(policy, peer="192.0.2.43"))[0][0]["status"] == 200
    asyncio.run(run())


@pytest.mark.parametrize("factory", [create_web_app, create_cloud_app])
def test_real_registered_path_requires_peer_identity_match(secured, factory):
    from basswiesn.app import db as app_db
    from basswiesn.app.models import Device
    with app_db.SessionLocal() as db:
        db.add(Device(device_id="EXAMPLE", ip_address=PEER))
        db.add(Device(device_id="OTHER", ip_address="192.0.2.99"))
        db.commit()
    client = TestClient(factory(), base_url="http://device-service.invalid", client=(PEER, 41000))
    assert client.get("/v1/blacklist/EXAMPLE").status_code == 200
    for identity in ("OTHER", "MISSING"):
        response = client.get(f"/v1/blacklist/{identity}")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "DEVICE_IDENTITY_DENIED"
