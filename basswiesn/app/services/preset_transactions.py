"""Durable, revisioned preset mutation state machine."""

from __future__ import annotations

from datetime import datetime, UTC
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from basswiesn.app.models import ConfigBackup, Preset, PresetMutation, utc_now


ACTIVE_DELETE_STATES = frozenset({"RADIO_WRITE", "RADIO_READBACK"})
TRANSITIONS = {
    "PREPARED": {"RADIO_WRITE", "FAILED"},
    "RADIO_WRITE": {"RADIO_READBACK", "FAILED", "RECONCILE"},
    "RADIO_READBACK": {"VERIFIED", "FAILED", "RECONCILE"},
    "VERIFIED": {"LOCAL_COMMIT", "RECONCILE"},
    "FAILED": {"RECONCILE", "ROLLBACK"},
    "RECONCILE": {"VERIFIED", "ROLLBACK", "FAILED"},
    "LOCAL_COMMIT": set(),
    "ROLLBACK": set(),
}


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def preset_snapshot(preset: Preset | None) -> dict[str, Any] | None:
    if preset is None:
        return None
    return {
        "button": preset.button,
        "station_id": preset.station_id,
        "source": preset.source,
        "source_account": preset.source_account,
        "location": preset.location,
        "content_item_xml": preset.content_item_xml,
    }


def original_preset_intent(db: Session, row: PresetMutation) -> dict[str, Any]:
    records = db.query(ConfigBackup).filter_by(device_id=row.device_id,
        path=f"preset-intent/{row.mutation_id}.json").populate_existing().all()
    if len(records) != 1:
        raise ValueError("original preset intent missing or ambiguous")
    value = json.loads(records[0].content)
    if not isinstance(value, dict) or _digest(value) != row.requested_sha256:
        raise ValueError("preset intent integrity failed")
    return value


def prepare_preset_mutation(
    db: Session,
    *,
    device_id: str,
    button: int,
    operation: str,
    requested_state: Any,
) -> PresetMutation:
    previous = db.query(Preset).filter(
        Preset.device_id == device_id, Preset.button == button
    ).one_or_none()
    latest = db.query(func.max(PresetMutation.revision)).filter(
        PresetMutation.device_id == device_id,
        PresetMutation.button == button,
    ).scalar()
    row = PresetMutation(
        mutation_id=uuid4().hex,
        device_id=device_id,
        button=button,
        operation=str(operation).upper(),
        state="PREPARED",
        revision=int(latest or 0) + 1,
        expected_previous_sha256=_digest(preset_snapshot(previous)),
        requested_sha256=_digest(requested_state),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db.add(row)
    # A digest alone cannot reconstruct a failed request from tomorrow's catalog.
    # Keep exact private intent in the existing backup store; do not log it.
    db.add(ConfigBackup(device_id=device_id, path=f"preset-intent/{row.mutation_id}.json",
        content=json.dumps(requested_state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
    db.commit()
    db.refresh(row)
    return row


def transition_preset_mutation(
    db: Session,
    mutation: PresetMutation,
    state: str,
    *,
    before_radio: str | None = None,
    after_radio: str | None = None,
    backup_ref: str | None = None,
    error: str | None = None,
    diverged: bool | None = None,
    commit: bool = True,
) -> PresetMutation:
    target = str(state).upper()
    current = str(mutation.state).upper()
    if target != current and target not in TRANSITIONS.get(current, set()):
        raise ValueError(f"invalid preset mutation transition {current} -> {target}")
    mutation.state = target
    if before_radio is not None:
        mutation.before_radio_sha256 = sha256(before_radio.encode("utf-8")).hexdigest()
    if after_radio is not None:
        mutation.after_radio_sha256 = sha256(after_radio.encode("utf-8")).hexdigest()
    if backup_ref is not None:
        mutation.backup_ref = backup_ref
    if error is not None:
        mutation.error = str(error)[:2048]
    if diverged is not None:
        mutation.diverged = diverged
    mutation.updated_at = datetime.now(UTC)
    if commit:
        db.commit()
    else:
        db.flush()
    return mutation


def active_delete_buttons(db: Session, device_id: str) -> set[int]:
    rows = db.query(PresetMutation.button).filter(
        PresetMutation.device_id == device_id,
        PresetMutation.operation == "DELETE",
        PresetMutation.state.in_(ACTIVE_DELETE_STATES),
    ).all()
    return {int(button) for (button,) in rows}
