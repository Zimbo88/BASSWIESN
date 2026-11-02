import xml.etree.ElementTree as ET
import asyncio as asyncio  # Compatibility for focused legacy test/client hooks.
import json

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.db import get_db
from basswiesn.app.models import ConfigBackup, Preset, PresetMutation, RuntimeState, Setting, Station, utc_now
from basswiesn.app.routers.shared import device_or_404, enforce_ip_write_guard, memory_check_plan, require_memory_checked
from basswiesn.app.services.orion import (
    OrionLocationError,
    StationDescriptor,
    station_location,
)
from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.services.xml import content_item_xml
from basswiesn.app.services.preset_contract import (
    preset_summaries_from_xml as preset_summaries_from_xml,
    _decoded_orion_station as _decoded_orion_station,
    _canonical_preset_source_account as _canonical_preset_source_account,
    _preset_content_item_xml as _preset_content_item_xml,
    _xml_local_name as _xml_local_name,
    _child_text as _child_text,
    _orion_data_key as _orion_data_key,
    _locations_match as _locations_match,
    _locations_select_same_station as _locations_select_same_station,
    _canonical_xml as _canonical_xml,
    PresetSyncResult as PresetSyncResult,
    classify_notification_sequence as classify_notification_sequence,
    _preset_projection as _preset_projection,
    _preset_contract_matches as _preset_contract_matches,
    compare_preset_snapshots as compare_preset_snapshots,
    _integrity_for_sync_result as _integrity_for_sync_result,
)
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.services.device_state import (
    load_runtime_state,
    merge_provider_maps,
    parse_service_availability_xml,
    parse_sources_xml,
)
from basswiesn.app.services.provider_registry import (
    SERVICE_MANIFEST,
    STREAM_SOURCE_PRIORITY,
    normalize_source_name,
)
from basswiesn.app.services.stream_compat import (
    ProtectedStreamTarget,
    analyze_stream_url,
    probe_stream_reachability,
    resolve_stream_url,
)
from basswiesn.app.services.logo_validation import probe_logo_reference, validate_logo_reference
from basswiesn.app.services.offline_mode import external_request_decision
from basswiesn.app.services.protected_devices import is_device_access_protected, is_protected_ip, require_unprotected_device
from basswiesn.app.services.device_policy import policy_for_device
from basswiesn.app.services.device_transactions import device_transaction, device_fingerprint
from basswiesn.app.services.preset_writer import PresetWritePorts, PresetWriteFailure, write_presets, apply_prepared_presets
from basswiesn.app.services.preset_observation import PresetAccess, local_version
from basswiesn.app.services.preset_recovery import PresetRecoveryError, download_presets, remove_preset
from basswiesn.app.services.safe_uploads import InvalidUpload, UploadError, UploadQuotaExceeded, UploadTooLarge, UnsupportedUploadType, store_upload
from basswiesn.app.services.action_journal import record_action

router = APIRouter(prefix="/api", tags=["stations-presets"])

SOURCE_ACCEPTANCE_ATTEMPTS = 20
SOURCE_ACCEPTANCE_INTERVAL_SECONDS = 0.5


def _soundtouch_client_for(device, *, purpose: str, trigger: str = "", policy=None) -> SoundTouchClient:
    try:
        return SoundTouchClient(
            device.ip_address,
            device_id=device.device_id,
            request_purpose=purpose,
            trigger=trigger,
            policy_context=policy.to_dict() if policy is not None else None,
        )
    except TypeError:
        return SoundTouchClient(device.ip_address)


def _source_candidates_from_xml(xml_text: str) -> dict[str, bool]:
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError:
        return {}
    candidates: dict[str, bool] = {}
    for node in root.iter():
        values = [
            node.attrib.get("source"),
            node.attrib.get("type"),
            node.attrib.get("displayName"),
        ]
        if node.tag.lower().endswith("sourcekey"):
            values.append(node.attrib.get("type"))
        status = (node.attrib.get("status") or "").upper()
        usable = status not in {"UNAVAILABLE", "NOT_READY", "ERROR", "DISABLED"}
        for value in values:
            source = (value or "").strip().upper().replace(" ", "_")
            if source in STREAM_SOURCE_PRIORITY:
                candidates[source] = candidates.get(source, False) or usable
    return candidates


def _stream_source_from_sources_xml(xml_text: str, fallback: str = "") -> str:
    candidates = _source_candidates_from_xml(xml_text)
    for source in STREAM_SOURCE_PRIORITY:
        if candidates.get(source):
            return source
    return fallback if fallback in STREAM_SOURCE_PRIORITY else ""




def _xml_preview(xml: str, limit: int = 600) -> str:
    return " ".join((xml or "").split())[:limit]


def _radio_response_preview(value: str, limit: int = 600) -> str:
    return " ".join((value or "").split())[:limit]




def _station_location_or_409(descriptor: StationDescriptor, db: Session, request: Request | None = None) -> str:
    try:
        return station_location(descriptor, db=db, request_host=request.headers.get("host", "") if request else "")
    except OrionLocationError as exc:
        raise HTTPException(status_code=409, detail={"error": str(exc), "hint": "BASSWIESN Host IP setzen"}) from exc


def _device_content_item_xml(db: Session, device_id: str, station: Station, location: str, source: str = "LOCAL_INTERNET_RADIO") -> str:
    # radio_symbol omits the element and lets firmware choose its native
    # source glyph. station_logo supplies a validated URL. no_station_logo
    # sends an explicit empty standard containerArt field so only text remains.
    mode = _station_art_mode(db, device_id)
    include_art = mode == "station_logo" and validate_logo_reference(station.image_url)["valid"]
    return content_item_xml(
        station,
        location,
        include_container_art=include_art,
        source=normalize_source_name(source),
        empty_container_art=mode == "no_station_logo",
    )


def _station_art_mode(db: Session, device_id: str) -> str:
    row = db.query(Setting).filter(Setting.key == f"station_art_mode:{device_id}").one_or_none()
    value = str(row.value if row else "radio_symbol")
    return value if value in {"radio_symbol", "station_logo", "no_station_logo"} else "radio_symbol"


def _station_logo_enabled(db: Session, device_id: str) -> bool:
    return _station_art_mode(db, device_id) == "station_logo"


def _record_preset_sync_state(db: Session, device_id: str, state: dict) -> None:
    key = f"preset_sync:{device_id}"
    row = db.query(RuntimeState).filter(RuntimeState.key == key).one_or_none()
    if row is None:
        row = RuntimeState(key=key)
        db.add(row)
    row.value = json.dumps({"device_id": device_id, "updated_at": utc_now().isoformat(), **state}, ensure_ascii=False)
    row.updated_at = utc_now()
    db.commit()


def media_type_for_url(url: str) -> dict:
    analysis = analyze_stream_url(url)
    if analysis.stream_format == "mp3":
        return {"status": "confirmed", "type": "direct_mp3", "message": "Direct MP3 is the safest confirmed preset stream type.", **analysis.to_dict()}
    if analysis.stream_format in {"aac", "ogg"}:
        return {"status": "candidate", "type": analysis.stream_format, "message": f"{analysis.stream_format.upper()} direct stream is a good SoundTouch candidate.", **analysis.to_dict()}
    if analysis.is_hls:
        return {"status": "candidate", "type": "hls", "message": analysis.compatibility_warning, **analysis.to_dict()}
    value = (url or "").lower().split("?", 1)[0]
    if value.endswith((".flac", ".opus", ".wma")):
        return {"status": "blocked", "type": "unsupported", "message": "This codec is not enabled for presets yet.", **analysis.to_dict()}
    if value.startswith(("http://", "https://")):
        return {"status": "candidate", "type": "http_live_radio", "message": "HTTP live radio URL; use test before writing preset.", **analysis.to_dict()}
    return {"status": "blocked", "type": "invalid", "message": "Only HTTP/HTTPS stream URLs are accepted for radio presets.", **analysis.to_dict()}


def station_payload_summary(station: Station) -> dict:
    media = media_type_for_url(station.stream_url)
    return {
        "id": station.id,
        "name": station.name,
        "stream_url": station.stream_url,
        "stream_url_original": station.stream_url_original,
        "stream_url_resolved": station.stream_url_resolved,
        "stream_format": station.stream_format,
        "stream_mime": station.stream_mime,
        "stream_codec": station.stream_codec,
        "compatibility_score": station.compatibility_score,
        "compatibility_warning": station.compatibility_warning,
        "is_hls": bool(station.is_hls),
        "is_direct_audio": bool(station.is_direct_audio),
        "image_url": station.image_url,
        "provider": station.provider,
        "internal": bool(getattr(station, "internal", False)),
        "purpose": getattr(station, "purpose", "") or "",
        "lab_only": bool(getattr(station, "lab_only", False)),
        "media": media,
    }


def _station_descriptor(station: Station, *, include_art: bool = True) -> StationDescriptor:
    return StationDescriptor(
        name=station.name,
        stream_url=station.stream_url,
        image_url=station.image_url if include_art else "",
        tunein_id=station.provider_station_id,
        stream_url_resolved=station.stream_url_resolved,
        stream_format=station.stream_format or station.stream_codec,
        stream_mime=station.stream_mime,
        compatibility_warning=station.compatibility_warning,
    )


def _device_station_descriptor(db: Session, device_id: str, station: Station) -> StationDescriptor:
    return _station_descriptor(station, include_art=_station_logo_enabled(db, device_id))


def _preset_station(db: Session, preset: Preset) -> Station | None:
    return db.query(Station).filter(Station.id == preset.station_id).one_or_none() if preset.station_id else None


def _effective_preset_location(db: Session, device_id: str, preset: Preset, *, request_host: str = "") -> str:
    station = _preset_station(db, preset)
    if station is None:
        return preset.location
    try:
        return station_location(_device_station_descriptor(db, device_id, station), db=db, request_host=request_host)
    except OrionLocationError:
        return preset.location


def _effective_preset_content_item_xml(db: Session, device_id: str, preset: Preset, *, request_host: str = "", location_override: str = "") -> str:
    station = _preset_station(db, preset)
    if station is None:
        return preset.content_item_xml
    location = location_override or _effective_preset_location(db, device_id, preset, request_host=request_host)
    return _device_content_item_xml(db, device_id, station, location, normalize_source_name(preset.source))


def _apply_stream_analysis(row: Station, analysis) -> None:
    row.stream_url_original = analysis.stream_url_original
    row.stream_url_resolved = analysis.stream_url_resolved
    row.stream_format = analysis.stream_format
    row.stream_mime = analysis.stream_mime
    row.stream_codec = analysis.stream_codec
    row.compatibility_score = analysis.compatibility_score
    row.compatibility_warning = analysis.compatibility_warning
    row.is_hls = int(analysis.is_hls)
    row.is_direct_audio = int(analysis.is_direct_audio)


def _station_add_response(row: Station, media: dict, created: bool) -> dict:
    station = station_payload_summary(row)
    return {
        "id": row.id,
        "created": created,
        "station": station,
        "stream_url": row.stream_url,
        "media": media,
        "compatibility": {
            "stream_format": row.stream_format,
            "stream_mime": row.stream_mime,
            "stream_codec": row.stream_codec,
            "compatibility_score": row.compatibility_score,
            "compatibility_warning": row.compatibility_warning,
            "is_hls": bool(row.is_hls),
            "is_direct_audio": bool(row.is_direct_audio),
            "stream_url_original": row.stream_url_original,
            "stream_url_resolved": row.stream_url_resolved,
        },
    }










def _upsert_station_from_import(db: Session, *, name: str, stream_url: str, image_url: str, source: str, provider_station_id: str = "", resolved_url: str = "") -> Station | None:
    stream = (stream_url or "").strip()
    if not stream:
        return None
    station = db.query(Station).filter(Station.stream_url == stream).order_by(Station.id).first()
    if station is None:
        station = db.query(Station).filter(Station.stream_url_original == stream).order_by(Station.id).first()
    if station is None:
        station = Station(
            name=(name or "Unbenannter Sender").strip(),
            stream_url=stream,
            image_url=(image_url or "").strip(),
            provider=source or "LOCAL_INTERNET_RADIO",
            provider_station_id=provider_station_id or "",
        )
        _apply_stream_analysis(station, analyze_stream_url(stream, resolved_url=resolved_url))
        db.add(station)
        db.flush()
        return station
    if image_url and not station.image_url:
        station.image_url = image_url
    if provider_station_id and not station.provider_station_id:
        station.provider_station_id = provider_station_id
    if source and not station.provider:
        station.provider = source
    if resolved_url and not station.stream_url_resolved:
        _apply_stream_analysis(station, analyze_stream_url(station.stream_url, resolved_url=resolved_url))
    return station


def import_presets_from_radio_backup(db: Session, device_id: str, xml_text: str, *, request_host: str = "") -> dict:
    """Import a pre-redirect radio /presets backup into local BASSWIESN presets."""
    rows = preset_summaries_from_xml(xml_text)
    include_art = _station_logo_enabled(db, device_id)
    imported: list[dict] = []
    skipped: list[dict] = []
    for item in rows:
        button = int(item.get("button") or 0)
        if button < 1 or button > 6:
            skipped.append({"button": button, "reason": "button outside SoundTouch preset range"})
            continue
        source = normalize_source_name(item.get("source"))
        source_account = _canonical_preset_source_account(
            source, item.get("source_account") or ""
        )
        location = item.get("location") or ""
        decoded = _decoded_orion_station(location)
        name = decoded.get("name") or item.get("item_name") or f"Preset {button}"
        stream_url = decoded.get("streamUrl") or decoded.get("stream_url") or ""
        resolved_url = decoded.get("streamUrlResolved") or decoded.get("stream_url_resolved") or ""
        image_url = decoded.get("imageUrl") or decoded.get("image_url") or item.get("container_art") or ""
        provider_station_id = decoded.get("tuneinId") or decoded.get("providerStationId") or ""
        station = _upsert_station_from_import(
            db,
            name=name,
            stream_url=stream_url,
            image_url=image_url,
            source=source,
            provider_station_id=provider_station_id,
            resolved_url=resolved_url,
        )
        if station is not None:
            descriptor = StationDescriptor(
                station.name,
                station.stream_url,
                station.image_url if include_art else "",
                station.provider_station_id,
                stream_url_resolved=station.stream_url_resolved,
                stream_format=station.stream_format,
                stream_mime=station.stream_mime,
                compatibility_warning=station.compatibility_warning,
            )
            try:
                location = station_location(descriptor, db=db, request_host=request_host)
            except OrionLocationError:
                skipped.append({"button": button, "reason": "could not build local Orion location", "station": station.name})
                continue
            name = station.name
            image_url = station.image_url
        elif not location:
            skipped.append({"button": button, "reason": "preset has no usable location"})
            continue
        content_xml = _preset_content_item_xml(
            item,
            location=location,
            source=source,
            source_account=source_account,
            name=name,
            art=image_url,
            include_art=include_art,
        )
        preset = db.query(Preset).filter(Preset.device_id == device_id, Preset.button == button).one_or_none()
        if preset is None:
            preset = Preset(device_id=device_id, button=button)
            db.add(preset)
        preset.station_id = station.id if station is not None else None
        preset.source = source
        preset.source_account = source_account
        preset.location = location
        preset.content_item_xml = content_xml
        preset.updated_at = utc_now()
        imported.append({"button": button, "station_id": preset.station_id, "name": name, "location": location})
    write_masterlog(
        "setup_presets_imported_from_backup",
        device_id=device_id,
        source_count=len(rows),
        imported_count=len(imported),
        skipped_count=len(skipped),
    )
    return {"device_id": device_id, "source_count": len(rows), "imported_count": len(imported), "imported": imported, "skipped": skipped}






















def _prepare_artwork_only_sync(
    db: Session,
    device_id: str,
    local_rows: list[Preset],
    radio_rows: list[dict],
) -> tuple[dict[int, dict], list[int], list[dict]]:
    """Build writes from live radio XML while preserving selection identity."""

    mode = _station_art_mode(db, device_id)
    current = {int(item.get("button") or 0): item for item in radio_rows}
    prepared: dict[int, dict] = {}
    already_current: list[int] = []
    skipped: list[dict] = []
    for preset in local_rows:
        actual = current.get(int(preset.button))
        station = _preset_station(db, preset)
        if actual is None:
            skipped.append({"button": preset.button, "reason": "radio slot is absent; artwork-only sync will not recreate it"})
            continue
        if station is None:
            skipped.append({"button": preset.button, "reason": "no local station mapping; artwork source is unknown"})
            continue
        logo = validate_logo_reference(station.image_url)
        include_art = mode == "station_logo" and bool(logo.get("valid"))
        empty_art = mode == "no_station_logo"
        # This is an artwork edit, not a new station selection. Do not insert
        # isPresetable/type/name defaults or normalize any account/unknown field.
        content = ET.fromstring(actual["content_item_xml"])
        art_nodes = [child for child in content if _xml_local_name(child.tag) == "containerArt"]
        if len(art_nodes) > 1:
            raise ValueError("ambiguous containerArt")
        art_node = art_nodes[0] if art_nodes else None
        if include_art or empty_art:
            if art_node is None:
                namespace = content.tag.rsplit("}", 1)[0] + "}" if "}" in content.tag else ""
                art_node = ET.SubElement(content, namespace + "containerArt")
            art_node.text = station.image_url if include_art else ""
        elif art_node is not None:
            content.remove(art_node)
        desired_xml = ET.tostring(content, encoding="unicode")
        parsed = preset_summaries_from_xml(
            f'<presets><preset id="{preset.button}">{desired_xml}</preset></presets>'
        )
        if not parsed:
            skipped.append({"button": preset.button, "reason": "desired artwork projection could not be parsed"})
            continue
        desired = parsed[0]
        if _preset_contract_matches(
            _preset_projection(actual), _preset_projection(desired)
        ):
            already_current.append(preset.button)
            continue
        prepared[preset.button] = {
            "button": preset.button,
            "station_id": station.id,
            "source": desired.get("source", ""),
            "source_account": desired.get("source_account", ""),
            "location": desired.get("location", ""),
            "content_item_xml": desired_xml,
            "change": "update only containerArt; preserve source, account, location and item name",
        }
    return prepared, already_current, skipped


def _expected_preset_summary(db: Session, device_id: str, button: int, location: str) -> dict:
    preset = db.query(Preset).filter(Preset.device_id == device_id, Preset.button == button).one_or_none()
    if preset is None or not preset.content_item_xml:
        return {"button": button, "location": location}
    effective_xml = _effective_preset_content_item_xml(db, device_id, preset, location_override=location)
    rows = preset_summaries_from_xml(f'<presets><preset id="{button}">{effective_xml}</preset></presets>')
    if not rows:
        return {"button": button, "location": location}
    item = dict(rows[0])
    item["location"] = location
    return item






def _preset_preview(payload: dict, *, default: bool = True) -> bool:
    value = payload.get("dry_run", default)
    if type(value) is not bool:
        raise HTTPException(status_code=400, detail="dry_run must be a JSON boolean")
    return value


async def _apply_prepared(device, prepared, db, **options) -> PresetSyncResult:
    try:
        return await apply_prepared_presets(device, prepared, db, ports=preset_write_ports(), **options)
    except PresetWriteFailure as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc


def preset_write_ports() -> PresetWritePorts:
    """One transport/policy adapter shared by explicit preset operations."""
    return PresetWritePorts(
        guard=enforce_ip_write_guard,
        client=lambda target: _soundtouch_client_for(target, purpose="preset_sync", trigger="stations_presets"),
        expected_summary=_expected_preset_summary,
        effective_item=lambda session, identity, preset, location: _effective_preset_content_item_xml(
            session, identity, preset, location_override=location),
    )


async def sync_presets_to_radio(
    device,
    expected: dict[int, str],
    db: Session,
    backup_label: str = "preset-sync",
    *,
    prepared_presets: dict[int, dict] | None = None,
    mutations: dict[int, PresetMutation] | None = None,
) -> PresetSyncResult:
    """Compatibility adapter; all write/readback orchestration lives in the service."""
    ports = preset_write_ports()
    try:
        return await write_presets(device, expected, db, backup_label, ports=ports,
                                   prepared_presets=prepared_presets, mutations=mutations)
    except PresetWriteFailure as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc


@router.get("/stations")
async def stations(include_internal: bool = False, db: Session = Depends(get_db)) -> list[dict]:
    lab = db.query(Setting).filter(Setting.key == "lab_mode").one_or_none()
    allow_internal = include_internal and lab is not None and str(lab.value).lower() == "true"
    query = db.query(Station)
    if not allow_internal:
        query = query.filter(Station.lab_only.is_(False), Station.internal.is_(False))
    rows = query.order_by(Station.name).all()
    return [station_payload_summary(s) for s in rows]


@router.get("/stations/{station_id}/logo-status")
async def station_logo_status(station_id: int, probe: bool = False, db: Session = Depends(get_db)) -> dict:
    station = db.query(Station).filter(Station.id == station_id).one_or_none()
    if station is None:
        raise HTTPException(status_code=404, detail="station not found")
    result = validate_logo_reference(station.image_url)
    if probe and result.get("probe_available"):
        decision = external_request_decision(
            db,
            service="logo_probe",
            url_or_host=station.image_url,
            reason="manuelle Senderlogo-Prüfung",
            required=False,
            stream_target=True,
            manual_action=True,
        )
        if not decision.allowed:
            result = {**result, "valid": False, "verification": "probe_blocked_by_offline_mode", "reason": "Strict Offline Mode blockiert den externen Logo-Request"}
        else:
            result = await probe_logo_reference(station.image_url)
    return {"station_id": station.id, "station_name": station.name, "image_configured": bool(station.image_url), "logo": result}


@router.post("/stations")
async def add_station(payload: dict, db: Session = Depends(get_db)) -> dict:
    stream_url = str(payload.get("stream_url", "")).strip()
    try:
        analysis = await resolve_stream_url(stream_url)
    except ProtectedStreamTarget as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "protected_stream_target", "message": str(exc)},
        ) from exc
    media = media_type_for_url(stream_url)
    media.update(analysis.to_dict())
    if media["status"] == "blocked":
        raise HTTPException(status_code=400, detail={"error": "unsupported media type", "media": media})
    write_masterlog("stream_type_detected", stream_url_original=analysis.stream_url_original, stream_url_resolved=analysis.stream_url_resolved, codec=analysis.stream_codec, mime=analysis.stream_mime, is_hls=analysis.is_hls, is_direct_audio=analysis.is_direct_audio, compatibility_score=analysis.compatibility_score, compatibility_warning=analysis.compatibility_warning)
    existing = db.query(Station).filter(Station.stream_url == stream_url).order_by(Station.id).first()
    if existing is None:
        existing = db.query(Station).filter(Station.stream_url_original == stream_url).order_by(Station.id).first()
    if existing is not None:
        # Online search and manual creation share this endpoint. Selecting an
        # already known stream must not create another visually identical row.
        if payload.get("image_url") and not existing.image_url:
            existing.image_url = payload["image_url"]
            db.commit()
        _apply_stream_analysis(existing, analysis)
        db.commit()
        return _station_add_response(existing, media, False)
    row = Station(
        name=payload["name"],
        stream_url=stream_url,
        image_url=payload.get("image_url", ""),
        provider=payload.get("provider", "LOCAL_INTERNET_RADIO"),
        provider_station_id=payload.get("provider_station_id", ""),
        internal=False,
        lab_only=False,
    )
    _apply_stream_analysis(row, analysis)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _station_add_response(row, media, True)


@router.post("/stations/upload")
async def upload_station_file(name: str, file: UploadFile = File(...), db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    media_dir = settings.data_dir / "media"
    try:
        stored = await store_upload(
            file,
            directory=media_dir,
            max_bytes=max(1, settings.station_upload_max_mb) * 1024 * 1024,
            quota_bytes=max(1, settings.station_upload_quota_mb) * 1024 * 1024,
        )
    except UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail="uploaded file exceeds the configured size limit") from exc
    except (UnsupportedUploadType, UploadQuotaExceeded) as exc:
        raise HTTPException(status_code=415 if isinstance(exc, UnsupportedUploadType) else 507, detail=str(exc)) from exc
    except InvalidUpload as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UploadError as exc:
        raise HTTPException(status_code=400, detail="invalid upload") from exc
    stream_url = f"{settings.local_base_url}/media/{stored.filename}"
    row = Station(name=name, stream_url=stream_url, provider="LOCAL_FILE")
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "stream_url": row.stream_url, "media": media_type_for_url(row.stream_url)}


@router.get("/presets/{device_id}")
async def get_presets(device_id: str, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(Preset).filter(Preset.device_id == device_id).order_by(Preset.button).all()
    result = []
    for row in rows:
        mutation = db.query(PresetMutation).filter(
            PresetMutation.device_id == device_id,
            PresetMutation.button == row.button,
        ).order_by(PresetMutation.revision.desc()).first()
        result.append({
            "button": row.button,
            "station_id": row.station_id,
            "source": row.source,
            "location": row.location,
            "content_item_xml": row.content_item_xml,
            "revision": mutation.revision if mutation else 0,
            "mutation_state": mutation.state if mutation else "NONE",
            "diverged": bool(mutation.diverged) if mutation else False,
        })
    return result


@router.post("/devices/{device_id}/presets/download")
async def download_device_presets(device_id: str, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    if type(payload.get("dry_run", False)) is not bool:
        raise HTTPException(status_code=400, detail="dry_run must be a JSON boolean")
    if payload.get("dry_run", False):
        return {"dry_run": True, "device_id": device.device_id, "target": device.ip_address, "endpoint": "/presets"}
    try:
        return await download_presets(db, device, client_factory=SoundTouchClient)
    except PresetRecoveryError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from None


@router.post("/devices/{device_id}/stations/{station_id}/play")
async def play_station_on_device(device_id: str, station_id: int, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    # Compatibility for existing in-process callers passing Session as fourth argument.
    context = request if isinstance(request, Request) else None
    return await _play_station_on_device(device_id, station_id, payload, context,
                                        db if context is not None else request)


async def _play_station_on_device(device_id, station_id, payload, request, db, *, participants=None):
    from basswiesn.app.services.station_playback import play_station, StationPlaybackError

    dry_run = _preset_preview(payload, default=False)
    for field, maximum in (("safe_volume", 5), ("target_volume", 100)):
        value = payload.get(field)
        if value is not None and (type(value) is not int or not 0 <= value <= maximum):
            raise HTTPException(status_code=400, detail=f"{field} must be a JSON integer in 0..{maximum}")
    if payload.get("safe_volume") is not None and payload.get("target_volume") is not None:
        raise HTTPException(status_code=400, detail="Choose safe_volume or target_volume, not both")
    device = device_or_404(db, device_id)
    station = db.query(Station).filter_by(id=station_id).one_or_none()
    if station is None:
        raise HTTPException(status_code=404, detail="station not found")
    descriptor = _device_station_descriptor(db, device_id, station)
    location = _station_location_or_409(descriptor, db, request)
    xml = _device_content_item_xml(db, device_id, station, location)
    if dry_run:
        return {"dry_run": True, "device_id": device_id, "target": device.ip_address, "path": "/select", "xml": xml}
    try:
        return await play_station(db, device, station, xml,
            client_factory=lambda item: _soundtouch_client_for(item, purpose="play_station",
                trigger=str(payload.get("trigger") or "api"), policy=policy_for_device(item, db)),
            guard=enforce_ip_write_guard, safe_volume=payload.get("safe_volume"),
            target_volume=payload.get("target_volume"), participants=participants,
            trigger=str(payload.get("trigger") or "stream")[:64],
            trigger_type=str(payload.get("trigger_type") or "station")[:64],
            internal_event=bool(payload.get("internal_event", False)),
            attempts=SOURCE_ACCEPTANCE_ATTEMPTS, interval=SOURCE_ACCEPTANCE_INTERVAL_SECONDS)
    except StationPlaybackError as exc:
        raise HTTPException(status_code=409 if exc.detail["status"] == "BLOCKED" else 502, detail=exc.detail) from None


def _preset_check(
    check_id: str,
    status: str,
    message: str,
    *,
    evidence: dict | None = None,
    affects_verdict: bool = True,
) -> dict:
    return {
        "id": check_id,
        "status": status,
        "message": message,
        "evidence": evidence or {},
        "affects_verdict": affects_verdict,
    }


def _preset_verdict(checks: list[dict]) -> str:
    statuses = {
        str(item.get("status") or "UNKNOWN").upper()
        for item in checks
        if item.get("affects_verdict", True)
    }
    if "BROKEN" in statuses:
        return "BROKEN"
    if "WARNING" in statuses:
        return "WARNING"
    if "UNKNOWN" in statuses:
        return "UNKNOWN"
    return "VALID"


@router.get("/presets/{device_id}/status")
async def preset_status(
    device_id: str,
    probe: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict:
    device = device_or_404(db, device_id)
    local_rows = db.query(Preset).filter(Preset.device_id == device_id).order_by(Preset.button).all()
    local = {row.button: row for row in local_rows}
    snapshot = None
    live_providers: dict[str, dict] = {}
    if probe:
        try:
            client = _soundtouch_client_for(
                device,
                purpose="preset_status_explicit_read",
                trigger="explicit_user_action",
            )
            radio_xml = await client.get_xml("/presets")
            snapshot = ConfigBackup(device_id=device.device_id, path="/presets", content=radio_xml)
            db.add(snapshot)
            db.commit()
            db.refresh(snapshot)
            radio_rows = preset_summaries_from_xml(radio_xml)
            radio_error = ""
            observed_at = utc_now().isoformat()
            source_providers: dict = {}
            availability_providers: dict = {}
            try:
                _source_rows, source_providers = parse_sources_xml(
                    await client.get_xml("/sources"), observed_at
                )
            except Exception:
                pass
            try:
                _availability_rows, availability_providers = (
                    parse_service_availability_xml(
                        await client.get_xml("/serviceAvailability"), observed_at
                    )
                )
            except Exception:
                pass
            live_providers = merge_provider_maps(
                source_providers, availability_providers
            )
        except Exception as exc:
            radio_xml = ""
            radio_rows = []
            radio_error = f"RADIO_READ_FAILED ({type(exc).__name__})"
    else:
        snapshot = (
            db.query(ConfigBackup)
            .filter(ConfigBackup.device_id == device_id, ConfigBackup.path == "/presets")
            .order_by(ConfigBackup.created_at.desc(), ConfigBackup.id.desc())
            .first()
        )
        radio_xml = snapshot.content if snapshot is not None else ""
        radio_rows = preset_summaries_from_xml(radio_xml) if radio_xml else []
        radio_error = "" if snapshot is not None else "Radio noch nicht ausdrücklich gelesen"
    _runtime_row, runtime = load_runtime_state(db, device_id)
    # An explicit refresh reports this observation, even when both reads fail.
    # Cached availability must not turn failed live reads into a green result.
    providers = live_providers if probe else (runtime.get("providers") or {})
    stream_probes: dict[int, dict] = {}
    if probe:
        for row in local_rows:
            if not row.station_id or row.station_id in stream_probes:
                continue
            station = db.query(Station).filter(Station.id == row.station_id).one_or_none()
            if station is None or not str(station.stream_url or "").strip():
                continue
            decision = external_request_decision(
                db,
                service="preset_checker",
                url_or_host=station.stream_url,
                reason="explicit preset stream check",
                required=True,
                stream_target=True,
                manual_action=True,
            )
            stream_probes[row.station_id] = (
                await probe_stream_reachability(station.stream_url)
                if decision.allowed
                else {
                    "status": "UNKNOWN",
                    "reachable": None,
                    "reason": "blocked by Strict Offline Mode",
                }
            )
    radio = {row["button"]: row for row in radio_rows}
    slots = []
    for button in range(1, 7):
        local_row = local.get(button)
        radio_row = radio.get(button)
        local_station = db.query(Station).filter(Station.id == local_row.station_id).one_or_none() if local_row and local_row.station_id else None
        local_location = local_row.location if local_row else ""
        radio_location = radio_row.get("location", "") if radio_row else ""
        local_source = local_row.source if local_row else ""
        radio_source = radio_row.get("source", "") if radio_row else ""
        empty_on_both = not local_location and not radio_location
        mutation = db.query(PresetMutation).filter(
            PresetMutation.device_id == device_id,
            PresetMutation.button == button,
        ).order_by(PresetMutation.revision.desc()).first()
        location_match = _locations_match(local_location, radio_location)
        same_station_selection = (
            not location_match
            and _locations_select_same_station(local_location, radio_location)
        )
        checks: list[dict] = []
        checks.append(
            _preset_check(
                "radio_readback",
                "BROKEN" if radio_error and probe else "UNKNOWN" if radio_error else "VALID" if probe else "UNKNOWN",
                radio_error or ("live /presets readback received" if probe else "using persisted radio snapshot"),
                evidence={"live": probe, "observed_at": snapshot.created_at.isoformat() if snapshot is not None else None},
            )
        )
        if not local_location and not radio_location:
            checks.append(_preset_check("slot_content", "VALID", "slot is empty on both sides"))
        elif not local_location:
            checks.append(_preset_check("local_mapping", "WARNING", "radio slot exists without a BASSWIESN mapping"))
        elif not radio_location:
            checks.append(_preset_check("radio_slot", "BROKEN", "BASSWIESN mapping exists but radio slot is empty"))
        elif same_station_selection:
            checks.append(_preset_check(
                "location",
                "WARNING",
                "same station selection uses a different BASSWIESN origin or normalized payload",
                evidence={"local": local_location, "radio": radio_location},
            ))
        elif not location_match:
            checks.append(_preset_check("location", "BROKEN", "radio and BASSWIESN locations differ", evidence={"local": local_location, "radio": radio_location}))
        else:
            checks.append(_preset_check("location", "VALID", "radio and BASSWIESN locations match"))

        normalized_source = normalize_source_name(
            radio_source or local_source, fallback=""
        )
        provider_contract = SERVICE_MANIFEST.get(normalized_source)
        if not normalized_source or provider_contract is None:
            checks.append(_preset_check("source", "BROKEN", "preset source is missing or unrecognized", evidence={"source": normalized_source}))
        elif provider_contract.get("contract_status") == "CONFIRMED":
            checks.append(_preset_check("source", "VALID", "source uses a confirmed BASSWIESN contract", evidence={"source": normalized_source}))
        elif provider_contract.get("contract_status") == "UNSUPPORTED":
            checks.append(_preset_check("source", "WARNING", "source depends on an unsupported or obsolete provider contract", evidence={"source": normalized_source}))
        else:
            checks.append(_preset_check("source", "WARNING", "source contract is not confirmed for local preset playback", evidence={"source": normalized_source}))

        stored_local_account = str(local_row.source_account or "") if local_row else ""
        local_account = _canonical_preset_source_account(
            local_source, stored_local_account
        )
        radio_account = str(radio_row.get("source_account", "") or "") if radio_row else ""
        checks.append(
            _preset_check(
                "source_account",
                "VALID" if local_account == radio_account else "BROKEN",
                "sourceAccount matches" if local_account == radio_account else "sourceAccount differs between radio and BASSWIESN",
                evidence={"local": local_account, "radio": radio_account},
            )
        )
        if stored_local_account != local_account:
            checks.append(_preset_check(
                "local_source_account_storage",
                "WARNING",
                "stored legacy sourceAccount is ignored; LOCAL_INTERNET_RADIO requires an empty value",
                evidence={"stored": stored_local_account, "effective": local_account},
            ))
        if local_row and local_row.station_id and local_station is None:
            checks.append(_preset_check("station_mapping", "BROKEN", "referenced BASSWIESN station no longer exists"))
        elif local_row and local_station is None:
            checks.append(_preset_check("station_mapping", "WARNING", "preset has no BASSWIESN station mapping"))
        elif local_station is not None:
            checks.append(_preset_check("station_mapping", "VALID", "BASSWIESN station mapping exists", evidence={"station_id": local_station.id}))

        if local_station is not None:
            compatibility = analyze_stream_url(
                local_station.stream_url_resolved or local_station.stream_url,
                local_station.stream_mime,
            )
            compatibility_status = (
                "BROKEN" if compatibility.is_hls or compatibility.compatibility_score < 40
                else "WARNING" if not compatibility.is_direct_audio or compatibility.compatibility_score < 70
                else "VALID"
            )
            checks.append(_preset_check("codec", compatibility_status, compatibility.compatibility_warning or f"{compatibility.stream_format or 'unknown'} compatibility score {compatibility.compatibility_score}", evidence=compatibility.to_dict()))
            stream_probe = stream_probes.get(local_station.id)
            checks.append(
                _preset_check(
                    "stream_reachability",
                    str(stream_probe.get("status") or "UNKNOWN") if stream_probe else "UNKNOWN",
                    str(stream_probe.get("reason") or "run an explicit checker refresh to test the stream") if stream_probe else "run an explicit checker refresh to test the stream",
                    evidence=stream_probe or {},
                )
            )

        provider_state = providers.get(normalized_source) if normalized_source else None
        if provider_state and provider_state.get("available"):
            checks.append(_preset_check("provider_availability", "VALID", "provider is reported available", evidence=provider_state))
        elif provider_state and (provider_state.get("service_observed") or provider_state.get("source_observed")):
            checks.append(_preset_check("provider_availability", "BROKEN", "provider is reported unavailable", evidence=provider_state))
        else:
            checks.append(_preset_check("provider_availability", "UNKNOWN", "provider availability was not observed", evidence=provider_state or {}))
        checks.append(_preset_check("hardware_button_playability", "UNKNOWN", "physical preset-button playback requires a manual step", affects_verdict=False))
        if empty_on_both:
            checks = [
                item
                for item in checks
                if item["id"]
                in {
                    "radio_readback",
                    "slot_content",
                    "hardware_button_playability",
                }
            ]
        local_xml = local_row.content_item_xml if local_row else ""
        radio_item_xml = radio_row.get("content_item_xml", "") if radio_row else ""
        logo = validate_logo_reference(local_station.image_url) if local_station is not None else {"configured": False, "valid": False, "verification": "not_configured", "reason": "Kein lokaler Sender"}
        changed_fields = []
        if local_source != radio_source and not location_match: changed_fields.append("source")
        if local_location != radio_location and not location_match: changed_fields.append("location")
        if local_xml and radio_item_xml and _canonical_xml(local_xml) != _canonical_xml(radio_item_xml) and not location_match: changed_fields.append("xml")
        if mutation is not None and mutation.diverged:
            checks.append(_preset_check("mutation", "BROKEN", "preset mutation is marked divergent", evidence={"mutation_id": mutation.mutation_id, "revision": mutation.revision, "state": mutation.state}))
        verdict = _preset_verdict(checks)
        message = next((item["message"] for item in checks if item["status"] == verdict), "preset checks complete")
        slots.append({"button": button, "state": verdict.lower(), "verdict": verdict, "message": message, "checks": checks, "changed_fields": changed_fields, "location_match": location_match, "mutation": {"id": mutation.mutation_id, "revision": mutation.revision, "state": mutation.state, "diverged": bool(mutation.diverged), "backup_ref": mutation.backup_ref, "error": mutation.error} if mutation else None, "basswiesn": {"source": local_source, "source_account": local_account, "location": local_location, "title": local_station.name if local_station else "", "provider": local_station.provider if local_station else local_source, "stream_url": local_station.stream_url if local_station else "", "logo_mode": _station_art_mode(db, device_id), "logo": logo, "xml": local_xml}, "radio": {"source": radio_source, "source_account": radio_account, "location": radio_location, "title": radio_row.get("item_name", "") if radio_row else "", "provider": radio_source, "container_art": radio_row.get("container_art", "") if radio_row else "", "xml": radio_item_xml}, "local_location": local_location, "radio_location": radio_location, "local_source": local_source, "radio_source": radio_source})
    sync_row = db.query(RuntimeState).filter(RuntimeState.key == f"preset_sync:{device_id}").one_or_none()
    try:
        sync_state = json.loads(sync_row.value) if sync_row is not None else {}
    except (TypeError, ValueError):
        sync_state = {"status": "unbekannt", "last_error": "gespeicherter Sync-Status unlesbar"}
    return {
        "device_id": device.device_id,
        "radio_ip": device.ip_address,
        "radio_error": radio_error,
        "slots": slots,
        "radio_xml": radio_xml if radio_error else "",
        "sync_state": sync_state,
        "probe_performed": probe,
        "verification_scope": "READ_ONLY_CONFIGURATION_AND_REACHABILITY",
        "audio_playback_verified": False,
        "physical_button_verified": False,
        "radio_snapshot_source": snapshot.path if snapshot is not None else "",
        "radio_observed_at": snapshot.created_at.isoformat() if snapshot is not None else None,
    }


@router.post("/presets/{device_id}/sync")
async def sync_local_presets(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    dry_run = _preset_preview(payload)
    probe = payload.get("probe", False)
    if type(probe) is not bool:
        raise HTTPException(status_code=400, detail="probe must be a JSON boolean")
    async with device_transaction([device_id], enabled=probe or not dry_run):
        device = device_or_404(db, device_id)
        require_memory_checked(device, payload)
        planned_local = local_version(db, device_id)
        rows = db.query(Preset).filter(Preset.device_id == device_id).order_by(Preset.button).all()
        logo_status = []
        for row in rows:
            station = _preset_station(db, row)
            if station is None:
                continue
            logo = validate_logo_reference(station.image_url)
            logo_status.append({"button": row.button, "station_id": station.id, "mode": _station_art_mode(db, device_id), "configured": logo["configured"], "valid": logo["valid"], "verification": logo["verification"], "reason": logo["reason"], "fallback": logo["fallback"]})
        expected: dict[int, str] = {}
        prepared: dict[int, dict] = {}
        current_rows: list[dict] = []
        expected_changes: list[dict] = []
        already_current_slots: list[int] = []
        skipped_slots: list[dict] = []
        preview_readback_error = ""
        readback_requested = probe or not dry_run
        observed = None
        if readback_requested:
            try:
                def read_guard(session, target):
                    require_unprotected_device(target, action="preset_artwork_read", requester="preset_artwork")
                access = PresetAccess(db, device, write=not dry_run,
                    guard=read_guard if dry_run else enforce_ip_write_guard,
                    client_factory=lambda *args, **kwargs: _soundtouch_client_for(
                        device, purpose="preset_sync_preview", trigger="device_settings_logo_preview"))
                observed = await access.observe()
                current_rows = observed.rows
                prepared, already_current_slots, skipped_slots = _prepare_artwork_only_sync(
                    db, device_id, rows, current_rows
                )
                expected = {
                    button: str(item.get("location") or "")
                    for button, item in prepared.items()
                }
                expected_changes = [
                    {
                        "button": button,
                        "location": item.get("location", ""),
                        "change": item.get("change", "update only containerArt"),
                        "preserves": ["source", "sourceAccount", "location", "itemName"],
                    }
                    for button, item in prepared.items()
                ]
            except Exception:
                preview_readback_error = "Preset identity or bounded readback was not confirmed."
        elif dry_run:
            preview_readback_error = "Explicit radio readback is required for an artwork-only preview."
        setting_rows = {row.key: row.value for row in db.query(Setting).all()}
        guard_enabled = str(setting_rows.get("ip_write_guard", "false")).lower() in {"true", "1", "yes", "on"}
        allowed_ips = {item.strip() for item in (setting_rows.get("ip_write_allowed_ips", "").replace(";", ",").split(",")) if item.strip()}
        allowed_ips.update(get_settings().setup_write_radio_ips)
        write_allowed = not guard_enabled or device.ip_address in allowed_ips
        write_blocker = ""
        protected = is_device_access_protected(device.ip_address, device.device_id)
        if protected:
            write_allowed = False
            write_blocker = "Protected-device policy blocks all radio access."
        elif not write_allowed:
            write_blocker = "IP Write Guard erlaubt dieses Ziel nicht."
        preview = {
            "dry_run": True,
            "device_id": device_id,
            "target": {"device_id": device.device_id, "ip_address": device.ip_address, "name": device.name},
            "expected_slots": expected,
            "expected_changes": expected_changes,
            "already_current_slots": already_current_slots,
            "skipped_slots": skipped_slots,
            "preview_readback_performed": readback_requested and not preview_readback_error,
            "preview_readback_error": preview_readback_error,
            "logo_status": logo_status,
            "protection": {"protected": protected, "protected_ip": is_protected_ip(device.ip_address), "write_guard_enabled": guard_enabled, "write_allowed": write_allowed, "write_blocker": write_blocker},
            "memory_check_required": True,
            "radio_action": "none",
            "sync_scope": "containerArt only; live radio selection identity is preserved",
        }
        if dry_run:
            return preview
        if preview_readback_error or observed is None:
            raise HTTPException(status_code=502, detail={"error": "radio preset readback failed before artwork sync", "message": preview_readback_error})
        try:
            radio_rows = await _apply_prepared(device, prepared, db, backup_label="preset-artwork-sync",
                operation="ARTWORK", expected_before=observed, expected_local=planned_local)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            integrity = detail.get("integrity", {})
            slot_results = integrity.get("slot_results", detail.get("slot_results", []))
            failed_slots = sum(1 for item in slot_results if item.get("status") in {"failed", "mismatch", "missing"})
            try:
                _record_preset_sync_state(db, device_id, {"status": integrity.get("overall_status", "failed"), "verified": False, "last_error": str(exc.detail)[:500], "successful_slots": sum(1 for item in slot_results if item.get("status") in {"verified", "unchanged"}), "different_slots": failed_slots or len(detail.get("unexpected_changes", [])), "slot_results": slot_results, "integrity": integrity})
            except Exception:
                db.rollback()  # Secondary UI bookkeeping must not mask the failed transaction.
            raise
        integrity = _integrity_for_sync_result(radio_rows, expected)
        verified = integrity["overall_status"] == "verified" and not skipped_slots
        status_saved = True
        try:
            _record_preset_sync_state(db, device_id, {"status": "verified" if verified else "partial", "verified": verified,
                "last_error": "", "successful_slots": len(prepared) + len(already_current_slots),
                "different_slots": len(skipped_slots), "radio_slots": radio_rows, "integrity": integrity})
        except Exception:
            db.rollback()
            status_saved = False  # Do not recast committed hardware state as a failed command.
        return {"dry_run": False, "device_id": device_id, "target": preview["target"], "verified": verified,
            "status": "verified" if verified else "partial",
            "radio_slots": radio_rows, "expected_slots": expected, "integrity": integrity, "logo_status": logo_status,
            "protection": preview["protection"], "skipped_slots": skipped_slots, "status_saved": status_saved}


@router.post("/presets/{device_id}/{button}")
async def set_preset(device_id: str, button: int, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    dry_run = _preset_preview(payload)
    if button not in range(1, 7):
        raise HTTPException(status_code=400, detail="preset button must be between 1 and 6")
    async with device_transaction([device_id], enabled=not dry_run):
        device = device_or_404(db, device_id)
        require_memory_checked(device, payload)
        station = db.query(Station).filter(Station.id == payload["station_id"]).one_or_none()
        if station is None:
            raise HTTPException(status_code=404, detail="station not found")
        media = media_type_for_url(station.stream_url)
        if media["status"] == "blocked":
            raise HTTPException(status_code=400, detail={"error": "unsupported media type", "media": media})
        descriptor = _device_station_descriptor(db, device.device_id, station)
        location = _station_location_or_409(descriptor, db, request)
        source = "LOCAL_INTERNET_RADIO"
        xml = _device_content_item_xml(db, device.device_id, station, location, source)
        if dry_run:
            return {"dry_run": True, "device_id": device_id, "button": button, "location": location, "xml": xml, "media": media, "memory_check": memory_check_plan(device)}
        prepared = {button: {"station_id": station.id, "source": source, "source_account": "",
                             "location": location, "content_item_xml": xml}}
        radio_rows = await _apply_prepared(device, prepared, db, backup_label=f"preset-write/{button}")
        verified_slot = next(item for item in radio_rows if item["button"] == button)
        return {"dry_run": False, "verified": True, "device_id": device_id, "button": button,
                "location": location, "media": media, "radio_slot": verified_slot, "integrity": radio_rows.integrity}


@router.delete("/presets/{device_id}/{button}")
async def delete_preset(device_id: str, button: int, db: Session = Depends(get_db),
                        expected_revision: int | None = None) -> dict:
    device = device_or_404(db, device_id)
    if button not in range(1, 7):
        raise HTTPException(status_code=400, detail="preset button must be between 1 and 6")
    enforce_ip_write_guard(db, device)
    try:
        return await remove_preset(db, device, button, client_factory=SoundTouchClient,
            guard=enforce_ip_write_guard, expected_revision=expected_revision)
    except PresetRecoveryError as exc:
        raise HTTPException(status_code=502 if exc.detail["reconciliation_required"] else 409, detail=exc.detail) from None


@router.post("/presets/clone")
async def clone_presets(payload: dict, db: Session = Depends(get_db)) -> dict:
    source_device_id, target_device_id = payload.get("source_device_id"), payload.get("target_device_id")
    if not isinstance(source_device_id, str) or not isinstance(target_device_id, str) or not source_device_id or not target_device_id or source_device_id.upper() == target_device_id.upper():
        raise HTTPException(status_code=400, detail="distinct source_device_id and target_device_id are required")
    dry_run = _preset_preview(payload, default=False)
    async with device_transaction([source_device_id, target_device_id], enabled=not dry_run):
        source_device = device_or_404(db, source_device_id)
        target_device = device_or_404(db, target_device_id)
        frozen_source = device_fingerprint(source_device)
        source_version = local_version(db, source_device_id)
        source_rows = db.query(Preset).filter(Preset.device_id == source_device_id).order_by(Preset.button).all()
        if not source_rows:
            raise HTTPException(status_code=404, detail="source device has no presets")
        prepared = {row.button: {"station_id": row.station_id, "source": row.source,
            "source_account": row.source_account, "location": row.location,
            "content_item_xml": row.content_item_xml} for row in source_rows}
        result = {"dry_run": dry_run, "source_device_id": source_device_id,
                  "target_device_id": target_device_id, "buttons": list(prepared)}
        if dry_run:
            return {**result, "verified": False, "source_basis": "saved_local_presets"}
        def unchanged_source():
            db.refresh(source_device)
            if device_fingerprint(source_device) != frozen_source or local_version(db, source_device_id) != source_version:
                raise ValueError("clone source changed; no automatic rebase")
        radio_rows = await _apply_prepared(target_device, prepared, db, backup_label="preset-clone",
                                          operation="CLONE", precondition=unchanged_source)
        return {**result, "verified": True, "radio_slots": radio_rows, "integrity": radio_rows.integrity}
