"""Human browser interaction with an offline synthetic ContentDirectory."""
import asyncio
import os
from pathlib import Path
import xml.etree.ElementTree as ET
import httpx
import pytest
from playwright.sync_api import expect, sync_playwright
from test_dlna_library_300 import MediaFixture, BASE
from test_mobile_release import _LiveServer
from basswiesn.app.routers import dlna
from basswiesn.app.services.dlna_library import ContentDirectory, SOAP

pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("unknown_total", [False, True])
def test_dlna_enable_connect_browse_import_mobile(engine, language, unknown_total, monkeypatch):
    fixture = MediaFixture()
    original = fixture.handle
    def handle(request):
        response = original(request)
        if unknown_total and request.url.path == "/ctl/ContentDir":
            action = ET.fromstring(request.content).find(f"{{{SOAP}}}Body")[0]
            if action.findtext("ObjectID") == "0":
                root = ET.fromstring(response.content)
                browse = root.find(f"{{{SOAP}}}Body")[0]
                browse.find("TotalMatches").text = "0"
                if action.findtext("StartingIndex") == "1":
                    browse.find("NumberReturned").text = "0"
                    browse.find("Result").text = ""
                return httpx.Response(200, content=ET.tostring(root))
        return response
    monkeypatch.setattr(fixture, "handle", handle)
    monkeypatch.setattr(dlna, "client_factory", lambda: ContentDirectory(transport=httpx.MockTransport(fixture.handle)))
    monkeypatch.setattr(dlna, "relay_slots", asyncio.Semaphore(4))
    with _LiveServer() as server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={"ui_mode":"lab", "web_language":language,
            "show_startup_warning":"false", "first_run_warning_required":"false"}).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width":430,"height":932}, is_mobile=True, has_touch=True)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(server.url + "/") else route.abort())
        page.goto(server.url, wait_until="networkidle")
        assert fixture.calls == []
        page.locator(".advanced-nav summary").click()
        page.locator('.advanced-nav [data-view="media"]').click()
        page.locator("#dlna-enabled").check()
        field = page.locator("#dlna-description-url")
        expect(field).to_be_enabled()
        field.fill(BASE + "/root.xml")
        page.locator("#dlna-connect-form button").click()
        expect(page.locator("#dlna-items")).to_contain_text("Music")
        if unknown_total:
            expect(page.locator("#dlna-library-panel")).to_contain_text("keine Gesamtzahl" if language=="de" else "not reported a total")
            page.locator('[data-dlna="next"]').click()
            expect(page.locator("#dlna-items")).to_contain_text("Keine weiteren Einträge" if language=="de" else "No more entries")
            expect(page.locator('[data-dlna="next"]')).to_be_disabled()
            page.locator('[data-dlna="root"]').click()
            expect(page.locator("#dlna-items")).to_contain_text("Music")
        page.locator('[data-dlna="open"]').click()
        expect(page.locator("#dlna-items")).to_contain_text("Example Artist")
        page.locator('[data-dlna="import"]').click()
        expect(page.locator("#dlna-status")).to_contain_text("gespeichert" if language=="de" else "saved")
        assert not any(path == "/audio.mp3" for _,path in fixture.calls)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        text = page.locator("#dlna-library-panel").inner_text()
        assert ("Musikbibliothek aktivieren" if language=="de" else "Enable media library") in text
        assert ("Enable media library" if language=="de" else "Musikbibliothek aktivieren") not in text
        expect(page.locator("#media-technical-details")).to_be_visible()
        assert not page.locator("#media-technical-details").evaluate("node => node.open")
        for theme in ["light", "dark"]:
            page.locator("[data-theme-select]").select_option(theme)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            folder = os.environ.get("BASSWIESN_UI_ARTIFACT_DIR")
            if folder:
                Path(folder).mkdir(parents=True, exist_ok=True)
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path=str(Path(folder) / f"dlna-{engine}-{language}-{theme}.png"), full_page=True)
        page.locator('[data-dlna="back"]').click()
        expect(page.locator("#dlna-items")).to_contain_text("Music")
        page.locator("#dlna-enabled").uncheck()
        expect(page.locator('[data-dlna="browse"]')).to_be_disabled()
        page.locator("#ui-mode-switch").select_option("lab")
        expect(page.locator("#media-technical-details")).to_be_visible()
        assert not page.locator("#media-technical-details").evaluate("node => node.open")
        page.locator("#ui-mode-switch").select_option("standard")
        expect(page.locator("#view-media")).to_be_hidden()
        assert errors == []
        browser.close()
