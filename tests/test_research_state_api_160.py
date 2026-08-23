from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from basswiesn.app.db import get_db
from basswiesn.app.db.migrations import ensure_schema_baseline
from basswiesn.app.models import Device, MetadataState, RequestLog, ReportingState, Setting, Station
from basswiesn.app.repositories.research_state_repository import ResearchStateRepository
from basswiesn.app.routers import cloud, research_state
from basswiesn.app.services.airplay_readiness import assess_airplay_readiness
from basswiesn.app.services.health_models import (
    PlaybackSignals,
    ProviderSignals,
    reduce_playback_health,
    reduce_provider_health,
)
from basswiesn.app.services.metadata_engine import (
    MetadataProvenance,
    MetadataSnapshot,
)
from basswiesn.app.services.orion import StationDescriptor, encode_orion_data
from basswiesn.app.services.reporting_scheduler import ReportPayload, ReportingScheduler
from basswiesn.app.services.reporting_store import (
    SqlAlchemyReportingStore,
    reporting_session_key,
)
from basswiesn.app.services.restrictions import parse_restrictions


pytestmark = pytest.mark.unit


@pytest.fixture
def state_app(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'research-state.db'}",
        connect_args={"check_same_thread": False},
    )
    ensure_schema_baseline(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(research_state.router)
    app.include_router(cloud.router)
    app.dependency_overrides[get_db] = override_db
    try:
        yield app, factory
    finally:
        engine.dispose()


def _seed(factory) -> None:
    db: Session = factory()
    db.add_all(
        [
            Device(
                device_id="STATE160",
                name="Research Radio",
                model="SoundTouch Test",
                ip_address="192.0.2.88",
            ),
            Station(
                name="Research FM",
                stream_url="https://radio.example/live.mp3",
                image_url="https://art.example/logo.png",
                provider_station_id="station-160",
            ),
        ]
    )
    db.commit()
    db.close()


def test_repository_preserves_absent_and_explicit_zero_and_keeps_health_separate(state_app):
    _, factory = state_app
    _seed(factory)
    now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    db: Session = factory()
    repository = ResearchStateRepository(db)
    repository.upsert_restrictions(
        "STATE160",
        "local:absent",
        parse_restrictions({}, received_at=now),
    )
    repository.upsert_restrictions(
        "STATE160",
        "local:zero",
        parse_restrictions(
            {"restrictions": {"inactivityTimeout": 0}}, received_at=now
        ),
    )
    repository.upsert_provider_health(
        "STATE160",
        "LOCAL_INTERNET_RADIO",
        reduce_provider_health(
            ProviderSignals(
                service_available=True,
                source_visible=True,
                account_available=True,
                auth_valid=True,
                last_success=now,
            ),
            since=now,
        ),
    )
    repository.upsert_playback_health(
        "STATE160",
        reduce_playback_health(
            PlaybackSignals(
                radio_status="PLAY_STATE",
                source="LOCAL_INTERNET_RADIO",
                position_advancing=True,
                last_success=now,
            ),
            since=now,
        ),
        source_valid=True,
        position_advancing=True,
        provider_health="HEALTHY",
    )
    db.commit()
    db.close()

    with TestClient(state_app[0]) as client:
        restrictions = client.get("/api/devices/STATE160/restrictions").json()
        provider = client.get("/api/devices/STATE160/provider-health").json()
        playback = client.get("/api/devices/STATE160/playback-health").json()

    values = {
        row["source_key"]: row["inactivity_timeout"]
        for row in restrictions["restrictions"]
    }
    assert values == {"local:absent": None, "local:zero": 0}
    assert provider["state"] == "HEALTHY"
    assert playback["state"] == "PLAYING"
    assert playback["confidence"] == 95


def test_read_api_returns_unknown_without_inventing_false_evidence(state_app):
    app, factory = state_app
    _seed(factory)
    with TestClient(app) as client:
        playback = client.get("/api/devices/STATE160/playback-health")
        metadata = client.get("/api/devices/STATE160/metadata")
        airplay = client.get("/api/devices/STATE160/airplay-readiness")
        missing = client.get("/api/devices/DOES-NOT-EXIST/metadata")

    assert playback.status_code == 200
    assert playback.json()["state"] == "UNKNOWN"
    assert playback.json()["source_valid"] is None
    assert metadata.json()["stale"] is True
    assert metadata.json()["track"] is None
    assert airplay.json()["label"] == "Unbekannt"
    assert airplay.json()["auth_hardware_detected"] is None
    assert missing.status_code == 404


def test_human_live_metadata_update_preserves_selection_and_requests_no_playback_action(state_app):
    app, factory = state_app
    _seed(factory)
    db: Session = factory()
    db.add(
        MetadataState(
            device_id="STATE160",
            station_name="Research FM",
            station_id="station-160",
            provider="LOCAL_INTERNET_RADIO",
            source="LOCAL_INTERNET_RADIO",
            provenance="PROVIDER",
            stale=False,
        )
    )
    db.commit()
    db.close()
    captured = {}

    class Runtime:
        async def ingest_metadata(self, device_id, payload, **identity):
            captured.update({"device_id": device_id, "payload": payload, **identity})
            return MetadataSnapshot(
                station_name=identity["station_name"],
                station_id=identity["station_id"],
                track=payload["track"],
                artist=payload["artist"],
                album=payload["album"],
                image_url=payload["imageUrl"],
                provider=identity["provider"],
                source=identity["source"],
                updated_at=datetime.now(UTC),
                provenance=identity["provenance"],
                confidence=identity["confidence"],
                stale=False,
            )

    app.state.research_runtime = Runtime()
    with TestClient(app) as client:
        updated = client.put(
            "/api/devices/STATE160/metadata/live",
            json={
                "track": "Neuer Titel",
                "artist": "Neue Interpretin",
                "album": "Neues Album",
                "imageUrl": "https://art.example/live.png",
            },
        )
        rejected_identity = client.put(
            "/api/devices/STATE160/metadata/live",
            json={"track": "X", "source": "AIRPLAY"},
        )

    assert updated.status_code == 200
    assert updated.json()["playback_action"] == "NONE"
    assert updated.json()["source_change"] is False
    assert updated.json()["set_url"] is False
    assert updated.json()["radio_write"] is False
    assert captured["station_id"] == "station-160"
    assert captured["provider"] == "LOCAL_INTERNET_RADIO"
    assert captured["source"] == "LOCAL_INTERNET_RADIO"
    assert captured["payload"]["track"] == "Neuer Titel"
    assert rejected_identity.status_code == 422


def test_clock_metadata_setting_is_per_device_off_by_default_and_minimum_60(state_app):
    app, factory = state_app
    _seed(factory)
    headers = {"x-basswiesn-device-id": "STATE160"}
    with TestClient(app, base_url="http://192.0.2.40:1516") as client:
        initial = client.get("/api/devices/STATE160/metadata/clock")
        lab_blocked = client.put(
            "/api/devices/STATE160/metadata/clock",
            json={"enabled": True, "mode": "APPEND", "interval_seconds": 60},
        )
        db = factory()
        db.add(Setting(key="lab_mode", value="true"))
        db.commit()
        db.close()
        too_fast = client.put(
            "/api/devices/STATE160/metadata/clock",
            json={"enabled": True, "mode": "APPEND", "interval_seconds": 30},
        )
        enabled = client.put(
            "/api/devices/STATE160/metadata/clock",
            json={"enabled": True, "mode": "APPEND", "interval_seconds": 60},
        )
        projected = client.get(
            "/bmx/orion/now-playing/station/station-160", headers=headers
        )
        db = factory()
        db.query(Setting).filter(Setting.key == "lab_mode").one().value = "false"
        db.commit()
        db.close()
        lab_off = client.get(
            "/bmx/orion/now-playing/station/station-160", headers=headers
        )

    assert initial.json()["enabled"] is False
    assert initial.json()["hardware_validation"] == "OPEN"
    assert lab_blocked.status_code == 403
    assert too_fast.status_code == 422
    assert enabled.json()["mode"] == "APPEND"
    assert enabled.json()["experimental"] is True
    assert projected.json()["askAgainAfter"] == 60
    assert len(projected.json()["track"]) == 5
    assert projected.json()["track"][2] == ":"
    assert lab_off.json()["track"] == "Research FM"


def test_orion_station_metadata_and_reporting_are_real_separate_contracts(state_app):
    app, factory = state_app
    _seed(factory)
    descriptor = StationDescriptor(
        "Research FM",
        "https://radio.example/live.mp3?token=stream-secret",
        "https://art.example/logo.png",
        "station-160",
        stream_mime="audio/mpeg",
    )
    headers = {"x-basswiesn-device-id": "STATE160"}
    with TestClient(app, base_url="http://192.0.2.40:1516") as client:
        station_response = client.get(
            "/core02/svc-bmx-adapter-orion/prod/orion/station",
            params={"data": encode_orion_data(descriptor)},
            headers=headers,
        )
        # 27.0.6 may consume later metadata only from the next ReportResponse
        # instead of polling the advertised now-playing link again.
        metadata_db: Session = factory()
        metadata_row = metadata_db.query(MetadataState).filter(
            MetadataState.device_id == "STATE160"
        ).one()
        metadata_row.track = "Live aus Reporting"
        metadata_row.artist = "BASSWIESN QA"
        metadata_row.album = "Contract Test"
        metadata_row.artwork_url = "https://art.example/live.png"
        metadata_db.commit()
        metadata_db.close()
        report_response = client.post(
            "/bmx/orion/reporting/station/station-160",
            json={
                "timeStamp": "2026-08-14T12:00:00Z",
                "eventType": "TIMED",
                "reason": "NORMAL",
                "timeIntoTrack": 10,
                "playbackDelay": 0,
                "absolutePlayPoint": "https://radio.example/x?token=report-secret",
                "reasonSubCode": "0",
                "unconfirmedField": "not persisted",
            },
            headers=headers,
        )
        metadata_response = client.get(
            "/bmx/orion/now-playing/station/station-160", headers=headers
        )
        invalid_report = client.post(
            "/bmx/orion/reporting/station/station-160",
            json={"timeIntoTrack": True},
            headers=headers,
        )

    station_payload = station_response.json()
    assert station_payload["restrictions"] == {"inactivityTimeout": 0}
    assert station_payload["_links"]["bmx_reporting"]["href"].endswith(
        "/bmx/orion/reporting/station/station-160"
    )
    assert station_payload["_links"]["bmx_nowplaying"]["href"].endswith(
        "/bmx/orion/now-playing/station/station-160"
    )
    assert set(report_response.json()) == {"nextReportIn", "_links", "_embedded"}
    assert report_response.json()["nextReportIn"] == 6
    embedded = report_response.json()["_embedded"]["bmx_nowplaying"]
    assert embedded["track"] == "Live aus Reporting"
    assert embedded["artist"] == "BASSWIESN QA"
    assert embedded["album"] == "Contract Test"
    assert embedded["imageUrl"] == "https://art.example/live.png"
    assert embedded["askAgainAfter"] == 6
    assert metadata_response.json()["track"] == "Live aus Reporting"
    assert metadata_response.json()["askAgainAfter"] == 6
    assert invalid_report.status_code == 400

    db: Session = factory()
    reporting = db.query(ReportingState).filter(ReportingState.device_id == "STATE160").one()
    metadata = db.query(MetadataState).filter(MetadataState.device_id == "STATE160").one()
    logged = "\n".join(row.body for row in db.query(RequestLog).all())
    db.close()
    assert reporting.state == "SUCCESS"
    assert reporting.last_http_status == 200
    assert metadata.station_name == "Research FM"
    assert "report-secret" not in logged
    assert "unconfirmedField" not in logged


def test_orion_station_switch_does_not_leak_previous_runtime_metadata(state_app):
    app, factory = state_app
    _seed(factory)
    observed = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    db: Session = factory()
    ResearchStateRepository(db).upsert_metadata(
        "STATE160",
        MetadataSnapshot(
            station_name="Old station",
            station_id="old-id",
            track="Old track",
            artist="Old artist",
            album="Old album",
            image_url="https://old-art.example/cover.jpg?token=old-secret",
            provider="LOCAL_INTERNET_RADIO",
            source="LOCAL_INTERNET_RADIO",
            updated_at=observed,
            provenance=MetadataProvenance.PROVIDER,
            confidence=100,
            stale=False,
        ),
    )
    db.add(
        Station(
            name="New station",
            stream_url="https://radio.example/new.mp3",
            image_url="https://new-art.example/cover.jpg?token=new-secret",
            provider_station_id="new-id",
        )
    )
    db.commit()
    db.close()

    descriptor = StationDescriptor(
        "New station",
        "https://radio.example/new.mp3",
        "https://new-art.example/cover.jpg?token=new-secret",
        "new-id",
    )
    headers = {"x-basswiesn-device-id": "STATE160"}
    with TestClient(app, base_url="http://192.0.2.40:1516") as client:
        selected = client.get(
            "/core02/svc-bmx-adapter-orion/prod/orion/station",
            params={"data": encode_orion_data(descriptor)},
            headers=headers,
        )
        now_playing = client.get(
            "/bmx/orion/now-playing/station/new-id", headers=headers
        ).json()

    assert selected.status_code == 200
    assert now_playing["track"] == "New station"
    assert now_playing["artist"] == ""
    assert now_playing["album"] == ""
    assert now_playing["imageUrl"] == "https://new-art.example/cover.jpg"
    serialized = str(now_playing)
    assert "Old track" not in serialized
    assert "Old artist" not in serialized
    assert "old-secret" not in serialized
    assert "new-secret" not in serialized

    db = factory()
    row = db.query(MetadataState).filter(MetadataState.device_id == "STATE160").one()
    assert row.station_id == "new-id"
    assert row.track is None
    assert row.artist is None
    assert row.album is None
    assert row.artwork_url == "https://new-art.example/cover.jpg"
    db.close()


def test_metadata_airplay_and_timeline_are_redacted_and_paged(state_app):
    app, factory = state_app
    _seed(factory)
    observed = datetime(2026, 8, 14, 13, 0, tzinfo=UTC)
    db: Session = factory()
    repository = ResearchStateRepository(db)
    repository.upsert_metadata(
        "STATE160",
        MetadataSnapshot(
            station_name="Research FM",
            station_id="station-160",
            track="A Track",
            artist="An Artist",
            album="An Album",
            image_url="https://192.0.2.99/art.jpg?token=art-secret",
            provider="LOCAL_INTERNET_RADIO",
            source="LOCAL_INTERNET_RADIO",
            updated_at=observed,
            provenance=MetadataProvenance.PROVIDER,
            confidence=91,
            stale=False,
        ),
    )
    repository.upsert_airplay_readiness(
        "STATE160",
        assess_airplay_readiness(
            firmware_version="27.0.6",
            product_id="0x093B",
            variant="SM2",
            auth_hardware_detected=True,
            sts_registered=True,
            source_visible=True,
            mdns_visible=True,
            pairing_ready=True,
            ptp_ready=True,
            audio_ready=True,
            evidence=({"probe": "read-only"},),
        ),
    )
    repository.record_event(
        device_id="STATE160",
        domain="PROVIDER",
        code="SECRET_CHECK",
        message="token=topsecret peer=192.0.2.99",
        evidence={
            "authorization": "Bearer topsecret",
            "url": "https://192.0.2.99/path?token=topsecret",
            "confidence": 88,
        },
        occurred_at=observed,
    )
    db.commit()
    db.close()

    with TestClient(app) as client:
        metadata = client.get("/api/devices/STATE160/metadata").json()
        airplay = client.get("/api/devices/STATE160/airplay-readiness").json()
        first_page = client.get(
            "/api/devices/STATE160/diagnostics/timeline", params={"limit": 2}
        ).json()
        filtered = client.get(
            "/api/devices/STATE160/diagnostics/timeline",
            params={"domain": "provider"},
        ).json()
        invalid_range = client.get(
            "/api/devices/STATE160/diagnostics/timeline",
            params={
                "from": "2026-08-15T00:00:00Z",
                "to": "2026-08-14T00:00:00Z",
            },
        )

    assert metadata["confidence"] == 91
    assert "art-secret" not in (metadata["image_url"] or "")
    assert metadata["image_url"] == "https://192.0.2.99/art.jpg"
    assert airplay["label"] == "Bereit"
    assert airplay["blocking_stage"] == "NONE"
    assert len(first_page["items"]) == 2
    assert first_page["next_cursor"] is not None
    assert [item["domain"] for item in filtered["items"]] == ["PROVIDER"]
    serialized = str(filtered)
    assert "topsecret" not in serialized
    assert "192.0.2.99" not in serialized
    assert invalid_range.status_code == 400


def test_expired_transient_airplay_evidence_cannot_remain_ready(state_app):
    app, factory = state_app
    _seed(factory)
    now = datetime.now(UTC)
    db: Session = factory()
    ResearchStateRepository(db).upsert_airplay_readiness(
        "STATE160",
        assess_airplay_readiness(
            firmware_version="27.0.6.46330.5043500",
            product_id="0x093B",
            variant="SM2",
            auth_hardware_detected=True,
            sts_registered=True,
            source_visible=True,
            mdns_visible=True,
            pairing_ready=True,
            ptp_ready=True,
            audio_ready=True,
            evidence=({"probe": "read-only-mdns"},),
        ),
        observed_at=now - timedelta(minutes=10),
        expires_at=now - timedelta(minutes=5),
        provenance="READ_ONLY_RUNTIME_COLLECTOR",
    )
    db.commit()
    db.close()

    with TestClient(app) as client:
        payload = client.get(
            "/api/devices/STATE160/airplay-readiness"
        ).json()

    assert payload["expired"] is True
    assert payload["label"] == "Teilweise bereit"
    assert payload["blocking_stage"] == "AUTH_HARDWARE"
    assert payload["auth_hardware_detected"] is None
    assert payload["mdns_visible"] is None
    assert payload["audio_ready"] is None
    assert payload["expires_at"] is not None
    assert payload["provenance"] == "READ_ONLY_RUNTIME_COLLECTOR"


def test_explicit_airplay_probe_is_targeted_read_only_and_marks_inference(state_app, monkeypatch):
    app, factory = state_app
    db: Session = factory()
    db.add(
        Device(
            device_id="AIRPLAY-SM2",
            name="Küche",
            model="SoundTouch 20 Series III",
            ip_address="192.0.2.112",
            firmware="27.0.6.46330.5043500",
        )
    )
    db.commit()
    db.close()
    calls: list[str] = []

    class FakeSoundTouchClient:
        def __init__(self, ip_address, **kwargs):
            assert ip_address == "192.0.2.112"
            assert kwargs["device_id"] == "AIRPLAY-SM2"
            assert kwargs["trigger"] == "explicit_webui_action"

        async def get_xml(self, path: str) -> str:
            calls.append(path)
            return {
                "/info": '<info deviceID="AIRPLAY-SM2"><type>SoundTouch 20 Series III</type><components><component><softwareVersion>27.0.6.46330.5043500 build</softwareVersion></component></components><moduleType>sm2</moduleType><variant>spotty</variant></info>',
                "/sources": '<sources><sourceItem source="AIRPLAY" status="READY"/></sources>',
                "/capabilities": '<capabilities><capability name="AIRPLAY"/></capabilities>',
            }[path]

    monkeypatch.setattr(research_state, "SoundTouchClient", FakeSoundTouchClient)
    monkeypatch.setattr(
        research_state,
        "probe_targeted_airplay_mdns",
        lambda *_args, **_kwargs: {
            "targeted": True,
            "transport": "UDP_LEGACY_UNICAST_MDNS",
            "mdns_visible": True,
            "ttl_seconds": 10,
            "visible_services": ["_airplay._tcp.local", "_raop._tcp.local"],
            "services": {},
        },
    )
    with TestClient(app) as client:
        response = client.post("/api/devices/AIRPLAY-SM2/airplay-readiness/probe", json={})

    assert response.status_code == 200
    payload = response.json()
    assert calls == ["/info", "/sources", "/capabilities"]
    assert payload["product_id"] == "0X093B"
    assert payload["platform"] == "SM2"
    assert payload["source_visible"] is True
    assert payload["auth_hardware_detected"] is True
    assert payload["sts_registered"] is True
    assert payload["mdns_visible"] is True
    assert payload["blocking_stage"] == "PAIRING"
    assert payload["label"] == "Teilweise bereit"
    assert payload["provenance"] == "EXPLICIT_READ_ONLY_HTTP_TARGETED_MDNS"
    assert payload["expires_at"] is not None
    assert (
        datetime.fromisoformat(payload["expires_at"])
        - datetime.fromisoformat(payload["observed_at"])
    ).total_seconds() == 10
    assert any(
        row.get("product_id_provenance") == "CONFIRMED_STATIC_PROFILE"
        for row in payload["evidence"]
    )
    assert any(row.get("mdns_scanned") is True for row in payload["evidence"])
    assert any(
        row.get("auth_hardware_and_sts_inferred") is True
        for row in payload["evidence"]
    )


def test_explicit_airplay_probe_stops_after_identity_mismatch(state_app, monkeypatch):
    app, factory = state_app
    _seed(factory)
    calls: list[str] = []

    class WrongIdentityClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get_xml(self, path: str) -> str:
            calls.append(path)
            return '<info deviceID="SOMEONE-ELSE"/>'

    monkeypatch.setattr(research_state, "SoundTouchClient", WrongIdentityClient)
    with TestClient(app) as client:
        response = client.post("/api/devices/STATE160/airplay-readiness/probe", json={})

    assert response.status_code == 409
    assert calls == ["/info"]


def test_explicit_airplay_probe_reports_unreachable_without_fake_success(state_app, monkeypatch):
    app, factory = state_app
    _seed(factory)

    class OfflineClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get_xml(self, path: str) -> str:
            raise OSError("offline")

    monkeypatch.setattr(research_state, "SoundTouchClient", OfflineClient)
    with TestClient(app) as client:
        response = client.post("/api/devices/STATE160/airplay-readiness/probe", json={})
        persisted = client.get("/api/devices/STATE160/airplay-readiness")

    assert response.status_code == 502
    assert "nicht erreichbar" in response.json()["detail"]
    assert persisted.json()["label"] == "Unbekannt"


def test_explicit_airplay_probe_blocks_protected_target_before_transport(state_app, monkeypatch):
    app, factory = state_app
    db: Session = factory()
    db.add(
        Device(
            device_id="CCDDEEFF0011",
            name="Protected",
            model="SoundTouch",
            ip_address="192.168.50.25",
        )
    )
    db.commit()
    db.close()

    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("transport must not be constructed")

    monkeypatch.setattr(research_state, "SoundTouchClient", ForbiddenClient)
    with TestClient(app) as client:
        response = client.post(
            "/api/devices/CCDDEEFF0011/airplay-readiness/probe", json={}
        )

    assert response.status_code == 403


def test_reporting_scheduler_store_restores_queue_and_absolute_due_time(state_app):
    _, factory = state_app
    store = SqlAlchemyReportingStore(factory)

    async def unused_post(url, payload):
        raise AssertionError(f"restore test must not send {url} {payload}")

    async def persist():
        scheduler = ReportingScheduler(unused_post, store=store)
        await scheduler.enqueue(
            reporting_session_key("STATE160", "LOCAL_INTERNET_RADIO"),
            ReportPayload(
                timeStamp="2026-08-14T12:00:00Z",
                eventType="TIMED",
                reason="NORMAL",
            ),
            report_url="https://provider.example/report?token=secret",
            due_at=datetime(2026, 8, 14, 12, 5, tzinfo=UTC),
            item_id="persisted-report",
        )

    asyncio.run(persist())
    restored = store.load_sessions()

    assert len(restored) == 1
    assert restored[0].key == "STATE160::LOCAL_INTERNET_RADIO"
    assert restored[0].queue[0].item_id == "persisted-report"
    assert restored[0].next_due_at.replace(tzinfo=UTC) == datetime(
        2026, 8, 14, 12, 5, tzinfo=UTC
    )
    assert restored[0].report_url is None
    assert restored[0].status.value == "DEGRADED"
    assert restored[0].last_failure == "REPORT_URL_REFRESH_REQUIRED"

    async def refresh_link():
        scheduler = ReportingScheduler(unused_post, store=store)
        await scheduler.restore(restored[0])
        await scheduler.update_report_url(
            restored[0].key, "https://provider.example/current-report?token=fresh"
        )
        return scheduler.session(restored[0].key)

    refreshed = asyncio.run(refresh_link())
    assert refreshed.report_url.endswith("token=fresh")
    assert refreshed.status.value == "QUEUED"
