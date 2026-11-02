from pathlib import Path

import pytest

from tools.check_dependency_lock import check, pins

pytestmark = pytest.mark.unit


def test_reviewed_dependency_locks() -> None:
    assert check(Path(__file__).resolve().parents[1]) == []


@pytest.mark.parametrize("text", ["httpx>=0.27", "httpx==0.28.1", "", "-e .", "https://example.invalid/wheel"])
def test_unlocked_dependency_is_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        pins(text)


def test_pins_accept_markers_and_normalize_package_names() -> None:
    digest = "a" * 64
    assert pins(f'Example_Package==1.0 ; sys_platform == "linux" --hash=sha256:{digest}') == {
        "example-package": {"1.0"}
    }


def test_changed_direct_requirement_requires_regeneration(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ("requirements.in", "requirements-dev.in", "requirements.lock", "requirements-dev.lock", "dependency-lock.json"):
        (tmp_path / name).write_bytes((root / name).read_bytes())
    (tmp_path / "requirements.in").write_text("httpx>=999\n")
    assert any("regeneration" in error for error in check(tmp_path))
