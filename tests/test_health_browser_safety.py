"""A late cached health response must never become another radio's projection."""
import httpx
import pytest
from playwright.sync_api import sync_playwright

from test_mobile_release import _LiveServer

pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_health_selection_clears_old_projection_and_discards_late_responses(engine):
    with _LiveServer() as server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={
            "ui_mode": "standard", "web_language": "en", "show_startup_warning": "false",
            "first_run_warning_required": "false",
        }).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": 393, "height": 852})
        held, writes, errors = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        def route(r):
            url = r.request.url
            if not url.startswith(server.url + "/"):
                return r.abort()
            if url.endswith("/api/devices"):
                return r.fulfill(json=[])
            if "/api/devices/A/" in url or "/api/devices/B/" in url:
                if r.request.method != "GET":
                    writes.append(url)
                    return r.abort()
                device = "A" if "/A/" in url else "B"
                if device == "A":
                    held.append(r)
                    return
                return r.fulfill(json={"device_id": device, "track": "B song", "items": []})
            return r.continue_()
        page.route("**/*", route)
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function("Boolean(state.systemHealth)")
        page.evaluate("""() => {
          const select = document.getElementById('health-device-select');
          select.replaceChildren(new Option('A', 'A'), new Option('B', 'B'));
          select.value = 'A';
          state.researchHealth.metadata = {track: 'stale track'};
          window.heldHealth = loadResearchHealth();
        }""")
        page.wait_for_function("state.researchHealth.metadata === null")
        page.wait_for_timeout(100)
        assert held
        page.evaluate("""() => {
          document.getElementById('health-device-select').value = 'B';
          window.healthReadDone = false;
          window.currentHealthRead = loadResearchHealth().then(() => { window.healthReadDone = true; });
        }""")
        page.wait_for_function("() => window.healthReadDone")
        assert page.evaluate("state.researchHealth.metadata.track") == "B song"
        for r in held:
            r.fulfill(json={"device_id": "A", "track": "A song", "items": []})
        page.evaluate("async () => { await window.heldHealth; }")
        assert page.evaluate("state.researchHealth.metadata.track") == "B song"
        assert page.evaluate("state.researchHealth.deviceId") == "B"
        assert writes == [] and errors == []
        browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("action", ["clock", "metadata", "airplay"])
def test_health_action_pins_target_and_cannot_replace_another_selection(engine, action):
    with _LiveServer() as server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={
            "ui_mode": "lab", "web_language": "en", "show_startup_warning": "false",
            "first_run_warning_required": "false",
        }).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": 393, "height": 852})
        held, calls, errors = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        def route(r):
            url = r.request.url
            if not url.startswith(server.url + "/"):
                return r.abort()
            if url.endswith("/api/devices"):
                return r.fulfill(json=[])
            if "/api/devices/A/" in url or "/api/devices/B/" in url:
                if r.request.method != "GET":
                    calls.append((url, r.request.post_data_json))
                    held.append(r)
                    return
                return r.fulfill(json={"device_id": "B", "track": "B song", "items": [],
                                       "enabled": False, "interval_seconds": 77, "label": "B readiness"})
            return r.continue_()
        page.route("**/*", route)
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function("Boolean(state.systemHealth)")
        page.evaluate("""action => {
          const select = document.getElementById('health-device-select');
          select.replaceChildren(new Option('A', 'A'), new Option('B', 'B'));
          select.value = 'A'; state.researchHealth = {deviceId:'A', loading:false};
          if (action === 'airplay') window.healthAction = probeAirplayReadiness();
          else {
            const form = document.getElementById(action === 'clock' ? 'clock-metadata-form' : 'live-metadata-form');
            form.dispatchEvent(new Event('submit', {cancelable:true}));
            form.dispatchEvent(new Event('submit', {cancelable:true}));
          }
        }""", action)
        page.wait_for_timeout(150)
        assert len(calls) == 1 and "/A/" in calls[0][0]
        page.evaluate("""() => {
          document.getElementById('health-device-select').value = 'B';
          window.healthReadDone = false;
          window.currentHealthRead = loadResearchHealth().then(() => { window.healthReadDone = true; });
        }""")
        page.wait_for_function("() => window.healthReadDone")
        held.pop().fulfill(json={"device_id": "A", "enabled": True, "interval_seconds": 120,
                                "label": "A readiness", "playback_action": "NONE"})
        if action == "airplay":
            page.evaluate("async () => { await window.healthAction; }")
        else:
            form_id = "clock-metadata-form" if action == "clock" else "live-metadata-form"
            page.wait_for_function("id => !healthWrites.has(document.getElementById(id))", arg=form_id)
        assert page.evaluate("state.researchHealth.deviceId") == "B"
        assert page.evaluate("state.researchHealth.metadata.track") == "B song"
        assert page.evaluate("state.researchHealth.clock.interval_seconds") == 77
        assert page.evaluate("state.researchHealth.airplay.label") == "B readiness"
        assert errors == []
        browser.close()
