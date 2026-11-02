"""Pure interpretation of radio readback; no transport, scheduling or writes.

An advertised source, a PLAY_STATE tag or a missing position is not evidence
of audible output. Missing metadata and explicitly cleared metadata likewise
have different meanings. Keep these decisions independent of recovery policy.
"""
from collections.abc import Mapping
from datetime import UTC, datetime
import math


def parse_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def source(runtime: Mapping[str, object]) -> str:
    return str(runtime.get("current_source") or "").upper()


def playback_state(runtime: Mapping[str, object]) -> str:
    return str(runtime.get("playback_state") or "").upper()


def _position(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = float(str(value))
        return int(number) if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def explicit_position(runtime: Mapping[str, object]) -> int | None:
    raw = runtime.get("now_playing")
    if not isinstance(raw, dict):
        return None
    folded = {
        str(key).rsplit("}", 1)[-1].strip().casefold(): value
        for key, value in raw.items()
    }
    for name in ("position", "positionms", "elapsedtime", "elapsedtimems"):
        value = _position(folded.get(name))
        if value is not None:
            return value
    return None


def progress_evidence(
    keepalive: Mapping[str, object], runtime: Mapping[str, object], now: datetime,
) -> tuple[bool | None, float | None, dict[str, object]]:
    status = playback_state(runtime)
    position = explicit_position(runtime)
    if status in {"BUFFERING", "BUFFERING_STATE"}:
        started = parse_iso(keepalive.get("buffering_since")) or now
        return None, max(0.0, (now - started).total_seconds()), {
            "buffering_since": started.isoformat(), "explicit_position": position,
        }
    if status not in {"PLAYING", "PLAY_STATE"} or position is None:
        return None, None, {"buffering_since": "", "explicit_position": position}
    previous = _position(keepalive.get("explicit_position"))
    previous_at = parse_iso(keepalive.get("position_observed_at"))
    if previous is None or previous_at is None or previous != position:
        return True, 0.0, {
            "buffering_since": "", "explicit_position": position,
            "position_observed_at": now.isoformat(), "position_stagnant_since": "",
        }
    stagnant = parse_iso(keepalive.get("position_stagnant_since")) or previous_at
    return False, max(0.0, (now - stagnant).total_seconds()), {
        "buffering_since": "", "explicit_position": position,
        "position_observed_at": previous_at.isoformat(),
        "position_stagnant_since": stagnant.isoformat(),
    }


def first_text(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def radio_metadata_observation(runtime: Mapping[str, object]) -> dict[str, object] | None:
    """Normalize present fields, without mistaking station identity for a song.

    A present empty XML field clears the corresponding value; an absent field
    must not clear it. Firmware paths vary in casing and XML namespaces.
    """
    raw = runtime.get("now_playing")
    if not isinstance(raw, dict):
        return None
    fields = {
        str(key).rsplit("}", 1)[-1].strip().casefold(): value
        for key, value in raw.items()
    }
    aliases = {
        "track": ("track", "title"), "artist": ("artist",),
        "album": ("album",), "imageUrl": ("imageurl", "arturl", "art"),
    }
    result: dict[str, object] = {}
    for target, candidates in aliases.items():
        for candidate in candidates:
            if candidate in fields:
                result[target] = str(fields[candidate] or "").strip() or None
                break
    return result or None
