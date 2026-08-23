from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import (
    Device,
    DiagnosticEvent,
    MetadataState,
    PlaybackHealthState,
    ProviderHealthState,
    ReportingState,
    RestrictionState,
    TelemetryEvent,
)
from basswiesn.app.services.device_state import save_runtime_state
from basswiesn.app.services.playback_keepalive import run_playback_keepalive_for_device
from basswiesn.app.services.research_runtime import ResearchRuntime


pytestmark = pytest.mark.integration


class ReadOnlyPlayingClient:
    posts: list[tuple[str, str]] = []
    gets: list[str] = []

    def __init__(self, _ip: str):
        pass

    async def get_xml(self, endpoint: str) -> str:
        self.gets.append(endpoint)
        if endpoint == "/now_playing":
            return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus></nowPlaying>'
        if endpoint == "/volume":
            return "<volume><actualvolume>1</actualvolume></volume>"
        raise AssertionError(f"unexpected keepalive read: {endpoint}")

    async def post_xml(self, endpoint: str, body: str) -> str:
        self.posts.append((endpoint, body))
        raise AssertionError("playback keepalive must never write")


def _device_with_runtime(
    db,
    device_id: str,
    *,
    now: datetime,
    providers: dict | None = None,
) -> Device:
    device = Device(device_id=device_id, name=device_id, ip_address="192.0.2.210")
    db.add(device)
    db.commit()
    save_runtime_state(
        db,
        device_id,
        {
            "providers": providers or {},
            "playback_keepalive": {
                "playing": True,
                "last_source": "LOCAL_INTERNET_RADIO",
                "playback_started_at": (now - timedelta(hours=24)).isoformat(),
            }
        },
    )
    return device


def test_explicit_zero_restriction_stays_disabled_during_24h_playback():
    now = datetime(2030, 1, 2, tzinfo=UTC)
    db = app_db.SessionLocal()
    device = _device_with_runtime(db, "KEEPZERO160", now=now)
    db.add(
        RestrictionState(
            device_id=device.device_id,
            source_key="LOCAL_INTERNET_RADIO",
            inactivity_timeout_s=0,
            timer_enabled=False,
            received_at=now - timedelta(hours=24),
            origin="SERVER_RESPONSE",
        )
    )
    db.commit()
    ReadOnlyPlayingClient.posts = []
    ReadOnlyPlayingClient.gets = []

    result = asyncio.run(
        run_playback_keepalive_for_device(device, db, now=now, client_factory=ReadOnlyPlayingClient)
    )
    diagnostics = db.query(DiagnosticEvent).filter(DiagnosticEvent.device_id == device.device_id).all()
    db.close()

    assert result["ok"] is True
    assert result["duration_seconds"] == 24 * 60 * 60
    assert result["restriction"]["state"] == "DISABLED"
    assert ReadOnlyPlayingClient.gets == ["/now_playing", "/volume"]
    assert ReadOnlyPlayingClient.posts == []
    assert [event.code for event in diagnostics] == [
        "PROVIDER_DEGRADED",
        "PLAYBACK_PLAYING",
    ]


def test_server_21600_expiry_is_diagnostic_only_while_radio_still_plays():
    now = datetime(2030, 1, 2, tzinfo=UTC)
    received = now - timedelta(seconds=21601)
    db = app_db.SessionLocal()
    device = _device_with_runtime(db, "KEEP21600160", now=now)
    db.add(
        RestrictionState(
            device_id=device.device_id,
            source_key="LOCAL_INTERNET_RADIO",
            inactivity_timeout_s=21600,
            timer_enabled=True,
            received_at=received,
            timer_started_at=received,
            effective_until=received + timedelta(seconds=21600),
            origin="SERVER_RESPONSE",
        )
    )
    db.commit()
    ReadOnlyPlayingClient.posts = []
    ReadOnlyPlayingClient.gets = []

    result = asyncio.run(
        run_playback_keepalive_for_device(device, db, now=now, client_factory=ReadOnlyPlayingClient)
    )
    diagnostics = db.query(DiagnosticEvent).filter(DiagnosticEvent.device_id == device.device_id).all()
    telemetry = db.query(TelemetryEvent).filter(TelemetryEvent.device_id == device.device_id).all()
    db.close()

    assert result["ok"] is True
    assert result["playing"] is True
    assert result["restriction"]["state"] == "EXPIRED"
    assert result["invalid_source_action"] == "NONE"
    assert ReadOnlyPlayingClient.gets == ["/now_playing", "/volume"]
    assert ReadOnlyPlayingClient.posts == []
    assert [event.code for event in diagnostics] == [
        "INACTIVITY_RESTRICTION_EXPIRED",
        "PROVIDER_DEGRADED",
        "PLAYBACK_PLAYING",
    ]
    assert [event.event_type for event in telemetry] == ["restriction_expired_observed"]


@pytest.mark.parametrize(
    ("reporting_state", "metadata_stale", "expected_provider_health"),
    [
        ("RETRY_WAIT", False, "REPORTING_DEGRADED"),
        ("SUCCESS", True, "METADATA_STALE"),
    ],
)
def test_keepalive_projects_subsystem_health_without_changing_playback(
    reporting_state: str,
    metadata_stale: bool,
    expected_provider_health: str,
):
    now = datetime(2030, 1, 2, tzinfo=UTC)
    db = app_db.SessionLocal()
    provider_id = "LOCAL_INTERNET_RADIO"
    device = _device_with_runtime(
        db,
        f"KEEP{expected_provider_health}160",
        now=now,
        providers={
            provider_id: {
                "source_observed": True,
                "visible_in_sources": True,
                "service_observed": True,
                "service_available": True,
            }
        },
    )
    db.add(
        MetadataState(
            device_id=device.device_id,
            provider=provider_id,
            source=provider_id,
            track="Track",
            provenance="PROVIDER",
            confidence=90,
            stale=metadata_stale,
            updated_at=now - timedelta(minutes=10) if metadata_stale else now,
        )
    )
    db.add(
        ReportingState(
            device_id=device.device_id,
            provider_id=provider_id,
            state=reporting_state,
            queue_depth=1 if reporting_state == "RETRY_WAIT" else 0,
            retry_count=1 if reporting_state == "RETRY_WAIT" else 0,
            last_http_status=503 if reporting_state == "RETRY_WAIT" else 200,
            last_success_at=now if reporting_state == "SUCCESS" else None,
        )
    )
    db.commit()
    ReadOnlyPlayingClient.posts = []
    ReadOnlyPlayingClient.gets = []

    result = asyncio.run(
        run_playback_keepalive_for_device(
            device,
            db,
            now=now,
            client_factory=ReadOnlyPlayingClient,
        )
    )
    provider = (
        db.query(ProviderHealthState)
        .filter(
            ProviderHealthState.device_id == device.device_id,
            ProviderHealthState.provider_id == provider_id,
        )
        .one()
    )
    playback = (
        db.query(PlaybackHealthState)
        .filter(PlaybackHealthState.device_id == device.device_id)
        .one()
    )

    assert result["ok"] is True
    assert result["playing"] is True
    assert result["provider_health"] == expected_provider_health
    assert provider.state == expected_provider_health
    assert playback.state == "PLAYING"
    assert playback.provider_health == expected_provider_health
    assert ReadOnlyPlayingClient.gets == ["/now_playing", "/volume"]
    assert ReadOnlyPlayingClient.posts == []
    db.close()


def test_failed_authoritative_readback_replaces_stale_playing_health_without_write():
    now = datetime(2030, 1, 2, tzinfo=UTC)
    db = app_db.SessionLocal()
    device = _device_with_runtime(db, "KEEPREADFAIL160", now=now)
    db.add(
        PlaybackHealthState(
            device_id=device.device_id,
            state="PLAYING",
            source_valid=True,
            reason="AUTHORITATIVE_RADIO_PLAY_STATE",
            evidence_json="[]",
            since=now - timedelta(minutes=5),
            observed_at=now - timedelta(minutes=1),
        )
    )
    db.commit()

    class FailedReadbackClient:
        posts: list[tuple[str, str]] = []

        def __init__(self, _ip: str):
            pass

        async def get_xml(self, endpoint: str) -> str:
            assert endpoint == "/now_playing"
            raise TimeoutError("local fake read timeout")

        async def post_xml(self, endpoint: str, body: str) -> str:
            self.posts.append((endpoint, body))
            raise AssertionError("failed keepalive must never write")

    result = asyncio.run(
        run_playback_keepalive_for_device(
            device,
            db,
            now=now,
            client_factory=FailedReadbackClient,
        )
    )
    health = (
        db.query(PlaybackHealthState)
        .filter(PlaybackHealthState.device_id == device.device_id)
        .one()
    )
    evidence = json.loads(health.evidence_json)

    assert result["ok"] is False
    assert result["reads"] == ["/now_playing"]
    assert health.state == "FAILED"
    assert health.reason == "NO_AUTHORITATIVE_RADIO_READBACK"
    assert health.source_valid is None
    assert any(
        item.get("type") == "AUTHORITATIVE_RADIO_READBACK_FAILED"
        and item.get("failed_endpoint") == "/now_playing"
        and item.get("automatic_action") == "NONE"
        for item in evidence
    )
    assert FailedReadbackClient.posts == []
    db.close()


def test_keepalive_ingests_observed_radio_metadata_and_clears_old_station_fields():
    now = datetime(2030, 1, 2, tzinfo=UTC)
    clock = [now]
    db = app_db.SessionLocal()
    device = _device_with_runtime(db, "KEEPMETADATA160", now=now)
    db.add(
        MetadataState(
            device_id=device.device_id,
            station_name="Station A",
            station_id="station-a",
            track="Old track",
            artist="Old artist",
            album="Old album",
            artwork_url="https://art.example/old.png",
            provider="LOCAL_INTERNET_RADIO",
            source="LOCAL_INTERNET_RADIO",
            provenance="RADIO",
            confidence=100,
            stale=False,
            updated_at=now - timedelta(minutes=1),
        )
    )
    db.commit()

    class MetadataReadbackClient:
        posts: list[tuple[str, str]] = []
        gets: list[str] = []
        now_xml = (
            '<nowPlaying source="LOCAL_INTERNET_RADIO">'
            "<playStatus>PLAY_STATE</playStatus>"
            "<stationName>Station A</stationName>"
            "<Track>Fresh track</Track><Artist>Fresh artist</Artist>"
            "<Album>Fresh album</Album>"
            "<ImageURL>https://art.example/fresh.png?token=removed</ImageURL>"
            '<ContentItem source="LOCAL_INTERNET_RADIO" '
            'location="https://provider.example/now-playing/station/station-a"/>'
            "</nowPlaying>"
        )

        def __init__(self, _ip: str):
            pass

        async def get_xml(self, endpoint: str) -> str:
            self.gets.append(endpoint)
            if endpoint == "/now_playing":
                return self.now_xml
            if endpoint == "/volume":
                return "<volume><actualvolume>1</actualvolume></volume>"
            raise AssertionError(endpoint)

        async def post_xml(self, endpoint: str, body: str) -> str:
            self.posts.append((endpoint, body))
            raise AssertionError("metadata ingestion must never write")

    async def wait_for_track(value: str) -> MetadataState:
        for _ in range(50):
            current_db = app_db.SessionLocal()
            try:
                row = (
                    current_db.query(MetadataState)
                    .filter(MetadataState.device_id == device.device_id)
                    .one()
                )
                if row.track == value:
                    current_db.expunge(row)
                    return row
            finally:
                current_db.close()
            await asyncio.sleep(0)
        raise AssertionError(f"metadata track {value!r} was not persisted")

    async def wait_for_explicit_clear() -> MetadataState:
        for _ in range(50):
            current_db = app_db.SessionLocal()
            try:
                row = (
                    current_db.query(MetadataState)
                    .filter(MetadataState.device_id == device.device_id)
                    .one()
                )
                if all(
                    value is None
                    for value in (
                        row.track,
                        row.artist,
                        row.album,
                        row.artwork_url,
                    )
                ):
                    current_db.expunge(row)
                    return row
            finally:
                current_db.close()
            await asyncio.sleep(0)
        raise AssertionError("explicit empty metadata was not persisted")

    async def scenario() -> tuple[
        dict, MetadataState, dict, MetadataState, dict, MetadataState
    ]:
        runtime = ResearchRuntime(
            lambda: app_db.SessionLocal(),
            metadata_coalesce_seconds=0,
            clock=lambda: clock[0],
        )
        runtime.enable_event_tasks()
        first_result = await run_playback_keepalive_for_device(
            device,
            db,
            now=clock[0],
            client_factory=MetadataReadbackClient,
            research_runtime=runtime,
        )
        first_metadata = await wait_for_track("Fresh track")

        clock[0] += timedelta(seconds=301)
        MetadataReadbackClient.now_xml = (
            '<nowPlaying source="LOCAL_INTERNET_RADIO">'
            "<playStatus>PLAY_STATE</playStatus>"
            "<stationName>Station B</stationName><track>Next track</track>"
            '<ContentItem source="LOCAL_INTERNET_RADIO" '
            'location="https://provider.example/now-playing/station/station-b"/>'
            "</nowPlaying>"
        )
        second_result = await run_playback_keepalive_for_device(
            device,
            db,
            now=clock[0],
            client_factory=MetadataReadbackClient,
            research_runtime=runtime,
        )
        second_metadata = await wait_for_track("Next track")

        clock[0] += timedelta(seconds=301)
        MetadataReadbackClient.now_xml = (
            '<nowPlaying source="LOCAL_INTERNET_RADIO">'
            "<playStatus>PLAY_STATE</playStatus>"
            "<stationName>Station B</stationName>"
            "<track></track><artist/><album/><art/>"
            '<ContentItem source="LOCAL_INTERNET_RADIO" '
            'location="https://provider.example/now-playing/station/station-b"/>'
            "</nowPlaying>"
        )
        third_result = await run_playback_keepalive_for_device(
            device,
            db,
            now=clock[0],
            client_factory=MetadataReadbackClient,
            research_runtime=runtime,
        )
        cleared_metadata = await wait_for_explicit_clear()
        await runtime.shutdown()
        return (
            first_result,
            first_metadata,
            second_result,
            second_metadata,
            third_result,
            cleared_metadata,
        )

    (
        first_result,
        first_metadata,
        second_result,
        second_metadata,
        third_result,
        cleared_metadata,
    ) = asyncio.run(scenario())

    assert first_result["metadata_ingested"] is True
    assert first_metadata.station_id == "station-a"
    assert first_metadata.track == "Fresh track"
    assert first_metadata.artist == "Fresh artist"
    assert first_metadata.album == "Fresh album"
    assert first_metadata.artwork_url == "https://art.example/fresh.png"
    assert first_metadata.provenance == "RADIO"
    assert second_result["metadata_ingested"] is True
    assert second_metadata.station_id == "station-b"
    assert second_metadata.track == "Next track"
    assert second_metadata.artist is None
    assert second_metadata.album is None
    assert second_metadata.artwork_url is None
    assert third_result["metadata_ingested"] is True
    assert cleared_metadata.station_id == "station-b"
    assert cleared_metadata.track is None
    assert cleared_metadata.artist is None
    assert cleared_metadata.album is None
    assert cleared_metadata.artwork_url is None
    assert MetadataReadbackClient.gets == [
        "/now_playing",
        "/volume",
        "/now_playing",
        "/volume",
        "/now_playing",
        "/volume",
    ]
    assert MetadataReadbackClient.posts == []
    db.close()
