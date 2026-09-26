"""Read-only listening summaries over confirmed, bounded history intervals."""
from datetime import UTC, datetime, timedelta

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


def listening_summary(rows, *, now, tolerance, device_names, station_names):
    now = aware(now)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    boundaries = {"today": midnight, "7d": now - timedelta(days=7),
                  "30d": now - timedelta(days=30), "all": datetime.min.replace(tzinfo=UTC)}
    result = {}
    for key, boundary in boundaries.items():
        devices, stations = {}, {}
        sessions, seconds = 0, 0
        for row in rows:
            start, end = observed_interval(row, now=now, tolerance=tolerance)
            duration = overlap_seconds(start, end, boundary)
            if not duration:
                continue
            sessions += 1
            seconds += duration
            for buckets, identity, label in (
                (devices, row.device_id, device_names.get(row.device_id) or row.device_name or row.device_id),
                (stations, station_names[row.id], station_names[row.id]),
            ):
                bucket = buckets.setdefault(identity, {"name": label, "seconds": 0, "sessions": 0})
                bucket["seconds"] += duration
                bucket["sessions"] += 1
        result[key] = {"seconds": seconds, "sessions": sessions,
                       "by_device": sorted(devices.values(), key=lambda item: item["seconds"], reverse=True),
                       "by_station": sorted(stations.values(), key=lambda item: item["seconds"], reverse=True)}
    return {"timezone": "UTC", "scope": "CONFIRMED_HISTORY_INTERVALS",
            "duration_is_estimate": True, "audible_output_verified": False,
            "generated_at": now.isoformat(), "periods": result}
