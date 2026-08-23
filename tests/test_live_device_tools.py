from tools.live_device_readonly_check import parse_device_result
from tools.live_device_ssh_readonly_check import ssh_command


def test_readonly_parser_extracts_required_matrix_fields():
    result = parse_device_result(
        "192.0.2.10",
        {
            "/info": '<info deviceID="RADIO1"><name>Küche</name><type>SoundTouch 20</type><components><component><softwareVersion>27.0.13</softwareVersion></component></components></info>',
            "/volume": "<volume><targetvolume>5</targetvolume><actualvolume>4</actualvolume></volume>",
            "/now_playing": '<nowPlaying source="LOCAL_INTERNET_RADIO"><ContentItem><itemName>Radio</itemName></ContentItem><playStatus>PLAY_STATE</playStatus><track>Titel</track></nowPlaying>',
            "/presets": "<presets><preset/><preset/></presets>",
            "/getZone": '<zone master="RADIO1"><member ipaddress="192.0.2.11"/></zone>',
        },
        {"/sources": "timeout"},
    )

    assert result["reachable"] is True
    assert result["device_id"] == "RADIO1"
    assert result["firmware"] == "27.0.13"
    assert result["volume"] == 4
    assert result["source"] == "LOCAL_INTERNET_RADIO"
    assert result["presets_count"] == 2
    assert result["zone_status"]["active"] is True
    assert result["errors"] == {"/sources": "timeout"}


def test_ssh_tool_is_noninteractive_and_uses_only_supplied_fixed_command():
    command = ssh_command("192.0.2.10", "root", "uptime", 5)

    assert "BatchMode=yes" in command
    assert "HostKeyAlgorithms=+ssh-rsa" in command
    assert "PubkeyAcceptedAlgorithms=+ssh-rsa" in command
    assert "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1" in command
    assert "Ciphers=+aes256-cbc,aes128-cbc" in command
    assert "root@192.0.2.10" in command
    assert command[-1] == "uptime"
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
