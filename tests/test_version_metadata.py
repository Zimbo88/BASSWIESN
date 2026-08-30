from pathlib import Path

from fastapi.testclient import TestClient

from basswiesn import __version__
from basswiesn.app.config import get_settings
from basswiesn.app.main import create_web_app


def test_app_version_is_exposed_consistently():
    assert __version__ == "2.5.1"
    assert get_settings().version == __version__

    with TestClient(create_web_app()) as client:
        dashboard = client.get("/").text
        remote = client.get("/remote/abc123").text
        health = client.get("/api/health").json()
        settings = client.get("/api/system/settings").json()

    assert 'data-version=' not in dashboard
    assert "Version wird geladen · Host nicht gesetzt" in dashboard
    assert 'data-version=' not in remote
    assert "basswiesn remote · Version nicht verfügbar" in remote
    assert f"/static/app.css?v={__version__}" in dashboard
    assert f"/static/app.js?v={__version__}" in dashboard
    assert f"/static/remote.js?v={__version__}" in remote
    assert health["version"] == __version__
    assert health["ok"] is True
    assert settings["version"] == __version__


def test_preserved_env_cannot_override_the_running_release_version(monkeypatch):
    monkeypatch.setenv("BASSWIESN_VERSION", "1.6.0")
    get_settings.cache_clear()
    try:
        assert get_settings().version == __version__
    finally:
        get_settings.cache_clear()


def test_version_and_html_cache_policies_are_upgrade_safe():
    with TestClient(create_web_app()) as client:
        dashboard = client.get("/")
        health = client.get("/api/health")
        static = client.get(f"/static/app.js?v={__version__}")

    assert dashboard.headers["cache-control"] == "no-cache"
    assert health.headers["cache-control"] == "no-store"
    assert static.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert 'viewport-fit=cover' in dashboard.text


def test_release_manifest_includes_single_source_version():
    script = Path("tools/package_release.sh").read_text(encoding="utf-8")
    assert "from basswiesn import __version__" in script
    assert '"version": __version__' in script
    assert 'f"# release-context {__version__}' in script


def test_source_installers_cannot_reuse_a_stale_application_layer():
    for path in (Path("install.sh"), Path("tools/install_release.sh")):
        script = path.read_text(encoding="utf-8")
        assert "DOCKER_BUILDKIT=0 docker compose build --pull --no-cache" in script
        assert "docker compose up -d --force-recreate" in script


def test_no_stale_v101_fallback_in_active_user_facing_sources():
    stale_release = ".".join(["1", "0", "1"])
    paths = [
        Path("basswiesn/app/main.py"),
        Path("basswiesn/app/config.py"),
        Path("basswiesn/app/static/app.js"),
        Path("basswiesn/app/static/remote.js"),
        Path("basswiesn/app/routers/cloud.py"),
        Path("basswiesn/app/routers/debug.py"),
    ]
    stale = {str(path): path.read_text(encoding="utf-8").count(stale_release) for path in paths}
    assert stale == {str(path): 0 for path in paths}
import pytest as _pytest_marker
pytestmark = [_pytest_marker.mark.integration, _pytest_marker.mark.release]
