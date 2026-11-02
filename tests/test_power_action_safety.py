"""Power transaction contracts: synthetic state, never hardware approval."""
import asyncio
import json

from starlette.responses import JSONResponse

from fastapi import HTTPException
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import ConfigBackup, Device, DeviceActionJournal
from basswiesn.app.routers import api
from basswiesn.app.services.device_policy import device_lock
from test_maintenance_reboot_safety import BUILD, ID, info, now, volume

pytestmark = pytest.mark.integration


@pytest.fixture
def power(monkeypatch):
    class Radio:
        def __init__(self):
            self.raw = {"/info": info(), "/now_playing": now("LOCAL_INTERNET_RADIO", "PLAY_STATE"),
                        "/volume": volume(1), "/getZone": "<zone/>", "/presets": "<presets/>"}
            self.constructed = 0
            self.writes, self.durable, self.owned = [], [], []
            self.apply = True
            self.lost_reply = False

        def factory(self, *args, **kwargs):
            self.constructed += 1
            return self

        async def get_xml(self, path):
            if path in {"/standby", "/lowPowerStandby"}:
                self.writes.append(path)
                self.owned.append(device_lock(ID).owned_by_current_task())
                with app_db.SessionLocal() as db:
                    self.durable.append(db.query(ConfigBackup).count() > 0 and db.query(DeviceActionJournal).count() > 0)
                if self.apply:
                    self.raw["/now_playing"] = now("STANDBY", "STOP_STATE")
                if self.lost_reply:
                    raise TimeoutError("synthetic lost power response")
                return "<status>OK</status>"
            return self.raw[path]

    radio = Radio()
    monkeypatch.setattr(api, "SoundTouchClient", radio.factory)
    with app_db.SessionLocal() as db:
        radio.db = db
        radio.device = Device(device_id=ID, ip_address="192.0.2.42", model="SoundTouch 30", firmware=BUILD, info_xml=info())
        db.add(radio.device)
        db.commit()
        yield radio


def run(power, action="standby", **payload):
    try:
        result = asyncio.run(api.device_power_action(ID, action, {"dry_run": False, "confirmation": "YES", **payload}, power.db))
        return json.loads(result.body) if isinstance(result, JSONResponse) else result
    except HTTPException as exc:
        return {"rejected": True, "detail": exc.detail}
    except Exception as exc:
        return {"unhandled_exception": type(exc).__name__}


def test_power_has_owned_durable_start_and_verified_result(power):
    result = run(power)
    assert power.writes == ["/standby"]
    assert all(power.durable) and all(power.owned)
    assert result.get("verified") is True


@pytest.mark.parametrize("change", ["identity", "profile", "zone"])
def test_power_preconditions_prevent_dispatch(power, change):
    if change == "identity":
        power.raw["/info"] = info("BB22CC33DD44")
    elif change == "profile":
        power.raw["/info"] = info(firmware="27.99.0")
    else:
        power.raw["/getZone"] = '<zone master="OTHER"><member>OTHER</member></zone>'
    result = run(power)
    assert not power.writes
    assert result.get("rejected") is True or result.get("verified") is False


def test_power_ack_does_not_certify_unchanged_playback(power):
    power.apply = False
    result = run(power)
    assert result.get("rejected") is True or result.get("verified") is False
    assert power.writes == ["/standby"]


def test_lost_power_reply_is_bounded_uncertain_not_unhandled_or_replayed(power):
    power.lost_reply = True
    result = run(power)
    assert "unhandled_exception" not in result
    assert result.get("rejected") is True or result.get("verified") is False
    assert power.writes == ["/standby"]


def test_power_backup_failure_prevents_dispatch(power, monkeypatch):
    def fail():
        raise RuntimeError("synthetic backup commit failed")
    monkeypatch.setattr(power.db, "commit", fail)
    run(power)
    assert not power.writes


def test_protected_target_is_blocked_before_transport_construction(power):
    power.device.ip_address = "192.168.50.25"  # Synthetic protected fixture from conftest.
    power.db.commit()
    run(power)
    assert power.constructed == 0
    assert not power.writes


def test_preview_does_not_construct_transport(power):
    assert run(power, dry_run=True)["dry_run"] is True
    assert power.constructed == 0


def test_power_confirmation_is_required(power):
    assert run(power, confirmation="NO").get("rejected") is True
    assert power.constructed == 0


@pytest.mark.parametrize("dry_run", [0, 1, None, "false", [], {}])
def test_power_rejects_ambiguous_preview_flags(power, dry_run):
    assert run(power, dry_run=dry_run)["rejected"] is True
    assert power.constructed == 0


@pytest.mark.parametrize("lost", [False, True])
def test_low_power_never_certifies_sleep_from_ack_or_disappearance(power, lost):
    power.lost_reply = lost
    result = run(power, "low_power_standby")
    assert result["status"] == "PENDING_READBACK"
    assert result["verified"] is False and result["reconciliation_required"] is True
    assert result["hardware_wake_may_be_required"] is True
    assert power.writes == ["/lowPowerStandby"]
    assert not power.db.query(DeviceActionJournal).filter_by(verified=True).count()


@pytest.mark.parametrize("field,value", [
    ("/volume", volume(2)), ("/volume", '<volume><actualvolume>1</actualvolume><muteenabled>true</muteenabled></volume>'),
    ("/presets", '<presets><preset id="1"/></presets>'),
    ("/getZone", '<zone master="OTHER"><member>OTHER</member></zone>'),
    ("/info", info("BB22CC33DD44")),
    ("/now_playing", now("STANDBY", "PLAY_STATE")),
])
def test_standby_does_not_hide_unrequested_changes(power, monkeypatch, field, value):
    original = power.get_xml
    async def changed(path):
        result = await original(path)
        if path == "/standby":
            power.raw[field] = value
        return result
    monkeypatch.setattr(power, "get_xml", changed)
    result = run(power)
    assert result["rejected"] is True
    assert power.writes == ["/standby"]
    assert not power.db.query(DeviceActionJournal).filter_by(verified=True).count()


@pytest.mark.parametrize("kind", ["backup", "state", "mapping", "profile"])
def test_change_during_durable_intent_blocks_power_write(power, monkeypatch, kind):
    original = power.db.commit
    calls = 0
    def changed():
        nonlocal calls
        calls += 1
        original()
        if calls == 2:
            if kind == "backup":
                power.db.query(ConfigBackup).one().content += " tampered"
                original()
            elif kind == "state":
                power.raw["/volume"] = volume(2)
            elif kind == "mapping":
                power.device.ip_address = "192.0.2.43"
                original()
            else:
                power.raw["/info"] = info(firmware="27.99.0")
    monkeypatch.setattr(power.db, "commit", changed)
    assert run(power)["rejected"] is True
    assert not power.writes


def test_result_commit_failure_never_publishes_verified_power(power, monkeypatch):
    original = power.db.commit
    calls = 0
    def fail_final():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("synthetic final database failure")
        original()
    monkeypatch.setattr(power.db, "commit", fail_final)
    result = run(power)
    assert result["detail"]["code"] == "POWER_RESULT_COMMIT_FAILED"
    assert power.writes == ["/standby"]
    assert power.db.query(ConfigBackup).count() == 1
    assert not power.db.query(DeviceActionJournal).filter_by(verified=True).count()


def test_power_cancels_queued_reconnect_before_dispatch(power, monkeypatch):
    from basswiesn.app.models import RuntimeState
    power.db.add(RuntimeState(key=f"live_radio_reconnect:{ID}", value=json.dumps({"phase": "PENDING", "generation": "old"})))
    power.db.commit()
    original = power.get_xml
    async def inspect(path):
        if path == "/standby":
            with app_db.SessionLocal() as db:
                value = json.loads(db.query(RuntimeState).filter_by(key=f"live_radio_reconnect:{ID}").one().value)
                assert value["phase"] == "CANCELLED" and value["generation"] != "old"
        return await original(path)
    monkeypatch.setattr(power, "get_xml", inspect)
    assert run(power)["verified"] is True


def test_power_cancellation_releases_ownership_without_retry(power, monkeypatch):
    original = power.get_xml
    async def cancelled(path):
        if path == "/standby":
            power.writes.append(path)
            raise asyncio.CancelledError()
        return await original(path)
    monkeypatch.setattr(power, "get_xml", cancelled)
    with pytest.raises(asyncio.CancelledError):
        run(power)
    assert power.writes == ["/standby"]
    assert not device_lock(ID).owned_by_current_task()
    assert power.db.query(ConfigBackup).count() == 1


def test_power_readback_timeout_is_not_a_second_command(power, monkeypatch):
    original = power.get_xml
    async def unavailable(path):
        if power.writes and path == "/info":
            raise TimeoutError("offline")
        return await original(path)
    monkeypatch.setattr(power, "get_xml", unavailable)
    assert run(power)["detail"]["code"] == "POWER_READBACK_MISMATCH"
    assert power.writes == ["/standby"]
