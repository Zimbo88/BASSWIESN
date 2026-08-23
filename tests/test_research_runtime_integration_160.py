from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import httpx
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.config import get_settings
from basswiesn.app.models import (
    AirPlayReadinessState,
    ArtworkCacheEntry,
    Device,
    DeviceCapabilitiesState,
    DiagnosticEvent,
    MetadataState,
    PlaybackHealthState,
    ReportingState,
    RestrictionState,
)
from basswiesn.app.services.device_state import save_runtime_state
from basswiesn.app.services.metadata_engine import MetadataCoalescer, MetadataSnapshot
from basswiesn.app.services.playback_keepalive import run_playback_keepalive_for_device
from basswiesn.app.services.recovery import (
    RecoveryCoordinator,
    RecoveryReason,
    RecoveryStage,
    RecoveryStatus,
    plan_recovery,
)
from basswiesn.app.services.reporting_scheduler import (
    ReportPayload,
    ReportingScheduler,
)
from basswiesn.app.services.reporting_store import (
    SqlAlchemyReportingStore,
    reporting_session_key,
)
from basswiesn.app.services.research_runtime import (
    ProviderContractMode,
    ResearchRuntime,
)
from basswiesn.app.services.research_state_retention import apply_research_retention


pytestmark = pytest.mark.integration
OBSERVED = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(self, body: dict):
        self.status_code = 200
        self._body = body

    def json(self) -> dict:
        return self._body


def test_reporting_task_replans_after_dynamic_next_report_in_without_poll_loop():
    calls: list[dict] = []

    async def scenario() -> None:
        clock = [OBSERVED]

        async def virtual_sleep(seconds: float) -> None:
            clock[0] += timedelta(seconds=seconds)
            await asyncio.sleep(0)

        async def post(_url: str, payload: dict) -> FakeResponse:
            calls.append(payload)
            return FakeResponse({"nextReportIn": 1 if len(calls) == 1 else 0})

        scheduler = ReportingScheduler(post, sleep=virtual_sleep)
        await scheduler.enqueue(
            "radio:provider",
            ReportPayload(OBSERVED.isoformat(), "start"),
            report_url="https://provider.example/report",
            due_at=OBSERVED,
        )
        scheduler.schedule_due("radio:provider", now=lambda: clock[0])
        for _ in range(20):
            if len(calls) == 2:
                break
            await asyncio.sleep(0)
        assert scheduler.session("radio:provider").next_due_at is None
        await scheduler.shutdown()

    asyncio.run(scenario())
    assert [payload["eventType"] for payload in calls] == ["start", "timed"]
    assert calls[1]["timeStamp"] == (OBSERVED + timedelta(seconds=1)).isoformat()


def test_metadata_coalescer_keeps_update_submitted_while_publish_is_in_flight():
    published: list[tuple[str | None, tuple[str, ...]]] = []

    async def scenario() -> None:
        coalescer = MetadataCoalescer(delay_seconds=0)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def publish(snapshot: MetadataSnapshot, changed: tuple[str, ...]) -> None:
            if snapshot.track == "A":
                entered.set()
                await release.wait()
            published.append((snapshot.track, changed))

        coalescer.submit("radio", MetadataSnapshot(track="A"), ("track",), publish)
        await entered.wait()
        coalescer.submit(
            "radio",
            MetadataSnapshot(track="B", image_url="art"),
            ("track", "image_url"),
            publish,
        )
        release.set()
        for _ in range(20):
            if len(published) == 2:
                break
            await asyncio.sleep(0)
        await coalescer.shutdown()

    asyncio.run(scenario())
    assert published == [
        ("A", ("track",)),
        ("B", ("image_url", "track")),
    ]


def test_recovery_is_single_flight_and_rechecks_cooldown_after_completion():
    calls: list[int] = []

    async def scenario() -> None:
        coordinator = RecoveryCoordinator(cooldown_seconds=300)
        entered = asyncio.Event()
        release = asyncio.Event()
        plan = plan_recovery(reason=RecoveryReason.UNKNOWN)

        async def readback() -> dict:
            calls.append(1)
            entered.set()
            await release.wait()
            return {"ok": True}

        async def recovered(_stage, result) -> bool:
            return bool(result and result.get("ok"))

        first_task = asyncio.create_task(
            coordinator.execute(
                "radio", plan, actions={RecoveryStage.READBACK: readback}, recovered=recovered
            )
        )
        await entered.wait()
        duplicate = await coordinator.execute(
            "radio", plan, actions={RecoveryStage.READBACK: readback}, recovered=recovered
        )
        assert duplicate is coordinator.active("radio")
        release.set()
        first = await first_task
        assert first.status == RecoveryStatus.RECOVERED
        cooldown = await coordinator.execute(
            "radio", plan, actions={RecoveryStage.READBACK: readback}, recovered=recovered
        )
        assert cooldown.status == RecoveryStatus.COOLDOWN

    asyncio.run(scenario())
    assert calls == [1]


def test_automatic_recovery_never_reselects_or_stops_radio():
    plan = plan_recovery(
        reason=RecoveryReason.SOURCE_INVALID,
        requested_max_stage=RecoveryStage.CONTROLLED_STOP_PLAY,
        automatic=True,
    )
    assert plan.effective_max_stage == RecoveryStage.STREAM_RERESOLVE
    assert RecoveryStage.SAME_SOURCE_RESELECT not in plan.stages
    assert RecoveryStage.CONTROLLED_STOP_PLAY not in plan.stages


def test_reporting_hostname_resolving_to_protected_radio_never_opens_transport(monkeypatch):
    from basswiesn.app import config

    protected_test_ip = "192.0.2.25"
    monkeypatch.setenv("PROTECTED_DEVICE_IPS", protected_test_ip)
    config.get_settings.cache_clear()
    transport_calls: list[str] = []

    async def resolver(_hostname: str, _port: int) -> tuple[str, ...]:
        return (protected_test_ip,)

    async def handler(request: httpx.Request) -> httpx.Response:
        transport_calls.append(str(request.url))
        return httpx.Response(200, json={})

    async def scenario() -> None:
        runtime = ResearchRuntime(
            lambda: app_db.SessionLocal(),
            report_resolver=resolver,
            reporting_transport=httpx.MockTransport(handler),
        )
        with pytest.raises(PermissionError, match="protected device"):
            await runtime._post_report("https://radio-alias.example/report", {"eventType": "timed"})

    try:
        asyncio.run(scenario())
        assert transport_calls == []
    finally:
        config.get_settings.cache_clear()


def test_disabled_runtime_does_not_create_metadata_background_task():
    device_id = "NO-BACKGROUND-RUNTIME"
    db = app_db.SessionLocal()
    db.add(
        MetadataState(
            device_id=device_id,
            station_id="station",
            provenance="PROVIDER",
            updated_at=OBSERVED,
            stale=False,
        )
    )
    db.commit()
    db.close()

    async def scenario() -> None:
        runtime = ResearchRuntime(lambda: app_db.SessionLocal(), clock=lambda: OBSERVED)
        assert runtime.schedule_metadata_staleness(device_id) is False
        assert runtime.metadata._tasks == {}
        await runtime.shutdown()

    asyncio.run(scenario())


def test_retention_removes_only_files_inside_artwork_cache(tmp_path: Path):
    cache_root = tmp_path / "data" / "media" / "artwork-cache"
    cache_root.mkdir(parents=True)
    cached = cache_root / "expired.png"
    cached.write_bytes(b"png")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep")
    symlink = cache_root / "outside-link.png"
    symlink.symlink_to(outside)

    db = app_db.SessionLocal()
    old = OBSERVED - timedelta(days=8)
    db.add_all(
        [
            ArtworkCacheEntry(
                cache_key="inside-expired",
                cached_path=str(cached),
                expires_at=old,
            ),
            ArtworkCacheEntry(
                cache_key="outside-expired",
                cached_path=str(outside),
                expires_at=old,
            ),
            ArtworkCacheEntry(
                cache_key="symlink-expired",
                cached_path=str(symlink),
                expires_at=old,
            ),
        ]
    )
    db.commit()

    result = apply_research_retention(
        db,
        now=OBSERVED,
        artwork_cache_root=cache_root,
    )
    remaining = {
        row.cache_key for row in db.query(ArtworkCacheEntry).order_by(ArtworkCacheEntry.id).all()
    }
    db.close()

    assert cached.exists() is False
    assert outside.read_bytes() == b"keep"
    assert symlink.is_symlink()
    assert remaining == {"outside-expired", "symlink-expired"}
    assert result["artwork_entries"] == 1
    assert result["artwork_files_deleted"] == 1
    assert result["artwork_files_blocked"] == 2


def test_runtime_rehydrates_locally_marks_metadata_stale_and_runs_retention():
    now = OBSERVED
    device_id = "RUNTIME160"
    db = app_db.SessionLocal()
    device = Device(
        device_id=device_id,
        name="Runtime",
        ip_address="192.0.2.160",
        firmware="27.0.6",
        info_xml=(
            '<info deviceID="RUNTIME160"><productID>0x093b</productID>'
            "<softwareVersion>27.0.6</softwareVersion></info>"
        ),
    )
    db.add(device)
    db.add(
        DeviceCapabilitiesState(
            device_id=device.device_id,
            sources_json='["AIRPLAY", "LOCAL_INTERNET_RADIO"]',
            supports_airplay=True,
            observed_at=now - timedelta(minutes=1),
        )
    )
    db.add(
        MetadataState(
            device_id=device.device_id,
            station_id="runtime-station",
            track="Old title",
            provider="LOCAL_INTERNET_RADIO",
            source="LOCAL_INTERNET_RADIO",
            provenance="PROVIDER",
            confidence=90,
            stale=False,
            updated_at=now - timedelta(seconds=301),
        )
    )
    db.add(
        DiagnosticEvent(
            event_id="expired-runtime-event",
            device_id=device.device_id,
            domain="TEST",
            code="EXPIRED",
            occurred_at=now - timedelta(days=31),
        )
    )
    db.commit()
    db.close()

    posted: list[str] = []
    store = SqlAlchemyReportingStore(lambda: app_db.SessionLocal())

    async def prepare_reporting() -> None:
        async def forbidden_post(url, _payload):
            posted.append(url)
            raise AssertionError("startup must not replay a report")

        scheduler = ReportingScheduler(forbidden_post, store=store)
        await scheduler.enqueue(
            reporting_session_key(device_id, "LOCAL_INTERNET_RADIO"),
            ReportPayload(now.isoformat(), "start"),
            report_url="https://provider.example/report?token=not-persisted",
            due_at=now - timedelta(seconds=1),
            item_id="restart-report",
        )

    asyncio.run(prepare_reporting())

    async def scenario() -> None:
        async def forbidden_post(url, _payload):
            posted.append(url)
            raise AssertionError("rehydration must wait for a fresh dynamic link")

        runtime = ResearchRuntime(
            lambda: app_db.SessionLocal(),
            reporting_post=forbidden_post,
            clock=lambda: now,
        )
        await runtime.start()
        for _ in range(10):
            await asyncio.sleep(0)
        assert runtime.reporting.session(
            reporting_session_key(device_id, "LOCAL_INTERNET_RADIO")
        ).report_url is None
        await runtime.shutdown()

    asyncio.run(scenario())
    db = app_db.SessionLocal()
    metadata = db.query(MetadataState).filter(MetadataState.device_id == device_id).one()
    readiness = (
        db.query(AirPlayReadinessState)
        .filter(AirPlayReadinessState.device_id == device_id)
        .one()
    )
    reporting = db.query(ReportingState).filter(ReportingState.device_id == device_id).one()
    expired = db.query(DiagnosticEvent).filter(DiagnosticEvent.event_id == "expired-runtime-event").one_or_none()
    db.close()

    assert metadata.stale is True
    assert readiness.product_allowed is True
    assert readiness.source_visible is True
    assert readiness.blocking_stage == "AUTH_HARDWARE"
    assert reporting.state == "DEGRADED"
    assert posted == []
    assert expired is None


def test_external_provider_selection_runs_initial_retry_and_ingests_report_metadata():
    device_id = "EXTERNAL-CONTRACT-160"
    db = app_db.SessionLocal()
    db.add(
        Device(
            device_id=device_id,
            name="External contract fake",
            ip_address="192.0.2.212",
        )
    )
    db.commit()
    db.close()
    calls: list[tuple[str, dict]] = []

    async def scenario() -> None:
        clock = [OBSERVED]

        async def virtual_sleep(seconds: float) -> None:
            clock[0] += timedelta(seconds=seconds)
            await asyncio.sleep(0)

        async def post(url: str, payload: dict) -> FakeResponse:
            calls.append((url, payload))
            if len(calls) == 1:
                response = FakeResponse({})
                response.status_code = 503
                return response
            return FakeResponse(
                {
                    "nextReportIn": 0,
                    "_links": {
                        "bmx_reporting": {
                            "href": "https://provider.example/report/fresh?opaque=runtime"
                        }
                    },
                    "_embedded": {
                        "bmx_nowplaying": {
                            "track": "Report title",
                            "artist": "Report artist",
                            "album": "Report album",
                            "imageUrl": "https://art.example/report.jpg?token=removed",
                        }
                    },
                }
            )

        runtime = ResearchRuntime(
            lambda: app_db.SessionLocal(),
            reporting_post=post,
            reporting_backoff_seconds=(1, 2, 3, 4, 5),
            reporting_sleep=virtual_sleep,
            metadata_coalesce_seconds=0,
            clock=lambda: clock[0],
        )
        runtime.enable_event_tasks()
        session = await runtime.observe_external_provider_selection_response(
            device_id,
            "EXTERNAL_PROVIDER",
            "station-160",
            {
                "_links": {
                    "bmx_reporting": {
                        "href": "https://provider.example/report/initial?opaque=runtime"
                    }
                },
                "_embedded": {
                    "bmx_nowplaying": {
                        "track": "Initial title",
                        "artist": "Initial artist",
                    }
                },
            },
            provider_origin="https://provider.example/provider/station-160",
            mode=ProviderContractMode.SELECTION_START,
        )
        assert session.queue_depth == 1
        for _ in range(100):
            db_poll = app_db.SessionLocal()
            try:
                row = (
                    db_poll.query(MetadataState)
                    .filter(MetadataState.device_id == device_id)
                    .one_or_none()
                )
                complete = (
                    len(calls) == 2
                    and row is not None
                    and row.track == "Report title"
                )
            finally:
                db_poll.close()
            if complete:
                break
            await asyncio.sleep(0)
        reporting_session = runtime.reporting.session(
            reporting_session_key(device_id, "EXTERNAL_PROVIDER")
        )
        assert reporting_session.status.value == "RECOVERED"
        assert reporting_session.report_url == (
            "https://provider.example/report/fresh?opaque=runtime"
        )
        await runtime.shutdown()

    asyncio.run(scenario())
    db = app_db.SessionLocal()
    reporting = (
        db.query(ReportingState)
        .filter(
            ReportingState.device_id == device_id,
            ReportingState.provider_id == "EXTERNAL_PROVIDER",
        )
        .one()
    )
    metadata = (
        db.query(MetadataState)
        .filter(MetadataState.device_id == device_id)
        .one()
    )
    db.close()

    assert len(calls) == 2
    assert all(url.startswith("https://provider.example/report/initial") for url, _ in calls)
    assert [payload["eventType"] for _, payload in calls] == ["start", "start"]
    assert reporting.state == "RECOVERED"
    assert reporting.retry_count == 0
    assert metadata.station_id == "station-160"
    assert metadata.provider == "EXTERNAL_PROVIDER"
    assert metadata.track == "Report title"
    assert metadata.artist == "Report artist"
    assert metadata.album == "Report album"
    assert metadata.artwork_url == "https://art.example/report.jpg"


def test_local_orion_reporting_link_is_never_scheduled_as_outbound_work():
    device_id = "LOCAL-INBOUND-ONLY-160"
    db = app_db.SessionLocal()
    db.add(Device(device_id=device_id, name="Local provider fake", ip_address="192.0.2.215"))
    db.commit()
    db.close()
    calls: list[tuple[str, dict]] = []

    async def scenario() -> None:
        async def post(url: str, payload: dict) -> FakeResponse:
            calls.append((url, payload))
            return FakeResponse({})

        runtime = ResearchRuntime(
            lambda: app_db.SessionLocal(), reporting_post=post
        )
        runtime.enable_event_tasks()
        settings = get_settings()
        local_origin = settings.local_base_url.rstrip("/") or (
            f"http://127.0.0.1:{settings.cloud_port}"
        )
        with pytest.raises(ValueError, match="inbound-only"):
            await runtime.observe_external_provider_selection_response(
                device_id,
                "LOCAL_INTERNET_RADIO",
                "station-local",
                {
                    "_links": {
                        "bmx_reporting": {
                            "href": f"{local_origin}/bmx/orion/reporting/station/station-local"
                        }
                    }
                },
                provider_origin=f"{local_origin}/bmx/orion/station/station-local",
                mode=ProviderContractMode.SELECTION_START,
            )
        await runtime.shutdown()

    asyncio.run(scenario())
    assert calls == []


def test_provider_now_playing_coalesces_partial_updates_and_drops_old_selection():
    device_id = "METADATA-CONTRACT-160"
    db = app_db.SessionLocal()
    db.add(Device(device_id=device_id, name="Metadata fake", ip_address="192.0.2.213"))
    db.commit()
    db.close()

    async def scenario() -> None:
        runtime = ResearchRuntime(
            lambda: app_db.SessionLocal(),
            metadata_coalesce_seconds=0.01,
            clock=lambda: OBSERVED,
        )
        runtime.enable_event_tasks()
        first = await runtime.observe_provider_now_playing_response(
            device_id,
            "EXTERNAL_PROVIDER",
            "station-current",
            {"track": "Current track", "askAgainAfter": 5},
        )
        second = await runtime.observe_provider_now_playing_response(
            device_id,
            "EXTERNAL_PROVIDER",
            "station-current",
            {
                "artist": "Current artist",
                "album": "Current album",
                "imageUrl": "https://art.example/current.png",
                "askAgainAfter": 5,
            },
        )
        stale = await runtime.observe_provider_now_playing_response(
            device_id,
            "EXTERNAL_PROVIDER",
            "station-old",
            {"track": "Must not win"},
        )
        assert first is not None
        assert second is not None
        assert second.track == "Current track"
        assert stale is None
        await asyncio.sleep(0.03)
        await runtime.shutdown()

    asyncio.run(scenario())
    db = app_db.SessionLocal()
    metadata = (
        db.query(MetadataState)
        .filter(MetadataState.device_id == device_id)
        .one()
    )
    db.close()

    assert metadata.station_id == "station-current"
    assert metadata.track == "Current track"
    assert metadata.artist == "Current artist"
    assert metadata.album == "Current album"
    assert metadata.artwork_url == "https://art.example/current.png"


def test_restart_reporting_waits_for_explicit_external_link_reacquire():
    device_id = "REPORT-REACQUIRE-160"
    db = app_db.SessionLocal()
    db.add(Device(device_id=device_id, name="Reporting fake", ip_address="192.0.2.214"))
    db.commit()
    db.close()
    store = SqlAlchemyReportingStore(lambda: app_db.SessionLocal())

    async def persist() -> None:
        scheduler = ReportingScheduler(lambda _url, _payload: None, store=store)
        await scheduler.enqueue(
            reporting_session_key(device_id, "EXTERNAL_PROVIDER"),
            ReportPayload(OBSERVED.isoformat(), "timed"),
            report_url="https://provider.example/report/expired?secret=not-persisted",
            due_at=OBSERVED,
            item_id="restart-timed-report",
        )

    asyncio.run(persist())
    calls: list[tuple[str, dict]] = []

    async def scenario() -> None:
        async def post(url: str, payload: dict) -> FakeResponse:
            calls.append((url, payload))
            return FakeResponse({"nextReportIn": 0})

        async def virtual_sleep(_seconds: float) -> None:
            await asyncio.sleep(0)

        runtime = ResearchRuntime(
            lambda: app_db.SessionLocal(),
            reporting_post=post,
            reporting_sleep=virtual_sleep,
            clock=lambda: OBSERVED,
        )
        await runtime.start()
        for _ in range(5):
            await asyncio.sleep(0)
        assert calls == []
        await runtime.observe_external_provider_selection_response(
            device_id,
            "EXTERNAL_PROVIDER",
            "station-after-restart",
            {
                "_links": {
                    "bmx_reporting": {
                        "href": "https://provider.example/report/reacquired?fresh=runtime"
                    }
                }
            },
            provider_origin="https://provider.example/provider/station-after-restart",
            mode=ProviderContractMode.RESTART_REACQUIRE,
        )
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0)
        await runtime.shutdown()

    asyncio.run(scenario())

    assert calls == [
        (
            "https://provider.example/report/reacquired?fresh=runtime",
            ReportPayload(OBSERVED.isoformat(), "timed").as_dict(),
        )
    ]


class InvalidSourceClient:
    posts: list[tuple[str, str]] = []

    def __init__(self, _ip: str):
        pass

    async def get_xml(self, endpoint: str) -> str:
        if endpoint == "/now_playing":
            return '<nowPlaying source="INVALID_SOURCE"><playStatus>STOP_STATE</playStatus></nowPlaying>'
        if endpoint == "/volume":
            return "<volume><actualvolume>1</actualvolume></volume>"
        raise AssertionError(endpoint)

    async def post_xml(self, endpoint: str, body: str) -> str:
        self.posts.append((endpoint, body))
        raise AssertionError("INVALID_SOURCE diagnosis must not write")


@pytest.mark.parametrize(
    ("selected_timeout", "expected_state", "expected_cause", "expected_confidence"),
    [
        (0, "DISABLED", "UNKNOWN", 0),
        (300, "EXPIRED", "INACTIVITY_TIMEOUT", 96),
    ],
)
def test_restriction_identity_selects_current_station_among_two_for_invalid_source(
    selected_timeout: int,
    expected_state: str,
    expected_cause: str,
    expected_confidence: int,
):
    now = OBSERVED
    db = app_db.SessionLocal()
    device = Device(device_id=f"TWOSTATION{selected_timeout}", ip_address="192.0.2.211")
    db.add(device)
    db.commit()
    save_runtime_state(
        db,
        device.device_id,
        {
            "playback_keepalive": {
                "playing": True,
                "last_source": "LOCAL_INTERNET_RADIO",
                "playback_started_at": (now - timedelta(hours=1)).isoformat(),
            }
        },
    )
    db.add(
        MetadataState(
            device_id=device.device_id,
            station_id="station-b",
            provider="LOCAL_INTERNET_RADIO",
            source="LOCAL_INTERNET_RADIO",
            provenance="PROVIDER",
            updated_at=now - timedelta(minutes=5),
            stale=False,
        )
    )
    db.add_all(
        [
            RestrictionState(
                device_id=device.device_id,
                source_key="LOCAL_INTERNET_RADIO:station-a",
                inactivity_timeout_s=1,
                timer_enabled=True,
                received_at=now - timedelta(hours=1),
                timer_started_at=now - timedelta(hours=1),
                effective_until=now - timedelta(minutes=59),
                origin="SERVER_RESPONSE",
            ),
            RestrictionState(
                device_id=device.device_id,
                source_key="LOCAL_INTERNET_RADIO:station-b",
                inactivity_timeout_s=selected_timeout,
                timer_enabled=selected_timeout > 0,
                received_at=now - timedelta(minutes=10),
                timer_started_at=(
                    now - timedelta(minutes=10) if selected_timeout > 0 else None
                ),
                effective_until=(
                    now - timedelta(minutes=5) if selected_timeout > 0 else None
                ),
                origin="SERVER_RESPONSE",
            ),
        ]
    )
    db.commit()
    InvalidSourceClient.posts = []

    result = asyncio.run(
        run_playback_keepalive_for_device(
            device, db, now=now, client_factory=InvalidSourceClient
        )
    )
    health = (
        db.query(PlaybackHealthState)
        .filter(PlaybackHealthState.device_id == device.device_id)
        .one()
    )
    invalid_event = (
        db.query(DiagnosticEvent)
        .filter(
            DiagnosticEvent.device_id == device.device_id,
            DiagnosticEvent.code == "INVALID_SOURCE_OBSERVED",
        )
        .one()
    )
    evidence = json.loads(invalid_event.evidence_json)
    db.close()

    assert result["restriction"]["source_key"] == "LOCAL_INTERNET_RADIO:station-b"
    assert result["restriction"]["identity_source"] == "metadata.station_id"
    assert result["restriction"]["state"] == expected_state
    assert result["invalid_source_cause"] == expected_cause
    assert result["invalid_source_confidence"] == expected_confidence
    assert health.state == "FAILED"
    assert health.reason == "AUTHORITATIVE_INVALID_SOURCE"
    assert any(
        item.get("type") == "INVALID_SOURCE_DIAGNOSIS"
        and item.get("cause") == expected_cause
        and item.get("confidence") == expected_confidence
        for item in evidence
    )
    assert InvalidSourceClient.posts == []
