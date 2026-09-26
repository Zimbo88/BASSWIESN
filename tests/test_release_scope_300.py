"""Deferred features cannot start a new operation outside LAB. Offline only."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from basswiesn.app import db as database
from basswiesn.app.models import Setting
from basswiesn.app.routers import dlna, update_admin

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mode", [None, "easy", "standard"])
def test_unfinished_actions_fail_before_transport_or_credentials(mode, monkeypatch):
    if mode is not None:
        with database.SessionLocal() as db:
            db.add(Setting(key="ui_mode", value=mode))
            db.commit()
    def forbidden(*a, **k):
        pytest.fail("LAB rejection must precede external transport")
    monkeypatch.setattr(dlna, "client_factory", forbidden)
    monkeypatch.setattr(update_admin.update_control, "exchange", forbidden)
    app = FastAPI()
    app.include_router(dlna.router)
    app.include_router(update_admin.router)
    with TestClient(app, base_url="https://example.test:1329") as client:
        for path, body in (("/api/dlna/settings", {"enabled": True}),
                           ("/api/dlna/servers", {"description_url": "http://192.0.2.42/root.xml", "approve": True})):
            result = client.post(path, json=body)
            assert result.status_code == 409
            assert result.json()["detail"]["code"] == "LAB_MODE_REQUIRED"
        result = client.post("/api/update/admin/install", content="{}", headers={
            "Origin": "https://example.test:1329", "X-BASSWIESN-Update": "1", "Content-Type": "application/json"})
        assert result.status_code == 409
        assert result.json()["detail"]["code"] == "LAB_MODE_REQUIRED"
        assert client.post("/api/dlna/settings", json={"enabled": False}).status_code == 200
