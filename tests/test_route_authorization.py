"""Permission follows the selected handler, including shadowed public paths."""
import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from basswiesn.app.main import create_cloud_app, create_debug_app, create_web_app
from test_admin_boundary import secured  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("factory,path,method", [
    (create_web_app, "/streaming/sourceproviders", "GET"),
    (create_cloud_app, "/streaming/sourceproviders", "GET"),
    (create_web_app, "/streaming/support/power_on", "POST"),
    (create_cloud_app, "/streaming/support/power_on", "POST"),
    (create_web_app, "/live", "GET"),
    (create_cloud_app, "/ready", "GET"),
    (create_debug_app, "/live", "GET"),
    (create_web_app, "/static/js/app.js", "GET"),
    (create_cloud_app, "/media/example.mp3", "GET"),
    (create_web_app, "/auth/login", "GET"),
    (create_web_app, "/auth/login", "POST"),
    (create_web_app, "/static/js/admin-login.js", "GET"),
])
def test_shadowed_public_path_never_authorizes_another_handler(secured, factory, path, method):
    calls = []
    def administrative_handler():
        calls.append("must not run")
        return {"private": True}
    app = factory()
    app.router.routes.insert(0, APIRoute(path, administrative_handler, methods=[method]))
    client = TestClient(app, base_url="https://testserver")
    response = client.request(method, path, headers={"Origin": "https://testserver"})
    assert response.status_code == 401
    assert calls == []


def test_prefix_of_reused_device_handler_is_not_a_grant(secured):
    from basswiesn.app.core.admin_protocols import device_routes
    from basswiesn.app.routers import cloud
    from basswiesn.app.core.admin_boundary import install_admin_boundary
    from fastapi import FastAPI
    router = APIRouter()
    router.add_api_route("/streaming/sourceproviders", cloud.streaming_sourceproviders, methods=["GET"])
    app = FastAPI()
    app.include_router(router, prefix="/administrative-alias")
    install_admin_boundary(app, settings=secured[0], listener="web", anonymous=device_routes(router.routes))
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/administrative-alias/streaming/sourceproviders").status_code == 401


@pytest.mark.parametrize("factory", [create_web_app, create_cloud_app])
def test_real_public_handlers_and_assets_remain_available(secured, factory):
    client = TestClient(factory(), base_url="http://device-service.invalid", client=("192.0.2.42", 41000))
    assert client.get("/streaming/sourceproviders").status_code == 200
    assert client.get("/live").json() == {"live": True}
    assert client.get("/ready").json() == {"ready": True}
    assert client.get("/static/js/admin-login.js").status_code == 200
