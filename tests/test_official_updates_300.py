import asyncio
import copy

import httpx
import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Setting
from basswiesn.app.services import official_updates as updates
from basswiesn.app.services import feature_status
from basswiesn.app.services.network_security import UrlValidation

pytestmark = pytest.mark.integration


def release():
    return {"tag_name": "v3.0.0", "draft": False, "prerelease": False,
            "published_at": "2026-01-01T00:00:00Z",
            "html_url": updates.REPOSITORY + "/releases/tag/v3.0.0",
            "assets": [{"name": name, "size": 123, "state": "uploaded",
                        "browser_download_url": updates.REPOSITORY + "/releases/download/v3.0.0/" + name}
                       for name in ("basswiesn-docker-release-3.0.0.tar.gz", "SHA256SUMS")]}


def test_official_check_does_not_imply_integrity_or_installation():
    value = updates.parse_release(release(), "2.6.5")
    assert value["status"] == "update_available" and value["package_assets_present"]
    assert not value["integrity_verified"] and not value["installation_available"]
    assert updates.parse_release(release(), "3.0.0")["status"] == "up_to_date"
    assert updates.parse_release(release(), "dev")["status"] == "local_version_unknown"


@pytest.mark.parametrize("field,value", [("draft", True), ("prerelease", True), ("tag_name", "v3.0.0-rc1"),
    ("tag_name", "v3.0.0/anything"), ("html_url", "https://example.invalid/release"), ("published_at", "9999-01-01T00:00:00Z")])
def test_rejects_wrong_release_provenance(field, value):
    payload = release()
    payload[field] = value
    with pytest.raises(ValueError):
        updates.parse_release(payload, "2.6.5")


def test_assets_must_match_tag_and_official_repository():
    payload = release()
    payload["assets"][0]["browser_download_url"] = "https://example.invalid/package.tar.gz"
    with pytest.raises(ValueError):
        updates.parse_release(payload, "2.6.5")
    payload = release()
    payload["assets"].append(copy.deepcopy(payload["assets"][0]))
    with pytest.raises(ValueError):
        updates.parse_release(payload, "2.6.5")
    payload["assets"] = []
    assert not updates.parse_release(payload, "2.6.5")["package_assets_present"]


@pytest.mark.parametrize("kind,expected", [("good", "update_available"), ("large", "response_too_large"),
    ("redirect", "unavailable"), ("rate", "rate_limited"), ("bad", "unavailable")])
def test_bounded_fixed_origin_no_redirects_or_secret_output(monkeypatch, kind, expected):
    def dns(url, **kwargs):
        assert url == updates.LATEST and kwargs == {"allowed_hosts": {"api.github.com"}, "public_only": True}
        return UrlValidation(True, "fixture", "api.github.com", ("93.184.216.34",), "https", 443)
    monkeypatch.setattr(updates, "validate_outbound_http_url", dns)
    calls = []
    def handle(request):
        calls.append(request)
        assert request.url.host == "93.184.216.34" and request.headers["host"] == "api.github.com"
        assert "authorization" not in request.headers
        return {"good": httpx.Response(200, json=release()), "large": httpx.Response(200, content=b"x" * (updates.MAX_BYTES + 1)),
                "redirect": httpx.Response(302, headers={"Location": "https://example.invalid"}),
                "rate": httpx.Response(429), "bad": httpx.Response(200, text="private response detail")}[kind]
    value = asyncio.run(updates.check_official_release("2.6.5", transport=httpx.MockTransport(handle)))
    assert value["status"] == expected and len(calls) == 1
    assert "private response" not in str(value)


def test_status_is_passive_and_strict_offline_blocks_check(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("No network allowed")
    monkeypatch.setattr(updates, "check_official_release", forbidden)
    with app_db.SessionLocal() as db:
        db.add(Setting(key="offline_mode", value="strict"))
        db.commit()
    with TestClient(create_web_app(background_tasks=False)) as client:
        assert client.get("/api/update/official").json()["status"] == "not_checked"
        assert client.post("/api/update/official/check").json()["status"] == "blocked_by_offline_mode"


@pytest.mark.parametrize("mode", ["auto", "off", "strict"])
def test_official_feature_does_not_depend_on_custom_manifest_settings(mode, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Feature catalogue must remain passive")
    monkeypatch.setattr(updates, "check_official_release", forbidden)
    with app_db.SessionLocal() as db:
        db.add_all([Setting(key="offline_mode", value=mode),
                    Setting(key="update_check_enabled", value="false"),
                    Setting(key="update_manifest_url", value="")])
        db.commit()
        features = {item["id"]: item for item in feature_status.build_feature_status(db)}
    official = features["official_update_check"]
    assert official["enabled"] and official["configured"]
    assert official["available"] is (mode != "strict")
    assert bool(official["blockers"]) is (mode == "strict")
    assert official["settings_target"]["anchor"] == "update-check"
    assert not official["restart_required"] and not official["safe_test_available"]
    assert features["update_check"]["lab_only"]
    assert features["local_update"]["lab_only"]
    assert not features["local_update"]["available"]
