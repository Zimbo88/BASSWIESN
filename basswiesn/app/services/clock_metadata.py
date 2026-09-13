from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import RuntimeState, Setting
from basswiesn.app.services.metadata_engine import ClockMetadataMode


CLOCK_METADATA_KEY_PREFIX = "research.clock_metadata."
CLOCK_METADATA_DEFAULT_INTERVAL_SECONDS = 60
CLOCK_METADATA_MIN_INTERVAL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ClockMetadataPreference:
    enabled: bool = True
    mode: ClockMetadataMode = ClockMetadataMode.APPEND
    interval_seconds: int = CLOCK_METADATA_DEFAULT_INTERVAL_SECONDS
    experimental: bool = False

    def as_dict(self) -> dict:
        data = asdict(self)
        data["mode"] = self.mode.value
        return data


def _key(device_id: str) -> str:
    normalized = str(device_id or "").strip().upper()
    if not normalized:
        raise ValueError("device_id is required")
    return f"{CLOCK_METADATA_KEY_PREFIX}{normalized}"


def _decode(value: str) -> ClockMetadataPreference:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return ClockMetadataPreference(enabled=False)
    if not isinstance(payload, dict):
        return ClockMetadataPreference(enabled=False)
    try:
        mode = ClockMetadataMode(str(payload.get("mode") or ClockMetadataMode.MISSING_TITLE.value))
    except ValueError:
        mode = ClockMetadataMode.MISSING_TITLE
    try:
        interval = int(payload.get("interval_seconds", CLOCK_METADATA_DEFAULT_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        interval = CLOCK_METADATA_DEFAULT_INTERVAL_SECONDS
    return ClockMetadataPreference(
        enabled=payload.get("enabled") is True,
        mode=mode,
        interval_seconds=max(CLOCK_METADATA_MIN_INTERVAL_SECONDS, interval),
    )


def load_clock_metadata_preference(db: Session, device_id: str) -> ClockMetadataPreference:
    row = db.query(Setting).filter(Setting.key == _key(device_id)).one_or_none()
    return _decode(row.value) if row else ClockMetadataPreference()


def clock_metadata_timezone(db: Session) -> ZoneInfo:
    """Use the application's timezone, independent of Docker's host timezone."""
    row = db.query(Setting).filter(Setting.key == "default_timezone").one_or_none()
    name = (row.value if row else "") or getattr(get_settings(), "default_timezone", "") or "Europe/Berlin"
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return ZoneInfo("UTC")


def _projection_key(device_id: str) -> str:
    return f"device:{device_id.strip().upper()}:clock_projection"


def remember_clock_projection(
    db: Session, device_id: str, station_id: str, rendered: str,
) -> None:
    """Keep bounded, server-generated prefixes to recognize radio echoes.

    Keep this separate from MetadataState and the in-memory monitor cache:
    provider serving and radio polling run in different processes. A clock
    echo is display readback, not a new song title. Minute rollover must not
    repeatedly append time to that echoed title. No independent timer or
    extra radio request is created here.
    """
    if re.fullmatch(r"(?:.* · )?(?:[01]\d|2[0-3]):[0-5]\d", rendered) is None:
        return
    prefix = rendered[:-5]
    key = _projection_key(device_id)
    row = db.query(RuntimeState).filter(RuntimeState.key == key).one_or_none()
    try:
        previous = json.loads(row.value) if row else {}
    except (TypeError, ValueError):
        previous = {}
    prefixes = previous.get("prefixes", []) if isinstance(previous, dict) and previous.get("station_id") == station_id else []
    prefixes = [item for item in prefixes if isinstance(item, str)] if isinstance(prefixes, list) else []
    if prefix in prefixes:
        return
    payload = {"station_id": station_id, "prefixes": [*prefixes[-15:], prefix]}
    if row is None:
        row = RuntimeState(key=key)
        db.add(row)
    row.value = json.dumps(payload, ensure_ascii=False)
    db.flush()  # The caller owns commit/rollback, including report persistence.


def is_clock_projection_echo(
    db: Session, device_id: str, station_id: str | None, track: object,
) -> bool:
    """Recognize only a projection generated for this exact device/selection."""
    if not isinstance(track, str) or not station_id:
        return False
    if re.fullmatch(r"(?:.* · )?(?:[01]\d|2[0-3]):[0-5]\d", track) is None:
        return False
    row = db.query(RuntimeState).filter(RuntimeState.key == _projection_key(device_id)).one_or_none()
    try:
        value = json.loads(row.value) if row else {}
    except (TypeError, ValueError):
        return False
    return (isinstance(value, dict) and value.get("station_id") == station_id
            and isinstance(value.get("prefixes"), list) and track[:-5] in value["prefixes"])


def clock_metadata_lab_enabled(db: Session) -> bool:
    """Return the effective LAB gate used by both API and provider runtime."""

    if get_settings().lab_mode:
        return True
    row = db.query(Setting).filter(Setting.key == "lab_mode").one_or_none()
    return bool(row is not None and str(row.value).strip().lower() == "true")


def save_clock_metadata_preference(
    db: Session,
    device_id: str,
    *,
    enabled: bool,
    mode: str | ClockMetadataMode,
    interval_seconds: int = CLOCK_METADATA_DEFAULT_INTERVAL_SECONDS,
) -> ClockMetadataPreference:
    try:
        parsed_mode = mode if isinstance(mode, ClockMetadataMode) else ClockMetadataMode(str(mode))
    except ValueError as exc:
        raise ValueError("clock metadata mode must be OFF, MISSING_TITLE or APPEND") from exc
    interval = int(interval_seconds)
    if interval < CLOCK_METADATA_MIN_INTERVAL_SECONDS:
        raise ValueError("clock metadata interval must be at least 60 seconds")
    # Disabling remains explicit OFF at runtime, while the selected display
    # style is retained for a later opt-in.
    preference = ClockMetadataPreference(
        enabled=bool(enabled),
        mode=parsed_mode,
        interval_seconds=interval,
    )
    key = _key(device_id)
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    if row is None:
        row = Setting(key=key, value="")
        db.add(row)
    row.value = json.dumps(preference.as_dict(), sort_keys=True)
    db.commit()
    return preference
