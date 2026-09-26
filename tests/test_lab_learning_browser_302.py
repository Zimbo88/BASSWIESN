"""Human browser interaction with fictional state; no private network access."""
import json
import subprocess
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

from basswiesn.app.main import create_web_app
from test_mobile_release import _LiveServer

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.release]


def configured_server(mode="lab", language="en"):
    server = _LiveServer()
    server.server.config.app = create_web_app(background_tasks=False)
    return server


def settings(server, mode="lab", language="en"):
    httpx.post(server.url + "/api/system/settings", json={
        "ui_mode": mode, "web_language": language,
        "show_startup_warning": "false", "first_run_warning_required": "false",
    }).raise_for_status()


@pytest.mark.parametrize("engine,width,language", [("chromium",390,"de"),("webkit",430,"en"),("chromium",1440,"fr")])
def test_simulator_profiles_and_preview_are_offline(engine, width, language, tmp_path):
    server = configured_server()
    with server, sync_playwright() as pw:
        settings(server, language=language)
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width":width,"height":932})
        calls, errors, unexpected = [], [], []
        page.on("pageerror", lambda err: errors.append(str(err)))
        def route(r):
            if not r.request.url.startswith(server.url + "/"):
                unexpected.append(r.request.url); return r.abort()
            if "/api/lab/learning/" in r.request.url:
                calls.append((r.request.method,r.request.url,r.request.post_data))
            return r.continue_()
        page.route("**/*",route)
        page.clock.install()
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function("document.body.classList.contains('lab-mode')")
        assert calls == []
        page.locator(".advanced-nav summary").click()
        page.locator('[data-view="lab"]').click()
        panel = page.locator("#lab-learning")
        expect(panel).to_be_visible()
        panel.locator("#learning-open").click()
        expect(panel.locator("#learning-next")).to_be_enabled()
        scenarios=panel.locator("#learning-scenario option").evaluate_all("opts=>opts.map(o=>o.value)")
        assert len(scenarios) == 7
        for scenario in scenarios:
            panel.locator("#learning-scenario").select_option(scenario)
            expect(panel.locator("#learning-next")).to_be_enabled()
            for step in [2,3,4]:
                panel.locator("#learning-next").click()
                expect(panel.locator("#learning-result")).to_contain_text(f"{step} / 4")
            expect(panel.locator("#learning-next")).to_be_disabled()
            panel.locator("#learning-reset").click()
            expect(panel.locator("#learning-result")).to_contain_text("1 / 4")
        name = '<img src=x onerror="BAD=1">'
        panel.locator("#learning-name").fill(name)
        panel.locator("#learning-up-clock").focus()
        page.keyboard.press("Enter")
        for sample in ["normal","missing","long"]:
            panel.locator("#learning-sample").select_option(sample)
            panel.locator("#learning-preview").click()
            expect(panel.locator("#learning-save")).to_be_enabled()
            preview=panel.locator("#learning-preview-output").inner_text()
            assert "20:15" in preview and len(preview)<=256
        # Two events in the same task must not submit the save twice.
        panel.locator("#learning-save").evaluate("b=>{b.click();b.click()}")
        expect(panel.locator("#learning-load-0")).to_be_visible()
        saves=[row for row in calls if row[0]=="POST" and row[1].endswith("/display-profiles")]
        assert len(saves)==1
        assert panel.locator("img").count()==0 and page.evaluate("window.BAD") is None
        panel.locator("#learning-name").fill("Draft only")
        panel.locator("#learning-load-0").click()
        expect(panel.locator("#learning-name")).to_have_value(name)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        panel.screenshot(path=str(tmp_path/f"learning-{engine}-{language}.png"))
        page.once("dialog",lambda d:d.dismiss())
        panel.locator("#learning-remove-0").click()
        assert not [row for row in calls if row[0]=="DELETE"]
        page.once("dialog",lambda d:d.accept())
        panel.locator("#learning-remove-0").click()
        expect(panel.locator("#learning-load-0")).to_have_count(0)
        page.locator("#ui-mode-switch").select_option("standard")
        expect(panel).not_to_be_visible()
        count=len(calls)
        page.clock.fast_forward(20000)
        assert len(calls)==count
        assert httpx.get(server.url+"/api/lab/learning/display-profiles").status_code==409
        assert errors==[] and unexpected==[]
        browser.close()


@pytest.mark.parametrize("engine,mode,width", [("chromium","easy",390),("webkit","standard",430),("chromium","lab",1366)])
def test_reading_keyboard_preferences_and_reflow(engine, mode, width, tmp_path):
    server=configured_server()
    with server, sync_playwright() as pw:
        settings(server,mode)
        browser=getattr(pw,engine).launch(headless=True)
        page=browser.new_page(viewport={"width":width,"height":932})
        page.route("**/*",lambda r:r.continue_() if r.request.url.startswith(server.url+"/") else r.abort())
        page.goto(server.url,wait_until="networkidle")
        page.keyboard.press("Tab")
        expect(page.locator(".skip-content")).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("main")).to_be_focused()
        assert page.locator(".skip-content").bounding_box()["y"] < 0
        expect(page.locator("#lab-learning")).not_to_be_visible()
        page.locator(".reading-preferences summary").click()
        page.locator("#reading-size").select_option("largest")
        page.locator("#reading-contrast").check()
        expect(page.locator("html")).to_have_attribute("data-text-size","largest")
        expect(page.locator("html")).to_have_attribute("data-contrast","high")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        for theme in ["light","dark"]:
            page.locator("[data-theme-select]").select_option(theme)
            # Text/background of the actual reading panel, not just variable names.
            colors=page.locator(".reading-preferences").evaluate("el=>{let s=getComputedStyle(el);return [s.color,getComputedStyle(document.body).backgroundColor]}")
            assert colors[0] != colors[1]
        page.screenshot(path=str(tmp_path/f"reading-{engine}-{mode}.png"), full_page=True)
        page.reload(wait_until="networkidle")
        expect(page.locator("#reading-size")).to_have_value("largest")
        expect(page.locator("#reading-contrast")).to_be_checked()
        # Approximate narrow reflow after zoom, in addition to enlarged font.
        page.set_viewport_size({"width":320,"height":844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        browser.close()


def test_new_native_safety_copy_has_all_28_languages():
    report=json.loads(subprocess.check_output(["node","tools/audit_languages.js"],text=True))
    assert not report["errors"] and len(report["languages"])==28
    for row in report["languages"].values():
        assert row["offline_tools_native_entries"] >= 50
        assert not set(row["offline_tools_english_fallback"]) & {
            "delete_question","error_forbidden","error_timeout","error_server",
            "help_lab","help_learning","help_backup","help_restart","help_privacy",
        }
