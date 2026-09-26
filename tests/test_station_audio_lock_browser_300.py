"""Real browser and API: a blocked station start stays silent and explained."""
import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

from basswiesn.app.routers import stations_presets
from test_human_ui_160 import LiveServer
from test_station_audio_lock_300 import _no_transport, _seed


pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("language", ["de", "en"])
def test_locked_station_click_shows_localized_recovery_without_radio(monkeypatch, engine, language):
    station_id = _seed("explicit")
    monkeypatch.setattr(stations_presets, "SoundTouchClient", _no_transport)
    with LiveServer() as server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={"ui_mode": "lab", "web_language": language,
            "show_startup_warning": "false", "first_run_warning_required": "false"}).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width":430,"height":932}, is_mobile=True, has_touch=True)
        page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(server.url + "/") else r.abort())
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function('document.querySelector("#local-test-summary")?.children.length > 0')
        nav = page.locator('[data-view="stations"]')
        if not nav.is_visible():
            page.locator('.advanced-nav summary').click()
        nav.click()
        page.locator('#station-play-select').select_option(str(station_id))
        form = page.locator('#station-play-form')
        form.locator('input[name="dry_run"]').uncheck()
        previous_output = page.locator('#station-play-output').inner_text()
        page.once('dialog', lambda d: d.accept())
        with page.expect_response(lambda r: r.url.endswith(f'/stations/{station_id}/play')) as result:
            form.locator('button[type="submit"]').click()
        assert result.value.status == 409
        expected = ("Wiedergabe gesperrt. Bitte zuerst die Audio-Sicherheitsprüfung im Setup ausführen."
                    if language == "de" else "Playback is locked. Run the audio safety check in Setup before trying again.")
        expect(page.locator('#app-toast')).to_contain_text(expected)
        assert page.locator('#station-play-output').inner_text() == previous_output
        browser.close()
