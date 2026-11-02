"""Read-only listening summaries over confirmed, bounded history intervals."""
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
import re

from basswiesn.app.services.playback_state import conservative_duration_seconds


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def observed_interval(row, *, now, tolerance):
    start = aware(row.started_at)
    seconds = conservative_duration_seconds(row, now=now, poll_tolerance_seconds=tolerance)
    # Neither malformed future timestamps nor a future end grow the counters.
    return start, max(start, min(now, start + timedelta(seconds=seconds)))


def overlap_seconds(start, end, boundary):
    return max(0, int((end - max(start, boundary)).total_seconds()))


def listening_summary(rows, *, now, tolerance, device_names, station_names, timezone_name="UTC"):
    now = aware(now)
    zone = ZoneInfo(timezone_name)
    midnight = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    boundaries = {"today": midnight, "7d": now - timedelta(days=7),
                  "30d": now - timedelta(days=30), "all": datetime.min.replace(tzinfo=UTC)}
    result = {}
    for key, boundary in boundaries.items():
        devices, stations = {}, {}
        sessions, seconds, open_sessions = 0, 0, 0
        endings = {}
        for row in rows:
            start, end = observed_interval(row, now=now, tolerance=tolerance)
            duration = overlap_seconds(start, end, boundary)
            if not duration:
                continue
            sessions += 1
            seconds += duration
            if row.ended_at is None:
                open_sessions += 1
            elif boundary <= aware(row.ended_at) <= now:
                reason = str(row.end_reason or "unknown")
                reason = reason if re.fullmatch(r"[a-z_]{1,64}", reason) else "unknown"
                endings[reason] = endings.get(reason, 0) + 1
            for buckets, identity, label in (
                (devices, row.device_id, device_names.get(row.device_id) or row.device_name or row.device_id),
                (stations, station_names[row.id], station_names[row.id]),
            ):
                bucket = buckets.setdefault(identity, {"name": label, "seconds": 0, "sessions": 0})
                bucket["seconds"] += duration
                bucket["sessions"] += 1
        result[key] = {"seconds": seconds, "sessions": sessions,
                       "window_start_utc": boundary.isoformat(),
                       "open_sessions": open_sessions, "observed_end_reasons": endings,
                       "by_device": sorted(devices.values(), key=lambda item: item["seconds"], reverse=True),
                       "by_station": sorted(stations.values(), key=lambda item: item["seconds"], reverse=True)}
    return {"timezone": zone.key, "scope": "CONFIRMED_HISTORY_INTERVALS",
            "rolling_periods_are_elapsed_days": True, "end_reason_is_cause_proof": False,
            "duration_is_estimate": True, "audible_output_verified": False,
            "generated_at": now.isoformat(), "periods": result}
