"""Actual LAB form/controller with loopback pages and simulated API responses."""
import re

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

from test_mobile_release import _LiveServer

pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("kind", ["wifi", "station"])
def test_manual_commands_preview_freeze_target_and_require_review(engine, kind):
    with _LiveServer() as server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={"ui_mode": "lab", "web_language": "en",
            "show_startup_warning": "false", "first_run_warning_required": "false"}).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": 430, "height": 932})
        requests, held, errors = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())
        endpoint = "wireless-profiles" if kind == "wifi" else "station/add-native"
        form_id = "wireless-profile-form" if kind == "wifi" else "native-station-add-form"
        output_id = "wireless-profile-output" if kind == "wifi" else "native-station-output"

        def route(r):
            if not r.request.url.startswith(server.url + "/"):
                return r.abort()
            if r.request.url == server.url + "/api/devices":
                return r.fulfill(json=[{"device_id": v, "name": f"Radio {v}", "ip_address": f"192.0.2.{n}"}
                                       for v, n in (("A", 42), ("B", 43))])
            if r.request.url.endswith("/" + endpoint) and r.request.method == "POST":
                payload = r.request.post_data_json
                requests.append((r.request.url, payload))
                if payload["dry_run"]:
                    return r.fulfill(json={"dry_run": True, "xml": "redacted preview"})
                held.append(r)
                return
            return r.continue_()

        page.route("**/*", route)
        page.goto(server.url, wait_until="networkidle")
        form = page.locator(f"#{form_id}")
        expect(form).to_have_attribute("data-manual-controller-ready", "true")
        form.evaluate("""form => {
          for (let node = form; node; node = node.parentElement) {
            node.hidden = false; node.style.display = 'block';
            if (node.tagName === 'DETAILS') node.open = true;
          }
          form.elements.device_id.replaceChildren(new Option('Radio A','A'), new Option('Radio B','B'));
          form.elements.device_id.value = 'A';
        }""")
        private = form.locator('[name="password"]' if kind == "wifi" else '[name="token"]')
        if kind == "wifi":
            form.locator('[name="ssid"]').fill("Example Network")
            form.locator('[name="understood"]').check()
        else:
            form.locator('[name="name"]').fill("Example Station")
        private.fill("TEST-PRIVATE-VALUE")
        button = form.locator('button[type="submit"]')
        button.click()
        expect(page.locator(f"#{output_id}")).to_contain_text("Preview")
        expect(private).to_have_value("")
        assert requests[0][1]["dry_run"] is True and "operation_id" not in requests[0][1]
        assert not held

        private.fill("TEST-PRIVATE-VALUE")
        form.locator('[name="dry_run"]').uncheck()
        button.click()
        expect(button).to_be_disabled()
        expect(form.locator('[name="device_id"]')).to_be_disabled()
        page.wait_for_timeout(100)
        assert len(held) == 1
        # Programmatic duplicate submit must also be ignored, not just clicks.
        form.dispatch_event("submit")
        assert len(requests) == 2
        sent = requests[-1][1]
        assert re.fullmatch(r"[a-f0-9]{32}", sent["operation_id"])
        assert sent["confirmation"] == "YES" and sent["acknowledge_manual_recovery"] is True
        assert requests[-1][0].endswith("/A/" + endpoint)
        # Force a programmatic selection change: the late result still must
        # not render as B's state or cause a second command to B.
        form.evaluate("form => { form.elements.device_id.value='B'; form.elements.device_id.dispatchEvent(new Event('change')); }")
        held.pop().fulfill(status=202, json={"device_id": "A", "status": "PENDING_READBACK", "verified": False,
                                            "persistence_verified": False, "reconciliation_required": True})
        expect(form.locator('[name="device_id"]')).to_be_enabled()
        expect(private).to_have_value("")
        expect(page.locator(f"#{output_id}")).not_to_contain_text("PENDING_READBACK")
        form.locator('[name="device_id"]').select_option("A")
        expect(page.locator(f"#{output_id}")).to_contain_text("State not fully confirmed")
        expect(page.locator(f"#{output_id}")).to_contain_text("PENDING_READBACK")
        expect(button).to_be_disabled()
        reset = form.locator('[data-manual-review]')
        reset.click()
        expect(button).to_be_enabled()
        assert len(requests) == 2  # Review is not replay.
        private.fill("TEST-PRIVATE-VALUE")
        button.click()
        page.wait_for_timeout(100)
        assert len(held) == 1 and requests[-1][1]["operation_id"] != sent["operation_id"]
        held.pop().fulfill(status=502, json={"detail": {"code": "MANUAL_COMMAND_OUTCOME_UNKNOWN", "status": "DIVERGED",
            "message": "Readback unavailable", "verified": False, "reconciliation_required": True}})
        expect(page.locator(f"#{output_id}")).to_contain_text("DIVERGED")
        expect(button).to_be_disabled()
        expect(private).to_have_value("")
        assert errors == []
        browser.close()
