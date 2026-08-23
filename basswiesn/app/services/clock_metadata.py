from __future__ import annotations

from dataclasses import asdict, dataclass
import json

from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import Setting
from basswiesn.app.services.metadata_engine import ClockMetadataMode


CLOCK_METADATA_KEY_PREFIX = "research.clock_metadata."
CLOCK_METADATA_DEFAULT_INTERVAL_SECONDS = 60
CLOCK_METADATA_MIN_INTERVAL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ClockMetadataPreference:
    enabled: bool = False
    mode: ClockMetadataMode = ClockMetadataMode.MISSING_TITLE
    interval_seconds: int = CLOCK_METADATA_DEFAULT_INTERVAL_SECONDS
    experimental: bool = True

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
        return ClockMetadataPreference()
    if not isinstance(payload, dict):
        return ClockMetadataPreference()
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
        raise ValueError("experimental clock metadata interval must be at least 60 seconds")
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
