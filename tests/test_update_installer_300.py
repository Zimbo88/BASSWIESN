"""Installer wiring and rejection tests; no systemd/Docker/host modification."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tools.configure_update_env import configure
from tools import install_update_helper as installer
from test_update_host_preflight_300 import enrolled, observed
from basswiesn.update_helper import host_preflight as host

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("effective,sudo,expected", [
    (1000, None, 1000), (1000, "2000", 1000), (0, "1000", 1000), (0, None, 0), (0, "0", 0),
])
def test_installer_binds_invoking_administrator_not_final_root_owner(monkeypatch, effective, sudo, expected):
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: effective)
    if sudo is None:
        monkeypatch.delenv("SUDO_UID", raising=False)
    else:
        monkeypatch.setenv("SUDO_UID", sudo)
    @contextmanager
    def guard(path, **kwargs):
        calls.append((path, kwargs)); yield 0
    monkeypatch.setattr(host, "directory", guard)
    assert installer.installation_owner("/srv/example-install") == expected
    assert calls == [("/srv/example-install", {"owners": {0, expected}})]


@pytest.mark.parametrize("value", ["", "-1", "01", "1.0", "1000\n", "4294967295", "10001", "not-an-id"])
def test_installer_rejects_invalid_sudo_actor_before_path_access(monkeypatch, value):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", value)
    def unexpected(*args, **kwargs):
        raise AssertionError("filesystem must not be opened")
    monkeypatch.setattr(host, "directory", unexpected)
    with pytest.raises(ValueError, match="INVALID_INSTALLATION_ADMINISTRATOR"):
        installer.installation_owner("/srv/example-install")


def test_installer_rejects_application_actor(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: host.APP_UID)
    monkeypatch.setenv("SUDO_UID", "0")
    with pytest.raises(ValueError, match="INVALID_INSTALLATION_ADMINISTRATOR"):
        installer.installation_owner("/srv/example-install")


def test_installer_uses_real_nofollow_ownership_guard(tmp_path, monkeypatch):
    actor = os.getuid()
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", str(actor))
    assert installer.installation_owner(tmp_path) == actor
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(host.HostRejected):
        installer.installation_owner(linked)
    # A root login cannot silently trust a different user's directory ancestry.
    if actor != 0:
        monkeypatch.delenv("SUDO_UID")
        with pytest.raises(host.HostRejected):
            installer.installation_owner(tmp_path)


def test_configuration_preserves_original_and_never_sources_shell(tmp_path):
    before = b'EXAMPLE="$(never-execute)"\nBASSWIESN_ENABLE_HTTPS=false\n'
    path = tmp_path / ".env"
    path.write_bytes(before)
    configure(tmp_path)
    assert (tmp_path / ".env.before-update-helper").read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text().count("BASSWIESN_ENABLE_HTTPS=") == 1
    assert "BASSWIESN_ENABLE_HTTPS=true\n" in path.read_text()
    assert "BASSWIESN_HTTPS_PORT=1329\n" in path.read_text()
    assert "BASSWIESN_UPDATE_CONTROL_DIR=/run/basswiesn-update\n" in path.read_text()
    assert 'EXAMPLE="$(never-execute)"' in path.read_text()
    saved = path.read_bytes()
    with pytest.raises(FileExistsError):
        configure(tmp_path)
    assert path.read_bytes() == saved


def test_configuration_rejects_symlink(tmp_path):
    other = tmp_path / "other"
    other.write_text("untouched")
    (tmp_path / ".env").symlink_to(other)
    with pytest.raises(OSError):
        configure(tmp_path)
    assert other.read_text() == "untouched"


@pytest.mark.parametrize("source,writable,accepted", [
    ("/run/basswiesn-update", False, True), ("/run", False, False),
    ("/run/docker.sock", False, False), ("/var/lib/basswiesn-update", False, False),
    ("/run/basswiesn-update", True, False),
])
def test_socket_mount_contract_never_allows_host_privilege_mounts(enrolled, source, writable, accepted):
    policy = enrolled[0]
    observation = observed(policy)
    observation.container["Mounts"].append({"Type": "bind", "Source": source,
        "Destination": "/run/basswiesn-update", "RW": writable, "Propagation": "rprivate"})
    if accepted:
        host.inspect_runtime(policy, observation)
    else:
        with pytest.raises(host.HostRejected):
            host.inspect_runtime(policy, observation)


def test_installer_requires_admin_explicit_approval(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit):
        installer.main(["bootstrap", "--approve-root-helper"])


def test_installer_never_echoes_sensitive_exception(monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    def fail():
        raise RuntimeError("SYNTHETIC_SECRET_NOT_FOR_OUTPUT")
    monkeypatch.setattr(installer, "bootstrap", fail)
    assert installer.main(["bootstrap", "--approve-root-helper"]) == 2
    text = capsys.readouterr().out
    assert "SYNTHETIC_SECRET" not in text
    assert json.loads(text)["partial_state_retained"] is True


@pytest.mark.parametrize("arguments", [["--unknown"], ["--enable-updater", "--no-updater"]])
def test_shell_rejects_ambiguous_arguments_before_any_action(tmp_path, arguments):
    shutil.copy2("install.sh", tmp_path / "install.sh")
    before = sorted(p.name for p in tmp_path.iterdir())
    result = subprocess.run(["/bin/bash", str(tmp_path / "install.sh"), *arguments],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    assert result.returncode == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_missing_docker_engine_does_not_bootstrap_or_create_data(tmp_path):
    shutil.copy2("install.sh", tmp_path / "install.sh")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text('#!/bin/sh\n[ "$1" = compose ] && exit 0\nexit 1\n')
    docker.chmod(0o755)
    result = subprocess.run(["/bin/bash", str(tmp_path / "install.sh"), "--enable-updater"],
        env={"PATH": str(binaries)+":/usr/bin:/bin"}, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    assert result.returncode == 1
    assert b"Docker daemon is not reachable" in result.stderr
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / ".env").exists()


def test_unattended_default_never_enrolls_privileged_updater(tmp_path):
    # Real install.sh control flow, simulated Docker CLI. This is a shell
    # contract test, not a claim of an actual Docker install.
    shutil.copy2("install.sh", tmp_path / "install.sh")
    (tmp_path / ".env.example").write_text("BASSWIESN_LAN_HOST=192.0.2.42\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text('#!/bin/sh\n[ "$1 $2" = "compose ps" ] && { printf "%064d\\n" 1; exit 0; }\n'
                      '[ "$1" = inspect ] && { echo healthy; exit 0; }\n'
                      '[ "$1" = compose ] && exit 0\n[ "$1" = info ] && exit 0\nexit 70\n')
    docker.chmod(0o755)
    ip = binaries / "ip"
    ip.write_text("#!/bin/sh\nexit 0\n")
    ip.chmod(0o755)
    sudo = binaries / "sudo"
    sudo.write_text("#!/bin/sh\nexit 71\n")
    sudo.chmod(0o755)
    result = subprocess.run(["/bin/bash", str(tmp_path / "install.sh")],
        env={"PATH": str(binaries)+":/usr/bin:/bin"}, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    assert result.returncode == 0, result.stderr.decode()
    assert (tmp_path / "data/update-disabled").is_dir()
    assert not (tmp_path / ".env.before-update-helper").exists()
    assert "BASSWIESN_UPDATE_CONTROL_DIR" not in (tmp_path / ".env").read_text()
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("reason", ["unsupported_arch", "low_disk", "symlink_data", "symlink_env"])
def test_readonly_preflight_stops_before_any_installation_mutation(tmp_path, reason):
    shutil.copy2("install.sh", tmp_path / "install.sh")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text('#!/bin/sh\n[ "$1" = compose ] && [ "$2" = version ] && exit 0\n[ "$1" = info ] && exit 0\nexit 70\n')
    docker.chmod(0o755)
    if reason == "unsupported_arch":
        (binaries / "uname").write_text("#!/bin/sh\necho armv7l\n")
        (binaries / "uname").chmod(0o755)
    if reason == "low_disk":
        (binaries / "df").write_text("#!/bin/sh\nprintf 'Filesystem blocks Used Available Capacity Mounted\\nfixture 100 90 10 90%% /\\n'\n")
        (binaries / "df").chmod(0o755)
    if reason.startswith("symlink"):
        target = tmp_path / "unrelated"
        target.mkdir()
        (tmp_path / ("data" if reason == "symlink_data" else ".env")).symlink_to(target)
    before = sorted(path.name for path in tmp_path.iterdir())
    result = subprocess.run(["/bin/bash", str(tmp_path / "install.sh"), "--no-updater"],
        env={"PATH": str(binaries) + ":/usr/bin:/bin"}, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    assert result.returncode == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_installer_does_not_recursively_widen_private_files():
    source = Path("install.sh").read_text()
    assert "chmod -R g+rwX" not in source
    assert "chgrp -R" not in source
    assert "-path /app/data/secrets -prune" in source
    assert "-type d ! -user 10001" in source
    assert "Existing database is not writable by UID 10001" in source


def test_compatibility_installer_delegates_arguments_status_and_uses_project_root(tmp_path):
    (tmp_path / "tools").mkdir()
    shutil.copy2("tools/install_release.sh", tmp_path / "tools/install_release.sh")
    (tmp_path / "install.sh").write_text(
        '#!/bin/bash\n[[ $# == 2 && "$1" == "--example" && "$2" == "two words" ]] || exit 99\nexit 73\n'
    )
    result = subprocess.run(["/bin/bash", str(tmp_path / "tools/install_release.sh"), "--example", "two words"],
                            cwd="/", capture_output=True, timeout=5)
    assert result.returncode == 73
    assert not (tmp_path / "tools/data").exists()
    assert not (tmp_path / "data").exists()
