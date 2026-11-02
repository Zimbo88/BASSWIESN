"""No Docker daemon or network in this harness test."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def test_runtime_probe_preserves_backups_and_never_claims_historical_hardware(tmp_path):
    working = tmp_path / "empty-working-directory"
    working.mkdir()
    result = subprocess.run([sys.executable, str(Path("tools/probe_migrations.py").resolve())],
                            cwd=working, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "PASS"
    assert report["interruption_recovery"] == "PASS"
    assert report["hardware"] == report["host_helper_cutover"] == "NOT_RUN"
    assert len(report["profiles"]) == 3
    assert all(row["restore_and_restart"] == "PASS" for row in report["profiles"])
    assert not list(working.iterdir())
