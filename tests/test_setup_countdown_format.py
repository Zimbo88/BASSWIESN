from pathlib import Path


APP_JS = Path("basswiesn/app/static/app.js").read_text(encoding="utf-8")


def test_setup_countdown_uses_seconds_formatter_and_clamps_stale_estimates():
    assert "function formatClockSeconds(seconds)" in APP_JS
    assert "estimate > 0 && estimate <= 3600 ? estimate : 390" in APP_JS
    assert "return formatClockSeconds(remaining);" in APP_JS
    assert "läuft länger als erwartet" not in APP_JS
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
