from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from basswiesn.app.services.airplay_readiness import (
    AirPlayBlockingStage,
    AirPlayReadinessLabel,
    assess_airplay_readiness,
    normalize_product_id,
)
from basswiesn.app.services.health_models import (
    InvalidSourceCause,
    MetadataHealth,
    PlaybackHealth,
    PlaybackSignals,
    ProviderHealth,
    ProviderSignals,
    ReportingHealth,
    StreamHealth,
    classify_invalid_source,
    reduce_playback_health,
    reduce_provider_health,
)
from basswiesn.app.services.metadata_engine import (
    ClockMetadataMode,
    MetadataProvenance,
    MetadataCoalescer,
    MetadataScheduler,
    MetadataSnapshot,
    clock_display_projection,
    metadata_changes,
    normalize_metadata,
)
from basswiesn.app.services.recovery import (
    RecoveryCoordinator,
    RecoveryReason,
    RecoveryStage,
    RecoveryStatus,
    plan_recovery,
)
from basswiesn.app.services.reporting_scheduler import (
    REPORT_MAX_ATTEMPTS,
    REPORT_QUEUE_CAPACITY,
    ReportPayload,
    ReportingQueueFull,
    ReportingScheduler,
    ReportingStatus,
    redact_report_url,
)
from basswiesn.app.services.restrictions import (
    UINT64_MAX,
    RestrictionParseError,
    parse_restrictions,
)


pytestmark = pytest.mark.unit


OBSERVED = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("payload", "value", "enabled", "origin"),
    [
        ({"name": "No restrictions"}, None, False, "ABSENT"),
        ({"restrictions": {}}, None, False, "ABSENT"),
        ({"restrictions": {"inactivityTimeout": 0}}, 0, False, "SERVER_RESPONSE"),
        ({"restrictions": {"inactivityTimeout": 1}}, 1, True, "SERVER_RESPONSE"),
        ({"restrictions": {"inactivityTimeout": 300}}, 300, True, "SERVER_RESPONSE"),
        ({"restrictions": {"inactivityTimeout": 21600}}, 21600, True, "SERVER_RESPONSE"),
        ({"restrictions": {"inactivityTimeout": UINT64_MAX}}, UINT64_MAX, True, "SERVER_RESPONSE"),
    ],
)
def test_restrictions_contract(payload, value, enabled, origin):
    parsed = parse_restrictions(payload, received_at=OBSERVED, source="station")

    assert parsed.inactivity_timeout_s == value
    assert parsed.timer_enabled is enabled
    assert parsed.origin == origin
    assert parsed.source == "station"
    # The response configures the timeout. Only a later radio Play readback
    # starts it, so receipt time is never used as the deadline anchor.
    assert parsed.timer_started_at is None
    assert parsed.effective_until is None


@pytest.mark.parametrize("value", [-1, UINT64_MAX + 1, 1.5, "invalid", True, None])
def test_restrictions_reject_invalid_values(value):
    with pytest.raises(RestrictionParseError, match="inactivityTimeout"):
        parse_restrictions({"restrictions": {"inactivityTimeout": value}})


@pytest.mark.parametrize("payload", ["{", "[]", None, {"restrictions": []}])
def test_restrictions_reject_malformed_provider_response(payload):
    with pytest.raises(RestrictionParseError):
        parse_restrictions(payload)


class FakeResponse:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


def test_reporting_success_uses_exact_post_contract_and_server_due_time():
    calls = []

    async def scenario():
        async def post(url, payload):
            calls.append((url, payload))
            return FakeResponse(
                200,
                {
                    "nextReportIn": 300,
                    "_links": {"bmx_reporting": {"href": "http://provider.test/report/next?secret=x"}},
                    "_embedded": {"bmx_nowplaying": {"track": "B"}},
                },
            )

        scheduler = ReportingScheduler(post)
        payload = ReportPayload(
            timeStamp="2030-01-01T00:00:00",
            eventType="timed",
            timeIntoTrack=120,
        )
        await scheduler.enqueue(
            "radio:provider", payload, report_url="http://provider.test/report", due_at=OBSERVED
        )
        result = await scheduler.process_due("radio:provider", now=OBSERVED)
        session = scheduler.session("radio:provider")
        assert result.status == ReportingStatus.SUCCESS
        assert result.queue_depth == 0
        assert result.next_due_at == OBSERVED + timedelta(seconds=300)
        assert result.embedded_now_playing == {"track": "B"}
        assert result.playback_action == "NONE"
        assert session.report_url == "http://provider.test/report/next?secret=x"

    asyncio.run(scenario())
    assert calls == [
        (
            "http://provider.test/report",
            {
                "timeStamp": "2030-01-01T00:00:00",
                "eventType": "timed",
                "reason": "",
                "timeIntoTrack": 120,
                "playbackDelay": 0,
                "absolutePlayPoint": "",
                "reasonSubCode": "",
            },
        )
    ]


def test_next_report_in_materializes_one_timed_report_when_due():
    calls = []

    async def scenario():
        async def post(url, payload):
            calls.append((url, payload))
            return FakeResponse(200, {"nextReportIn": 10 if len(calls) == 1 else 0})

        scheduler = ReportingScheduler(post)
        await scheduler.enqueue(
            "radio:provider",
            ReportPayload("2030-01-01T00:00:00+00:00", "start"),
            report_url="http://provider.test/report",
            due_at=OBSERVED,
        )
        first = await scheduler.process_due("radio:provider", now=OBSERVED)
        assert first.queue_depth == 0
        assert first.next_due_at == OBSERVED + timedelta(seconds=10)
        waiting = await scheduler.process_due(
            "radio:provider", now=OBSERVED + timedelta(seconds=9)
        )
        assert waiting.queue_depth == 0
        second = await scheduler.process_due(
            "radio:provider", now=OBSERVED + timedelta(seconds=10)
        )
        assert second.status == ReportingStatus.SUCCESS
        assert second.next_due_at is None

    asyncio.run(scenario())
    assert len(calls) == 2
    assert calls[1][1]["eventType"] == "timed"
    assert calls[1][1]["timeStamp"] == "2030-01-01T00:00:10+00:00"


def test_reporting_initial_send_plus_five_retries_then_fails_without_playback_action():
    calls = []

    async def scenario():
        async def post(url, payload):
            calls.append((url, payload))
            return FakeResponse(503)

        scheduler = ReportingScheduler(post, backoff_seconds=(1, 2, 3, 4, 5))
        await scheduler.enqueue(
            "session", ReportPayload("2030-01-01T00:00:00", "start"),
            report_url="http://provider.test/report", due_at=OBSERVED,
        )
        now = OBSERVED
        result = None
        for _ in range(REPORT_MAX_ATTEMPTS):
            result = await scheduler.process_due("session", now=now)
            if result.next_due_at:
                now = result.next_due_at
        assert result is not None
        assert result.status == ReportingStatus.FAILED
        assert result.queue_depth == 0
        assert result.playback_action == "NONE"

    asyncio.run(scenario())
    assert len(calls) == 6


def test_reporting_queue_capacity_fails_closed_without_invented_eviction():
    async def scenario():
        async def post(url, payload):
            return FakeResponse(200)

        scheduler = ReportingScheduler(post)
        for index in range(REPORT_QUEUE_CAPACITY):
            await scheduler.enqueue(
                "session",
                ReportPayload("2030-01-01T00:00:00", f"timed-{index}"),
                report_url="http://provider.test/report",
            )
        with pytest.raises(ReportingQueueFull):
            await scheduler.enqueue(
                "session",
                ReportPayload("2030-01-01T00:00:00", "overflow"),
            )
        assert scheduler.session("session").queue_depth == 20
        assert scheduler.session("session").status == ReportingStatus.DEGRADED

    asyncio.run(scenario())


def test_reporting_url_rejects_credentials_and_redaction_drops_query():
    async def scenario():
        scheduler = ReportingScheduler(lambda _url, _payload: None)
        with pytest.raises(ValueError):
            await scheduler.enqueue(
                "session",
                ReportPayload("2030-01-01T00:00:00", "start"),
                report_url="https://user:secret@example.test/report",
            )

    asyncio.run(scenario())
    assert redact_report_url("https://user:secret@example.test/report?token=x") == "https://example.test/report"


def test_metadata_updates_only_runtime_fields_and_never_selection_identity():
    before = MetadataSnapshot(
        station_name="Station",
        station_id="42",
        track="A",
        artist="Artist",
        provider="ORION",
        source="LOCAL_INTERNET_RADIO",
        updated_at=OBSERVED,
        stale=False,
    )
    after = normalize_metadata(
        {
            "track": "B",
            "artist": "Artist",
            "album": "Album",
            "imageUrl": "https://example.invalid/art.png",
            "stationName": "Must not replace selection",
            "source": "Must not replace selection",
            "askAgainAfter": 1,
        },
        previous=before,
        observed_at=OBSERVED + timedelta(seconds=1),
    )

    assert after.station_name == "Station"
    assert after.station_id == "42"
    assert after.source == "LOCAL_INTERNET_RADIO"
    assert after.provider == "ORION"
    assert metadata_changes(before, after) == ("track", "album", "image_url")
    assert after.next_due_at == OBSERVED + timedelta(seconds=6)  # 1 s observed + 5 s floor


def test_metadata_scheduler_has_no_playback_control_path():
    events = []

    async def scenario():
        scheduler = MetadataScheduler()
        before = MetadataSnapshot(
            station_name="Station", track="A", updated_at=OBSERVED,
            provenance=MetadataProvenance.PROVIDER, stale=False,
        )

        async def fetch():
            events.append("fetch")
            return {"track": "B", "artist": "Artist", "askAgainAfter": 60}

        async def publish(snapshot, changed):
            events.append((snapshot.track, changed))

        result = await scheduler.refresh_once(
            "device:source:1", previous=before, fetch=fetch, publish=publish,
            observed_at=OBSERVED + timedelta(minutes=1),
        )
        assert result.track == "B"

    asyncio.run(scenario())
    assert events == ["fetch", ("B", ("track", "artist"))]


def test_metadata_coalescer_publishes_latest_state_once():
    events = []

    async def scenario():
        coalescer = MetadataCoalescer(delay_seconds=0.001)

        async def publish(snapshot, changed):
            events.append((snapshot.track, changed))

        coalescer.submit("radio", MetadataSnapshot(track="A"), ("track",), publish)
        coalescer.submit(
            "radio", MetadataSnapshot(track="B", image_url="art"),
            ("track", "image_url"), publish,
        )
        await asyncio.sleep(0.01)
        await coalescer.shutdown()

    asyncio.run(scenario())
    assert events == [("B", ("image_url", "track"))]


def test_partial_metadata_update_retains_omitted_runtime_fields_and_can_clear_one():
    before = MetadataSnapshot(track="Titel", artist="Interpret", album="Album")
    partial = normalize_metadata({"track": "Neu"}, previous=before, observed_at=OBSERVED)
    cleared = normalize_metadata({"artist": None}, previous=partial, observed_at=OBSERVED)
    assert (partial.track, partial.artist, partial.album) == ("Neu", "Interpret", "Album")
    assert (cleared.track, cleared.artist, cleared.album) == ("Neu", None, "Album")


def test_clock_as_metadata_is_lab_projection_and_preserves_original_track():
    snapshot = MetadataSnapshot(track="Titel", artist="Interpret")
    assert clock_display_projection(snapshot, mode=ClockMetadataMode.OFF, now=OBSERVED) == "Titel"
    assert clock_display_projection(snapshot, mode=ClockMetadataMode.MISSING_TITLE, now=OBSERVED) == "Titel"
    assert clock_display_projection(snapshot, mode=ClockMetadataMode.APPEND, now=OBSERVED).endswith(" · 01:00")
    assert snapshot.track == "Titel"
    empty = MetadataSnapshot()
    assert clock_display_projection(empty, mode=ClockMetadataMode.MISSING_TITLE, now=OBSERVED) == "01:00"


def test_airplay_product_id_accepts_firmware_hex_notation():
    assert normalize_product_id("0x093b") == "0X093B"
    assert normalize_product_id("093B") == "0X093B"
    assert normalize_product_id("0939") == "0X0939"


def test_provider_and_playback_health_are_orthogonal():
    provider = reduce_provider_health(
        ProviderSignals(
            service_available=True,
            source_visible=True,
            auth_valid=True,
            reporting_health=ReportingHealth.FAILED,
        ),
        since=OBSERVED,
    )
    playback = reduce_playback_health(
        PlaybackSignals(
            radio_status="PLAY_STATE",
            source="LOCAL_INTERNET_RADIO",
            source_valid=True,
            position_advancing=True,
            stream_health=StreamHealth.REACHABLE,
        ),
        since=OBSERVED,
    )

    assert provider.state == ProviderHealth.REPORTING_DEGRADED
    assert playback.state == PlaybackHealth.PLAYING


def test_metadata_stale_does_not_change_playback_health():
    provider = reduce_provider_health(
        ProviderSignals(
            service_available=True,
            source_visible=True,
            auth_valid=True,
            metadata_health=MetadataHealth.STALE,
        )
    )
    playback = reduce_playback_health(
        PlaybackSignals(radio_status="PLAYING", source="LOCAL_INTERNET_RADIO")
    )
    assert provider.state == ProviderHealth.METADATA_STALE
    assert playback.state == PlaybackHealth.PLAYING


def test_stream_failure_can_stall_playback_while_provider_is_healthy():
    provider = reduce_provider_health(
        ProviderSignals(
            service_available=True,
            source_visible=True,
            account_available=True,
            auth_valid=True,
        )
    )
    playback = reduce_playback_health(
        PlaybackSignals(
            radio_status="BUFFERING",
            source="LOCAL_INTERNET_RADIO",
            progress_observed_for_s=31,
            stall_after_s=30,
            stream_health=StreamHealth.FAILED,
        )
    )
    assert provider.state == ProviderHealth.HEALTHY
    assert playback.state == PlaybackHealth.STALLED


def test_invalid_source_classifier_never_invents_a_cause():
    unknown = classify_invalid_source()
    assert unknown.cause == InvalidSourceCause.UNKNOWN
    assert unknown.confidence == 0
    known = classify_invalid_source(restriction_expired=True)
    assert known.cause == InvalidSourceCause.INACTIVITY_TIMEOUT
    assert known.confidence == 96
    ambiguous = classify_invalid_source(restriction_expired=True, stream_failed=True)
    assert ambiguous.cause == InvalidSourceCause.UNKNOWN
    assert ambiguous.confidence < known.confidence


def test_recovery_policy_limits_isolated_subsystem_failures():
    metadata = plan_recovery(
        reason=RecoveryReason.METADATA_STALE,
        requested_max_stage=RecoveryStage.CONTROLLED_STOP_PLAY,
        automatic=True,
    )
    reporting = plan_recovery(
        reason=RecoveryReason.REPORTING_DEGRADED,
        requested_max_stage=RecoveryStage.CONTROLLED_STOP_PLAY,
        automatic=True,
    )
    assert metadata.effective_max_stage == RecoveryStage.METADATA_REFRESH
    assert reporting.effective_max_stage == RecoveryStage.READBACK
    assert RecoveryStage.SAME_SOURCE_RESELECT not in metadata.stages
    assert RecoveryStage.CONTROLLED_STOP_PLAY not in reporting.stages


def test_radio_reboot_is_manual_lab_only_and_factory_reset_does_not_exist():
    denied = plan_recovery(
        reason=RecoveryReason.SOURCE_INVALID,
        requested_max_stage=RecoveryStage.MANUAL_LAB_RADIO_REBOOT,
        automatic=True,
        lab_mode=True,
        manual_radio_reboot=True,
    )
    allowed = plan_recovery(
        reason=RecoveryReason.SOURCE_INVALID,
        requested_max_stage=RecoveryStage.MANUAL_LAB_RADIO_REBOOT,
        automatic=False,
        lab_mode=True,
        manual_radio_reboot=True,
    )
    assert denied.allowed is False
    assert allowed.allowed is True
    assert max(stage.value for stage in RecoveryStage) == 7


def test_protected_device_recovery_denies_even_readback():
    denied = plan_recovery(
        reason=RecoveryReason.UNKNOWN,
        requested_max_stage=RecoveryStage.READBACK,
        protected_device=True,
    )
    assert denied.allowed is False
    assert denied.stages == ()
    assert "weder gelesen" in (denied.blocker or "")


def test_recovery_coordinator_stops_at_first_confirmed_recovery():
    calls = []

    async def scenario():
        plan = plan_recovery(
            reason=RecoveryReason.STREAM_FAILURE,
            requested_max_stage=RecoveryStage.STREAM_RERESOLVE,
        )
        actions = {}
        for stage in plan.stages:
            async def action(stage=stage):
                calls.append(stage)
                return {"ok": stage == RecoveryStage.PROVIDER_REFRESH}
            actions[stage] = action

        async def recovered(stage, result):
            return bool(result and result.get("ok"))

        run = await RecoveryCoordinator(cooldown_seconds=0).execute(
            "device", plan, actions=actions, recovered=recovered, now=OBSERVED
        )
        assert run.status == RecoveryStatus.RECOVERED

    asyncio.run(scenario())
    assert calls == [
        RecoveryStage.READBACK,
        RecoveryStage.METADATA_REFRESH,
        RecoveryStage.PROVIDER_REFRESH,
    ]


def test_airplay_readiness_uses_all_eight_gates_without_bypass():
    unsupported = assess_airplay_readiness(
        firmware_version="27.0.6.46330.5043500",
        product_id="0x0923",
        variant="spotty-scm",
    )
    assert unsupported.label == AirPlayReadinessLabel.UNSUPPORTED
    assert unsupported.blocking_stage == AirPlayBlockingStage.PRODUCT_ID

    partial = assess_airplay_readiness(
        firmware_version="27.0.6.46330.5043500",
        product_id="0x093b",
        variant="spotty-sm2",
        auth_hardware_detected=True,
        sts_registered=True,
        source_visible=True,
        mdns_visible=None,
    )
    assert partial.label == AirPlayReadinessLabel.PARTIAL
    assert partial.blocking_stage == AirPlayBlockingStage.MDNS

    ready = assess_airplay_readiness(
        firmware_version="27.0.6.46330.5043500",
        product_id="0x093b",
        variant="spotty-sm2",
        auth_hardware_detected=True,
        sts_registered=True,
        source_visible=True,
        mdns_visible=True,
        pairing_ready=True,
        ptp_ready=True,
        audio_ready=True,
    )
    assert ready.label == AirPlayReadinessLabel.READY
    assert ready.blocking_stage == AirPlayBlockingStage.NONE
