"""Authorization must stop requests before any hardware handler can execute."""
import pytest
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute, iter_route_contexts

from basswiesn.app.core.api_tokens import ApiTokenStore
from basswiesn.app.core.token_policy import CONTRACTS, required_scope
from basswiesn.app.main import create_web_app, create_cloud_app, create_debug_app
from test_admin_boundary import secured, authenticate  # noqa: F401
from test_settings_write_safety import rig, DEVICE_ID  # noqa: F401

pytestmark = pytest.mark.integration


def bearer(store, scopes):
    issued = ApiTokenStore(store).issue("offline fixture", scopes, 3600)
    return {"Authorization": f"Bearer {issued.token}"}, issued


def test_every_positive_contract_matches_real_registered_handler(secured):
    app = create_web_app(background_tasks=False)
    for (module, function, path, method), grant in CONTRACTS.items():
        matching = [r for r in iter_route_contexts(app.routes) if isinstance(r.original_route, APIRoute) and r.path == path and method in r.methods]
        assert matching and matching[0].endpoint.__module__ == f"basswiesn.app.routers.{module}"
        assert matching[0].endpoint.__name__ == function
        actual = path.replace("{device_id}", "EXAMPLE").replace("{setting}", "volume")
        assert required_scope(app.routes, {"type": "http", "path": actual, "method": method}) == grant


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/devices/EXAMPLE/settings/volume"),
    ("POST", "/api/devices/EXAMPLE/settings-apply"),
    ("POST", "/api/devices/EXAMPLE/key"),
    ("POST", "/api/devices/EXAMPLE/rename"),
    ("POST", "/api/update/admin/install"),
    ("POST", "/api/multiroom/set"),
    ("POST", "/api/presets/EXAMPLE/1"),
    ("GET", "/api/devices/EXAMPLE/settings?probe=true"),
    ("GET", "/auth/session"), ("GET", "/auth/tokens"),
    ("POST", "/auth/tokens"), ("GET", "/docs"),
    ("GET", "/api/support-bundle"), ("GET", "/api/future/endpoint"),
])
def test_read_token_cannot_reach_writes_or_unreviewed_routes(secured, method, path):
    headers, issued = bearer(secured[1], ["read"])
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver")
    response = client.request(method, path, headers=headers, json={})
    assert response.status_code == 403
    assert ApiTokenStore(secured[1]).list()[0].last_used is None
    assert issued.token not in response.text


def test_scopes_are_independent_and_do_not_authorize_host_or_token_admin(secured):
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver")
    headers, _ = bearer(secured[1], ["admin", "playback"])
    assert client.get("/api/version", headers=headers).status_code == 403
    for path in ("/auth/tokens", "/api/update/admin/install", "/api/devices/EXAMPLE/settings/bass"):
        assert client.post(path, json={}, headers=headers).status_code == 403
    # Authorized scope reaches the unchanged target lookup, not a blanket 200.
    assert client.post("/api/devices/EXAMPLE/settings/volume", json={"value": 1}, headers=headers).status_code == 404
    assert client.post("/api/devices/EXAMPLE/rename", json={}, headers=headers).status_code == 404


@pytest.mark.parametrize("scope,path", [("read", "/api/version"), ("presets", "/api/presets/EXAMPLE"),
    ("multiroom", "/api/multiroom/scenarios"), ("diagnostics", "/api/diagnostics/telemetry/summary")])
def test_explicit_projection_scopes_work_without_network(secured, scope, path):
    headers, _ = bearer(secured[1], [scope])
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver")
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert ApiTokenStore(secured[1]).list()[0].last_used is not None


@pytest.mark.parametrize("extra", [{"Origin": "https://testserver"}, {"Sec-Fetch-Site": "same-origin"}, {"Cookie": "ignored=anything"}])
def test_browser_context_cannot_use_bearer_to_bypass_csrf(secured, extra):
    headers, _ = bearer(secured[1], ["read"])
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver")
    assert client.get("/api/version", headers={**headers, **extra}).status_code == 403


def test_duplicate_invalid_or_revoked_credentials_never_fall_back(secured):
    headers, issued = bearer(secured[1], ["read"])
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver")
    assert client.get("/api/version", headers=[*headers.items(), *headers.items()]).status_code == 403
    assert client.get("/api/version", headers={"Authorization": "Basic invalid"}).status_code == 401
    ApiTokenStore(secured[1]).revoke(issued.record.id)
    assert client.get("/api/version", headers=headers).status_code == 401
    authenticate(client)
    assert client.get("/api/version", headers=headers).status_code == 403


@pytest.mark.parametrize("factory,path", [(create_cloud_app, "/api/cloud/stations"),
    (create_debug_app, "/health"), (create_web_app, "/streaming/sourceproviders")])
def test_bearer_has_no_device_cloud_debug_privilege(secured, factory, path):
    headers, _ = bearer(secured[1], ["read", "admin"])
    client = TestClient(factory(), base_url="https://testserver")
    assert client.get(path, headers=headers).status_code == 403


def test_token_management_session_csrf_one_time_secret_and_rotation(secured):
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver")
    csrf = authenticate(client)
    payload = {"name": "automation", "scopes": ["read"], "lifetime_seconds": 3600}
    assert client.post("/auth/tokens", json=payload, headers={"Origin": "https://testserver"}).status_code == 403
    headers = {"Origin": "https://testserver", "X-BASSWIESN-CSRF": csrf}
    issued = client.post("/auth/tokens", json=payload, headers=headers)
    assert issued.status_code == 201
    token, identifier = issued.json()["token"], issued.json()["id"]
    assert issued.headers["cache-control"] == "no-store"
    listing = client.get("/auth/tokens")
    assert token not in listing.text and "digest" not in listing.text
    rotated = client.post(f"/auth/tokens/{identifier}/rotate", headers=headers)
    assert rotated.status_code == 200 and rotated.json()["token"] != token
    assert client.delete(f"/auth/tokens/{identifier}", headers=headers).json() == {"revoked": True}
    assert client.post(f"/auth/tokens/{identifier}/rotate", headers=headers).status_code == 404


def test_management_never_echoes_bad_body_or_runs_in_legacy(secured):
    client = TestClient(create_web_app(), base_url="https://testserver")
    csrf = authenticate(client)
    headers = {"Origin": "https://testserver", "X-BASSWIESN-CSRF": csrf}
    for payload in ({"secret": "canary-value"}, {"name": "canary-value", "scopes": ["root"], "lifetime_seconds": 3600}, ["canary-value"]):
        response = client.post("/auth/tokens", json=payload, headers=headers)
        assert response.status_code == 400 and "canary-value" not in response.text
    assert client.post("/auth/tokens", content=b"x" * 5000, headers=headers).status_code == 400
    secured[0].admin_auth_mode = "legacy"
    client = TestClient(create_web_app(), base_url="https://testserver")
    assert client.post("/auth/tokens", json={}, headers=headers).status_code == 401


def test_alias_handler_and_first_route_shadowing_fail_closed(secured):
    from basswiesn.app.routers.api import api_version
    app = create_web_app()
    alias = APIRoute("/api/version-alias", api_version, methods=["GET"])
    scope = {"type": "http", "path": "/api/version-alias", "method": "GET"}
    assert required_scope([alias], scope) is None
    shadow = APIRoute("/api/version", lambda: {"unsafe": True}, methods=["GET"])
    assert required_scope([shadow, *app.routes], {**scope, "path": "/api/version"}) is None


def test_included_prefix_never_inherits_original_handler_contract(secured):
    from fastapi import FastAPI
    from basswiesn.app.routers.api import router
    app = FastAPI()
    app.include_router(router, prefix="/unexpected")
    assert required_scope(app.routes, {"type": "http", "path": "/unexpected/api/version", "method": "GET"}) is None


@pytest.mark.parametrize("failure", ["none", "ignored-write", "protected", "protected-id", "protected-ip", "identity", "unknown-firmware", "lost-reply"])
def test_bearer_retains_all_volume_transaction_gates(secured, rig, failure):
    from basswiesn.app import db as app_db
    from basswiesn.app.models import Setting
    radio, original = rig
    headers, _ = bearer(secured[1], ["playback"])
    client = TestClient(original.app, base_url="https://testserver")
    if failure == "ignored-write": radio.ignore_write = True
    elif failure == "identity": radio.device_id = "OTHER"
    elif failure == "unknown-firmware": radio.build = "27.0.7.unknown"
    elif failure == "lost-reply": radio.lost_reply = True
    elif failure in {"protected", "protected-id", "protected-ip"}:
        with app_db.SessionLocal() as db:
            key, value = {"protected": ("ip_write_guard", "true"),
                          "protected-id": ("protected_device_ids", DEVICE_ID),
                          "protected-ip": ("protected_device_ips", "192.0.2.42")}[failure]
            db.add(Setting(key=key, value=value))
            db.commit()
    result = client.post(f"/api/devices/{DEVICE_ID}/settings/volume",
                         json={"value": 1, "dry_run": False, "expected_previous": {"volume": 5}}, headers=headers)
    if failure in {"none", "lost-reply"}:
        assert result.status_code == 200, result.text
        assert result.json()["verified"] == 1
        assert radio.calls.count(("POST", "/volume")) == 1
    elif failure in {"protected", "protected-id", "protected-ip"}:
        assert result.status_code == 403
        assert radio.constructors == []
    else:
        assert result.status_code == 409, result.text
        assert radio.calls.count(("POST", "/volume")) == int(failure == "ignored-write")


@pytest.mark.parametrize("action", ["issue", "rotate", "revoke"])
def test_logout_between_middleware_and_token_mutation_wins(secured, monkeypatch, action):
    from basswiesn.app.core.admin_boundary import COOKIE
    client = TestClient(create_web_app(), base_url="https://testserver")
    csrf = authenticate(client)
    cookie = client.cookies.get(COOKIE)
    _, issued = bearer(secured[1], ["read"])
    original = getattr(ApiTokenStore, action)
    def after_logout(self, *args, **kwargs):
        self.admin.logout(cookie)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ApiTokenStore, action, after_logout)
    headers = {"Origin": "https://testserver", "X-BASSWIESN-CSRF": csrf}
    if action == "issue":
        response = client.post("/auth/tokens", headers=headers,
                               json={"name": "new", "scopes": ["admin"], "lifetime_seconds": 3600})
    elif action == "rotate": response = client.post(f"/auth/tokens/{issued.record.id}/rotate", headers=headers)
    else: response = client.delete(f"/auth/tokens/{issued.record.id}", headers=headers)
    assert response.status_code == 401
    assert len(ApiTokenStore(secured[1]).list()) == 1
    assert ApiTokenStore(secured[1]).verify(issued.token) is not None


def test_token_still_requires_real_https_and_trusted_host(secured):
    headers, _ = bearer(secured[1], ["read"])
    secured[0].admin_origins = ("http://testserver", "https://testserver")
    client = TestClient(create_web_app(), base_url="http://testserver")
    response = client.get("/api/version", headers={**headers, "X-Forwarded-Proto": "https"})
    assert response.json()["error"]["code"] == "AUTH_TLS_REQUIRED"
    client = TestClient(create_web_app(), base_url="https://unknown.invalid")
    assert client.get("/api/version", headers=headers).status_code == 403


@pytest.mark.parametrize("method,path,operation", [("GET", "/auth/tokens", "list"),
    ("POST", "/auth/tokens", "issue"), ("POST", "/auth/tokens/" + "a" * 32 + "/rotate", "rotate"),
    ("DELETE", "/auth/tokens/" + "a" * 32, "revoke")])
def test_storage_failure_after_middleware_returns_no_secret(secured, monkeypatch, method, path, operation):
    from basswiesn.app.core.admin_store import AuthUnavailable
    client = TestClient(create_web_app(), base_url="https://testserver")
    csrf = authenticate(client)
    def failed(*args, **kwargs):
        raise AuthUnavailable("private internal canary not for clients")
    monkeypatch.setattr(ApiTokenStore, operation, failed)
    response = client.request(method, path,
        headers={"Origin": "https://testserver", "X-BASSWIESN-CSRF": csrf},
        json={"name": "fixture", "scopes": ["read"], "lifetime_seconds": 3600})
    assert response.status_code == 503
    assert "canary" not in response.text


def test_deep_json_is_bounded_and_token_routes_never_grant_legacy_authority(secured):
    client = TestClient(create_web_app(), base_url="https://testserver")
    csrf = authenticate(client)
    response = client.post("/auth/tokens", content="[" * 1500 + "]" * 1500,
                          headers={"Origin": "https://testserver", "X-BASSWIESN-CSRF": csrf})
    assert response.status_code == 400
    secured[0].admin_auth_mode = "legacy"
    client = TestClient(create_web_app(), base_url="https://testserver")
    for method, path in [("GET", "/auth/tokens"), ("POST", "/auth/tokens/a/rotate"), ("DELETE", "/auth/tokens/a")]:
        assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("failure", ["store-first", "store-second", "revoked-before-touch"])
def test_verification_failure_cannot_reach_handler(secured, monkeypatch, failure):
    from basswiesn.app.core.admin_store import AuthUnavailable
    headers, _ = bearer(secured[1], ["read"])
    original = ApiTokenStore.verify
    def fail(self, token, *, touch=True):
        if failure == "store-first" and not touch or failure == "store-second" and touch:
            raise AuthUnavailable("private diagnostic canary")
        if failure == "revoked-before-touch" and touch:
            return None
        return original(self, token, touch=touch)
    monkeypatch.setattr(ApiTokenStore, "verify", fail)
    client = TestClient(create_web_app(), base_url="https://testserver")
    response = client.get("/api/version", headers=headers)
    assert response.status_code == (401 if failure == "revoked-before-touch" else 503)
    assert "canary" not in response.text
    assert ApiTokenStore(secured[1]).list()[0].last_used is None
