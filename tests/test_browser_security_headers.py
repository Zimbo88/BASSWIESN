import pytest
from fastapi.testclient import TestClient

from basswiesn.app.core.browser_headers import AUTH_CSP, secure_browser_response
from basswiesn.app.main import create_web_app
from test_admin_boundary import secured, authenticate  # noqa: F401
from test_token_boundary import bearer

pytestmark = pytest.mark.integration


def test_management_page_session_only_and_bearer_denied(secured):
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver")
    assert client.get("/auth/automation").status_code == 401
    headers, _ = bearer(secured[1], ["read", "admin"])
    assert client.get("/auth/automation", headers=headers).status_code == 403
    authenticate(client)
    response = client.get("/auth/automation")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == AUTH_CSP
    assert 'id="token-create"' in response.text
    assert 'src="/static/js/admin-tokens.js"' in response.text
    assert "bwapi_" not in response.text
    assert client.get("/static/admin-tokens.html").status_code == 404


def test_human_response_headers_cover_login_errors_and_authenticated_views(secured):
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver")
    for path in ("/auth/login", "/api/devices", "/auth/automation"):
        response = client.get(path)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        assert "microphone=()" in response.headers["permissions-policy"]
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "strict-transport-security" not in response.headers
    authenticate(client)
    response = client.get("/")
    assert response.status_code == 200
    assert "unsafe-eval" not in response.headers["content-security-policy"]
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "script-src-attr 'none'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


def test_device_contract_remains_unchanged_and_legacy_is_not_claimed_hardened(secured):
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver", client=("192.0.2.42", 41000))
    response = client.get("/streaming/sourceproviders")
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    secured[0].admin_auth_mode = "legacy"
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver")
    assert client.get("/auth/automation").status_code == 401
    assert "content-security-policy" not in client.get("/").headers


def test_header_policy_preserves_stricter_existing_policy_and_body():
    policy = b"default-src 'none'"
    original = {"type": "http.response.start", "status": 200,
                "headers": [(b"Content-Security-Policy", policy), (b"Cache-Control", b"public")]}
    headers = dict(secure_browser_response(original, path="/other")["headers"])
    assert headers[b"Content-Security-Policy"] == policy
    assert headers[b"cache-control"] == b"no-store"
    assert b"Cache-Control" not in headers
    body = {"type": "http.response.body", "body": b"unchanged"}
    assert secure_browser_response(body, path="/auth/login") is body


def test_remote_and_auxiliary_views_receive_script_policy_but_docs_keep_compatibility(secured):
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver")
    authenticate(client)
    assert "script-src 'self'" in client.get("/remote/EXAMPLE").headers["content-security-policy"]
    # FastAPI's optional interactive docs currently use an inline initializer
    # and CDN assets. Do not pretend they have the application's strict policy.
    assert "script-src" not in client.get("/docs").headers["content-security-policy"]
    assert "frame-ancestors 'none'" in client.get("/docs").headers["content-security-policy"]
