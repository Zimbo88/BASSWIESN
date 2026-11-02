from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from basswiesn.app.config import get_settings
from basswiesn.app.core.admin_boundary import COOKIE, origin
from basswiesn.app.core.admin_store import AdminStore
from basswiesn.app.main import create_cloud_app, create_debug_app, create_web_app

PASSWORD = "offline example passphrase only"
pytestmark = pytest.mark.integration


@pytest.fixture
def secured(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "admin_auth_mode", "required")
    monkeypatch.setattr(settings, "admin_origins", ("https://testserver",))
    monkeypatch.setattr(settings, "device_allowed_peers", ("192.0.2.42",))
    store = AdminStore(tmp_path / "secrets" / "admin" / "auth.sqlite3")
    store.bootstrap("operator", PASSWORD)
    return settings, store


def authenticate(client):
    response = client.post("/auth/login", json={"username": "operator", "password": PASSWORD}, headers={"Origin": "https://testserver"})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


@pytest.mark.parametrize("factory,paths", [
    (create_web_app, ("/", "/remote/EXAMPLE", "/api/devices", "/api/devices/EXAMPLE/state", "/api/health", "/docs", "/redoc", "/openapi.json", "/api/unknown", "/auth/session")),
    (create_cloud_app, ("/api/cloud/stations", "/about", "/docs", "/openapi.json", "/api/devices", "/not-a-contract")),
    (create_debug_app, ("/", "/requests", "/diagnostics.json", "/health", "/docs", "/openapi.json")),
])
def test_anonymous_human_requests_rejected_on_every_listener(secured, factory, paths):
    client = TestClient(factory(), base_url="https://testserver")
    for path in paths:
        response = client.get(path)
        assert response.status_code == 401, (path, response.text)
        assert response.json()["error"]["code"] == "AUTH_REQUIRED"


@pytest.mark.parametrize("factory", [create_web_app, create_cloud_app, create_debug_app])
def test_minimal_health_is_public_not_configuration(secured, factory):
    client = TestClient(factory(), base_url="http://localhost")
    assert client.get("/live").json() == {"live": True}
    assert client.get("/ready").json() == {"ready": True}


@pytest.mark.parametrize("factory", [create_web_app, create_cloud_app])
def test_exact_device_protocols_still_anonymous(secured, factory):
    client = TestClient(factory(), base_url="http://device-service.invalid", client=("192.0.2.42", 41000))
    response = client.get("/streaming/sourceproviders")
    assert response.status_code == 200
    assert "sourceProviders" in response.text or "sourceproviders" in response.text.lower()
    response = client.post("/streaming/support/power_on", content="<test/>")
    assert response.status_code == 200
    # A nearby path or another method must not inherit a prefix exemption.
    for method, path in [("DELETE", "/streaming/sourceproviders"), ("GET", "/streaming/sourceproviders/admin"), ("OPTIONS", "/streaming/support/power_on")]:
        assert client.request(method, path).status_code == 403
    assert client.post("/streaming/support/power_on", content="<test/>", headers={"Origin": "https://testserver"}).status_code == 403


def test_session_csrf_rotation_logout_and_no_sensitive_cache(secured):
    app = create_web_app(background_tasks=False)
    calls = []
    @app.post("/test/admin-action")
    def action():
        calls.append("action")
        return {"ok": True}
    client = TestClient(app, base_url="https://testserver")
    token = authenticate(client)
    old_cookie = client.cookies.get(COOKIE)
    response = client.get("/auth/session")
    assert response.json()["csrf_token"] == token
    assert response.headers["cache-control"] == "no-store"
    assert "HttpOnly" in client.post("/auth/login", json={"username": "operator", "password": PASSWORD}, headers={"Origin": "https://testserver"}).headers["set-cookie"]
    assert client.cookies.get(COOKIE) != old_cookie
    assert secured[1].session(old_cookie) is None
    token = client.get("/auth/session").json()["csrf_token"]
    for headers in ({}, {"Origin": "https://evil.invalid", "X-BASSWIESN-CSRF": token}, {"Origin": "null"}, {"Origin": "https://testserver", "X-BASSWIESN-CSRF": "wrong"}):
        assert client.post("/test/admin-action", headers=headers).status_code == 403
    assert calls == []
    good = {"Origin": "https://testserver", "X-BASSWIESN-CSRF": token}
    assert client.post("/test/admin-action", headers=good).status_code == 200
    assert calls == ["action"]
    cookie = client.cookies.get(COOKIE)
    assert client.post("/auth/logout", headers=good).status_code == 200
    assert secured[1].session(cookie) is None
    assert client.get("/api/devices").status_code == 401


def test_unauthenticated_write_stops_before_handler_or_transport(secured):
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver")
    for method, path in [("POST", "/api/devices/EXAMPLE/settings-apply"), ("POST", "/api/devices/EXAMPLE/rename"), ("DELETE", "/api/devices/EXAMPLE"), ("POST", "/api/update/admin/install")]:
        response = client.request(method, path, json={}, headers={"Origin": "https://testserver"})
        assert response.status_code == 401


def test_login_rejects_cross_origin_body_errors_and_never_echoes_password(secured):
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver")
    for headers in ({}, {"Origin": "https://evil.invalid"}, {"Origin": "null"}):
        assert client.post("/auth/login", json={"username": "operator", "password": PASSWORD}, headers=headers).status_code == 403
    for payload in ({"password": PASSWORD}, [PASSWORD], {"username": "operator", "password": {"secret": PASSWORD}}, {"username": "operator", "password": "x" * 9000}):
        response = client.post("/auth/login", json=payload, headers={"Origin": "https://testserver"})
        assert response.status_code == 400
        assert PASSWORD not in response.text


def test_login_cookie_is_secure_http_only_host_only_strict(secured):
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver")
    response = client.post("/auth/login", json={"username": "operator", "password": PASSWORD}, headers={"Origin": "https://testserver"})
    cookie = response.headers["set-cookie"]
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag in cookie
    assert "Domain=" not in cookie
    assert "Max-Age=" not in cookie  # browser-session cookie, server has hard expiry


def test_forwarding_headers_cannot_authorize_http_or_untrusted_host(secured):
    settings, _ = secured
    settings.admin_origins = ("http://testserver", "https://testserver")
    client = TestClient(create_web_app(), base_url="http://testserver")
    response = client.post("/auth/login", json={}, headers={"Origin": "https://testserver", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "testserver"})
    assert response.json()["error"]["code"] == "AUTH_TLS_REQUIRED"
    client = TestClient(create_web_app(), base_url="https://evil.invalid")
    assert client.get("/api/devices", headers={"X-Forwarded-Host": "testserver"}).status_code == 403


def test_human_session_required_on_cloud_inventory_and_debug(secured):
    web = TestClient(create_web_app(), base_url="https://testserver")
    authenticate(web)
    for factory, path in [(create_cloud_app, "/api/cloud/stations"), (create_debug_app, "/requests")]:
        client = TestClient(factory(), base_url="https://testserver")
        assert client.get(path).status_code == 401
        client.cookies.update(web.cookies)
        assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", ["/api/%64evices", "/api/devices/", "/static/../api/devices", "/bmx/../api/devices"])
def test_path_spelling_cannot_bypass(secured, path):
    client = TestClient(create_web_app(), base_url="https://testserver")
    assert client.get(path).status_code == 401


def test_browser_navigation_login_and_websocket_fail_closed(secured):
    client = TestClient(create_web_app(), base_url="https://testserver")
    response = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"
    page = client.get("/auth/login")
    assert "unsafe-eval" not in page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/anything"):
            pass


def test_legacy_mode_does_not_create_admin_database(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    settings.admin_auth_mode = "legacy"
    client = TestClient(create_web_app())
    assert client.get("/auth/session").json() == {"enabled": False}
    assert client.get("/api/devices").status_code == 200
    assert client.post("/auth/login", json={}).status_code == 404
    response = client.post("/auth/logout")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AUTH_DISABLED"
    assert "set-cookie" not in response.headers
    assert not (tmp_path / "secrets" / "admin").exists()


@pytest.mark.parametrize("value", ["null", "file:///tmp/test", "https://user:pass@example.invalid", "https://example.invalid/path", "https://example.invalid?query=1", "https://example.invalid:bad", "https://example.invalid\\evil"])
def test_invalid_origin(value):
    assert origin(value) == ""


@pytest.mark.parametrize("value,expected", [
    ("https://[2001:db8::42]", "https://[2001:db8::42]:443"),
    ("http://[2001:db8::42]:1328/", "http://[2001:db8::42]:1328"),
])
def test_literal_ipv6_origin_keeps_brackets_and_explicit_port(value, expected):
    assert origin(value) == expected


def test_session_storage_failure_after_validation_never_reaches_handler(secured, monkeypatch):
    from basswiesn.app.core.admin_store import AuthUnavailable
    app = create_web_app(background_tasks=False)
    client = TestClient(app, base_url="https://testserver")
    authenticate(client)
    original = app.state.admin_store.session
    calls = []
    @app.get("/test/private-action")
    def action():
        calls.append("handler")
        return {"ok": True}
    def unavailable(cookie, *, touch=True):
        if touch:
            raise AuthUnavailable("private database path canary")
        return original(cookie, touch=False)
    monkeypatch.setattr(app.state.admin_store, "session", unavailable)
    response = client.get("/test/private-action")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AUTH_UNAVAILABLE"
    assert "canary" not in response.text and calls == []


def test_unregistered_handler_alias_or_method_never_becomes_anonymous():
    from fastapi.routing import APIRoute
    from basswiesn.app.core.admin_protocols import device_routes
    from basswiesn.app.routers import cloud
    assert device_routes([APIRoute("/api/management-alias", cloud.streaming_sourceproviders, methods=["GET"])]) == []
    assert device_routes([APIRoute("/streaming/sourceproviders", cloud.streaming_sourceproviders, methods=["DELETE"])]) == []


def test_required_mode_configuration_and_store_fail_closed(secured):
    settings, store = secured
    settings.admin_origins = ()
    client = TestClient(create_web_app(), base_url="https://testserver")
    assert client.get("/api/devices").status_code == 503
    settings.admin_origins = ("https://testserver",)
    client = TestClient(create_web_app(), base_url="https://testserver")
    authenticate(client)
    store.path.chmod(0o644)
    assert client.get("/auth/session").status_code == 503
    response = client.post("/auth/login", json={"username": "operator", "password": PASSWORD}, headers={"Origin": "https://testserver"})
    assert response.status_code == 503


def test_login_attempt_limit_http_response_and_credential_safe_errors(secured):
    client = TestClient(create_web_app(), base_url="https://testserver")
    for _ in range(5):
        response = client.post("/auth/login", json={"username": "operator", "password": "incorrect"}, headers={"Origin": "https://testserver"})
        assert response.status_code == 401
        assert "incorrect" not in response.text
    response = client.post("/auth/login", json={"username": "operator", "password": PASSWORD}, headers={"Origin": "https://testserver"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"


def test_store_failure_during_logout_is_not_reported_as_success(secured, monkeypatch):
    from basswiesn.app.core.admin_store import AuthUnavailable
    app = create_web_app()
    client = TestClient(app, base_url="https://testserver")
    token = authenticate(client)
    def unavailable(cookie):
        raise AuthUnavailable("injected database failure")
    monkeypatch.setattr(app.state.admin_store, "logout", unavailable)
    response = client.post("/auth/logout", headers={"Origin": "https://testserver", "X-BASSWIESN-CSRF": token})
    assert response.status_code == 503
    assert "set-cookie" not in response.headers


def test_session_revoked_between_validation_and_touch_is_rejected(secured, monkeypatch):
    app = create_web_app()
    client = TestClient(app, base_url="https://testserver")
    authenticate(client)
    original = app.state.admin_store.session
    def racing(cookie, *, touch=True):
        return None if touch else original(cookie, touch=False)
    monkeypatch.setattr(app.state.admin_store, "session", racing)
    assert client.get("/api/devices").status_code == 401


def test_authenticated_unsupported_method_not_delegated_to_device_contract(secured):
    client = TestClient(create_web_app(), base_url="https://testserver")
    csrf = authenticate(client)
    response = client.delete("/streaming/sourceproviders", headers={"Origin": "https://testserver", "X-BASSWIESN-CSRF": csrf})
    assert response.status_code == 405
