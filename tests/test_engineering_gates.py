from pathlib import Path
import re
import base64
import json
import tomllib
import os
import subprocess
import sys

import pytest
import yaml

from tools.check_coverage import check

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_coverage_checks_lines_and_branches_independently():
    summary = {"covered_lines": 90, "num_statements": 100, "covered_branches": 7, "num_branches": 10}
    report = {"totals": summary, "files": {"safety.py": {"summary": summary}}}
    assert check(report, {"minimums": {"global": {"line": 90, "branch": 70}}}) == []
    failures = check(report, {"minimums": {"global": {"line": 91, "branch": 71}}})
    assert len(failures) == 2
    assert check(report, {"minimums": {"missing.py": {"line": 1, "branch": 1}}})
    assert check(report, {"minimums": {}})


def test_declarative_module_has_no_missing_branches_but_new_branches_are_checked():
    summary = {"covered_lines": 4, "num_statements": 4, "covered_branches": 0, "num_branches": 0}
    report = {"totals": summary, "files": {"registry.py": {"summary": summary}}}
    baseline = {"minimums": {"global": {"line": 100, "branch": 100}, "registry.py": {"line": 100, "branch": 100}}}
    assert check(report, baseline) == []
    summary["num_branches"] = 2
    summary["covered_branches"] = 1
    failures = check(report, baseline)
    assert len(failures) == 2
    assert all("branch 50.00% < 100.00%" in message for message in failures)


def test_ci_action_pins_and_no_privileged_events():
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        text = path.read_text()
        workflow = yaml.safe_load(text)
        assert workflow["permissions"] == {"contents": "read"}
        # PyYAML's YAML 1.1 loader recognizes GitHub's "on" key as True.
        events = workflow.get("on", workflow.get(True))
        assert events["push"] == {"branches": ["main"], "tags": ["v*"]}
        assert "pull_request" in events and "workflow_dispatch" in events
        assert "pull_request_target" not in text
        assert "self-hosted" not in text
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"])


def test_release_keeps_required_checks_and_locked_runtime():
    docker = (ROOT / "Dockerfile").read_text()
    assert "--require-hashes --only-binary=:all:" in docker
    assert "USER basswiesn" in docker
    assert re.search(r"^ARG PYTHON_BASE=python:3\.12\.[0-9]+-alpine3\.24@sha256:[0-9a-f]{64}$", docker, re.M)
    assert docker.count("FROM ${PYTHON_BASE}") == 2
    assert "COPY --from=ssh-client-build /openssh-client/ /usr/local/" in docker
    assert "tools/build_ssh_client.py tools/openssh-source.json" in docker
    runtime = docker.rsplit("FROM ${PYTHON_BASE}", 1)[1]
    assert "build-essential" not in runtime
    assert "build-base" not in runtime and "linux-headers" not in runtime
    assert "apk add --no-cache openssl sshpass bash tzdata" in runtime
    assert "&& pip check" in runtime
    assert "openssh-client openssl" not in runtime
    workflow = (ROOT / ".github/workflows/quality.yml").read_text()
    for command in ("make test-fast", "make test-integration", "make test-ui", "make test-release"):
        assert "bash tools/ci_offline.sh " + command in workflow
    assert 'cmp "$RUNNER_TEMP/build-one/' in workflow
    smoke = (ROOT / "tools/test_container_offline.sh").read_text()
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "noexec,nosuid"):
        assert flag in smoke
    assert "docker.sock" not in smoke
    # Keep the old-image compatibility check; Alpine does not install PCRE2.
    assert "'dpkg', '--compare-versions', version, 'ge', '10.46-1~deb13u3'" in smoke
    assert "Unreviewed Alpine family" in smoke and "Reassess added PCRE2 dependency" in smoke
    assert "ZoneInfo('Europe/Berlin')" in smoke


def test_emulator_allowance_is_explicit_and_cannot_change_native_health_deadline():
    smoke = (ROOT / "tools/test_container_offline.sh").read_text()
    assert '${BASSWIESN_TEST_EMULATED:-0}' in smoke
    emulated = smoke.split('if [[ "$emulation" == 1 ]]; then', 1)[1].split('\nfi', 1)[0]
    assert "docker image inspect" in emulated and "== arm64" in emulated
    assert "docker info" in emulated and "x86_64|amd64" in emulated
    assert "--health-timeout=30s" in emulated
    assert "--timeout=8s" in (ROOT / "Dockerfile").read_text()
    assert 'BASSWIESN_TEST_EMULATED' not in (ROOT / '.github/workflows/quality.yml').read_text()


def test_explicit_test_python_is_not_overridden_by_local_venv():
    for name in ("test_fast.sh", "test_integration.sh", "test_ui.sh", "test_release.sh", "package_release.sh"):
        assert '[[ -z "${BASSWIESN_PYTHON:-}" && -x .venv/bin/python ]]' in (ROOT / "tools" / name).read_text()


def test_arm64_runtime_and_both_architecture_security_gates_are_explicit():
    quality = yaml.safe_load((ROOT / ".github/workflows/quality.yml").read_text())
    arm = quality["jobs"]["arm64-runtime"]
    assert arm["runs-on"] == "ubuntu-24.04-arm"
    run = next(step["run"] for step in arm["steps"] if "run" in step)
    assert 'test "$(uname -m)" = aarch64' in run
    assert "--network none --read-only --cap-drop ALL" in run
    assert "tools/probe_migrations.py" in run
    assert "required validation" in run
    assert "arm64-runtime" in quality["jobs"]["release-verification"]["needs"]
    assert "needs" not in arm
    assert "needs" not in quality["jobs"]["release-tests"]
    gate = quality["jobs"]["release-verification"]
    assert set(gate["needs"]) == {"fast", "integration", "browser", "arm64-runtime", "release-tests"}
    assert gate["if"] == "${{ always() }}"
    gate_step = gate["steps"][0]
    assert gate_step["env"]["GATE_RESULTS"] == "${{ toJSON(needs) }}"
    # Exercise the actual aggregate script: a cancelled/skipped dependency is
    # never silently promoted to a passing publication gate.
    command = gate_step["run"].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    results = {name: {"result": "success"} for name in gate["needs"]}
    for result, expected in (("success", 0), ("failure", 1), ("skipped", 1), ("cancelled", 1)):
        results["arm64-runtime"]["result"] = result
        process = subprocess.run([sys.executable, "-c", command],
                                 env={**os.environ, "GATE_RESULTS": json.dumps(results)}, capture_output=True)
        assert process.returncode == expected
    security = yaml.safe_load((ROOT / ".github/workflows/security.yml").read_text())
    matrix = security["jobs"]["container"]["strategy"]["matrix"]["include"]
    assert {(row["runner"], row["architecture"]) for row in matrix} == {
        ("ubuntu-24.04", "amd64"), ("ubuntu-24.04-arm", "arm64"),
    }


def test_container_failure_retains_evidence_without_waiving_security_gate():
    workflow = yaml.safe_load((ROOT / ".github/workflows/security.yml").read_text())
    steps = workflow["jobs"]["container"]["steps"]
    scan = next(step for step in steps if step.get("id") == "scan")
    assert scan["with"]["exit-code"] == "1"
    assert scan["with"]["ignore-unfixed"] is False
    assert "continue-on-error" not in scan
    assert scan["with"]["output"] == "container-vulnerabilities.json"
    sbom = next(step for step in steps if step.get("name") == "Container SBOM")
    upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
    for step in (sbom, upload):
        assert "always()" in step["if"]
        assert "steps.image.outcome == 'success'" in step["if"]
    assert "steps.scan.outcome == 'failure'" in sbom["if"]
    assert set(upload["with"]["path"].split()) == {
        "container-sbom.json", "container-sbom-packages.json", "ssh-source-component.json",
        "container-vulnerabilities.json",
    }
    assert "check_ssh_provenance.py" in sbom["run"]
    assert "--network none --read-only --cap-drop ALL" in sbom["run"]
    assert upload["with"]["if-no-files-found"] == "error"


def test_release_epoch_is_not_a_runtime_backup_clock():
    script = (ROOT / "tools/package_release.sh").read_text()
    assert 'env -u SOURCE_DATE_EPOCH "$PYTHON" -m pytest -m "not hardware" -q' in script
    assert '--mtime="@$SOURCE_DATE_EPOCH"' in script
    result = subprocess.run(
        ["env", "-u", "SOURCE_DATE_EPOCH", sys.executable, "-c",
         "import io, os, zipfile; assert 'SOURCE_DATE_EPOCH' not in os.environ; "
         "z=zipfile.ZipFile(io.BytesIO(), 'w'); z.writestr('snapshot.json', '{}'); z.close()"],
        env={**os.environ, "SOURCE_DATE_EPOCH": "0"}, capture_output=True, timeout=10,
    )
    assert result.returncode == 0


@pytest.mark.parametrize("caller_mask", ["0002", "0022", "0077"])
def test_release_permissions_do_not_inherit_caller_mask(caller_mask, tmp_path):
    script = (ROOT / "tools/package_release.sh").read_text()
    # Exercise the real shell preamble without starting packaging, application
    # processes or transports. Archive acceptance separately checks final modes.
    preamble = script.split("ROOT_DIR=", 1)[0]
    result = subprocess.run(
        ["bash", "-c", 'umask "$1"\n' + preamble + "\numask", "mask-probe", caller_mask],
        cwd=tmp_path, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "0022"


def test_secret_exception_only_covers_two_synthetic_provider_descriptors():
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert config["extend"]["useDefault"] is True
    assert len(config["rules"]) == 1
    rule = config["rules"][0]
    assert rule["id"] == "generic-api-key"
    allow, = rule["allowlists"]
    assert allow["condition"] == "AND"
    assert allow["regexTarget"] == "secret"
    for label in ("local-internet-radio", "tunein"):
        value = base64.b64encode(json.dumps({"serial": label}, separators=(",", ":")).encode()).decode()
        assert re.fullmatch(allow["regexes"][0], value)
    assert not re.fullmatch(allow["regexes"][0], "completely-different-value")
    assert not re.search(allow["paths"][0], "another/module.py")
