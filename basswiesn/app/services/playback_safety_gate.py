"""Cross-process safety gate between WebUI selection and the local provider.

Firmware can restore a remembered source volume while processing ``/select``.
The local BMX/Orion endpoint is the last point before the radio receives an
audio URL.  A human-triggered safe playback therefore arms a short-lived gate;
the provider withholds its response until volume 1 and mute have been read back
after selection.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json

from sqlalchemy.orm import Session

from basswiesn.app.models import RuntimeState, utc_now


GATE_TTL_SECONDS = 30.0
PROVIDER_WAIT_SECONDS = 5.0
PROVIDER_POLL_SECONDS = 0.05


class PlaybackSafetyGateError(RuntimeError):
    pass


def _key(device_id: str) -> str:
    return f"device:{str(device_id or '').strip().upper()}:playback_safety_gate"


def _decode(value: object) -> dict:
    try:
        result = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _write(db: Session, device_id: str, payload: dict) -> dict:
    row = db.query(RuntimeState).filter(RuntimeState.key == _key(device_id)).one_or_none()
    if row is None:
        row = RuntimeState(key=_key(device_id))
        db.add(row)
    value = {
        "device_id": str(device_id or "").strip().upper(),
        "updated_at": datetime.now(UTC).isoformat(),
        **payload,
    }
    row.value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    row.updated_at = utc_now()
    db.commit()
    return value


def arm_playback_safety_gate(
    db: Session,
    device_id: str,
    *,
    safe_volume: int,
    station_id: int,
) -> dict:
    return _write(
        db,
        device_id,
        {
            "state": "ARMED",
            "safe_volume": int(safe_volume),
            "station_id": int(station_id),
            "mute_required": True,
            "reason": "waiting for post-select volume and mute readback",
        },
    )


def verify_playback_safety_gate(db: Session, device_id: str, *, volume: int, muted: bool) -> dict:
    if int(volume) != 1 or muted is not True:
        raise PlaybackSafetyGateError("volume 1 and mute are required before provider release")
    return _write(
        db,
        device_id,
        {
            "state": "VERIFIED",
            "safe_volume": 1,
            "mute_required": True,
            "volume_readback": int(volume),
            "mute_readback": bool(muted),
            "reason": "post-select safety readback verified",
        },
    )


def fail_playback_safety_gate(db: Session, device_id: str, reason: str) -> dict:
    return _write(
        db,
        device_id,
        {
            "state": "FAILED",
            "safe_volume": 1,
            "mute_required": True,
            "reason": str(reason or "playback safety verification failed")[:500],
        },
    )


def clear_playback_safety_gate(db: Session, device_id: str) -> None:
    row = db.query(RuntimeState).filter(RuntimeState.key == _key(device_id)).one_or_none()
    if row is not None:
        db.delete(row)
        db.commit()


def load_playback_safety_gate(db: Session, device_id: str) -> dict:
    db.expire_all()
    row = db.query(RuntimeState).filter(RuntimeState.key == _key(device_id)).one_or_none()
    if row is None:
        return {}
    value = _decode(row.value)
    observed = value.get("updated_at")
    try:
        updated = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - updated.astimezone(UTC)).total_seconds()
    except (TypeError, ValueError):
        age = GATE_TTL_SECONDS + 1
    return {**value, "age_seconds": max(0.0, age), "expired": age > GATE_TTL_SECONDS}


async def wait_for_provider_release(db: Session, device_id: str) -> dict:
    """Wait for a currently armed gate; absence means a non-WebUI selection."""

    deadline = asyncio.get_running_loop().time() + PROVIDER_WAIT_SECONDS
    while True:
        gate = load_playback_safety_gate(db, device_id)
        if not gate:
            return {"required": False, "state": "NOT_ARMED"}
        if gate.get("expired"):
            raise PlaybackSafetyGateError("playback safety gate expired before provider release")
        state = str(gate.get("state") or "").upper()
        if state == "VERIFIED":
            return {"required": True, "state": state, "volume_readback": gate.get("volume_readback")}
        if state == "FAILED":
            raise PlaybackSafetyGateError(str(gate.get("reason") or "playback safety gate failed"))
        if asyncio.get_running_loop().time() >= deadline:
            raise PlaybackSafetyGateError("timed out waiting for post-select volume and mute readback")
        await asyncio.sleep(PROVIDER_POLL_SECONDS)
