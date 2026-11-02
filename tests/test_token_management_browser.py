import pytest
from playwright.sync_api import sync_playwright, expect

from basswiesn.app.core.api_tokens import ApiTokenStore
from tests.test_admin_browser import secured_server, PASSWORD

pytestmark = pytest.mark.browser


def login(page, base):
    page.goto(base + "/auth/login")
    page.locator("#username").fill("operator")
    page.locator("#password").fill(PASSWORD)
    page.locator("#login-submit").click()
    page.wait_for_url(base + "/")
    page.locator("#admin-tokens-link").wait_for(state="attached")
    page.goto(base + "/auth/automation")
    expect(page.locator("#token-submit")).to_be_enabled()


@pytest.mark.parametrize("engine,width,height,locale", [
    ("chromium", 1920, 1080, "en-US"), ("chromium", 1440, 900, "de-DE"),
    ("chromium", 390, 844, "en-US"), ("webkit", 393, 852, "de-DE"),
    ("webkit", 430, 932, "fr-FR"),
])
def test_token_lifecycle_and_no_browser_storage(tmp_path, monkeypatch, engine, width, height, locale):
    with secured_server(tmp_path, monkeypatch) as (base, store, writes), sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height}, locale=locale)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())
        login(page, base)
        assert page.locator("html").get_attribute("lang") == ("de" if locale.startswith("de") else "en")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("#token-name").fill("Office automation")
        page.locator("#token-create").evaluate("form => { form.requestSubmit(); form.requestSubmit(); }")
        expect(page.locator(".token-card")).to_have_count(1)
        issued = page.locator("#token-secret").input_value()
        records = ApiTokenStore(store).list()
        assert len(records) == 1 and records[0].scopes == {"read"}
        assert ApiTokenStore(store).verify(issued, touch=False) is not None
        for storage in ("localStorage", "sessionStorage"):
            assert issued not in page.evaluate(f"JSON.stringify({storage})")
        expect(page.locator("#token-submit")).to_be_enabled()
        page.locator('[data-action="rotate"]').click()
        expect(page.locator("#token-submit")).to_be_enabled()
        rotated = page.locator("#token-secret").input_value()
        assert issued != rotated
        assert ApiTokenStore(store).verify(issued) is None
        assert ApiTokenStore(store).verify(rotated) is not None
        assert records[0].expires_at == ApiTokenStore(store).list()[0].expires_at
        page.locator("#clear-secret").click()
        expect(page.locator("#new-secret")).to_be_hidden()
        assert page.locator("#token-secret").input_value() == ""
        page.locator('[data-action="revoke"]').click()
        expect(page.locator("#token-submit")).to_be_enabled()
        expect(page.locator('[data-action="revoke"]')).to_be_disabled()
        assert ApiTokenStore(store).verify(rotated) is None
        assert writes == [] and errors == []
        browser.close()


def test_lost_response_blocks_replay_until_explicit_refresh(tmp_path, monkeypatch):
    with secured_server(tmp_path, monkeypatch) as (base, store, writes), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
        page = context.new_page()
        login(page, base)
        requests = []
        def lose_response(route):
            if route.request.method == "POST":
                requests.append("POST")
                # The operation happened. Only the reply was lost.
                result = route.fetch()
                assert result.status == 201
                route.abort()
            else:
                route.continue_()
        context.route(base + "/auth/tokens", lose_response)
        page.locator("#token-name").fill("Lost response")
        page.locator("#token-submit").click()
        expect(page.locator("#token-status")).to_contain_text("Outcome not confirmed")
        expect(page.locator("#token-submit")).to_be_disabled()
        assert len(ApiTokenStore(store).list()) == 1
        assert page.locator("#token-secret").input_value() == ""
        assert requests == ["POST"]
        page.locator("#token-refresh").click()
        expect(page.locator(".token-card")).to_have_count(1)
        expect(page.locator("#token-submit")).to_be_enabled()
        assert requests == ["POST"] and writes == []
        browser.close()


def test_navigation_clears_secret_and_late_read_cannot_restore_it(tmp_path, monkeypatch):
    with secured_server(tmp_path, monkeypatch) as (base, store, _), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
        page = context.new_page()
        login(page, base)
        page.locator("#token-name").fill("Navigation test")
        page.locator("#token-submit").click()
        expect(page.locator(".token-card")).to_have_count(1)
        assert page.locator("#token-secret").input_value().startswith("bwapi_")
        page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        expect(page.locator("#new-secret")).to_be_hidden()
        assert page.locator("#token-secret").input_value() == ""
        page.locator("#token-create").evaluate("form => form.requestSubmit()")
        assert len(ApiTokenStore(store).list()) == 1
        browser.close()


def test_delayed_mutation_body_never_redisplays_secret_after_pagehide(tmp_path, monkeypatch):
    with secured_server(tmp_path, monkeypatch) as (base, store, _), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
        page = context.new_page()
        login(page, base)
        # Header arrived, but its body resolves only after navigation. This
        # deterministic fixture does not mint a credential or contact a radio.
        page.evaluate("""() => {
          BasswiesnAuth.fetch = async () => ({ok: true,
            json: () => new Promise(resolve => { window.finishBody = resolve; })});
        }""")
        page.locator("#token-name").fill("Delayed body")
        page.locator("#token-submit").click()
        page.wait_for_function("typeof finishBody === 'function'")
        page.evaluate("""async () => {
          window.dispatchEvent(new Event('pagehide'));
          finishBody({token: 'bwapi_OFFLINE_SYNTHETIC_NOT_A_CREDENTIAL'});
          await new Promise(resolve => setTimeout(resolve, 0));
        }""")
        expect(page.locator("#new-secret")).to_be_hidden()
        assert page.locator("#token-secret").input_value() == ""
        assert ApiTokenStore(store).list() == []
        browser.close()


def test_stale_revoke_readback_is_not_reported_as_success(tmp_path, monkeypatch):
    with secured_server(tmp_path, monkeypatch) as (base, store, _), sync_playwright() as playwright:
        issued = ApiTokenStore(store).issue("Stale readback", ["read"], 86400)
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        login(page, base)
        context.route(base + "/auth/tokens", lambda route: route.fulfill(json={"tokens": [issued.record.public()]}))
        page.locator('[data-action="revoke"]').click()
        expect(page.locator("#token-status")).to_contain_text("Outcome not confirmed")
        expect(page.locator("#token-submit")).to_be_disabled()
        assert ApiTokenStore(store).verify(issued.token) is None
        browser.close()


def test_backgrounding_invalidates_delayed_secret_even_after_return(tmp_path, monkeypatch):
    with secured_server(tmp_path, monkeypatch) as (base, store, _), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
        page = context.new_page()
        login(page, base)
        page.evaluate("""() => {
          window.calls = 0;
          BasswiesnAuth.fetch = async () => {
            calls++;
            return {ok: true, json: () => new Promise(resolve => { window.finishBody = resolve; })};
          };
        }""")
        page.locator("#token-name").fill("Background test")
        page.locator("#token-submit").click()
        page.wait_for_function("typeof finishBody === 'function'")
        page.evaluate("""async () => {
          Object.defineProperty(document, 'hidden', {value: true, configurable: true});
          document.dispatchEvent(new Event('visibilitychange'));
          Object.defineProperty(document, 'hidden', {value: false, configurable: true});
          document.dispatchEvent(new Event('visibilitychange'));
          finishBody({token: 'bwapi_' + 'a'.repeat(32) + '_' + 'b'.repeat(43), id: 'a'.repeat(32)});
          await new Promise(resolve => setTimeout(resolve, 0));
        }""")
        expect(page.locator("#new-secret")).to_be_hidden()
        expect(page.locator("#token-submit")).to_be_disabled()
        expect(page.locator("#token-refresh")).to_be_enabled()
        expect(page.locator("#token-status")).to_contain_text("Outcome not confirmed")
        assert page.locator("#token-secret").input_value() == ""
        assert page.evaluate("calls") == 1 and ApiTokenStore(store).list() == []
        browser.close()


@pytest.mark.parametrize("mismatch", ["selected_grant", "token_envelope"])
def test_rotation_requires_selected_grant_and_credential_identity(tmp_path, monkeypatch, mismatch):
    with secured_server(tmp_path, monkeypatch) as (base, store, _), sync_playwright() as playwright:
        first = ApiTokenStore(store).issue("Selected grant", ["read"], 86400)
        second = ApiTokenStore(store).issue("Other grant", ["read"], 86400)
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        login(page, base)
        reply = (second if mismatch == "selected_grant" else first).record.public()
        reply["token"] = second.token
        context.route(base + f"/auth/tokens/{first.record.id}/rotate", lambda route: route.fulfill(json=reply))
        page.locator(f'[data-token-id="{first.record.id}"] [data-action="rotate"]').click()
        expect(page.locator("#token-status")).to_contain_text("Outcome not confirmed")
        expect(page.locator("#token-submit")).to_be_disabled()
        expect(page.locator("#new-secret")).to_be_hidden()
        assert page.locator("#token-secret").input_value() == ""
        assert ApiTokenStore(store).verify(first.token, touch=False) is not None
        browser.close()
