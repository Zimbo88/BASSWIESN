import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, Setting, Station
from basswiesn.app.routers import setup
from basswiesn.app.config import Settings


def _add_devices():
    db = app_db.SessionLocal()
    db.add_all([
        Device(device_id="BATCH1", name="Batch One", ip_address="192.0.2.11", model="SoundTouch 10"),
        Device(device_id="BATCH2", name="Batch Two", ip_address="192.0.2.12", model="SoundTouch 20"),
    ])
    db.commit()
    db.close()


def _wait_job(client: TestClient, job_id: str) -> dict:
    deadline = time.time() + 5
    last = {}
    while time.time() < deadline:
        last = client.get(f"/api/setup/jobs/{job_id}").json()
        if not last["running"]:
            return last
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {last}")


def test_retired_setup_device_snapshot_never_probes_ssh_or_cli(monkeypatch):
    _add_devices()

    def forbidden_port_probe(*args, **kwargs):
        raise AssertionError("passive setup snapshot opened a network probe")

    monkeypatch.setattr(setup.api_core, "_tcp_port_open", forbidden_port_probe)
    with TestClient(create_web_app()) as client:
        rows = client.get("/api/setup/devices").json()

    by_id = {row["device_id"]: row for row in rows}
    assert by_id["BATCH1"]["network_probe"] is False
    assert by_id["BATCH1"]["ssh_status"] == "not_probed"
    assert by_id["BATCH2"]["port_17000_status"] == "not_probed"
    assert by_id["BATCH2"]["ready_status"] == "not_probed"


def test_explicit_setup_host_overrides_stale_configured_lan_host(monkeypatch):
    monkeypatch.setattr(setup, "get_settings", lambda: Settings(lan_host="192.168.50.50", lan_host_configured=True))

    class Request:
        class URL:
            hostname = "127.0.0.1"
        url = URL()

    assert setup._setup_target_host({"host": "192.168.50.77"}, Request()) == "192.168.50.77"


def test_private_server_host_is_allowed_even_when_it_was_a_previous_live_target():
    class Request:
        url = type("URL", (), {"hostname": "127.0.0.1"})()

    assert setup._setup_target_host({"host": "192.168.50.200"}, Request()) == "192.168.50.200"


def test_successful_setup_job_is_stale_after_20_minutes_but_failed_job_stays_visible():
    now = datetime(2026, 6, 27, 12, 30, tzinfo=UTC)
    success = {
        "running": False,
        "finished_at": (now - timedelta(minutes=21)).isoformat(),
        "summary": {"successful": 1, "failed": 0, "cancelled": 0},
    }
    failed = {
        "running": False,
        "finished_at": (now - timedelta(hours=2)).isoformat(),
        "summary": {"successful": 0, "failed": 1, "cancelled": 0},
    }

    assert setup._setup_job_is_stale_success(success, now=now) is True
    assert setup._setup_job_is_stale_success(failed, now=now) is False


def test_setup_job_processes_devices_sequentially(monkeypatch):
    _add_devices()
    order = []

    async def fake_run(job, device_id, dry_run, host, port):
        order.append(device_id)
        setup._set_job_device(job, device_id, status="running", started_at=setup._iso_now(), step="verify")
        await asyncio.sleep(0.01)
        setup._set_job_device(job, device_id, status="ready", finished_at=setup._iso_now(), step="done")

    monkeypatch.setattr(setup, "_run_setup_device", fake_run)
    with TestClient(create_web_app()) as client:
        job = client.post("/api/setup/jobs/start", json={"device_ids": ["BATCH1", "BATCH2"], "host": "192.0.2.10", "dry_run": True}).json()
        final = _wait_job(client, job["job_id"])

    assert order == ["BATCH1", "BATCH2"]
    assert final["summary"]["successful"] == 2
    assert [item["status"] for item in final["devices"]] == ["ready", "ready"]


def test_setup_job_failure_does_not_stop_next_device(monkeypatch):
    _add_devices()

    async def fake_run(job, device_id, dry_run, host, port):
        if device_id == "BATCH1":
            setup._set_job_device(job, device_id, status="failed", finished_at=setup._iso_now(), error="verify failed")
        else:
            setup._set_job_device(job, device_id, status="ready", finished_at=setup._iso_now(), step="done")

    monkeypatch.setattr(setup, "_run_setup_device", fake_run)
    with TestClient(create_web_app()) as client:
        job = client.post("/api/setup/jobs/start", json={"device_ids": ["BATCH1", "BATCH2"], "host": "192.0.2.10", "dry_run": True}).json()
        final = _wait_job(client, job["job_id"])

    assert [item["status"] for item in final["devices"]] == ["failed", "ready"]
    assert final["summary"]["successful"] == 1
    assert final["summary"]["failed"] == 1


def test_setup_job_cancel_marks_queued_devices_cancelled(monkeypatch):
    _add_devices()
    started = asyncio.Event()

    async def fake_run(job, device_id, dry_run, host, port):
        setup._set_job_device(job, device_id, status="running", started_at=setup._iso_now())
        started.set()
        await asyncio.sleep(0.3)
        setup._set_job_device(job, device_id, status="ready", finished_at=setup._iso_now())

    monkeypatch.setattr(setup, "_run_setup_device", fake_run)
    with TestClient(create_web_app()) as client:
        job = client.post("/api/setup/jobs/start", json={"device_ids": ["BATCH1", "BATCH2"], "host": "192.0.2.10", "dry_run": True}).json()
        time.sleep(0.05)
        cancelled = client.post(f"/api/setup/jobs/{job['job_id']}/cancel").json()

    assert cancelled["devices"][1]["status"] == "cancelled"


def test_setup_job_success_logs_complete_not_cancelled(monkeypatch):
    _add_devices()
    events = []
    monkeypatch.setattr(setup, "write_masterlog", lambda event, **kwargs: events.append((event, kwargs)))

    async def fake_run(job, device_id, dry_run, host, port):
        setup._set_job_device(job, device_id, status="ready", finished_at=setup._iso_now(), step="done")

    monkeypatch.setattr(setup, "_run_setup_device", fake_run)
    with TestClient(create_web_app()) as client:
        job = client.post("/api/setup/jobs/start", json={"device_ids": ["BATCH1"], "host": "192.0.2.10", "dry_run": True}).json()
        _wait_job(client, job["job_id"])

    names = [event for event, _ in events]
    assert "setup_batch_complete" in names
    assert "setup_batch_cancelled" not in names


def test_setup_job_cancel_logs_cancelled_not_complete(monkeypatch):
    _add_devices()
    events = []
    monkeypatch.setattr(setup, "write_masterlog", lambda event, **kwargs: events.append((event, kwargs)))

    async def fake_run(job, device_id, dry_run, host, port):
        setup._set_job_device(job, device_id, status="running", started_at=setup._iso_now())
        while not job.get("cancel_requested"):
            await asyncio.sleep(0.01)

    monkeypatch.setattr(setup, "_run_setup_device", fake_run)
    with TestClient(create_web_app()) as client:
        job = client.post("/api/setup/jobs/start", json={"device_ids": ["BATCH1", "BATCH2"], "host": "192.0.2.10", "dry_run": True}).json()
        time.sleep(0.05)
        client.post(f"/api/setup/jobs/{job['job_id']}/cancel")
        time.sleep(0.08)

    names = [event for event, _ in events]
    assert "setup_batch_cancelled" in names
    assert "setup_batch_complete" not in names


def test_setup_job_partial_failure_logs_complete_with_failed_count(monkeypatch):
    _add_devices()
    events = []
    monkeypatch.setattr(setup, "write_masterlog", lambda event, **kwargs: events.append((event, kwargs)))

    async def fake_run(job, device_id, dry_run, host, port):
        status = "failed" if device_id == "BATCH1" else "ready"
        setup._set_job_device(job, device_id, status=status, finished_at=setup._iso_now(), step="done", error="verify failed" if status == "failed" else None)

    monkeypatch.setattr(setup, "_run_setup_device", fake_run)
    with TestClient(create_web_app()) as client:
        job = client.post("/api/setup/jobs/start", json={"device_ids": ["BATCH1", "BATCH2"], "host": "192.0.2.10", "dry_run": True}).json()
        _wait_job(client, job["job_id"])

    complete = [kwargs for event, kwargs in events if event == "setup_batch_complete"]
    assert complete
    assert complete[-1]["failed_count"] == 1
    assert not [event for event, _ in events if event == "setup_batch_cancelled"]


def test_source_bootstrap_retries_transient_set_marge_account_failure(monkeypatch):
    db = app_db.SessionLocal()
    device = Device(device_id="BOOTRETRY", name="Retry", ip_address="192.0.2.44")
    db.add(device)
    db.commit()
    attempts = []

    async def readiness(*args, **kwargs):
        return {"info": True, "sources": True, "bmx_registry": True}

    async def attempt(*args, **kwargs):
        attempts.append("try")
        if len(attempts) == 1:
            raise RuntimeError("500 Internal Server Error")
        return {"ok": True}

    monkeypatch.setattr(setup, "_source_bootstrap_readiness", readiness)
    monkeypatch.setattr(setup, "_source_bootstrap_attempt", attempt)
    job = {"job_id": "JOBRETRY", "devices": [{"device_id": "BOOTRETRY"}]}
    result = asyncio.run(setup._source_bootstrap_with_retries(job, job["devices"][0], device, "123", db, "192.0.2.10", 1516, False, False, delays=(0, 0)))
    db.close()

    assert result == {"ok": True}
    assert attempts == ["try", "try"]


def test_source_bootstrap_empty_exception_reports_non_empty_failure(monkeypatch):
    db = app_db.SessionLocal()
    device = Device(device_id="BOOTEMPTY", name="Empty", ip_address="192.0.2.45")
    db.add(device)
    db.commit()

    class EmptyError(Exception):
        def __str__(self):
            return ""

    async def readiness(*args, **kwargs):
        return {"info": True, "sources": True, "bmx_registry": True}

    async def attempt(*args, **kwargs):
        raise EmptyError()

    monkeypatch.setattr(setup, "_source_bootstrap_readiness", readiness)
    monkeypatch.setattr(setup, "_source_bootstrap_attempt", attempt)
    job = {"job_id": "JOBEMPTY", "devices": [{"device_id": "BOOTEMPTY"}]}
    with pytest.raises(OSError) as exc:
        asyncio.run(setup._source_bootstrap_with_retries(job, job["devices"][0], device, "123", db, "192.0.2.10", 1516, False, False, delays=(0,)))
    db.close()

    assert str(exc.value)
    assert "EmptyError" in str(exc.value)
    assert "endpoint=/setMargeAccount" in str(exc.value)


def test_sources_persistence_is_augmented_when_existing_file_is_incomplete(monkeypatch):
    db = app_db.SessionLocal()
    device = Device(device_id="SOURCEAUG", name="Source Aug", ip_address="192.0.2.46")
    db.add(device)
    db.commit()
    commands = []

    async def fake_factory_ssh(_device, command, timeout=25):
        commands.append(command)
        return {"returncode": 0, "stdout": "AUGMENTED", "stderr": ""}

    monkeypatch.setattr(setup, "_factory_ssh", fake_factory_ssh)
    result = asyncio.run(setup._ensure_sources_persistence_file(device))
    db.close()

    assert result["created"] is True
    command = commands[0]
    assert "TUNEIN" in command
    assert "WBMX" in command
    assert "Sources.xml.basswiesn-pre-augment" in command


def test_normal_setup_ui_shows_one_click_and_hides_old_cards():
    with TestClient(create_web_app()) as client:
        html = client.get("/").text
    js = open("basswiesn/app/static/app.js", encoding="utf-8").read()
    assert "SETUP AUSGEWÄHLTE RADIOS STARTEN" in html
    assert "setup-layout-main" in html
    assert 'setup-wizard-shell setup-locked lab-only' in html
    assert "Host nicht gesetzt" in html
    assert "Was zeigt das Radio?" not in html
    assert '<section class="panel lab-only"><h3>Bass capabilities</h3>' in html
    assert "activation_playback" in js
    assert "Aktivierungs-Wiedergabe erneut starten" in js


def test_setup_activation_playback_marks_device_complete(monkeypatch):
    db = app_db.SessionLocal()
    device = Device(device_id="ACTIVATE1", name="Activate", ip_address="192.0.2.55")
    db.add(device)
    db.add(Station(name="Activation", stream_url="http://example.test/live.mp3"))
    db.commit()
    events = []

    async def fake_play(device_id, station_id, payload, request, db):
        events.append(("play", device_id, station_id, payload))
        return {"ok": True}

    class Client:
        def __init__(self, ip):
            self.ip = ip

        async def get_xml(self, endpoint):
            return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus><ContentItem source="LOCAL_INTERNET_RADIO" location="x"/></nowPlaying>'

    monkeypatch.setattr(setup, "play_station_on_device", fake_play)
    monkeypatch.setattr(setup.api_core, "SoundTouchClient", Client)

    result = asyncio.run(setup._run_setup_activation_playback(device, db, duration_seconds=0))
    done = db.query(Setting).filter(Setting.key == "device:ACTIVATE1:first_playback_activation_done").one()
    db.close()

    assert result["ok"] is True
    assert events and events[0][1] == "ACTIVATE1"
    assert events[0][3]["trigger_type"] == "setup_activation"
    assert events[0][3]["internal_event"] is True
    assert done.value == "true"


def test_setup_activation_uses_candidate_pool_not_single_default():
    names = {item["name"] for item in setup.ACTIVATION_CANDIDATE_POOL}
    formats = {item["format"] for item in setup.ACTIVATION_CANDIDATE_POOL}

    assert len(names) >= 4
    assert {"mp3", "aac", "ogg", "hls"} <= formats
import pytest as _pytest_marker
pytestmark = [_pytest_marker.mark.integration, _pytest_marker.mark.slow]
