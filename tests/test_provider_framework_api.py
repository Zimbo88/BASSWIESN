"""Actual web/session boundaries and existing saved-station integration."""

import json

import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.core.api_tokens import ApiTokenStore
from basswiesn.app.main import create_cloud_app, create_debug_app, create_web_app
from basswiesn.app.models import Device, Station
from basswiesn.app.routers import providers
from basswiesn.app.services.providers.manager import ProviderManager
from basswiesn.app.services.providers.runtime import ProviderRuntimeConfig, build_provider_manager
from test_admin_boundary import authenticate, secured  # noqa: F401
from test_provider_framework import PUBLIC, TOKEN, TRACK, local, modern

pytestmark = pytest.mark.integration


def client_and_headers(manager=None):
    app = create_web_app(background_tasks=False)
    if manager:
        app.state.provider_manager = manager
    client = TestClient(app, base_url="https://testserver")
    csrf = authenticate(client)
    return client, {"Origin": "https://testserver", "X-BASSWIESN-CSRF": csrf}


def test_router_registered_only_on_web_listener(secured):
    for factory, expected in ((create_web_app, True), (create_cloud_app, False), (create_debug_app, False)):
        paths = [item.path for item in iter_route_contexts(factory().routes)]
        assert any(path.startswith("/api/providers") for path in paths) is expected


def test_legacy_or_missing_boundary_never_authorizes_new_catalog_operations():
    for app in (create_web_app(background_tasks=False), FastAPI()):
        if not hasattr(app.state, "admin_settings"):
            app.include_router(providers.router)
        client = TestClient(app)
        assert client.get("/api/providers").status_code == 401
        assert client.post("/api/providers/local_stations/browse", json={}).status_code == 401
        assert client.get("/api/providers/local_stations/health").status_code == 401


def test_disabled_optional_provider_inventory_is_cached_not_a_probe(secured, monkeypatch):
    monkeypatch.setenv("MUSIC_ASSISTANT_ENABLED", "false")
    monkeypatch.setattr("socket.getaddrinfo", lambda *args: pytest.fail("provider inventory used DNS"))
    client, headers = client_and_headers()
    result = client.get("/api/providers")
    assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
    native, modern = result.json()["providers"]
    assert native["provider_id"] == "local_stations" and native["enabled"]
    assert modern["stage"] == "LAB" and not modern["enabled"]
    assert modern["health"]["provider"] == "DISABLED"
    assert modern["capabilities"]["resolve"] is False
    assert client.get("/api/providers/local_stations/health").json()["provider"] == "UNKNOWN"
    assert client.post("/api/providers/music_assistant/health", json={}, headers=headers).status_code == 409


@pytest.mark.parametrize("method,path", [("GET", "/api/providers"), ("GET", "/api/providers/local_stations/health"),
    ("POST", "/api/providers/local_stations/search"), ("POST", "/api/providers/local_stations/browse"),
    ("POST", "/api/providers/local_stations/metadata"), ("POST", "/api/providers/local_stations/resolve"),
    ("POST", "/api/providers/music_assistant/health")])
def test_bearer_has_no_provider_credential_proxy_privilege(secured, method, path):
    client = TestClient(create_web_app(background_tasks=False), base_url="https://testserver")
    issued = ApiTokenStore(secured[1]).issue("catalog fixture", ["read", "admin", "diagnostics"], 3600)
    response = client.request(method, path, headers={"Authorization": f"Bearer {issued.token}"}, json={})
    assert response.status_code == 403
    assert issued.token not in response.text and not hasattr(client.app.state, "provider_manager")


def test_csrf_origin_and_session_checked_before_optional_transport(secured):
    adapter, calls = modern(result=[])
    client, good = client_and_headers(ProviderManager((adapter,)))
    path = "/api/providers/music_assistant/health"
    for headers in ({}, {"Origin": "https://testserver"}, {**good, "Origin": "https://evil.invalid"}, {**good, "X-BASSWIESN-CSRF": "wrong"}):
        assert client.post(path, json={}, headers=headers).status_code == 403
    assert not calls
    assert client.post(path, json={}, headers=good).status_code == 200
    assert len(calls) == 2


def test_saved_station_catalog_uses_existing_rows_and_filters_internal_lab(secured, monkeypatch):
    monkeypatch.setenv("MUSIC_ASSISTANT_ENABLED", "false")
    with app_db.SessionLocal() as db:
        db.add_all([
            Station(name="Public fixture", stream_url=f"https://{PUBLIC}/radio.mp3"),
            Station(name="Internal fixture", stream_url="https://private.invalid", internal=True),
            Station(name="Lab fixture", stream_url="https://private.invalid", lab_only=True),
            Station(name="Literal % fixture", stream_url=f"https://{PUBLIC}/literal.mp3"),
        ])
        db.commit()
        station_id = str(db.query(Station).filter_by(name="Public fixture").one().id)
    client, headers = client_and_headers()
    page = client.post("/api/providers/local_stations/browse", json={}, headers=headers)
    assert page.status_code == 200
    assert [item["name"] for item in page.json()["items"]] == ["Literal % fixture", "Public fixture"]
    result = client.post("/api/providers/local_stations/search", json={"query": "%"}, headers=headers)
    assert [item["name"] for item in result.json()["items"]] == ["Literal % fixture"]
    metadata = client.post("/api/providers/local_stations/metadata", json={"item_id": station_id}, headers=headers)
    assert metadata.json()["item_id"] == station_id
    assert "stream_url" not in metadata.text and "image_url" not in metadata.text
    # Numeric public address takes no DNS path; resolution never probes audio.
    resolved = client.post("/api/providers/local_stations/resolve", json={"item_id": station_id}, headers=headers)
    assert resolved.status_code == 200 and resolved.json()["policy_recheck_required"]
    assert resolved.json()["playback_verified"] is False
    assert resolved.json()["uri"] == f"https://{PUBLIC}/radio.mp3"
    with app_db.SessionLocal() as db:
        assert db.query(Station).count() == 4  # no importing/copying/updating sources


def test_actual_radio_inventory_blocks_even_public_candidate(secured):
    with app_db.SessionLocal() as db:
        db.add(Device(device_id="PROVIDERFIXTURE", name="Fixture radio", ip_address=PUBLIC))
        station = Station(name="Unsafe target fixture", stream_url=f"https://{PUBLIC}/private")
        db.add(station)
        db.commit()
        station_id = str(station.id)
    client, headers = client_and_headers(build_provider_manager(ProviderRuntimeConfig()))
    result = client.post("/api/providers/local_stations/resolve", json={"item_id": station_id}, headers=headers)
    assert result.status_code == 403 and result.json()["error"]["code"] == "PROVIDER_UNSAFE_TARGET"


@pytest.mark.parametrize("path,body", [
    ("search", {"query": "secret-fixture", "unknown": True}), ("search", {"query": 1}),
    ("search", {"query": "secret-fixture", "limit": True}), ("search", {"query": "secret-fixture", "limit": 0}),
    ("browse", {"parent": []}), ("metadata", {}), ("metadata", {"item_id": 12}),
    ("resolve", {"item_id": "1", "secret": "secret-fixture"}), ("health", {"secret": "secret-fixture"}),
])
def test_invalid_body_never_echoes_fields_or_queries(secured, path, body):
    client, headers = client_and_headers(ProviderManager((local(),)))
    result = client.post(f"/api/providers/local_stations/{path}", json=body, headers=headers)
    assert result.status_code == 400 and "secret-fixture" not in result.text
    assert result.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("body,content_type", [(b"invalid secret-fixture", "application/json"),
    (b"x" * 4097, "application/json"), (b"[]", "application/json"), (b"{}", "text/plain"),
    (b"[" * 1500 + b"]" * 1500, "application/json"), (b"\xff", "application/json")])
def test_request_body_size_json_shape_and_content_type_bounded(secured, body, content_type):
    client, headers = client_and_headers(ProviderManager((local(),)))
    result = client.post("/api/providers/local_stations/health", content=body,
                         headers={**headers, "Content-Type": content_type})
    assert result.status_code == 400 and "secret-fixture" not in result.text


def test_route_error_codes_and_real_modern_catalog_result(secured):
    adapter, calls = modern(result={"tracks": [TRACK]})
    client, headers = client_and_headers(ProviderManager((local(), adapter)))
    result = client.post("/api/providers/music_assistant/search", json={"query": "Fixture"}, headers=headers)
    assert result.status_code == 200 and result.json()["items"][0]["provider_id"] == "music_assistant"
    assert TOKEN not in result.text
    assert client.post("/api/providers/music_assistant/resolve", json={"item_id": TRACK["uri"]}, headers=headers).status_code == 409
    assert len(calls) == 2
    assert client.get("/api/providers/unknown/health").status_code == 404
    assert client.post("/api/providers/unknown/health", json={}, headers=headers).status_code == 404
    assert client.post("/api/providers/local_stations/metadata", json={"item_id": "99"}, headers=headers).status_code == 404


def test_no_upstream_cookie_reuse_or_secrets_returned(secured):
    import httpx
    def handler(request):
        assert "Cookie" not in request.headers
        if request.url.path == "/info":
            return httpx.Response(200, json={"server_version": "2.7.4"}, headers={"Set-Cookie": "private=secret; Path=/"})
        return httpx.Response(500, text=f"failure {TOKEN}")
    adapter, calls = modern(handler)
    client, headers = client_and_headers(ProviderManager((adapter,)))
    result = client.post("/api/providers/music_assistant/health", json={}, headers=headers)
    assert result.status_code == 502 and TOKEN not in result.text
    assert "Set-Cookie" not in result.headers
    assert len(calls) == 2
    state = client.get("/api/providers/music_assistant/health").json()
    assert state["provider"] == "UNAVAILABLE" and state["playback"] == "UNKNOWN"
