"""Offline module behavior gate included in normal pytest/CI."""

from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.unit


def test_frontend_modules_with_node():
    files = sorted(str(path) for path in Path("tests/js").glob("*.test.mjs"))
    assert files
    result = subprocess.run(["node", "--test", *files], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
