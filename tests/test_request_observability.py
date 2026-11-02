"""Offline request/job trace continuity without secret or identity collection."""

import asyncio
import json
import re
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
import httpx
import pytest

from basswiesn.app.core import masterlog
from basswiesn.app.core.errors import DeviceUnavailable, install_exception_handlers
from basswiesn.app.core.request_context import (
    RequestObservability,
    correlation_scope,
    current_context,
    current_correlation_id,
    install_request_observability,
    new_correlation_id,
    request_id_from_scope,
)
from basswiesn.app.services import task_registry
from basswiesn.app import db as app_db
from basswiesn.app.models import Device, DeviceInteraction
from basswiesn.app.services import device_interactions


pytestmark = pytest.mark.unit


def _app():
    app = FastAPI()
    install_exception_handlers(app)
    install_request_observability(app, listener="web")
    return app


def _records(tmp_path):
    path = tmp_path / "logs" / "master.log"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_request_ids_are_server_generated_and_logs_use_route_templates(tmp_path):
    app = _app()

    @app.get("/devices/{device_id}")
    async def endpoint(device_id: str):
        masterlog.write_masterlog("inside_request")
        return {"context": current_context()}

    with TestClient(app) as client:
        first = client.get("/devices/synthetic-private-identity?opaque=synthetic-private-query",
                           headers={"X-Request-ID": "untrusted-id", "Authorization": "Bearer opaque-private-value"})
        second = client.get("/devices/second")
    assert re.fullmatch(r"[a-f0-9]{32}", first.headers["X-Request-ID"])
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
    assert first.json()["context"] == {"request_id": first.headers["X-Request-ID"]}
    records = _records(tmp_path)
    assert records[0]["request_id"] == records[1]["request_id"] == first.headers["X-Request-ID"]
    completed = records[1]
    assert completed["route"] == "/devices/{device_id}"
    assert completed["status_code"] == 200
    assert completed["duration_ms"] >= 0
    assert "synthetic-private" not in json.dumps(records)
    assert "opaque-private-value" not in json.dumps(records)
    assert "untrusted-id" not in json.dumps(records)
    assert current_context() == {}


@pytest.mark.parametrize("error,status", [
    (DeviceUnavailable("token=synthetic-secret"), 503),
    (HTTPException(403, "denied"), 403),
    (RuntimeError("token=synthetic-secret"), 500),
])
def test_error_responses_and_local_error_events_share_id(tmp_path, error, status):
    app = _app()

    @app.get("/failure")
    async def fail():
        raise error

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/failure")
    assert response.status_code == status
    request_id = response.headers["X-Request-ID"]
    records = _records(tmp_path)
    assert all(row["request_id"] == request_id for row in records)
    assert "synthetic-secret" not in json.dumps(records)
    assert "synthetic-secret" not in response.text
    assert current_context() == {}


def test_unmatched_route_never_collects_raw_path(tmp_path):
    with TestClient(_app()) as client:
        response = client.get("/private-account-value")
    assert response.status_code == 404
    records = _records(tmp_path)
    assert records[-1]["route"] == "<unmatched>"
    assert "private-account-value" not in json.dumps(records)


def test_stream_is_not_buffered_or_replaced(tmp_path):
    app = _app()

    @app.get("/stream")
    async def stream():
        async def content():
            for value in (b"first", b"second"):
                masterlog.write_masterlog("stream_chunk")
                yield value
        return StreamingResponse(content(), headers={"X-Request-ID": "not-authoritative"})

    with TestClient(app) as client:
        response = client.get("/stream")
    assert response.content == b"firstsecond"
    assert response.headers["X-Request-ID"] != "not-authoritative"
    assert {row["request_id"] for row in _records(tmp_path)} == {response.headers["X-Request-ID"]}


def test_concurrent_requests_keep_independent_contexts(tmp_path):
    async def scenario():
        app = _app()
        arrived = 0
        release = asyncio.Event()

        @app.get("/concurrent/{number}")
        async def concurrent(number: int):
            nonlocal arrived
            first = current_context()
            arrived += 1
            if arrived == 12:
                release.set()
            await release.wait()
            await asyncio.sleep(0)
            assert current_context() == first
            masterlog.write_masterlog("concurrent_handler")
            return first

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            responses = await asyncio.gather(*(client.get(f"/concurrent/{number}") for number in range(12)))
        assert len({response.headers["X-Request-ID"] for response in responses}) == 12
        assert all(response.json()["request_id"] == response.headers["X-Request-ID"] for response in responses)
        assert current_context() == {}

    asyncio.run(scenario())
    rows = _records(tmp_path)
    assert len(rows) == 24
    assert len({row["request_id"] for row in rows}) == 12


def test_cancellation_preserves_semantics_and_resets_context(tmp_path):
    async def scenario():
        async def cancelled(scope, receive, send):
            assert current_correlation_id()
            raise asyncio.CancelledError()

        async def unused():
            raise AssertionError("not used")

        middleware = RequestObservability(cancelled, listener="debug")
        with pytest.raises(asyncio.CancelledError):
            await middleware({"type": "http", "method": "GET"}, unused, unused)
        assert current_context() == {}

    asyncio.run(scenario())
    assert _records(tmp_path)[0]["outcome"] == "cancelled"


def test_non_http_scope_passes_through_without_creating_context(tmp_path):
    async def scenario():
        calls = []

        async def app(scope, receive, send):
            calls.append(scope["type"])
            assert current_context() == {}

        middleware = RequestObservability(app, listener="web")
        await middleware({"type": "websocket"}, None, None)
        assert calls == ["websocket"]

    asyncio.run(scenario())
    assert not (tmp_path / "logs" / "master.log").exists()


def test_job_lifecycle_keeps_origin_after_request_context_ends(tmp_path, caplog):
    async def scenario():
        task_registry.clear_owned_tasks_for_tests()
        request_id = new_correlation_id()
        release = asyncio.Event()

        async def failing_worker():
            await release.wait()
            assert current_context()["request_id"] == request_id
            assert current_correlation_id() == current_context()["job_id"]
            raise RuntimeError("arbitrary-opaque-private-error")

        with correlation_scope(request_id=request_id):
            task = task_registry.start_owned_task("privacy-worker", failing_worker)
            assert task is task_registry.start_owned_task("privacy-worker", failing_worker)
            status = task_registry.owned_task_status()[0]
            assert status["request_id"] == request_id
            assert status["job_id"] != request_id
        assert current_context() == {}
        release.set()
        await task
        await task_registry.stop_owned_task("privacy-worker")
        task_registry.clear_owned_tasks_for_tests()
        return request_id, status["job_id"]

    request_id, job_id = asyncio.run(scenario())
    records = _records(tmp_path)
    assert [row["event"] for row in records] == [
        "background_task_started", "background_task_failed", "background_task_stopped",
    ]
    assert all(row["request_id"] == request_id and row["job_id"] == job_id for row in records)
    assert "arbitrary-opaque-private-error" not in json.dumps(records) + caplog.text


def test_success_and_cancelled_jobs_are_distinguishable(tmp_path):
    async def scenario():
        task_registry.clear_owned_tasks_for_tests()

        async def complete():
            return None

        task = task_registry.start_owned_task("completed", complete)
        await task
        await task_registry.stop_owned_task("completed")
        stop = asyncio.Event()
        task = task_registry.start_owned_task("cancelled", stop.wait)
        await asyncio.sleep(0)
        await task_registry.stop_owned_task("cancelled")
        task_registry.clear_owned_tasks_for_tests()

    asyncio.run(scenario())
    events = {row["event"] for row in _records(tmp_path)}
    assert {"background_task_completed", "background_task_cancelled"} <= events


def test_context_validation_restoration_and_copy():
    identifier = new_correlation_id()
    with correlation_scope(request_id=identifier):
        value = current_context()
        value["request_id"] = "mutated-copy"
        assert current_correlation_id() == identifier
        with correlation_scope():
            assert current_context() == {}
        assert current_correlation_id() == identifier
        with pytest.raises(ValueError):
            with correlation_scope(request_id="request\ninjection"):
                pass
        assert current_correlation_id() == identifier
    assert current_correlation_id() is None
    assert request_id_from_scope({"state": {"request_id": identifier}}) == identifier
    assert request_id_from_scope({"state": {"request_id": "untrusted"}}) is None
    assert request_id_from_scope({"state": None}) is None


def test_logging_failure_cannot_fail_successful_request(monkeypatch, tmp_path):
    # A path that is a file cannot contain the logs directory.
    denied = tmp_path / "not-a-directory"
    denied.write_text("fixture")
    monkeypatch.setattr(masterlog, "get_settings", lambda: SimpleNamespace(data_dir=denied, masterlog_enabled=True))
    app = _app()

    @app.get("/ok")
    def ok():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/ok")
    assert response.status_code == 200
    assert response.headers["X-Request-ID"]


@pytest.mark.parametrize("path", ["/live", "/ready", "/api/health", "/static/file.js", "/media/clip"])
def test_routine_probe_and_asset_successes_do_not_fsync_request_log(tmp_path, path):
    app = _app()
    app.add_api_route(path, lambda: {"ok": True})
    with TestClient(app) as client:
        response = client.get(path)
    assert response.headers["X-Request-ID"]
    assert not (tmp_path / "logs" / "master.log").exists()


def test_failed_health_probe_is_still_recorded(tmp_path):
    app = _app()

    @app.get("/ready")
    async def ready():
        raise HTTPException(503, "unavailable")

    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert _records(tmp_path)[0]["status_code"] == 503


def test_unexpected_sink_failure_does_not_replace_original_transport_error(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic-observer-error")

    monkeypatch.setattr(masterlog, "write_masterlog", broken)

    async def scenario():
        async def app(scope, receive, send):
            raise ValueError("original-transport-error")

        middleware = RequestObservability(app, listener="web")
        with pytest.raises(ValueError, match="original-transport-error"):
            await middleware({"type": "http", "method": "GET"}, None, None)
        assert current_context() == {}

    asyncio.run(scenario())


def test_invalid_settings_are_best_effort_and_never_print_exception_value(monkeypatch, caplog):
    def invalid():
        raise RuntimeError("opaque-configuration-secret")

    monkeypatch.setattr(masterlog, "get_settings", invalid)
    masterlog.write_masterlog("test")
    assert "opaque-configuration-secret" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_device_interactions_inherit_request_or_job_and_preserve_explicit_ids(monkeypatch):
    policy = SimpleNamespace(polling_profile=SimpleNamespace(value="test"),
                             device_class=SimpleNamespace(value="test"), safe_mode_active=True,
                             circuit_state=SimpleNamespace(value="closed"))
    monkeypatch.setattr(device_interactions, "policy_for_device", lambda *args: policy)
    monkeypatch.setattr(device_interactions, "is_protected_device", lambda *args: True)
    monkeypatch.setattr(device_interactions, "log_protected_block", lambda *args, **kwargs: None)
    request_id, job_id = new_correlation_id(), new_correlation_id()

    async def scenario():
        with app_db.SessionLocal() as db:
            device = Device(device_id="fixture-protected", name="fixture", ip_address="192.0.2.19")
            db.add(device)
            db.commit()
            coordinator = device_interactions.DeviceInteractionCoordinator()

            async def call(**kwargs):
                return await coordinator.request_xml(db, device, "/info", request_purpose="fixture",
                                                     requester="unit-test", **kwargs)

            with correlation_scope(request_id=request_id):
                assert (await call()).correlation_id == request_id
                assert (await call(correlation_id="explicit-existing-job")).correlation_id == "explicit-existing-job"
            with correlation_scope(request_id=request_id, job_id=job_id):
                assert (await call()).correlation_id == job_id
            assert (await call()).correlation_id not in {request_id, job_id}
            db.commit()
            rows = db.query(DeviceInteraction).order_by(DeviceInteraction.id).all()
            assert [row.correlation_id for row in rows[:3]] == [request_id, "explicit-existing-job", job_id]

    asyncio.run(scenario())
