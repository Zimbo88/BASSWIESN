"""Installer return status is tied to the selected service's actual readiness.

The Docker executable is synthetic. These tests never access a daemon or LAN.
"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("state,expected", [("healthy", 0), ("unhealthy", 1), ("starting", 1),
                                            ("missing", 1), ("ambiguous", 1), ("inspect-failed", 1)])
def test_installation_requires_observed_health_without_enrolling_updater(tmp_path, state, expected):
    shutil.copy2("install.sh", tmp_path / "install.sh")
    (tmp_path / ".env.example").write_text("BASSWIESN_LAN_HOST=192.0.2.42\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    scripts = {
        "docker": '''#!/bin/bash
if [[ "$1 $2" == 'compose ps' ]]; then
  [[ "$FIXTURE_HEALTH" == missing ]] && exit 0
  printf '%064d\\n' 1
  [[ "$FIXTURE_HEALTH" == ambiguous ]] && printf '%064d\\n' 2
  exit 0
fi
if [[ "$1" == inspect ]]; then
  [[ "$FIXTURE_HEALTH" == inspect-failed ]] && exit 77
  printf '%s\\n' "$FIXTURE_HEALTH"
  exit 0
fi
[[ "$1" == compose || "$1" == info ]] && exit 0
exit 78
''',
        "ip": "#!/bin/sh\nexit 0\n",
        "sleep": "#!/bin/sh\nexit 0\n",
        "sudo": "#!/bin/sh\nexit 79\n",
    }
    for name, body in scripts.items():
        (binaries / name).write_text(body)
        (binaries / name).chmod(0o755)
    result = subprocess.run(["/bin/bash", str(tmp_path / "install.sh"), "--no-updater"],
                            env={"PATH": str(binaries) + ":/usr/bin:/bin", "FIXTURE_HEALTH": state},
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    assert result.returncode == expected, result.stderr
    assert ("BASSWIESN is ready:" in result.stdout) == (expected == 0)
    assert not (tmp_path / ".env.before-update-helper").exists()
