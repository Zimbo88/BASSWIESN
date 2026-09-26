"""Synthetic, offline preflight: no production files, Docker daemon or radios."""
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from basswiesn.update_helper import host_preflight as host
from basswiesn.update_helper import local_docker as docker


pytestmark = pytest.mark.unit


def digest(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def enrolled(tmp_path):
    root = tmp_path / "fixture-install"
    root.mkdir(mode=0o755)
    (root / "basswiesn").mkdir(mode=0o755)
    (root / "data").mkdir(mode=0o700)
    (root / "data/secrets").mkdir(mode=0o700)
    (root / "data/secrets/setup-rebuild").mkdir(mode=0o700)
    files = {}
    for name in host.RELEASE_FILES:
        content = b"synthetic trusted release fixture, not executable\n"
        (root / name).write_bytes(content)
        (root / name).chmod(0o644)
        files[name] = digest(content)
    env = b'EXAMPLE_SETTING="$(never-execute-this)"\n'
    (root / ".env").write_bytes(env)
    (root / ".env").chmod(0o600)
    body = {"schema": 1, "installation_id": "12345678-1234-4234-8234-123456789abc",
            "root": str(root), "installation_owner_uid": os.getuid(), "compose_project": "example",
            "container_id": "c" * 64, "image_id": "sha256:" + "a" * 64,
            "version": "2.6.5", "file_sha256": files, "env_sha256": digest(env)}
    private = tmp_path / "private-policy"
    private.mkdir(mode=0o700)
    policy_path = private / "enrollment.json"
    policy_path.write_text(json.dumps(body))
    policy_path.chmod(0o600)
    return host.Enrollment.parse(json.dumps(body).encode()), body, policy_path


def observed(policy):
    labels = {"com.docker.compose.project": policy.compose_project,
              "com.docker.compose.service": "basswiesn", "com.docker.compose.oneoff": "False",
              "com.docker.compose.project.working_dir": policy.root,
              "com.docker.compose.project.config_files": policy.root + "/docker-compose.yml"}
    config = {"User": "basswiesn", "WorkingDir": "/app", "Entrypoint": None,
              "Cmd": ["python", "tools/run_dev.py"], "Labels": labels,
              "Env": ["SYNTHETIC_SECRET=DO_NOT_REPORT_THIS_VALUE"]}
    image_config = deepcopy(config)
    image_config["Labels"] = {"org.opencontainers.image.source": host.SOURCE,
                              "org.opencontainers.image.version": policy.version}
    container = {"Id": policy.container_id, "Image": policy.image_id, "RestartCount": 0,
        "State": {"Running": True, "Paused": False, "Restarting": False, "OOMKilled": False,
                  "Dead": False, "Status": "running", "Pid": 12345, "StartedAt": "synthetic-start",
                  "Health": {"Status": "healthy", "Log": [{"Output": "DO_NOT_REPORT_THIS_VALUE"}]}},
        "Config": config,
        "HostConfig": {"Privileged": False, "ReadonlyRootfs": True, "Init": True,
            "CapDrop": ["ALL"], "CapAdd": None, "SecurityOpt": ["no-new-privileges:true"],
            "NetworkMode": policy.compose_project + "_default", "PidMode": "", "IpcMode": "private",
            "UTSMode": "", "UsernsMode": "", "Devices": [], "DeviceRequests": None,
            "VolumesFrom": None, "Links": None, "RestartPolicy": {"Name": "unless-stopped"},
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"},
            "PortBindings": {p: [{"HostIp": "", "HostPort": p.split("/")[0]}] for p in host.PORTS}},
        "Mounts": [{"Type": "bind", "Source": policy.root + "/data", "Destination": "/app/data",
                    "RW": True, "Propagation": "rprivate"},
                   {"Type": "bind", "Source": policy.root + "/data/secrets/setup-rebuild",
                    "Destination": "/app/secrets/setup-rebuild", "RW": False, "Propagation": "rprivate"}]}
    image = {"Id": policy.image_id, "Os": "linux", "Architecture": "arm64", "Config": image_config}
    daemon = {"OSType": "linux", "SecurityOptions": ["name=seccomp,profile=builtin"],
              "Swarm": {"LocalNodeState": "inactive"}}
    return host.RuntimeObservation(daemon, container, image, host.ProcessIdentity(12345, 10001, 10001, True, 500))


def test_pass_is_only_a_layout_check_not_onboarding_or_install_approval(enrolled):
    policy, body, path = enrolled
    loaded = host.load_enrollment(str(path), policy_owner_uid=os.getuid())
    assert loaded == policy
    result = host.evaluate(policy, observation=observed(policy))
    assert result["layout_checks_passed"]
    assert not result["installation_available"] and not result["backup_restore_verified"]
    assert not result["onboarding_complete"]
    output = json.dumps(result)
    for forbidden in (policy.root, policy.container_id, policy.image_id, policy.env_sha256, "DO_NOT_REPORT_THIS_VALUE"):
        assert forbidden not in output
        assert forbidden not in repr(policy)
        assert forbidden not in repr(observed(policy))


def test_no_implicit_docker_or_shell_or_environment_execution(enrolled, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("preflight must not implicitly start processes")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    result = host.evaluate(enrolled[0])
    assert result["checks"][0]["status"] == "PASS"
    assert result["checks"][1] == {"check": "runtime_layout", "status": "UNKNOWN", "code": "RUNTIME_NOT_OBSERVED"}
    assert not result["layout_checks_passed"]


@pytest.mark.parametrize("change", [
    {"schema": True}, {"schema": 2}, {"installation_id": "bad"}, {"root": "/"},
    {"root": "/tmp/../wrong"}, {"root": "/tmp/a/"}, {"root": "/tmp//a"},
    {"root": "//tmp/a"}, {"root": "/tmp/a\n"}, {"root": "/tmp/a:b"},
    {"installation_owner_uid": True}, {"installation_owner_uid": 10001}, {"installation_owner_uid": -1},
    {"container_id": "--host=tcp://example.invalid"}, {"image_id": "latest"},
    {"compose_project": "../bad"}, {"compose_project": "-evil"}, {"version": "2.6.5-rc1"},
    {"file_sha256": {}}, {"env_sha256": "bad"}, {"shell": "anything"},
])
def test_policy_rejects_extra_fields_and_ambiguous_identity(enrolled, change):
    _, body, _ = enrolled
    body.update(change)
    with pytest.raises(host.HostRejected, match="^POLICY_INVALID$"):
        host.Enrollment.parse(json.dumps(body).encode())


@pytest.mark.parametrize("payload", [b"", b"[]", b"null", b"\xff", b"x" * 16385,
    b'{"schema":1,"schema":1}', b'{"value":NaN}'])
def test_policy_json_strict_and_bounded(payload):
    with pytest.raises(host.HostRejected, match="^POLICY_INVALID$"):
        host.Enrollment.parse(payload)


def test_approved_file_hashes_cannot_be_mutated(enrolled):
    with pytest.raises(TypeError):
        enrolled[0].file_sha256["Dockerfile"] = "b" * 64


@pytest.mark.parametrize("kind", ["mode", "parent-mode", "symlink", "hardlink", "fifo"])
def test_enrollment_file_and_parent_are_private_regular_owned_inputs(enrolled, kind):
    _, _, path = enrolled
    if kind == "mode":
        path.chmod(0o644)
    elif kind == "parent-mode":
        path.parent.chmod(0o755)
    else:
        saved = path.with_suffix(".saved")
        path.rename(saved)
        if kind == "symlink":
            path.symlink_to(saved)
        elif kind == "hardlink":
            os.link(saved, path)
        else:
            os.mkfifo(path, mode=0o600)
    with pytest.raises(host.HostRejected):
        host.load_enrollment(str(path), policy_owner_uid=os.getuid())


@pytest.mark.parametrize("filename", [*sorted(host.RELEASE_FILES), ".env"])
def test_any_enrolled_configuration_change_blocks_update(enrolled, filename):
    policy, _, _ = enrolled
    (Path(policy.root) / filename).write_bytes(b"modified fixture content")
    result = host.evaluate(policy)
    assert result["checks"][0]["code"] == "CONFIG_CHANGED"
    assert not result["installation_available"]


@pytest.mark.parametrize("filename,mode", [("Dockerfile", 0o666), (".env", 0o644), ("Dockerfile", 0o4755)])
def test_unsafe_config_permissions_rejected_without_repair(enrolled, filename, mode):
    policy, _, _ = enrolled
    path = Path(policy.root) / filename
    path.chmod(mode)
    assert host.evaluate(policy)["checks"][0]["code"] == "FILE_UNSAFE"
    assert path.stat().st_mode & 0o7777 == mode


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "deleted", "oversize"])
def test_unsafe_config_types_never_read_arbitrary_target(enrolled, tmp_path, kind):
    policy, _, _ = enrolled
    path = Path(policy.root) / "Dockerfile"
    saved = tmp_path / "saved-file"
    path.rename(saved)
    if kind == "symlink":
        path.symlink_to(saved)
    elif kind == "hardlink":
        os.link(saved, path)
    elif kind == "fifo":
        os.mkfifo(path, mode=0o600)
    elif kind == "oversize":
        with path.open("wb") as handle:
            handle.truncate(host.MAX_CONFIG_BYTES + 1)
    result = host.evaluate(policy)
    assert result["checks"][0]["status"] == "FAIL"


def test_directory_symlinks_shared_write_and_overrides_are_rejected(enrolled, tmp_path):
    policy, body, _ = enrolled
    alias = tmp_path / "alias"
    alias.symlink_to(policy.root, target_is_directory=True)
    body["root"] = str(alias)
    assert host.evaluate(host.Enrollment.parse(json.dumps(body).encode()))["checks"][0]["code"] == "UNSAFE_PATH"
    root = Path(policy.root)
    root.chmod(0o777)
    assert host.evaluate(policy)["checks"][0]["code"] == "UNSAFE_PATH"
    root.chmod(0o755)
    (root / "docker-compose.override.yml").write_text("unapproved override")
    assert host.evaluate(policy)["checks"][0]["code"] == "COMPOSE_MISMATCH"


@pytest.mark.parametrize("relative", ["data", "data/secrets", "data/secrets/setup-rebuild"])
def test_every_runtime_mount_source_component_rejects_symlinks(enrolled, tmp_path, relative):
    policy = enrolled[0]
    path = Path(policy.root) / relative
    saved = tmp_path / "relocated-runtime"
    path.rename(saved)
    path.symlink_to(saved, target_is_directory=True)
    assert host.evaluate(policy)["checks"][0]["code"] == "UNSAFE_PATH"


def test_concurrent_file_replacement_is_detected(enrolled, monkeypatch):
    policy, _, _ = enrolled
    root = Path(policy.root)
    original = host.os.read
    replaced = False
    def race(fd, count):
        nonlocal replaced
        data = original(fd, count)
        if not replaced:
            replaced = True
            # Replace every possible first selected config with same-content
            # new inode, not just changed content. The descriptor stays old.
            for filename in policy.file_sha256:
                path = root / filename
                temporary = path.with_name(path.name + ".replacement")
                temporary.write_bytes(path.read_bytes())
                os.replace(temporary, path)
        return data
    monkeypatch.setattr(host.os, "read", race)
    assert host.evaluate(policy)["checks"][0]["code"] == "FILE_CHANGED"


@pytest.mark.parametrize("change,code", [
    (("daemon", "OSType", "windows"), "DOCKER_UNSUPPORTED"),
    (("daemon", "SecurityOptions", ["name=rootless"]), "DOCKER_UNSUPPORTED"),
    (("daemon", "SecurityOptions", ["name=userns"]), "DOCKER_UNSUPPORTED"),
    (("daemon", "Swarm", {"LocalNodeState": "active"}), "DOCKER_UNSUPPORTED"),
    (("container", "Id", "b" * 64), "CONTAINER_MISMATCH"),
    (("container", "Image", "sha256:" + "b" * 64), "CONTAINER_MISMATCH"),
    (("image", "Id", "sha256:" + "b" * 64), "IMAGE_MISMATCH"),
    (("image", "Architecture", "unsupported"), "IMAGE_MISMATCH"),
])
def test_daemon_and_immutable_id_mismatch_rejected(enrolled, change, code):
    policy = enrolled[0]
    observation = observed(policy)
    object_name, key, value = change
    getattr(observation, object_name)[key] = value
    with pytest.raises(host.HostRejected, match="^" + code + "$"):
        host.inspect_runtime(policy, observation)


@pytest.mark.parametrize("key,value", [("Running", False), ("Running", 1), ("Paused", True),
    ("Restarting", True), ("OOMKilled", True), ("Dead", True), ("Health", {"Status": "starting"})])
def test_unhealthy_container_not_ready(enrolled, key, value):
    policy = enrolled[0]
    observation = observed(policy)
    observation.container["State"][key] = value
    with pytest.raises(host.HostRejected, match="^CONTAINER_UNHEALTHY$"):
        host.inspect_runtime(policy, observation)


@pytest.mark.parametrize("change", [
    {"Privileged": True}, {"ReadonlyRootfs": False}, {"Init": False}, {"CapDrop": []},
    {"CapAdd": ["SYS_ADMIN"]}, {"CapAdd": {}}, {"SecurityOpt": []}, {"NetworkMode": "host"},
    {"PidMode": "host"}, {"IpcMode": "host"}, {"UTSMode": "host"}, {"UsernsMode": "host"},
    {"Devices": [{"PathOnHost": "/dev/example"}]}, {"VolumesFrom": ["other"]},
    {"Links": ["other"]}, {"DeviceRequests": [{"Driver": "other"}]},
    {"RestartPolicy": {"Name": "always"}}, {"Tmpfs": {"/tmp": "rw"}},
])
def test_unsafe_or_custom_runtime_config_rejected(enrolled, change):
    policy = enrolled[0]
    observation = observed(policy)
    observation.container["HostConfig"].update(change)
    with pytest.raises(host.HostRejected, match="^RUNTIME_UNSAFE$"):
        host.inspect_runtime(policy, observation)


@pytest.mark.parametrize("section", ["container", "image"])
@pytest.mark.parametrize("change", [{"User": "root"}, {"User": "0:0"}, {"WorkingDir": "/"},
    {"Entrypoint": ["sh"]}, {"Cmd": ["sh", "custom-script.sh"]}])
def test_container_and_image_entrypoint_are_fixed(enrolled, section, change):
    policy = enrolled[0]
    observation = observed(policy)
    getattr(observation, section)["Config"].update(change)
    with pytest.raises(host.HostRejected, match="^RUNTIME_UNSAFE$"):
        host.inspect_runtime(policy, observation)


@pytest.mark.parametrize("key", ["com.docker.compose.project", "com.docker.compose.service",
    "com.docker.compose.oneoff", "com.docker.compose.project.working_dir", "com.docker.compose.project.config_files"])
def test_project_and_service_binding_required(enrolled, key):
    policy = enrolled[0]
    observation = observed(policy)
    observation.container["Config"]["Labels"][key] = "different"
    with pytest.raises(host.HostRejected, match="^COMPOSE_MISMATCH$"):
        host.inspect_runtime(policy, observation)


@pytest.mark.parametrize("change", [{"Source": "/unapproved/data"}, {"Type": "volume"},
    {"Destination": "/run/docker.sock"}, {"Propagation": "rshared"}, {"RW": False}])
def test_mount_contract_is_not_arbitrary_host_access(enrolled, change):
    policy = enrolled[0]
    observation = observed(policy)
    observation.container["Mounts"][0].update(change)
    with pytest.raises(host.HostRejected, match="^MOUNTS_UNSUPPORTED$"):
        host.inspect_runtime(policy, observation)


def test_additional_mount_or_writable_setup_secret_alias_rejected(enrolled):
    policy = enrolled[0]
    observation = observed(policy)
    observation.container["Mounts"].append(deepcopy(observation.container["Mounts"][0]))
    with pytest.raises(host.HostRejected, match="^MOUNTS_UNSUPPORTED$"):
        host.inspect_runtime(policy, observation)
    observation = observed(policy)
    observation.container["Mounts"][1]["RW"] = True
    with pytest.raises(host.HostRejected, match="^MOUNTS_UNSUPPORTED$"):
        host.inspect_runtime(policy, observation)


@pytest.mark.parametrize("key", ["org.opencontainers.image.version", "org.opencontainers.image.source"])
def test_image_version_and_repository_labels_must_match_enrollment(enrolled, key):
    policy = enrolled[0]
    observation = observed(policy)
    observation.image["Config"]["Labels"][key] = "different"
    with pytest.raises(host.HostRejected, match="^IMAGE_MISMATCH$"):
        host.inspect_runtime(policy, observation)


@pytest.mark.parametrize("value", [{}, {"1328/tcp": []}, {"1328/tcp": [{"HostIp": "", "HostPort": "80"}]}])
def test_custom_port_mapping_not_silently_replaced(enrolled, value):
    policy = enrolled[0]
    observation = observed(policy)
    observation.container["HostConfig"]["PortBindings"] = value
    with pytest.raises(host.HostRejected, match="^PORTS_UNSUPPORTED$"):
        host.inspect_runtime(policy, observation)


@pytest.mark.parametrize("change", [{"pid": 5678}, {"uid": 0}, {"gid": 0}, {"identity_uid_map": False}, {"start_ticks": 0}])
def test_actual_host_process_identity_required_not_just_container_user(enrolled, change):
    policy = enrolled[0]
    observation = observed(policy)
    observation = replace(observation, process=replace(observation.process, **change))
    with pytest.raises(host.HostRejected, match="^PROCESS_IDENTITY_UNVERIFIED$"):
        host.inspect_runtime(policy, observation)


@pytest.mark.parametrize("time_kind", ["old", "future", "nan", "bool"])
def test_stale_or_invalid_runtime_observation_never_authorizes_cutover(enrolled, time_kind):
    policy = enrolled[0]
    captured = {"old": time.monotonic() - 16, "future": time.monotonic() + 60, "nan": float("nan"), "bool": True}[time_kind]
    observation = replace(observed(policy), captured_monotonic=captured)
    with pytest.raises(host.HostRejected, match="^OBSERVATION_EXPIRED$"):
        host.inspect_runtime(policy, observation)


def test_space_budget_is_explicit_and_not_backup_success(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    free = SimpleNamespace(f_flag=0, f_frsize=4096, f_bavail=1000, f_favail=1000)
    monkeypatch.setattr(os, "fstatvfs", lambda fd: free)
    host.inspect_space(str(tmp_path), owner_uid=os.getuid(), required_bytes=1024)
    for field, value in [("f_bavail", 0), ("f_favail", 1), ("f_flag", os.ST_RDONLY)]:
        previous = getattr(free, field)
        setattr(free, field, value)
        with pytest.raises(host.HostRejected, match="^INSUFFICIENT_SPACE$"):
            host.inspect_space(str(tmp_path), owner_uid=os.getuid(), required_bytes=1024)
        setattr(free, field, previous)
    with pytest.raises(host.HostRejected, match="^POLICY_INVALID$"):
        host.inspect_space(str(tmp_path), owner_uid=os.getuid(), required_bytes=True)


def process_stat(ticks=500):
    fields = ["S"] + ["0"] * 18 + [str(ticks)]
    return "12345 (name with ) parentheses) " + " ".join(fields)


@pytest.mark.parametrize("uid_text,code", [
    ("Uid:\t10001\t10001\t10001\t10001\nGid:\t10001\t10001\t10001\t10001\n", None),
    ("Uid:\t0\t10001\t0\t0\nGid:\t10001\t10001\t10001\t10001\n", "PROCESS_IDENTITY_UNVERIFIED"),
    ("Uid: 1\n", "PROCESS_IDENTITY_UNVERIFIED"),
    ("Uid: 1 1 1 1\nUid: 1 1 1 1\nGid: 1 1 1 1\n", "PROCESS_IDENTITY_UNVERIFIED"),
])
def test_proc_identity_parser_does_not_read_environ_or_secret_config(monkeypatch, uid_text, code):
    calls = []
    def read(pid, name):
        calls.append(name)
        return process_stat() if name == "stat" else uid_text if name == "status" else "0 0 4294967295\n"
    monkeypatch.setattr(docker, "_proc_read", read)
    if code:
        with pytest.raises(host.HostRejected, match="^" + code + "$"):
            docker.process_identity(12345)
    else:
        assert docker.process_identity(12345) == host.ProcessIdentity(12345, 10001, 10001, True, 500)
    assert set(calls) <= {"stat", "status", "uid_map", "gid_map"}


def test_process_reuse_detected_between_proc_reads(monkeypatch):
    ticks = iter((500, 600))
    def read(pid, name):
        return process_stat(next(ticks)) if name == "stat" else "Uid: 10001 10001 10001 10001\nGid: 10001 10001 10001 10001\n" if name == "status" else "0 0 4294967295"
    monkeypatch.setattr(docker, "_proc_read", read)
    with pytest.raises(host.HostRejected, match="^OBSERVATION_CHANGED$"):
        docker.process_identity(12345)


def test_collector_uses_only_read_only_exact_local_queries(enrolled, monkeypatch):
    policy = enrolled[0]
    fixture = observed(policy)
    calls = []
    monkeypatch.setattr(docker, "_trusted_tools", lambda: None)
    monkeypatch.setattr(docker, "process_identity", lambda pid: fixture.process)
    def read(args, deadline):
        calls.append(args)
        return fixture.daemon if args[0] == "info" else [fixture.image] if args[2] == "image" else [fixture.container]
    monkeypatch.setattr(docker, "_read_command", read)
    result = docker.collect(policy)
    host.inspect_runtime(policy, result)
    assert calls == [["info", "--format", "{{json .}}"],
                     ["inspect", "--type", "container", policy.container_id],
                     ["inspect", "--type", "image", policy.image_id],
                     ["inspect", "--type", "container", policy.container_id]]


def test_collector_rejects_restart_during_inspection(enrolled, monkeypatch):
    policy = enrolled[0]
    fixture = observed(policy)
    count = 0
    monkeypatch.setattr(docker, "_trusted_tools", lambda: None)
    monkeypatch.setattr(docker, "process_identity", lambda pid: fixture.process)
    def read(args, deadline):
        nonlocal count
        if args[0] == "info":
            return fixture.daemon
        if args[2] == "image":
            return [fixture.image]
        count += 1
        value = deepcopy(fixture.container)
        value["RestartCount"] = count
        return [value]
    monkeypatch.setattr(docker, "_read_command", read)
    with pytest.raises(host.HostRejected, match="^OBSERVATION_CHANGED$"):
        docker.collect(policy)


def test_non_root_collector_denied_before_any_file_or_process_access(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "stat", lambda *a, **kw: pytest.fail("must reject before file access"))
    with pytest.raises(host.HostRejected, match="^LOCAL_DOCKER_UNSAFE$"):
        docker._trusted_tools()


@pytest.mark.parametrize("change", [None, "binary-link", "binary-owner", "binary-write", "binary-setuid",
    "binary-noexec", "run-owner", "run-write", "socket-file", "socket-owner", "socket-world-write", "client-config"])
def test_local_docker_prerequisite_metadata_is_checked(monkeypatch, change):
    facts = {
        "binary": SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0),
        "run": SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0),
        "socket": SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=0),
    }
    if change == "binary-link":
        facts["binary"].st_mode = stat.S_IFLNK | 0o777
    elif change == "binary-owner":
        facts["binary"].st_uid = 1000
    elif change == "binary-write":
        facts["binary"].st_mode |= 0o020
    elif change == "binary-setuid":
        facts["binary"].st_mode |= 0o4000
    elif change == "binary-noexec":
        facts["binary"].st_mode = stat.S_IFREG | 0o644
    elif change == "run-owner":
        facts["run"].st_uid = 1000
    elif change == "run-write":
        facts["run"].st_mode |= 0o020
    elif change == "socket-file":
        facts["socket"].st_mode = stat.S_IFREG | 0o660
    elif change == "socket-owner":
        facts["socket"].st_uid = 1000
    elif change == "socket-world-write":
        facts["socket"].st_mode |= 0o002
    @contextmanager
    def directory(path, **kwargs):
        assert path in {"/usr/bin", docker.CLIENT_CONFIG}
        yield 99
    monkeypatch.setattr(docker, "directory", directory)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "open", lambda path, flags: 98 if path == "/run" else pytest.fail("unexpected open"))
    monkeypatch.setattr(os, "stat", lambda name, **kwargs: facts["binary"] if name == "docker" else facts["socket"])
    monkeypatch.setattr(os, "fstat", lambda fd: facts["run"])
    monkeypatch.setattr(os, "close", lambda fd: None)
    monkeypatch.setattr(os, "listdir", lambda fd: ["config.json"] if change == "client-config" else [])
    if change is None:
        docker._trusted_tools()
    else:
        with pytest.raises(host.HostRejected, match="^LOCAL_DOCKER_UNSAFE$"):
            docker._trusted_tools()


def test_collector_mismatch_stops_before_proc_read(enrolled, monkeypatch):
    policy = enrolled[0]
    fixture = observed(policy)
    fixture.container["Id"] = "a" * 64
    monkeypatch.setattr(docker, "_trusted_tools", lambda: None)
    monkeypatch.setattr(docker, "_read_command", lambda args, deadline: fixture.daemon if args[0] == "info" else [fixture.container])
    monkeypatch.setattr(docker, "process_identity", lambda pid: pytest.fail("must not inspect unrelated process"))
    with pytest.raises(host.HostRejected, match="^CONTAINER_MISMATCH$"):
        docker.collect(policy)


def test_stopped_container_is_classified_without_opening_proc(enrolled, monkeypatch):
    policy = enrolled[0]
    fixture = observed(policy)
    fixture.container["State"].update({"Running": False, "Pid": 0})
    monkeypatch.setattr(docker, "_trusted_tools", lambda: None)
    monkeypatch.setattr(docker, "_read_command", lambda args, deadline: fixture.daemon if args[0] == "info" else
                        [fixture.image] if args[2] == "image" else [fixture.container])
    monkeypatch.setattr(docker, "process_identity", lambda pid: pytest.fail("must not open /proc for a stopped container"))
    with pytest.raises(host.HostRejected, match="^CONTAINER_UNHEALTHY$"):
        docker.collect(policy)


@pytest.mark.parametrize("pid,name", [(True, "stat"), (0, "stat"), (-1, "stat"), (2**31, "stat"),
    ("1/../self", "stat"), (1, "environ"), (1, "root/etc/passwd")])
def test_proc_path_cannot_expand_to_unapproved_information(monkeypatch, pid, name):
    monkeypatch.setattr(os, "open", lambda *a, **kw: pytest.fail("invalid /proc access"))
    with pytest.raises(host.HostRejected, match="^PROCESS_IDENTITY_UNVERIFIED$"):
        docker._proc_read(pid, name)


@pytest.mark.parametrize("args", [["ps"], ["start", "c" * 64], ["exec", "anything"],
    ["inspect", "--type", "container", "--host=tcp://example.invalid"],
    ["inspect", "--type", "image", "latest"], ["info", "--format", "unapproved-format"]])
def test_command_runner_rejects_mutations_and_option_injection(monkeypatch, args):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("must not execute"))
    with pytest.raises(host.HostRejected, match="^LOCAL_DOCKER_UNSAFE$"):
        docker._read_command(args, time.monotonic() + 1)


def substitute_child(monkeypatch, code, seen):
    original = subprocess.Popen
    def start(args, **kwargs):
        seen.append((args, kwargs.copy()))
        # A local synthetic Python child replaces Docker. There is NO daemon
        # connection in this test. Environment/deadline/size code is exercised.
        return original([sys.executable, "-c", code], **kwargs)
    monkeypatch.setattr(subprocess, "Popen", start)


def test_real_subprocess_reader_has_fixed_host_and_sanitized_environment(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(docker, "CLIENT_CONFIG", str(tmp_path))
    monkeypatch.setenv("DOCKER_HOST", "tcp://example.invalid:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "unapproved")
    monkeypatch.setenv("HTTP_PROXY", "http://example.invalid")
    substitute_child(monkeypatch, 'print("{\\\"synthetic\\\":true}")', seen)
    assert docker._read_command(["info", "--format", "{{json .}}"], time.monotonic() + 2) == {"synthetic": True}
    args, options = seen[0]
    assert args[:4] == ["/usr/bin/docker", "--host", "unix:///run/docker.sock", "--config"]
    assert Path(args[4]).parent == tmp_path and not Path(args[4]).exists()
    assert options["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    assert options["cwd"] == "/" and options.get("shell", False) is False
    assert options["stderr"] is subprocess.DEVNULL


@pytest.mark.parametrize("code", ['print("invalid-secret-content")',
    'import sys; print("private stderr", file=sys.stderr); sys.exit(3)',
    'import time; time.sleep(2)', 'print("x" * (2 * 1024 * 1024 + 1))'])
def test_subprocess_bad_json_failure_timeout_overflow_do_not_leak(monkeypatch, tmp_path, code):
    seen = []
    monkeypatch.setattr(docker, "CLIENT_CONFIG", str(tmp_path))
    substitute_child(monkeypatch, code, seen)
    with pytest.raises(host.HostRejected, match="^LOCAL_DOCKER_UNAVAILABLE$"):
        docker._read_command(["info", "--format", "{{json .}}"], time.monotonic() + 0.25)


def test_cli_missing_untrusted_policy_prints_only_fixed_code(tmp_path):
    path = tmp_path / "PRIVATE_PATH_MUST_NOT_BE_PRINTED.json"
    result = subprocess.run([sys.executable, "tools/check_update_host.py", "--policy", str(path)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    body = json.loads(result.stdout)
    assert not body["installation_available"]
    assert "PRIVATE_PATH_MUST_NOT_BE_PRINTED" not in result.stdout + result.stderr
    assert not result.stderr
