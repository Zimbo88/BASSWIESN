from uuid import uuid4

from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device
from basswiesn.app.routers import media, setup
from basswiesn.app.services.battery import battery_bucket, battery_state, parse_battery_cli, parse_power_management, portable_battery_diagnosis


CLI_SAMPLE = """->DC present; true
->Battery voltage: 12.365 V
->Battery temperature: 29.8 deg. C; 302.9 deg. K
->Battery current: +0.000 A
->Battery status is  : 0x40e0 -  Discharging; Fully Charged; Error Code: OK
->Battery build date: raw=20522, 01/10/2020
->Battery serial number: 165
->Battery version: H
->Battery relative state of charge: 100% (calc'ed: 99.72 %)
->Battery Fault Code: 0x0000
->Battery charger: on
->Manufacturer name: BOSE_A
"""


def test_parse_power_management_high_level_mirror():
    parsed = parse_power_management("<powerManagementResponse><powerState>FullPower</powerState><battery><capable>true</capable><present>true</present><runningOnBattery>false</runningOnBattery><percentCharge>94</percentCharge></battery></powerManagementResponse>")
    assert parsed["power_state"] == "FullPower"
    assert parsed["percent_charge"] == 94
    assert parsed["level_bucket"] == 100


def test_parse_cli_battery_hardware_fields():
    parsed = parse_battery_cli(CLI_SAMPLE)
    assert parsed["dc_present"] is True
    assert parsed["voltage_v"] == 12.365
    assert parsed["temperature_c"] == 29.8
    assert parsed["current_a"] == 0.0
    assert parsed["relative_state_of_charge"] == 100
    assert parsed["fault_code"] == "0x0000"
    assert parsed["manufacturer"] == "BOSE_A"


def test_ba8_wins_over_http_and_ship_command_is_excluded():
    state = battery_state("<powerManagementResponse><battery><percentCharge>92</percentCharge></battery></powerManagementResponse>", CLI_SAMPLE)
    assert state["percent_charge"] == 100
    assert state["source_of_truth"] == "cli17000.ba8"
    assert state["percent_sources_match"] is False
    assert state["safety"]["excluded_command"] == "ba s"


def test_battery_buckets_match_firmware_image_ranges():
    assert [battery_bucket(value) for value in (0, 20, 21, 40, 41, 60, 61, 75, 76, 100)] == [20, 20, 40, 40, 60, 60, 75, 75, 100, 100]


def test_portable_battery_diagnosis_recommends_fix_for_unknown_profile():
    diagnosis = portable_battery_diagnosis("SoundTouch Portable", "<powerManagementResponse><battery><present>true</present><percentCharge>64</percentCharge></battery></powerManagementResponse>", "Manufacturer name: THIRD_PARTY\nBattery relative state of charge: 64%", media.BATTERY_MONITOR_PATCH["expected_original_sha256"], "53 41 4e 59 4f", "")
    assert diagnosis["supported_portable"] is True
    assert diagnosis["battery_detected"] is True
    assert diagnosis["battery_profile_known"] is False
    assert diagnosis["fix_recommended"] is True


def test_portable_battery_diagnosis_known_profile_needs_no_fix():
    diagnosis = portable_battery_diagnosis("SoundTouch Portable", "<powerManagementResponse><battery><present>true</present><percentCharge>94</percentCharge></battery></powerManagementResponse>", CLI_SAMPLE, media.BATTERY_MONITOR_PATCH["expected_original_sha256"], "53 41 4e 59 4f", "")
    assert diagnosis["battery_profile_known"] is True
    assert diagnosis["fix_recommended"] is False


def _add_portable(device_id: str):
    db = app_db.SessionLocal()
    db.add(Device(device_id=device_id, name="Portable", ip_address="192.0.2.80", model="SoundTouch Portable"))
    db.commit()
    db.close()


def _probe_output(sha: str, bytes_hex: str = "53 41 4e 59 4f", backup_sha: str = "") -> str:
    backup = f"{backup_sha} /mnt/nv/BatteryMonitor.basswiesn-backup" if backup_sha else ""
    return f"__SHA__\n{sha} /opt/Bose/BatteryMonitor\n__BYTES__\n{bytes_hex}\n__BACKUP__\n{backup}\n"


def test_battery_status_detects_supported_portable_and_patched(monkeypatch):
    device_id = f"BAT{uuid4().hex[:8]}"
    _add_portable(device_id)
    monkeypatch.setattr(media, "_run_battery_ssh", lambda *args, **kwargs: _probe_output(media.BATTERY_MONITOR_PATCH["expected_patched_sha256"], "41 00 00 00 00"))

    class Client:
        def __init__(self, ip):
            self.ip = ip

        async def get_xml(self, endpoint):
            return "<powerManagementResponse><battery><present>true</present><percentCharge>90</percentCharge></battery></powerManagementResponse>"

    monkeypatch.setattr(media, "SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        result = client.get(f"/api/battery/status/{device_id}").json()
    assert result["supported_portable"] is True
    assert result["patch_status"] == "patched"


def test_battery_patch_dry_run_writes_nothing(monkeypatch):
    device_id = f"BAT{uuid4().hex[:8]}"
    _add_portable(device_id)
    calls = []
    monkeypatch.setattr(media, "_run_battery_ssh", lambda *args, **kwargs: calls.append(args[1]) or _probe_output(media.BATTERY_MONITOR_PATCH["expected_original_sha256"]))

    class Client:
        def __init__(self, ip):
            self.ip = ip

        async def get_xml(self, endpoint):
            return "<powerManagementResponse><battery><present>true</present><percentCharge>50</percentCharge></battery></powerManagementResponse>"

    monkeypatch.setattr(media, "SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        result = client.post(f"/api/battery/patch/{device_id}/dry-run").json()
    assert result["dry_run"] is True
    assert result["will_write"] is False
    assert all("dd of=" not in command for command in calls)


def test_battery_patch_apply_blocks_without_yes_and_unsupported_checksum(monkeypatch):
    device_id = f"BAT{uuid4().hex[:8]}"
    _add_portable(device_id)
    monkeypatch.setattr(media, "_run_battery_ssh", lambda *args, **kwargs: _probe_output("0" * 64))

    class Client:
        def __init__(self, ip):
            self.ip = ip

        async def get_xml(self, endpoint):
            return "<powerManagementResponse><battery><present>true</present><percentCharge>50</percentCharge></battery></powerManagementResponse>"

    monkeypatch.setattr(media, "SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        assert client.post(f"/api/battery/patch/{device_id}/apply", json={"confirmation": "no"}).status_code == 409
        unsupported = client.post(f"/api/battery/patch/{device_id}/apply", json={"confirmation": media.BATTERY_PATCH_CONFIRMATION, "memory_checked": True})
    assert unsupported.status_code == 409
    assert unsupported.json()["detail"]["error"] == "unsupported checksum"


def test_battery_patch_apply_creates_backup_before_write(monkeypatch):
    device_id = f"BAT{uuid4().hex[:8]}"
    _add_portable(device_id)
    commands = []

    def fake_ssh(device, command, **kwargs):
        commands.append(command)
        if "dd of=" in command:
            assert command.index("cp /opt/Bose/BatteryMonitor /mnt/nv/BatteryMonitor.basswiesn-backup") < command.index("dd of=/opt/Bose/BatteryMonitor")
            return _probe_output(media.BATTERY_MONITOR_PATCH["expected_patched_sha256"], "41 00 00 00 00", media.BATTERY_MONITOR_PATCH["expected_original_sha256"])
        if len(commands) >= 2:
            return _probe_output(media.BATTERY_MONITOR_PATCH["expected_patched_sha256"], "41 00 00 00 00", media.BATTERY_MONITOR_PATCH["expected_original_sha256"])
        return _probe_output(media.BATTERY_MONITOR_PATCH["expected_original_sha256"], "53 41 4e 59 4f")

    monkeypatch.setattr(media, "_run_battery_ssh", fake_ssh)

    class Client:
        def __init__(self, ip):
            self.ip = ip

        async def get_xml(self, endpoint):
            return "<powerManagementResponse><battery><present>true</present><percentCharge>50</percentCharge></battery></powerManagementResponse>"

    monkeypatch.setattr(media, "SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        result = client.post(f"/api/battery/patch/{device_id}/apply", json={"confirmation": media.BATTERY_PATCH_CONFIRMATION, "memory_checked": True})
    assert result.status_code == 200
    assert result.json()["changed"] is True


def test_battery_rollback_blocks_without_backup_or_yes(monkeypatch):
    device_id = f"BAT{uuid4().hex[:8]}"
    _add_portable(device_id)
    monkeypatch.setattr(media, "_run_battery_ssh", lambda *args, **kwargs: _probe_output(media.BATTERY_MONITOR_PATCH["expected_patched_sha256"], "41 00 00 00 00", ""))

    with TestClient(create_web_app()) as client:
        assert client.post(f"/api/battery/patch/{device_id}/rollback", json={"confirmation": "no"}).status_code == 409
        missing = client.post(f"/api/battery/patch/{device_id}/rollback", json={"confirmation": media.BATTERY_ROLLBACK_CONFIRMATION, "memory_checked": True})
    assert missing.status_code == 409
    assert "backup" in str(missing.json()).lower()


def test_normal_setup_flow_never_uses_battery_patch():
    assert all("battery" not in step for step in setup.SETUP_JOB_STEPS)
    assert "battery_patch" not in open("basswiesn/app/routers/setup.py", encoding="utf-8").read()
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
