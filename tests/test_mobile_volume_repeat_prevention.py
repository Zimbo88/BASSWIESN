from pathlib import Path


APP_JS = Path("basswiesn/app/static/app.js").read_text(encoding="utf-8")


def test_mobile_volume_touch_stops_repeat_and_clears_focus():
    assert '"touchend"' in APP_JS
    assert '"touchcancel"' in APP_JS
    assert '"visibilitychange"' in APP_JS
    assert '"pagehide"' in APP_JS
    assert "button.blur()" in APP_JS
    assert "isTouchVolumeEvent(event)" in APP_JS
    assert "volumeHold.suppressClick = true;" in APP_JS
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
