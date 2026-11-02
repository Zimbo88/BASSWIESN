"""Persisted provider-health observation, independent of polling and recovery.

This reads local evidence only. Selected source, metadata and reporting remain
separate signals; a successful report does not prove speaker output.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from sqlalchemy.orm import Session

from basswiesn.app.models import MetadataState, ReportingState
from basswiesn.app.services.health_models import MetadataHealth, ProviderSignals, ReportingHealth, reduce_provider_health
from basswiesn.app.services.provider_registry import SERVICE_MANIFEST


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _provider_health_projection(
    db: Session,
    device_id: str,
    stored: dict[str, Any],
    *,
    current_source: str,
    previous_source: str,
    observed_at: datetime,
) -> dict[str, Any] | None:
    """Reduce persisted subsystem evidence for the current radio source."""

    source_invalid = current_source == "INVALID_SOURCE"
    provider_id = (previous_source if source_invalid else current_source).strip().upper()
    if not provider_id or provider_id in {"INVALID_SOURCE", "STANDBY"}:
        return None
    providers = stored.get("providers")
    item = next(
        (
            value
            for key, value in (providers.items() if isinstance(providers, dict) else ())
            if str(key).strip().upper() == provider_id and isinstance(value, dict)
        ),
        {},
    )
    source_observed = bool(item.get("source_observed")) or bool(
        item.get("visible_in_sources")
    )
    service_observed = bool(item.get("service_observed"))
    # A current non-invalid /now_playing source is itself authoritative
    # evidence that this source is selected.  For INVALID_SOURCE, retain only
    # previously observed /sources evidence for the former provider.
    source_visible = (
        bool(item.get("visible_in_sources"))
        if source_invalid and source_observed
        else None
        if source_invalid
        else True
    )
    service_available = (
        bool(item.get("service_available")) if service_observed else None
    )
    auth_model = str(SERVICE_MANIFEST.get(provider_id, {}).get("auth_model") or "")
    anonymous_contract = auth_model in {"anonymous", "local"}
    account_available = True if anonymous_contract and source_visible else None
    auth_valid = True if anonymous_contract and source_visible else None

    metadata_row = (
        db.query(MetadataState)
        .filter(MetadataState.device_id == device_id)
        .one_or_none()
    )
    metadata_matches = bool(
        metadata_row is not None
        and provider_id
        in {
            str(metadata_row.provider or "").strip().upper(),
            str(metadata_row.source or "").strip().upper(),
        }
    )
    metadata_health = (
        MetadataHealth.STALE
        if metadata_row is not None and metadata_matches and bool(metadata_row.stale)
        else MetadataHealth.CURRENT
        if metadata_matches
        else MetadataHealth.UNKNOWN
    )

    reporting_row = (
        db.query(ReportingState)
        .filter(
            ReportingState.device_id == device_id,
            ReportingState.provider_id == provider_id,
        )
        .one_or_none()
    )
    reporting_state = str(reporting_row.state or "").strip().upper() if reporting_row else ""
    reporting_health = (
        ReportingHealth.FAILED
        if reporting_state == "FAILED"
        else ReportingHealth.DEGRADED
        if reporting_state in {"DEGRADED", "RETRY_WAIT"}
        else ReportingHealth.RECOVERED
        if reporting_state == "RECOVERED"
        else ReportingHealth.HEALTHY
        if reporting_state == "SUCCESS"
        else ReportingHealth.UNKNOWN
    )
    metadata_time = _as_utc(metadata_row.updated_at) if metadata_row is not None and metadata_matches else None
    evidence: list[dict[str, Any]] = [
        {
            "type": "AUTHORITATIVE_RADIO_SOURCE_READBACK",
            "source": current_source or None,
            "previous_source": previous_source or None,
            "source_invalid": source_invalid,
        },
        {
            "type": "PERSISTED_RADIO_PROVIDER_READBACK",
            "source_observed": source_observed,
            "source_visible": source_visible,
            "service_observed": service_observed,
            "service_available": service_available,
            "auth_model": auth_model or None,
        },
        {
            "type": "METADATA_HEALTH",
            "state": metadata_health.value,
            "matching_contract": metadata_matches,
            "updated_at": metadata_time.isoformat() if metadata_time is not None else None,
        },
        {
            "type": "REPORTING_HEALTH",
            "state": reporting_health.value,
            "contract_state": reporting_state or None,
            "last_http_status": reporting_row.last_http_status if reporting_row else None,
        },
    ]
    assessment = reduce_provider_health(
        ProviderSignals(
            source_invalid=source_invalid,
            source_visible=source_visible,
            service_available=service_available,
            account_available=account_available,
            auth_valid=auth_valid,
            metadata_health=metadata_health,
            reporting_health=reporting_health,
            last_success=(
                observed_at
                if source_visible is True
                and service_available is True
                and account_available is True
                and auth_valid is True
                else None
            ),
            evidence=evidence,
        ),
        since=observed_at,
    )
    return {
        "provider_id": provider_id,
        "assessment": assessment,
        "source_invalid": source_invalid,
        "source_visible": source_visible,
        "service_available": service_available,
        "account_available": account_available,
        "auth_valid": auth_valid,
        "auth_model": auth_model,
        "metadata_health": metadata_health,
        "reporting_health": reporting_health,
        "evidence": evidence,
        "availability": (
            "AVAILABLE"
            if service_available is True
            else "UNAVAILABLE"
            if service_available is False
            else "UNKNOWN"
        ),
        "association": "AVAILABLE" if account_available is True else "UNKNOWN",
        "observed_at": observed_at,
    }
