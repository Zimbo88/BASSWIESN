"""Persistent, fail-closed audio safety state for setup hardware tests."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json

from sqlalchemy.orm import Session

from basswiesn.app.models import RuntimeState, SetupRebuildDeviceState, utc_now


def _key(device_id: str) -> str:
    return f"device:{str(device_id or '').strip().upper()}:audio_safety"


def _payload(value: object) -> dict:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class AudioSafetyState:
    locked: bool = False
    reason: str = ""
    source: str = "none"
    observed_at: str = ""
    recovery: str = ""
    volume_limit: int = 1

    def public_dict(self) -> dict:
        return asdict(self)


def load_audio_safety(db: Session, device_id: str) -> AudioSafetyState:
    """Read the explicit state or derive a legacy lock without network I/O.

    An explicit state is authoritative.  Before that state existed, setup-job
    evidence was the only persistent safety record, so any historical lock is
    inherited fail-closed until a human runs the dedicated verification.
    """

    normalized = str(device_id or "").strip().upper()
    state_row = db.query(RuntimeState).filter(RuntimeState.key == _key(normalized)).one_or_none()
    if state_row is not None:
        value = _payload(state_row.value)
        if not value:
            return AudioSafetyState(
                locked=True,
                reason="Persistenter Audio-Sicherheitszustand ist unlesbar.",
                source="runtime_state_malformed",
                observed_at=state_row.updated_at.isoformat() if state_row.updated_at else "",
                recovery="STOP/STANDBY und Lautstärke-1-Readback erneut bestätigen",
            )
        return AudioSafetyState(
            locked=bool(value.get("locked", True)),
            reason=str(value.get("reason") or ""),
            source=str(value.get("source") or "runtime_state"),
            observed_at=str(value.get("observed_at") or ""),
            recovery=str(value.get("recovery") or ""),
            volume_limit=1,
        )

    rows = (
        db.query(SetupRebuildDeviceState)
        .filter(SetupRebuildDeviceState.device_id == normalized)
        .order_by(SetupRebuildDeviceState.updated_at.desc(), SetupRebuildDeviceState.id.desc())
        .all()
    )
    for row in rows:
        evidence = _payload(row.evidence_json)
        if evidence.get("audio_test_locked"):
            return AudioSafetyState(
                locked=True,
                reason=str(evidence.get("audio_lock_reason") or row.last_error or "Früherer Audio-Sicherheitstest wurde gesperrt."),
                source="legacy_setup_evidence",
                observed_at=row.updated_at.isoformat() if row.updated_at else "",
                recovery=str(evidence.get("audio_lock_recovery") or "STOP/STANDBY und Lautstärke-1-Readback erneut bestätigen"),
            )
    return AudioSafetyState()


def save_audio_safety(
    db: Session,
    device_id: str,
    *,
    locked: bool,
    reason: str,
    source: str,
    recovery: str,
) -> AudioSafetyState:
    normalized = str(device_id or "").strip().upper()
    now = datetime.now(UTC).isoformat()
    state = AudioSafetyState(
        locked=bool(locked),
        reason=str(reason)[:500],
        source=str(source)[:80],
        observed_at=now,
        recovery=str(recovery)[:500],
    )
    row = db.query(RuntimeState).filter(RuntimeState.key == _key(normalized)).one_or_none()
    if row is None:
        row = RuntimeState(key=_key(normalized))
        db.add(row)
    row.value = json.dumps(state.public_dict(), ensure_ascii=False, sort_keys=True)
    row.updated_at = utc_now()
    db.commit()
    return state


def lock_audio_safety(db: Session, device_id: str, reason: str) -> AudioSafetyState:
    return save_audio_safety(
        db,
        device_id,
        locked=True,
        reason=reason,
        source="setup_playback_guard",
        recovery="Explizite UI-Prüfung: Identität, STOP/STANDBY und Lautstärke-1-Readback",
    )


def clear_audio_safety(db: Session, device_id: str, reason: str) -> AudioSafetyState:
    return save_audio_safety(
        db,
        device_id,
        locked=False,
        reason=reason,
        source="human_safety_verification",
        recovery="",
    )
