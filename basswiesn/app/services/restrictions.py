"""BMX Restrictions parsing without invented firmware defaults.

Phase 12 recovered exactly one ``BMX.Restrictions`` field:
``inactivityTimeout`` (optional uint64 seconds).  Missing and explicit zero are
both disabled, but remain observably different states.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
from typing import Any, Mapping


UINT64_MAX = (1 << 64) - 1


class RestrictionParseError(ValueError):
    """Raised when a provider response carries an invalid restriction value."""


@dataclass(frozen=True)
class ParsedRestrictions:
    inactivity_timeout_s: int | None
    timer_enabled: bool
    received_at: datetime
    source: str
    origin: str
    timer_started_at: datetime | None
    effective_until: datetime | None
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["received_at"] = self.received_at.isoformat()
        value["timer_started_at"] = (
            self.timer_started_at.isoformat() if self.timer_started_at else None
        )
        value["effective_until"] = (
            self.effective_until.isoformat() if self.effective_until else None
        )
        return value


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decode(payload: Any) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="strict")
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RestrictionParseError("malformed provider response") from exc
        if isinstance(decoded, Mapping):
            return decoded
    raise RestrictionParseError("provider response must be a JSON object")


def _find_restrictions(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, str]:
    direct = payload.get("restrictions")
    if isinstance(direct, Mapping):
        return direct, "SERVER_RESPONSE"
    if direct is not None:
        raise RestrictionParseError("restrictions must be an object")

    # TrackList responses may carry Restrictions below an embedded object.  We
    # intentionally inspect only known structural wrappers, not arbitrary
    # recursive user data.
    for wrapper_name in ("station", "trackList", "tracklist", "_embedded"):
        wrapper = payload.get(wrapper_name)
        if not isinstance(wrapper, Mapping):
            continue
        nested = wrapper.get("restrictions")
        if isinstance(nested, Mapping):
            return nested, "SERVER_RESPONSE"
        if nested is not None:
            raise RestrictionParseError("restrictions must be an object")
    return None, "ABSENT"


def _parse_uint64(value: Any) -> int:
    # bool is an int subclass and must not silently become 0/1.
    if isinstance(value, bool):
        raise RestrictionParseError("inactivityTimeout must be uint64 seconds")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip(), 10)
    else:
        raise RestrictionParseError("inactivityTimeout must be uint64 seconds")
    if parsed < 0 or parsed > UINT64_MAX:
        raise RestrictionParseError("inactivityTimeout is outside uint64 range")
    return parsed


def deadline_from_play(play_started_at: datetime, seconds: int) -> datetime | None:
    """Project a timeout from an authoritative Play/reset observation."""

    if seconds <= 0:
        return None
    try:
        return _utc(play_started_at) + timedelta(seconds=seconds)
    except OverflowError:
        # Preserve the provider value even when Python's datetime range cannot
        # represent the projected deadline.
        return None


def parse_restrictions(
    payload: Any,
    *,
    received_at: datetime | None = None,
    source: str = "provider",
) -> ParsedRestrictions:
    """Parse a Station/TrackList response into the normalized contract.

    ``None`` is never accepted as a shorthand response because a malformed
    provider response and a valid response without Restrictions are different
    diagnostics.
    """

    decoded = _decode(payload)
    restrictions, origin = _find_restrictions(decoded)
    observed_at = _utc(received_at)
    if restrictions is None or "inactivityTimeout" not in restrictions:
        return ParsedRestrictions(
            inactivity_timeout_s=None,
            timer_enabled=False,
            received_at=observed_at,
            source=source,
            origin="ABSENT",
            timer_started_at=None,
            effective_until=None,
            evidence={"contract": "BMX.Restrictions", "field_present": False},
        )

    timeout = _parse_uint64(restrictions.get("inactivityTimeout"))
    return ParsedRestrictions(
        inactivity_timeout_s=timeout,
        timer_enabled=timeout > 0,
        received_at=observed_at,
        source=source,
        origin=origin,
        # A provider response configures the timeout but is not the firmware
        # Play event that resets/starts it. The playback collector binds the
        # deadline after authoritative radio readback.
        timer_started_at=None,
        effective_until=None,
        evidence={
            "contract": "BMX.Restrictions",
            "field_present": True,
            "value_kind": "uint64_seconds",
        },
    )
