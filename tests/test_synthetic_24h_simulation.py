from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import time
from typing import Any

import pytest

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
    MetadataProvenance,
    MetadataSnapshot,
    mark_metadata_stale,
    metadata_changes,
    normalize_metadata,
)
from basswiesn.app.services.reporting_scheduler import (
    ReportPayload,
    ReportingScheduler,
    ReportingStatus,
)
from basswiesn.app.services.restrictions import deadline_from_play, parse_restrictions


pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "phase12"
START = datetime(2030, 1, 1, tzinfo=UTC)


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass
class VirtualTimeline:
    now: datetime = START
    events: list[dict[str, Any]] = field(default_factory=list)

    def advance(self, *, seconds: int = 0, hours: int = 0) -> datetime:
        self.now += timedelta(seconds=seconds, hours=hours)
        return self.now

    def record(self, domain: str, state: str, **evidence: Any) -> None:
        self.events.append(
            {
                "at": self.now.isoformat(),
                "domain": domain,
                "state": str(state),
                "evidence": evidence,
            }
        )


class Response:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None):
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict[str, Any]:
        return self._body


def _playing() -> PlaybackSignals:
    return PlaybackSignals(
        radio_status="PLAY_STATE",
        source="LOCAL_INTERNET_RADIO",
        source_valid=True,
        position_advancing=True,
        stream_health=StreamHealth.REACHABLE,
    )


def test_accelerated_24h_provider_playback_contract_from_phase12_fixtures():
    """Exercise a full day of contract transitions without sleeping or I/O."""

    wall_started = time.monotonic()
    timeline = VirtualTimeline()

    disabled_fixture = _fixture("station_inactivity_disabled.json")
    disabled = parse_restrictions(disabled_fixture["input"], received_at=timeline.now)
    assert disabled.inactivity_timeout_s == disabled_fixture["expected"]["inactivity_timeout_s"]
    assert disabled.timer_enabled is disabled_fixture["expected"]["timer_enabled"]
    assert disabled.effective_until is None
    timeline.record("RESTRICTIONS", "DISABLED", timeout=disabled.inactivity_timeout_s)

    timed_fixture = _fixture("station_inactivity_21600.json")
    timed = parse_restrictions(timed_fixture["input"], received_at=timeline.now)
    assert timed.timer_enabled is True
    assert timed.inactivity_timeout_s == 21600
    assert timed.effective_until is None
    play_started_at = timeline.now + timedelta(seconds=90)
    timed_deadline = deadline_from_play(play_started_at, timed.inactivity_timeout_s)
    assert timed_deadline == play_started_at + timedelta(seconds=21600)
    timeline.record("RESTRICTIONS", "ACTIVE", due=timed_deadline.isoformat())

    report_fixture = _fixture("reporting_success.json")
    report_attempts: list[tuple[str, dict[str, Any]]] = []

    async def reporting_cycle() -> None:
        responses = [
            Response(503),
            Response(report_fixture["input"]["http_status"], report_fixture["input"]["response"]),
        ]

        async def post(url: str, payload: dict[str, Any]) -> Response:
            report_attempts.append((url, payload))
            return responses.pop(0)

        scheduler = ReportingScheduler(post, backoff_seconds=(5, 10, 20, 40, 80))
        request = report_fixture["input"]["request"]
        await scheduler.enqueue(
            "synthetic-session",
            ReportPayload(
                timeStamp=request["timeStamp"],
                eventType=request["eventType"],
                timeIntoTrack=request["timeIntoTrack"],
            ),
            report_url="https://example.invalid/bmx/report",
            due_at=timeline.now,
        )
        failed = await scheduler.process_due("synthetic-session", now=timeline.now)
        assert failed.status == ReportingStatus.RETRY_WAIT
        assert failed.playback_action == "NONE"
        timeline.record("REPORTING", failed.status, playback_action=failed.playback_action)

        provider_degraded = reduce_provider_health(
            ProviderSignals(
                service_available=True,
                source_visible=True,
                auth_valid=True,
                reporting_health=ReportingHealth.DEGRADED,
            ),
            since=timeline.now,
        )
        playback_while_reporting_failed = reduce_playback_health(_playing(), since=timeline.now)
        assert provider_degraded.state == ProviderHealth.REPORTING_DEGRADED
        assert playback_while_reporting_failed.state == PlaybackHealth.PLAYING
        timeline.record("PROVIDER", provider_degraded.state)
        timeline.record("PLAYBACK", playback_while_reporting_failed.state)

        timeline.advance(seconds=5)
        recovered = await scheduler.process_due("synthetic-session", now=timeline.now)
        assert recovered.status == ReportingStatus.RECOVERED
        assert recovered.next_due_at == timeline.now + timedelta(seconds=300)
        assert recovered.playback_action == "NONE"
        timeline.record("REPORTING", recovered.status, next_due=recovered.next_due_at.isoformat())

        provider_recovered = reduce_provider_health(
            ProviderSignals(
                service_available=True,
                source_visible=True,
                account_available=True,
                auth_valid=True,
                reporting_health=ReportingHealth.RECOVERED,
                last_success=timeline.now,
            ),
            since=timeline.now,
        )
        assert provider_recovered.state == ProviderHealth.HEALTHY
        timeline.record("PROVIDER", provider_recovered.state)

    asyncio.run(reporting_cycle())
    assert len(report_attempts) == 2

    metadata_fixture = _fixture("metadata_title_change.json")
    before = MetadataSnapshot(
        station_name="Synthetic Station",
        station_id="fixture:station",
        track=metadata_fixture["input"]["before"]["track"],
        artist=metadata_fixture["input"]["before"]["artist"],
        provider="ORION",
        source="LOCAL_INTERNET_RADIO",
        updated_at=timeline.now,
        provenance=MetadataProvenance.PROVIDER,
        stale=False,
    )
    timeline.advance(hours=1)
    changed = normalize_metadata(
        metadata_fixture["input"]["after"],
        previous=before,
        observed_at=timeline.now,
    )
    assert metadata_changes(before, changed) == tuple(metadata_fixture["expected"]["changed"])
    assert changed.next_due_at == timeline.now + timedelta(seconds=60)
    assert changed.station_id == before.station_id
    assert changed.source == before.source
    timeline.record("METADATA", "UPDATED", changed=metadata_changes(before, changed))

    timeline.advance(seconds=301)
    stale = mark_metadata_stale(changed, now=timeline.now, stale_after_s=300)
    assert stale.stale is True
    stale_provider = reduce_provider_health(
        ProviderSignals(
            service_available=True,
            source_visible=True,
            auth_valid=True,
            metadata_health=MetadataHealth.STALE,
        ),
        since=timeline.now,
    )
    playback_during_stale_metadata = reduce_playback_health(_playing(), since=timeline.now)
    assert stale_provider.state == ProviderHealth.METADATA_STALE
    assert playback_during_stale_metadata.state == PlaybackHealth.PLAYING
    timeline.record("METADATA", "STALE")
    timeline.record("PLAYBACK", playback_during_stale_metadata.state)

    metadata_recovered = normalize_metadata(
        {"track": "Synthetic Track C", "artist": "Synthetic Artist", "askAgainAfter": 60},
        previous=stale,
        observed_at=timeline.advance(seconds=1),
    )
    assert metadata_recovered.stale is False
    timeline.record("METADATA", "RECOVERED")

    timeline.now = timed_deadline + timedelta(seconds=1)
    assert timeline.now > timed_deadline
    expired_diagnosis = classify_invalid_source(restriction_expired=True)
    assert expired_diagnosis.cause == InvalidSourceCause.INACTIVITY_TIMEOUT
    timeline.record("RESTRICTIONS", "EXPIRED", cause=expired_diagnosis.cause)

    timeline.advance(hours=6)
    stalled = reduce_playback_health(
        PlaybackSignals(
            radio_status="BUFFERING",
            source="LOCAL_INTERNET_RADIO",
            progress_observed_for_s=31,
            stall_after_s=30,
            stream_health=StreamHealth.FAILED,
        ),
        since=timeline.now,
    )
    assert stalled.state == PlaybackHealth.STALLED
    timeline.record("STREAM", "FAILED")
    timeline.record("PLAYBACK", stalled.state)

    invalid_fixture = _fixture("invalid_source.json")
    timeline.advance(hours=6)
    invalid_playback = reduce_playback_health(
        PlaybackSignals(
            radio_status="STOP_STATE",
            source=invalid_fixture["input"]["source_readback"],
            source_valid=False,
        ),
        since=timeline.now,
    )
    invalid_provider = reduce_provider_health(
        ProviderSignals(source_invalid=True, service_available=False),
        since=timeline.now,
    )
    invalid_cause = classify_invalid_source(
        provider_available=invalid_fixture["input"]["provider_available"]
    )
    assert invalid_playback.state == PlaybackHealth.FAILED
    assert invalid_provider.state == ProviderHealth.SOURCE_INVALID
    assert invalid_cause.cause == InvalidSourceCause.PROVIDER_UNAVAILABLE
    timeline.record("PROVIDER", invalid_provider.state)
    timeline.record("PLAYBACK", invalid_playback.state, automatic_action="NONE")
    timeline.record("INVALID_SOURCE", invalid_cause.cause, confidence=invalid_cause.confidence)

    timeline.now = START + timedelta(hours=24)
    final_metadata = normalize_metadata(
        {"track": "Synthetic Track D", "artist": "Synthetic Artist", "askAgainAfter": 60},
        previous=metadata_recovered,
        observed_at=timeline.now,
    )
    final_provider = reduce_provider_health(
        ProviderSignals(
            service_available=True,
            source_visible=True,
            account_available=True,
            auth_valid=True,
            metadata_health=MetadataHealth.CURRENT,
            reporting_health=ReportingHealth.RECOVERED,
            last_success=timeline.now,
        ),
        since=timeline.now,
    )
    final_playback = reduce_playback_health(_playing(), since=timeline.now)
    assert final_metadata.stale is False
    assert final_provider.state == ProviderHealth.HEALTHY
    assert final_playback.state == PlaybackHealth.PLAYING
    timeline.record("METADATA", "RECOVERED")
    timeline.record("PROVIDER", final_provider.state)
    timeline.record("PLAYBACK", final_playback.state)

    assert timeline.now == START + timedelta(hours=24)
    assert {event["domain"] for event in timeline.events} >= {
        "RESTRICTIONS",
        "REPORTING",
        "METADATA",
        "PROVIDER",
        "STREAM",
        "PLAYBACK",
        "INVALID_SOURCE",
    }
    assert time.monotonic() - wall_started < 2
