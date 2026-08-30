"""Orthogonal provider, playback and subsystem health reducers for 2.5.1."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ProviderHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    REPORTING_DEGRADED = "REPORTING_DEGRADED"
    METADATA_STALE = "METADATA_STALE"
    AUTH_REFRESH_REQUIRED = "AUTH_REFRESH_REQUIRED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    SOURCE_INVALID = "SOURCE_INVALID"
    RECOVERING = "RECOVERING"
    FAILED = "FAILED"


class PlaybackHealth(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    BUFFERING = "BUFFERING"
    PLAYING = "PLAYING"
    PAUSED = "PAUSED"
    STALLED = "STALLED"
    RECOVERING = "RECOVERING"
    FAILED = "FAILED"


class MetadataHealth(StrEnum):
    UNKNOWN = "UNKNOWN"
    CURRENT = "CURRENT"
    STALE = "STALE"
    FAILED = "FAILED"
    RECOVERING = "RECOVERING"


class ReportingHealth(StrEnum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    RECOVERED = "RECOVERED"


class SessionHealth(StrEnum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    EXPIRING = "EXPIRING"
    REFRESH_REQUIRED = "REFRESH_REQUIRED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class StreamHealth(StrEnum):
    UNKNOWN = "UNKNOWN"
    REACHABLE = "REACHABLE"
    CONNECTING = "CONNECTING"
    BUFFERING = "BUFFERING"
    STALLED = "STALLED"
    FAILED = "FAILED"


class InvalidSourceCause(StrEnum):
    INACTIVITY_TIMEOUT = "INACTIVITY_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    ACCOUNT_UNAVAILABLE = "ACCOUNT_UNAVAILABLE"
    REPORTING_DEGRADED = "REPORTING_DEGRADED"
    STREAM_FAILURE = "STREAM_FAILURE"
    UNSUPPORTED_STREAM = "UNSUPPORTED_STREAM"
    SOURCE_REMOVED = "SOURCE_REMOVED"
    INTERNAL_SERVICE_FAILURE = "INTERNAL_SERVICE_FAILURE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class HealthAssessment:
    state: str
    cause: str
    evidence: list[dict[str, Any]]
    last_success: datetime | None
    since: datetime
    recovery_action: str
    user_visible_reason: str
    confidence: int

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["last_success"] = self.last_success.isoformat() if self.last_success else None
        result["since"] = self.since.isoformat()
        return result


@dataclass(frozen=True)
class ProviderSignals:
    source_invalid: bool = False
    source_visible: bool | None = None
    service_available: bool | None = None
    account_available: bool | None = None
    auth_valid: bool | None = None
    auth_refresh_required: bool = False
    reporting_health: ReportingHealth = ReportingHealth.UNKNOWN
    reporting_semantic_persistent: bool = False
    metadata_health: MetadataHealth = MetadataHealth.UNKNOWN
    recovering: bool = False
    failure_budget_exhausted: bool = False
    last_success: datetime | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class PlaybackSignals:
    radio_status: str | None
    source: str | None = None
    source_valid: bool | None = None
    position_advancing: bool | None = None
    progress_observed_for_s: float | None = None
    stall_after_s: float = 30.0
    stream_health: StreamHealth = StreamHealth.UNKNOWN
    recovering: bool = False
    recovery_failed: bool = False
    last_success: datetime | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class InvalidSourceDiagnosis:
    cause: InvalidSourceCause
    confidence: int
    evidence: list[dict[str, Any]]
    user_visible_reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "cause": self.cause.value,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "user_visible_reason": self.user_visible_reason,
        }


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def reduce_provider_health(
    signals: ProviderSignals, *, since: datetime | None = None
) -> HealthAssessment:
    observed = _now(since)
    common = {
        "evidence": list(signals.evidence),
        "last_success": signals.last_success,
        "since": observed,
    }
    if signals.recovering:
        return HealthAssessment(
            ProviderHealth.RECOVERING,
            "TARGETED_RECOVERY_ACTIVE",
            recovery_action="Recovery-Ergebnis abwarten",
            user_visible_reason="Der Provider wird kontrolliert wiederhergestellt.",
            confidence=100,
            **common,
        )
    if signals.failure_budget_exhausted:
        return HealthAssessment(
            ProviderHealth.FAILED,
            "RECOVERY_BUDGET_EXHAUSTED",
            recovery_action="Manuelle Diagnose öffnen",
            user_visible_reason="Der Provider konnte innerhalb des sicheren Budgets nicht wiederhergestellt werden.",
            confidence=95,
            **common,
        )
    if signals.source_invalid:
        return HealthAssessment(
            ProviderHealth.SOURCE_INVALID,
            "AUTHORITATIVE_SOURCE_INVALID",
            recovery_action="Ursache klassifizieren; noch nicht neu auswählen",
            user_visible_reason="Das Radio meldet die Quelle als ungültig.",
            confidence=100,
            **common,
        )
    if signals.auth_refresh_required or signals.auth_valid is False:
        return HealthAssessment(
            ProviderHealth.AUTH_REFRESH_REQUIRED,
            "AUTHENTICATION_EXPIRED_OR_REJECTED",
            recovery_action="Provider-Anmeldung einmalig aktualisieren",
            user_visible_reason="Die Provider-Anmeldung muss aktualisiert werden.",
            confidence=95 if signals.auth_refresh_required else 80,
            **common,
        )
    if signals.service_available is False or signals.source_visible is False:
        return HealthAssessment(
            ProviderHealth.SERVICE_UNAVAILABLE,
            "SERVICE_OR_SOURCE_UNAVAILABLE",
            recovery_action="Service- und Quellenstatus aktualisieren",
            user_visible_reason="Der Dienst oder seine Quelle ist derzeit nicht verfügbar.",
            confidence=95,
            **common,
        )
    if signals.account_available is False:
        return HealthAssessment(
            ProviderHealth.SERVICE_UNAVAILABLE,
            "ACCOUNT_UNAVAILABLE",
            recovery_action="Kontozuordnung prüfen",
            user_visible_reason="Die lokale Provider-Zuordnung ist nicht verfügbar.",
            confidence=85,
            **common,
        )
    if signals.reporting_semantic_persistent:
        return HealthAssessment(
            ProviderHealth.DEGRADED,
            "PERSISTENT_REPORTING_SEMANTIC_ERROR",
            recovery_action="Providerstatus aktualisieren; Wiedergabe beibehalten",
            user_visible_reason="Der Provider meldet einen dauerhaften Reportingfehler.",
            confidence=90,
            **common,
        )
    if signals.reporting_health in {ReportingHealth.DEGRADED, ReportingHealth.FAILED}:
        return HealthAssessment(
            ProviderHealth.REPORTING_DEGRADED,
            "REPORTING_TRANSPORT_DEGRADED",
            recovery_action="Reporting separat mit Backoff wiederholen",
            user_visible_reason="Statusberichte sind gestört; die Wiedergabe wird nicht unterbrochen.",
            confidence=100,
            **common,
        )
    if signals.metadata_health in {MetadataHealth.STALE, MetadataHealth.FAILED}:
        return HealthAssessment(
            ProviderHealth.METADATA_STALE,
            "METADATA_STALE",
            recovery_action="Nur Metadaten aktualisieren",
            user_visible_reason="Die Wiedergabe läuft, aber die Titelinformationen sind veraltet.",
            confidence=100,
            **common,
        )
    # HEALTHY is a positive assertion.  Unknown values are not equivalent to
    # successful observations; all four independent provider gates must have
    # been observed positively before exposing that state.
    if (
        signals.service_available is True
        and signals.source_visible is True
        and signals.account_available is True
        and signals.auth_valid is True
    ):
        return HealthAssessment(
            ProviderHealth.HEALTHY,
            "ALL_OBSERVED_PROVIDER_SIGNALS_OK",
            recovery_action="Keine",
            user_visible_reason="Provider ist verfügbar.",
            confidence=90,
            **common,
        )
    return HealthAssessment(
        ProviderHealth.DEGRADED,
        "INSUFFICIENT_OR_PARTIAL_EVIDENCE",
        recovery_action="Read-only Providerstatus ergänzen",
        user_visible_reason="Der Providerstatus ist nur teilweise bekannt.",
        confidence=45,
        **common,
    )


def reduce_playback_health(
    signals: PlaybackSignals, *, since: datetime | None = None
) -> HealthAssessment:
    observed = _now(since)
    common = {
        "evidence": list(signals.evidence),
        "last_success": signals.last_success,
        "since": observed,
    }
    if signals.recovering:
        return HealthAssessment(
            PlaybackHealth.RECOVERING,
            "TARGETED_RECOVERY_ACTIVE",
            recovery_action="Recovery-Ergebnis per Radio-Readback prüfen",
            user_visible_reason="Die Wiedergabe wird kontrolliert wiederhergestellt.",
            confidence=100,
            **common,
        )
    if signals.recovery_failed:
        return HealthAssessment(
            PlaybackHealth.FAILED,
            "RECOVERY_FAILED",
            recovery_action="Manuelle Diagnose",
            user_visible_reason="Die Wiedergabe konnte nicht sicher wiederhergestellt werden.",
            confidence=100,
            **common,
        )

    status = (signals.radio_status or "").strip().upper()
    source = (signals.source or "").strip().upper()
    if source == "INVALID_SOURCE" or signals.source_valid is False:
        return HealthAssessment(
            PlaybackHealth.FAILED,
            "AUTHORITATIVE_INVALID_SOURCE",
            recovery_action="INVALID_SOURCE-Ursache klassifizieren",
            user_visible_reason="Das Radio meldet eine ungültige Quelle.",
            confidence=100,
            **common,
        )
    if not status:
        return HealthAssessment(
            PlaybackHealth.FAILED,
            "NO_AUTHORITATIVE_RADIO_READBACK",
            recovery_action="Autoritativen Readback ergänzen",
            user_visible_reason="Der Wiedergabestatus ist noch nicht durch das Radio belegt.",
            confidence=0,
            **common,
        )
    if status in {"STOPPED", "STOP_STATE", "STANDBY"} or source == "STANDBY":
        return HealthAssessment(
            PlaybackHealth.STOPPED,
            "RADIO_STOPPED",
            recovery_action="Keine",
            user_visible_reason="Das Radio spielt nicht.",
            confidence=95 if status else 60,
            **common,
        )
    if status in {"PAUSED", "PAUSE_STATE"}:
        return HealthAssessment(
            PlaybackHealth.PAUSED,
            "RADIO_PAUSED",
            recovery_action="Keine",
            user_visible_reason="Die Wiedergabe ist pausiert.",
            confidence=100,
            **common,
        )
    if status in {"STARTING", "CONNECTING", "CONNECT_STATE"}:
        return HealthAssessment(
            PlaybackHealth.STARTING,
            "RADIO_STARTING",
            recovery_action="Verbindungszeitlimit abwarten",
            user_visible_reason="Die Wiedergabe wird gestartet.",
            confidence=95,
            **common,
        )
    if status in {"BUFFERING", "BUFFERING_STATE"}:
        elapsed = signals.progress_observed_for_s
        if elapsed is not None and elapsed > signals.stall_after_s:
            return HealthAssessment(
                PlaybackHealth.STALLED,
                "BUFFERING_TIMEOUT",
                recovery_action="Stream prüfen und URL kontrolliert neu auflösen",
                user_visible_reason="Das Radio puffert länger als erwartet.",
                confidence=95,
                **common,
            )
        return HealthAssessment(
            PlaybackHealth.BUFFERING,
            "RADIO_BUFFERING",
            recovery_action="Pufferzeitlimit abwarten",
            user_visible_reason="Das Radio puffert den Stream.",
            confidence=100,
            **common,
        )
    if status in {"PLAYING", "PLAY_STATE"}:
        elapsed = signals.progress_observed_for_s
        if (
            signals.position_advancing is False
            and elapsed is not None
            and elapsed > signals.stall_after_s
        ):
            return HealthAssessment(
                PlaybackHealth.STALLED,
                "NO_PLAYBACK_PROGRESS",
                recovery_action="Readback wiederholen und Stream prüfen",
                user_visible_reason="Das Radio meldet Wiedergabe, zeigt aber keinen Fortschritt.",
                confidence=90,
                **common,
            )
        # Provider, reporting and metadata are deliberately ignored here.
        return HealthAssessment(
            PlaybackHealth.PLAYING,
            "AUTHORITATIVE_RADIO_PLAY_STATE",
            recovery_action="Keine",
            user_visible_reason="Das Radio meldet laufende Wiedergabe.",
            confidence=95 if signals.position_advancing is True else 80,
            **common,
        )
    return HealthAssessment(
        PlaybackHealth.FAILED,
        "UNKNOWN_RADIO_PLAYER_STATE",
        recovery_action="Autoritativen Readback wiederholen",
        user_visible_reason="Der Playerzustand konnte nicht eingeordnet werden.",
        confidence=50,
        **common,
    )


def classify_invalid_source(
    *,
    restriction_expired: bool = False,
    provider_available: bool | None = None,
    account_available: bool | None = None,
    reporting_semantic_persistent: bool = False,
    stream_failed: bool = False,
    unsupported_stream: bool = False,
    source_removed: bool = False,
    internal_service_failed: bool = False,
    evidence: list[dict[str, Any]] | None = None,
) -> InvalidSourceDiagnosis:
    rows = list(evidence or [])
    candidates = [
        (restriction_expired, InvalidSourceCause.INACTIVITY_TIMEOUT, 96, "Der Provider-Inaktivitätstimer ist nachweislich abgelaufen."),
        (unsupported_stream, InvalidSourceCause.UNSUPPORTED_STREAM, 97, "Der Streamtyp oder Codec wird vom Radio nicht unterstützt."),
        (stream_failed, InvalidSourceCause.STREAM_FAILURE, 95, "Der Audiostream ist ausgefallen oder nicht auflösbar."),
        (source_removed, InvalidSourceCause.SOURCE_REMOVED, 96, "Die Quelle wurde aus der Quellenliste entfernt."),
        (provider_available is False, InvalidSourceCause.PROVIDER_UNAVAILABLE, 94, "Der Provider ist nicht verfügbar."),
        (account_available is False, InvalidSourceCause.ACCOUNT_UNAVAILABLE, 78, "Die Provider-Zuordnung ist nicht verfügbar."),
        (internal_service_failed, InvalidSourceCause.INTERNAL_SERVICE_FAILURE, 86, "Ein zugeordneter lokaler Radio-Dienst ist ausgefallen."),
        (reporting_semantic_persistent, InvalidSourceCause.REPORTING_DEGRADED, 85, "Ein dauerhafter semantischer Reportingfehler könnte den Provider degradiert haben."),
    ]
    matches = [entry for entry in candidates if entry[0]]
    if len(matches) == 1:
        _, cause, confidence, reason = matches[0]
        return InvalidSourceDiagnosis(cause, confidence, rows, reason)
    if len(matches) > 1:
        return InvalidSourceDiagnosis(
            InvalidSourceCause.UNKNOWN,
            35,
            rows,
            "Mehrere Ursachen passen zur Evidenz; eine eindeutige Ursache ist nicht belegt.",
        )
    return InvalidSourceDiagnosis(
        InvalidSourceCause.UNKNOWN,
        0,
        rows,
        "Für INVALID_SOURCE liegt noch keine belastbare Ursache vor.",
    )
