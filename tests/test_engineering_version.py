"""Negative release-version tests, with no application or network startup."""
import json
from pathlib import Path

import pytest

from tools.check_version import check_version, project_version

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def fixture_tree(tmp_path: Path) -> Path:
    (tmp_path / "basswiesn").mkdir()
    for name in ("basswiesn/__init__.py", "Dockerfile", ".env.example"):
        (tmp_path / name).write_text((ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def test_checked_tree_uses_package_version() -> None:
    assert check_version(ROOT) == []


def test_docker_drift_is_rejected(tmp_path: Path) -> None:
    root = fixture_tree(tmp_path)
    docker = root / "Dockerfile"
    docker.write_text(docker.read_text().replace(f"ARG BASSWIESN_VERSION={project_version(root)}", "ARG BASSWIESN_VERSION=0.0.0"))
    assert "Docker version argument differs" in check_version(root)[0]


@pytest.mark.parametrize("declaration", ["BASSWIESN_VERSION=0.0.0\n", "", "BASSWIESN_VERSION=5.0.0\nBASSWIESN_VERSION=5.0.0\n"])
def test_distributed_environment_drift_is_rejected(tmp_path: Path, declaration: str) -> None:
    root = fixture_tree(tmp_path)
    (root / ".env.example").write_text(declaration)
    assert any("environment version" in error for error in check_version(root))


def test_missing_distributed_environment_is_rejected(tmp_path: Path) -> None:
    root = fixture_tree(tmp_path)
    (root / ".env.example").unlink()
    assert any("environment version" in error for error in check_version(root))


def test_static_label_cannot_bypass_check(tmp_path: Path) -> None:
    root = fixture_tree(tmp_path)
    docker = root / "Dockerfile"
    docker.write_text(docker.read_text().replace('${BASSWIESN_VERSION}', '0.0.0'))
    assert any("OCI version label" in item for item in check_version(root))


def test_build_override_guard_cannot_disappear(tmp_path: Path) -> None:
    root = fixture_tree(tmp_path)
    docker = root / "Dockerfile"
    docker.write_text(docker.read_text().replace("assert __version__ == sys.argv[1]", "assert True"))
    assert any("override" in item for item in check_version(root))


def test_artifact_and_manifest_versions_must_match(tmp_path: Path) -> None:
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(json.dumps({"version": project_version(ROOT)}))
    artifact = tmp_path / f"basswiesn-docker-release-{project_version(ROOT)}.tar.gz"
    assert check_version(ROOT, manifest=manifest, artifact=artifact) == []
    manifest.write_text('{"version":"0.0.0"}')
    assert len(check_version(ROOT, manifest=manifest, artifact=tmp_path / "basswiesn-docker-release-0.0.0.tar.gz")) == 2


def test_version_reader_does_not_execute_package(tmp_path: Path) -> None:
    root = fixture_tree(tmp_path)
    (root / "basswiesn/__init__.py").write_text('__version__ = "3.0.2"\nraise RuntimeError("must not execute")\n')
    assert project_version(root) == "3.0.2"


@pytest.mark.parametrize("declaration", ['__version__ = get_version()', '__version__ = "3.0.2"\n__version__ = "3.0.3"', '__version__ = "../escape"'])
def test_invalid_version_fails_closed(tmp_path: Path, declaration: str) -> None:
    root = fixture_tree(tmp_path)
    (root / "basswiesn/__init__.py").write_text(declaration)
    with pytest.raises(ValueError):
        project_version(root)
