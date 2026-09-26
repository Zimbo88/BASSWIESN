"""HTTPS boundary tests; no external transport, host changes or real secrets."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from basswiesn.app.routers import update_admin as api
from basswiesn.app import db as database
from basswiesn.app.models import Setting
from basswiesn.app.services import update_control as control
from test_update_helper_300 import wire, REQUEST_ID, TOKEN

pytestmark = pytest.mark.unit
ORIGIN = "https://example.test:1329"
HEADERS = {"Origin": ORIGIN, "X-BASSWIESN-Update": "1", "Content-Type": "application/json",
           "Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def client(monkeypatch):
    with database.SessionLocal() as db:
        db.add(Setting(key="ui_mode", value="lab"))
        db.commit()
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(version="2.6.5", update_validation_mode=False))
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client


def test_only_explicit_https_submission_reaches_helper(client, monkeypatch):
    calls = []
    async def exchange(value):
        calls.append(value)
        return {"ok": True, "result": {"request_id": REQUEST_ID, "state": "REQUESTED"}}
    monkeypatch.setattr(control, "exchange", exchange)
    response = client.post("/api/update/admin/install", content=wire(action="submit"), headers=HEADERS)
    assert response.status_code == 202
    assert calls[0]["credential"] == TOKEN and len(calls) == 1
    assert TOKEN not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("headers,base", [
    ({}, ORIGIN), ({**HEADERS, "Origin": "https://other.invalid:1329"}, ORIGIN),
    ({**HEADERS, "Origin": "null"}, ORIGIN), ({**HEADERS, "Origin": ORIGIN+"/"}, ORIGIN),
    ({**HEADERS, "Sec-Fetch-Site": "same-site"}, ORIGIN),
    ({**HEADERS, "Sec-Fetch-Site": "cross-site"}, ORIGIN),
    ({**HEADERS, "X-BASSWIESN-Update": "0"}, ORIGIN),
    ({**HEADERS, "Content-Type": "text/plain"}, ORIGIN),
    ({**HEADERS, "X-Forwarded-Proto": "https"}, "http://example.test:1329"),
    ({**HEADERS, "X-Forwarded-Proto": "https"}, ORIGIN),
    ({**HEADERS, "Forwarded": "proto=https"}, ORIGIN),
    ({**HEADERS, "Origin": "https://user@example.test:1329"}, ORIGIN),
])
def test_denied_origins_never_parse_or_forward_secret(client, monkeypatch, headers, base):
    async def forbidden(*a, **k):
        pytest.fail("helper must not be contacted")
    monkeypatch.setattr(control, "exchange", forbidden)
    response = client.post(base+"/api/update/admin/install", content=wire(action="submit"), headers=headers)
    assert response.status_code == 403 and TOKEN not in response.text


@pytest.mark.parametrize("body", [b"{", b"x"*2049, wire(), wire(action="submit", command="no"),
    wire(action="submit", expected_current_version="0.0.0"), wire(action="submit", credential="invalid"),
    wire(action="submit")[:-1]+b',"credential":"'+TOKEN.encode()+b'"}'])
def test_bad_body_never_echoes_input(client, monkeypatch, body):
    async def forbidden(*a, **k):
        pytest.fail("invalid request reached helper")
    monkeypatch.setattr(control, "exchange", forbidden)
    response = client.post("/api/update/admin/install", content=body, headers=HEADERS)
    assert response.status_code == 400 and TOKEN not in response.text and "input" not in response.text


@pytest.mark.parametrize("submitted,expected", [(False,503),(True,202)])
def test_uncertain_submission_never_retries(client, monkeypatch, submitted, expected):
    calls = []
    async def failed(value):
        calls.append(value)
        raise control.ControlUnavailable(submitted=submitted)
    monkeypatch.setattr(control, "exchange", failed)
    response = client.post("/api/update/admin/install", content=wire(action="submit"), headers=HEADERS)
    assert response.status_code == expected and len(calls) == 1
    assert response.json()["code"] == ("SUBMISSION_UNCERTAIN" if submitted else "HELPER_UNAVAILABLE")
    assert TOKEN not in response.text


def test_offline_policy_blocks_before_secret_delivery(client, monkeypatch):
    monkeypatch.setattr(api, "external_request_decision", lambda *a, **k: SimpleNamespace(allowed=False))
    assert client.post("/api/update/admin/install", content=wire(action="submit"), headers=HEADERS).status_code == 409


def test_untrusted_helper_fields_are_not_forwarded():
    value = control._safe_result({"ok": True, "result": {"state": "IDLE", "secret": TOKEN}})
    assert TOKEN not in json.dumps(value)
    with pytest.raises(ValueError):
        control._safe_result({"ok": False, "code": TOKEN})


def test_unexpected_adapter_error_never_reaches_generic_secret_logger(client, monkeypatch, caplog):
    async def failed(_body):
        raise RuntimeError(TOKEN)
    monkeypatch.setattr(control, "exchange", failed)
    response = client.post("/api/update/admin/install", content=wire(action="submit"), headers=HEADERS)
    assert response.status_code == 202 and response.json()["code"] == "SUBMISSION_UNCERTAIN"
    assert TOKEN not in response.text + caplog.text


def test_missing_socket_has_no_network_fallback(monkeypatch):
    monkeypatch.setattr(control, "available", lambda: False)
    with pytest.raises(control.ControlUnavailable):
        asyncio.run(control.status())
