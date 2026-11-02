from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import xml.etree.ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from basswiesn.app.db.migrations import ensure_schema_baseline
from basswiesn.app.main import create_web_app
from basswiesn.app.models import ArtworkCacheEntry, Device, ReportingQueueEntry, ReportingState
from basswiesn.app.routers import api
from basswiesn.app.routers.multiroom import zone_payload
from basswiesn.app.services.artwork import cache_artwork, choose_artwork
from basswiesn.app.services.network_security import UrlValidation
from basswiesn.app.services.reporting_scheduler import ReportPayload, ReportingScheduler
from basswiesn.app.services.reporting_store import (
    PERSISTED_REPORT_REACQUIRE_REQUIRED,
    SqlAlchemyReportingStore,
    reporting_session_key,
)


def _store() -> tuple[object, sessionmaker, SqlAlchemyReportingStore]:
    engine = create_engine("sqlite:///:memory:")
    ensure_schema_baseline(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, factory, SqlAlchemyReportingStore(factory)


@pytest.mark.unit
def test_passive_device_status_badges_do_not_probe_ssh(monkeypatch) -> None:
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("passive status badges attempted SSH")

    monkeypatch.setattr(api, "_read_ssh_hosts", forbidden)
    monkeypatch.setattr(api, "_run_ssh_readonly_command", forbidden)
    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.get("/api/devices/status-badges")

    assert response.status_code == 200
    assert all(row["ssh"] == "unknown" for row in response.json())
    assert all(row["provenance"] == "NOT_PROBED" for row in response.json())


@pytest.mark.unit
def test_reporting_restart_sends_only_exact_integrity_checked_wire_payload() -> None:
    engine, factory, store = _store()
    due = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    expected = ReportPayload(
        timeStamp="2026-08-14T12:00:00Z",
        eventType="timed",
        reason="NORMAL",
        timeIntoTrack=120,
        playbackDelay=2,
        absolutePlayPoint="2026-08-14T11:58:00Z",
        reasonSubCode="NONE",
    )

    async def persist() -> None:
        scheduler = ReportingScheduler(lambda _url, _payload: None, store=store)
        await scheduler.enqueue(
            reporting_session_key("REPORT160", "ORION"),
            expected,
            report_url="https://provider.example/dynamic/opaque?token=secret",
            due_at=due,
            item_id="exact-report",
        )

    asyncio.run(persist())
    db = factory()
    try:
        state = db.query(ReportingState).one()
        entry = db.query(ReportingQueueEntry).one()
        stored = json.loads(entry.payload_json)
        assert state.report_url == "https://provider.example"
        assert "secret" not in state.report_url
        assert stored["persistable"] is True
        assert stored["wire_payload"] == expected.as_dict()
        assert entry.redacted is False
    finally:
        db.close()

    calls: list[tuple[str, dict]] = []

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"nextReportIn": 0}

    async def send(url: str, payload: dict) -> Response:
        calls.append((url, payload))
        return Response()

    async def resume() -> None:
        restored = store.load_sessions()[0]
        scheduler = ReportingScheduler(send, store=store)
        await scheduler.restore(restored)
        await scheduler.update_report_url(restored.key, "https://provider.example/fresh?token=new")
        await scheduler.process_due(restored.key, now=due)

    asyncio.run(resume())
    assert calls == [("https://provider.example/fresh?token=new", expected.as_dict())]
    engine.dispose()


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["unsafe", "tampered"])
def test_reporting_restart_fails_closed_for_unpersistable_or_tampered_payload(mode: str) -> None:
    engine, factory, store = _store()
    due = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    payload = ReportPayload(
        timeStamp="2026-08-14T12:00:00Z",
        eventType="start",
        reason="token=plain-secret" if mode == "unsafe" else "NORMAL",
    )

    async def persist() -> None:
        scheduler = ReportingScheduler(lambda _url, _payload: None, store=store)
        await scheduler.enqueue(
            reporting_session_key("REPORT160", "ORION"),
            payload,
            report_url="https://provider.example/opaque-secret?token=url-secret",
            due_at=due,
            item_id="token=item-secret" if mode == "unsafe" else "checked-report",
        )

    asyncio.run(persist())
    db = factory()
    try:
        entry = db.query(ReportingQueueEntry).one()
        if mode == "tampered":
            stored = json.loads(entry.payload_json)
            stored["wire_payload"]["reason"] = "ALTERED"
            entry.payload_json = json.dumps(stored)
            db.commit()
        serialized_rows = " ".join(
            str(value)
            for value in (
                db.query(ReportingState).one().report_url,
                entry.item_id,
                entry.event_type,
                entry.reason,
                entry.payload_json,
            )
        )
        assert "plain-secret" not in serialized_rows
        assert "url-secret" not in serialized_rows
        assert "item-secret" not in serialized_rows
    finally:
        db.close()

    restored = store.load_sessions()[0]
    assert restored.queue == []
    assert restored.next_due_at is None
    assert restored.report_url is None
    assert restored.status.value == "DEGRADED"
    assert restored.last_failure == PERSISTED_REPORT_REACQUIRE_REQUIRED

    calls = []

    async def must_not_send(url, wire):
        calls.append((url, wire))
        raise AssertionError("invalid persisted report reached the wire")

    async def resume() -> None:
        scheduler = ReportingScheduler(must_not_send, store=store)
        await scheduler.restore(restored)
        await scheduler.update_report_url(restored.key, "https://provider.example/fresh")
        await scheduler.process_due(restored.key, now=due + timedelta(hours=1))

    asyncio.run(resume())
    assert calls == []
    engine.dispose()


def _allowed_artwork(_url: str) -> UrlValidation:
    return UrlValidation(
        True,
        "ok",
        hostname="art.example",
        addresses=("203.0.113.9",),
        scheme="https",
        port=443,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content_type", "content"),
    [
        ("image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
        ("image/png", b'  <?xml version="1.0"?><svg><script/></svg>'),
    ],
)
def test_remote_svg_is_rejected_and_negative_cached(tmp_path, content_type: str, content: bytes) -> None:
    engine, factory, _ = _store()
    db: Session = factory()
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, headers={"content-type": content_type}, content=content)

    try:
        choice = choose_artwork(image_url="https://art.example/cover?token=runtime-only")
        first = asyncio.run(
            cache_artwork(
                db,
                choice,
                media_dir=tmp_path,
                transport=httpx.MockTransport(handler),
                validator=_allowed_artwork,
            )
        )
        second = asyncio.run(
            cache_artwork(
                db,
                choice,
                media_dir=tmp_path,
                transport=httpx.MockTransport(handler),
                validator=_allowed_artwork,
            )
        )
        row = db.query(ArtworkCacheEntry).one()
        assert first.failure_status == "UNSAFE_IMAGE_TYPE"
        assert second.failure_status == "UNSAFE_IMAGE_TYPE"
        assert first.public_url.startswith("/static/")
        assert row.cached_path is None
        assert len(calls) == 1
        assert calls[0].url.host == "203.0.113.9"
        assert calls[0].headers["host"] == "art.example"
        assert calls[0].extensions["sni_hostname"] == "art.example"
    finally:
        db.close()
        engine.dispose()


@pytest.mark.unit
def test_artwork_fetch_requires_a_dns_pin_even_after_positive_validation(tmp_path) -> None:
    engine, factory, _ = _store()
    db: Session = factory()
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"png")

    def incomplete(_url: str) -> UrlValidation:
        return UrlValidation(True, "ok", hostname="art.example", addresses=(), scheme="https", port=443)

    try:
        result = asyncio.run(
            cache_artwork(
                db,
                choose_artwork(image_url="https://art.example/cover.png"),
                media_dir=tmp_path,
                transport=httpx.MockTransport(handler),
                validator=incomplete,
            )
        )
        assert result.failure_status == "DNS_PIN_REQUIRED"
        assert called is False
    finally:
        db.close()
        engine.dispose()


@pytest.mark.unit
def test_zone_xml_builder_escapes_every_dynamic_value() -> None:
    master = Device(device_id='MASTER<&"', ip_address="192.0.2.1")
    member = Device(device_id="MEMBER<&>", ip_address='192.0.2.2&"')
    payload = zone_payload(master, [member])
    parsed = ET.fromstring(payload)

    assert parsed.attrib["master"] == master.device_id
    assert parsed.find("member").attrib["ipaddress"] == member.ip_address
    assert parsed.findtext("member") == member.device_id
    assert "&lt;" in payload and "&amp;" in payload and "&quot;" in payload


@pytest.mark.integration
def test_unknown_multiroom_member_fails_before_any_transport(monkeypatch) -> None:
    constructed: list[str] = []

    class NoTransport:
        def __init__(self, ip_address: str):
            constructed.append(ip_address)
            raise AssertionError("unknown-member validation must precede transport")

    monkeypatch.setattr("basswiesn.app.routers.multiroom.SoundTouchClient", NoTransport)
    with TestClient(create_web_app()) as client:
        client.post(
            "/api/devices",
            json={"device_id": "KNOWNMASTER160", "name": "Master", "ip_address": "192.0.2.61", "model": "SoundTouch Test"},
        )
        preview = client.post(
            "/api/multiroom/preview",
            json={"master_device_id": "KNOWNMASTER160", "member_device_ids": ["MISSING160"]},
        )
        write = client.post(
            "/api/multiroom/set",
            json={
                "master_device_id": "KNOWNMASTER160",
                "member_device_ids": ["MISSING160"],
                "dry_run": False,
                "memory_checked": True,
            },
        )

    assert preview.status_code == 404
    assert write.status_code == 404
    assert preview.json()["detail"]["missing_device_ids"] == ["MISSING160"]
    assert constructed == []


@pytest.mark.integration
def test_multiroom_status_keeps_contracts_separate(monkeypatch) -> None:
    responses = {
        "/getZone": '<zone master="STATUSMASTER160" senderIPAddress="192.0.2.71" senderIsMaster="true"><member ipaddress="192.0.2.72">STATUSMEMBER160</member></zone>',
        "/rebroadcastlatencymode": '<rebroadcastlatencymode mode="SYNC_TO_ZONE" controllable="true"/>',
        "/sources": '<sources><sourceItem source="LOCAL_INTERNET_RADIO" status="READY" multiroomallowed="true"/></sources>',
        "/now_playing": '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus><stationName>Test</stationName><ContentItem source="LOCAL_INTERNET_RADIO" sourceAccount="must-not-leak"/></nowPlaying>',
        "/volume": '<volume><actualvolume>1</actualvolume><targetvolume>1</targetvolume><mute>false</mute></volume>',
        "/outputLatency": '<OutputLatency value="37"/>',
    }

    class Client:
        def __init__(self, _ip_address: str):
            pass

        async def get_xml(self, path: str) -> str:
            return responses[path]

    monkeypatch.setattr("basswiesn.app.routers.multiroom.SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        for device_id, ip in (("STATUSMASTER160", "192.0.2.71"), ("STATUSMEMBER160", "192.0.2.72")):
            created = client.post("/api/devices", json={"device_id": device_id, "name": device_id, "ip_address": ip, "model": "SoundTouch Test"})
            assert created.status_code == 200
        response = client.get("/api/multiroom/status/STATUSMASTER160")

    assert response.status_code == 200
    body = response.json()
    assert set(body["contracts"]) == {
        "topology",
        "master",
        "members",
        "source",
        "clock",
        "output_latency",
        "volume",
        "rebroadcast_latency_mode",
    }
    assert body["master"]["device_id"] == "STATUSMASTER160"
    assert body["members"][0]["device_id"] == "STATUSMEMBER160"
    assert body["source"]["source"] == "LOCAL_INTERNET_RADIO"
    assert body["source"]["source_account_present"] is True
    assert "must-not-leak" not in response.text
    assert body["clock"]["confidence"] == "INFERRED_FROM_TOPOLOGY"
    assert body["output_latency"]["milliseconds"] == 37
    assert body["volume"]["actual"] == 1
