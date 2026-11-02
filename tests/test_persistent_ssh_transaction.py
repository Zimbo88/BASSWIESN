"""Software-only characterization of the explicit legacy recovery action."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import ConfigBackup, Device, DeviceActionJournal
from basswiesn.app.routers import api
from basswiesn.app.services import ssh_persistence as persistence
from basswiesn.app.services.device_policy import device_lock

pytestmark = pytest.mark.integration
DEVICE_ID = "AA11BB22CC33"
BUILD = "27.0.6.46330.5043500"


def info(identity=DEVICE_ID):
    return (f'<info deviceID="{identity}"><name>Example</name><type>SoundTouch 30</type>'
            '<variant>mojo</variant><moduleType>SCM</moduleType><productID>0x0924</productID>'
            f'<components><component><softwareVersion>{BUILD}</softwareVersion></component></components></info>')


@pytest.fixture
def rig(monkeypatch):
    class Radio:
        def __init__(self):
            self.identity = DEVICE_ID
            self.ssh_calls = 0
            self.info_calls = 0
            self.reads = 0
            self.writes = 0
            self.state = persistence.MarkerState(False, False, False)
            self.fail_probe = False
            self.fail_readback = False
            self.ignore_write = False
            self.lost_reply = False
            self.cancel_write = False
            self.switch_identity = False
            self.during_recheck = lambda: None

        async def get_xml(self, path):
            assert path == "/info"
            self.info_calls += 1
            return info("BB22CC33DD44" if self.writes and self.switch_identity else self.identity)

        async def ssh(self, address, username, command, timeout):
            assert address == "192.0.2.42"
            assert device_lock(DEVICE_ID).locked()
            self.ssh_calls += 1
            if command == persistence.READ_COMMAND:
                self.reads += 1
                if self.reads == 2:
                    self.during_recheck()
                if self.fail_probe or (self.writes and self.fail_readback):
                    return {"returncode": 7, "stdout": "", "stderr": "simulated denied read"}
                return reply(self.state)
            assert command == persistence.WRITE_COMMAND
            with app_db.SessionLocal() as db:
                assert db.query(ConfigBackup).count() == 1
                assert db.query(DeviceActionJournal).filter_by(phase="ssh_persistence_intent").count() == 1
            self.writes += 1
            if not self.ignore_write:
                self.state = persistence.MarkerState(True, True, True)
            if self.cancel_write:
                raise asyncio.CancelledError()
            if self.lost_reply:
                raise TimeoutError("simulated lost write reply, never echo credentials")
            return {"returncode": 0, "stdout": "", "stderr": ""}

    radio = Radio()
    with app_db.SessionLocal() as db:
        db.add(Device(device_id=DEVICE_ID, ip_address="192.0.2.42", name="Example",
                      model="SoundTouch 30", firmware=BUILD, info_xml=info()))
        db.commit()
    monkeypatch.setattr(api, "SoundTouchClient", lambda *a, **kw: radio)
    monkeypatch.setattr(api, "_run_ssh_readonly_command", radio.ssh)

    async def logs(*args, **kwargs):
        return {}

    monkeypatch.setattr(api, "_capture_radio_logs", logs)
    with TestClient(create_web_app(background_tasks=False), raise_server_exceptions=False) as client:
        yield radio, client


def reply(state):
    return {"returncode": 0, "stdout": (
        f"PERSISTENT={int(state.persistent)}\nTEMPORARY={int(state.temporary)}\n"
        f"ENABLED={int(state.enabled)}\nPROBE_COMPLETE\n"), "stderr": ""}


def write(client, **extra):
    return client.post(f"/api/devices/{DEVICE_ID}/recovery/persistent_ssh",
                       json={"dry_run": False, "confirmation": "YES", **extra})


def test_changed_identity_aborts_before_any_ssh_command(rig):
    radio, client = rig
    radio.identity = "BB22CC33DD44"
    response = write(client)
    assert response.status_code == 409
    assert radio.ssh_calls == 0


def test_failed_ssh_is_not_reported_as_marker_written(rig):
    radio, client = rig
    radio.fail_probe = True
    response = write(client)
    assert response.status_code == 409
    assert "Wrote /mnt/nv" not in response.text
    assert radio.writes == 0


def test_verified_marker_transaction_has_durable_backup_and_separate_readback(rig):
    radio, client = rig
    response = write(client)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "VERIFIED" and result["ok"] is True
    assert result["persistent_ssh_boot_verified"] is False
    assert result["automatic_retry"] is result["automatic_rollback"] is False
    assert radio.writes == 1 and radio.reads == 3 and radio.info_calls == 3
    with app_db.SessionLocal() as db:
        backup = db.query(ConfigBackup).one()
        assert json.loads(backup.content)["marker_state"] == {"persistent": False, "temporary": False, "enabled": False}
        assert db.query(DeviceActionJournal).filter_by(phase="ssh_persistence_verified", verified=True).count() == 1


def test_existing_state_is_observed_without_touch_or_daemon_restart(rig):
    radio, client = rig
    radio.state = persistence.MarkerState(True, True, True)
    response = write(client)
    assert response.status_code == 200 and response.json()["status"] == "UNCHANGED"
    assert radio.writes == 0 and radio.reads == 1


@pytest.mark.parametrize("field", ["ignore_write", "fail_readback", "switch_identity"])
def test_unconfirmed_result_is_diverged_never_replayed(rig, field):
    radio, client = rig
    setattr(radio, field, True)
    response = write(client)
    assert response.status_code == 409, response.text
    result = response.json()["detail"]
    assert result["status"] == "DIVERGED" and result["reconciliation_required"] is True
    assert radio.writes == 1
    with app_db.SessionLocal() as db:
        assert db.query(DeviceActionJournal).filter_by(verified=True).count() == 0


def test_write_reply_loss_is_resolved_only_by_actual_readback(rig):
    radio, client = rig
    radio.lost_reply = True
    response = write(client)
    assert response.status_code == 200
    assert response.json()["transport_uncertain"] is True
    assert radio.writes == 1 and radio.reads == 3


def test_cancelled_write_retains_intent_and_releases_device_ownership(rig):
    radio, _ = rig
    radio.cancel_write = True
    with app_db.SessionLocal() as db:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(persistence.enable_persistence(
                db, db.query(Device).one(), username="root", client_factory=api.SoundTouchClient,
                ssh_runner=radio.ssh, guard=api.enforce_ip_write_guard,
            ))
    assert radio.writes == 1
    assert not device_lock(DEVICE_ID).locked()
    with app_db.SessionLocal() as db:
        assert db.query(DeviceActionJournal).filter_by(phase="ssh_persistence_intent").count() == 1
        assert db.query(DeviceActionJournal).filter_by(phase="ssh_persistence_diverged").count() == 1
        assert db.query(DeviceActionJournal).filter_by(verified=True).count() == 0


def test_dry_run_constructs_neither_transport(rig):
    radio, client = rig
    response = write(client, dry_run=True)
    assert response.status_code == 200 and response.json()["dry_run"] is True
    assert radio.info_calls == radio.ssh_calls == 0


@pytest.mark.parametrize("value", [0, "false", None])
def test_write_intent_is_not_inferred_from_non_boolean_dry_run(rig, value):
    radio, client = rig
    assert write(client, dry_run=value).status_code == 422
    assert radio.info_calls == radio.ssh_calls == 0


def test_unknown_or_boolean_exit_status_is_not_success():
    for code in (True, None, "0", 7):
        with pytest.raises(ValueError):
            persistence.parse_state({**reply(persistence.MarkerState(True, True, True)), "returncode": code})


@pytest.mark.parametrize("username", ["-V", "user name", "../user", ""])
def test_service_rejects_unsafe_username_without_relying_on_router(rig, username):
    radio, _ = rig
    with app_db.SessionLocal() as db:
        with pytest.raises(persistence.SshPersistenceError) as error:
            asyncio.run(persistence.enable_persistence(
                db, db.query(Device).one(), username=username, client_factory=api.SoundTouchClient,
                ssh_runner=radio.ssh, guard=api.enforce_ip_write_guard,
            ))
    assert error.value.result["status"] == "BLOCKED"
    assert radio.info_calls == radio.ssh_calls == 0


@pytest.mark.parametrize("previous", [{}, {"persistent": True, "temporary": False, "enabled": True},
    {"persistent": 0, "temporary": False, "enabled": False}])
def test_expected_previous_is_exact_not_truthy_or_partial(rig, previous):
    radio, client = rig
    response = write(client, expected_previous=previous)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "WRITE_PRECONDITION_FAILED"
    assert radio.writes == 0


@pytest.mark.parametrize("mutation", ["backup", "address", "state"])
def test_changed_preconditions_stop_before_write(rig, mutation):
    radio, client = rig

    def change():
        if mutation == "state":
            radio.state = persistence.MarkerState(True, False, True)
            return
        with app_db.SessionLocal() as db:
            if mutation == "backup":
                db.query(ConfigBackup).one().content = "altered"
            else:
                db.query(Device).one().ip_address = "192.0.2.43"
            db.commit()

    radio.during_recheck = change
    response = write(client)
    assert response.status_code == 409, response.text
    assert radio.writes == 0


@pytest.mark.parametrize("phase", ["ssh_persistence_intent", "ssh_persistence_verified"])
def test_database_commit_failure_never_reports_success(rig, monkeypatch, phase):
    from sqlalchemy.orm import Session

    radio, client = rig
    real_commit = Session.commit

    def commit(db):
        if db.query(DeviceActionJournal).filter_by(phase=phase).count():
            raise RuntimeError("simulated database failure")
        return real_commit(db)

    monkeypatch.setattr(Session, "commit", commit)
    response = write(client)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["status"] == ("BLOCKED" if phase.endswith("intent") else "DIVERGED")
    assert radio.writes == (0 if phase.endswith("intent") else 1)


def test_unknown_hardware_and_bad_confirmation_never_reach_ssh(rig):
    radio, client = rig
    assert write(client, confirmation="no").status_code == 400
    with app_db.SessionLocal() as db:
        db.query(Device).one().firmware = "27.0.7.unknown"
        db.commit()
    assert write(client).status_code == 409
    assert radio.ssh_calls == 0


def test_ip_guard_blocks_before_either_transport(rig):
    from basswiesn.app.models import Setting

    radio, client = rig
    with app_db.SessionLocal() as db:
        db.add(Setting(key="ip_write_guard", value="true"))
        db.commit()
    assert write(client).status_code == 403
    assert radio.info_calls == radio.ssh_calls == 0


@pytest.mark.parametrize("stdout", ["", "PERSISTENT=1\n", "PERSISTENT=1\nTEMPORARY=1\nENABLED=0\nPROBE_COMPLETE\n",
    "PERSISTENT=1\nPERSISTENT=1\nENABLED=1\nPROBE_COMPLETE\n", "x" * 1025])
def test_incomplete_or_contradictory_readback_is_not_state(stdout):
    with pytest.raises(ValueError):
        persistence.parse_state({"returncode": 0, "stdout": stdout})


def test_fixed_commands_never_rewrite_startup_or_configuration_files():
    for command in (persistence.READ_COMMAND, persistence.WRITE_COMMAND):
        for forbidden in ("rc.local", "/etc/hosts", "reboot", "rm ", "cp ", "sshd start"):
            assert forbidden not in command
    assert "if ! test -e /mnt/nv/remote_services" in persistence.WRITE_COMMAND
    assert "touch " not in persistence.WRITE_COMMAND
