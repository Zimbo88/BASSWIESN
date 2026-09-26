import httpx
import pytest
from playwright.sync_api import sync_playwright, expect

from test_mobile_release import _LiveServer

pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_statistics_filter_and_official_check_are_passive_until_clicked(language, engine):
    with _LiveServer() as server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={"ui_mode": "standard", "web_language": language,
            "show_startup_warning": "false", "first_run_warning_required": "false"}).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": 430, "height": 932}, is_mobile=True, has_touch=True)
        requests, errors = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        def route_request(route):
            if not route.request.url.startswith(server.url + "/"):
                return route.abort()
            path = route.request.url.removeprefix(server.url)
            if path == "/api/stats/playback":
                return route.fulfill(json={"listening": {"periods": {
                    "7d": {"seconds": 3600, "sessions": 2, "by_device": [{"name": "Example Radio", "seconds": 3600, "sessions": 2}],
                           "by_station": [{"name": "Example Station", "seconds": 3600, "sessions": 2}]},
                    "today": {"seconds": 0, "sessions": 0, "by_device": [], "by_station": []}}}})
            if path == "/api/update/official/check":
                requests.append(path)
                return route.fulfill(json={"status": "update_available", "remote_version": "3.0.0", "installation_available": False,
                    "release_url": "https://github.com/Zimbo88/BASSWIESN/releases/tag/v3.0.0"})
            return route.continue_()
        page.route("**/*", route_request)
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function('document.querySelector("#local-test-summary")?.children.length > 0')
        assert requests == []
        page.locator('[data-view="dashboard"]').click()
        expect(page.locator(".listening-summary")).to_contain_text("Example Radio")
        expect(page.locator(".listening-summary")).to_contain_text("Example Station")
        assert page.locator("#dashboard-play-stats details").get_attribute("open") is None
        page.locator("#listening-period").select_option("today")
        expect(page.locator(".listening-summary")).to_contain_text("Noch keine bestätigte" if language == "de" else "No confirmed playback")
        page.locator("#listening-period").select_option("7d")
        expect(page.locator(".listening-summary")).to_contain_text("Example Radio")
        page.locator(".advanced-nav summary").click()
        page.locator('[data-view="features"]').click()
        title = "Offizielle Release-Prüfung" if language == "de" else "Official release check"
        official = page.locator("#feature-status-groups .feature-card").filter(has=page.get_by_role("heading", name=title, exact=True))
        expect(official).to_be_visible()
        expect(official).to_contain_text("Installiert kein Update" if language == "de" else "Does not install an update")
        assert requests == []
        official.locator(".feature-actions button").click()
        expect(page.locator("#view-system-settings")).to_be_visible()
        expect(page.locator("#update-manifest-url")).not_to_be_visible()
        page.locator("#update-check").click()
        expect(page.locator("#update-status")).to_contain_text("3.0.0")
        expect(page.locator("#official-release-link")).to_have_attribute("href", "https://github.com/Zimbo88/BASSWIESN/releases/tag/v3.0.0")
        assert requests == ["/api/update/official/check"]
        expect(page.locator('[data-i18n="update_install_boundary"]')).to_contain_text("Administratorcode" if language == "de" else "administrator code")
        expect(page.locator('#update-admin-install')).to_be_disabled()
        expect(page.locator('#update-admin-code')).to_be_disabled()  # never enter privileged code over HTTP
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert errors == []
        browser.close()
