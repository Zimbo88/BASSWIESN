"""Actual browser interactions; every request intercepted, no radio access."""
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect
import pytest

from basswiesn.app.services.radio_reboots import schedule_default, MANUAL_CONFIRMATION, SCHEDULE_CONFIRMATION

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.release]


@pytest.mark.parametrize("lang,width,height", [("de",390,844), ("en",430,932), ("de",1440,900), ("en",1920,1080)])
def test_reboot_preview_schedule_and_reload_are_explicit(tmp_path, lang, width, height):
    assets = Path("basswiesn/app/static")
    calls = []
    config = {"devices": [{"device_id":"REBOOT-A", "name":"Example Radio", "reachable":True},
                          {"device_id":"REBOOT-B", "name":"Other Radio", "reachable":True},
                          {"device_id":"OFFLINE", "name":"Offline", "reachable":False}],
              "schedule": schedule_default(), "manual_confirmation":MANUAL_CONFIRMATION,
              "schedule_confirmation": SCHEDULE_CONFIRMATION, "latest_job_id":None}
    job = {"id":"a" * 32, "status":"PREPARING", "results":[]}
    def handler(route):
        req = route.request; path = urlparse(req.url).path
        body = req.post_data_json if req.method in {"POST", "PUT"} else None
        calls.append((req.method, path, body))
        if path == "/reboots": return route.fulfill(body=(assets/"reboots.html").read_text(), content_type="text/html")
        if path in {"/static/reboots.js", "/static/reboots.css", "/static/remote.css"}:
            return route.fulfill(body=(assets/Path(path).name).read_text(), content_type="text/javascript" if path.endswith(".js") else "text/css")
        if path == "/api/system/settings": return route.fulfill(json={"web_language":lang})
        if path == "/api/radio-reboots": return route.fulfill(json=deepcopy(config))
        if path.endswith("/preview"):
            return route.fulfill(json={"devices":[d for d in config["devices"] if d["device_id"] in body["device_ids"]]})
        if path.endswith("/start"):
            assert body["confirmation"] == MANUAL_CONFIRMATION
            config["latest_job_id"] = job["id"]
            return route.fulfill(json=job)
        if path.endswith("/jobs/" + job["id"]): return route.fulfill(json=job)
        if path.endswith("/schedule"):
            assert body["confirmation"] == SCHEDULE_CONFIRMATION if body["enabled"] else True
            config["schedule"] = {k:v for k,v in body.items() if k != "confirmation"}
            return route.fulfill(json={"schedule": config["schedule"]})
        pytest.fail(f"Unexpected request intercepted: {req.method} {path}")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width":width, "height":height})
        errors=[]; page.on("pageerror", lambda error:errors.append(str(error)))
        page.route("**/*", handler)
        page.goto("http://127.0.0.1:8765/reboots?device=REBOOT-A")
        expect(page.locator('#reboot-devices input[value="REBOOT-A"]')).to_be_checked()
        expect(page.locator("#reboot-preview")).to_be_enabled()
        assert not [c for c in calls if c[0] != "GET"]
        assert page.locator("html").get_attribute("lang") == lang
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("#reboot-preview").click()
        expect(page.locator("#reboot-confirmation")).to_be_visible()
        expect(page.locator("#reboot-start")).to_be_disabled()
        page.locator("#reboot-confirm-check").check()
        expect(page.locator("#reboot-start")).to_be_enabled()
        page.locator("#reboot-select-all").click()
        expect(page.locator("#reboot-confirmation")).not_to_be_visible()
        expect(page.locator('#reboot-devices input[value="OFFLINE"]')).not_to_be_checked()
        page.locator("#reboot-preview").click()
        expect(page.locator("#reboot-confirm-check")).to_be_enabled()
        page.locator("#reboot-confirm-check").check()
        page.locator("#reboot-start").click()
        expect(page.locator("#reboot-job-status")).to_contain_text("Identitäten" if lang == "de" else "identities")
        assert len([c for c in calls if c[1].endswith("/start")]) == 1
        job["status"]="VERIFIED"
        page.reload()
        expect(page.locator("#reboot-job-status")).to_contain_text("bestätigt" if lang == "de" else "verified")
        assert len([c for c in calls if c[1].endswith("/start")]) == 1
        page.locator("#reboot-schedule-enabled").check()
        expect(page.locator("#reboot-schedule-save")).to_be_disabled()
        page.locator("#reboot-schedule-confirm").check()
        page.locator("#reboot-schedule-time").fill("05:30")
        page.locator("#reboot-schedule-time").blur()
        expect(page.locator("#reboot-schedule-confirm")).not_to_be_checked()
        page.locator("#reboot-schedule-confirm").check()
        page.locator("#reboot-schedule-save").click()
        expect(page.locator("#reboot-message")).to_have_text("Zeitplan gespeichert und zurückgelesen." if lang == "de" else "Schedule saved and read back.")
        assert calls[-1][:2] == ("GET", "/api/radio-reboots")
        assert config["schedule"]["time"] == "05:30" and config["schedule"]["skip_active"] is True
        assert not errors
        page.screenshot(path=str(tmp_path/f"reboots-{lang}-{width}.png"), full_page=True)
        browser.close()
