"""Read-only reconciliation and one-shot removal, not automatic restoration.

Only original hash-bound intent can close an uncertain mutation. Current station
labels, a callback or a matching URL alone cannot prove the requested outcome.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from hashlib import sha256
from typing import Any

from sqlalchemy.orm import Session

from basswiesn.app.models import ConfigBackup, Device, Preset, PresetMutation, Station, utc_now
from basswiesn.app.services.action_journal import record_action, record_transport_attempt
from basswiesn.app.services.device_settings_writer import ClientFactory
from basswiesn.app.services.device_transactions import device_transaction
from basswiesn.app.services.preset_observation import PresetAccess, PresetObservation, local_version, requested_observation
from basswiesn.app.services.preset_transactions import (
    _digest, preset_snapshot, prepare_preset_mutation, transition_preset_mutation,
    original_preset_intent as _intent,
)
from basswiesn.app.services.protected_devices import require_unprotected_device


class PresetRecoveryError(Exception):
    def __init__(self, code: str, *, attempted: bool = False, mutation_id: str = "") -> None:
        self.detail: dict[str, Any] = {"code": code, "verified": False, "local_deleted": False,
            "local_saved": False, "reconciliation_required": attempted, "automatic_retry": False,
            "mutation_id": mutation_id, "status": "DIVERGED" if attempted else "BLOCKED",
            "message": "Preset state was not confirmed. No automatic retry or rollback; read and reconcile actual state."}
        super().__init__(code)


def _latest(db: Session, identity: str, button: int) -> PresetMutation | None:
    return db.query(PresetMutation).filter_by(device_id=identity, button=button).order_by(
        PresetMutation.revision.desc(), PresetMutation.id.desc()).populate_existing().first()


def _start(db: Session, row: PresetMutation) -> PresetObservation:
    records = db.query(ConfigBackup).filter_by(device_id=row.device_id, path=row.backup_ref).populate_existing().all()
    if len(records) != 1 or sha256(records[0].content.encode()).hexdigest() != row.before_radio_sha256:
        raise ValueError("original preset backup missing, changed or ambiguous")
    return PresetObservation.parse(records[0].content, row.device_id)


def _confirmed(db: Session, row: PresetMutation, actual: PresetObservation, local: Preset | None) -> tuple[bool, dict[str, Any]]:
    if row.state not in {"FAILED", "RECONCILE", "RADIO_WRITE", "RADIO_READBACK", "VERIFIED"}:
        return False, {}
    try:
        requested, before = _intent(db, row), _start(db, row)
        if _digest(preset_snapshot(local)) != row.expected_previous_sha256:
            return False, {}
        if before.without(row.button) != actual.without(row.button):
            return False, {}
        if row.operation == "DELETE":
            return (requested.get("deleted") is True and row.button not in actual.slots), requested
        desired = requested_observation(row.button, requested, row.device_id)
        return actual.slots.get(row.button) == desired.slots[row.button], requested
    except (ValueError, TypeError, KeyError):
        # Old hash-only requests cannot be reconstructed from today's catalog.
        return False, {}


def _project(db: Session, identity: str, actual: PresetObservation,
             resolved: dict[int, dict[str, Any]], pending: set[int]) -> None:
    rows = {row.button: row for row in db.query(Preset).filter_by(device_id=identity).all()}
    for item in actual.rows:
        slot = item["button"]
        if slot in pending:
            continue  # Preserve the original local state needed for reconciliation.
        row = rows.pop(slot, None)
        if row is None:
            row = Preset(device_id=identity, button=slot)
            db.add(row)
        if slot in resolved:
            station_id = resolved[slot].get("station_id")
            row.station_id = station_id if station_id and db.get(Station, station_id) else None
        elif (row.source, row.source_account, row.location) != (item["source"], item["source_account"], item["location"]):
            row.station_id = None
        row.source, row.source_account = item["source"], item["source_account"]
        row.location, row.content_item_xml = item["location"], item["content_item_xml"]
        row.updated_at = utc_now()
    for slot, row in rows.items():
        if slot not in pending:
            db.delete(row)


async def download_presets(db: Session, device: Device, *, client_factory: ClientFactory) -> dict[str, Any]:
    try:
        async with device_transaction([device.device_id]):
            def guard(session: Session, target: Device) -> None:
                require_unprotected_device(target, action="preset_readback", requester="preset_recovery")
            access = PresetAccess(db, device, client_factory=client_factory, guard=guard, write=False)
            expected = local_version(db, access.identity)
            before = await access.observe()
            await asyncio.sleep(0.5)
            actual = await access.observe()
            if before.slots != actual.slots:
                raise ValueError("unstable preset observation")
            access.commit_window(expected)
            rows = {row.button: row for row in db.query(Preset).filter_by(device_id=access.identity).all()}
            resolved: dict[int, dict[str, Any]] = {}
            pending: set[int] = set()
            reconciled = []
            for slot in range(1, 7):
                mutation = _latest(db, access.identity, slot)
                if mutation is None or mutation.state in {"LOCAL_COMMIT", "ROLLBACK"}:
                    continue
                matched, intent = _confirmed(db, mutation, actual, rows.get(slot))
                if not matched:
                    pending.add(slot)
                    continue
                if mutation.state == "RADIO_WRITE":
                    transition_preset_mutation(db, mutation, "RADIO_READBACK", commit=False)
                if mutation.state == "FAILED":
                    transition_preset_mutation(db, mutation, "RECONCILE", commit=False)
                transition_preset_mutation(db, mutation, "VERIFIED", after_radio=actual.raw, diverged=False, commit=False)
                transition_preset_mutation(db, mutation, "LOCAL_COMMIT", commit=False)
                resolved[slot] = intent
                reconciled.append({"button": slot, "mutation_id": mutation.mutation_id, "revision": mutation.revision})
                record_action(db, job_id=mutation.mutation_id, device_id=access.identity, ip_address=access.frozen[1], action="preset_reconcile",
                    trigger="explicit_download", phase="VERIFIED", backup_ref=mutation.backup_ref,
                    result="radio_readback_verified", verified=True,
                    readback={"presets_sha256": sha256(actual.raw.encode()).hexdigest()})
            _project(db, access.identity, actual, resolved, pending)
            db.add(ConfigBackup(device_id=access.identity, path="/presets", content=actual.raw))
            db.commit()
            return {"dry_run": False, "device_id": access.identity, "count": len(actual.rows), "presets": actual.rows,
                "reconciled_mutations": reconciled, "pending_slots": sorted(pending),
                "observed_state_verified": True, "all_mutations_reconciled": not pending}
    except (Exception, asyncio.CancelledError) as exc:
        db.rollback()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise PresetRecoveryError("PRESET_READBACK_NOT_CONFIRMED") from None


async def remove_preset(db: Session, device: Device, button: int, *, client_factory: ClientFactory,
                        guard: Callable[[Session, Device], None], expected_revision: int | None = None) -> dict[str, Any]:
    attempted, mutation_id = False, ""
    identity = device.device_id
    if type(button) is not int or button not in range(1, 7):
        raise PresetRecoveryError("INVALID_PRESET_SLOT")
    try:
        async with device_transaction([identity]):
            access = PresetAccess(db, device, client_factory=client_factory, guard=guard, write=True)
            latest = _latest(db, identity, button)
            if latest and latest.state not in {"LOCAL_COMMIT", "ROLLBACK"} and not (
                    latest.state == "FAILED" and not latest.diverged):
                raise ValueError("reconcile the outstanding mutation first")
            if expected_revision is not None and expected_revision != (latest.revision if latest else 0):
                raise ValueError("requested revision is stale")
            local = db.query(Preset).filter_by(device_id=identity, button=button).one()
            expected = local_version(db, identity)
            before = await access.observe()
            local_intent = {"content_item_xml": local.content_item_xml}
            if before.slots.get(button) != requested_observation(button, local_intent, identity).slots[button]:
                raise ValueError("radio slot differs from local selection; read back first")
            access.commit_window(expected)
            mutation = prepare_preset_mutation(db, device_id=identity, button=button, operation="DELETE",
                requested_state={"button": button, "deleted": True})
            mutation_id = mutation.mutation_id
            owned = {mutation_id}
            if local_version(db, identity, exclude_mutations=owned) != expected:
                raise ValueError("preset changed while intent persisted")
            backup_ref = f"preset-delete/{mutation_id}/before.xml"
            db.add(ConfigBackup(device_id=identity, path=backup_ref, content=before.raw))
            transition_preset_mutation(db, mutation, "RADIO_WRITE", before_radio=before.raw, backup_ref=backup_ref)
            anchor = (mutation.requested_sha256, mutation.expected_previous_sha256,
                      mutation.before_radio_sha256, mutation.backup_ref, mutation.revision)
            def check() -> None:
                access.unchanged()
                if local_version(db, identity, exclude_mutations=owned) != expected:
                    raise ValueError("preset state/revision changed during command")
                db.refresh(mutation)
                if anchor != (mutation.requested_sha256, mutation.expected_previous_sha256,
                              mutation.before_radio_sha256, mutation.backup_ref, mutation.revision):
                    raise ValueError("original mutation changed")
                _intent(db, mutation)
                if _start(db, mutation).slots != before.slots:
                    raise ValueError("START changed")
            if (await access.observe()).slots != before.slots:
                raise ValueError("radio presets changed while intent persisted")
            check()
            attempted = True
            response_received = True
            try:
                await access.client.post_xml("/removePreset", f'<preset id="{button}"></preset>')
            except Exception:
                response_received = False  # A lost response is not permission to replay.
            check()
            transition_preset_mutation(db, mutation, "RADIO_READBACK")
            actual = before
            stable_reads = 0
            for _ in range(12):
                await asyncio.sleep(0.5)
                check()
                actual = await access.observe()
                if actual.without(button) != before.without(button):
                    raise ValueError("unrequested preset changed")
                stable_reads = stable_reads + 1 if button not in actual.slots else 0
                if stable_reads == 2:
                    break
            if stable_reads != 2:
                raise ValueError("radio did not confirm stable deletion")
            check()
            access.commit_window(expected, exclude_mutations=owned)
            check()
            local = db.query(Preset).filter_by(device_id=identity, button=button).one()
            transition_preset_mutation(db, mutation, "VERIFIED", after_radio=actual.raw, diverged=False, commit=False)
            db.delete(local)
            transition_preset_mutation(db, mutation, "LOCAL_COMMIT", commit=False)
            record_action(db, job_id=mutation_id, device_id=identity, ip_address=access.frozen[1],
                action="preset_delete", trigger="stations_presets", phase="VERIFIED", backup_ref=backup_ref,
                rollback_ref=backup_ref, requested_state={"button": button, "deleted": True},
                result="radio_readback_verified", verified=True,
                readback={"presets_sha256": sha256(actual.raw.encode()).hexdigest(), "slot_absent": True,
                          "untouched_slots_verified": True, "stable_readbacks": stable_reads})
            db.commit()
            return {"device_id": identity, "button": button, "deleted": True, "verified": True,
                "mutation_id": mutation_id, "revision": mutation.revision, "response_received": response_received}
    except (Exception, asyncio.CancelledError) as exc:
        db.rollback()
        try:
            row = db.query(PresetMutation).filter_by(mutation_id=mutation_id).one_or_none() if mutation_id else None
            if row and row.state not in {"LOCAL_COMMIT", "ROLLBACK"}:
                transition_preset_mutation(db, row, "RECONCILE" if attempted else "FAILED",
                    error="PRESET_DELETE_NOT_CONFIRMED", diverged=attempted)
            record_transport_attempt(device_id=identity, ip_address=device.ip_address, action="preset_delete", trigger="stations_presets",
                job_id=mutation_id, phase="RECONCILE" if attempted else "BLOCKED", verified=False,
                result="radio_readback_not_verified", backup_ref=row.backup_ref if row else "",
                rollback_ref=row.backup_ref if row else "", error_category="PRESET_DELETE_NOT_CONFIRMED")
        except Exception:
            db.rollback()  # Original durable intent remains when the DB is unavailable.
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise PresetRecoveryError("PRESET_DELETE_NOT_CONFIRMED", attempted=attempted, mutation_id=mutation_id) from None
