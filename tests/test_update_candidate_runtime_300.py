"""Isolated release/snapshot fixtures and fake Docker; never contact hardware."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from basswiesn.update_helper import candidate_runtime as candidate
from basswiesn.update_helper import artifact_staging, installation_snapshot
from test_update_artifact_staging_300 import OfficialFixture, VERSION
from test_update_host_preflight_300 import enrolled, substitute_child
from test_update_installation_snapshot_300 import context, capture


pytestmark = pytest.mark.unit
JOB = "11223344-1234-4234-8234-123456789abc"
IMAGE = "sha256:" + "b" * 64
CONTAINER = "d" * 64


class DockerFixture:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.image = {"Id": IMAGE, "Os": "linux", "Architecture": "arm64",
            "Config": {"User": "basswiesn", "WorkingDir": "/app", "Entrypoint": None,
                "Cmd": ["python", "tools/run_dev.py"], "Labels": {
                    "org.opencontainers.image.version": VERSION,
                    "org.opencontainers.image.source": artifact_staging.REPOSITORY}}}
        self.container = {"Id": CONTAINER, "Image": IMAGE, "RestartCount": 0,
            "Name": "/basswiesn-update-check-" + JOB,
            "Config": {"User": "10001:10001", "Labels": {candidate.LABEL: JOB, candidate.ROLE: "validation"}},
            "State": {"Running": False, "Pid": 0, "OOMKilled": False, "StartedAt": "not-started"},
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False, "PortBindings": {}}}
        self.probe = {"ready": True, "version": VERSION, "validation_mode": True,
                      "sqlite_integrity": True, "schema_sha256": "a" * 64, "table_count": 12}

    def __call__(self, args, *, seconds):
        self.calls.append(list(args))
        assert 0 < seconds <= 900
        command = args[0]
        if command == self.failure:
            raise candidate.CandidateRejected("COMMAND_FAILED")
        if command == "build":
            return IMAGE.encode()
        if command == "inspect":
            assert args[1:3] in [["--type", "image"], ["--type", "container"]]
            value = self.image if args[2] == "image" else self.container
            assert args[3] in (value["Id"], value.get("Name", "").lstrip("/"))
            return json.dumps([value]).encode()
        if command == "create":
            assert args[-1] == IMAGE
            assert "--network" in args and args[args.index("--network")+1] == "none"
            assert not any(a in args for a in ("--privileged", "--publish", "-p", "--net=host"))
            assert args[args.index("--user")+1] == "10001:10001"
            env = Path(args[args.index("--env-file")+1]).read_text()
            assert "BASSWIESN_UPDATE_VALIDATION_MODE=1\n" in env
            assert "BASSWIESN_ENABLE_HTTPS=0\n" in env
            assert "fixture-admin-secret" not in str(args)
            return CONTAINER.encode()
        if command == "start":
            self.container["State"].update(Running=True, Pid=123, StartedAt="one-start")
            return CONTAINER.encode()
        if command == "stop":
            self.container["State"].update(Running=False, Pid=0)
            return CONTAINER.encode()
        if command == "exec":
            assert args == ["exec", CONTAINER, "python", "-I", "-B", "-c", candidate.PROBE]
            return json.dumps(self.probe).encode()
        if command == "rm":
            assert args == ["rm", CONTAINER] and not self.container["State"]["Running"]
            return CONTAINER.encode()
        pytest.fail("unexpected Docker action")


@pytest.fixture
def prepared(context, tmp_path):
    policy, backups, restores = context
    staging, candidates = tmp_path / "source-staging", tmp_path / "candidates"
    staging.mkdir(mode=0o700)
    candidates.mkdir(mode=0o700)
    artifact_staging.stage_release(JOB, VERSION, staging_root=str(staging), owner_uid=os.getuid(), fetch=OfficialFixture())
    docker = DockerFixture()
    runtime = candidate.CandidateRuntime(root=str(candidates), staging_root=str(staging),
        restore_root=str(restores), owner_uid=os.getuid(), run=docker)
    return runtime, docker, context


def validate(prepared, **kwargs):
    runtime, docker, context = prepared
    runtime.build(JOB, VERSION)
    receipt = capture(context)
    return runtime.validate(context[0], receipt, VERSION, assert_stopped=lambda _: True,
        backup_root=str(context[1]), environment=["BASSWIESN_TEST_CREDENTIAL=fixture-admin-secret"], **kwargs)


def test_verified_source_build_isolated_copy_start_health_schema_and_cleanup(prepared):
    runtime, docker, context = prepared
    before = (Path(context[0].root) / "data/app.db").read_bytes()
    result = validate(prepared)
    assert result["state"] == "CANDIDATE_VALIDATED" and not result["production_changed"]
    assert result["version"] == VERSION and result["schema_sha256"] == "a" * 64
    assert (Path(context[0].root) / "data/app.db").read_bytes() == before
    assert not docker.container["State"]["Running"]
    assert docker.calls[-1] == ["rm", CONTAINER]
    assert "fixture-admin-secret" not in json.dumps(result)
    assert (Path(runtime.root) / JOB / "validated.json").exists()
    assert (Path(runtime.root) / JOB / "candidate.env").stat().st_mode & 0o777 == 0o600
    for args in docker.calls:
        assert context[0].container_id not in args
        if args[0] == "create":
            assert not any(context[0].root in a for a in args)


def test_build_reservation_refuses_second_attempt(prepared):
    runtime, docker, _ = prepared
    runtime.build(JOB, VERSION)
    previous = len(docker.calls)
    with pytest.raises(candidate.CandidateRejected, match="^CANDIDATE_EXISTS$"):
        runtime.build(JOB, VERSION)
    assert len(docker.calls) == previous


def test_modified_source_rejected_before_any_build(prepared):
    runtime, docker, _ = prepared
    (Path(runtime.staging_root) / JOB / "source/Dockerfile").write_text("modified source")
    with pytest.raises(artifact_staging.StagingRejected):
        runtime.build(JOB, VERSION)
    assert docker.calls == []


@pytest.mark.parametrize("key,value", [("User", "root"), ("Entrypoint", ["sh"]),
    ("Cmd", ["sh", "-c", "bad"]), ("WorkingDir", "/")])
def test_candidate_image_runtime_contract_is_checked(prepared, key, value):
    runtime, docker, _ = prepared
    docker.image["Config"][key] = value
    with pytest.raises(candidate.CandidateRejected, match="^IMAGE_INVALID$"):
        runtime.build(JOB, VERSION)
    assert not any(a[0] == "create" for a in docker.calls)


@pytest.mark.parametrize("key,value", [("ready", False), ("version", "2.0.0"), ("validation_mode", False),
    ("sqlite_integrity", False), ("table_count", 0), ("table_count", True), ("schema_sha256", "bad")])
def test_false_health_schema_or_version_never_passes_and_stops_candidate(prepared, key, value):
    runtime, docker, _ = prepared
    docker.probe[key] = value
    with pytest.raises(ValueError):
        validate(prepared)
    assert docker.calls[-1] == ["rm", CONTAINER]
    assert not (Path(runtime.root) / JOB / "validated.json").exists()


def test_candidate_crash_cannot_look_ready(prepared):
    runtime, docker, _ = prepared
    real = docker.__call__
    def run(args, **kwargs):
        result = real(args, **kwargs)
        if args[0] == "exec":
            docker.container["State"].update(Running=False, Pid=0)
        return result
    runtime.run = run
    with pytest.raises(candidate.CandidateRejected, match="^CANDIDATE_UNHEALTHY$"):
        validate(prepared)
    assert docker.calls[-1] == ["rm", CONTAINER]


def test_cleanup_failure_does_not_write_success(prepared):
    runtime, docker, _ = prepared
    docker.failure = "stop"
    with pytest.raises(candidate.CandidateRejected):
        validate(prepared)
    assert not (Path(runtime.root) / JOB / "validated.json").exists()
    assert not any(a[0] == "rm" for a in docker.calls)


def test_wrong_container_label_never_authorizes_stop_or_remove(prepared):
    _, docker, _ = prepared
    docker.container["Config"]["Labels"][candidate.LABEL] = "different-owner"
    with pytest.raises(candidate.CandidateRejected, match="^CONTAINER_INVALID$"):
        validate(prepared)
    assert not any(a[0] in {"start", "stop", "rm"} for a in docker.calls)


@pytest.mark.parametrize("reply", ["timeout", "not-a-container-id", "non-ascii"])
def test_uncertain_create_recovers_only_exact_owned_container(prepared, reply):
    runtime, docker, _ = prepared
    real = docker.__call__
    def run(args, **kwargs):
        result = real(args, **kwargs)
        if args[0] == "create":
            if reply == "timeout":
                raise candidate.CandidateRejected("COMMAND_TIMEOUT")
            return b"\xff" if reply == "non-ascii" else reply.encode()
        return result
    runtime.run = run
    with pytest.raises(candidate.CandidateRejected):
        validate(prepared)
    assert docker.calls[-1] == ["rm", CONTAINER]
    assert not any(a[0] == "start" for a in docker.calls)
    assert not (Path(runtime.root) / JOB / "validated.json").exists()


@pytest.mark.parametrize("different", ["label", "image", "name", "unavailable"])
def test_uncertain_create_never_removes_unproven_target(prepared, different):
    runtime, docker, _ = prepared
    real = docker.__call__
    def run(args, **kwargs):
        if args[0] == "inspect" and args[-1] == "basswiesn-update-check-" + JOB:
            if different == "unavailable":
                raise candidate.CandidateRejected("COMMAND_FAILED")
            value = deepcopy(docker.container)
            if different == "label":
                value["Config"]["Labels"][candidate.LABEL] = "other"
                docker.container = value
            elif different == "image":
                value["Image"] = "sha256:" + "f" * 64
                docker.container = value
            else:
                value["Name"] = "/unrelated-container"
            return json.dumps([value]).encode()
        result = real(args, **kwargs)
        if args[0] == "create":
            raise candidate.CandidateRejected("COMMAND_TIMEOUT")
        return result
    runtime.run = run
    with pytest.raises(candidate.CandidateRejected, match="^CLEANUP_FAILED$"):
        validate(prepared)
    assert not any(a[0] in {"start", "stop", "rm"} for a in docker.calls)
    assert not (Path(runtime.root) / JOB / "validated.json").exists()


@pytest.mark.parametrize("value", [None, ["BROKEN"], ["X=1", "X=2"], ["BASSWIESN_X=line\nsecond"],
    ["BASSWIESN_X=a\x00b"], ["=value"], [True], ["x=" + "s" * 16384]])
def test_environment_refuses_ambiguous_secret_boundaries(value):
    with pytest.raises(candidate.CandidateRejected, match="^ENVIRONMENT_INVALID$"):
        candidate.environment_bytes(value)


def test_env_does_not_inherit_code_injection_and_does_not_evaluate_shell():
    result = candidate.environment_bytes(["PYTHONPATH=/unsafe", "LD_PRELOAD=/unsafe", "PATH=/unsafe",
        "BASSWIESN_X=$(not-executed)", "BASSWIESN_UPDATE_VALIDATION_MODE=0", "PROTECTED_DEVICE_IDS=EXAMPLE"])
    assert b"unsafe" not in result
    assert b"BASSWIESN_X=$(not-executed)\n" in result
    assert b"BASSWIESN_UPDATE_VALIDATION_MODE=1\n" in result
    assert b"PROTECTED_DEVICE_IDS=EXAMPLE\n" in result


@pytest.mark.parametrize("mode", ["ok", "error", "timeout", "overflow"])
def test_bounded_real_command_child_no_host_context_or_output_leak(monkeypatch, tmp_path, mode):
    monkeypatch.setattr(candidate.local_docker, "_trusted_tools", lambda: None)
    monkeypatch.setattr(candidate.local_docker, "CLIENT_CONFIG", str(tmp_path))
    seen = []
    code = {"ok": "print('approved-result')", "error": "raise RuntimeError('secret-fixture')",
        "timeout": "import time;time.sleep(5)", "overflow": "print('x' * (2*1024*1024+1))"}[mode]
    substitute_child(monkeypatch, code, seen)
    if mode == "ok":
        assert candidate._command(["start", CONTAINER], seconds=1) == b"approved-result\n"
    else:
        with pytest.raises(candidate.CandidateRejected) as error:
            candidate._command(["start", CONTAINER], seconds=0.2)
        assert "secret-fixture" not in str(error.value)
    args, options = seen[0]
    assert args[:4] == ["/usr/bin/docker", "--host", "unix:///run/docker.sock", "--config"]
    assert str(Path(args[4]).parent) == str(tmp_path)
    assert not Path(args[4]).exists()
    assert not list(tmp_path.glob("command-*"))
    assert options["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    assert options["stderr"] is subprocess.DEVNULL and not options.get("shell")


@pytest.mark.parametrize("title", ["basswiesn WebGUI", "basswiesn Cloud Emulator", "basswiesn Diagnostics"])
def test_validation_boot_never_starts_background_or_event_tasks(monkeypatch, tmp_path, title):
    from basswiesn.app import main
    from basswiesn.app.routers import api
    from basswiesn.app.config import get_settings
    from basswiesn.app.services.research_runtime import ResearchRuntime
    settings = get_settings().model_copy(update={"update_validation_mode": True, "data_dir": tmp_path})
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    def unexpected(*args, **kwargs):
        pytest.fail("validation startup must not start hardware/background work")
    monkeypatch.setattr(main, "start_owned_task", unexpected)
    monkeypatch.setattr(ResearchRuntime, "start", unexpected)
    monkeypatch.setattr(ResearchRuntime, "enable_event_tasks", unexpected)
    app = FastAPI(title=title, lifespan=main.lifespan)
    app.state.database_schema_prepared = True
    app.add_api_route("/api/readiness", api.readiness)
    with TestClient(app) as client:
        response = client.get("/api/readiness")
        assert response.status_code == 200
        assert response.json()["update_validation_mode"] is True
