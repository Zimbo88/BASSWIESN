from pathlib import Path


APP_JS = Path("basswiesn/app/static/app.js").read_text(encoding="utf-8")


def test_mobile_volume_touch_stops_repeat_and_clears_focus():
    # The old timer-based press/repeat/release implementation was removed.
    # Native click fires once on release, and the command/readback is serialized.
    # Browser coverage holds the pointer down and checks request counts.
    assert "volumeHold" not in APP_JS
    assert 'document.getElementById("view-controls")?.addEventListener("click"' in APP_JS
    assert "if (!key || controlsState.busy) return;" in APP_JS
    assert "if (controlsState.busy) return;" in APP_JS
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
