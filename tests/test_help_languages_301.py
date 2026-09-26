"""Language and guide acceptance. Loopback/fixtures only; no hardware."""
import json
import subprocess
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

from basswiesn.app.main import create_web_app
from test_mobile_release import _LiveServer


@pytest.mark.unit
def test_all_28_languages_have_complete_native_guides_and_english_fallback():
    script = r'''
    global.window={};
    for(const f of ['translations','language-extension','locale-301','help-content'])
      require('./basswiesn/app/static/js/'+f+'.js');
    const i=window.BasswiesnI18n, out={};
    for (const lang of i.languages) {
      i.setLanguage(lang);
      const row=window.BasswiesnHelpContent[lang];
      out[lang]={words:Object.keys(i.vocabulary[lang]).length, row,
        terms:i.extraTerms[lang].length,
        label:i.word('tutorial'), safe:i.word('passive'),
        scoped:i.scoped({en:{title:'Artist',missing:'A deliberately untranslated explanation.',
          protocol:'LOCAL_INTERNET_RADIO'},de:{title:'Interpret',missing:'Deutsche Erklärung.'}})};
    }
    i.setLanguage('unsupported'); out.fallback=i.word('tutorial');
    console.log(JSON.stringify(out));
    '''
    data = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert data.pop("fallback") == "Tutorial"
    assert len(data) == 28
    for lang, value in data.items():
        assert value["words"] == 52
        assert value["terms"] == 35
        assert len(value["row"]) == 10
        assert value["label"] and value["safe"]
        assert all(len(steps) == 3 and all(text.strip() for text in steps) for steps in value["row"].values())
        if lang != "en":
            assert all(steps != data["en"]["row"][key] for key, steps in value["row"].items())
        assert value["scoped"]["protocol"] == "LOCAL_INTERNET_RADIO"
        assert value["scoped"]["missing"] == ("Deutsche Erklärung." if lang == "de" else "A deliberately untranslated explanation.")


@pytest.mark.browser
@pytest.mark.integration
@pytest.mark.parametrize("engine,width", [("chromium",390),("webkit",430)])
def test_all_languages_help_and_tutorial_clicks_are_inert_and_mobile_safe(engine, width, tmp_path):
    server = _LiveServer()
    server.server.config.app = create_web_app(background_tasks=False)
    with server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={"ui_mode":"lab", "web_language":"en",
            "show_startup_warning":"false", "first_run_warning_required":"false"}).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width":width,"height":932}, is_mobile=True, has_touch=True)
        errors, calls = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(server.url + "/") else route.abort())
        page.clock.install()
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function("document.body.classList.contains('lab-mode')")
        page.locator(".advanced-nav summary").click()
        page.locator('[data-view="lab"]').click()
        page.clock.pause_at(datetime.now(UTC) + timedelta(seconds=1))
        page.wait_for_load_state("networkidle")
        page.on("request", lambda req: calls.append(req.url))
        languages = page.evaluate("BasswiesnI18n.languages")
        for lang in languages:
            # Same language application as saved settings, without background
            # writes that would obscure this guide's zero-request assertion.
            page.evaluate("lang=>{BasswiesnI18n.setLanguage(lang);document.documentElement.lang=lang}", lang)
            trigger = page.locator('[data-guide-page="lab"]')
            trigger.click()
            dialog = page.locator("#page-help")
            expect(dialog).to_be_visible()
            assert dialog.get_attribute("open") is not None
            tutorial = page.evaluate("BasswiesnI18n.word('tutorial')")
            dialog.get_by_role("button", name=tutorial, exact=True).click()
            if lang in {"en", "de", "fr", "zh-Hant"}:
                dialog.screenshot(path=str(tmp_path / f"guide-{engine}-{lang}.png"))
            for step in [1,2,3]:
                expect(dialog.locator("#guide-step-title")).to_contain_text(f"{step} / 3")
                assert page.evaluate("document.querySelector('#page-help').scrollWidth <= document.querySelector('#page-help').clientWidth + 1")
                label = page.evaluate("BasswiesnI18n.word('done')" if step == 3 else "BasswiesnI18n.word('next')")
                dialog.get_by_role("button", name=label, exact=True).click()
            expect(dialog).not_to_be_visible()
            # Mobile browser focus restoration and keyboard dismissal.
            trigger.click(); page.keyboard.press("Escape")
            expect(dialog).not_to_be_visible()
        assert calls == [] and errors == []
        browser.close()


@pytest.mark.unit
def test_language_audit_is_a_release_gate():
    result = subprocess.run(["node", "tools/audit_languages.js", "--check"], capture_output=True, text=True, check=True)
    assert "PASS: 28 locales" in result.stdout


@pytest.mark.browser
@pytest.mark.integration
@pytest.mark.parametrize("mode", ["easy","standard","lab"])
def test_help_available_in_every_mode_without_running_setup(mode):
    server = _LiveServer(); server.server.config.app = create_web_app(background_tasks=False)
    with server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={"ui_mode":mode,"web_language":"de",
            "show_startup_warning":"false","first_run_warning_required":"false"}).raise_for_status()
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(server.url + "/") else r.abort())
        page.goto(server.url, wait_until="networkidle")
        page.locator('[data-view="setup"]').click()
        page.locator('[data-guide-page="setup"]').click()
        expect(page.locator("#page-help")).to_contain_text("sendet keine Befehle")
        page.locator("#page-help").get_by_role("button",name="Anleitung",exact=True).click()
        expect(page.locator(".guide-step")).to_contain_text("keine automatische Discovery")
        page.keyboard.press("Escape")
        browser.close()
