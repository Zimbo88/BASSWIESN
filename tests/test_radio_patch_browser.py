"""The real patch selector only saves plans; all browser traffic is mocked."""
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
import pytest

pytestmark = [pytest.mark.browser, pytest.mark.integration]
SCRIPT = Path(__file__).parents[1] / "basswiesn/app/static/js/radio-patches.js"


@pytest.mark.parametrize("language,width", [("de", 393), ("en", 1440)])
def test_patch_selector_is_explicit_independent_and_never_installs(language, width):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 852})
        calls, errors = [], []
        selection = {"display": "enable", "bootlogo": "enable", "battery": "keep",
                     "battery_kind": "unknown", "revision": "initial"}
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(r):
            url = r.request.url
            if url == "http://127.0.0.1/":
                return r.fulfill(content_type="text/html", body=f'<html lang="{language}"><body class="lab-mode"><section id="radio-patches"></section></body></html>')
            calls.append((r.request.method, url))
            if url == "http://127.0.0.1/api/lab/radio-patches":
                assert r.request.method == "GET"
                return r.fulfill(json={"devices": [{"device_id": "AA11BB22CC33", "name": "<img src=x onerror=alert(1)>"}], "installation_available": False})
            assert url == "http://127.0.0.1/api/lab/radio-patches/AA11BB22CC33/selection"
            if r.request.method == "PUT":
                payload = r.request.post_data_json
                assert payload == {**selection, "display": "enable", "bootlogo": "disable"}
                selection.update(payload, revision="next")
                return r.fulfill(json={"status": "PLANNED", "installed": False})
            assert r.request.method == "GET"
            return r.fulfill(json={**selection, "installed": False})

        page.route("**/*", route)
        page.goto("http://127.0.0.1/")
        page.add_script_tag(path=str(SCRIPT))
        assert calls == []  # Normal page load does not even inspect a radio.
        page.locator("#patch-open").click()
        expect(page.locator("#patch-display")).to_have_value("enable")
        expect(page.locator("#patch-bootlogo")).to_have_value("enable")
        expect(page.locator("#patch-battery")).to_have_value("keep")
        assert page.locator("#radio-patches img").count() == 0
        page.locator("#patch-display").select_option("enable")
        page.locator("#patch-bootlogo").select_option("disable")
        page.locator("#patch-save").click()
        expect(page.locator('[role="status"]')).to_contain_text("Plan gespeichert" if language == "de" else "Plan saved")
        expect(page.locator("#patch-battery")).to_have_value("keep")
        assert sum(method == "PUT" for method, _ in calls) == 1
        count = len(calls)
        page.evaluate("document.body.classList.remove('lab-mode')")
        expect(page.locator("#patch-save")).to_have_count(0)
        page.locator("#patch-open").click()
        assert len(calls) == count
        page.evaluate("document.documentElement.lang='ja'")
        expect(page.locator("#radio-patches h3")).to_have_text("Radio presentation and patches")
        assert errors == []
        browser.close()
