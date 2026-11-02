"""Small host monitoring helpers used by diagnostics and release tests."""

from __future__ import annotations


def parse_meminfo(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        parts = raw.strip().split()
        if not parts:
            continue
        try:
            kb = int(parts[0])
        except ValueError:
            continue
        values[key] = kb // 1024
    return values


def memory_status_from_meminfo(text: str, *, warning_mb: int = 250, critical_mb: int = 120) -> dict:
    values = parse_meminfo(text)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    if available < critical_mb:
        status = "critical"
    elif available < warning_mb:
        status = "warning"
    else:
        status = "ok"
    return {
        "status": status,
        "available_mb": available,
        "free_mb": values.get("MemFree", 0),
        "warning_mb": warning_mb,
        "critical_mb": critical_mb,
        "basis": "MemAvailable" if "MemAvailable" in values else "MemFree",
    }
