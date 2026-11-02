"""Preset write orchestration: backup, single write, whole-state and stability readback.

Dependencies are explicit ports so the service never imports HTTP routers. The
adapter alone translates domain failures into the legacy HTTP response shape.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from basswiesn.app.models import ConfigBackup, Device, Preset, PresetMutation, utc_now
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.services.action_journal import record_action
from basswiesn.app.services.device_transactions import device_transaction
from basswiesn.app.services.preset_observation import PresetAccess, PresetObservation, local_version, requested_observation
from basswiesn.app.services.preset_transactions import prepare_preset_mutation, transition_preset_mutation, original_preset_intent
from basswiesn.app.services.preset_contract import (
    PresetSyncResult, preset_summaries_from_xml, compare_preset_snapshots,
    classify_notification_sequence, _preset_projection, _preset_contract_matches,
)


class PresetTransport(Protocol):
    async def get_xml(self, path: str) -> str: ...
    async def post_xml(self, path: str, xml: str) -> str: ...


@dataclass(frozen=True)
class PresetWritePorts:
    guard: Callable[[Session, Device], None]
    client: Callable[[Device], PresetTransport]
    expected_summary: Callable[[Session, str, int, str], dict[str, Any]]
    effective_item: Callable[[Session, str, Preset, str], str]


class PresetWriteFailure(Exception):
    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(str(detail.get("error", "Preset readback failed")))


def _xml_preview(value: str, limit: int = 600) -> str:
    return " ".join((value or "").split())[:limit]


_radio_response_preview = _xml_preview


async def write_presets(
    device: Device,
    expected: dict[int, str],
    db: Session,
    backup_label: str = "preset-sync",
    *,
    ports: PresetWritePorts,
    prepared_presets: dict[int, dict[str, Any]] | None = None,
    mutations: dict[int, PresetMutation] | None = None,
    expected_before: PresetObservation | None = None,
    precondition: Callable[[], None] | None = None,
) -> PresetSyncResult:
    """Write selected slots and require stable full-radio readback.

    ``presetsUpdated`` is a radio-owned event.  A real factory-fresh Portable
    showed that POSTing a synthetic notification can asynchronously restore
    the previous (empty) preset snapshot after an initially successful
    ``/storePreset`` readback.  The client therefore never injects that event.
    """
    async with device_transaction([device.device_id]):
        def factory(ip_address: str, *, device_id: str, request_purpose: str, trigger: str) -> PresetTransport:
            return ports.client(device)
        try:
            access = PresetAccess(db, device, client_factory=factory, guard=ports.guard, write=True)
            owned_mutations = {row.mutation_id for row in (mutations or {}).values()}
            def intent_key(row: PresetMutation) -> tuple[Any, ...]:
                return (row.device_id, row.button, row.operation, row.revision,
                        row.requested_sha256, row.expected_previous_sha256)
            original_keys = {row.mutation_id: intent_key(row) for row in (mutations or {}).values()}
            local_expected = local_version(db, device.device_id, exclude_mutations=owned_mutations)
            if precondition:
                precondition()
            before = await access.observe()
            if expected_before is not None and before.slots != expected_before.slots:
                raise ValueError("radio changed after artwork plan")
            before_xml = before.raw
            if not expected or any(type(button) is not int or button not in range(1, 7) for button in expected):
                raise ValueError("invalid preset slots")
        except Exception:
            raise PresetWriteFailure({"error": "preset identity or START precondition failed", "local_saved": False}) from None
        client = access.client
        backup_ref = f"{backup_label}/{uuid4().hex}/before.xml"
        backup = ConfigBackup(device_id=device.device_id, path=backup_ref, content=before_xml)
        db.add(backup)
        db.commit()
        backup_id, backup_hash = backup.id, sha256(before_xml.encode()).hexdigest()
        for mutation in (mutations or {}).values():
            transition_preset_mutation(
                db,
                mutation,
                "RADIO_WRITE",
                before_radio=before_xml,
                backup_ref=backup_ref,
            )
        def check() -> None:
            access.unchanged()
            if precondition:
                precondition()
            for mutation in (mutations or {}).values():
                db.refresh(mutation)
                if intent_key(mutation) != original_keys[mutation.mutation_id]:
                    raise PresetWriteFailure({"error": "preset original intent changed", "local_saved": False})
                try:
                    original_preset_intent(db, mutation)
                except ValueError:
                    raise PresetWriteFailure({"error": "preset intent integrity failed", "local_saved": False}) from None
            if local_version(db, device.device_id, exclude_mutations=owned_mutations) != local_expected:
                raise PresetWriteFailure({"error": "preset local state/revision changed", "local_saved": False})
            stored = db.get(ConfigBackup, backup_id, populate_existing=True)
            if (stored is None or stored.device_id != device.device_id or stored.path != backup_ref
                    or sha256(stored.content.encode()).hexdigest() != backup_hash):
                raise PresetWriteFailure({"error": "preset START integrity failed", "local_saved": False})

        check()
        if (await access.observe()).slots != before.slots:
            raise PresetWriteFailure({"error": "radio presets changed while START persisted", "local_saved": False})

        slot_results: list[dict[str, Any]] = [{"button": button, "expected_location": location, "status": "pending"} for button, location in expected.items()]
        expected_items: dict[int, dict[str, Any]] = {}
        allowed_orion_origins = {
            f"{origin.scheme}://{origin.netloc}"
            for location in expected.values()
            if (origin := urlparse(location or "")).scheme and origin.netloc
        }
        for button, location in expected.items():
            prepared = (prepared_presets or {}).get(button)
            if prepared and prepared.get("content_item_xml"):
                parsed = preset_summaries_from_xml(
                    f'<presets><preset id="{button}">{prepared["content_item_xml"]}</preset></presets>'
                )
                expected_items[button] = parsed[0] if parsed else {
                    "button": button,
                    "location": location,
                    "source": prepared.get("source", ""),
                }
            else:
                expected_items[button] = ports.expected_summary(
                    db, device.device_id, button, location
                )
        last_xml = before_xml
        before_items = {
            int(item["button"]): _preset_projection(item)
            for item in preset_summaries_from_xml(before_xml)
        }
        notification_sequence = ["storePreset"]
        transport_uncertainties: list[dict[str, Any]] = []
        for button, location in expected.items():
            check()
            prepared = (prepared_presets or {}).get(button)
            preset = db.query(Preset).filter(Preset.device_id == device.device_id, Preset.button == button).one_or_none()
            content_item = str((prepared or {}).get("content_item_xml") or "")
            if not content_item and preset is not None:
                content_item = ports.effective_item(
                    db, device.device_id, preset, location
                )
            if not content_item:
                continue
            desired = _preset_projection(expected_items.get(button, {}))
            if _preset_contract_matches(before_items.get(int(button)), desired):
                next(item for item in slot_results if item["button"] == button).update({
                    "status": "unchanged",
                    "actual_location": before_items[int(button)].get("location", ""),
                })
                continue
            body = f'<preset id="{button}">{content_item}</preset>'
            station_id = (prepared or {}).get("station_id") or (preset.station_id if preset else None)
            preset_source = (prepared or {}).get("source") or (preset.source if preset else "")
            preset_location = (prepared or {}).get("location") or (preset.location if preset else location)
            try:
                next(item for item in slot_results if item["button"] == button)["status"] = "writing"
                write_masterlog("preset_apply_store_sent", device_id=device.device_id, radio_ip=device.ip_address, button=button, station_id=station_id, source=preset_source, location=preset_location, endpoint="/storePreset", xml_preview=_xml_preview(body))
                await client.post_xml("/storePreset", body)
                next(item for item in slot_results if item["button"] == button)["status"] = "written_waiting_for_readback"
            except Exception as exc:
                if isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError)):
                    # Dispatch may already have succeeded. Stop the remaining
                    # batch and inspect real state; never replay the POST.
                    uncertainty = {"button": button, "error_type": type(exc).__name__}
                    transport_uncertainties.append(uncertainty)
                    next(item for item in slot_results if item["button"] == button).update(
                        {"status": "outcome_unknown", **uncertainty})
                    write_masterlog("preset_reply_uncertain", device_id=device.device_id, **uncertainty)
                    break
                next(item for item in slot_results if item["button"] == button).update({"status": "failed", "error": str(exc) or exc.__class__.__name__})
                status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                radio_response = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else str(exc) or exc.__class__.__name__
                write_masterlog("preset_apply_failed", device_id=device.device_id, radio_ip=device.ip_address, button=button, station_id=station_id, source=preset_source, location=preset_location, endpoint="/storePreset", status_code=status_code, radio_response_preview=_radio_response_preview(radio_response), xml_preview=_xml_preview(body), error=str(exc) or exc.__class__.__name__)
                raise PresetWriteFailure({"error": "radio direct /storePreset failed", "button": button, "message": str(exc), "local_saved": False, "slot_results": slot_results}) from exc
        for mutation in (mutations or {}).values():
            transition_preset_mutation(db, mutation, "RADIO_READBACK")
        integrity: dict[str, Any] = {}
        readback_delays = (0, *([0.5] * 11), 1, 2, 3)
        for attempt, delay in enumerate(readback_delays, start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                check()
                last_xml = (await access.observe()).raw
                radio_rows = preset_summaries_from_xml(last_xml)
                integrity = compare_preset_snapshots(
                    before_xml,
                    last_xml,
                    expected,
                    expected_items=expected_items,
                    allowed_orion_origins=allowed_orion_origins,
                )
                if integrity["target_verified"] and integrity["untouched_slots_verified"] and not integrity["unexpected_changes"]:
                    break
            except (ValueError, PresetWriteFailure):
                raise PresetWriteFailure({"error": "preset identity or readback precondition changed", "local_saved": False}) from None
            except Exception:
                continue
        db.add(ConfigBackup(device_id=device.device_id, path=f"{backup_label}/after-storePreset.xml", content=last_xml))
        db.commit()
        notification_sequence.append("readback")
        integrity["notification_sequence"] = classify_notification_sequence(notification_sequence)
        integrity["transport_uncertainties"] = transport_uncertainties
        if integrity.get("overall_status") != "verified":
            raise PresetWriteFailure({"error": "preset integrity check failed after storePreset", "device_id": device.device_id, "expected_slots": expected, "integrity": integrity, "radio_presets": last_xml, "local_saved": False})

        # A single immediate GET can only prove the radio's in-memory response.
        # Two later reads make asynchronous persistence/cache rollback visible.
        final_xml = last_xml
        final_integrity = integrity
        stability_readbacks: list[dict[str, Any]] = []
        for delay in (0.5, 1.0):
            await asyncio.sleep(delay)
            try:
                check()
                final_xml = (await access.observe()).raw
            except Exception as exc:
                raise PresetWriteFailure({
                    "error": "radio preset stability readback failed",
                    "message": str(exc),
                    "local_saved": False,
                    "integrity": final_integrity,
                }) from exc
            final_integrity = compare_preset_snapshots(
                before_xml,
                final_xml,
                expected,
                expected_items=expected_items,
                allowed_orion_origins=allowed_orion_origins,
            )
            stability_readbacks.append({
                "delay_seconds": delay,
                "presets_sha256": sha256(final_xml.encode("utf-8")).hexdigest(),
                "overall_status": final_integrity.get("overall_status", "unknown"),
            })
            if final_integrity.get("overall_status") != "verified":
                break
        final_integrity["notification_sequence"] = classify_notification_sequence(notification_sequence)
        final_integrity["radio_owned_notification"] = True
        final_integrity["stability_readbacks"] = stability_readbacks
        final_integrity["transport_uncertainties"] = transport_uncertainties
        db.add(ConfigBackup(device_id=device.device_id, path=f"{backup_label}/after-stable-readback.xml", content=final_xml))
        db.commit()
        check()
        radio_rows = preset_summaries_from_xml(final_xml)
        if final_integrity["overall_status"] != "verified":
            raise PresetWriteFailure({"error": "preset integrity check failed during stability window", "device_id": device.device_id, "expected_slots": expected, "integrity": final_integrity, "radio_presets": final_xml, "local_saved": False})
        for item in slot_results:
            actual = next((row for row in radio_rows if row["button"] == item["button"]), {})
            item.update({"status": "unchanged" if item.get("status") == "unchanged" else "verified", "actual_location": actual.get("location", "")})
        final_integrity["overall_status"] = "verified"
        for mutation in (mutations or {}).values():
            transition_preset_mutation(
                db,
                mutation,
                "VERIFIED",
                after_radio=final_xml,
                diverged=False,
            )
        record_action(
            db,
            job_id=next(
                (row.mutation_id for row in (mutations or {}).values()), ""
            ),
            device_id=device.device_id,
            ip_address=device.ip_address,
            action="preset_sync",
            trigger="stations_presets",
            phase="VERIFIED",
            requested_state={"slots": expected},
            backup_ref=backup_ref,
            before_state={"presets_sha256": sha256(before_xml.encode("utf-8")).hexdigest()},
            result="radio_readback_verified",
            readback={
                "presets_sha256": sha256(final_xml.encode("utf-8")).hexdigest(),
                "slots": radio_rows,
                "integrity": final_integrity,
            },
            rollback_ref=backup_ref,
            verified=True,
        )
        db.commit()
        check()
        return PresetSyncResult(radio_rows, final_integrity)


async def apply_prepared_presets(device: Device, prepared: dict[int, dict[str, Any]], db: Session, *,
                                 ports: PresetWritePorts, backup_label: str,
                                 operation: str = "WRITE", expected_before: PresetObservation | None = None,
                                 expected_local: str | None = None,
                                 precondition: Callable[[], None] | None = None) -> PresetSyncResult:
    """One local commit boundary for single, clone, profile and artwork writes."""
    async with device_transaction([device.device_id]):
        ports.guard(db, device)
        if (not prepared and expected_before is None) or any(type(slot) is not int or slot not in range(1, 7) for slot in prepared):
            raise PresetWriteFailure({"error": "invalid or empty preset profile", "local_saved": False})
        previous = local_version(db, device.device_id)
        mutations: dict[int, PresetMutation] = {}
        try:
            if expected_local is not None and previous != expected_local:
                raise ValueError("local presets changed after artwork plan")
            if operation not in {"WRITE", "CLONE", "ARTWORK"}:
                raise ValueError("unsupported preset operation")
            if precondition:
                precondition()
            for slot, intent in prepared.items():
                requested_observation(slot, intent, device.device_id)
            for slot in prepared:
                latest = db.query(PresetMutation).filter_by(device_id=device.device_id, button=slot).order_by(
                    PresetMutation.revision.desc(), PresetMutation.id.desc()).first()
                if latest and latest.state not in {"LOCAL_COMMIT", "ROLLBACK"} and not (
                        latest.state == "FAILED" and not latest.diverged):
                    raise ValueError("unresolved preset intent requires reconciliation")
            def factory(ip_address: str, *, device_id: str, request_purpose: str, trigger: str) -> PresetTransport:
                return ports.client(device)
            access = PresetAccess(db, device, client_factory=factory, guard=ports.guard, write=True)
            if not prepared:
                # A no-op is still an observed-state claim. Never certify a
                # malformed response or a stale plan simply because no POST is due.
                if expected_before is None:
                    raise ValueError("no-op requires an observed START")
                for _ in range(2):
                    actual = await access.observe()
                    if actual.slots != expected_before.slots:
                        raise ValueError("no-op observation changed")
                    await asyncio.sleep(0.5)
                access.commit_window(previous)
                if precondition:
                    precondition()
                db.add(ConfigBackup(device_id=device.device_id, path="/presets", content=actual.raw))
                db.commit()
                return PresetSyncResult(actual.rows, {"overall_status": "verified", "target_verified": True,
                    "untouched_slots_verified": True, "unexpected_changes": [], "slot_results": [],
                    "radio_write_count": 0, "local_projection_changed": False})
            for slot, intent in prepared.items():
                mutations[slot] = prepare_preset_mutation(db, device_id=device.device_id, button=slot,
                    operation=operation, requested_state=intent)
            owned = {row.mutation_id for row in mutations.values()}
            if local_version(db, device.device_id, exclude_mutations=owned) != previous:
                raise ValueError("preset state changed while profile intent persisted")
            result = await write_presets(device, {slot: str(intent["location"]) for slot, intent in prepared.items()},
                db, backup_label, ports=ports, prepared_presets=prepared, mutations=mutations,
                expected_before=expected_before, precondition=precondition)
            access.commit_window(previous, exclude_mutations=owned)
            if precondition:
                precondition()
            for item in result:
                slot = item["button"]
                if slot not in prepared:
                    continue
                row = db.query(Preset).filter_by(device_id=device.device_id, button=slot).one_or_none()
                if row is None:
                    row = Preset(device_id=device.device_id, button=slot)
                    db.add(row)
                row.station_id = prepared[slot].get("station_id")
                row.source, row.source_account = item["source"], item["source_account"]
                row.location, row.content_item_xml = item["location"], item["content_item_xml"]
                row.updated_at = utc_now()
                transition_preset_mutation(db, mutations[slot], "LOCAL_COMMIT", commit=False)
            db.commit()
            return result
        except (Exception, asyncio.CancelledError) as exc:
            db.rollback()
            for mutation in mutations.values():
                try:
                    db.refresh(mutation)
                    if mutation.state not in {"LOCAL_COMMIT", "ROLLBACK"}:
                        transition_preset_mutation(db, mutation,
                            "FAILED" if mutation.state == "PREPARED" else "RECONCILE",
                            diverged=mutation.state != "PREPARED", error="PRESET_APPLY_NOT_CONFIRMED")
                except Exception:
                    db.rollback()
            if isinstance(exc, asyncio.CancelledError):
                raise
            detail = exc.detail if isinstance(exc, PresetWriteFailure) else {}
            raise PresetWriteFailure({"error": "preset apply not confirmed; local rows unchanged",
                "local_saved": False, "verified": False, "reconciliation_required": bool(mutations),
                "integrity": detail.get("integrity", {}), "automatic_retry": False}) from None
