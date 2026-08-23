import subprocess

from basswiesn.app.adapters.ssh import build_legacy_ssh_command
from tools.live_device_ssh_preflight import (
    check_device,
    empty_result,
    parse_preflight_output,
)


PREFLIGHT_OUTPUT = """BASSWIESN_SSH_OK
__BASSWIESN_HOSTNAME__
taigan
__BASSWIESN_DATE__
Sun Jun 21 10:00:00 CEST 2026
__BASSWIESN_UPTIME__
10:00:00 up 2:00
__BASSWIESN_UNAME__
Linux taigan 3.14 armv7l GNU/Linux
__BASSWIESN_INTERFACES__
eth0 Link encap:Ethernet
inet addr:192.0.2.10
__BASSWIESN_REMOTE_SERVICES__
true
__BASSWIESN_SETTINGS_DIR__
false
__BASSWIESN_PATHS__
/mnt/nv
"""


def test_central_ssh_command_contains_all_legacy_options_and_batchmode():
    command = build_legacy_ssh_command("192.0.2.10", "root", "hostname", connect_timeout=8)

    for option in (
        "BatchMode=yes",
        "HostKeyAlgorithms=+ssh-rsa",
        "PubkeyAcceptedAlgorithms=+ssh-rsa",
        "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
        "Ciphers=+aes256-cbc,aes128-cbc",
        "ConnectTimeout=8",
        "StrictHostKeyChecking=no",
        "UserKnownHostsFile=/dev/null",
        "LogLevel=ERROR",
    ):
        assert option in command


def test_preflight_parser_extracts_success_marker_and_fields():
    parsed = parse_preflight_output(PREFLIGHT_OUTPUT)

    assert parsed["ssh_login_ok"] is True
    assert parsed["hostname"] == "taigan"
    assert parsed["interfaces"].endswith("inet addr:192.0.2.10")
    assert parsed["remote_services_present"] == "true"
    assert parsed["settings_dir_present"] == "false"


def test_tcp_check_and_runner_are_mockable_and_output_has_required_fields():
    class Connection:
        def close(self):
            return None

    def connection_factory(address, timeout):
        assert address == ("192.0.2.10", 22)
        assert timeout == 8
        return Connection()

    def runner(command, **kwargs):
        assert "BatchMode=yes" in command
        return subprocess.CompletedProcess(command, 0, PREFLIGHT_OUTPUT, "")

    result = check_device(
        "192.0.2.10",
        "root",
        8,
        connection_factory=connection_factory,
        runner=runner,
    )

    assert set(result) == set(empty_result("192.0.2.10", 8))
    assert result["tcp_22_open"] is True
    assert result["ssh_login_ok"] is True


def test_tcp_and_ssh_errors_do_not_crash():
    def closed(_address, timeout):
        raise OSError(f"closed after {timeout}")

    result = check_device(
        "192.0.2.20", "root", 3, connection_factory=closed
    )

    assert result["tcp_22_open"] is False
    assert result["ssh_login_ok"] is False
    assert result["errors"]
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
