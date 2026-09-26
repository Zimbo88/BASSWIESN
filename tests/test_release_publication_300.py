import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import RuntimeState
from basswiesn.app.services import release_publication as publication
from basswiesn.app.services.network_security import UrlValidation

pytestmark = pytest.mark.integration


def test_version_date_is_observed_publication_not_build_or_today(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Normal version reads must not contact GitHub")
    monkeypatch.setattr(publication, "fetch_publication", forbidden)
    with TestClient(create_web_app()) as client:
        result = client.get("/api/version").json()
    assert result["version"] == "3.0.1"
    assert result["published_at"] is None
    assert result["publication_status"] == "UNKNOWN"
    assert result["publication_source"] == "NONE"
    with app_db.SessionLocal() as db:
        previous = publication.publication_info(db, "2.6.5")
        assert previous["published_at"] == "2026-09-19T13:36:41Z"


def test_unknown_version_never_inherits_previous_date():
    with app_db.SessionLocal() as db:
        for version in ("3.0.0", "3.0.0-rc1", "dev", "../2.6.5"):
            result = publication.publication_info(db, version)
            assert result["publication_status"] == "UNKNOWN"
            assert result["published_at"] is None
            assert result["release_url"] is None


@pytest.mark.parametrize("cached", ["not json", "[]", '{}',
    '{"version":"other","published_at":"2026-01-01T00:00:00Z"}',
    '{"version":"3.0.0","published_at":"9999-01-01T00:00:00Z"}'])
def test_invalid_cached_data_does_not_invent_publication(cached):
    with app_db.SessionLocal() as db:
        db.add(RuntimeState(key="release_publication:3.0.0", value=cached))
        db.commit()
        assert publication.publication_info(db, "3.0.0")["publication_status"] == "UNKNOWN"


def _mock_dns(monkeypatch):
    calls = []
    def validated(url, *, allowed_hosts, public_only):
        calls.append(url)
        assert allowed_hosts == {"api.github.com"} and public_only is True
        return UrlValidation(True, "fixture", "api.github.com", ("93.184.216.34",), "https", 443)
    monkeypatch.setattr(publication, "validate_outbound_http_url", validated)
    return calls


def _response(version="3.0.0"):
    return {"tag_name": "v" + version, "draft": False,
            "html_url": f"{publication.REPOSITORY}/releases/tag/v{version}",
            "published_at": "2026-01-15T08:09:10Z"}


def test_explicit_lookup_is_fixed_origin_pinned_and_strips_extra_fields(monkeypatch):
    calls = _mock_dns(monkeypatch)
    def handler(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "api.github.com"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={**_response(), "body": "unused release prose", "assets": []})
    result = asyncio.run(publication.fetch_publication("3.0.0", transport=httpx.MockTransport(handler)))
    assert calls == [publication.API + "v3.0.0"]
    assert result == {"refresh_status": "OK", "version": "3.0.0", "published_at": "2026-01-15T08:09:10Z"}


@pytest.mark.parametrize("changed", [{"tag_name": "v2.0.0"}, {"draft": True}, {"draft": None},
    {"html_url": "https://example.invalid/release"}, {"published_at": None},
    {"published_at": "2026-02-30T00:00:00Z"}, {"published_at": "9999-01-01T00:00:00Z"}])
def test_mismatched_or_unpublished_release_rejected(monkeypatch, changed):
    _mock_dns(monkeypatch)
    result = asyncio.run(publication.fetch_publication("3.0.0", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={**_response(), **changed}))))
    assert result == {"refresh_status": "INVALID_RESPONSE"}


@pytest.mark.parametrize("status,expected", [(302, "UNAVAILABLE"), (404, "NOT_FOUND"), (429, "RATE_LIMITED")])
def test_redirects_are_never_followed_and_remote_bodies_not_returned(monkeypatch, status, expected):
    _mock_dns(monkeypatch)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "http://127.0.0.1/private"}, text="untrusted response body")
    assert asyncio.run(publication.fetch_publication("3.0.0", transport=httpx.MockTransport(handler))) == {"refresh_status": expected}
    assert len(requests) == 1


def test_rejected_dns_and_oversized_json_fail_closed(monkeypatch):
    monkeypatch.setattr(publication, "validate_outbound_http_url", lambda *args, **kwargs: UrlValidation(False, "blocked"))
    assert asyncio.run(publication.fetch_publication("3.0.0")) == {"refresh_status": "TARGET_REJECTED"}
    _mock_dns(monkeypatch)
    result = asyncio.run(publication.fetch_publication("3.0.0", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"x" * (publication.MAX_BYTES + 1)))))
    assert result == {"refresh_status": "RESPONSE_TOO_LARGE"}


def test_cache_only_contains_matching_allowlisted_public_fields(monkeypatch):
    async def fetched(version):
        return {"refresh_status": "OK", "version": version, "published_at": "2026-01-15T08:09:10Z", "body": "discard"}
    monkeypatch.setattr(publication, "fetch_publication", fetched)
    with app_db.SessionLocal() as db:
        result = asyncio.run(publication.refresh_publication(db, "3.0.0"))
        assert result["publication_source"] == "GITHUB_CACHE"
        row = db.query(RuntimeState).filter(RuntimeState.key == "release_publication:3.0.0").one()
        assert json.loads(row.value) == {"version": "3.0.0", "published_at": "2026-01-15T08:09:10Z"}
        async def offline(_):
            return {"refresh_status": "UNAVAILABLE"}
        monkeypatch.setattr(publication, "fetch_publication", offline)
        assert asyncio.run(publication.refresh_publication(db, "3.0.0"))["published_at"] == result["published_at"]


def test_refresh_route_uses_installed_version_not_request_input(monkeypatch):
    versions = []
    async def fetched(version):
        versions.append(version)
        return {"refresh_status": "NOT_FOUND"}
    monkeypatch.setattr(publication, "fetch_publication", fetched)
    with TestClient(create_web_app()) as client:
        result = client.post("/api/version/publication-refresh", json={"version": "evil", "url": "http://127.0.0.1"})
    assert result.status_code == 200
    assert versions == ["3.0.1"]
    assert result.json()["published_at"] is None
