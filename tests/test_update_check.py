import asyncio

import httpx
from fastapi.testclient import TestClient

from basswiesn.app.main import create_web_app
from basswiesn.app.services import updates


def test_update_check_without_url_is_not_configured():
    with TestClient(create_web_app()) as client:
        result = client.post("/api/update/check").json()
    assert result["status"] == "not_configured"


def test_invalid_manifest_url_is_error():
    result = asyncio.run(updates.check_update("1.0.1", "not-a-url"))
    assert result["status"] == "error"


def test_equal_version_is_up_to_date(monkeypatch):
    async def fake_fetch(_url):
        return {"version": "1.0.1", "release_date": "2026-07-02"}
    monkeypatch.setattr(updates, "fetch_manifest", fake_fetch)
    assert asyncio.run(updates.check_update("v1.0.1", "https://example.test/manifest.json"))["status"] == "up_to_date"


def test_new_version_is_available(monkeypatch):
    async def fake_fetch(_url):
        return {"version": "1.0.2", "download_url": "https://example.test/release.tar.gz"}
    monkeypatch.setattr(updates, "fetch_manifest", fake_fetch)
    assert asyncio.run(updates.check_update("1.0.1", "https://example.test/manifest.json"))["status"] == "update_available"


def test_manifest_timeout_is_error(monkeypatch):
    async def fake_fetch(_url):
        raise httpx.TimeoutException("timed out")
    monkeypatch.setattr(updates, "fetch_manifest", fake_fetch)
    assert asyncio.run(updates.check_update("1.0.1", "https://example.test/manifest.json"))["status"] == "error"
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
