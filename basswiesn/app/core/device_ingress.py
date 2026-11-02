"""Bounded device-protocol ingress for the opt-in authenticated listener mode.

An explicitly allowed socket peer is a network policy, not device cryptographic
authentication. Never trust forwarding headers or learn peers from requests.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import ipaddress
import time

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from basswiesn.app.core.protocol_identity import cached_account_binding, registration_device_id
from basswiesn.app.core.ingress_budget import IngressBudgetUnavailable, SharedIngressBudget

MAX_BODY = 1024 * 1024
BODY_TIMEOUT = 5.0
MAX_HEADERS = 16 * 1024
MAX_TARGET = 8192
BURST = 120.0
RATE = 2.0
MAX_ACTIVE = 4
MAX_TOTAL_ACTIVE = 32


def canonical_peer(value: str) -> str:
    try:
        if "%" in value:
            return ""
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if address.is_unspecified or address.is_multicast:
            return ""
        return str(address)
    except ValueError:
        return ""


def cached_device_peer(device_id: str) -> str | None:
    # Incoming traffic is never permission to discover or probe a radio. Only
    # an existing administration-controlled inventory row can bind a path ID.
    from basswiesn.app import db as app_db
    from basswiesn.app.models import Device
    with app_db.SessionLocal() as db:
        rows = db.query(Device.device_id, Device.ip_address).all()
        matches = [row for row in rows if row.device_id.upper() == device_id.upper()]
        if len(matches) != 1:
            return None
        peer = canonical_peer(matches[0].ip_address)
        # A stale inventory row sharing an address cannot authorize a second
        # identity, including IPv4-mapped spelling. Never learn from callbacks.
        if not peer or sum(canonical_peer(row.ip_address) == peer for row in rows) != 1:
            return None
        return peer


@dataclass
class Budget:
    """Local diagnostic count only; never an authorization/rate authority."""
    active: int = 0


async def reject(send: Send, code: str, status: int) -> None:
    response = JSONResponse({"error": {"code": code, "message": "Device request not permitted"}},
                            status_code=status, headers={"Cache-Control": "no-store"})
    await send({"type": "http.response.start", "status": status, "headers": response.raw_headers})
    await send({"type": "http.response.body", "body": response.body})


class DeviceIngress:
    def __init__(self, peers: tuple[str, ...], *, clock: Callable[[], float] = time.time,
                 identity_peer: Callable[[str], str | None] = cached_device_peer,
                 account_binding: Callable[[str, str, str | None], bool] = cached_account_binding) -> None:
        normalized = tuple(canonical_peer(value) for value in peers)
        self.valid = bool(normalized) and len(normalized) <= 256 and all(normalized)
        self.peers = frozenset(normalized) if self.valid else frozenset()
        self.clock, self.identity_peer = clock, identity_peer
        self.account_binding = account_binding
        self.budgets: dict[str, Budget] = {}

    async def __call__(self, app: ASGIApp, scope: Scope, receive: Receive, send: Send,
                       *, device_id: str | None = None, account_id: str | None = None,
                       registration: bool = False) -> None:
        if not self.valid:
            await reject(send, "DEVICE_PEER_POLICY_UNAVAILABLE", 503)
            return
        peer = canonical_peer(str((scope.get("client") or ("", 0))[0]))
        if peer not in self.peers:
            await reject(send, "DEVICE_PEER_DENIED", 403)
            return
        headers = scope.get("headers", [])
        if (sum(len(key) + len(value) for key, value in headers) > MAX_HEADERS
                or len(scope.get("raw_path", b"")) + len(scope.get("query_string", b"")) > MAX_TARGET):
            await reject(send, "DEVICE_REQUEST_TOO_LARGE", 413)
            return
        lengths = [value for key, value in headers if key.lower() == b"content-length"]
        encodings = [value for key, value in headers if key.lower() == b"content-encoding"]
        if (len(lengths) > 1 or any(not value.isdigit() or len(value) > 10 for value in lengths)
                or any(value.strip().lower() != b"identity" for value in encodings)):
            await reject(send, "DEVICE_REQUEST_FRAMING", 400)
            return
        if lengths and int(lengths[0]) > MAX_BODY:
            await reject(send, "DEVICE_REQUEST_TOO_LARGE", 413)
            return
        try:
            limits = SharedIngressBudget(clock=self.clock, burst=BURST, rate=RATE,
                                         max_active=MAX_ACTIVE, max_total=MAX_TOTAL_ACTIVE)
            lease = await limits.acquire(peer)
        except IngressBudgetUnavailable:
            await reject(send, "DEVICE_BUDGET_UNAVAILABLE", 503)
            return
        if lease is None:
            await reject(send, "DEVICE_RATE_LIMITED", 429)
            return
        budget = self.budgets.setdefault(peer, Budget())
        budget.active += 1
        try:
            # Buffer under a strict budget BEFORE the handler. A streamed overrun
            # must not arrive after the handler has already mutated local state.
            body = bytearray()
            try:
                async with asyncio.timeout(BODY_TIMEOUT):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        if len(body) + len(chunk) > MAX_BODY:
                            await reject(send, "DEVICE_REQUEST_TOO_LARGE", 413)
                            return
                        body.extend(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await reject(send, "DEVICE_REQUEST_TIMEOUT", 408)
                return
            if lengths and len(body) != int(lengths[0]):
                await reject(send, "DEVICE_REQUEST_FRAMING", 400)
                return
            if registration:
                try:
                    body_id = registration_device_id(bytes(body))
                except ValueError:
                    await reject(send, "DEVICE_REGISTRATION_INVALID", 400)
                    return
                if device_id is not None and device_id != body_id:
                    await reject(send, "DEVICE_IDENTITY_DENIED", 403)
                    return
                device_id = body_id
            if device_id is not None:
                if not device_id or len(device_id) > 128:
                    await reject(send, "DEVICE_IDENTITY_DENIED", 403)
                    return
                try:
                    known = await run_in_threadpool(self.identity_peer, device_id)
                except Exception:
                    await reject(send, "DEVICE_IDENTITY_UNAVAILABLE", 503)
                    return
                if known is None or canonical_peer(known) != peer:
                    await reject(send, "DEVICE_IDENTITY_DENIED", 403)
                    return
            if account_id is not None:
                if not account_id or len(account_id) > 128:
                    await reject(send, "DEVICE_ACCOUNT_DENIED", 403)
                    return
                try:
                    bound = await run_in_threadpool(self.account_binding, peer, account_id, device_id)
                except Exception:
                    await reject(send, "DEVICE_ACCOUNT_UNAVAILABLE", 503)
                    return
                if not bound:
                    await reject(send, "DEVICE_ACCOUNT_DENIED", 403)
                    return
            consumed = False
            # Handler-specific contracts may use this checked socket peer.
            # It is server-side scope state, never an HTTP header claim.
            scope.setdefault("state", {})["device_ingress_peer"] = peer

            async def bounded_receive() -> Message:
                nonlocal consumed
                if consumed:
                    return await receive()
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            await app(scope, bounded_receive, send)
        finally:
            lease.release()
            budget.active -= 1
