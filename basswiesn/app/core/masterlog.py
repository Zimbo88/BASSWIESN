"""Best-effort append-only JSONL log for private Raspberry Pi testing."""

from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import threading

from basswiesn.app.config import get_settings


logger = logging.getLogger(__name__)
_SECRET_MARKERS = ("password", "passwd", "token", "secret", "private_key", "credential", "authorization")
_LOG_LOCK = threading.Lock()


def _sanitize(value, key: str = ""):
    if any(marker in key.lower() for marker in _SECRET_MARKERS):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {str(item_key): _sanitize(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def rotate_masterlog(path: Path, *, max_bytes: int, backup_count: int) -> bool:
    """Rotate the JSONL log before it exceeds its configured bound."""

    if max_bytes <= 0 or not path.exists():
        return False
    try:
        if path.stat().st_size < max_bytes:
            return False
        count = max(1, int(backup_count))
        for index in range(count, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            target = path.with_name(f"{path.name}.{index + 1}")
            if index == count:
                target.unlink(missing_ok=True)
            elif source.exists():
                os.replace(source, target)
        os.replace(path, path.with_name(f"{path.name}.1"))
        return True
    except OSError as exc:
        logger.warning("Could not rotate BASSWIESN masterlog: %s", exc)
        return False


def write_masterlog(event: str, **fields) -> None:
    """Append one sanitized JSON event; logging failure never breaks the app."""

    try:
        settings = get_settings()
        if not settings.masterlog_enabled:
            return
        path = Path(settings.data_dir) / "logs" / "master.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            **_sanitize(fields),
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _LOG_LOCK:
            rotate_masterlog(
                path,
                max_bytes=max(0, int(getattr(settings, "masterlog_max_mb", 50))) * 1024 * 1024,
                backup_count=max(1, int(getattr(settings, "masterlog_backup_count", 5))),
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Could not write BASSWIESN masterlog: %s", exc)
