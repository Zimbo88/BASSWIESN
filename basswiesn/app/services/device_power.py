"""Explicit one-shot power requests; an HTTP GET can still be a device write.

Regular standby has an observable source-state contract. Low-power standby does
not: disappearance (or ordinary standby) cannot distinguish deep sleep from a
network failure. Never wake, replay or automatically restore either operation.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from basswiesn.app.models import ConfigBackup, Device
from basswiesn.app.services.action_journal import record_action, record_transport_attempt
from basswiesn.app.services.device_settings_writer import ClientFactory, IdentityMismatch, verify_settings_identity
from basswiesn.app.services.device_transactions import device_fingerprint, device_transaction
from basswiesn.app.services.live_radio_reconnect import cancel
from basswiesn.app.services.maintenance_reboot import PATHS, RebootSnapshot, _root
from basswiesn.app.services.playback_state import close_open_sessions

POWER_PATHS = {"standby": "/standby", "low_power_standby": "/lowPowerStandby"}


class PowerActionError(Exception):
    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(str(detail["code"]))


async def execute_power(
    db: Session, device: Device, action: str, *, client_factory: ClientFactory,
    guard: Callable[[Session, Device], None],
) -> dict[str, Any]:
    path = POWER_PATHS[action]
    frozen = device_fingerprint(device)
    identity, address = frozen[:2]
    result: dict[str, Any] = {
        "device_id": identity, "job_id": f"power-{uuid4().hex}", "dry_run": False,
        "path": path, "http_method": "GET", "verified": False, "command_attempted": False,
        "automatic_retry": False, "automatic_rollback": False, "reconciliation_required": False,
        "hardware_wake_may_be_required": action == "low_power_standby",
    }
    attempted, backup_ref = False, ""
    code = "POWER_PREFLIGHT_FAILED"
    try:
        guard(db, device)  # This must precede even construction of the transport.
        async with device_transaction([identity]):
            def unchanged() -> None:
                db.refresh(device)
                if device_fingerprint(device) != frozen:
                    raise IdentityMismatch("stored target changed")
                guard(db, device)

            unchanged()
            _root(frozen[4], "info", identity)
            verify_settings_identity(device, frozen[4])
            client = client_factory(address, device_id=identity, request_purpose="power_action", trigger="manual")

            async def observe() -> RebootSnapshot:
                unchanged()
                raw = {"/info": await client.get_xml("/info")}
                _root(raw["/info"], "info", identity)
                verify_settings_identity(device, raw["/info"])
                for endpoint in PATHS:
                    unchanged()
                    raw[endpoint] = await client.get_xml(endpoint)
                unchanged()
                snapshot = RebootSnapshot.parse(raw, identity)
                if not snapshot.zone.standalone(identity):
                    raise ValueError("power operation requires a standalone radio")
                return snapshot

            before = await observe()
            serialized = json.dumps({"schema": 1, "action": action, "raw": before.raw}, sort_keys=True)
            digest = sha256(serialized.encode()).hexdigest()
            backup = ConfigBackup(device_id=identity, path="http8090:power-start", content=serialized)
            db.add(backup)
            db.flush()
            backup_id = backup.id
            backup_ref = f"config-backup:{backup_id}:sha256:{digest}"
            result["backup_ref"] = backup_ref

            def ledger(phase: str, *, verified: bool = False) -> None:
                record_action(db, job_id=result["job_id"], device_id=identity, ip_address=address,
                    action="power_action", trigger="manual", phase=phase, requested_state={"action": action},
                    backup_ref=backup_ref, rollback_ref=backup_ref, verified=verified,
                    result="VERIFIED" if verified else "PENDING_READBACK")
                db.commit()

            def integrity() -> None:
                unchanged()
                stored = db.get(ConfigBackup, backup_id, populate_existing=True)
                if stored is None or stored.device_id != identity or sha256(stored.content.encode()).hexdigest() != digest:
                    raise ValueError("power START missing or changed")

            ledger("POWER_START")
            integrity()
            code = "WRITE_PRECONDITION_FAILED"
            if not before.same_state(await observe()):
                raise ValueError("state changed while START persisted")
            # Cancel an already queued opt-in reconnect before an explicit stop.
            # Sharing device ownership prevents that worker from writing midway.
            cancel(db, identity)
            ledger("POWER_WRITE_INTENT")
            integrity()
            if not before.same_state(await observe()):
                raise ValueError("state changed while intent persisted")
            integrity()
            lost_reply = False
            attempted = result["command_attempted"] = True
            code = "POWER_OUTCOME_UNKNOWN"
            try:
                # SoundTouchClient.get_xml performs exactly one request and no
                # redirects/retries. Do not substitute a retrying read helper.
                await client.get_xml(path)
            except Exception:
                lost_reply = True
            result["response_received"] = not lost_reply
            observed = None
            try:
                observed = await observe()
                result["last_observation"] = {
                    "source": observed.selection.source, "play_status": observed.selection.play_status,
                    "volume": observed.volume[0], "muted": observed.volume[1],
                }
            except IdentityMismatch:
                raise
            except Exception:
                result["readback_unavailable"] = True
            if lost_reply or action == "low_power_standby":
                # Even a returned STANDBY state is not evidence of deep sleep.
                result.update(ok=False, status="PENDING_READBACK", reconciliation_required=True,
                    code="POWER_OUTCOME_UNKNOWN" if lost_reply else "LOW_POWER_NOT_VERIFIED",
                    message="Power state is not confirmed. No retry or wake was sent; manual readback may require physical wake.")
                integrity()
                ledger("POWER_PENDING_READBACK")
                return result
            code = "POWER_READBACK_MISMATCH"
            if (observed is None or observed.selection.source != "STANDBY"
                    or observed.selection.play_status != "STOP_STATE"
                    or (observed.volume, observed.zone, observed.presets) != (before.volume, before.zone, before.presets)):
                raise ValueError("standby or preserved state not confirmed")
            integrity()
            code = "POWER_RESULT_COMMIT_FAILED"
            close_open_sessions(db, identity, reason="manual_stop", device_last_seen=device.last_seen)
            ledger("POWER_COMPLETED", verified=True)
            return {**result, "ok": True, "verified": True, "status": "VERIFIED"}
    except (Exception, asyncio.CancelledError) as exc:
        db.rollback()
        if isinstance(exc, IdentityMismatch):
            code = "IDENTITY_MISMATCH"
        detail = {**result, "ok": False, "verified": False, "code": code,
                  "status": "DIVERGED" if attempted else "BLOCKED", "reconciliation_required": attempted,
                  "message": "Power state not confirmed. No automatic retry or rollback; read the radio state again."}
        record_transport_attempt(device_id=identity, ip_address=address, action="power_action", trigger="manual",
            phase=detail["status"], result=detail["status"], job_id=result["job_id"], backup_ref=backup_ref,
            rollback_ref=backup_ref, error_category=code, verified=False)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise PowerActionError(detail) from None
