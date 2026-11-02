"""A repository file is not necessarily present in the distributed archive."""
from pathlib import Path

import pytest

from tools.check_package_links import check_links

pytestmark = pytest.mark.unit


def test_packaged_relative_links_and_synthetic_external_links(tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text("# Guide\n")
    (tmp_path / "README.md").write_text(
        "[guide](guide.md#section) [website](https://example.invalid/not-contacted)\n"
        "[same page](#section) [mail](mailto:example@example.invalid)\n"
        "```sh\n[not a navigation link](missing-example)\n```\n"
        "~~~text\n[also an example](missing-example)\n~~~\n"
        "[reference]: guide.md \"Title\"\n"
    )
    assert check_links(tmp_path) == []


@pytest.mark.parametrize("link", ["[bad](absent.md)", "![bad](absent.svg)", "[bad]: absent.md"])
def test_missing_destination_is_rejected(tmp_path: Path, link: str) -> None:
    (tmp_path / "README.md").write_text(link)
    assert len(check_links(tmp_path)) == 1


def test_repository_neighbor_cannot_satisfy_archive_link(tmp_path: Path) -> None:
    root = tmp_path / "package"
    root.mkdir()
    (tmp_path / "private.md").write_text("Not distributed")
    (root / "README.md").write_text("[outside](../private.md)")
    assert "out-of-package" in check_links(root)[0]


def test_symlink_does_not_import_external_documentation(tmp_path: Path) -> None:
    root = tmp_path / "package"
    root.mkdir()
    (tmp_path / "private.md").write_text("Do not follow me")
    (root / "linked.md").symlink_to(tmp_path / "private.md")
    assert "symlinked documentation" in check_links(root)[0]


def test_encoded_and_angle_bracket_paths(tmp_path: Path) -> None:
    (tmp_path / "two words.md").write_text("# Example")
    (tmp_path / "README.md").write_text("[one](two%20words.md) [two](<two words.md>)")
    assert check_links(tmp_path) == []
