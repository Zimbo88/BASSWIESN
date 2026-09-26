"""Real Chromium/WebKit interactions, offline HTTPS browser with mocked API."""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

pytestmark = [pytest.mark.browser, pytest.mark.integration]
TOKEN = "A" * 43


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("language", ["de", "en"])
def test_secure_install_and_reload_only_poll_never_resubmit(engine, language):
    script = Path("basswiesn/app/static/js/update-admin.js").read_text()
    catalogs = {f"/{name}.js": Path(f"basswiesn/app/static/js/{name}.js").read_text()
                for name in ("translations", "language-extension", "locale-301")}
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width":430,"height":932})
        calls, errors = [], []
        state = {"state":"IDLE", "executor_available":True}
        page.on("pageerror", lambda err: errors.append(str(err)))
        def route(r):
            path = r.request.url.removeprefix("https://example.invalid")
            if path == "/":
                assets = ''.join(f'<script src="{name}"></script>' for name in catalogs)
                return r.fulfill(content_type="text/html",body=f'<html lang="{language}"><body><p data-i18n="update_install_boundary"></p>{assets}<script>BasswiesnI18n.setLanguage(document.documentElement.lang)</script><script src="/update.js"></script></body></html>')
            if path in catalogs:
                return r.fulfill(content_type="application/javascript",body=catalogs[path])
            if path == "/update.js":
                return r.fulfill(content_type="application/javascript",body=script)
            if path == "/api/update/admin/status":
                return r.fulfill(json={"ok":True,"installation_available":True,"result":state})
            if path == "/api/update/admin/install":
                calls.append(r.request.post_data_json)
                assert r.request.headers["origin"] == "https://example.invalid"
                assert r.request.headers["x-basswiesn-update"] == "1"
                state.update(state="STAGING",request_id=calls[-1]["request_id"],target_version="3.0.0")
                return r.abort()  # response lost AFTER durable reservation
            return r.abort()
        page.route("**/*",route)
        page.goto("https://example.invalid")
        assert calls == []
        page.evaluate("window.dispatchEvent(new CustomEvent('basswiesn:official-release',{detail:{status:'update_available',local_version:'2.6.5',remote_version:'3.0.0',package_assets_present:true}}))")
        expect(page.locator("#update-admin-code")).to_be_enabled()
        page.locator("#update-admin-code").fill(TOKEN)
        page.locator("#update-admin-confirm").check()
        page.locator("#update-admin-install").click()
        expect(page.locator("#update-admin-code")).to_have_value("")
        assert len(calls) == 1
        assert TOKEN not in page.evaluate("JSON.stringify(localStorage)+JSON.stringify(sessionStorage)")
        page.reload()
        expect(page.locator("#update-admin-status")).to_contain_text("Release" if language == "de" else "release")
        expect(page.locator("#update-admin-install")).to_be_disabled()
        assert len(calls) == 1
        state.update(state="RESTORED")
        page.locator("#update-admin-refresh").click()
        expect(page.locator("#update-admin-status")).to_contain_text("wiederhergestellt" if language == "de" else "restored")
        assert page.evaluate("sessionStorage.getItem('basswiesn.update.request')") is None
        page.evaluate("language => { BasswiesnI18n.setLanguage(language); document.documentElement.lang = language; }", "en" if language == "de" else "de")
        expect(page.locator("#update-admin-status")).to_contain_text("restored" if language == "de" else "wiederhergestellt")
        assert len(calls) == 1 and errors == []
        browser.close()
