"""A failing test interpreter must never produce a qualified release archive."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("runner", ["test_release.sh", "package_release.sh", "paired"])
def test_first_failure_is_reported_and_packaging_stops(tmp_path, runner):
    calls = tmp_path / "calls.jsonl"
    interpreter = tmp_path / "fake-python"
    interpreter.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"with open({str(calls)!r}, 'a') as log: log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if '-m' in sys.argv and 'pytest' in sys.argv:\n"
        "    print('SYNTHETIC FIRST TEST FAILURE')\n"
        "    sys.exit(23)\n"
        "if sys.argv[1:] == ['-']: print('3.0.2')\n"
    )
    interpreter.chmod(0o700)
    output = tmp_path / "release-output"
    command = ["bash", str(ROOT / "tools" / ("package_release.sh" if runner == "paired" else runner))]
    if runner in {"package_release.sh", "paired"}:
        command += ["--output-dir", str(output)]
    if runner == "paired":
        command += ["--repro-output-dir", str(tmp_path / "second")]
    result = subprocess.run(
        command, cwd=ROOT,
        env={**os.environ, "BASSWIESN_PYTHON": str(interpreter), "TMPDIR": str(tmp_path)},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert "SYNTHETIC FIRST TEST FAILURE" in result.stdout
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    tests = [args for args in invocations if "pytest" in args]
    assert len(tests) == 1
    assert "--maxfail=1" in tests[0] and "--tb=short" in tests[0]
    assert "not hardware" in tests[0]
    assert not list(output.glob("*.tar.gz"))
    assert not (output / "manifest.json").exists()
    assert not list((tmp_path / "second").glob("*.tar.gz"))
    if runner != "test_release.sh":
        saved = output / "failed-software-gate.log"
        assert "SYNTHETIC FIRST TEST FAILURE" in saved.read_text()
        assert saved.stat().st_mode & 0o777 == 0o600


def test_paired_staging_runs_one_mandatory_gate_and_builds_two_real_archives(tmp_path):
    calls = tmp_path / "gate-calls"
    interpreter = tmp_path / "gate-probe"
    # Substitute only pytest in this orchestration test. Real version, lock,
    # syntax, language, privacy, manifest and archive validators still execute.
    interpreter.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if sys.argv[1:3] == ['-m', 'pytest']:\n"
        f"    with open({str(calls)!r}, 'a') as log: log.write('gate\\n')\n"
        "    print('1 passed in 0.01s')\n"
        "    sys.exit(0)\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
    )
    interpreter.chmod(0o700)
    first, second = tmp_path / "first", tmp_path / "second"
    result = subprocess.run(
        ["bash", str(ROOT / "tools/package_release.sh"), "--output-dir", str(first),
         "--repro-output-dir", str(second)], cwd=ROOT,
        env={**os.environ, "BASSWIESN_PYTHON": str(interpreter), "SOURCE_DATE_EPOCH": "0",
             "TMPDIR": str(tmp_path)}, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout[-2500:] + result.stderr[-1500:]
    assert calls.read_text().splitlines() == ["gate"]
    assert (first / "basswiesn-docker-release.tar.gz").read_bytes() == (second / "basswiesn-docker-release.tar.gz").read_bytes()
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
