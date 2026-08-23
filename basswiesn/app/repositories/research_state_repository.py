"""Persistence boundary for the independent 1.6 research state contracts.

The repository deliberately has no radio or HTTP client dependency.  It can
therefore be used by provider callbacks, offline importers and simulations
without turning a state update into an implicit device action.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from enum import Enum
import ipaddress
import json
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from fastapi import Request
from sqlalchemy.orm import Session

from basswiesn.app.models import (
    AirPlayReadinessState,
    Device,
    DiagnosticEvent,
    MetadataState,
    PlaybackHealthState,
    ProviderHealthState,
    ReportingState,
    RestrictionState,
    utc_now,
)
from basswiesn.app.services.airplay_readiness import AirPlayReadiness
from basswiesn.app.services.health_models import HealthAssessment
from basswiesn.app.services.metadata_engine import MetadataSnapshot
from basswiesn.app.services.restrictions import ParsedRestrictions, deadline_from_play
from basswiesn.app.services.support_export import redact_payload, redact_text


LOCAL_PROVIDER_ID = "LOCAL_INTERNET_RADIO"
MAX_EVIDENCE_ITEMS = 50
MAX_EVIDENCE_JSON_BYTES = 32_768


def aware_utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def isoformat(value: datetime | None) -> str | None:
    return aware_utc(value).isoformat() if value is not None else None


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return aware_utc(value).isoformat()
    if isinstance(value, Enum):
        return value.value
    return str(value)


def redacted_evidence(value: Any) -> list[dict[str, Any]]:
    """Return a bounded, recursively redacted evidence list."""

    if value is None:
        rows: list[Any] = []
    elif isinstance(value, Mapping):
        rows = [dict(value)]
    elif isinstance(value, (list, tuple)):
        rows = list(value[:MAX_EVIDENCE_ITEMS])
    else:
        rows = [{"value": str(value)}]
    clean: list[dict[str, Any]] = []
    for row in rows:
        candidate = row if isinstance(row, Mapping) else {"value": row}
        sanitized = redact_payload(dict(candidate), anonymize_ips=True)
        clean.append(sanitized)
    encoded = json.dumps(clean, ensure_ascii=False, default=_json_default)
    if len(encoded.encode("utf-8")) > MAX_EVIDENCE_JSON_BYTES:
        return [{"redacted": True, "reason": "evidence exceeded storage limit"}]
    return clean


def dump_evidence(value: Any) -> str:
    return json.dumps(
        redacted_evidence(value),
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


def load_evidence(value: str | None) -> list[dict[str, Any]]:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, ValueError):
        return [{"redacted": True, "reason": "stored evidence is malformed"}]
    return redacted_evidence(decoded)


def sanitize_operational_url(value: str | None) -> str | None:
    """Remove credentials/query secrets while retaining a routable host."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        netloc = f"{hostname}{port}" if hostname else ""
        result = urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))
    except (TypeError, ValueError):
        return None
    return redact_text(result, anonymize_ips=False)[:2048]


def redact_url(value: str | None) -> str | None:
    """Return the API/diagnostic projection of a sanitized URL."""

    sanitized = sanitize_operational_url(value)
    return (
        redact_text(sanitized, anonymize_ips=True)[:2048]
        if sanitized is not None
        else None
    )


def resolve_request_device(db: Session, request: Request) -> Device | None:
    """Resolve an inbound provider callback without performing network I/O.

    Real radios are associated through their peer IP.  A known device header
    is accepted for reverse-proxy and deterministic test setups, but a valid
    peer IP and a contradictory header fail closed instead of attributing the
    callback to the wrong radio.
    """

    header_id = next(
        (
            request.headers.get(name, "").strip().upper()
            for name in (
                "x-basswiesn-device-id",
                "x-bose-device-id",
                "x-device-id",
            )
            if request.headers.get(name, "").strip()
        ),
        "",
    )
    header_device = (
        db.query(Device).filter(Device.device_id == header_id).one_or_none()
        if header_id
        else None
    )
    remote = (request.client.host if request.client else "").strip()
    try:
        remote_ip = str(ipaddress.ip_address(remote))
    except ValueError:
        remote_ip = ""
    ip_device = (
        db.query(Device).filter(Device.ip_address == remote_ip).one_or_none()
        if remote_ip
        else None
    )
    if ip_device is not None:
        return ip_device if header_device is None or header_device.id == ip_device.id else None
    if remote_ip and header_device is not None and header_device.ip_address != remote_ip:
        return None
    return header_device


class ResearchStateRepository:
    """Upsert and timeline operations for independently healthy subsystems."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record_event(
        self,
        *,
        device_id: str | None,
        domain: str,
        code: str,
        message: str | None = None,
        severity: str = "INFO",
        evidence: Any = None,
        correlation_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> DiagnosticEvent:
        row = DiagnosticEvent(
            event_id=uuid4().hex,
            occurred_at=aware_utc(occurred_at),
            domain=str(domain or "UNKNOWN").upper()[:64],
            severity=str(severity or "INFO").upper()[:32],
            device_id=(str(device_id).strip() if device_id else None),
            correlation_id=(str(correlation_id).strip()[:128] if correlation_id else None),
            code=str(code or "UNKNOWN")[:128],
            message=redact_text(str(message), anonymize_ips=True)[:2048] if message else None,
            evidence_json=dump_evidence(evidence),
            redacted=True,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def upsert_restrictions(
        self,
        device_id: str,
        source_key: str,
        parsed: ParsedRestrictions,
    ) -> RestrictionState:
        row = (
            self.session.query(RestrictionState)
            .filter(
                RestrictionState.device_id == device_id,
                RestrictionState.source_key == source_key,
            )
            .one_or_none()
        )
        before = (
            (row.inactivity_timeout_s, row.timer_enabled, row.origin)
            if row is not None
            else None
        )
        if row is None:
            row = RestrictionState(device_id=device_id, source_key=source_key)
            self.session.add(row)
        row.inactivity_timeout_s = parsed.inactivity_timeout_s
        row.timer_enabled = parsed.timer_enabled
        row.received_at = aware_utc(parsed.received_at)
        row.origin = parsed.origin
        # A response configures the timer but does not prove the firmware Play
        # event. Preserve a running timer only while the contract is unchanged.
        if before != (parsed.inactivity_timeout_s, parsed.timer_enabled, parsed.origin):
            row.timer_started_at = None
            row.effective_until = None
        if not parsed.timer_enabled:
            row.timer_started_at = None
            row.effective_until = None
        row.evidence_json = dump_evidence(parsed.evidence)
        row.updated_at = utc_now()
        after = (row.inactivity_timeout_s, row.timer_enabled, row.origin)
        if before != after:
            self.record_event(
                device_id=device_id,
                domain="RESTRICTIONS",
                code="RESTRICTION_UPDATED",
                message="Provider restriction contract updated.",
                evidence={
                    "source_key": source_key,
                    "inactivity_timeout_s": parsed.inactivity_timeout_s,
                    "timer_enabled": parsed.timer_enabled,
                    "origin": parsed.origin,
                },
                occurred_at=parsed.received_at,
            )
        self.session.flush()
        return row

    def set_restriction_timer(
        self,
        device_id: str,
        source_key: str,
        *,
        play_started_at: datetime | None,
        reason: str,
    ) -> RestrictionState | None:
        """Start/reset or cancel one selection-scoped inactivity timer."""

        row = (
            self.session.query(RestrictionState)
            .filter(
                RestrictionState.device_id == device_id,
                RestrictionState.source_key == source_key,
            )
            .one_or_none()
        )
        if row is None:
            return None
        before = (row.timer_started_at, row.effective_until)
        timeout = row.inactivity_timeout_s
        if (
            play_started_at is None
            or not row.timer_enabled
            or timeout is None
            or int(timeout) <= 0
        ):
            row.timer_started_at = None
            row.effective_until = None
            code = "RESTRICTION_TIMER_CANCELLED"
        else:
            started = aware_utc(play_started_at)
            row.timer_started_at = started
            row.effective_until = deadline_from_play(started, int(timeout))
            code = "RESTRICTION_TIMER_STARTED"
        row.updated_at = utc_now()
        after = (row.timer_started_at, row.effective_until)
        if before != after:
            self.record_event(
                device_id=device_id,
                domain="RESTRICTIONS",
                code=code,
                message="Provider inactivity timer was bound to radio playback evidence.",
                evidence={
                    "source_key": source_key,
                    "reason": reason,
                    "timer_started_at": isoformat(row.timer_started_at),
                    "effective_until": isoformat(row.effective_until),
                },
                occurred_at=play_started_at,
            )
        self.session.flush()
        return row

    def set_current_restriction_timer(
        self,
        device_id: str,
        source: str,
        *,
        play_started_at: datetime | None,
        reason: str,
    ) -> RestrictionState | None:
        """Bind the timer only when current selection identity is explicit."""

        normalized_source = str(source or "").strip().upper()
        if not normalized_source:
            return None
        keys: list[str] = []
        metadata = (
            self.session.query(MetadataState)
            .filter(MetadataState.device_id == device_id)
            .one_or_none()
        )
        if metadata is not None and metadata.station_id:
            metadata_sources = {
                str(metadata.source or "").strip().upper(),
                str(metadata.provider or "").strip().upper(),
            }
            if normalized_source in metadata_sources:
                keys.append(f"{normalized_source}:{metadata.station_id}")
        keys.append(normalized_source)
        for source_key in dict.fromkeys(keys):
            row = self.set_restriction_timer(
                device_id,
                source_key,
                play_started_at=play_started_at,
                reason=reason,
            )
            if row is not None:
                return row
        return None

    def upsert_metadata(
        self, device_id: str, snapshot: MetadataSnapshot
    ) -> MetadataState:
        row = (
            self.session.query(MetadataState)
            .filter(MetadataState.device_id == device_id)
            .one_or_none()
        )
        before = (
            (row.track, row.artist, row.album, row.artwork_url, row.stale)
            if row is not None
            else None
        )
        if row is None:
            row = MetadataState(device_id=device_id)
            self.session.add(row)
        row.station_name = snapshot.station_name
        row.station_id = snapshot.station_id
        row.track = snapshot.track
        row.artist = snapshot.artist
        row.album = snapshot.album
        # This is operational WebUI state, not diagnostic evidence.  Keep the
        # routable host/path while removing credentials, query data and
        # fragments.  IP anonymization belongs only in logs/support exports;
        # applying it here produced unusable ``<redacted-ip>`` image URLs.
        row.artwork_url = sanitize_operational_url(snapshot.image_url)
        row.artwork_provenance = snapshot.provenance.value
        row.provider = snapshot.provider
        row.source = snapshot.source
        row.provenance = snapshot.provenance.value
        row.confidence = max(0, min(100, int(snapshot.confidence)))
        row.updated_at = aware_utc(snapshot.updated_at) if snapshot.updated_at else None
        row.stale = bool(snapshot.stale)
        row.display_projection = snapshot.display_projection
        after = (row.track, row.artist, row.album, row.artwork_url, row.stale)
        if before != after:
            self.record_event(
                device_id=device_id,
                domain="METADATA",
                code="METADATA_STALE" if row.stale else "METADATA_UPDATED",
                message=(
                    "Live metadata became stale."
                    if row.stale
                    else "Live metadata fields updated without changing playback."
                ),
                evidence={
                    "station_id": row.station_id,
                    "provenance": row.provenance,
                    "changed": [
                        name
                        for name, old, new in zip(
                            ("track", "artist", "album", "image_url", "stale"),
                            before or (None, None, None, None, None),
                            after,
                        )
                        if old != new
                    ],
                },
                occurred_at=snapshot.updated_at,
            )
        self.session.flush()
        return row

    def upsert_provider_health(
        self,
        device_id: str,
        provider_id: str,
        assessment: HealthAssessment,
        *,
        source: str = "",
        availability: str = "UNKNOWN",
        association: str = "UNKNOWN",
    ) -> ProviderHealthState:
        row = (
            self.session.query(ProviderHealthState)
            .filter(
                ProviderHealthState.device_id == device_id,
                ProviderHealthState.provider_id == provider_id,
            )
            .one_or_none()
        )
        previous_state = row.state if row is not None else None
        if row is None:
            row = ProviderHealthState(device_id=device_id, provider_id=provider_id)
            self.session.add(row)
        state = assessment.state.value if isinstance(assessment.state, Enum) else str(assessment.state)
        row.provider_id = provider_id
        row.source = source
        row.availability = availability
        row.association = association
        row.state = state
        row.cause = assessment.cause
        row.evidence_json = dump_evidence(
            [
                {"kind": "health_assessment", "confidence": assessment.confidence},
                *assessment.evidence,
            ]
        )
        row.last_success_at = (
            aware_utc(assessment.last_success) if assessment.last_success else None
        )
        if previous_state != state:
            row.since = aware_utc(assessment.since)
            row.changed_at = aware_utc(assessment.since)
        row.recovery_action = assessment.recovery_action
        row.user_visible_reason = assessment.user_visible_reason
        row.updated_at = utc_now()
        if previous_state != state:
            self.record_event(
                device_id=device_id,
                domain="PROVIDER",
                code=f"PROVIDER_{state}",
                message=assessment.user_visible_reason,
                evidence=[
                    {"previous_state": previous_state, "cause": assessment.cause},
                    *assessment.evidence,
                ],
                occurred_at=assessment.since,
            )
        self.session.flush()
        return row

    def upsert_playback_health(
        self,
        device_id: str,
        assessment: HealthAssessment,
        *,
        source_valid: bool | None = None,
        stream_alive: bool | None = None,
        position_advancing: bool | None = None,
        provider_health: str | None = None,
        recovery_stage: int = 0,
    ) -> PlaybackHealthState:
        row = (
            self.session.query(PlaybackHealthState)
            .filter(PlaybackHealthState.device_id == device_id)
            .one_or_none()
        )
        previous_state = row.state if row is not None else None
        if row is None:
            row = PlaybackHealthState(device_id=device_id)
            self.session.add(row)
        state = assessment.state.value if isinstance(assessment.state, Enum) else str(assessment.state)
        row.state = state
        row.source_valid = source_valid
        row.stream_alive = stream_alive
        row.position_advancing = position_advancing
        row.provider_health = provider_health
        row.reason = assessment.cause
        row.evidence_json = dump_evidence(
            [
                {"kind": "health_assessment", "confidence": assessment.confidence},
                *assessment.evidence,
            ]
        )
        if previous_state != state:
            row.since = aware_utc(assessment.since)
        row.recovery_stage = max(0, min(7, int(recovery_stage)))
        row.observed_at = aware_utc(assessment.since)
        row.updated_at = utc_now()
        if previous_state != state:
            self.record_event(
                device_id=device_id,
                domain="PLAYBACK",
                code=f"PLAYBACK_{state}",
                message=assessment.user_visible_reason,
                evidence=[
                    {"previous_state": previous_state, "reason": assessment.cause},
                    *assessment.evidence,
                ],
                occurred_at=assessment.since,
            )
        self.session.flush()
        return row

    def record_reporting_success(
        self,
        device_id: str,
        provider_id: str,
        *,
        next_due_at: datetime | None,
        report_url: str | None,
        evidence: Any = None,
        observed_at: datetime | None = None,
    ) -> ReportingState:
        observed = aware_utc(observed_at)
        row = (
            self.session.query(ReportingState)
            .filter(
                ReportingState.device_id == device_id,
                ReportingState.provider_id == provider_id,
            )
            .one_or_none()
        )
        previous_state = row.state if row is not None else None
        if row is None:
            row = ReportingState(device_id=device_id, provider_id=provider_id)
            self.session.add(row)
        row.state = (
            "RECOVERED"
            if previous_state in {"RETRY_WAIT", "DEGRADED", "FAILED"}
            else "SUCCESS"
        )
        row.report_url = sanitize_operational_url(report_url)
        row.queue_depth = 0
        row.retry_count = 0
        row.next_due_at = aware_utc(next_due_at) if next_due_at else None
        row.last_http_status = 200
        row.last_success_at = observed
        row.last_failure_json = None
        row.updated_at = observed
        self.record_event(
            device_id=device_id,
            domain="REPORTING",
            code="REPORT_OK",
            message="Provider report accepted; playback state was not changed.",
            evidence={
                "previous_state": previous_state,
                "next_due_at": isoformat(next_due_at),
                **(dict(evidence) if isinstance(evidence, Mapping) else {}),
            },
            occurred_at=observed,
        )
        self.session.flush()
        return row

    def observe_reporting_contract(
        self,
        device_id: str,
        provider_id: str,
        *,
        report_url: str,
        observed_at: datetime | None = None,
    ) -> ReportingState:
        """Persist a newly selected dynamic reporting link as its own lease."""

        observed = aware_utc(observed_at)
        row = (
            self.session.query(ReportingState)
            .filter(
                ReportingState.device_id == device_id,
                ReportingState.provider_id == provider_id,
            )
            .one_or_none()
        )
        if row is None:
            row = ReportingState(device_id=device_id, provider_id=provider_id)
            self.session.add(row)
        row.state = "IDLE"
        row.report_url = sanitize_operational_url(report_url)
        row.queue_depth = 0
        row.retry_count = 0
        row.next_due_at = None
        row.last_http_status = None
        row.last_failure_json = None
        row.generation = int(row.generation or 0) + 1
        row.updated_at = observed
        self.record_event(
            device_id=device_id,
            domain="REPORTING",
            code="REPORTING_CONTRACT_RECEIVED",
            message="Dynamic provider reporting link registered.",
            evidence={"provider_id": provider_id, "url": redact_url(report_url)},
            occurred_at=observed,
        )
        self.session.flush()
        return row

    def upsert_airplay_readiness(
        self,
        device_id: str,
        readiness: AirPlayReadiness,
        *,
        observed_at: datetime | None = None,
        expires_at: datetime | None = None,
        provenance: str = "UNKNOWN",
    ) -> AirPlayReadinessState:
        row = (
            self.session.query(AirPlayReadinessState)
            .filter(AirPlayReadinessState.device_id == device_id)
            .one_or_none()
        )
        previous_stage = row.blocking_stage if row is not None else None
        if row is None:
            row = AirPlayReadinessState(device_id=device_id)
            self.session.add(row)
        values = asdict(readiness)
        for name in (
            "firmware_version",
            "product_id",
            "variant",
            "platform",
            "product_allowed",
            "auth_hardware_expected",
            "auth_hardware_detected",
            "sts_registered",
            "source_visible",
            "mdns_visible",
            "pairing_ready",
            "ptp_ready",
            "audio_ready",
            "confidence",
        ):
            setattr(row, name, values[name])
        row.blocking_stage = readiness.blocking_stage.value
        row.evidence_json = dump_evidence(readiness.evidence)
        row.provenance = str(provenance or "UNKNOWN")[:64]
        row.observed_at = aware_utc(observed_at) if observed_at else utc_now()
        row.expires_at = aware_utc(expires_at) if expires_at else None
        row.updated_at = utc_now()
        if previous_stage != row.blocking_stage:
            self.record_event(
                device_id=device_id,
                domain="AIRPLAY",
                code="AIRPLAY_READINESS_CHANGED",
                message=readiness.user_visible_status,
                evidence={
                    "previous_stage": previous_stage,
                    "blocking_stage": row.blocking_stage,
                    "confidence": readiness.confidence,
                },
                occurred_at=row.observed_at,
            )
        self.session.flush()
        return row


def health_confidence(evidence: Iterable[Mapping[str, Any]]) -> int:
    for row in evidence:
        if row.get("kind") != "health_assessment":
            continue
        try:
            return max(0, min(100, int(row.get("confidence", 0))))
        except (TypeError, ValueError):
            return 0
    return 0
