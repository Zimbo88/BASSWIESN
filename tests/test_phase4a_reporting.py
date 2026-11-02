from pathlib import Path

import pytest

from tools.generate_project_metrics import metrics_from_outputs, parse_collected_count, parse_duration


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_collection_parser_handles_pytest_summary():
    assert parse_collected_count("84 tests collected") == 84
    assert parse_collected_count("91/383 tests collected (292 deselected)") == 91
    assert parse_collected_count("no tests collected") == 0
    assert parse_duration("================ 84 passed in 1.25s ================") == 1.25


def test_metrics_output_is_deterministic_for_same_inputs():
    marker_outputs = {
        "unit": "84 tests collected",
        "integration": "292 tests collected",
        "browser": "16 tests collected",
        "slow": "79 tests collected",
        "release": "50 tests collected",
        "hardware": "0 tests collected",
    }
    first = metrics_from_outputs(
        ROOT,
        generated_at="2026-07-31T12:00:00+00:00",
        collected_output="376 tests collected",
        marker_outputs=marker_outputs,
        git_commit="deadbeef",
        dirty=True,
    )
    second = metrics_from_outputs(
        ROOT,
        generated_at="2026-07-31T12:00:00+00:00",
        collected_output="376 tests collected",
        marker_outputs=marker_outputs,
        git_commit="deadbeef",
        dirty=True,
    )
    assert first == second
    assert first["tests"]["counts"] == {
        "total": 376,
        "unit": 84,
        "integration": 292,
        "browser": 16,
        "slow": 79,
        "release": 50,
        "hardware": 0,
    }
    assert first["hardware_tests_started"] is False
