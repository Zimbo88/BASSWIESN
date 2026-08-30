import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from basswiesn import __version__
from basswiesn.app.main import create_web_app


pytestmark = [pytest.mark.unit, pytest.mark.release]


def test_release_candidate_version_is_consistent_across_read_only_status_api():
    with TestClient(create_web_app()) as client:
        health = client.get("/api/health").json()
        version = client.get("/api/version").json()
        readiness = client.get("/api/readiness").json()

    assert __version__ == "2.5.1"
    assert health["version"] == __version__
    assert version["version"] == __version__
    assert version["build_type"] == "Stable Release"
    assert readiness["version"] == __version__


def test_release_packaging_uses_single_source_version_and_excludes_local_env():
    script = Path("tools/package_release.sh").read_text(encoding="utf-8")
    dockerignore = Path(".dockerignore").read_text(encoding="utf-8")

    assert 'from basswiesn import __version__' in script
    assert 'ARCHIVE="$DIST/basswiesn-docker-release-${VERSION}.tar.gz"' in script
    assert "!.env.example" in dockerignore
    assert ".env\n" in dockerignore


def test_container_includes_public_feature_documentation():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "COPY FEATURES.md RELEASE_CHECKLIST.md ./" in dockerfile


def test_release_docs_expose_portable_runtime_protected_device_contract():
    checklist = Path("RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
    env_example = Path(".env.example").read_text(encoding="utf-8")
    config_source = Path("basswiesn/app/config.py").read_text(encoding="utf-8")

    assert "basswiesn-docker-release-1.5.0.tar.gz" not in checklist
    assert "basswiesn-docker-release-2.5.1.tar.gz" in checklist
    assert "Do not push, tag or create a GitHub release" in checklist
    assert "while a critical gate is failed or\nunverified" in checklist
    assert "PROTECTED_DEVICE_IPS=" in env_example
    assert "PROTECTED_DEVICE_IDS=" in env_example
    assert "installation-specific radio identities" in config_source
    assert "192.168.50.25" not in env_example
    assert "CCDDEEFF0011" not in env_example
    assert "192.168.50.25" not in config_source
    assert "CCDDEEFF0011" not in config_source


def test_release_candidate_manifest_contract_is_versioned():
    manifest = Path("dist/manifest.json")
    archive = Path(f"dist/basswiesn-docker-release-{__version__}.tar.gz")
    if not archive.exists() or not manifest.exists():
        pytest.skip("release artifact is created after the software test gate")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["version"] == __version__
