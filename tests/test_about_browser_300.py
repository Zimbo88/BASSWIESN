"""Authored text, explicit publication lookup and mobile layout; no LAN calls."""
import json
import os
from pathlib import Path

import httpx
import pytest
from basswiesn import __version__
from basswiesn.app import db as database
from basswiesn.app.models import RuntimeState
from playwright.sync_api import sync_playwright
from test_mobile_release import _LiveServer

pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("mode", ["easy", "standard", "lab"])
def test_about_original_statement_publication_date_and_manual_refresh(engine, language, mode):
    # Synthetic cached GitHub observation: never assume the running release has
    # an already-bundled publication date, and never invent one in product code.
    with database.SessionLocal() as db:
        db.add(RuntimeState(key="release_publication:" + __version__, value=json.dumps({
            "version": __version__, "published_at": "2026-01-15T08:09:10Z"})))
        db.commit()
    data = Path("basswiesn/app/static/js/about.js").read_text()
    story = json.loads(data.split("window.BasswiesnAbout = ", 1)[1].rstrip(";\n"))["stories"][language]
    with _LiveServer() as server, sync_playwright() as pw:
        assert httpx.post(server.url + "/api/system/settings", json={
            "ui_mode": mode, "web_language": language,
            "show_startup_warning": "false", "first_run_warning_required": "false"}).status_code == 200
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": 430, "height": 932}, is_mobile=True, has_touch=True)
        requests, errors = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(server.url + "/") else route.abort())
        def publication(route):
            requests.append(route.request.method)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "version": __version__, "publication_status": "PUBLISHED", "published_at": "2026-01-15T08:09:10Z",
                "refresh_status": "OK"}))
        page.route("**/api/version/publication-refresh", publication)
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function('document.querySelector("#local-test-summary")?.children.length > 0')
        page.locator('[data-view="about"]').click()
        assert requests == []  # Not on load or opening About.
        assert page.locator(".about-story > *").all_text_contents() == [block["text"] for block in story]
        assert "2026" in page.locator("#about-release-date").inner_text()
        assert "2026" in page.locator("#server-identity").inner_text()
        assert page.locator(".about-technical-note").count() == 1
        for theme in ("light", "dark"):
            page.locator("[data-theme-select]").select_option(theme)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            if folder := os.environ.get("BASSWIESN_UI_ARTIFACT_DIR"):
                Path(folder).mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(Path(folder) / f"about-{engine}-{language}-{mode}-{theme}.png"))
                if engine == "webkit" and mode == "easy":
                    page.locator(".about-story h3").first.scroll_into_view_if_needed()
                    page.screenshot(path=str(Path(folder) / f"story-{language}-{theme}.png"))
                    page.locator(".about-technical-note").scroll_into_view_if_needed()
                    page.screenshot(path=str(Path(folder) / f"status-{language}-{theme}.png"))
        page.locator("#about-publication-refresh").click()
        page.wait_for_function('document.querySelector("#about-publication-message").textContent.includes("Update") || document.querySelector("#about-publication-message").textContent.includes("update")')
        assert requests == ["POST"]
        assert ("kein Update installiert" if language == "de" else "No update was installed") in page.locator("#about-publication-message").inner_text()
        # No date from another version or the local clock may leak into a
        # not-yet-published development version.
        page.evaluate('state.applicationVersion = "3.0.0-unpublished"; renderAboutContent(); updateServerIdentity();')
        assert page.locator("#about-release-date").inner_text() == ("Datum noch nicht bekannt" if language == "de" else "Date not yet known")
        assert "2026" not in page.locator("#server-identity").inner_text()
        page.evaluate('state.systemSettings.lan_host = ""; state.setupWizardServer = {}; updateServerIdentity();')
        assert page.locator("#server-identity").get_attribute("title") == (
            "Keine sichere LAN Host-IP erkannt oder gesetzt." if language == "de"
            else "No safe LAN host IP detected or configured.")
        assert page.locator(".about-story > *").all_text_contents() == [block["text"] for block in story]
        assert errors == []
        browser.close()
