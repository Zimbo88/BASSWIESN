"""Read-only API for the independent BASSWIESN 2.0 health contracts."""

from __future__ import annotations

import asyncio
import base64
import binascii
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
from typing import Any
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.config import get_settings
from basswiesn.app.db import get_db
from basswiesn.app.models import (
    AirPlayReadinessState,
    ArtworkCacheEntry,
    DiagnosticEvent,
    MetadataState,
    PlaybackHealthState,
    ProviderHealthState,
    ReportingState,
    RestrictionState,
    Station,
)
from basswiesn.app.repositories.research_state_repository import (
    ResearchStateRepository,
    health_confidence,
    isoformat,
    load_evidence,
    redact_url,
    sanitize_operational_url,
)
from basswiesn.app.routers.shared import device_or_404
from basswiesn.app.services.clock_metadata import (
    clock_metadata_lab_enabled,
    load_clock_metadata_preference,
    save_clock_metadata_preference,
)
from basswiesn.app.services.metadata_engine import MetadataProvenance
from basswiesn.app.services.airplay_readiness import assess_airplay_readiness
from basswiesn.app.services.protected_devices import require_unprotected_device
from basswiesn.app.services.targeted_mdns import probe_targeted_airplay_mdns
from basswiesn.app.services.setup_rebuild.profiles import DeviceFacts, detect_profile
from basswiesn.app.services.artwork import (
    DEFAULT_SOURCE_ICON,
    SAFE_REMOTE_IMAGE_TYPES,
    ArtworkResult,
    cache_artwork,
    choose_artwork,
)


router = APIRouter(prefix="/api", tags=["research-state"])

_ARTWORK_CACHE_KEY = re.compile(r"^[0-9a-f]{64}$")
_AIRPLAY_RUNTIME_TTL_SECONDS = 300


def _xml_local_name(element: Any) -> str:
    return str(getattr(element, "tag", "")).rsplit("}", 1)[-1].lower()


def _xml_first(root: Any, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for element in root.iter():
        if _xml_local_name(element) in wanted and str(element.text or "").strip():
            return str(element.text).strip()
    return ""


def _airplay_source_visible(root: Any) -> bool:
    for element in root.iter():
        attributes = {str(key).lower(): str(value).strip().upper() for key, value in element.attrib.items()}
        if any(attributes.get(key) == "AIRPLAY" for key in ("source", "type", "sourcekey")):
            return True
        if _xml_local_name(element) in {"source", "sourcekey", "sourcetype"}:
            if str(element.text or "").strip().upper() == "AIRPLAY":
                return True
    return False


def _artwork_station(db: Session, station_id: str | None) -> Station | None:
    """Resolve a persisted selection without contacting a radio/provider."""

    key = str(station_id or "").strip()
    if not key:
        return None
    station = (
        db.query(Station)
        .filter(Station.provider_station_id == key)
        .one_or_none()
    )
    if station is None and key.isdigit():
        station = db.query(Station).filter(Station.id == int(key)).one_or_none()
    return station


def _public_artwork_payload(result: ArtworkResult) -> dict[str, Any]:
    """Expose only a same-origin render target, never the provider URL."""

    if result.cache_key and result.status in {"FETCHED", "HIT"}:
        public_url = f"/api/artwork-cache/{result.cache_key}"
    elif result.public_url.startswith("/static/"):
        public_url = result.public_url
    else:
        public_url = DEFAULT_SOURCE_ICON
    return {
        "status": result.status,
        "source": result.choice.source.value,
        "public_url": public_url,
        "fetched_at": isoformat(result.fetched_at),
        "expires_at": isoformat(result.expires_at),
        "failure_status": result.failure_status,
        "webui_supported": result.choice.webui_supported,
        "radio_oled_supported": result.choice.radio_oled_supported,
    }


async def _metadata_artwork_result(
    db: Session, device_id: str, row: MetadataState | None
) -> ArtworkResult:
    station = _artwork_station(db, row.station_id if row is not None else None)
    artwork_url = str(row.artwork_url or "").strip() if row is not None else ""
    artwork_provenance = str(
        (row.artwork_provenance or row.provenance or "") if row is not None else ""
    ).upper()
    # The persistence model has one operational live-artwork field. Its
    # provenance distinguishes provider artwork from other live imageUrl
    # updates; station artwork remains an independent lower-priority source.
    live_image_url = artwork_url if artwork_provenance != "PROVIDER" else None
    provider_artwork_url = artwork_url if artwork_provenance == "PROVIDER" else None
    station_logo_url = station.image_url if station is not None else None
    source_icon_url = (
        DEFAULT_SOURCE_ICON
        if row is not None and (row.source or row.provider)
        else None
    )
    choice = choose_artwork(
        image_url=live_image_url,
        provider_artwork_url=provider_artwork_url,
        station_logo_url=station_logo_url,
        source_icon_url=source_icon_url,
    )
    return await cache_artwork(
        db,
        choice,
        media_dir=get_settings().data_dir / "media",
        device_id=device_id,
        provider_id=str(row.provider or "") if row is not None else "",
        station_id=str(row.station_id or "") if row is not None else "",
    )


async def _station_artwork_result(db: Session, station: Station) -> ArtworkResult:
    station_key = str(station.provider_station_id or station.id)
    choice = choose_artwork(station_logo_url=station.image_url)
    return await cache_artwork(
        db,
        choice,
        media_dir=get_settings().data_dir / "media",
        provider_id=str(station.provider or ""),
        station_id=station_key,
    )


def _cache_entry_path(entry: ArtworkCacheEntry) -> tuple[Path, str]:
    """Validate both the database reference and filesystem containment."""

    mime_type = str(entry.mime_type or "").lower()
    extensions = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    expected_suffix = extensions.get(mime_type)
    if mime_type not in SAFE_REMOTE_IMAGE_TYPES or expected_suffix is None:
        raise HTTPException(status_code=404, detail="artwork not available")
    if not entry.cached_path or entry.failure_status:
        raise HTTPException(status_code=404, detail="artwork not available")
    now = datetime.now(UTC)
    expires_at = entry.expires_at
    if expires_at is not None:
        expires_at = (
            expires_at.replace(tzinfo=UTC)
            if expires_at.tzinfo is None
            else expires_at.astimezone(UTC)
        )
        if expires_at <= now:
            raise HTTPException(status_code=404, detail="artwork cache expired")

    allowed_root = (get_settings().data_dir / "media" / "artwork-cache").resolve()
    configured_path = Path(entry.cached_path)
    try:
        resolved = configured_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="artwork not available") from None
    expected_name = f"{entry.cache_key}{expected_suffix}"
    if (
        configured_path.is_symlink()
        or resolved.parent != allowed_root
        or resolved.name != expected_name
        or not resolved.is_file()
    ):
        raise HTTPException(status_code=404, detail="artwork not available")
    return resolved, mime_type


@router.get("/artwork-cache/{cache_key}")
async def cached_artwork(cache_key: str, db: Session = Depends(get_db)) -> FileResponse:
    """Serve one validated raster from the dedicated WebUI artwork directory."""

    if _ARTWORK_CACHE_KEY.fullmatch(cache_key) is None:
        raise HTTPException(status_code=404, detail="artwork not available")
    entry = (
        db.query(ArtworkCacheEntry)
        .filter(ArtworkCacheEntry.cache_key == cache_key)
        .one_or_none()
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="artwork not available")
    path, mime_type = _cache_entry_path(entry)
    return FileResponse(
        path,
        media_type=mime_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _encode_timeline_cursor(row: DiagnosticEvent) -> str:
    payload = json.dumps(
        [isoformat(row.occurred_at), row.id], separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_timeline_cursor(value: str) -> tuple[datetime, int]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError
        occurred_at = datetime.fromisoformat(str(decoded[0]).replace("Z", "+00:00"))
        occurred_at = (
            occurred_at.replace(tzinfo=UTC)
            if occurred_at.tzinfo is None
            else occurred_at.astimezone(UTC)
        )
        row_id = int(decoded[1])
        if row_id < 1:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="invalid timeline cursor") from exc
    return occurred_at, row_id


def _unknown(device_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "state": "UNKNOWN",
        "observed_at": None,
        "confidence": 0,
        "evidence": [],
        **extra,
    }


def _provider_row(row: ProviderHealthState) -> dict[str, Any]:
    evidence = load_evidence(row.evidence_json)
    return {
        "provider_id": row.provider_id,
        "source": row.source or None,
        "state": row.state,
        "availability": row.availability,
        "association": row.association,
        "cause": row.cause,
        "last_success": isoformat(row.last_success_at),
        "since": isoformat(row.since),
        "observed_at": isoformat(row.updated_at or row.changed_at),
        "recovery_action": row.recovery_action,
        "user_visible_reason": row.user_visible_reason,
        "confidence": health_confidence(evidence),
        "evidence": evidence,
    }


def _aggregate_provider_state(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "UNKNOWN"
    priorities = (
        "FAILED",
        "SOURCE_INVALID",
        "SERVICE_UNAVAILABLE",
        "AUTH_REFRESH_REQUIRED",
        "RECOVERING",
        "DEGRADED",
        "REPORTING_DEGRADED",
        "METADATA_STALE",
        "HEALTHY",
    )
    present = {str(row.get("state") or "UNKNOWN") for row in rows}
    return next((state for state in priorities if state in present), "UNKNOWN")


@router.get("/devices/{device_id}/playback-health")
async def playback_health(device_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return persisted radio-readback health without contacting the radio."""

    device_or_404(db, device_id)
    row = (
        db.query(PlaybackHealthState)
        .filter(PlaybackHealthState.device_id == device_id)
        .one_or_none()
    )
    if row is None:
        return _unknown(
            device_id,
            source_valid=None,
            stream_alive=None,
            position_advancing=None,
            provider_health=None,
            reason="NO_AUTHORITATIVE_READBACK",
            since=None,
            recovery_stage=0,
        )
    evidence = load_evidence(row.evidence_json)
    return {
        "device_id": device_id,
        "state": row.state,
        "source_valid": row.source_valid,
        "stream_alive": row.stream_alive,
        "position_advancing": row.position_advancing,
        "provider_health": row.provider_health,
        "reason": row.reason,
        "since": isoformat(row.since),
        "recovery_stage": row.recovery_stage,
        "observed_at": isoformat(row.observed_at),
        "confidence": health_confidence(evidence),
        "evidence": evidence,
    }


@router.get("/devices/{device_id}/provider-health")
async def provider_health(
    device_id: str,
    provider: str | None = Query(default=None, min_length=1, max_length=128),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    device_or_404(db, device_id)
    query = db.query(ProviderHealthState).filter(
        ProviderHealthState.device_id == device_id
    )
    if provider:
        query = query.filter(ProviderHealthState.provider_id == provider)
    rows = [_provider_row(row) for row in query.order_by(ProviderHealthState.provider_id).all()]
    if provider and not rows:
        rows = [
            {
                "provider_id": provider,
                "source": None,
                "state": "UNKNOWN",
                "availability": "UNKNOWN",
                "association": "UNKNOWN",
                "cause": None,
                "last_success": None,
                "since": None,
                "observed_at": None,
                "recovery_action": None,
                "user_visible_reason": "Für diesen Provider liegen noch keine Beobachtungen vor.",
                "confidence": 0,
                "evidence": [],
            }
        ]
    observed = max(
        (row["observed_at"] for row in rows if row["observed_at"]),
        default=None,
    )
    return {
        "device_id": device_id,
        "provider_id": provider,
        "state": _aggregate_provider_state(rows),
        "providers": rows,
        "observed_at": observed,
        "confidence": min((row["confidence"] for row in rows), default=0),
        "evidence": [
            {"provider_id": row["provider_id"], "refs": row["evidence"]}
            for row in rows
            if row["evidence"]
        ],
    }


@router.get("/devices/{device_id}/metadata")
async def metadata(device_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    device_or_404(db, device_id)
    row = (
        db.query(MetadataState)
        .filter(MetadataState.device_id == device_id)
        .one_or_none()
    )
    if row is None:
        return _unknown(
            device_id,
            station_name=None,
            station_id=None,
            track=None,
            artist=None,
            album=None,
            image_url=None,
            provider=None,
            source=None,
            provenance="UNKNOWN",
            stale=True,
            display_projection=None,
        )
    return {
        "device_id": device_id,
        "state": "STALE" if row.stale else "CURRENT",
        "station_name": row.station_name,
        "station_id": row.station_id,
        "track": row.track,
        "artist": row.artist,
        "album": row.album,
        # Artwork is an operational field.  It is already stored without
        # credentials/query secrets, and unlike diagnostic evidence its host
        # must remain routable for the WebUI.
        "image_url": sanitize_operational_url(row.artwork_url),
        "artwork_provenance": row.artwork_provenance,
        "provider": row.provider,
        "source": row.source,
        "provenance": row.provenance,
        "stale": row.stale,
        "display_projection": row.display_projection,
        "observed_at": isoformat(row.updated_at),
        "confidence": row.confidence,
        "evidence": [],
    }


@router.put("/devices/{device_id}/metadata/live")
async def update_live_metadata(
    device_id: str,
    payload: dict,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Update runtime metadata without any radio playback operation.

    Selection identity is always taken from the current persisted provider
    contract.  The human UI may change only the four confirmed runtime fields;
    it cannot smuggle in a source/provider/station change.
    """

    device_or_404(db, device_id)
    row = (
        db.query(MetadataState)
        .filter(MetadataState.device_id == device_id)
        .one_or_none()
    )
    if row is None or not row.station_id or not row.provider or not row.source:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no active metadata selection",
                "message": "Zuerst einen BASSWIESN-Sender starten; danach können dessen Live-Metadaten ohne Sourcewechsel aktualisiert werden.",
            },
        )
    allowed = {"track": 512, "artist": 512, "album": 512, "imageUrl": 2048}
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported live metadata fields: {', '.join(unknown)}",
        )
    normalized: dict[str, str | None] = {}
    for name, maximum in allowed.items():
        value = payload.get(name)
        if value is None:
            normalized[name] = None
            continue
        value = str(value).strip()
        if len(value) > maximum:
            raise HTTPException(
                status_code=422,
                detail=f"{name} exceeds {maximum} characters",
            )
        normalized[name] = value or None
    image_url = normalized.get("imageUrl")
    if image_url and not re.match(r"^https?://", image_url, flags=re.IGNORECASE):
        raise HTTPException(
            status_code=422,
            detail="imageUrl muss eine HTTP- oder HTTPS-Adresse sein.",
        )
    runtime = getattr(request.app.state, "research_runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail="Live-Metadaten-Runtime ist in diesem Prozess nicht aktiv.",
        )
    snapshot = await runtime.ingest_metadata(
        device_id,
        normalized,
        provenance=MetadataProvenance.BASSWIESN,
        confidence=100,
        station_name=row.station_name,
        station_id=row.station_id,
        provider=row.provider,
        source=row.source,
    )
    return {
        "device_id": device_id,
        "accepted": True,
        "selection": {
            "station_name": row.station_name,
            "station_id": row.station_id,
            "provider": row.provider,
            "source": row.source,
        },
        "metadata": snapshot.as_dict(),
        "coalesced_in_seconds": 2,
        "radio_write": False,
        "playback_action": "NONE",
        "source_change": False,
        "set_url": False,
        "rebuffer_requested": False,
    }


@router.get("/devices/{device_id}/artwork")
async def device_artwork(device_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Resolve live/provider/station artwork into a same-origin WebUI asset."""

    device_or_404(db, device_id)
    row = (
        db.query(MetadataState)
        .filter(MetadataState.device_id == device_id)
        .one_or_none()
    )
    result = await _metadata_artwork_result(db, device_id, row)
    return {"device_id": device_id, **_public_artwork_payload(result)}


@router.get("/devices/{device_id}/artwork/image")
async def device_artwork_image(
    device_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    payload = await device_artwork(device_id, db)
    return RedirectResponse(str(payload["public_url"]), status_code=307)


@router.get("/stations/{station_id}/artwork")
async def station_artwork(station_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    station = db.query(Station).filter(Station.id == station_id).one_or_none()
    if station is None:
        raise HTTPException(status_code=404, detail="station not found")
    result = await _station_artwork_result(db, station)
    return {"station_id": station.id, **_public_artwork_payload(result)}


@router.get("/stations/{station_id}/artwork/image")
async def station_artwork_image(
    station_id: int, db: Session = Depends(get_db)
) -> RedirectResponse:
    payload = await station_artwork(station_id, db)
    return RedirectResponse(str(payload["public_url"]), status_code=307)


@router.get("/devices/{device_id}/metadata/clock")
async def clock_metadata_preference(
    device_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    device_or_404(db, device_id)
    return {
        "device_id": device_id,
        **load_clock_metadata_preference(db, device_id).as_dict(),
        "lab_enabled": clock_metadata_lab_enabled(db),
        "label": "Uhrzeit in Live-Metadaten anzeigen",
        "hardware_validation": "OPEN",
    }


@router.put("/devices/{device_id}/metadata/clock")
async def update_clock_metadata_preference(
    device_id: str, payload: dict, db: Session = Depends(get_db)
) -> dict[str, Any]:
    device_or_404(db, device_id)
    if not clock_metadata_lab_enabled(db):
        raise HTTPException(
            status_code=403,
            detail="Uhr als Live-Metadaten ist ausschließlich im aktivierten LAB-Modus verfügbar.",
        )
    try:
        preference = save_clock_metadata_preference(
            db,
            device_id,
            enabled=payload.get("enabled") is True,
            mode=str(payload.get("mode") or "MISSING_TITLE"),
            interval_seconds=int(payload.get("interval_seconds", 60)),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "device_id": device_id,
        **preference.as_dict(),
        "lab_enabled": True,
        "label": "Uhrzeit in Live-Metadaten anzeigen",
        "hardware_validation": "OPEN",
    }


@router.get("/devices/{device_id}/restrictions")
async def restrictions(device_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    device_or_404(db, device_id)
    db_rows = (
        db.query(RestrictionState)
        .filter(RestrictionState.device_id == device_id)
        .order_by(RestrictionState.source_key)
        .all()
    )
    rows = [
        {
            "source_key": row.source_key,
            "inactivity_timeout": row.inactivity_timeout_s,
            "unit": "seconds",
            "timer_enabled": row.timer_enabled,
            "received_at": isoformat(row.received_at),
            "source": row.origin,
            "effective_until": isoformat(row.effective_until),
            "observed_at": isoformat(row.received_at or row.updated_at),
            "confidence": 100,
            "evidence": load_evidence(row.evidence_json),
        }
        for row in db_rows
    ]
    observed = max(
        (row["observed_at"] for row in rows if row["observed_at"]),
        default=None,
    )
    return {
        "device_id": device_id,
        "state": "PRESENT" if rows else "ABSENT",
        "restrictions": rows,
        "observed_at": observed,
        "confidence": 100 if rows else 0,
        "evidence": [
            {"source_key": row["source_key"], "refs": row["evidence"]}
            for row in rows
        ],
    }


@router.get("/devices/{device_id}/reporting")
async def reporting(device_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    device_or_404(db, device_id)
    db_rows = (
        db.query(ReportingState)
        .filter(ReportingState.device_id == device_id)
        .order_by(ReportingState.provider_id)
        .all()
    )
    rows = [
        {
            "provider_id": row.provider_id,
            "state": row.state,
            "report_url": redact_url(row.report_url),
            "queue_depth": row.queue_depth,
            "retry_count": row.retry_count,
            "next_due_at": isoformat(row.next_due_at),
            "last_http_status": row.last_http_status,
            "last_success": isoformat(row.last_success_at),
            "last_failure": load_evidence(row.last_failure_json),
            "generation": row.generation,
            "observed_at": isoformat(row.updated_at),
            "confidence": 100 if row.last_http_status is not None else 50,
            "evidence": [],
        }
        for row in db_rows
    ]
    state = (
        "FAILED"
        if any(row["state"] == "FAILED" for row in rows)
        else "DEGRADED"
        if any(row["state"] == "DEGRADED" for row in rows)
        else "SUCCESS"
        if rows and all(row["state"] in {"SUCCESS", "RECOVERED"} for row in rows)
        else rows[0]["state"]
        if len(rows) == 1
        else "UNKNOWN"
    )
    return {
        "device_id": device_id,
        "state": state,
        "providers": rows,
        "observed_at": max(
            (row["observed_at"] for row in rows if row["observed_at"]),
            default=None,
        ),
        "confidence": min((row["confidence"] for row in rows), default=0),
        "evidence": [],
    }


def _airplay_label(row: AirPlayReadinessState) -> str:
    if row.product_allowed is False:
        return "Nicht unterstützt"
    if row.blocking_stage == "NONE" and row.audio_ready is True:
        return "Bereit"
    if row.blocking_stage == "UNKNOWN" or row.product_allowed is None:
        return "Unbekannt"
    if any(
        value is False
        for value in (
            row.auth_hardware_detected,
            row.sts_registered,
            row.source_visible,
            row.mdns_visible,
            row.pairing_ready,
            row.ptp_ready,
            row.audio_ready,
        )
    ):
        return f"Blockiert bei: {row.blocking_stage}"
    return "Teilweise bereit"


@router.get("/devices/{device_id}/airplay-readiness")
async def airplay_readiness(
    device_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    device_or_404(db, device_id)
    row = (
        db.query(AirPlayReadinessState)
        .filter(AirPlayReadinessState.device_id == device_id)
        .one_or_none()
    )
    if row is None:
        return _unknown(
            device_id,
            label="Unbekannt",
            firmware_version=None,
            product_id=None,
            variant=None,
            platform=None,
            product_allowed=None,
            auth_hardware_expected=None,
            auth_hardware_detected=None,
            sts_registered=None,
            source_visible=None,
            mdns_visible=None,
            pairing_ready=None,
            ptp_ready=None,
            audio_ready=None,
            blocking_stage="UNKNOWN",
            expires_at=None,
            expired=False,
            provenance="UNKNOWN",
        )
    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    expired = bool(expires_at is not None and expires_at <= datetime.now(UTC))
    effective = None
    if expired:
        effective = assess_airplay_readiness(
            firmware_version=row.firmware_version,
            product_id=row.product_id,
            variant=row.variant,
            platform=row.platform,
            auth_hardware_detected=None,
            sts_registered=None,
            source_visible=None,
            mdns_visible=None,
            pairing_ready=None,
            ptp_ready=None,
            audio_ready=None,
            evidence=tuple(load_evidence(row.evidence_json))
            + ({"source": "transient_airplay_evidence_expired"},),
        )
    transient = {
        "auth_hardware_detected": (
            effective.auth_hardware_detected if effective else row.auth_hardware_detected
        ),
        "sts_registered": effective.sts_registered if effective else row.sts_registered,
        "source_visible": effective.source_visible if effective else row.source_visible,
        "mdns_visible": effective.mdns_visible if effective else row.mdns_visible,
        "pairing_ready": effective.pairing_ready if effective else row.pairing_ready,
        "ptp_ready": effective.ptp_ready if effective else row.ptp_ready,
        "audio_ready": effective.audio_ready if effective else row.audio_ready,
    }
    blocking_stage = effective.blocking_stage.value if effective else row.blocking_stage
    label = effective.user_visible_status if effective else _airplay_label(row)
    confidence = effective.confidence if effective else row.confidence
    return {
        "device_id": device_id,
        "state": "READY" if blocking_stage == "NONE" else "NOT_READY",
        "label": label,
        "firmware_version": row.firmware_version,
        "firmware_build": row.firmware_build,
        "product_id": row.product_id,
        "variant": row.variant,
        "platform": row.platform,
        "product_allowed": row.product_allowed,
        "auth_hardware_expected": row.auth_hardware_expected,
        **transient,
        "blocking_stage": blocking_stage,
        "observed_at": isoformat(row.observed_at),
        "expires_at": isoformat(row.expires_at),
        "expired": expired,
        "provenance": row.provenance,
        "confidence": confidence,
        "evidence": load_evidence(row.evidence_json),
    }


@router.post("/devices/{device_id}/airplay-readiness/probe")
async def probe_airplay_readiness(
    device_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Run one explicitly requested, bounded, selected-device-only probe.

    HTTP identity/source reads are followed by legacy-unicast mDNS questions
    sent only to the selected radio. No SSDP, multicast/subnet scan, SSH or
    CLI access is performed. Unobserved runtime gates remain UNKNOWN.
    """

    device = device_or_404(db, device_id)
    require_unprotected_device(
        device,
        action="AirPlay read-only diagnostics",
        requester="airplay_readiness_probe",
    )
    observed_at = datetime.now(UTC)
    client = SoundTouchClient(
        device.ip_address,
        device_id=device.device_id,
        request_purpose="airplay_readiness_read_only",
        trigger="explicit_webui_action",
        get_timeout=3.0,
    )
    repository = ResearchStateRepository(db)
    try:
        info_xml = await client.get_xml("/info")
        info_root = ET.fromstring(info_xml)
    except Exception as exc:
        repository.record_event(
            device_id=device.device_id,
            domain="AIRPLAY",
            severity="WARNING",
            code="AIRPLAY_READONLY_PROBE_FAILED",
            message="Read-only AirPlay-Prüfung konnte die Geräteinformationen nicht lesen.",
            evidence={"endpoint": "/info", "error_class": exc.__class__.__name__},
            occurred_at=observed_at,
        )
        db.commit()
        raise HTTPException(
            status_code=502,
            detail="Das ausgewählte Radio ist für die Read-only-AirPlay-Prüfung nicht erreichbar.",
        ) from exc

    observed_device_id = str(info_root.attrib.get("deviceID") or "").strip().upper()
    if observed_device_id != str(device.device_id or "").strip().upper():
        repository.record_event(
            device_id=device.device_id,
            domain="AIRPLAY",
            severity="ERROR",
            code="AIRPLAY_IDENTITY_MISMATCH",
            message="AirPlay-Prüfung abgebrochen: Geräteidentität stimmt nicht überein.",
            evidence={"endpoint": "/info", "identity_match": False},
            occurred_at=observed_at,
        )
        db.commit()
        raise HTTPException(
            status_code=409,
            detail="Die gelesene Geräte-ID stimmt nicht mit dem ausgewählten Radio überein. Es wurden keine weiteren Endpunkte gelesen.",
        )

    firmware = _xml_first(info_root, "softwareVersion", "firmwareVersion") or str(device.firmware or "").strip()
    model = _xml_first(info_root, "type") or str(device.model or "").strip()
    variant = _xml_first(info_root, "variant")
    platform = _xml_first(info_root, "moduleType")
    observed_product_id = _xml_first(info_root, "productID", "product_id", "productCode")
    profile_match = detect_profile(
        DeviceFacts(
            device_id=device.device_id,
            ip_address=device.ip_address,
            model=model,
            firmware=firmware,
            product_id=observed_product_id,
            variant=variant,
            platform=platform,
        )
    )
    product_id = observed_product_id or profile_match.product_id
    product_provenance = "RADIO_INFO" if observed_product_id else (
        "CONFIRMED_STATIC_PROFILE" if profile_match.profile is not None else "UNKNOWN"
    )

    endpoint_results: list[dict[str, Any]] = [
        {"endpoint": "/info", "status": "READ", "identity_match": True}
    ]
    source_visible: bool | None = None
    try:
        sources_root = ET.fromstring(await client.get_xml("/sources"))
        source_visible = _airplay_source_visible(sources_root)
        endpoint_results.append(
            {"endpoint": "/sources", "status": "READ", "airplay_visible": source_visible}
        )
    except Exception as exc:
        endpoint_results.append(
            {"endpoint": "/sources", "status": "UNAVAILABLE", "error_class": exc.__class__.__name__}
        )

    try:
        capabilities_root = ET.fromstring(await client.get_xml("/capabilities"))
        endpoint_results.append(
            {
                "endpoint": "/capabilities",
                "status": "READ",
                "airplay_mentioned": _airplay_source_visible(capabilities_root),
            }
        )
    except Exception as exc:
        endpoint_results.append(
            {"endpoint": "/capabilities", "status": "UNAVAILABLE", "error_class": exc.__class__.__name__}
        )

    mdns_result: dict[str, Any]
    try:
        mdns_result = await asyncio.to_thread(
            probe_targeted_airplay_mdns,
            device.ip_address,
            device_id=device.device_id,
        )
    except Exception as exc:
        mdns_result = {
            "targeted": True,
            "transport": "UDP_LEGACY_UNICAST_MDNS",
            "mdns_visible": None,
            "status": "UNAVAILABLE",
            "error_class": exc.__class__.__name__,
        }

    # The Phase-13 chain places Product/Auth and STS registration before
    # internal AIRPLAY source registration. A visible source on an allowed
    # SM2 profile therefore provides strong downstream evidence for both
    # preceding gates, but remains explicitly labelled as inference.
    preliminary = assess_airplay_readiness(
        firmware_version=firmware or None,
        product_id=product_id or None,
        variant=variant or None,
        platform=platform or None,
        source_visible=source_visible,
    )
    downstream_source_inference = bool(
        source_visible is True
        and preliminary.product_allowed is True
        and str(preliminary.platform or "").upper() == "SM2"
    )

    evidence = (
        {
            "source": "explicit_selected_device_read_only_probe",
            "trigger": "visible_user_action",
            "network_scope": "selected_device_only",
            "mdns_scanned": True,
            "mdns_multicast_or_subnet_scan": False,
        },
        {
            "source": "device_identity",
            "firmware": firmware,
            "variant": variant,
            "platform": platform,
            "product_id_provenance": product_provenance,
            "profile_key": profile_match.profile.key if profile_match.profile is not None else None,
        },
        {
            "source": "downstream_airplay_source_gate",
            "auth_hardware_and_sts_inferred": downstream_source_inference,
            "evidence_level": "STRONG_INFERENCE" if downstream_source_inference else "UNKNOWN",
        },
        {"source": "targeted_mdns", **mdns_result},
        *endpoint_results,
    )
    readiness = assess_airplay_readiness(
        firmware_version=firmware or None,
        product_id=product_id or None,
        variant=variant or None,
        platform=platform or None,
        auth_hardware_detected=True if downstream_source_inference else None,
        sts_registered=True if downstream_source_inference else None,
        source_visible=source_visible,
        mdns_visible=mdns_result.get("mdns_visible"),
        evidence=evidence,
    )
    mdns_ttl = mdns_result.get("ttl_seconds")
    runtime_ttl = _AIRPLAY_RUNTIME_TTL_SECONDS
    if isinstance(mdns_ttl, int) and mdns_ttl > 0:
        runtime_ttl = min(runtime_ttl, mdns_ttl)
    repository.upsert_airplay_readiness(
        device.device_id,
        readiness,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(seconds=runtime_ttl),
        provenance="EXPLICIT_READ_ONLY_HTTP_TARGETED_MDNS",
    )
    repository.record_event(
        device_id=device.device_id,
        domain="AIRPLAY",
        code="AIRPLAY_READONLY_PROBE_COMPLETED",
        message="Gezielte Read-only-AirPlay-Prüfung abgeschlossen.",
        evidence={
            "source_visible": source_visible,
            "mdns_visible": mdns_result.get("mdns_visible"),
            "runtime_ttl_seconds": runtime_ttl,
            "product_id_provenance": product_provenance,
            "confidence": readiness.confidence,
        },
        occurred_at=observed_at,
    )
    db.commit()
    return await airplay_readiness(device_id, db)


@router.get("/devices/{device_id}/diagnostics/timeline")
async def diagnostics_timeline(
    device_id: str,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    domain: str | None = Query(default=None, min_length=1, max_length=64),
    severity: str | None = Query(default=None, min_length=1, max_length=32),
    cursor: str | None = Query(default=None, min_length=1, max_length=256),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    device_or_404(db, device_id)
    if from_time and to_time:
        left = from_time.replace(tzinfo=UTC) if from_time.tzinfo is None else from_time
        right = to_time.replace(tzinfo=UTC) if to_time.tzinfo is None else to_time
        if left > right:
            raise HTTPException(status_code=400, detail="from must not be after to")
    query = db.query(DiagnosticEvent).filter(DiagnosticEvent.device_id == device_id)
    if from_time:
        query = query.filter(DiagnosticEvent.occurred_at >= from_time)
    if to_time:
        query = query.filter(DiagnosticEvent.occurred_at <= to_time)
    if domain:
        query = query.filter(DiagnosticEvent.domain == domain.strip().upper())
    if severity:
        query = query.filter(DiagnosticEvent.severity == severity.strip().upper())
    if cursor:
        cursor_time, cursor_id = _decode_timeline_cursor(cursor)
        query = query.filter(
            or_(
                DiagnosticEvent.occurred_at < cursor_time,
                and_(
                    DiagnosticEvent.occurred_at == cursor_time,
                    DiagnosticEvent.id < cursor_id,
                ),
            )
        )
    rows = query.order_by(DiagnosticEvent.occurred_at.desc(), DiagnosticEvent.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "id": row.id,
            "event_id": row.event_id,
            "device_id": device_id,
            "occurred_at": isoformat(row.occurred_at),
            "domain": row.domain,
            "severity": row.severity,
            "code": row.code,
            "message": row.message,
            "correlation_id": row.correlation_id,
            "confidence": next(
                (
                    int(item.get("confidence"))
                    for item in load_evidence(row.evidence_json)
                    if isinstance(item.get("confidence"), (int, float))
                ),
                100,
            ),
            "evidence": load_evidence(row.evidence_json),
            "redacted": True,
        }
        for row in rows
    ]
    return {
        "device_id": device_id,
        "state": "AVAILABLE",
        "items": items,
        "next_cursor": _encode_timeline_cursor(rows[-1]) if has_more and rows else None,
        "observed_at": items[0]["occurred_at"] if items else None,
        "confidence": 100,
        "evidence": [],
    }
