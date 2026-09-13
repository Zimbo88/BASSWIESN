"""All targets, DNS, media probes and writes are mocks; no hardware contacts."""
import asyncio
import json
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import ConfigBackup, Device, RuntimeState
from basswiesn.app.services import live_radio_reconnect as recovery
from basswiesn.app.services.orion import StationDescriptor, station_contract_key


@pytest.fixture
def scenario(monkeypatch):
    clock = [10000.0]
    calls = []
    values = {"source": "INVALID_SOURCE", "zone": "<zone />", "identity": "RECONNECT-A",
              "volume": 17, "written": None, "broken": None, "post_error": False,
              "probe_valid": True, "change_during_probe": None}
    request = SimpleNamespace(client=SimpleNamespace(host="192.0.2.42"), headers={"host": "192.0.2.10:1329"})
    descriptor = StationDescriptor("Example Live", "https://stream.example.invalid/live.mp3",
                                   stream_url_resolved="https://old.example.invalid/expired.mp3")
    monkeypatch.setattr(recovery.time, "time", lambda: clock[0])
    monkeypatch.setattr(recovery, "DELAY_SECONDS", 0)
    monkeypatch.setattr(recovery, "validate_outbound_http_url", lambda *a, **k: SimpleNamespace(ok=True))

    async def no_sleep(_):
        pass
    monkeypatch.setattr(recovery.asyncio, "sleep", no_sleep)

    with app_db.SessionLocal() as db:
        db.add(Device(device_id="RECONNECT-A", ip_address="192.0.2.42", name="Example Radio", reachable=True))
        db.commit()

    def contract():
        with app_db.SessionLocal() as db:
            device = db.query(Device).one()
            recovery.observe_contract(db, request, device, descriptor)
            db.commit()

    def report(event, reason=None, position=64):
        with app_db.SessionLocal() as db:
            device = db.query(Device).one()
            generation = recovery.observe_report(db, request, device, station_contract_key(descriptor),
                                                  {"eventType": event, "reason": reason, "timeIntoTrack": position})
            db.commit()
            return generation

    def arm():
        with app_db.SessionLocal() as db:
            recovery.save_preference(db, "RECONNECT-A", True)
        contract()
        clock[0] += 60
        report("TIMED", position=60)
        clock[0] += 4
        return report("STOP", "FINISH")

    class Radio:
        def __init__(self, ip, **kwargs):
            assert ip == "192.0.2.42"

        async def get_xml(self, path):
            calls.append(("GET", path))
            if path == values["broken"]:
                raise TimeoutError()
            if path == "/info":
                return f'<info deviceID="{values["identity"]}"><type>SoundTouch 30</type></info>'
            assert calls[-2] == ("GET", "/info")
            if path == "/now_playing":
                if values["written"]:
                    return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus>' + values["written"] + '</nowPlaying>'
                return f'<nowPlaying source="{values["source"]}" />'
            return {"/volume": f'<volume><actualvolume>{values["volume"]}</actualvolume></volume>',
                    "/getZone": values["zone"], "/presets": "<presets />", "/sources": "<sources />"}[path]

        async def post_xml(self, path, body):
            assert path == "/select"
            assert calls[-1] == ("GET", "/info")
            with app_db.SessionLocal() as db:
                assert db.query(ConfigBackup).count() == 1
                backup = json.loads(db.query(ConfigBackup).one().content)
                assert len(backup["sha256"]) == 6
                assert recovery.state(db, "RECONNECT-A")[1]["phase"] == "ATTEMPTED"
            calls.append(("POST", path))
            if values["post_error"]:
                raise TimeoutError()
            values["written"] = body
            return "<status>OK</status>"

    async def probe(url):
        calls.append(("PROBE", "original"))
        assert url == descriptor.stream_url  # not the expired resolved URL
        if values["change_during_probe"]:
            values["change_during_probe"]()
        return {"status": "VALID" if values["probe_valid"] else "BROKEN", "reachable": values["probe_valid"],
                "resolved_url": "https://new.example.invalid/live.mp3"}

    coordinator = recovery.LiveRadioReconnect(app_db.SessionLocal, client_factory=Radio, probe=probe)
    return SimpleNamespace(clock=clock, calls=calls, values=values, request=request,
                           arm=arm, contract=contract, report=report, coordinator=coordinator)


def test_disabled_by_default_no_registration_or_task(scenario):
    scenario.contract()
    assert scenario.report("STOP", "FINISH") is None
    with app_db.SessionLocal() as db:
        assert not recovery.preference(db, "RECONNECT-A")["enabled"]
        assert recovery.state(db, "RECONNECT-A")[0] is None
    assert scenario.calls == []


def test_confirmed_finish_single_select_and_readback_preserves_volume(scenario):
    generation = scenario.arm()
    assert generation
    asyncio.run(scenario.coordinator.run("RECONNECT-A", generation))
    assert [c for c in scenario.calls if c[0] == "POST"] == [("POST", "/select")]
    assert ET.fromstring(scenario.values["written"]).get("sourceAccount") == ""
    with app_db.SessionLocal() as db:
        assert recovery.preference(db, "RECONNECT-A")["last_result"] == "PLAYBACK_VERIFIED"
        backup = json.loads(db.query(ConfigBackup).one().content)
        assert backup["readback_result"] == "PLAYBACK_VERIFIED"
        assert set(backup["after_sha256"]) == {"/now_playing", "/volume"}
        from hashlib import sha256
        assert all(sha256(xml.encode()).hexdigest() == backup["after_sha256"][path]
                   for path, xml in backup["after_http"].items())


def test_stale_worker_progress_cannot_overwrite_a_pending_claim(scenario):
    with app_db.SessionLocal() as db:
        recovery.save_preference(db, "RECONNECT-A", True)
    scenario.contract()
    scenario.clock[0] += 60
    scenario.report("TIMED", position=60)
    with app_db.SessionLocal() as stale_db:
        stale_row, stale_value = recovery.state(stale_db, "RECONNECT-A")
        scenario.clock[0] += 4
        generation = scenario.report("STOP", "FINISH")
        assert generation
        stale_value.update(position=64, progress_at=scenario.clock[0])
        assert not recovery._compare_store(stale_db, stale_row, stale_value)
        stale_db.commit()
    with app_db.SessionLocal() as db:
        value = recovery.state(db, "RECONNECT-A")[1]
        assert value["phase"] == "PENDING"
        assert value["generation"] == generation


@pytest.mark.parametrize("source", ["STANDBY", "AIRPLAY", "BLUETOOTH", "LOCAL_INTERNET_RADIO", ""])
def test_never_wake_standby_or_replace_another_state(scenario, source):
    generation = scenario.arm()
    scenario.values["source"] = source
    asyncio.run(scenario.coordinator.run("RECONNECT-A", generation))
    assert not any(c[0] == "POST" for c in scenario.calls)


@pytest.mark.parametrize("case", ["identity", "zone", "backup", "volume", "probe", "expired", "disabled", "new_session"])
def test_fail_closed_before_write(scenario, case):
    generation = scenario.arm()
    if case == "identity": scenario.values["identity"] = "OTHER"
    elif case == "zone": scenario.values["zone"] = '<zone master="OTHER" />'
    elif case == "backup": scenario.values["broken"] = "/presets"
    elif case == "volume": scenario.values["volume"] = -1
    elif case == "probe": scenario.values["probe_valid"] = False
    elif case == "expired": scenario.clock[0] += 91
    elif case == "disabled":
        with app_db.SessionLocal() as db:
            recovery.save_preference(db, "RECONNECT-A", False)
    elif case == "new_session": scenario.contract()
    asyncio.run(scenario.coordinator.run("RECONNECT-A", generation))
    assert not any(c[0] == "POST" for c in scenario.calls)
    if case == "identity": assert scenario.calls == [("GET", "/info")]


def test_user_stop_during_probe_wins(scenario):
    generation = scenario.arm()
    def stop():
        with app_db.SessionLocal() as db:
            recovery.cancel(db, "RECONNECT-A")
            db.commit()
    scenario.values["change_during_probe"] = stop
    asyncio.run(scenario.coordinator.run("RECONNECT-A", generation))
    assert not any(c[0] == "POST" for c in scenario.calls)


def test_timeout_after_post_is_never_retried(scenario):
    generation = scenario.arm()
    scenario.values["post_error"] = True
    asyncio.run(scenario.coordinator.run("RECONNECT-A", generation))
    asyncio.run(scenario.coordinator.run("RECONNECT-A", generation))
    assert [c for c in scenario.calls if c[0] == "POST"] == [("POST", "/select")]


def test_duplicate_finish_and_frozen_timed_reports_do_not_rearm(scenario):
    assert scenario.arm()
    assert scenario.report("STOP", "FINISH") is None
    assert scenario.report("TIMED") is None
    assert scenario.report("STOP", "FINISH") is None


def test_spoofed_device_header_is_not_native_peer(scenario):
    scenario.request.client.host = "192.0.2.99"
    assert scenario.arm() is None
    assert scenario.calls == []


def test_stale_replayed_finish_after_new_selection_rejected(scenario):
    scenario.arm()
    scenario.contract()
    scenario.clock[0] += 1
    scenario.report("TIMED", position=0)
    assert scenario.report("STOP", "FINISH", position=21604) is None


def test_no_recovery_after_explicit_report_stop(scenario):
    scenario.arm()
    scenario.contract()
    scenario.clock[0] += 60
    scenario.report("TIMED", position=60)
    scenario.report("STOP", "USER")
    assert scenario.report("STOP", "FINISH") is None


def test_cooldown_persists_across_new_provider_session(scenario):
    generation = scenario.arm()
    asyncio.run(scenario.coordinator.run("RECONNECT-A", generation))
    scenario.contract()
    scenario.clock[0] += 60
    scenario.report("TIMED", position=60)
    assert scenario.report("STOP", "FINISH") is None
    with app_db.SessionLocal() as db:
        assert recovery.preference(db, "RECONNECT-A")["last_result"] == "COOLDOWN"


@pytest.mark.parametrize("case", ["safe_mode", "protected_ip", "protected_id", "ip_moved"])
def test_policy_and_protection_deny_before_any_transport(scenario, monkeypatch, case):
    generation = scenario.arm()
    with app_db.SessionLocal() as db:
        device = db.query(Device).one()
        if case == "safe_mode": device.safe_mode = "always"
        elif case == "protected_ip": device.ip_address = "192.168.50.25"
        elif case == "protected_id":
            from basswiesn.app.services import protected_devices
            monkeypatch.setattr(protected_devices, "protected_device_ids", lambda: {"RECONNECT-A"})
        elif case == "ip_moved": device.ip_address = "192.0.2.43"
        db.commit()
    asyncio.run(scenario.coordinator.run("RECONNECT-A", generation))
    assert scenario.calls == []


def test_zone_join_during_probe_blocks_write(scenario):
    generation = scenario.arm()
    scenario.values["change_during_probe"] = lambda: scenario.values.update(zone='<zone master="OTHER" />')
    asyncio.run(scenario.coordinator.run("RECONNECT-A", generation))
    assert not any(c[0] == "POST" for c in scenario.calls)


def test_bounded_attempt_budget_persists(scenario):
    scenario.arm()
    scenario.contract()
    with app_db.SessionLocal() as db:
        row, value = recovery.state(db, "RECONNECT-A")
        value["attempts"] = [scenario.clock[0] - 1000, scenario.clock[0] - 800, scenario.clock[0] - 600]
        row.value = json.dumps(value)
        db.commit()
    scenario.clock[0] += 60
    scenario.report("TIMED", position=60)
    assert scenario.report("STOP", "FINISH") is None


def test_backup_commit_failure_prevents_select(scenario, monkeypatch):
    generation = scenario.arm()
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from sqlalchemy.exc import OperationalError
    def fail_backup(session, context, instances):
        if any(isinstance(item, ConfigBackup) for item in session.new):
            raise OperationalError("backup", {}, Exception("storage unavailable"))
    event.listen(Session, "before_flush", fail_backup)
    try:
        asyncio.run(scenario.coordinator.run("RECONNECT-A", generation))
    finally:
        event.remove(Session, "before_flush", fail_backup)
    assert not any(c[0] == "POST" for c in scenario.calls)


def test_opt_in_api_is_db_only_strict_and_never_starts_playback(scenario):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from basswiesn.app.routers.telemetry import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        url = "/api/devices/RECONNECT-A/live-reconnect"
        assert client.get(url).json()["enabled"] is False
        assert client.put(url, json={"enabled": "false"}).status_code == 422
        assert client.put(url, json={"enabled": True, "volume": 30}).status_code == 422
        assert client.put(url, json={"enabled": True}).json()["enabled"] is True
        assert client.get(url).json()["state"] == "IDLE"
        assert client.put(url, json={"enabled": False}).json()["enabled"] is False
    assert scenario.calls == []


def test_cloud_contract_and_finish_schedule_only_after_commit(scenario, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from basswiesn.app.routers import cloud
    from basswiesn.app.services.orion import encode_orion_data
    app = FastAPI()
    app.include_router(cloud.router)
    scheduled = []
    def schedule(device_id, generation):
        with app_db.SessionLocal() as db:
            assert recovery.state(db, device_id)[1]["phase"] == "PENDING"
        scheduled.append(generation)
    app.state.live_radio_reconnect = SimpleNamespace(schedule=schedule)
    with app_db.SessionLocal() as db:
        recovery.save_preference(db, "RECONNECT-A", True)
    descriptor = StationDescriptor("Example Live", "https://stream.example.invalid/live.mp3")
    with TestClient(app, client=("192.0.2.42", 1234), base_url="http://192.0.2.10:1329") as client:
        assert client.get("/core02/svc-bmx-adapter-orion/prod/orion/station", params={"data": encode_orion_data(descriptor)}).status_code == 200
        scenario.clock[0] += 60
        url = "/bmx/orion/reporting/station/" + station_contract_key(descriptor)
        assert client.post(url, json={"eventType": "TIMED", "timeIntoTrack": 60}).status_code == 200
        scenario.clock[0] += 4
        assert client.post(url, json={"eventType": "STOP", "reason": "FINISH", "timeIntoTrack": 64}).status_code == 200
        assert client.post(url, json={"eventType": "STOP", "reason": "FINISH", "timeIntoTrack": 64}).status_code == 200
    assert len(scheduled) == 1
    assert scenario.calls == []


def test_explicit_transport_key_invalidates_pending_job_before_send(scenario, monkeypatch):
    import httpx
    from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
    scenario.arm()
    def transport(request):
        with app_db.SessionLocal() as db:
            assert recovery.state(db, "RECONNECT-A")[1]["phase"] == "CANCELLED"
        return httpx.Response(200, text="<status>OK</status>")
    async def command():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            radio = SoundTouchClient("192.0.2.42", device_id="RECONNECT-A", http_client=http, trigger="remote")
            await radio.post_xml("/key", '<key state="press" sender="Gabbo">STOP</key>')
    asyncio.run(command())


pytestmark = [pytest.mark.integration, pytest.mark.release]
