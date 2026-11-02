from contextlib import contextmanager
import threading
import time

import httpx
import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from basswiesn.app.config import get_settings
from basswiesn.app.core.admin_store import AdminStore
from basswiesn.app.main import create_web_app
from tests.test_mobile_release import _free_port

pytestmark = pytest.mark.browser
PASSWORD = "offline example passphrase only"


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_dashboard_script_policy_blocks_injected_scripts_and_eval(tmp_path, monkeypatch, engine):
    with secured_server(tmp_path, monkeypatch) as (base, store, writes), sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch(headless=True)
        context = browser.new_context()
        foreign = []
        # Browser automation evaluates in a privileged realm; load a same-origin
        # script normally so eval and injected scripts are governed by the CSP.
        probe_script = """(async () => {
          window.cspViolations = [];
          document.addEventListener('securitypolicyviolation', event => cspViolations.push(event.violatedDirective));
          let blockedEval = false;
          try { window.eval('window.evaluatedCanary = true'); } catch { blockedEval = true; }
          const inline = document.createElement('script');
          inline.textContent = 'window.inlineCanary = true';
          document.body.append(inline);
          const external = document.createElement('script');
          external.src = 'https://example.invalid/disallowed.js';
          document.body.append(external);
          const button = document.createElement('button');
          button.setAttribute('onclick', 'window.handlerCanary = true');
          document.body.append(button); button.click();
          await new Promise(resolve => setTimeout(resolve, 100));
          window.cspResult = {blockedEval, evaluated: !!window.evaluatedCanary,
            inline: !!window.inlineCanary, handler: !!window.handlerCanary, violations:cspViolations};
        })();"""
        def route(request):
            if request.request.url == base + "/static/csp-runtime-canary.js":
                return request.fulfill(content_type="application/javascript", body=probe_script)
            if request.request.url.startswith(base + "/"):
                return request.continue_()
            foreign.append(request.request.url)
            return request.abort()
        context.route("**/*", route)
        page = context.new_page()
        page.goto(base + "/auth/login")
        page.locator("#username").fill("operator")
        page.locator("#password").fill(PASSWORD)
        page.locator("#login-submit").click()
        page.wait_for_url(base + "/")
        page.wait_for_function("() => Boolean(state.systemHealth)")
        page.evaluate("""() => {
          const probe = document.createElement('script');
          probe.src = '/static/csp-runtime-canary.js';
          document.body.append(probe);
        }""")
        page.wait_for_function("() => Boolean(window.cspResult)")
        result = page.evaluate("() => window.cspResult")
        assert result["blockedEval"] is True
        assert not any(result[key] for key in ("evaluated", "inline", "handler"))
        assert len(result["violations"]) >= 3
        assert not any("example.invalid/disallowed.js" in url for url in foreign)
        assert writes == []
        browser.close()


@contextmanager
def secured_server(tmp_path, monkeypatch):
    settings = get_settings()
    base = f"http://127.0.0.1:{_free_port()}"
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    settings.admin_auth_mode = "required"
    settings.admin_origins = (base,)
    settings.admin_allow_http = True  # explicitly loopback-only browser fixture
    store = AdminStore(tmp_path / "secrets" / "admin" / "auth.sqlite3")
    store.bootstrap("operator", PASSWORD)
    app = create_web_app(background_tasks=False)
    writes = []
    @app.post("/test/admin-action")
    def action():
        writes.append("performed")
        return {"ok": True}
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=int(base.rsplit(":", 1)[1]), proxy_headers=False, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(base + "/ready", timeout=0.2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("Loopback fixture failed to start")
        yield base, store, writes
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.parametrize("engine,width,height", [
    ("chromium", 1920, 1080), ("chromium", 1440, 900),
    ("webkit", 390, 844), ("webkit", 393, 852), ("webkit", 430, 932),
])
def test_authenticated_navigation_uses_real_csp_without_device_actions(tmp_path, monkeypatch, engine, width, height):
    with secured_server(tmp_path, monkeypatch) as (base, store, writes), sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height})
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
        page = context.new_page()
        errors, mutations, assets = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: mutations.append(request.url) if request.method not in {"GET", "HEAD"} else None)
        page.on("response", lambda response: assets.append(response.url) if "/static/" in response.url and response.status >= 400 else None)
        page.goto(base + "/auth/login")
        page.locator("#username").fill("operator")
        page.locator("#password").fill(PASSWORD)
        page.locator("#login-submit").click()
        page.wait_for_url(base + "/")
        page.wait_for_function("() => Boolean(state.systemHealth)")
        for mode in ("easy", "standard", "lab"):
            assert page.evaluate("""async mode => (await BasswiesnAuth.fetch('/api/system/settings', {
              method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
                ui_mode:mode, show_startup_warning:'false', first_run_warning_required:'false'
              })})).status""", mode) == 200
            page.reload(wait_until="networkidle")
            page.wait_for_function("() => Boolean(state.systemHealth)")
            menu = page.locator(".advanced-nav > summary")
            if menu.is_visible():
                menu.click()
            targets = page.locator(".topnav .nav-button:visible").evaluate_all(
                "buttons => buttons.map(button => ({view:button.dataset.view, advanced:!!button.closest('.advanced-nav')}))"
            )
            assert len(targets) >= 4
            for target in targets:
                advanced_open = page.locator(".advanced-nav[open]").count() == 1
                if menu.is_visible() and advanced_open != target["advanced"]:
                    menu.click()
                button = page.locator(f'.topnav .nav-button[data-view="{target["view"]}"]')
                button.scroll_into_view_if_needed()
                button.click()
                assert page.locator(f'#view-{target["view"]}.is-active').count() == 1
            assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth")
        assert errors == [] and assets == [] and writes == []
        # Settings here change only this isolated application's UI mode. Opening
        # any normal or LAB view must not enqueue a radio/probe operation.
        # This legacy POST records a browser service-status observation in the
        # local master log only; it neither probes nor mutates any radio. Keep
        # that exact exception separate from actual commands, never all POSTs.
        commands = [url for url in mutations if url != base + "/api/setup/wizard/service-status"]
        assert commands == [base + "/auth/login"] + [base + "/api/system/settings"] * 3
        browser.close()


@pytest.mark.parametrize("engine,language", [("chromium", "de-DE"), ("webkit", "en-US")])
def test_real_login_mutation_csrf_logout_mobile(tmp_path, monkeypatch, engine, language):
    with secured_server(tmp_path, monkeypatch) as (base, store, writes), sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch(headless=True)
        context = browser.new_context(viewport={"width": 390, "height": 844}, locale=language)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base)
        page.wait_for_url("**/auth/login")
        page.locator("#username").fill("operator")
        page.locator("#password").fill(PASSWORD)
        page.locator("#login-submit").click()
        page.wait_for_url(base + "/")
        page.locator("#admin-logout").wait_for()
        assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth")
        assert page.evaluate("document.cookie") == ""
        # Real server checks the synchronizer token supplied by the shared helper.
        assert page.evaluate("async () => (await BasswiesnAuth.fetch('/test/admin-action', {method:'POST'})).status") == 200
        assert writes == ["performed"]
        assert page.evaluate("async () => (await fetch('/test/admin-action', {method:'POST'})).status") == 403
        assert writes == ["performed"]
        cookies = context.cookies()
        session = next(row["value"] for row in cookies if row["name"] == "basswiesn_admin_session")
        assert session not in page.evaluate("JSON.stringify(localStorage)")
        assert session not in page.evaluate("JSON.stringify(sessionStorage)")
        page.locator("#admin-logout").click()
        page.wait_for_url("**/auth/login")
        assert store.session(session) is None
        assert errors == []
        browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_expired_session_no_automatic_replay_and_no_external_csrf(tmp_path, monkeypatch, engine):
    with secured_server(tmp_path, monkeypatch) as (base, store, writes), sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch(headless=True)
        context = browser.new_context()
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
        page = context.new_page()
        page.goto(base + "/auth/login")
        page.locator("#username").fill("operator")
        page.locator("#password").fill(PASSWORD)
        page.locator("#login-submit").click()
        page.wait_for_url(base + "/")
        page.locator("#admin-logout").wait_for()
        cookie = next(row["value"] for row in context.cookies() if row["name"] == "basswiesn_admin_session")
        store.logout(cookie)
        attempts = []
        page.on("request", lambda request: attempts.append(request.method) if request.url == base + "/test/admin-action" else None)
        # A dashboard read may already have observed expiry and invalidated the
        # cached CSRF/session promise. Both paths must refuse, never replay:
        # preflight refusal before POST, or a single POST rejected by the server.
        result = page.evaluate("""async () => {
          try { return {http: (await BasswiesnAuth.fetch('/test/admin-action', {method:'POST'})).status}; }
          catch (error) { return {blocked: error.message}; }
        }""")
        assert result in ({"http": 401}, {"blocked": "Administrator session unavailable; sign in again."})
        assert attempts in ([], ["POST"])
        assert writes == []
        # Replace fetch only inside this test to observe a cross-origin request
        # without actually contacting a host. Production never monkeypatches it.
        result = page.evaluate("""async () => {
          const original = window.fetch;
          let captured;
          window.fetch = async (url, options) => { captured = options; return new Response('{}'); };
          try { await BasswiesnAuth.fetch('https://provider.example.invalid/test', {method:'POST'}); }
          finally { window.fetch = original; }
          return new Headers(captured.headers).has('X-BASSWIESN-CSRF');
        }""")
        assert result is False
        assert writes == [] and attempts in ([], ["POST"])
        browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_navigation_while_session_is_loading_never_starts_queued_write(tmp_path, monkeypatch, engine):
    with secured_server(tmp_path, monkeypatch) as (base, store, writes), sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch(headless=True)
        context = browser.new_context()
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
        pending = []
        context.route("**/auth/session", lambda route: pending.append(route))
        page = context.new_page()
        attempts = []
        page.on("request", lambda request: attempts.append(request.method) if request.url == base + "/test/admin-action" else None)
        page.goto(base + "/auth/login")
        page.locator("#username").fill("operator")
        page.locator("#password").fill(PASSWORD)
        page.locator("#login-submit").click()
        page.wait_for_url(base + "/")
        page.wait_for_function("typeof BasswiesnAuth === 'object'")
        page.evaluate("() => { window.writeResult = BasswiesnAuth.fetch('/test/admin-action', {method:'POST'}).then(r => r.status).catch(e => e.name); }")
        page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        cookie = next(row["value"] for row in context.cookies() if row["name"] == "basswiesn_admin_session")
        session = store.session(cookie)
        assert pending
        pending.pop().fulfill(json={"enabled": True, "csrf_token": session.csrf_token})
        assert page.evaluate("writeResult") == "AbortError"
        assert attempts == [] and writes == []
        context.unroute_all(behavior="wait")
        browser.close()
