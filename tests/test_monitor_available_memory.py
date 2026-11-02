from tools.system_monitor import memory_status_from_meminfo, parse_meminfo


def test_meminfo_parser_uses_memavailable_not_free_memory():
    text = """MemTotal:        2048000 kB
MemFree:           50000 kB
MemAvailable:    1200000 kB
SwapFree:          10000 kB
"""
    parsed = parse_meminfo(text)
    status = memory_status_from_meminfo(text)

    assert parsed["MemFree"] == 48
    assert parsed["MemAvailable"] == 1171
    assert status["basis"] == "MemAvailable"
    assert status["status"] == "ok"


def test_low_memavailable_warns_even_if_parser_has_free_value():
    text = """MemTotal:        2048000 kB
MemFree:          200000 kB
MemAvailable:     200000 kB
"""
    status = memory_status_from_meminfo(text)

    assert status["available_mb"] == 195
    assert status["status"] == "warning"
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
