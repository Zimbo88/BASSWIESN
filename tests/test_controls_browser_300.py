"""Main remote: real pointer interaction, all radio paths intercepted locally."""
import json

import httpx
from playwright.sync_api import sync_playwright, expect
import pytest

from test_mobile_release import _LiveServer

pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("language", ["de", "en"])
def test_main_controls_are_readback_driven_and_do_not_fake_history(engine, language):
    calls = []
    readback = {"device_id": "CONTROL-A", "verified": True, "volume": 23, "source": "LOCAL_INTERNET_RADIO", "play_status": "PLAY_STATE", "now_playing": {"stationName": "Example Station", "artist": "Example Artist", "track": "Example Track"}}
    broken = {"readback": False}
    with _LiveServer() as server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={"ui_mode": "standard", "web_language": language, "show_startup_warning": "false", "first_run_warning_required": "false"}).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": 430, "height": 932}, is_mobile=True, has_touch=True)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        def routes(route):
            path = route.request.url.removeprefix(server.url)
            if not route.request.url.startswith(server.url + "/"):
                return route.abort()
            if path == "/api/devices":
                return route.fulfill(json=[{"device_id": "CONTROL-A", "name": "Example A", "ip_address": "192.0.2.42", "reachable": True}, {"device_id": "CONTROL-B", "name": "Example B", "ip_address": "192.0.2.43", "reachable": True}, {"device_id": "PROTECTED", "protected": True}])
            if path == "/api/devices/health":
                return route.fulfill(json=[{"device_id": "CONTROL-A", "circuit_state": "closed", "suspected_state": "online", "current_backoff_seconds": 0}, {"device_id": "PROTECTED", "circuit_state": "closed"}])
            if path.startswith("/api/devices/CONTROL-A/"):
                calls.append((route.request.method, path, route.request.post_data_json if route.request.method == "POST" else None))
                if path.endswith("/remote-state"):
                    return route.fulfill(status=502, json={"detail": "fixture failure"}) if broken["readback"] else route.fulfill(json=readback)
                if path.endswith("/key"):
                    readback["volume"] += 1 if route.request.post_data_json["key"] == "VOLUME_UP" else 0
                    return route.fulfill(json={"key": route.request.post_data_json["key"]})
                if path.endswith("/settings/volume"):
                    readback["volume"] = route.request.post_data_json["value"]
                    return route.fulfill(json={"verified": True})
                if path.endswith("/diagnostics/readiness"):
                    return route.fulfill(json={"device_id": "CONTROL-A", "ssh": "available", "persistent_ssh": True, "remote_services": True, "host_redirect": False, "observed_at": "2026-09-23T00:00:00Z"})
                raise AssertionError(path)
            if path in ("/api/play-history/start", "/api/play-history/event"):
                raise AssertionError("UI must not infer a session end from PLAY_PAUSE")
            return route.continue_()
        page.route("**/*", routes)
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function('document.querySelector("#local-test-summary")?.children.length > 0')
        page.locator('[data-view="controls"]').click()
        assert calls == []
        expect(page.locator("#key-safe-volume-enabled")).not_to_be_checked()
        expect(page.locator("#key-volume-slider")).to_be_disabled()
        expect(page.locator("#key-command-output")).not_to_be_visible()
        assert page.locator('#key-command-grid [data-key-command]').evaluate_all('nodes => nodes.map(n => n.dataset.keyCommand)') == ["PREV_TRACK", "PLAY_PAUSE", "NEXT_TRACK", "STOP", "MUTE", "POWER", *[f"PRESET_{n}" for n in range(1, 7)]]
        page.locator("#reload-controls").click()
        expect(page.locator("#key-volume-label")).to_have_text("23%")
        plus = page.locator('[data-key-command="VOLUME_UP"]')
        plus.scroll_into_view_if_needed()
        bounds = plus.bounding_box()
        page.mouse.move(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)
        page.mouse.down()
        page.wait_for_timeout(800)
        assert not [call for call in calls if call[0] == "POST"]
        page.mouse.up()
        expect(page.locator("#key-volume-label")).to_have_text("24%")
        page.wait_for_timeout(500)
        assert len([call for call in calls if call[0] == "POST"]) == 1
        assert "safe_volume" not in calls[-2][2]
        page.locator('[data-key-command="PLAY_PAUSE"]').click()
        expect(page.locator("#key-command-status")).to_have_text("Befehl gesendet; aktueller Zustand zurückgelesen." if language == "de" else "Command sent; current state read back.")
        assert calls[-1][1].endswith("/remote-state")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        before_power = len(calls)
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.locator('[data-key-command="POWER"]').click()
        assert len(calls) == before_power
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator('[data-key-command="POWER"]').click()
        expect(page.locator("#key-command-status")).to_have_text("Befehl gesendet; aktueller Zustand zurückgelesen." if language == "de" else "Command sent; current state read back.")
        assert calls[-2][2]["confirmation"] == "YES"
        assert calls[-1][1].endswith("/remote-state")
        broken["readback"] = True
        page.locator('[data-key-command="MUTE"]').click()
        expect(page.locator("#key-volume-label")).to_have_text("—")
        expect(page.locator("#key-volume-slider")).to_be_disabled()
        expect(page.locator("#key-command-status")).to_contain_text("Rücklesen fehlgeschlagen" if language == "de" else "readback failed")
        page.locator("#controls-multiroom").click()
        assert page.locator("#multiroom-master").input_value() == "CONTROL-A"
        expect(page.locator('#multiroom-form input[name="preserve_volumes"]')).to_be_checked()
        page.locator('[data-view="devices"]').click()
        button = page.locator('#devices-cards [data-check-readiness="CONTROL-A"]')
        button.locator("xpath=..").locator("summary").click()
        button.click()
        expect(page.locator('#devices-cards [data-check-readiness="CONTROL-A"]').locator("xpath=..")).to_contain_text("SSH-Prüfung erfolgreich" if language == "de" else "SSH check succeeded")
        assert page.locator('[data-check-readiness="PROTECTED"]').count() == 0
        policy = page.locator("#devices-cards .policy-line")
        expect(policy).to_have_count(1)
        expect(policy).to_contain_text("Geräteanfragen freigegeben" if language == "de" else "Device requests allowed")
        expect(policy).to_contain_text("nicht beobachtet" if language == "de" else "not observed")
        expect(policy.locator("details")).not_to_be_visible()
        assert "online" not in policy.inner_text()  # no fabricated connectivity PASS
        assert "Backoff" not in policy.inner_text()
        page.locator("#ui-mode-switch").select_option("lab")
        expect(policy.locator("details")).to_be_visible()
        expect(policy.locator("details")).not_to_have_attribute("open", "")
        policy.locator("summary").click()
        expect(policy.locator("code").first).to_have_text("closed")
        page.evaluate('''() => {
          state.deviceHealth[0].circuit_state = 'open';
          state.deviceHealth[0].last_skip_reason = '<img src=x onerror=alert(1)>';
          renderDevices();
        }''')
        expect(policy).to_contain_text("Automatische Anfragen pausiert" if language == "de" else "Automatic requests paused")
        policy.locator("summary").click()
        expect(policy.locator("img")).to_have_count(0)
        expect(policy.locator("code").last).to_have_text("<img src=x onerror=alert(1)>")
        page.locator("#ui-mode-switch").select_option("easy")
        expect(policy).not_to_be_visible()
        assert errors == []
        browser.close()
