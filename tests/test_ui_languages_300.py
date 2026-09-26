"""UI languages, never firmware-language writes. Only isolated DB/loopback."""
import json
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from basswiesn.app.main import create_web_app
from basswiesn.app.routers.media import WEB_LANGUAGE_CODES, _detected_web_language, _normalize_web_language
from basswiesn.app.services.catalogs import STOCKHOLM_LANGUAGES
from test_mobile_release import _LiveServer


@pytest.mark.unit
@pytest.mark.parametrize("value,expected", [
    ("ko_KR.UTF-8", "ko"), ("th-TH", "th"), ("zh_hant", "zh-Hant"),
    ("zh_Hant_HK", "zh-Hant"), ("zh-TW", "zh-Hant"), ("zh-HK", "zh-Hant"),
    ("zh-MO", "zh-Hant"), ("zh-Hans-TW", "zh"), ("zh-CN", "zh"),
    ("zh_hans", "zh"), ("nb_NO", "no"), ("no-NO", "no"),
    ("de-DE", "de"), ("xx", None), ("C.UTF-8", None), ("en,fr", None)])
def test_normalize_ui_language_only(value, expected):
    assert _normalize_web_language(value) == expected


@pytest.mark.unit
def test_all_stock_languages_have_a_web_equivalent():
    assert all(_normalize_web_language(row["code"]) in WEB_LANGUAGE_CODES for row in STOCKHOLM_LANGUAGES)
    assert _detected_web_language("C.UTF-8", "zh-Hant-TW,zh;q=0.8,en;q=0.6") == "zh-Hant"
    assert _detected_web_language("de_DE.UTF-8", "ko-KR") == "de"
    assert _detected_web_language("C", "xx,ko;q=0,th;q=0.4,fr;q=0.8") == "fr"
    assert _detected_web_language("C", "ko;q=NaN,th;q=5,en;q=0") == "en"


@pytest.mark.unit
def test_native_core_safety_copy_and_explicit_fallback():
    program = r'''
    global.window={}; require('./basswiesn/app/static/js/translations.js');
    const unchanged = JSON.stringify([window.BasswiesnI18n.catalogs.de,window.BasswiesnI18n.catalogs.en]);
    require('./basswiesn/app/static/js/about.js'); require('./basswiesn/app/static/js/language-extension.js');
    const i=window.BasswiesnI18n, results={};
    for (const code of ['ko','th','zh-Hant','nb_NO','zh_hans']) {
      i.setLanguage(code); results[code]={normalized:i.normalizeLanguage(code),remote:i.phrase('Remote'),
        coverage:i.coverage(code),warning:i.t('first_run_p2'),notice:i.languageNotice(code)};
    }
    i.setLanguage('ko'); const korean=i.phrase('Device Settings');
    i.setLanguage('en'); const englishAgain=i.phrase(korean);
    console.log(JSON.stringify({results,englishAgain,unchanged:unchanged===JSON.stringify([i.catalogs.de,i.catalogs.en])}));
    '''
    data = json.loads(subprocess.check_output(["node", "-e", program], text=True))
    assert data["unchanged"] and data["englishAgain"] == "Device Settings"
    for code in ["ko", "th", "zh-Hant"]:
        value = data["results"][code]
        assert value["normalized"] == code and value["remote"] != "Remote"
        assert "Telnet" in value["warning"] and "Radio setup" not in value["warning"]
        assert not value["coverage"]["missing"] and value["coverage"]["sameAsEnglish"] > 0
        assert value["notice"]  # no false claim of complete native translation


@pytest.mark.integration
@pytest.mark.parametrize("language,canonical", [("ko", "ko"), ("th", "th"), ("zh_hant", "zh-Hant"), ("zh_hans", "zh"), ("nb", "no")])
def test_saved_ui_locale_does_not_change_firmware_locale(language, canonical):
    with TestClient(create_web_app()) as client:
        before = client.get("/api/system/settings").json()["device_language_default"]
        response = client.post("/api/system/settings", json={"web_language": language})
        assert response.status_code == 200
        assert response.json()["web_language"] == canonical
        assert response.json()["device_language_default"] == before
        assert client.get("/api/system/settings").json()["web_language"] == canonical
        assert client.post("/api/system/settings", json={"web_language":"not-supported"}).status_code == 400


@pytest.mark.browser
@pytest.mark.integration
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("language,label", [("ko", "설정"), ("th", "การตั้งค่า"), ("zh-Hant", "設定")])
def test_native_navigation_and_language_switch_on_mobile(engine, language, label):
    with _LiveServer() as server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={"ui_mode":"standard", "web_language":language,
            "show_startup_warning":"false", "first_run_warning_required":"false"}).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width":430,"height":932}, is_mobile=True, has_touch=True)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(server.url + "/") else route.abort())
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function("code => document.documentElement.lang === code", arg=language)
        page.locator('.topnav [data-view="system-settings"]').click()
        expect(page.locator("#view-system-settings .page-head h2")).to_have_text(label)
        expect(page.locator("#web-language-coverage-note")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("#web-language-select").select_option("en")
        page.locator('#system-settings-form button[type="submit"]').click()
        page.wait_for_function("document.documentElement.lang === 'en'")
        expect(page.locator("#view-system-settings .page-head h2")).to_have_text("Settings")
        expect(page.locator("#web-language-coverage-note")).to_be_hidden()
        page.reload(wait_until="networkidle")
        assert page.locator("html").get_attribute("lang") == "en"
        assert errors == []
        browser.close()


@pytest.mark.browser
@pytest.mark.integration
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_lost_settings_response_and_service_read_do_not_escape_or_replay(engine):
    # Real UI with deliberately dropped loopback responses. A lost POST is
    # uncertain, not permission to resubmit; failed reads must not remain online.
    with _LiveServer() as server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={"ui_mode":"standard", "web_language":"en",
            "show_startup_warning":"false", "first_run_warning_required":"false"}).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width":430,"height":932}, is_mobile=True, has_touch=True)
        errors, writes = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        def route(r):
            if not r.request.url.startswith(server.url + "/"):
                return r.abort()
            if r.request.url.endswith("/api/system/service-health"):
                return r.abort()
            if r.request.url.endswith("/api/system/settings") and r.request.method == "POST":
                writes.append(r.request.post_data_json)
                return r.abort()
            return r.continue_()
        page.route("**/*", route)
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function("document.documentElement.lang === 'en'")
        page.locator('.topnav [data-view="system-settings"]').click()
        page.locator("#web-language-select").select_option("de")
        save = page.locator('#system-settings-form button[type="submit"]')
        save.click()
        expect(save).to_be_enabled()
        expect(page.locator("#cloud-state")).to_contain_text("not confirmed")
        assert len(writes) == 1
        page.reload(wait_until="networkidle")
        expect(page.locator("html")).to_have_attribute("lang", "en")
        assert len(writes) == 1 and errors == []
        browser.close()
