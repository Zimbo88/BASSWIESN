import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def test_benchmark_is_isolated_and_does_not_claim_pi_or_hardware_performance(tmp_path):
    script = Path("tools/benchmark_offline.py").resolve()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    result = subprocess.run([sys.executable, str(script), "--samples", "10", "--history-rows", "100"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["hardware_validation"] == "NOT_RUN"
    assert report["network_access"] == "BLOCKED"
    assert report["pi_performance"] == "NOT_MEASURED"
    assert set(report["metrics"]) == {"presets_cache_6", "history_100", "station_catalog_100", "schema_inspection"}
    assert all(item["p95_ms"] >= item["median_ms"] > 0 for item in report["metrics"].values())
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
