"""Target changes must not display another radio's preset readback."""

import httpx
import pytest
from playwright.sync_api import sync_playwright

from test_mobile_release import _LiveServer

pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("outcome", ["buffering", "late", "error"])
def test_station_start_pins_target_and_does_not_claim_transport_success(engine, outcome):
    with _LiveServer() as server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={
            "ui_mode": "standard", "web_language": "en", "show_startup_warning": "false",
            "first_run_warning_required": "false",
        }).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": 393, "height": 852})
        held, calls, errors = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())
        def route(r):
            if not r.request.url.startswith(server.url + "/"):
                return r.abort()
            if r.request.url.endswith("/api/devices"):
                return r.fulfill(json=[])
            if "/stations/7/play" in r.request.url:
                calls.append((r.request.url, r.request.post_data_json))
                held.append(r)
                return
            return r.continue_()
        page.route("**/*", route)
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function("Boolean(state.systemHealth)")
        page.evaluate("""() => {
          const select = document.getElementById('station-play-device');
          select.replaceChildren(new Option('A', 'A'), new Option('B', 'B'));
          select.value = 'A';
          window.stationPromise = playStation(7, false);
          playStation(7, false);
        }""")
        page.wait_for_timeout(150)
        assert len(calls) == 1
        assert calls[0][0].endswith("/api/devices/A/stations/7/play")
        assert calls[0][1]["dry_run"] is False
        if outcome == "late":
            page.evaluate("""() => {
              const select = document.getElementById('station-play-device');
              select.value = 'B';
              select.dispatchEvent(new Event('change'));
              document.getElementById('station-play-output').textContent = 'B: unchanged';
            }""")
        if outcome == "error":
            held.pop().fulfill(status=502, json={"detail": {"code": "STATION_OUTCOME_UNKNOWN"}})
        else:
            held.pop().fulfill(json={"verified": False, "status": "PENDING_READBACK", "confirmed_volume": None})
        page.evaluate("async () => { await window.stationPromise; }")
        text = page.locator("#station-play-output").text_content()
        if outcome == "late":
            assert text == "B: unchanged"
        else:
            assert "not confirmed" in text
            assert "reports playback" not in text
        assert page.evaluate("pendingStationActions.size") == 0
        assert len(calls) == 1 and errors == []
        browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_late_preset_response_cannot_replace_new_selection(engine):
    with _LiveServer() as server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={
            "ui_mode": "standard", "web_language": "en",
            "show_startup_warning": "false", "first_run_warning_required": "false",
        }).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": 393, "height": 852})
        held, probes, errors = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(r):
            url = r.request.url
            if not url.startswith(server.url + "/"):
                return r.abort()
            if url.endswith("/api/devices"):
                return r.fulfill(json=[])
            if "/api/presets/" in url:
                if "probe=true" in url:
                    probes.append(url)
                if url.endswith("/A"):
                    held.append(r)
                    return
                device = url.split("/api/presets/")[1].split("/")[0]
                if "/status" in url:
                    return r.fulfill(json={"device_id": device, "slots": []})
                return r.fulfill(json=[])
            return r.continue_()

        page.route("**/*", route)
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function("Boolean(state.systemHealth)")
        page.evaluate("""() => {
          const select = document.querySelector('#preset-device-select');
          select.replaceChildren(new Option('A', 'A'), new Option('B', 'B'));
          select.value = 'A';
          window.pendingPresetRead = loadPresetsForSelectedDevice();
        }""")
        page.wait_for_timeout(100)
        assert len(held) == 1
        page.evaluate("""async () => {
          document.querySelector('#preset-device-select').value = 'B';
          await loadPresetsForSelectedDevice(new Event('change'));
        }""")
        assert page.evaluate("state.presetStatus.device_id") == "B"
        held.pop().fulfill(json=[])
        page.evaluate("async () => { await window.pendingPresetRead; }")
        assert page.evaluate("state.presetStatus.device_id") == "B"
        assert probes == []  # A change event is never permission to probe.
        assert errors == []
        browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("action", ["set", "clone", "profile", "set-unverified"])
def test_preset_forms_pin_payload_block_duplicates_and_ignore_late_target(engine, action):
    unverified = action == "set-unverified"
    action = "set" if unverified else action
    with _LiveServer() as server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={
            "ui_mode": "standard", "web_language": "en",
            "show_startup_warning": "false", "first_run_warning_required": "false",
        }).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": 393, "height": 852})
        held, calls, errors = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(r):
            url = r.request.url
            if not url.startswith(server.url + "/"):
                return r.abort()
            if r.request.method == "POST" and ("/api/presets/" in url or "/apply/" in url):
                calls.append((url, r.request.post_data_json))
                held.append(r)
                return
            if url.endswith("/api/devices"):
                return r.fulfill(json=[])
            if "/api/presets/" in url:
                return r.fulfill(json={"device_id": "B", "slots": []} if "/status" in url else [])
            return r.continue_()

        page.route("**/*", route)
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function("Boolean(state.systemHealth)")
        page.evaluate("""action => {
          for (const id of ['preset-device-select', 'clone-target-device', 'profile-apply-device']) {
            const select = document.getElementById(id);
            select.replaceChildren(new Option('A', 'A'), new Option('B', 'B'));
            select.value = 'A';
          }
          document.getElementById('clone-source-device').replaceChildren(new Option('Source', 'S'));
          document.getElementById('preset-station-select').replaceChildren(new Option('Station', '7'));
          document.getElementById('preset-profile-select').replaceChildren(new Option('Profile', '9'));
          const id = {set: 'preset-form', clone: 'preset-clone-form', profile: 'preset-profile-apply-form'}[action];
          const form = document.getElementById(id);
          form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
          form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
        }""", action)
        page.wait_for_timeout(100)
        assert len(calls) == 1
        url, payload = calls[0]
        if action == "set":
            assert url.endswith("/api/presets/A/1") and payload["station_id"] == 7
        elif action == "profile":
            assert url.endswith("/api/preset-profiles/9/apply/A")
        else:
            assert payload == {"source_device_id": "S", "target_device_id": "A"}
        if not unverified:
            page.evaluate("""() => {
          for (const id of ['preset-device-select', 'clone-target-device', 'profile-apply-device']) {
            document.getElementById(id).value = 'B';
          }
          document.getElementById('preset-result').textContent = 'B: unchanged';
          document.getElementById('preset-clone-output').textContent = 'B: unchanged';
        }""")
        held.pop().fulfill(json={"verified": not unverified, "dry_run": False, "button": 1})
        page.wait_for_function("pendingPresetActions.size === 0")
        if unverified:
            assert "Preset could not be verified" in page.locator("#preset-result").text_content()
            assert "erfolgreich geprüft" not in page.locator("#preset-result").text_content()
        else:
            assert page.locator("#preset-result").text_content() == "B: unchanged"
            assert page.locator("#preset-clone-output").text_content() == "B: unchanged"
        assert errors == []
        browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("scenario", ["delete-unverified", "delete-late", "download-late"])
def test_preset_action_keeps_target_revision_and_honest_result(engine, scenario):
    with _LiveServer() as server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={
            "ui_mode": "standard", "web_language": "de",
            "show_startup_warning": "false", "first_run_warning_required": "false",
        }).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": 393, "height": 852})
        held, calls, errors = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())

        def route(r):
            url = r.request.url
            if not url.startswith(server.url + "/"):
                return r.abort()
            if r.request.method == "DELETE" or url.endswith("/presets/download"):
                calls.append(url)
                held.append(r)
                return
            if url.endswith("/api/devices"):
                return r.fulfill(json=[])
            if "/api/presets/" in url:
                return r.fulfill(json={"device_id": "A", "slots": []} if "/status" in url else [])
            return r.continue_()

        page.route("**/*", route)
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function("Boolean(state.systemHealth)")
        page.evaluate("""() => {
          const select = document.querySelector('#preset-device-select');
          select.replaceChildren(new Option('A', 'A'), new Option('B', 'B'));
          select.value = 'A';
          document.querySelector('#preset-slot-grid').innerHTML =
            '<button data-delete-preset="1" data-preset-revision="7">Delete</button>';
        }""")
        selector = "#download-radio-presets" if scenario.startswith("download") else "[data-delete-preset]"
        page.evaluate("selector => { const b = document.querySelector(selector); b.click(); b.click(); }", selector)
        page.wait_for_timeout(100)
        assert len(calls) == 1
        if scenario.startswith("delete"):
            assert "/api/presets/A/1?expected_revision=7" in calls[0]
        if scenario.endswith("late"):
            page.evaluate("""() => {
              document.querySelector('#preset-device-select').value = 'B';
              document.querySelector('#preset-result').textContent = 'B: unchanged';
            }""")
        held.pop().fulfill(json={"verified": False, "dry_run": False, "pending_slots": [1]})
        page.wait_for_function("pendingPresetActions.size === 0")
        output = page.locator("#preset-result").text_content()
        if scenario.endswith("late"):
            assert output == "B: unchanged"
        else:
            assert "nicht bestätigt" in output
            assert "wurde entfernt" not in page.locator("body").inner_text()
        assert errors == []
        browser.close()
