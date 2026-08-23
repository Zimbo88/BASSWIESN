from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import asyncio
from hashlib import sha256
import json
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4
from xml.etree import ElementTree as ET

from sqlalchemy.orm import Session

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.config import get_settings
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.models import (
    Device,
    DiagnosticEvent,
    MetadataState,
    PlaybackHealthState,
    ReportingState,
    RestrictionState,
    Station,
    TelemetryEvent,
    utc_now,
)
from basswiesn.app.repositories.research_state_repository import ResearchStateRepository
from basswiesn.app.services.playback_state import close_open_sessions, confirm_playback_session, is_confirmed_playing
from basswiesn.app.services.device_state import load_runtime_state, runtime_from_now_playing, save_runtime_state
from basswiesn.app.services.device_state import (
    merge_provider_maps,
    parse_service_availability_xml,
    parse_sources_xml,
)
from basswiesn.app.services.device_policy import (
    device_lock,
    policy_for_device,
    recommended_backoff_seconds,
    should_poll,
)
from basswiesn.app.services.protected_devices import is_protected_device
from basswiesn.app.services.health_models import (
    MetadataHealth,
    PlaybackSignals,
    ProviderSignals,
    ReportingHealth,
    classify_invalid_source,
    reduce_playback_health,
    reduce_provider_health,
)
from basswiesn.app.services.provider_registry import SERVICE_MANIFEST
from basswiesn.app.services.metadata_engine import MetadataProvenance
from basswiesn.app.services.recovery import (
    RecoveryReason,
    RecoveryStage,
    plan_recovery,
)
from basswiesn.app.services.stream_compat import resolve_stream_url
from basswiesn.app.services.orion import (
    StationDescriptor,
    decode_orion_data,
    station_contract_key,
)
from basswiesn.app.services.research_runtime import (
    project_airplay_readiness_from_persisted,
)
from basswiesn.app.routers.shared import summarize_payload


PLAYBACK_KEY_PREFIX = "device:"
PLAYBACK_KEY_SUFFIX = ":runtime_state"
OFFLINE_FAILURE_THRESHOLD = 3
KEEPALIVE_PAUSE_FAILURE_THRESHOLD = 5
KEEPALIVE_BACKOFF_SECONDS = (5 * 60, 15 * 60, 30 * 60, 60 * 60)
_last_ok_log: dict[str, datetime] = {}


@dataclass
class PlaybackSession:
    session_id: str
    content_session_id: str
    source_binding_id: str
    source: str
    started_at: str
    current_stream_index: int
    retry_count: int
    play_state: str


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _error_text(exc: BaseException) -> str:
    return str(exc).strip() or exc.__class__.__name__


def _backoff_seconds(failures: int) -> int:
    if failures < KEEPALIVE_PAUSE_FAILURE_THRESHOLD:
        return 0
    index = min(max(failures - KEEPALIVE_PAUSE_FAILURE_THRESHOLD, 0), len(KEEPALIVE_BACKOFF_SECONDS) - 1)
    return KEEPALIVE_BACKOFF_SECONDS[index]


def _is_playing(runtime: dict) -> bool:
    return is_confirmed_playing(reachable=True, current_source=runtime.get("current_source"), playback_state=runtime.get("playback_state"), play_status=runtime.get("play_status"), state_observed_at=datetime.now(UTC), now=datetime.now(UTC), stale_after_seconds=30)


def _source(runtime: dict) -> str:
    return str(runtime.get("current_source") or "").upper()


def _playback_state(runtime: dict) -> str:
    return str(runtime.get("playback_state") or "").upper()


def _explicit_position(runtime: dict[str, Any]) -> int | None:
    raw = runtime.get("now_playing")
    if not isinstance(raw, dict):
        return None
    folded = {
        str(key).rsplit("}", 1)[-1].strip().casefold(): value
        for key, value in raw.items()
    }
    for name in ("position", "positionms", "elapsedtime", "elapsedtimems"):
        value = folded.get(name)
        if value in (None, ""):
            continue
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            continue
    return None


def _progress_evidence(
    keepalive: dict[str, Any], runtime: dict[str, Any], now: datetime
) -> tuple[bool | None, float | None, dict[str, Any]]:
    status = _playback_state(runtime)
    position = _explicit_position(runtime)
    if status in {"BUFFERING", "BUFFERING_STATE"}:
        started = _parse_iso(keepalive.get("buffering_since")) or now
        return None, max(0.0, (now - started).total_seconds()), {
            "buffering_since": started.isoformat(),
            "explicit_position": position,
        }
    if status not in {"PLAYING", "PLAY_STATE"} or position is None:
        return None, None, {"buffering_since": "", "explicit_position": position}
    previous = keepalive.get("explicit_position")
    previous_at = _parse_iso(keepalive.get("position_observed_at"))
    if previous is None or previous_at is None or int(previous) != position:
        return True, 0.0, {
            "buffering_since": "",
            "explicit_position": position,
            "position_observed_at": now.isoformat(),
            "position_stagnant_since": "",
        }
    stagnant = _parse_iso(keepalive.get("position_stagnant_since")) or previous_at
    return False, max(0.0, (now - stagnant).total_seconds()), {
        "buffering_since": "",
        "explicit_position": position,
        "position_observed_at": previous_at.isoformat(),
        "position_stagnant_since": stagnant.isoformat(),
    }


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _radio_metadata_observation(runtime: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize only runtime fields actually present in radio readback.

    SoundTouch XML tag casing varies between firmware paths.  Station/source
    identity remains outside this payload so no station label is mistaken for
    a track title.
    """

    raw = runtime.get("now_playing")
    if not isinstance(raw, dict):
        return None
    fields = {
        str(key).rsplit("}", 1)[-1].strip().casefold(): value
        for key, value in raw.items()
    }
    aliases = {
        "track": ("track", "title"),
        "artist": ("artist",),
        "album": ("album",),
        "imageUrl": ("imageurl", "arturl", "art"),
    }
    result: dict[str, Any] = {}
    for target, candidates in aliases.items():
        for candidate in candidates:
            if candidate not in fields:
                continue
            value = str(fields[candidate] or "").strip()
            # Presence and value are separate in protobuf/XML optionals.  An
            # explicit empty known tag clears the old value; an absent tag
            # still leaves it untouched in normalize_metadata().
            result[target] = value or None
            break
    return result or None


def _radio_metadata_selection(runtime: dict[str, Any]) -> tuple[str | None, str | None]:
    now_playing = runtime.get("now_playing")
    selected = runtime.get("selected_content_item")
    selection_id = None
    if isinstance(selected, dict):
        location = str(selected.get("location") or "").strip()
        selection_id = _station_contract_from_location(location)
        if selection_id is None and location:
            # Preserve identity without storing a possibly credential-bearing
            # provider URL as diagnostics/state.
            selection_id = f"radio-location:{sha256(location.encode('utf-8')).hexdigest()}"
    station_name = None
    if isinstance(now_playing, dict):
        folded = {
            str(key).rsplit("}", 1)[-1].strip().casefold(): value
            for key, value in now_playing.items()
        }
        station_name = _first_text(
            folded.get("stationname"), folded.get("itemname")
        ) or None
    return selection_id, station_name


def _history_identity_payload(stored: dict, runtime: dict, pending: dict) -> dict:
    now_playing = runtime.get("now_playing") or stored.get("now_playing") or {}
    selected = runtime.get("selected_content_item") or stored.get("selected_content_item") or {}
    station_name = _first_text(
        pending.get("station_name"),
        now_playing.get("stationName"),
        now_playing.get("station_name"),
        now_playing.get("itemName"),
        now_playing.get("sourceTitle"),
    )
    stream_url = _first_text(
        pending.get("stream_url"),
        selected.get("location"),
        now_playing.get("streamUrl"),
        now_playing.get("stream_url"),
    )
    source_account = _first_text(pending.get("source_account"), selected.get("sourceAccount"))
    content_item_name = _first_text(pending.get("content_item_name"), now_playing.get("itemName"), station_name)
    return {
        "station_id": pending.get("station_id"),
        "station_name": station_name,
        "stream_url": stream_url,
        "source_account": source_account,
        "content_item_name": content_item_name,
        "trigger": pending.get("trigger", "live_state"),
        "trigger_type": pending.get("trigger_type", "station"),
        "internal_event": bool(pending.get("internal_event", False)),
    }


def _manual_stop_seen(runtime: dict) -> bool:
    if _source(runtime) == "INVALID_SOURCE":
        return False
    state = _playback_state(runtime)
    return _source(runtime) == "STANDBY" or state in {"STOP_STATE", "PAUSE_STATE", "STANDBY"}


def _session_id(prefix: str, device_id: str, started_at: datetime | None) -> str:
    seed = int((started_at or datetime.now(UTC)).timestamp())
    return f"{prefix}-{device_id}-{seed}"


def _session_payload(device_id: str, source: str, play_state: str, started_at: datetime | None, keepalive: dict) -> dict:
    previous = keepalive.get("playback_session") or {}
    previous_playing = bool(keepalive.get("playing"))
    session_id = str(previous.get("session_id") or keepalive.get("playback_session_id") or "")
    content_session_id = str(previous.get("content_session_id") or keepalive.get("content_session_id") or "")
    source_binding_id = str(previous.get("source_binding_id") or keepalive.get("source_binding_id") or "")
    if not previous_playing or not session_id:
        session_id = _session_id("playback", device_id, started_at)
    if not previous_playing or not content_session_id:
        content_session_id = _session_id("content", device_id, started_at)
    if not previous_playing or not source_binding_id or str(previous.get("source") or "") != source:
        source_binding_id = _session_id("binding", device_id, started_at)
    return asdict(PlaybackSession(
        session_id=session_id,
        content_session_id=content_session_id,
        source_binding_id=source_binding_id,
        source=source,
        started_at=started_at.isoformat() if started_at else "",
        current_stream_index=int(previous.get("current_stream_index") or 0),
        retry_count=0,
        play_state=play_state,
    ))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _station_contract_from_db_id(db: Session, value: Any) -> str | None:
    try:
        station_id = int(value)
    except (TypeError, ValueError):
        return str(value).strip() or None
    station = db.query(Station).filter(Station.id == station_id).one_or_none()
    if station is None:
        return None
    return station_contract_key(
        StationDescriptor(
            station.name,
            station.stream_url,
            station.image_url,
            station.provider_station_id,
            stream_url_resolved=station.stream_url_resolved,
            stream_format=station.stream_format,
            stream_mime=station.stream_mime,
            compatibility_warning=station.compatibility_warning,
        )
    )


def _station_contract_from_location(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    marker = "/station/"
    if marker in parsed.path and (
        "/now-playing/" in parsed.path or "/reporting/" in parsed.path
    ):
        return unquote(parsed.path.rsplit(marker, 1)[-1]).strip() or None
    encoded = parse_qs(parsed.query).get("data", [""])[0]
    if not encoded:
        return None
    try:
        decoded = decode_orion_data(encoded)
    except Exception:
        return None
    if not isinstance(decoded, dict):
        return None
    return station_contract_key(
        StationDescriptor(
            name=str(decoded.get("name") or "Custom Station"),
            stream_url=str(decoded.get("streamUrl") or ""),
            image_url=str(decoded.get("imageUrl") or ""),
            tunein_id=str(decoded.get("tuneinId") or ""),
            stream_url_resolved=str(decoded.get("streamUrlResolved") or ""),
            stream_format=str(decoded.get("streamFormat") or ""),
            stream_mime=str(decoded.get("streamMime") or ""),
            compatibility_warning=str(decoded.get("compatibilityWarning") or ""),
        )
    )


def _restriction_selection_identities(
    db: Session,
    device_id: str,
    runtime: dict[str, Any],
    stored: dict[str, Any],
) -> list[tuple[str, str]]:
    """Return ordered, explicit station identities for this readback."""

    candidates: list[tuple[str, str | None]] = []
    pending = stored.get("playback_pending")
    if isinstance(pending, dict):
        candidates.append(
            ("playback_pending.station_id", _station_contract_from_db_id(db, pending.get("station_id")))
        )
    selected = runtime.get("selected_content_item")
    if isinstance(selected, dict):
        candidates.append(
            ("radio_content_item.location", _station_contract_from_location(selected.get("location")))
        )
    metadata = (
        db.query(MetadataState)
        .filter(MetadataState.device_id == device_id)
        .one_or_none()
    )
    if metadata is not None:
        candidates.append(("metadata.station_id", str(metadata.station_id or "").strip() or None))
    candidates.append(
        (
            "runtime.current_station_id",
            str(stored.get("current_station_id") or "").strip() or None,
        )
    )

    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for origin, candidate in candidates:
        normalized = str(candidate or "").strip()
        if not normalized or normalized.casefold() in seen:
            continue
        seen.add(normalized.casefold())
        result.append((origin, normalized))
    return result


def _restriction_observation(
    db: Session,
    device_id: str,
    source: str,
    now: datetime,
    *,
    selection_identities: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Return current persisted restriction evidence without inventing a timeout.

    Selection-scoped provider rows are matched only through an explicit
    station identity.  A legacy unscoped row may still match the source, but
    row count is never used as an identity guess.  This helper never turns the
    timeout into a playback-control action.
    """

    rows = (
        db.query(RestrictionState)
        .filter(RestrictionState.device_id == device_id)
        .order_by(RestrictionState.received_at.desc(), RestrictionState.updated_at.desc())
        .all()
    )
    source_key = str(source or "").strip().upper()
    normalized_rows = {
        str(candidate.source_key or "").strip().casefold(): candidate
        for candidate in rows
    }
    row = None
    identity_origin = None
    identity_value = None
    for origin, selection_id in selection_identities or []:
        scoped = f"{source_key}:{selection_id}".casefold()
        if scoped in normalized_rows:
            row = normalized_rows[scoped]
            identity_origin = origin
            identity_value = selection_id
            break
    if row is None:
        row = normalized_rows.get(source_key.casefold())
        if row is not None:
            identity_origin = "legacy_unscoped_source"
    if row is None:
        return {
            "state": "UNKNOWN",
            "inactivity_timeout_s": None,
            "effective_until": None,
            "origin": "ABSENT",
            "source_key": None,
            "selection_identity": None,
            "identity_source": None,
            "candidate_count": len(selection_identities or []),
        }

    timeout = row.inactivity_timeout_s
    enabled = bool(row.timer_enabled and timeout is not None and int(timeout) > 0)
    effective_until = _as_utc(row.effective_until)
    timer_started_at = _as_utc(row.timer_started_at)
    received_at = _as_utc(row.received_at)
    if not enabled:
        state = "DISABLED"
    elif timer_started_at is None:
        state = "PENDING_PLAY"
    elif effective_until is None:
        state = "ACTIVE_DEADLINE_UNREPRESENTABLE"
    elif now >= effective_until:
        state = "EXPIRED"
    else:
        state = "ACTIVE"
    return {
        "state": state,
        "inactivity_timeout_s": int(timeout) if timeout is not None else None,
        "effective_until": effective_until.isoformat() if effective_until else None,
        "timer_started_at": timer_started_at.isoformat() if timer_started_at else None,
        "origin": row.origin,
        "received_at": received_at.isoformat() if received_at else None,
        "source_key": row.source_key,
        "selection_identity": identity_value,
        "identity_source": identity_origin,
        "candidate_count": len(selection_identities or []),
    }


def _provider_available(stored: dict[str, Any], source: str) -> bool | None:
    providers = stored.get("providers")
    if not isinstance(providers, dict):
        return None
    item = next(
        (
            value
            for key, value in providers.items()
            if str(key).strip().upper() == str(source or "").strip().upper()
            and isinstance(value, dict)
        ),
        None,
    )
    if item is None or not isinstance(item.get("available"), bool):
        return None
    return item["available"]


def _radio_source_items(xml: str) -> set[str] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    root_tag = str(root.tag).rsplit("}", 1)[-1].casefold()
    items: set[str] = set()
    contract_observed = root_tag in {"sources", "sourcelist"}
    for node in root.iter():
        tag = str(node.tag).rsplit("}", 1)[-1].casefold()
        if tag not in {"source", "sourceitem"}:
            continue
        contract_observed = True
        name = str(
            node.attrib.get("source")
            or node.attrib.get("type")
            or node.findtext("name", "")
            or ""
        ).strip().upper()
        if name:
            items.add(name)
    return items if contract_observed else None


def _service_availability(xml: str, source: str) -> bool | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    wanted = str(source or "").strip().upper()
    matched = []
    for node in root.iter():
        name = str(node.attrib.get("service") or node.attrib.get("source") or node.attrib.get("name") or "").strip().upper()
        if name != wanted:
            continue
        raw = str(node.attrib.get("available") or node.attrib.get("status") or node.text or "").strip().lower()
        if raw in {"true", "available", "ready", "1"}:
            matched.append(True)
        elif raw in {"false", "unavailable", "failed", "0"}:
            matched.append(False)
    return matched[-1] if matched else None


async def _collect_invalid_source_evidence(
    client: SoundTouchClient,
    db: Session,
    device: Device,
    previous_source: str,
    reads: list[str],
) -> dict[str, Any]:
    """Collect abnormal-path evidence only; every unknown remains nullable."""

    captured: dict[str, str] = {}
    errors: dict[str, str] = {}
    for endpoint in ("/sources", "/serviceAvailability", "/info"):
        try:
            captured[endpoint] = await _read_endpoint(client, endpoint, reads)
        except Exception as exc:
            errors[endpoint] = exc.__class__.__name__
    source_items = _radio_source_items(captured.get("/sources", ""))
    source_removed: bool | None = (
        previous_source not in source_items
        if source_items is not None and previous_source
        else None
    )
    account_available = None
    try:
        info = ET.fromstring(captured.get("/info", ""))
        account_node = info.find(".//margeAccountUUID")
        if account_node is not None:
            account_available = bool(str(account_node.text or "").strip())
    except ET.ParseError:
        pass
    provider_available = _service_availability(
        captured.get("/serviceAvailability", ""), previous_source
    )
    reporting = (
        db.query(ReportingState)
        .filter(
            ReportingState.device_id == device.device_id,
            ReportingState.provider_id == previous_source,
        )
        .one_or_none()
    )
    reporting_state = str(reporting.state or "").upper() if reporting else ""
    semantic_reporting_failure = bool(
        reporting
        and reporting_state in {"DEGRADED", "FAILED"}
        and reporting.last_http_status is not None
        and 400 <= int(reporting.last_http_status) < 500
    )
    return {
        "provider_available": provider_available,
        "account_available": account_available,
        "source_removed": source_removed,
        "reporting_semantic_persistent": semantic_reporting_failure,
        "evidence": [
            {
                "type": "RADIO_SOURCE_LIST",
                "observed": source_items is not None,
                "previous_source_visible": (
                    previous_source in source_items if source_items is not None else None
                ),
                "source_count": len(source_items) if source_items is not None else None,
            },
            {
                "type": "RADIO_ACCOUNT_READBACK",
                "account_available": account_available,
            },
            {
                "type": "SERVICE_AVAILABILITY_READBACK",
                "provider": previous_source or None,
                "available": provider_available,
            },
            {
                "type": "REPORTING_STATE",
                "state": reporting_state or None,
                "last_http_status": reporting.last_http_status if reporting else None,
                "semantic_persistent": semantic_reporting_failure,
            },
            {"type": "COLLECTOR_ERRORS", "errors": errors},
        ],
    }


def _recovery_station(
    db: Session, stored: dict[str, Any], runtime: dict[str, Any]
) -> Station | None:
    pending = stored.get("playback_pending")
    if isinstance(pending, dict):
        try:
            station_id = int(pending.get("station_id"))
        except (TypeError, ValueError):
            station_id = 0
        if station_id:
            row = db.query(Station).filter(Station.id == station_id).one_or_none()
            if row is not None:
                return row
    metadata = stored.get("current_station_id")
    try:
        station_id = int(metadata)
    except (TypeError, ValueError):
        station_id = 0
    if station_id:
        return db.query(Station).filter(Station.id == station_id).one_or_none()
    selected = runtime.get("selected_content_item")
    location = str(selected.get("location") or "") if isinstance(selected, dict) else ""
    if location:
        return (
            db.query(Station)
            .filter(
                (Station.stream_url == location)
                | (Station.stream_url_original == location)
                | (Station.stream_url_resolved == location)
            )
            .first()
        )
    return None


async def _run_safe_automatic_recovery(
    *,
    device: Device,
    db: Session,
    client: SoundTouchClient,
    runtime: dict[str, Any],
    stored: dict[str, Any],
    research_runtime: Any,
    reason: RecoveryReason,
    source: str,
) -> dict[str, Any] | None:
    if research_runtime is None:
        return None
    plan = plan_recovery(
        reason=reason,
        requested_max_stage=RecoveryStage.STREAM_RERESOLVE,
        automatic=True,
        protected_device=is_protected_device(device),
    )
    initial_position = _explicit_position(runtime)
    initial_status = _playback_state(runtime)

    async def readback() -> dict[str, Any]:
        xml = await client.get_xml("/now_playing")
        observed = runtime_from_now_playing(xml)
        return {
            "source": _source(observed),
            "radio_status": _playback_state(observed),
            "authoritative": True,
        }

    async def metadata_refresh() -> dict[str, Any]:
        xml = await client.get_xml("/now_playing")
        observed = runtime_from_now_playing(xml)
        payload = _radio_metadata_observation(observed)
        if payload:
            selection_id, station_name = _radio_metadata_selection(observed)
            await research_runtime.ingest_metadata(
                device.device_id,
                payload,
                provenance=MetadataProvenance.RADIO,
                confidence=100,
                observed_at=datetime.now(UTC),
                station_name=station_name,
                station_id=selection_id,
                provider=source or None,
                source=source or None,
            )
        return {"metadata_observed": bool(payload), "playback_action": "NONE"}

    async def provider_refresh() -> dict[str, Any]:
        observed_at = datetime.now(UTC).isoformat()
        sources_xml = await client.get_xml("/sources")
        service_xml = await client.get_xml("/serviceAvailability")
        source_state, source_providers = parse_sources_xml(
            sources_xml, observed_at
        )
        service_state, service_providers = parse_service_availability_xml(
            service_xml, observed_at
        )
        providers = merge_provider_maps(source_providers, service_providers)
        stored["provider_state"] = source_state
        stored["service_availability"] = service_state
        stored["providers"] = providers
        save_runtime_state(db, device.device_id, stored, commit=True)
        return {
            "provider": source or None,
            "provider_observed": source.upper()
            in {str(key).upper() for key in providers},
            "playback_action": "NONE",
        }

    async def stream_reresolve() -> dict[str, Any]:
        station = _recovery_station(db, stored, runtime)
        if station is None or not str(station.stream_url or "").strip():
            return {"station_identified": False, "playback_action": "NONE"}
        before = str(station.stream_url_resolved or "")
        analysis = await resolve_stream_url(str(station.stream_url))
        resolved = str(analysis.stream_url_resolved or "")
        station.stream_url_resolved = resolved
        station.stream_format = analysis.stream_format
        station.stream_mime = analysis.stream_mime
        station.stream_codec = analysis.stream_codec
        station.compatibility_score = analysis.compatibility_score
        station.compatibility_warning = analysis.compatibility_warning
        station.is_hls = analysis.is_hls
        station.is_direct_audio = analysis.is_direct_audio
        db.commit()
        return {
            "station_identified": True,
            "resolved": bool(resolved),
            "changed": before != resolved,
            "compatibility_score": analysis.compatibility_score,
            "unsupported": bool(analysis.is_hls or analysis.compatibility_score < 30),
            "playback_action": "NONE",
        }

    async def recovered(_stage: RecoveryStage, result: dict[str, Any] | None) -> bool:
        del result
        xml = await client.get_xml("/now_playing")
        observed = runtime_from_now_playing(xml)
        observed_source = _source(observed)
        observed_status = _playback_state(observed)
        source_valid = bool(
            observed_source and observed_source != "INVALID_SOURCE"
        )
        if not source_valid:
            return False
        if reason == RecoveryReason.STREAM_FAILURE:
            observed_position = _explicit_position(observed)
            if initial_position is not None and observed_position is not None:
                return observed_position != initial_position
            # A transition out of an authoritative buffering state is useful
            # progress evidence. A repeated PLAY_STATE without a position is
            # not enough to claim that a previously stalled stream recovered.
            return bool(
                initial_status in {"BUFFERING", "BUFFERING_STATE"}
                and observed_status in {"PLAYING", "PLAY_STATE"}
            )
        return observed_status not in {"BUFFERING", "BUFFERING_STATE"}

    run = await research_runtime.execute_recovery(
        device.device_id,
        plan,
        actions={
            RecoveryStage.READBACK: readback,
            RecoveryStage.METADATA_REFRESH: metadata_refresh,
            RecoveryStage.PROVIDER_REFRESH: provider_refresh,
            RecoveryStage.STREAM_RERESOLVE: stream_reresolve,
        },
        recovered=recovered,
        provider_id=source or None,
        source=source or None,
        correlation_id=f"keepalive:{device.device_id}:{int(datetime.now(UTC).timestamp())}",
    )
    return {
        "operation_id": run.operation_id,
        "status": run.status.value,
        "stage": int(run.current_stage),
        "automatic": True,
        "radio_write": False,
    }


def _provider_health_projection(
    db: Session,
    device_id: str,
    stored: dict[str, Any],
    *,
    current_source: str,
    previous_source: str,
    observed_at: datetime,
):
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
        if metadata_matches and bool(metadata_row.stale)
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
    evidence = [
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
            "updated_at": (
                _as_utc(metadata_row.updated_at).isoformat()
                if metadata_matches and metadata_row.updated_at is not None
                else None
            ),
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


def _add_diagnostic_event(
    db: Session,
    *,
    device_id: str,
    occurred_at: datetime,
    code: str,
    message: str,
    evidence: list[dict[str, Any]],
    severity: str = "WARNING",
) -> None:
    db.add(
        DiagnosticEvent(
            event_id=str(uuid4()),
            occurred_at=occurred_at,
            domain="PLAYBACK",
            severity=severity,
            device_id=device_id,
            code=code,
            message=message,
            evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            redacted=True,
        )
    )


async def _read_endpoint(client: SoundTouchClient, endpoint: str, reads: list[str]) -> str:
    reads.append(endpoint)
    return await client.get_xml(endpoint)


def _client_for_device(device: Device, policy, purpose: str, client_factory):
    context = policy.to_dict()
    try:
        return client_factory(
            device.ip_address,
            device_id=device.device_id,
            request_purpose=purpose,
            trigger="background",
            policy_context=context,
        )
    except TypeError:
        return client_factory(device.ip_address)


async def run_playback_keepalive_for_device(
    device: Device,
    db: Session,
    *,
    now: datetime | None = None,
    client_factory=SoundTouchClient,
    research_runtime=None,
) -> dict:
    now = _as_utc(now) or datetime.now(UTC)
    if is_protected_device(device):
        write_masterlog(
            "device_poll_skipped",
            device_id=device.device_id,
            radio_ip=device.ip_address,
            request_purpose="playback_keepalive",
            trigger="background",
            reason="fully protected device",
        )
        return {
            "device_id": device.device_id,
            "ok": False,
            "skipped": True,
            "protected": True,
            "reason": "fully protected device",
            "reads": [],
        }
    _row, stored = load_runtime_state(db, device.device_id)
    keepalive = stored.get("playback_keepalive") or {}
    policy = policy_for_device(device, db, runtime_state=stored, now=now)
    last_probe_at = _parse_iso(keepalive.get("last_keepalive_at"))
    decision = should_poll(policy, now=now, last_probe_at=last_probe_at)
    if not decision.allowed:
        keepalive.update({
            "paused": True,
            "skip_reason": decision.reason,
            "last_skip_at": now.isoformat(),
            "polling_profile": decision.profile.value,
            "circuit_state": policy.circuit_state.value,
            "safe_mode_active": policy.safe_mode_active,
            "device_class": policy.device_class.value,
            "next_retry_at": policy.next_retry_at or keepalive.get("next_retry_at", ""),
            "backoff_seconds": policy.backoff_seconds,
        })
        stored["playback_keepalive"] = keepalive
        save_runtime_state(db, device.device_id, stored, commit=True)
        write_masterlog(
            "device_poll_skipped",
            device_id=device.device_id,
            radio_ip=device.ip_address,
            device_class=policy.device_class.value,
            request_purpose="playback_keepalive",
            trigger="background",
            polling_profile=decision.profile.value,
            safe_mode_active=policy.safe_mode_active,
            circuit_breaker_state=policy.circuit_state.value,
            reason=decision.reason,
            next_retry_at=keepalive.get("next_retry_at", ""),
        )
        return {
            "device_id": device.device_id,
            "ok": False,
            "skipped": True,
            "paused": True,
            "reason": decision.reason,
            "polling_profile": decision.profile.value,
            "safe_mode_active": policy.safe_mode_active,
            "circuit_state": policy.circuit_state.value,
            "reads": [],
        }
    lock = device_lock(device.device_id)
    if lock.locked():
        return {
            "device_id": device.device_id,
            "ok": False,
            "skipped": True,
            "reason": "device poll already running",
            "polling_profile": policy.polling_profile.value,
            "safe_mode_active": policy.safe_mode_active,
            "circuit_state": policy.circuit_state.value,
            "reads": [],
        }
    async with lock:
        return await _run_playback_keepalive_for_device_locked(
            device,
            db,
            now=now,
            client_factory=client_factory,
            policy=policy,
            research_runtime=research_runtime,
        )


async def _run_playback_keepalive_for_device_locked(
    device: Device,
    db: Session,
    *,
    now: datetime,
    client_factory,
    policy,
    research_runtime=None,
) -> dict:
    _row, stored = load_runtime_state(db, device.device_id)
    keepalive = stored.get("playback_keepalive") or {}
    next_retry_at = _parse_iso(keepalive.get("next_retry_at"))
    if next_retry_at and now < next_retry_at:
        return {"device_id": device.device_id, "ok": False, "paused": True, "failure_count": int(keepalive.get("consecutive_failures") or 0), "next_retry_at": next_retry_at.isoformat(), "reads": []}
    client = _client_for_device(device, policy, "playback_keepalive", client_factory)
    reads: list[str] = []
    try:
        now_xml = await _read_endpoint(client, "/now_playing", reads)
        volume_xml = await _read_endpoint(client, "/volume", reads)
        runtime = runtime_from_now_playing(now_xml)
        playing = _is_playing(runtime)
        previous_started = _parse_iso(keepalive.get("playback_started_at"))
        previous_playing = bool(keepalive.get("playing"))
        started_at = previous_started if playing and previous_playing and previous_started else now if playing else None
        duration = int((now - started_at).total_seconds()) if playing and started_at else 0
        current_source = _source(runtime)
        play_state = _playback_state(runtime)
        position_advancing, progress_observed_for_s, progress_state = (
            _progress_evidence(keepalive, runtime, now)
        )
        previous_source = str(keepalive.get("last_source") or "").upper()
        restriction_source = (
            previous_source if current_source == "INVALID_SOURCE" else current_source
        )
        restriction = _restriction_observation(
            db,
            device.device_id,
            restriction_source,
            now,
            selection_identities=_restriction_selection_identities(
                db, device.device_id, runtime, stored
            ),
        )
        restriction_source_key = str(restriction.get("source_key") or "")
        previous_restriction_source_key = str(
            keepalive.get("last_restriction_source_key") or ""
        )
        selection_changed = bool(
            previous_restriction_source_key
            and restriction_source_key
            and restriction_source_key != previous_restriction_source_key
        )
        if playing and restriction_source_key and (
            restriction.get("state") == "PENDING_PLAY"
            or not previous_playing
            or selection_changed
        ):
            timer_anchor = now if (not previous_playing or selection_changed) else started_at
            ResearchStateRepository(db).set_restriction_timer(
                device.device_id,
                restriction_source_key,
                play_started_at=timer_anchor,
                reason=(
                    "selection_play_readback"
                    if selection_changed
                    else "play_transition_readback"
                ),
            )
            restriction = _restriction_observation(
                db,
                device.device_id,
                restriction_source,
                now,
                selection_identities=_restriction_selection_identities(
                    db, device.device_id, runtime, stored
                ),
            )
        observation_status = "playing_observed" if playing else "stopped_observed"
        if restriction["state"] == "EXPIRED":
            observation_status = "restriction_expired_observed"
            expired_marker = restriction.get("effective_until") or restriction.get("received_at")
            if keepalive.get("last_restriction_expired_marker") != expired_marker:
                evidence = [
                    {
                        "type": "BMX_RESTRICTION",
                        "source": restriction_source,
                        **restriction,
                    }
                ]
                db.add(
                    TelemetryEvent(
                        device_id=device.device_id,
                        event_type="restriction_expired_observed",
                        endpoint="/now_playing",
                        payload=summarize_payload(now_xml),
                        parsed_summary="automatic_action=NONE",
                    )
                )
                _add_diagnostic_event(
                    db,
                    device_id=device.device_id,
                    occurred_at=now,
                    code="INACTIVITY_RESTRICTION_EXPIRED",
                    message="Der gespeicherte Provider-Inaktivitaetstimer ist abgelaufen; BASSWIESN fuehrt keine automatische Wiedergabeaktion aus.",
                    evidence=evidence,
                )
                keepalive["last_restriction_expired_event_at"] = now.isoformat()
                keepalive["last_restriction_expired_marker"] = expired_marker
        if not playing and restriction_source_key and restriction.get("timer_started_at"):
            ResearchStateRepository(db).set_restriction_timer(
                device.device_id,
                restriction_source_key,
                play_started_at=None,
                reason="pause_stop_or_deactivate_readback",
            )
        manual_stop = bool(keepalive.get("manual_stop")) or _manual_stop_seen(runtime)
        current_preset = runtime.get("current_preset") or keepalive.get("last_preset_slot") or stored.get("current_preset")
        try:
            preset_slot = int(current_preset or 0)
        except (TypeError, ValueError):
            preset_slot = 0
        invalid_diagnosis = None
        invalid_source_transition = current_source == "INVALID_SOURCE" and previous_source != "INVALID_SOURCE"
        if invalid_source_transition:
            observation_status = "invalid_source_diagnosis_required"
            evidence = [
                {"type": "RADIO_READBACK", "source": current_source, "play_state": play_state},
                {"type": "PREVIOUS_SOURCE", "source": previous_source or None},
                {"type": "BMX_RESTRICTION", **restriction},
            ]
            collected = await _collect_invalid_source_evidence(
                client, db, device, previous_source, reads
            )
            evidence.extend(collected["evidence"])
            invalid_diagnosis = classify_invalid_source(
                restriction_expired=(
                    restriction["state"] == "EXPIRED"
                    and bool(restriction.get("source_key"))
                ),
                provider_available=(
                    collected["provider_available"]
                    if collected["provider_available"] is not None
                    else _provider_available(stored, previous_source)
                ),
                account_available=collected["account_available"],
                reporting_semantic_persistent=collected[
                    "reporting_semantic_persistent"
                ],
                source_removed=collected["source_removed"] is True,
                evidence=evidence,
            )
            diagnosis_evidence = {
                "type": "INVALID_SOURCE_DIAGNOSIS",
                "cause": invalid_diagnosis.cause.value,
                "confidence": invalid_diagnosis.confidence,
                "user_visible_reason": invalid_diagnosis.user_visible_reason,
                "automatic_action": "NONE",
            }
            evidence.append(diagnosis_evidence)
            db.add(
                TelemetryEvent(
                    device_id=device.device_id,
                    event_type="invalid_source_observed",
                    endpoint="/now_playing",
                    payload=summarize_payload(now_xml),
                    parsed_summary=(
                        f"cause={invalid_diagnosis.cause.value};"
                        f"confidence={invalid_diagnosis.confidence};"
                        "automatic_action=NONE"
                    ),
                )
            )
            _add_diagnostic_event(
                db,
                device_id=device.device_id,
                occurred_at=now,
                code="INVALID_SOURCE_OBSERVED",
                message=(
                    f"{invalid_diagnosis.user_visible_reason} "
                    "Es wurde keine automatische Wiedergabeaktion ausgeführt."
                ),
                evidence=evidence,
                severity="ERROR",
            )
            write_masterlog(
                "invalid_source_observed",
                device_id=device.device_id,
                radio_ip=device.ip_address,
                previous_source=previous_source,
                restriction_state=restriction["state"],
                cause=invalid_diagnosis.cause.value,
                confidence=invalid_diagnosis.confidence,
                automatic_action="NONE",
            )
        if not playing and previous_playing:
            previous_duration = int((now - previous_started).total_seconds()) if previous_started else 0
            db.add(
                TelemetryEvent(
                    device_id=device.device_id,
                    event_type="playback_stop_observed",
                    endpoint="/now_playing",
                    payload=summarize_payload(now_xml),
                    parsed_summary=f"previous_playback_seconds={previous_duration};restriction_state={restriction['state']}",
                )
            )
            observation_status = "invalid_source_diagnosis_required" if current_source == "INVALID_SOURCE" else "stop_observed"
            write_masterlog(
                "playback_stop_observed",
                device_id=device.device_id,
                radio_ip=device.ip_address,
                previous_playback_seconds=previous_duration,
                restriction_state=restriction["state"],
            )
        if playing and not previous_playing:
            write_masterlog("playback_session_start", device_id=device.device_id, radio_ip=device.ip_address, source=current_source)
        if playing:
            pending = stored.get("playback_pending") or {}
            identity_payload = _history_identity_payload(stored, runtime, pending)
            confirm_playback_session(
                db, device, observed_at=now, source=current_source,
                station_id=identity_payload["station_id"], station_name=identity_payload["station_name"],
                stream_url=identity_payload["stream_url"], source_account=identity_payload["source_account"],
                content_item_name=identity_payload["content_item_name"], trigger=identity_payload["trigger"],
                trigger_type=identity_payload["trigger_type"], internal_event=identity_payload["internal_event"],
            )
            stored.pop("playback_pending", None)
        else:
            close_open_sessions(db, device.device_id, reason=(play_state or current_source or "inactive").lower(), transition_at=now, device_last_seen=device.last_seen)
        write_masterlog("playback_session_heartbeat", device_id=device.device_id, radio_ip=device.ip_address, source=current_source, playing=playing, duration_seconds=duration)
        keepalive.update({
            "playing": playing,
            "last_keepalive_at": now.isoformat(),
            "last_heartbeat_at": now.isoformat(),
            "last_seen_playback": now.isoformat() if playing else keepalive.get("last_seen_playback", ""),
            "playback_started_at": started_at.isoformat() if started_at else "",
            "current_playback_seconds": duration,
            "longest_playback_seconds": max(int(keepalive.get("longest_playback_seconds") or 0), duration),
            "playback_session_id": keepalive.get("playback_session_id") if playing and previous_playing else _session_id("playback", device.device_id, started_at) if playing else keepalive.get("playback_session_id", ""),
            "content_session_id": keepalive.get("content_session_id") if playing and previous_playing else _session_id("content", device.device_id, started_at) if playing else keepalive.get("content_session_id", ""),
            "source_binding_id": keepalive.get("source_binding_id") if playing and previous_playing and previous_source == current_source else _session_id("binding", device.device_id, started_at) if playing else keepalive.get("source_binding_id", ""),
            "playback_session": _session_payload(device.device_id, current_source, play_state, started_at, keepalive) if playing or current_source == "INVALID_SOURCE" else keepalive.get("playback_session", {}),
            "last_source": current_source or previous_source,
            "last_preset_slot": preset_slot or keepalive.get("last_preset_slot", ""),
            "manual_stop": manual_stop if not playing else False,
            "invalid_source_automatic_action": "NONE" if current_source == "INVALID_SOURCE" else "",
            "invalid_source_cause": (
                invalid_diagnosis.cause.value if invalid_diagnosis is not None else ""
            ),
            "invalid_source_confidence": (
                invalid_diagnosis.confidence if invalid_diagnosis is not None else 0
            ),
            "consecutive_failures": 0,
            "last_error": "",
            "next_retry_at": "",
            "paused": False,
            "last_stop_detected": now.isoformat() if not playing and previous_playing else keepalive.get("last_stop_detected", ""),
            "playback_observation_status": observation_status,
            "restriction": restriction,
            "last_restriction_source_key": restriction_source_key,
            "last_read_endpoints": reads,
            "last_volume_summary": summarize_payload(volume_xml),
            "skip_reason": "",
            "polling_profile": policy.polling_profile.value,
            "circuit_state": "closed",
            "safe_mode_active": policy.safe_mode_active,
            "device_class": policy.device_class.value,
            **progress_state,
        })
        stored.update(runtime)
        stored["playback_keepalive"] = keepalive
        save_runtime_state(db, device.device_id, stored, commit=False)
        repository = ResearchStateRepository(db)
        provider_projection = _provider_health_projection(
            db,
            device.device_id,
            stored,
            current_source=current_source,
            previous_source=previous_source,
            observed_at=now,
        )
        provider_health = None
        if provider_projection is not None:
            provider_assessment = provider_projection["assessment"]
            provider_health = str(provider_assessment.state)
            repository.upsert_provider_health(
                device.device_id,
                provider_projection["provider_id"],
                provider_assessment,
                source=provider_projection["provider_id"],
                availability=provider_projection["availability"],
                association=provider_projection["association"],
            )
        previous_playback_health = (
            db.query(PlaybackHealthState)
            .filter(PlaybackHealthState.device_id == device.device_id)
            .one_or_none()
        )
        previous_playback_health_state = (
            str(previous_playback_health.state or "").upper()
            if previous_playback_health is not None
            else ""
        )
        playback_assessment = reduce_playback_health(
            PlaybackSignals(
                radio_status=play_state,
                source=current_source,
                source_valid=(
                    False
                    if current_source == "INVALID_SOURCE"
                    else True
                    if current_source
                    else None
                ),
                position_advancing=position_advancing,
                progress_observed_for_s=progress_observed_for_s,
                last_success=now,
                evidence=[
                    {
                        "type": "AUTHORITATIVE_RADIO_READBACK",
                        "endpoints": list(reads),
                        "source": current_source or None,
                        "radio_status": play_state or None,
                        "position_advancing": position_advancing,
                        "progress_observed_for_s": progress_observed_for_s,
                    }
                ],
            ),
            since=now,
        )
        repository.upsert_playback_health(
            device.device_id,
            playback_assessment,
            source_valid=(
                False
                if current_source == "INVALID_SOURCE"
                else True
                if current_source
                else None
            ),
            # A single readback cannot prove transport liveness or position
            # progress. These remain unknown instead of being inferred from a
            # responsive stream/provider.
            stream_alive=None,
            position_advancing=position_advancing,
            provider_health=provider_health,
        )
        project_airplay_readiness_from_persisted(
            db, device, runtime_state=stored
        )
        reachable_value = getattr(device, "reachable", True)
        was_unreachable = (reachable_value is False) or int(getattr(device, "failure_count", 0) or 0) > 0
        device.last_seen = utc_now()
        device.reachable = True
        device.failure_count = 0
        device.last_failed_at = None
        device.offline_reason = ""
        if was_unreachable:
            write_masterlog("device_recovered_online", device_id=device.device_id, radio_ip=device.ip_address)
        last_log = _last_ok_log.get(device.device_id)
        log_every = get_settings().playback_keepalive_log_every_seconds
        if last_log is None or (now - last_log).total_seconds() >= log_every:
            write_masterlog("playback_keepalive_ok", device_id=device.device_id, radio_ip=device.ip_address, playing=playing, duration_seconds=duration)
            _last_ok_log[device.device_id] = now
        try:
            db.commit()
        except Exception:
            db.rollback()
            save_runtime_state(db, device.device_id, stored, commit=True)
            device.last_seen = utc_now()
            db.commit()
        metadata_ingested = False
        radio_metadata = _radio_metadata_observation(runtime)
        if research_runtime is not None and radio_metadata is not None:
            selection_id, station_name = _radio_metadata_selection(runtime)
            try:
                await research_runtime.ingest_metadata(
                    device.device_id,
                    radio_metadata,
                    provenance=MetadataProvenance.RADIO,
                    confidence=100,
                    observed_at=now,
                    station_name=station_name,
                    station_id=selection_id,
                    provider=current_source or None,
                    source=current_source or None,
                )
                metadata_ingested = True
            except Exception as metadata_exc:
                # Metadata is independent from an otherwise successful radio
                # readback. Record only the error class; do not retry playback
                # or turn this into a keepalive failure.
                write_masterlog(
                    "radio_metadata_ingest_failed",
                    device_id=device.device_id,
                    error_type=type(metadata_exc).__name__,
                    playback_action="NONE",
                )
        recovery_result = None
        stalled_transition = (
            str(playback_assessment.state).upper() == "STALLED"
            and previous_playback_health_state != "STALLED"
        )
        recovery_reason = None
        if (
            invalid_source_transition
            and not manual_stop
            and policy.allow_invalid_source_recovery
        ):
            cause = invalid_diagnosis.cause.value if invalid_diagnosis else "UNKNOWN"
            if cause in {"STREAM_FAILURE", "UNSUPPORTED_STREAM"}:
                recovery_reason = RecoveryReason.STREAM_FAILURE
            elif cause in {"PROVIDER_UNAVAILABLE", "ACCOUNT_UNAVAILABLE"}:
                recovery_reason = RecoveryReason.PROVIDER_UNAVAILABLE
            elif cause == "REPORTING_DEGRADED":
                recovery_reason = RecoveryReason.REPORTING_DEGRADED
            else:
                recovery_reason = RecoveryReason.SOURCE_INVALID
        elif stalled_transition:
            recovery_reason = RecoveryReason.STREAM_FAILURE
        if recovery_reason is not None and research_runtime is not None:
            try:
                recovery_result = await _run_safe_automatic_recovery(
                    device=device,
                    db=db,
                    client=client,
                    runtime=runtime,
                    stored=stored,
                    research_runtime=research_runtime,
                    reason=recovery_reason,
                    source=(previous_source if invalid_source_transition else current_source),
                )
            except Exception as recovery_exc:
                write_masterlog(
                    "safe_automatic_recovery_failed",
                    device_id=device.device_id,
                    error_type=type(recovery_exc).__name__,
                    radio_write=False,
                )
                recovery_result = {
                    "status": "FAILED",
                    "error_type": type(recovery_exc).__name__,
                    "automatic": True,
                    "radio_write": False,
                }
            _runtime_row, latest_stored = load_runtime_state(db, device.device_id)
            latest_keepalive = latest_stored.get("playback_keepalive") or {}
            latest_keepalive["last_safe_recovery"] = recovery_result
            latest_keepalive["invalid_source_automatic_action"] = (
                "SAFE_RECOVERY_0_3"
                if invalid_source_transition
                else latest_keepalive.get("invalid_source_automatic_action", "")
            )
            latest_stored["playback_keepalive"] = latest_keepalive
            save_runtime_state(db, device.device_id, latest_stored, commit=True)
        return {
            "device_id": device.device_id,
            "ok": True,
            "playing": playing,
            "reads": reads,
            "duration_seconds": duration,
            "playback_observation_status": observation_status,
            "restriction": restriction,
            "invalid_source_action": (
                "SAFE_RECOVERY_0_3"
                if invalid_source_transition and recovery_result is not None
                else "NONE"
            ),
            "invalid_source_cause": (
                invalid_diagnosis.cause.value if invalid_diagnosis is not None else None
            ),
            "invalid_source_confidence": (
                invalid_diagnosis.confidence if invalid_diagnosis is not None else None
            ),
            "provider_health": provider_health,
            "metadata_ingested": metadata_ingested,
            "playback_health": str(playback_assessment.state),
            "position_advancing": position_advancing,
            "progress_observed_for_s": progress_observed_for_s,
            "recovery": recovery_result,
            "polling_profile": policy.polling_profile.value,
            "safe_mode_active": policy.safe_mode_active,
            "circuit_state": "closed",
        }
    except Exception as exc:
        db.rollback()
        error = _error_text(exc)
        failures = max(int(keepalive.get("consecutive_failures") or 0), int(getattr(device, "failure_count", 0) or 0)) + 1
        backoff = recommended_backoff_seconds(failures, policy.device_class)
        next_retry = now + timedelta(seconds=backoff) if backoff else None
        reachable_value = getattr(device, "reachable", True)
        was_reachable = reachable_value is not False
        device.failure_count = failures
        device.last_failed_at = utc_now()
        device.offline_reason = error
        if failures >= OFFLINE_FAILURE_THRESHOLD:
            device.reachable = False
            keepalive["playing"] = False
            keepalive["playback_started_at"] = ""
            keepalive["current_playback_seconds"] = 0
            close_open_sessions(db, device.device_id, reason="offline", device_last_seen=device.last_seen)
            if was_reachable:
                write_masterlog("device_marked_offline", device_id=device.device_id, radio_ip=device.ip_address, failure_count=failures, offline_reason=error)
        paused = failures >= KEEPALIVE_PAUSE_FAILURE_THRESHOLD
        keepalive.update({
            "consecutive_failures": failures,
            "last_error": error,
            "last_keepalive_at": now.isoformat(),
            "last_failed_at": now.isoformat(),
            "paused": paused,
            "next_retry_at": next_retry.isoformat() if next_retry else "",
            "backoff_seconds": backoff,
            "playback_observation_status": "readback_warning" if failures >= 2 else keepalive.get("playback_observation_status", "unknown"),
            "skip_reason": "backoff after failed poll" if paused else "",
            "polling_profile": "offline_backoff" if paused else policy.polling_profile.value,
            "circuit_state": "open" if failures >= KEEPALIVE_PAUSE_FAILURE_THRESHOLD else "half_open",
            "safe_mode_active": policy.safe_mode_active,
            "device_class": policy.device_class.value,
        })
        stored["playback_keepalive"] = keepalive
        save_runtime_state(db, device.device_id, stored, commit=False)
        # A failed /now_playing read is not evidence that the previously
        # observed PLAYING state is still current.  Persist an explicit
        # authoritative-readback failure immediately (not only after the
        # offline threshold), while retaining the old source merely as
        # diagnostic context.  This path performs no radio action.
        previous_health = (
            db.query(PlaybackHealthState)
            .filter(PlaybackHealthState.device_id == device.device_id)
            .one_or_none()
        )
        readback_evidence = [
            {
                "type": "AUTHORITATIVE_RADIO_READBACK_FAILED",
                "attempted_endpoints": list(reads) or ["/now_playing"],
                "failed_endpoint": reads[-1] if reads else "/now_playing",
                "error_type": type(exc).__name__,
                "failure_count": failures,
                "automatic_action": "NONE",
            }
        ]
        failed_assessment = reduce_playback_health(
            PlaybackSignals(
                radio_status=None,
                source=(
                    str(keepalive.get("last_source") or "").strip().upper()
                    or None
                ),
                source_valid=None,
                last_success=(
                    _as_utc(previous_health.observed_at)
                    if previous_health is not None
                    and previous_health.state == "PLAYING"
                    else None
                ),
                evidence=readback_evidence,
            ),
            since=now,
        )
        ResearchStateRepository(db).upsert_playback_health(
            device.device_id,
            failed_assessment,
            source_valid=None,
            stream_alive=None,
            position_advancing=None,
            provider_health=(
                previous_health.provider_health
                if previous_health is not None
                else None
            ),
        )
        db.add(TelemetryEvent(device_id=device.device_id, event_type="playback_keepalive_failed", endpoint=",".join(reads) or "/now_playing", payload=error, parsed_summary=f"failure_count={failures}"))
        if paused:
            write_masterlog("keepalive_backoff", device_id=device.device_id, radio_ip=device.ip_address, failure_count=failures, backoff_seconds=backoff, next_retry_at=next_retry.isoformat() if next_retry else "")
            write_masterlog("keepalive_paused", device_id=device.device_id, radio_ip=device.ip_address, error=error, failure_count=failures, backoff_seconds=backoff, next_retry_at=next_retry.isoformat() if next_retry else "")
        else:
            write_masterlog("playback_keepalive_failed", device_id=device.device_id, radio_ip=device.ip_address, error=error, failure_count=failures)
        try:
            db.commit()
        except Exception:
            db.rollback()
            save_runtime_state(db, device.device_id, stored, commit=True)
        return {"device_id": device.device_id, "ok": False, "error": error, "failure_count": failures, "paused": paused, "backoff_seconds": backoff, "next_retry_at": next_retry.isoformat() if next_retry else "", "reads": reads}


async def run_playback_keepalive_once(
    db: Session,
    *,
    client_factory=SoundTouchClient,
    now: datetime | None = None,
    research_runtime=None,
) -> list[dict]:
    results = []
    for device in db.query(Device).order_by(Device.name).all():
        if not device.ip_address:
            continue
        results.append(
            await run_playback_keepalive_for_device(
                device,
                db,
                now=now,
                client_factory=client_factory,
                research_runtime=research_runtime,
            )
        )
    return results


async def playback_keepalive_loop(
    stop_event: asyncio.Event, *, research_runtime=None
) -> None:
    from basswiesn.app.db import SessionLocal

    settings = get_settings()
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            await run_playback_keepalive_once(
                db, research_runtime=research_runtime
            )
        except Exception as exc:
            write_masterlog("playback_keepalive_failed", error=str(exc), scope="loop")
        finally:
            db.close()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.playback_keepalive_interval_seconds)
        except asyncio.TimeoutError:
            pass
