"""Offline restart safety: fixture radios only, no network transports."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
import hashlib
import stat
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import Device, RuntimeState, Setting, SetupRebuildCoordinatorLease
from basswiesn.app.routers import radio_reboots as routes
from basswiesn.app.services import radio_reboots as reboot
from basswiesn.app.services.setup_rebuild.repository import SetupRepository

pytestmark = pytest.mark.integration


def snapshot(source="STANDBY", zone="<zone/>"):
    return {"http": {"/now_playing": f'<nowPlaying source="{source}"><playStatus>PLAY_STATE</playStatus></nowPlaying>',
                     "/getZone": zone}, "sha256": "fixture-hash"}


@pytest.fixture
def seeded():
    with app_db.SessionLocal() as db:
        db.add_all([Device(device_id=f"REBOOT-{n}", name=f"Fixture {n}", ip_address=f"192.0.2.{40+n}") for n in range(1, 5)])
        db.commit()
    return [f"REBOOT-{n}" for n in range(1, 5)]


def fake_factory(events, *, bad_backup=None, source="STANDBY", zone="<zone/>", failed_verify=None):
    class Radio:
        def __init__(self, target):
            self.target = target
        async def snapshot(self, root):
            events.append(("backup", self.target.device_id))
            if self.target.device_id == bad_backup:
                raise OSError("Required backup unavailable")
            return snapshot(source, zone)
        async def read(self, path):
            events.append(("read", self.target.device_id, path))
            return snapshot(source, zone)["http"][path]
        async def send(self):
            events.append(("send", self.target.device_id))
            await asyncio.sleep(0.01)
            events.append(("sent", self.target.device_id))
        async def verify(self, backup):
            events.append(("verify", self.target.device_id))
            verified = self.target.device_id != failed_verify
            return {"status": "VERIFIED" if verified else "STATE_DIFFERENCE", "verified": verified, "volume_commands": 0}
    return Radio


def test_all_backups_precede_concurrent_commands_and_setup_is_locked(seeded):
    async def scenario():
        events = []
        manager = reboot.RadioRebootManager(app_db.SessionLocal, radio_factory=fake_factory(events))
        job = manager.start(seeded)
        with app_db.SessionLocal() as db:
            assert not SetupRepository().acquire_lease(db, owner_id="unrelated-setup", job_id="unrelated")
        with pytest.raises(ValueError):
            manager.start(seeded)
        await asyncio.gather(*list(manager.tasks))
        assert manager.job(job["id"])["status"] == "VERIFIED"
        first_send = next(i for i, item in enumerate(events) if item[0] == "send")
        assert len([item for item in events[:first_send] if item[0] == "backup"]) == 4
        first_complete = next(i for i, item in enumerate(events) if item[0] == "sent")
        assert len([item for item in events[:first_complete] if item[0] == "send"]) == 4
        with app_db.SessionLocal() as db:
            assert reboot.read_value(db, reboot.ACTIVE_KEY) is None
            assert db.query(SetupRebuildCoordinatorLease).one().owner_id == ""
        await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["backup", "active", "zone"])
def test_failed_required_preflight_blocks_entire_batch(seeded, reason):
    async def scenario():
        events = []
        factory = fake_factory(events, bad_backup=seeded[-1] if reason == "backup" else None,
                               source="AIRPLAY" if reason == "active" else "STANDBY",
                               zone='<zone master="REBOOT-1"><member>REBOOT-2</member></zone>' if reason == "zone" else "<zone/>")
        manager = reboot.RadioRebootManager(app_db.SessionLocal, radio_factory=factory)
        job = manager.start(seeded)
        await asyncio.gather(*list(manager.tasks))
        assert manager.job(job["id"])["status"] == "BLOCKED_NO_REBOOT"
        assert not any(item[0] == "send" for item in events)
    asyncio.run(scenario())


def test_manual_interrupt_is_explicit_but_schedule_never_interrupts(seeded):
    async def scenario():
        events = []
        manager = reboot.RadioRebootManager(app_db.SessionLocal, radio_factory=fake_factory(events, source="LOCAL_INTERNET_RADIO"))
        manual = manager.start(seeded, allow_interrupt=True)
        await asyncio.gather(*list(manager.tasks))
        assert manager.job(manual["id"])["status"] == "VERIFIED"
        events.clear()
        scheduled = manager.start(seeded, allow_interrupt=True, scheduled_occurrence="fixture-day")
        await asyncio.gather(*list(manager.tasks))
        assert manager.job(scheduled["id"])["status"] == "BLOCKED_NO_REBOOT"
        assert not any(item[0] == "send" for item in events)
    asyncio.run(scenario())


def test_partial_verification_is_not_green(seeded):
    async def scenario():
        events = []
        manager = reboot.RadioRebootManager(app_db.SessionLocal, radio_factory=fake_factory(events, failed_verify=seeded[-1]))
        job = manager.start(seeded)
        await asyncio.gather(*list(manager.tasks))
        result = manager.job(job["id"])
        assert result["status"] == "PARTIAL_OR_UNCONFIRMED"
        assert sum(row["verified"] for row in result["results"]) == 3
    asyncio.run(scenario())


def test_dst_fold_runs_once_and_missing_spring_time_is_skipped():
    schedule = {**reboot.schedule_default(), "enabled": True, "time": "02:30"}
    first = datetime(2030, 10, 27, 0, 30, tzinfo=UTC)
    second = first + timedelta(hours=1)
    assert reboot.schedule_occurrence(schedule, first) == reboot.schedule_occurrence(schedule, second)
    assert reboot.schedule_occurrence(schedule, first + timedelta(minutes=1)) is None
    assert reboot.schedule_occurrence(schedule, datetime(2030, 3, 31, 1, 30, tzinfo=UTC)) is None
    assert reboot.schedule_occurrence({**schedule, "enabled": False}, first) is None


def test_scheduler_is_opt_in_and_durable_no_replay_on_restart(seeded):
    async def scenario():
        events = []
        factory = fake_factory(events)
        manager = reboot.RadioRebootManager(app_db.SessionLocal, radio_factory=factory)
        instant = datetime(2030, 7, 1, 2, 0, tzinfo=UTC)
        await manager.tick(instant)
        assert events == []
        with app_db.SessionLocal() as db:
            reboot.put_value(db, reboot.SCHEDULE_KEY, {**reboot.schedule_default(), "enabled": True, "device_ids": seeded}, Setting)
            db.commit()
        await manager.tick(instant)
        await asyncio.gather(*list(manager.tasks))
        assert len([item for item in events if item[0] == "send"]) == 4
        again = reboot.RadioRebootManager(app_db.SessionLocal, radio_factory=factory)
        await again.tick(instant)
        assert not again.tasks
        assert len([item for item in events if item[0] == "send"]) == 4
    asyncio.run(scenario())


def test_protected_target_is_rejected_before_factory_and_listing_filtered(seeded, monkeypatch):
    def guard(device, **kwargs):
        if device.device_id == seeded[-1]:
            raise HTTPException(403, "protected")
    monkeypatch.setattr(reboot, "require_unprotected_device", guard)
    monkeypatch.setattr(routes, "is_device_access_protected", lambda ip, did: did == seeded[-1])
    async def scenario():
        manager = reboot.RadioRebootManager(app_db.SessionLocal, radio_factory=lambda _: pytest.fail("Factory must not be called"))
        with pytest.raises(HTTPException):
            manager.start(seeded)
        assert not manager.tasks
    asyncio.run(scenario())
    app = FastAPI(); app.include_router(routes.router)
    with TestClient(app) as client:
        data = client.get("/api/radio-reboots").json()
        assert len(data["devices"]) == 3 and data["radio_requests"] == 0
        assert client.post("/api/radio-reboots/preview", json={"device_ids": seeded}).status_code == 403


def test_schedule_requires_explicit_confirmation_and_save_does_not_reboot(seeded):
    app = FastAPI(); app.include_router(routes.router)
    body = {**reboot.schedule_default(), "enabled": True, "device_ids": seeded}
    with TestClient(app) as client:
        assert client.put("/api/radio-reboots/schedule", json=body).status_code == 422
        body["confirmation"] = reboot.SCHEDULE_CONFIRMATION
        result = client.put("/api/radio-reboots/schedule", json=body)
        assert result.status_code == 200 and result.json()["radio_requests"] == 0
        assert client.get("/api/radio-reboots").json()["schedule"]["enabled"] is True
        for invalid in ({"skip_active": False}, {"weekdays": [True]}, {"time": "25:00"}, {"timezone": "../bad"}):
            assert client.put("/api/radio-reboots/schedule", json={**body, **invalid}).status_code == 422
        assert client.post("/api/radio-reboots/start", json={"device_ids": seeded, "confirmation": ""}).status_code == 409
        # A stale selection can always be disabled locally.
        assert client.put("/api/radio-reboots/schedule", json={**body, "enabled": False, "device_ids": ["REMOVED"]}).status_code == 200


def test_cancel_during_backup_never_sends_and_does_not_replay(seeded):
    async def scenario():
        entered = asyncio.Event()
        class Radio:
            def __init__(self, target): pass
            async def snapshot(self, root):
                entered.set()
                await asyncio.Event().wait()
            async def send(self): pytest.fail("No command on cancellation")
        manager = reboot.RadioRebootManager(app_db.SessionLocal, radio_factory=Radio)
        job = manager.start(seeded)
        await entered.wait()
        await manager.shutdown()
        assert manager.job(job["id"])["status"] == "INTERRUPTED_NO_REPLAY"
    asyncio.run(scenario())


def test_adapter_checks_every_identity_and_latches_mismatch(monkeypatch):
    async def scenario():
        calls = []
        raw_id = "REBOOT-A"
        class Client:
            async def get_xml(self, path):
                calls.append(path)
                return f'<info deviceID="{raw_id}"/>' if path == "/info" else "<volume/>"
        radio = reboot.RebootRadio(reboot.Target("REBOOT-A", "192.0.2.42", "Fixture"))
        radio.client = Client()
        await radio.read("/volume")
        await radio.read("/sources")
        assert calls == ["/info", "/volume", "/info", "/sources"]
        raw_id = "OTHER"
        with pytest.raises(PermissionError):
            await radio.read("/volume")
        first = list(calls)
        with pytest.raises(PermissionError):
            await radio.send()
        assert calls == first
    asyncio.run(scenario())


def test_adapter_backup_complete_hashed_private_and_fixed_reboot(monkeypatch, tmp_path):
    async def scenario():
        calls = []
        info = ('<info deviceID="REBOOT-A"><type>SoundTouch 30</type>'
                '<components><component><softwareVersion>27.0.6.46330.5043500</softwareVersion></component></components>'
                '<variant>mojo</variant><moduleType>SCM</moduleType></info>')
        class Client:
            async def get_xml(self, path):
                calls.append(path)
                if path == "/info": return info
                if path == "/sources": return '<sources><sourceItem source="AUX"/></sources>'
                if path == "/now_playing": return '<nowPlaying source="STANDBY"/>'
                if path == "/volume": return '<volume><actualvolume>1</actualvolume></volume>'
                return f'<{reboot.SNAPSHOT_ROOTS[path]}/>'
        async def config(ip, identifier):
            calls.append("CLI:read")
            assert calls[-2] == "/info"
            return SimpleNamespace(output="".join(f'<{key}>http://example.invalid/{key}</{key}>' for key in
                ("bmxRegistryUrl", "margeServerUrl", "swUpdateUrl", "statsServerUrl")))
        async def send(ip, identifier):
            calls.append("CLI:reboot")
            assert calls[-2] == "/info"
        monkeypatch.setattr(reboot, "read_current_config", config)
        monkeypatch.setattr(reboot, "reboot", send)
        radio = reboot.RebootRadio(reboot.Target("REBOOT-A", "192.0.2.42", "Fixture"))
        radio.client = Client()
        folder = tmp_path / "START"
        backup = await radio.snapshot(folder)
        assert set(backup["http"]) == set(reboot.HTTP_SNAPSHOT)
        for row in (folder / "SHA256SUMS").read_text().splitlines():
            digest, name = row.split("  ")
            assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest
            assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        await radio.send()
        assert calls.count("CLI:reboot") == 1
    asyncio.run(scenario())


def test_adapter_unknown_profile_blocks_cli_and_write(monkeypatch, tmp_path):
    async def scenario():
        radio = reboot.RebootRadio(reboot.Target("REBOOT-A", "192.0.2.42", "Fixture"))
        class Client:
            async def get_xml(self, path):
                return '<info deviceID="REBOOT-A"><type>Unknown model</type></info>'
        radio.client = Client()
        monkeypatch.setattr(reboot, "read_current_config", lambda *a: pytest.fail("No CLI without exact profile"))
        with pytest.raises(PermissionError):
            await radio.snapshot(tmp_path / "START")
        assert not (tmp_path / "START").exists()
    asyncio.run(scenario())


def test_adapter_return_timeout_never_claims_verification(monkeypatch):
    async def scenario():
        clock = [0.0]
        async def sleep(seconds): clock[0] += seconds
        monkeypatch.setattr(reboot.asyncio, "sleep", sleep)
        monkeypatch.setattr(reboot.time, "monotonic", lambda: clock[0])
        radio = reboot.RebootRadio(reboot.Target("REBOOT-A", "192.0.2.42", "Fixture"))
        async def offline(): raise ConnectionRefusedError()
        radio.identity = offline
        result = await radio.verify({}, timeout=10)
        assert result == {"status": "VERIFICATION_FAILED", "verified": False, "reason": "ENDPOINT_DID_NOT_RETURN"}
    asyncio.run(scenario())
