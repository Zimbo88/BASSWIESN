import xml.etree.ElementTree as ET
import asyncio
import json
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.db import get_db
from basswiesn.app.models import ConfigBackup, PlayHistory, Preset, PresetMutation, RuntimeState, Setting, Station, utc_now
from basswiesn.app.routers.shared import device_or_404, enforce_ip_write_guard, memory_check_plan, require_memory_checked
from basswiesn.app.services.orion import (
    ORION_STATION_PATH,
    OrionLocationError,
    StationDescriptor,
    decode_orion_data,
    station_location,
)
from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.services.xml import content_item_xml
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.services.device_state import (
    load_runtime_state,
    merge_provider_maps,
    parse_service_availability_xml,
    parse_sources_xml,
    update_runtime_state,
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
from basswiesn.app.services.protected_devices import is_device_access_protected, is_protected_ip
from basswiesn.app.services.device_policy import policy_for_device
from basswiesn.app.services.safe_uploads import InvalidUpload, UploadError, UploadQuotaExceeded, UploadTooLarge, UnsupportedUploadType, store_upload
from basswiesn.app.services.preset_transactions import (
    prepare_preset_mutation,
    transition_preset_mutation,
)
from basswiesn.app.services.action_journal import record_action
from basswiesn.app.services.setup_rebuild.audio_safety import lock_audio_safety
from basswiesn.app.services.playback_safety_gate import (
    arm_playback_safety_gate,
    clear_playback_safety_gate,
    fail_playback_safety_gate,
    verify_playback_safety_gate,
)

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


async def _stream_source_for_device(device, fallback: str = "") -> str:
    try:
        return _stream_source_from_sources_xml(await SoundTouchClient(device.ip_address).get_xml("/sources"), fallback)
    except Exception:
        return fallback if fallback in STREAM_SOURCE_PRIORITY else ""


def _require_stream_source(source: str, device_id: str) -> str:
    if source:
        return source
    raise HTTPException(status_code=409, detail={"error": "no stream-capable source ready", "device_id": device_id, "priority": list(STREAM_SOURCE_PRIORITY)})


def _xml_preview(xml: str, limit: int = 600) -> str:
    return " ".join((xml or "").split())[:limit]


def _radio_response_preview(value: str, limit: int = 600) -> str:
    return " ".join((value or "").split())[:limit]


def _compatible_source(station: Station) -> str:
    return normalize_source_name(station.provider)


def _source_attempt_order(db: Session, device_id: str, station: Station) -> list[str]:
    row = db.query(Setting).filter(Setting.key == f"playback_source:{device_id}").one_or_none()
    preferred = ["LOCAL_INTERNET_RADIO"]
    compatible = _compatible_source(station)
    if compatible in STREAM_SOURCE_PRIORITY:
        preferred.append(compatible)
    if row and normalize_source_name(row.value) in STREAM_SOURCE_PRIORITY:
        preferred.append(normalize_source_name(row.value))
    result = []
    for source in preferred:
        if source and source not in result:
            result.append(source)
    return result


async def _live_source_attempt_order(
    client: SoundTouchClient,
    db: Session,
    device_id: str,
    station: Station,
) -> list[str]:
    """Restrict fallback attempts to sources the radio currently exposes."""

    preferred = _source_attempt_order(db, device_id, station)
    try:
        candidates = _source_candidates_from_xml(await client.get_xml("/sources"))
    except Exception as exc:
        write_masterlog(
            "playback_source_readback_failed",
            device_id=device_id,
            error=str(exc)[:300],
            fallback_source=preferred[0] if preferred else "",
        )
        return preferred[:1]
    return [source for source in preferred if candidates.get(source)]


def _volume_and_mute(xml_text: str) -> tuple[int, bool]:
    root = ET.fromstring(xml_text or "")
    volume_text = root.findtext("actualvolume") or root.findtext("targetvolume") or "-1"
    mute_text = root.findtext("muteenabled") or root.findtext("mute") or root.attrib.get("muted") or "false"
    return int(float(volume_text)), str(mute_text).strip().lower() == "true"


async def _set_mute_readback(client: SoundTouchClient, enabled: bool) -> tuple[int, bool]:
    state = (-1, not enabled)
    for _attempt in range(3):
        state = _volume_and_mute(await client.get_xml("/volume"))
        if state[1] == enabled:
            return state
        for key_state in ("press", "release"):
            await client.post_xml("/key", f'<key state="{key_state}" sender="Gabbo">MUTE</key>')
        await asyncio.sleep(0.25)
        state = _volume_and_mute(await client.get_xml("/volume"))
        if state[1] == enabled:
            return state
    expected = "enabled" if enabled else "disabled"
    raise OSError(f"safe playback mute could not be confirmed {expected}")


async def _restore_volume_readback(client: SoundTouchClient, safe_volume: int) -> int:
    observed = -1
    for _attempt in range(3):
        await client.post_xml("/volume", f"<volume>{safe_volume}</volume>")
        await asyncio.sleep(0.2)
        observed, _muted = _volume_and_mute(await client.get_xml("/volume"))
        if observed == safe_volume:
            return observed
    raise OSError(f"safe volume restoration failed: requested {safe_volume}, read-back {observed}")


async def _safe_playback_failure_cleanup(
    client: SoundTouchClient,
    db: Session,
    device,
    safe_volume: int | None,
) -> dict:
    """Stop a failed selection and prove the radio is safe before returning."""

    if safe_volume is None:
        return {"required": False}
    errors: list[str] = []
    observed_before_cleanup: int | None = None
    try:
        observed_before_cleanup, _muted = _volume_and_mute(await client.get_xml("/volume"))
    except Exception as exc:
        errors.append(f"volume observation: {exc}")
    try:
        for key_state in ("press", "release"):
            await client.post_xml("/key", f'<key state="{key_state}" sender="Gabbo">STOP</key>')
        await client.get_xml("/standby")
    except Exception as exc:
        errors.append(f"STOP/STANDBY: {exc}")
    confirmed_volume: int | None = None
    try:
        confirmed_volume = await _restore_volume_readback(client, safe_volume)
    except Exception as exc:
        errors.append(str(exc))
    excursion = observed_before_cleanup is not None and observed_before_cleanup != safe_volume
    locked = excursion or bool(errors) or confirmed_volume != safe_volume
    if locked:
        parts = []
        if excursion:
            parts.append(
                f"Source-Auswahl änderte Lautstärke von {safe_volume} auf {observed_before_cleanup}."
            )
        parts.extend(errors)
        if confirmed_volume != safe_volume:
            parts.append(f"finaler Volume-{safe_volume}-Readback fehlt")
        lock_audio_safety(db, device.device_id, "; ".join(parts)[:500])
    stopped_and_standby = not any(item.startswith("STOP/STANDBY") for item in errors)
    write_masterlog(
        "playback_failure_safety_cleanup",
        device_id=device.device_id,
        radio_ip=device.ip_address,
        requested_volume=safe_volume,
        observed_before_cleanup=observed_before_cleanup,
        confirmed_volume=confirmed_volume,
        stopped_and_standby=stopped_and_standby,
        locked=locked,
        errors=errors,
    )
    return {
        "required": True,
        "observed_before_cleanup": observed_before_cleanup,
        "confirmed_volume": confirmed_volume,
        "stopped_and_standby": stopped_and_standby,
        "locked": locked,
        "recovery": "Run the visible audio safety check before another playback attempt." if locked else "",
        "errors": errors,
    }


async def _wait_for_source_acceptance(
    client: SoundTouchClient,
    *,
    device,
    station: Station,
    source: str,
    location: str,
    xml: str,
) -> tuple[bool, str]:
    """Wait for the asynchronous provider selection to converge.

    `/select` acknowledges queueing before BMX has resolved the station URL.
    An immediate `INVALID_SOURCE` readback is therefore not authoritative: on
    hardware the provider request arrived shortly afterwards.  Keep polling
    the radio readback, while never inferring playback from stream reachability.
    """

    attempts = SOURCE_ACCEPTANCE_ATTEMPTS
    delay = SOURCE_ACCEPTANCE_INTERVAL_SECONDS
    last = ""
    for attempt in range(1, attempts + 1):
        last = await client.get_xml("/now_playing")
        try:
            root = ET.fromstring(last or "")
            observed_source = normalize_source_name(root.attrib.get("source"))
            play_status = str(root.findtext("playStatus") or "").strip().upper()
        except ET.ParseError:
            observed_source = ""
            play_status = ""
        invalid = any(
            token in (last or "").upper()
            for token in ("INVALID_SOURCE", "UNKNOWN_SOURCE_ERROR")
        )
        accepted = (
            observed_source == source
            and not invalid
            and play_status in {"PLAY_STATE", "BUFFERING_STATE"}
        )
        write_masterlog(
            "now_playing_after_select",
            device_id=device.device_id,
            radio_ip=device.ip_address,
            station_id=station.id,
            source=source,
            observed_source=observed_source,
            play_status=play_status,
            location=location,
            endpoint="/now_playing",
            status_code=200,
            attempt=attempt,
            accepted=accepted,
            radio_response_preview=_radio_response_preview(last),
            location_is_absolute=location.startswith("http://"),
            xml_preview=_xml_preview(xml),
        )
        if accepted:
            return True, last
        if attempt < attempts and delay:
            await asyncio.sleep(delay)
    return False, last


def _learn_playback_source(db: Session, device_id: str, source: str) -> None:
    key = f"playback_source:{device_id}"
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    if row is None:
        row = Setting(key=key)
        db.add(row)
    row.value = source


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


def preset_summaries_from_xml(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    rows = []
    for index, node in enumerate([item for item in root.iter() if _xml_local_name(item.tag) == "preset"], start=1):
        content = next((item for item in node.iter() if _xml_local_name(item.tag) == "ContentItem"), None)
        button = node.attrib.get("id") or node.attrib.get("button") or node.attrib.get("index") or str(index)
        rows.append({
            "button": int(button or 0),
            "source": content.attrib.get("source", "") if content is not None else "",
            "source_account": content.attrib.get("sourceAccount", "") if content is not None else "",
            "type": content.attrib.get("type", "") if content is not None else "",
            "location": content.attrib.get("location", "") if content is not None else "",
            "item_name": _child_text(content, "itemName") if content is not None else "",
            "container_art": _child_text(content, "containerArt") if content is not None else "",
            "container_art_present": bool(content is not None and any(_xml_local_name(child.tag) == "containerArt" for child in content)),
            "content_item_xml": ET.tostring(content, encoding="unicode") if content is not None else "",
        })
    return rows


def _decoded_orion_station(location: str) -> dict:
    try:
        parsed = urlparse(location or "")
    except ValueError:
        return {}
    if ORION_STATION_PATH not in parsed.path:
        return {}
    data = parse_qs(parsed.query).get("data", [""])[0]
    if not data:
        return {}
    try:
        decoded = decode_orion_data(data)
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _canonical_preset_source_account(source: str, value: str) -> str:
    """Return the firmware contract value, not a stale display-name import."""

    if normalize_source_name(source) == "LOCAL_INTERNET_RADIO":
        return ""
    return str(value or "")


def _preset_content_item_xml(
    item: dict,
    *,
    location: str,
    source: str,
    source_account: str,
    name: str,
    art: str,
    include_art: bool = True,
    empty_art: bool = False,
    canonicalize_source_account: bool = True,
) -> str:
    try:
        root = ET.fromstring(item.get("content_item_xml") or "")
    except ET.ParseError:
        root = ET.Element("ContentItem")
    root.attrib["source"] = normalize_source_name(source)
    root.attrib["type"] = item.get("type") or root.attrib.get("type") or "stationurl"
    root.attrib["location"] = location
    root.attrib["sourceAccount"] = (
        _canonical_preset_source_account(source, source_account)
        if canonicalize_source_account
        else str(source_account or "")
    )
    root.attrib["isPresetable"] = "true"
    item_name = next((child for child in root if _xml_local_name(child.tag) == "itemName"), None)
    if item_name is None:
        item_name = ET.SubElement(root, "itemName")
    item_name.text = name or item.get("item_name") or "Unbenanntes Preset"
    container_art = next((child for child in root if _xml_local_name(child.tag) == "containerArt"), None)
    if include_art and art:
        if container_art is None:
            container_art = ET.SubElement(root, "containerArt")
        container_art.text = art
    elif empty_art:
        if container_art is None:
            container_art = ET.SubElement(root, "containerArt")
        container_art.text = ""
    elif container_art is not None:
        root.remove(container_art)
    return ET.tostring(root, encoding="unicode")


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


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(node, local_name: str) -> str:
    if node is None:
        return ""
    child = next((item for item in node.iter() if _xml_local_name(item.tag) == local_name), None)
    return (child.text or "").strip() if child is not None else ""


def _orion_data_key(location: str) -> str:
    try:
        parsed = urlparse(location or "")
    except ValueError:
        return ""
    data = parse_qs(parsed.query).get("data", [""])[0]
    return unquote(data or "")


def _locations_match(left: str, right: str) -> bool:
    left_clean = (left or "").strip()
    right_clean = (right or "").strip()
    if not left_clean or not right_clean:
        return False
    if left_clean == right_clean:
        return True
    left_data = _orion_data_key(left_clean)
    right_data = _orion_data_key(right_clean)
    return bool(left_data and right_data and left_data == right_data)


def _locations_select_same_station(left: str, right: str) -> bool:
    """Compare selection identity while ignoring server origin/extra hints.

    This deliberately does not replace the strict write/readback comparator.
    It is used only by the read-only Preset Checker so a valid preset hosted
    by another BASSWIESN endpoint is shown as a warning instead of falsely as
    an unplayable station.
    """

    left_item = _decoded_orion_station(left)
    right_item = _decoded_orion_station(right)
    if not left_item or not right_item:
        return False
    for key in ("streamUrl", "stream_url", "tuneinId", "providerStationId"):
        left_value = str(left_item.get(key) or "").strip()
        right_value = str(right_item.get(key) or "").strip()
        if left_value and right_value:
            return left_value == right_value
    return False


def _canonical_xml(xml_text: str) -> str:
    try:
        return ET.tostring(ET.fromstring(xml_text or ""), encoding="unicode")
    except ET.ParseError:
        return (xml_text or "").strip()


class PresetSyncResult(list):
    """List-compatible radio rows with a machine-readable integrity report."""

    def __init__(self, rows: list[dict], integrity: dict):
        super().__init__(rows)
        self.integrity = integrity


def classify_notification_sequence(sequence: list[str] | tuple[str, ...]) -> dict:
    """Classify the four notification-order variants without contacting a radio."""
    normalized = tuple(str(item or "").strip() for item in sequence)
    variants = {
        ("storePreset", "readback", "notification", "final_readback"): {
            "variant": "A",
            "safe_for_full_sync": False,
            "write_integrity_verified": False,
            "reason": "Eine clientseitig injizierte presetsUpdated-Notification kann einen gerade geschriebenen Slot aus dem noch alten Presetstand wieder leeren.",
        },
        ("notification", "storePreset", "readback"): {
            "variant": "B",
            "safe_for_full_sync": False,
            "write_integrity_verified": False,
            "reason": "Notification vor dem Store kann Firmware- oder Cache-Normalisierung ausloesen.",
        },
        ("storePreset", "readback"): {
            "variant": "C",
            "safe_for_full_sync": True,
            "write_integrity_verified": True,
            "reason": "Store und stabiler Readback sind der bestaetigte Clientvertrag; presetsUpdated wird vom Radio selbst als Ereignis erzeugt und nicht injiziert.",
        },
        ("notification",): {
            "variant": "D",
            "safe_for_full_sync": False,
            "write_integrity_verified": False,
            "reason": "Eine Notification ohne Store kann keinen Preset-Write bestaetigen.",
        },
    }
    result = variants.get(normalized)
    if result is None:
        return {
            "variant": "unbekannt",
            "safe_for_full_sync": False,
            "write_integrity_verified": False,
            "reason": "Unbekannte oder unvollstaendige Reihenfolge erfordert manuelle Pruefung.",
        }
    return {**result, "sequence": list(normalized)}


def _preset_projection(item: dict) -> dict:
    return {
        "button": int(item.get("button") or 0),
        "source": item.get("source", ""),
        "source_account": item.get("source_account", ""),
        "type": item.get("type", ""),
        "location": item.get("location", ""),
        "item_name": item.get("item_name", ""),
        "container_art": item.get("container_art", ""),
        "container_art_present": bool(item.get("container_art_present", False)),
        "orion_payload": _orion_data_key(item.get("location", "")),
        "content_item_xml": _canonical_xml(item.get("content_item_xml", "")),
    }


def _preset_contract_matches(actual: dict | None, expected: dict | None) -> bool:
    if not actual or not expected:
        return False
    if not _locations_match(actual.get("location", ""), expected.get("location", "")):
        return False
    return all(
        actual.get(field, "") == expected.get(field, "")
        for field in (
            "source",
            "source_account",
            "type",
            "item_name",
            "container_art",
            "container_art_present",
        )
    )


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
        desired_xml = _preset_content_item_xml(
            actual,
            location=str(actual.get("location") or ""),
            source=str(actual.get("source") or preset.source or ""),
            source_account=str(actual.get("source_account") or ""),
            name=str(actual.get("item_name") or station.name or ""),
            art=str(station.image_url or "") if include_art else "",
            include_art=include_art,
            empty_art=empty_art,
            canonicalize_source_account=False,
        )
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


def _commit_radio_preset_projection(
    db: Session,
    device_id: str,
    radio_rows: list[dict],
) -> None:
    """Commit only verified radio readback into existing local slot mappings."""

    for item in radio_rows:
        button = int(item.get("button") or 0)
        preset = db.query(Preset).filter(
            Preset.device_id == device_id,
            Preset.button == button,
        ).one_or_none()
        if preset is None:
            continue
        source = normalize_source_name(item.get("source"))
        preset.source = source
        preset.source_account = _canonical_preset_source_account(
            source, item.get("source_account") or ""
        )
        preset.location = str(item.get("location") or preset.location or "")
        preset.content_item_xml = str(item.get("content_item_xml") or preset.content_item_xml or "")
        preset.updated_at = utc_now()


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


def compare_preset_snapshots(
    before_xml: str,
    after_xml: str,
    expected: dict[int, str],
    *,
    expected_items: dict[int, dict] | None = None,
    allowed_orion_origins: set[str] | None = None,
) -> dict:
    """Compare target slots and every untouched slot without hiding changes."""
    before_rows = {int(item["button"]): _preset_projection(item) for item in preset_summaries_from_xml(before_xml)}
    after_rows = {int(item["button"]): _preset_projection(item) for item in preset_summaries_from_xml(after_xml)}
    target_buttons = {int(button) for button in expected}
    unexpected: list[dict] = []
    slot_results: list[dict] = []
    normalization_detected: list[dict] = []

    target_verified = True
    for button, location in expected.items():
        actual = after_rows.get(int(button))
        target_expected = (expected_items or {}).get(int(button), {"button": button, "location": location})
        target_ok = bool(actual and _locations_match(actual.get("location", ""), location))
        for field in ("source", "source_account", "type", "item_name", "container_art", "container_art_present"):
            expected_value = target_expected.get(field)
            if expected_value not in (None, "") and actual and actual.get(field, "") != expected_value:
                target_ok = False
                unexpected.append({"button": int(button), "scope": "target", "field": field, "before": target_expected.get(field, ""), "after": actual.get(field, "")})
        target_verified = target_verified and target_ok
        slot_results.append({
            "button": int(button),
            "scope": "target",
            "status": "verified" if target_ok else "mismatch" if actual else "missing",
            "expected": {"location": location, "source": target_expected.get("source", ""), "source_account": target_expected.get("source_account", ""), "type": target_expected.get("type", ""), "item_name": target_expected.get("item_name", ""), "container_art": target_expected.get("container_art", ""), "container_art_present": target_expected.get("container_art_present", False)},
            "actual": actual or {},
        })

    untouched_buttons = set(before_rows) - target_buttons
    untouched_verified = True
    for button in sorted(untouched_buttons):
        before = before_rows[button]
        after = after_rows.get(button)
        unchanged = after == before
        if not unchanged and after:
            after_url = urlparse(after.get("location", ""))
            after_origin = f"{after_url.scheme}://{after_url.netloc}" if after_url.scheme and after_url.netloc else ""
            same_contract = (
                _locations_match(before.get("location", ""), after.get("location", ""))
                and all(
                    before.get(field, "") == after.get(field, "")
                    for field in ("source", "source_account", "type", "item_name", "container_art", "container_art_present", "orion_payload")
                )
            )
            if same_contract and after_origin in (allowed_orion_origins or set()):
                unchanged = True
                normalization_detected.append(
                    {
                        "button": button,
                        "kind": "approved_local_orion_origin_migration",
                        "before": before.get("location", ""),
                        "after": after.get("location", ""),
                    }
                )
        untouched_verified = untouched_verified and unchanged
        slot_results.append({"button": button, "scope": "untouched", "status": "normalized" if any(item["button"] == button for item in normalization_detected) else "unchanged" if unchanged else "changed" if after else "missing", "before": before, "actual": after or {}})
        if not unchanged:
            unexpected.append({"button": button, "scope": "untouched", "reason": "slot changed", "before": before, "after": after or {}})

    extra_buttons = set(after_rows) - set(before_rows) - target_buttons
    for button in sorted(extra_buttons):
        untouched_verified = False
        unexpected.append({"button": button, "scope": "untouched", "reason": "unexpected slot added", "after": after_rows[button]})

    if target_verified and untouched_verified and not unexpected:
        overall_status = "verified"
    elif unexpected:
        overall_status = "integrity_failure"
    else:
        overall_status = "partial_failure"
    return {
        "target_verified": target_verified,
        "untouched_slots_verified": untouched_verified,
        "normalization_detected": normalization_detected,
        "unexpected_changes": unexpected,
        "rollback_recommended": bool(unexpected),
        "manual_review_required": bool(unexpected) or not target_verified,
        "overall_status": overall_status,
        "slot_results": slot_results,
        "before_slot_count": len(before_rows),
        "after_slot_count": len(after_rows),
    }


def _integrity_for_sync_result(radio_rows: list[dict], expected: dict[int, str]) -> dict:
    """Keep old list-only test/service doubles honest without claiming proof."""
    report = getattr(radio_rows, "integrity", None)
    if isinstance(report, dict):
        return report
    target_verified = all(
        any(int(row.get("button") or 0) == int(button) and _locations_match(row.get("location", ""), location) for row in radio_rows)
        for button, location in expected.items()
    )
    return {
        "target_verified": target_verified,
        "untouched_slots_verified": False,
        "normalization_detected": [],
        "unexpected_changes": [],
        "rollback_recommended": False,
        "manual_review_required": True,
        "overall_status": "manual_review_required",
        "slot_results": [],
        "integrity_source": "legacy list-only adapter",
    }


async def sync_presets_to_radio(
    device,
    expected: dict[int, str],
    db: Session,
    backup_label: str = "preset-sync",
    *,
    prepared_presets: dict[int, dict] | None = None,
    mutations: dict[int, PresetMutation] | None = None,
) -> PresetSyncResult:
    """Write selected slots and require stable full-radio readback.

    ``presetsUpdated`` is a radio-owned event.  A real factory-fresh Portable
    showed that POSTing a synthetic notification can asynchronously restore
    the previous (empty) preset snapshot after an initially successful
    ``/storePreset`` readback.  The client therefore never injects that event.
    """
    enforce_ip_write_guard(db, device)
    client = _soundtouch_client_for(device, purpose="preset_sync", trigger="stations_presets")
    try:
        before_xml = await client.get_xml("/presets")
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": "could not back up radio presets", "message": str(exc)}) from exc
    backup_ref = f"{backup_label}/before.xml"
    db.add(ConfigBackup(device_id=device.device_id, path=backup_ref, content=before_xml))
    db.commit()
    for mutation in (mutations or {}).values():
        transition_preset_mutation(
            db,
            mutation,
            "RADIO_WRITE",
            before_radio=before_xml,
            backup_ref=backup_ref,
        )

    slot_results = [{"button": button, "expected_location": location, "status": "pending"} for button, location in expected.items()]
    expected_items = {}
    allowed_orion_origins = {
        f"{parsed.scheme}://{parsed.netloc}"
        for location in expected.values()
        if (parsed := urlparse(location or "")).scheme and parsed.netloc
    }
    for button, location in expected.items():
        prepared = (prepared_presets or {}).get(button)
        if prepared and prepared.get("content_item_xml"):
            parsed = preset_summaries_from_xml(
                f'<presets><preset id="{button}">{prepared["content_item_xml"]}</preset></presets>'
            )
            expected_items[button] = parsed[0] if parsed else {
                "button": button,
                "location": location,
                "source": prepared.get("source", ""),
            }
        else:
            expected_items[button] = _expected_preset_summary(
                db, device.device_id, button, location
            )
    last_xml = before_xml
    before_items = {
        int(item["button"]): _preset_projection(item)
        for item in preset_summaries_from_xml(before_xml)
    }
    notification_sequence = ["storePreset"]
    for button, location in expected.items():
        prepared = (prepared_presets or {}).get(button)
        preset = db.query(Preset).filter(Preset.device_id == device.device_id, Preset.button == button).one_or_none()
        content_item = str((prepared or {}).get("content_item_xml") or "")
        if not content_item and preset is not None:
            content_item = _effective_preset_content_item_xml(
                db, device.device_id, preset, location_override=location
            )
        if not content_item:
            continue
        desired = _preset_projection(expected_items.get(button, {}))
        if _preset_contract_matches(before_items.get(int(button)), desired):
            next(item for item in slot_results if item["button"] == button).update({
                "status": "unchanged",
                "actual_location": before_items[int(button)].get("location", ""),
            })
            continue
        body = f'<preset id="{button}">{content_item}</preset>'
        station_id = (prepared or {}).get("station_id") or (preset.station_id if preset else None)
        preset_source = (prepared or {}).get("source") or (preset.source if preset else "")
        preset_location = (prepared or {}).get("location") or (preset.location if preset else location)
        try:
            next(item for item in slot_results if item["button"] == button)["status"] = "writing"
            write_masterlog("preset_apply_store_sent", device_id=device.device_id, radio_ip=device.ip_address, button=button, station_id=station_id, source=preset_source, location=preset_location, endpoint="/storePreset", xml_preview=_xml_preview(body))
            await client.post_xml("/storePreset", body)
            next(item for item in slot_results if item["button"] == button)["status"] = "written_waiting_for_readback"
        except Exception as exc:
            next(item for item in slot_results if item["button"] == button).update({"status": "failed", "error": str(exc) or exc.__class__.__name__})
            status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            radio_response = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else str(exc) or exc.__class__.__name__
            write_masterlog("preset_apply_failed", device_id=device.device_id, radio_ip=device.ip_address, button=button, station_id=station_id, source=preset_source, location=preset_location, endpoint="/storePreset", status_code=status_code, radio_response_preview=_radio_response_preview(radio_response), xml_preview=_xml_preview(body), error=str(exc) or exc.__class__.__name__)
            raise HTTPException(status_code=502, detail={"error": "radio direct /storePreset failed", "button": button, "message": str(exc), "local_saved": False, "slot_results": slot_results}) from exc
    for mutation in (mutations or {}).values():
        transition_preset_mutation(db, mutation, "RADIO_READBACK")
    integrity: dict = {}
    readback_delays = (0, *([0.5] * 11), 1, 2, 3)
    for attempt, delay in enumerate(readback_delays, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            last_xml = await client.get_xml("/presets")
            radio_rows = preset_summaries_from_xml(last_xml)
            integrity = compare_preset_snapshots(
                before_xml,
                last_xml,
                expected,
                expected_items=expected_items,
                allowed_orion_origins=allowed_orion_origins,
            )
            if integrity["target_verified"] and integrity["untouched_slots_verified"] and not integrity["unexpected_changes"]:
                break
        except Exception:
            continue
    db.add(ConfigBackup(device_id=device.device_id, path=f"{backup_label}/after-storePreset.xml", content=last_xml))
    db.commit()
    notification_sequence.append("readback")
    integrity["notification_sequence"] = classify_notification_sequence(notification_sequence)
    if integrity.get("overall_status") != "verified":
        raise HTTPException(status_code=502, detail={"error": "preset integrity check failed after storePreset", "device_id": device.device_id, "expected_slots": expected, "integrity": integrity, "radio_presets": last_xml, "local_saved": False})

    # A single immediate GET can only prove the radio's in-memory response.
    # Two later reads make asynchronous persistence/cache rollback visible.
    final_xml = last_xml
    final_integrity = integrity
    stability_readbacks: list[dict] = []
    for delay in (0.5, 1.0):
        await asyncio.sleep(delay)
        try:
            final_xml = await client.get_xml("/presets")
        except Exception as exc:
            raise HTTPException(status_code=502, detail={
                "error": "radio preset stability readback failed",
                "message": str(exc),
                "local_saved": False,
                "integrity": final_integrity,
            }) from exc
        final_integrity = compare_preset_snapshots(
            before_xml,
            final_xml,
            expected,
            expected_items=expected_items,
            allowed_orion_origins=allowed_orion_origins,
        )
        stability_readbacks.append({
            "delay_seconds": delay,
            "presets_sha256": sha256(final_xml.encode("utf-8")).hexdigest(),
            "overall_status": final_integrity.get("overall_status", "unknown"),
        })
        if final_integrity.get("overall_status") != "verified":
            break
    final_integrity["notification_sequence"] = classify_notification_sequence(notification_sequence)
    final_integrity["radio_owned_notification"] = True
    final_integrity["stability_readbacks"] = stability_readbacks
    db.add(ConfigBackup(device_id=device.device_id, path=f"{backup_label}/after-stable-readback.xml", content=final_xml))
    db.commit()
    radio_rows = preset_summaries_from_xml(final_xml)
    if final_integrity["overall_status"] != "verified":
        raise HTTPException(status_code=502, detail={"error": "preset integrity check failed during stability window", "device_id": device.device_id, "expected_slots": expected, "integrity": final_integrity, "radio_presets": final_xml, "local_saved": False})
    for item in slot_results:
        actual = next((row for row in radio_rows if row["button"] == item["button"]), {})
        item.update({"status": "unchanged" if item.get("status") == "unchanged" else "verified", "actual_location": actual.get("location", "")})
    final_integrity["overall_status"] = "verified"
    for mutation in (mutations or {}).values():
        transition_preset_mutation(
            db,
            mutation,
            "VERIFIED",
            after_radio=final_xml,
            diverged=False,
        )
    record_action(
        db,
        job_id=next(
            (row.mutation_id for row in (mutations or {}).values()), ""
        ),
        device_id=device.device_id,
        ip_address=device.ip_address,
        action="preset_sync",
        trigger="stations_presets",
        phase="VERIFIED",
        requested_state={"slots": expected},
        backup_ref=backup_ref,
        before_state={"presets_sha256": sha256(before_xml.encode("utf-8")).hexdigest()},
        result="radio_readback_verified",
        readback={
            "presets_sha256": sha256(final_xml.encode("utf-8")).hexdigest(),
            "slots": radio_rows,
            "integrity": final_integrity,
        },
        rollback_ref=backup_ref,
        verified=True,
    )
    db.commit()
    return PresetSyncResult(radio_rows, final_integrity)


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
    if payload.get("dry_run", False):
        return {"dry_run": True, "device_id": device.device_id, "target": device.ip_address, "endpoint": "/presets"}
    xml = await SoundTouchClient(device.ip_address).get_xml("/presets")
    rows = preset_summaries_from_xml(xml)
    db.add(ConfigBackup(device_id=device.device_id, path="/presets", content=xml))
    reconciled: list[dict] = []
    for item in rows:
        preset = db.query(Preset).filter(Preset.device_id == device.device_id, Preset.button == item["button"]).one_or_none()
        if preset is None:
            preset = Preset(device_id=device.device_id, button=item["button"])
            db.add(preset)
        mutation = db.query(PresetMutation).filter(
            PresetMutation.device_id == device.device_id,
            PresetMutation.button == item["button"],
        ).order_by(PresetMutation.revision.desc()).first()
        can_reconcile = False
        if mutation is not None and mutation.diverged and mutation.state in {"FAILED", "RECONCILE"} and preset.station_id:
            expected_location = _effective_preset_location(
                db,
                device.device_id,
                preset,
                request_host=request.headers.get("host", ""),
            )
            can_reconcile = _locations_match(item.get("location", ""), expected_location)
            if can_reconcile and mutation.state == "FAILED":
                transition_preset_mutation(db, mutation, "RECONCILE")
            if can_reconcile:
                transition_preset_mutation(
                    db,
                    mutation,
                    "VERIFIED",
                    after_radio=xml,
                    diverged=False,
                )
        preset.source = normalize_source_name(item["source"])
        preset.location = item["location"]
        preset.content_item_xml = _preset_content_item_xml(
            item,
            location=item["location"],
            source=preset.source,
            source_account=item.get("source_account", ""),
            name=item.get("item_name", ""),
            art=item.get("container_art", ""),
            include_art=_station_logo_enabled(db, device.device_id),
        )
        preset.updated_at = utc_now()
        if can_reconcile and mutation is not None:
            transition_preset_mutation(db, mutation, "LOCAL_COMMIT", commit=False)
            reconciled.append({"button": item["button"], "mutation_id": mutation.mutation_id, "revision": mutation.revision})
    db.commit()
    return {"dry_run": False, "device_id": device.device_id, "count": len(rows), "presets": rows, "reconciled_mutations": reconciled}


@router.post("/devices/{device_id}/stations/{station_id}/play")
async def play_station_on_device(device_id: str, station_id: int, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    request_context = request if isinstance(request, Request) else None
    if request_context is None:
        db = request
    device = device_or_404(db, device_id)
    station = db.query(Station).filter(Station.id == station_id).one_or_none()
    if station is None:
        raise HTTPException(status_code=404, detail="station not found")
    descriptor = _device_station_descriptor(db, device.device_id, station)
    location = _station_location_or_409(descriptor, db, request_context)
    source = "LOCAL_INTERNET_RADIO"
    xml = _device_content_item_xml(db, device.device_id, station, location, source)
    if payload.get("dry_run", False):
        return {"dry_run": True, "device_id": device.device_id, "target": device.ip_address, "path": "/select", "xml": xml}
    enforce_ip_write_guard(db, device)
    safe_volume = payload.get("safe_volume")
    if safe_volume is not None:
        safe_volume = int(safe_volume)
        if safe_volume < 0 or safe_volume > 5:
            raise HTTPException(status_code=400, detail="live playback safe_volume must be 0..5")
    target_volume = payload.get("target_volume")
    if target_volume is not None:
        target_volume = int(target_volume)
        if target_volume < 0 or target_volume > 100:
            raise HTTPException(status_code=400, detail="target_volume must be 0..100")
    policy = policy_for_device(device, db)
    client = _soundtouch_client_for(device, purpose="play_station", trigger=str(payload.get("trigger") or "api"), policy=policy)
    confirmed_volume = None
    response = ""
    last_invalid = None
    muted_before = False
    safety_gate_armed = False
    try:
        if safe_volume is not None:
            await client.post_xml("/volume", f"<volume>{safe_volume}</volume>")
            before_volume, muted_before = _volume_and_mute(await client.get_xml("/volume"))
            if before_volume != safe_volume:
                raise OSError(f"safe volume preflight failed: requested {safe_volume}, read-back {before_volume}")
            write_masterlog("volume_safety_verified", device_id=device.device_id, radio_ip=device.ip_address, volume=before_volume, stage="before_playback")
            muted_volume, muted_for_select = await _set_mute_readback(client, True)
            if muted_volume != safe_volume or not muted_for_select:
                raise OSError("safe volume and mute were not both confirmed before select")
            arm_playback_safety_gate(
                db,
                device.device_id,
                safe_volume=safe_volume,
                station_id=station.id,
            )
            safety_gate_armed = True
        elif target_volume is not None:
            try:
                standby = 'source="STANDBY"' in await client.get_xml("/now_playing")
            except Exception:
                standby = True
            if standby:
                if not policy.allow_auto_wakeup:
                    write_masterlog(
                        "auto_wakeup_blocked",
                        device_id=device.device_id,
                        radio_ip=device.ip_address,
                        device_class=policy.device_class.value,
                        request_purpose="play_station_target_volume",
                        polling_profile=policy.polling_profile.value,
                        safe_mode_active=policy.safe_mode_active,
                        circuit_breaker_state=policy.circuit_state.value,
                        reason="device policy blocks automatic POWER",
                    )
                    raise HTTPException(status_code=409, detail={"error": "device policy blocks automatic wakeup", "device_id": device.device_id})
                for state in ("press", "release"):
                    await client.post_xml("/key", f'<key state="{state}" sender="Gabbo">POWER</key>')
                    await asyncio.sleep(0.12)
                await asyncio.sleep(0.8)
            await client.post_xml("/volume", f"<volume>{target_volume}</volume>")
            write_masterlog("alarm_timer_volume_set", device_id=device.device_id, radio_ip=device.ip_address, volume=target_volume, stage="before_playback")
        source_attempts = await _live_source_attempt_order(client, db, device.device_id, station)
        if not source_attempts:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "no stream-capable source is READY on the radio",
                    "device_id": device.device_id,
                },
            )
        for index, attempt_source in enumerate(source_attempts):
            source = attempt_source
            xml = _device_content_item_xml(db, device.device_id, station, location, source)
            if index:
                write_masterlog("playback_source_fallback_attempt", device_id=device.device_id, radio_ip=device.ip_address, station_id=station.id, source=source, location=location, endpoint="/select", attempt=index + 1, xml_preview=_xml_preview(xml))
            write_masterlog("playback_select_start", device_id=device.device_id, radio_ip=device.ip_address, station_id=station.id, source=source, location=location, endpoint="/select", location_is_absolute=location.startswith("http://"), stream_format=station.stream_format, stream_mime=station.stream_mime, compatibility_score=station.compatibility_score, xml_preview=_xml_preview(xml))
            write_masterlog("playback_select_xml_preview", device_id=device.device_id, radio_ip=device.ip_address, station_id=station.id, source=source, location=location, endpoint="/select", location_is_absolute=location.startswith("http://"), xml_preview=_xml_preview(xml))
            response = await client.post_xml("/select", xml)
            write_masterlog("playback_select_complete", device_id=device.device_id, radio_ip=device.ip_address, station_id=station.id, source=source, location=location, endpoint="/select", status_code=200, radio_response_preview=_radio_response_preview(response), location_is_absolute=location.startswith("http://"), xml_preview=_xml_preview(xml))
            if safe_volume is not None:
                selected_volume, still_muted = _volume_and_mute(await client.get_xml("/volume"))
                if selected_volume != safe_volume:
                    selected_volume = await _restore_volume_readback(client, safe_volume)
                if not still_muted:
                    selected_volume, still_muted = await _set_mute_readback(client, True)
                if selected_volume != safe_volume or not still_muted:
                    raise OSError("post-select volume 1 and mute could not be restored")
                confirmed_volume = selected_volume
                verify_playback_safety_gate(
                    db,
                    device.device_id,
                    volume=confirmed_volume,
                    muted=still_muted,
                )
            accepted, now_playing = await _wait_for_source_acceptance(
                client,
                device=device,
                station=station,
                source=source,
                location=location,
                xml=xml,
            )
            if not accepted:
                last_invalid = now_playing
                write_masterlog("invalid_source_detected", device_id=device.device_id, radio_ip=device.ip_address, station_id=station.id, source=source, location=location, endpoint="/now_playing", status_code=200, radio_response_preview=_radio_response_preview(now_playing), location_is_absolute=location.startswith("http://"), xml_preview=_xml_preview(xml))
                continue
            if safe_volume is not None:
                if not muted_before:
                    confirmed_volume, still_muted = await _set_mute_readback(client, False)
                    if confirmed_volume != safe_volume or still_muted:
                        raise OSError("safe unmute at volume 1 could not be confirmed")
                write_masterlog("volume_safety_verified", device_id=device.device_id, radio_ip=device.ip_address, volume=confirmed_volume, stage="after_playback_select")
                clear_playback_safety_gate(db, device.device_id)
                safety_gate_armed = False
            elif target_volume is not None:
                await asyncio.sleep(0.35)
                await client.post_xml("/volume", f"<volume>{target_volume}</volume>")
                volume_root = ET.fromstring(await client.get_xml("/volume"))
                confirmed_volume = int(float(volume_root.findtext("actualvolume") or "-1"))
                if confirmed_volume != target_volume:
                    await asyncio.sleep(0.35)
                    await client.post_xml("/volume", f"<volume>{target_volume}</volume>")
                    volume_root = ET.fromstring(await client.get_xml("/volume"))
                    confirmed_volume = int(float(volume_root.findtext("actualvolume") or "-1"))
                write_masterlog("alarm_timer_volume_verified", device_id=device.device_id, radio_ip=device.ip_address, volume=confirmed_volume, requested=target_volume, stage="after_playback_select")
            if source != "LOCAL_INTERNET_RADIO":
                _learn_playback_source(db, device.device_id, source)
                write_masterlog("preset_source_learned", device_id=device.device_id, radio_ip=device.ip_address, station_id=station.id, source=source, location=location, endpoint="/select")
            break
        else:
            raise HTTPException(status_code=502, detail={"error": "Radio hat Preset angenommen, aber Wiedergabe abgelehnt. Streamformat oder Source nicht kompatibel.", "device_id": device.device_id, "station_id": station.id, "location": location, "last_now_playing": _radio_response_preview(last_invalid or "")})
    except httpx.HTTPStatusError as exc:
        if safety_gate_armed:
            fail_playback_safety_gate(db, device.device_id, str(exc))
        cleanup = await _safe_playback_failure_cleanup(client, db, device, safe_volume)
        write_masterlog("playback_select_failed", device_id=device.device_id, radio_ip=device.ip_address, station_id=station.id, source=source, location=location, endpoint="/select", status_code=exc.response.status_code, radio_response_preview=_radio_response_preview(exc.response.text), location_is_absolute=location.startswith("http://"), xml_preview=_xml_preview(xml))
        raise HTTPException(
            status_code=502,
            detail={
                "error": "radio rejected /select",
                "status_code": exc.response.status_code,
                "radio_response": exc.response.text,
                "xml": xml,
                "audio_safety": cleanup,
            },
        ) from exc
    except Exception as exc:
        if safety_gate_armed:
            fail_playback_safety_gate(db, device.device_id, str(exc))
        cleanup = await _safe_playback_failure_cleanup(client, db, device, safe_volume)
        if isinstance(exc, HTTPException):
            detail = exc.detail
            if isinstance(detail, dict):
                detail = {**detail, "audio_safety": cleanup}
            else:
                detail = {"error": str(detail), "audio_safety": cleanup}
            raise HTTPException(status_code=exc.status_code, detail=detail) from exc
        if safe_volume is not None:
            write_masterlog("volume_safety_failed", device_id=device.device_id, radio_ip=device.ip_address, error=str(exc), stage="playback")
        write_masterlog("playback_select_failed", device_id=device.device_id, radio_ip=device.ip_address, station_id=station.id, source=source, location=location, endpoint="/select", status_code=None, radio_response_preview=str(exc) or exc.__class__.__name__, location_is_absolute=location.startswith("http://"), xml_preview=_xml_preview(xml))
        raise HTTPException(status_code=502, detail={"error": "safe playback failed", "message": str(exc), "audio_safety": cleanup}) from exc
    update_runtime_state(db, device.device_id, current_source=source, selected_content_item={"source": source, "location": location}, playback_state="selected", current_preset=None)
    from basswiesn.app.services.device_state import load_runtime_state, save_runtime_state
    from basswiesn.app.services.playback_state import close_open_sessions
    _state_row, runtime_state = load_runtime_state(db, device.device_id)
    close_open_sessions(db, device.device_id, reason="source_command_pending", device_last_seen=device.last_seen)
    trigger = str(payload.get("trigger") or "stream")[:64]
    trigger_type = str(payload.get("trigger_type") or "station")[:64]
    runtime_state["playback_pending"] = {"station_id": station.id, "station_name": station.name, "stream_url": station.stream_url, "source": source, "trigger": trigger, "trigger_type": trigger_type, "internal_event": bool(payload.get("internal_event", False) or getattr(station, "internal", False)), "created_at": utc_now().isoformat()}
    save_runtime_state(db, device.device_id, runtime_state, commit=False)
    db.commit()
    write_masterlog("playback_command_pending", device_id=device.device_id, radio_ip=device.ip_address, station_id=station.id, station_name=station.name, trigger_type=trigger_type, source_type=source)
    return {"dry_run": False, "device_id": device.device_id, "station_id": station.id, "path": "/select", "response": response, "confirmed_volume": confirmed_volume, "playback_history": "pending_live_confirmation"}


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
            radio_error = str(exc)
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
    providers = live_providers or runtime.get("providers") or {}
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
        "radio_snapshot_source": snapshot.path if snapshot is not None else "",
        "radio_observed_at": snapshot.created_at.isoformat() if snapshot is not None else None,
    }


@router.post("/presets/{device_id}/sync")
async def sync_local_presets(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
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
    readback_requested = bool(payload.get("probe", False)) or not payload.get("dry_run", True)
    if readback_requested:
        try:
            current_xml = await _soundtouch_client_for(
                device,
                purpose="preset_sync_preview",
                trigger="device_settings_logo_preview",
            ).get_xml("/presets")
            current_rows = preset_summaries_from_xml(current_xml)
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
        except Exception as exc:
            preview_readback_error = str(exc)[:500]
    elif payload.get("dry_run", True):
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
    if payload.get("dry_run", True):
        return preview
    require_memory_checked(device, payload)
    if preview_readback_error:
        raise HTTPException(status_code=502, detail={"error": "radio preset readback failed before artwork sync", "message": preview_readback_error})
    if not prepared:
        _commit_radio_preset_projection(db, device_id, current_rows)
        db.commit()
        integrity = {
            "overall_status": "verified",
            "target_verified": True,
            "untouched_slots_verified": True,
            "unexpected_changes": [],
            "slot_results": [],
            "radio_write_count": 0,
        }
        _record_preset_sync_state(db, device_id, {"status": "verified", "verified": True, "last_error": "", "successful_slots": len(already_current_slots), "different_slots": 0, "radio_slots": current_rows, "integrity": integrity})
        return {"dry_run": False, "device_id": device_id, "target": preview["target"], "verified": True, "radio_slots": current_rows, "expected_slots": {}, "integrity": integrity, "logo_status": logo_status, "protection": preview["protection"], "skipped_slots": skipped_slots}
    try:
        radio_rows = await sync_presets_to_radio(
            device,
            expected,
            db,
            "preset-artwork-sync",
            prepared_presets=prepared,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        integrity = detail.get("integrity", {})
        slot_results = integrity.get("slot_results", detail.get("slot_results", []))
        failed_slots = sum(1 for item in slot_results if item.get("status") in {"failed", "mismatch", "missing"})
        _record_preset_sync_state(db, device_id, {"status": integrity.get("overall_status", "failed"), "verified": False, "last_error": str(exc.detail)[:500], "successful_slots": sum(1 for item in slot_results if item.get("status") in {"verified", "unchanged"}), "different_slots": failed_slots or len(detail.get("unexpected_changes", [])), "slot_results": slot_results, "integrity": integrity})
        raise
    integrity = _integrity_for_sync_result(radio_rows, expected)
    _commit_radio_preset_projection(db, device_id, radio_rows)
    db.commit()
    _record_preset_sync_state(db, device_id, {"status": integrity["overall_status"], "verified": integrity["overall_status"] == "verified", "last_error": "", "successful_slots": len(radio_rows), "different_slots": len(integrity.get("unexpected_changes", [])), "radio_slots": radio_rows, "integrity": integrity})
    return {"dry_run": False, "device_id": device_id, "target": preview["target"], "verified": integrity["overall_status"] == "verified", "radio_slots": radio_rows, "expected_slots": expected, "integrity": integrity, "logo_status": logo_status, "protection": preview["protection"], "skipped_slots": skipped_slots}


@router.post("/presets/{device_id}/{button}")
async def set_preset(device_id: str, button: int, payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
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
    if payload.get("dry_run", True):
        return {"dry_run": True, "device_id": device_id, "button": button, "location": location, "xml": xml, "media": media, "memory_check": memory_check_plan(device)}
    enforce_ip_write_guard(db, device)
    write_masterlog("preset_apply_start", device_id=device_id, radio_ip=device.ip_address, button=button, station_id=station.id, source=source, location=location, endpoint="/storePreset", location_is_absolute=location.startswith("http://"), stream_format=station.stream_format, stream_mime=station.stream_mime, compatibility_score=station.compatibility_score, xml_preview=_xml_preview(xml))
    write_masterlog("preset_apply", device_id=device_id, radio_ip=device.ip_address, button=button, station_id=station.id, source=source, location=location, endpoint="/storePreset", location_is_absolute=location.startswith("http://"), stream_format=station.stream_format, stream_mime=station.stream_mime, compatibility_score=station.compatibility_score, xml_preview=_xml_preview(xml))
    write_masterlog("content_item_source", device_id=device_id, radio_ip=device.ip_address, station_id=station.id, source=source, location=location, endpoint="/storePreset", location_is_absolute=location.startswith("http://"), stream_format=station.stream_format, stream_mime=station.stream_mime, compatibility_score=station.compatibility_score, xml_preview=_xml_preview(xml))
    write_masterlog("content_item_location", device_id=device_id, radio_ip=device.ip_address, station_id=station.id, source=source, location=location, endpoint="/storePreset", location_is_absolute=location.startswith("http://"), stream_format=station.stream_format, stream_mime=station.stream_mime, compatibility_score=station.compatibility_score, xml_preview=_xml_preview(xml))
    prepared = {
        button: {
            "station_id": station.id,
            "source": source,
            "source_account": "",
            "location": location,
            "content_item_xml": xml,
        }
    }
    mutation = prepare_preset_mutation(
        db,
        device_id=device_id,
        button=button,
        operation="WRITE",
        requested_state=prepared[button],
    )
    try:
        radio_rows = await sync_presets_to_radio(
            device,
            {button: location},
            db,
            f"preset-write/{button}",
            prepared_presets=prepared,
            mutations={button: mutation},
        )
        integrity = _integrity_for_sync_result(radio_rows, {button: location})
        verified_slot = next((item for item in radio_rows if item["button"] == button and _locations_match(item.get("location", ""), location)), None)
        if verified_slot is None:
            raise ValueError("radio preset slot mismatch")
    except (HTTPException, ValueError) as exc:
        db.rollback()
        mutation = db.query(PresetMutation).filter(
            PresetMutation.mutation_id == mutation.mutation_id
        ).one()
        if mutation.state not in {"LOCAL_COMMIT", "ROLLBACK"}:
            failure_state = "FAILED" if mutation.state == "PREPARED" else "RECONCILE"
            transition_preset_mutation(
                db,
                mutation,
                failure_state,
                error=str(getattr(exc, "detail", exc)),
                diverged=mutation.state in {"RADIO_WRITE", "RADIO_READBACK", "VERIFIED"},
            )
        write_masterlog("preset_apply_failed", device_id=device_id, radio_ip=device.ip_address, button=button, station_id=station.id, source=source, location=location, endpoint="/storePreset", status_code=getattr(exc, "status_code", None), radio_response_preview=_radio_response_preview(str(getattr(exc, "detail", exc))), xml_preview=_xml_preview(xml), error=str(exc))
        detail = exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) else {}
        raise HTTPException(status_code=502, detail={
            "error": "Das Radio hat das Preset nicht bestätigt; der lokale Zustand blieb unverändert.",
            "local_saved": False,
            "integrity": detail.get("integrity", {}),
            "overall_status": detail.get("integrity", {}).get("overall_status", "manual_review_required"),
        }) from exc
    preset = db.query(Preset).filter(Preset.device_id == device_id, Preset.button == button).one_or_none()
    if preset is None:
        preset = Preset(device_id=device_id, button=button)
        db.add(preset)
    preset.station_id = station.id
    # The stable radio readback is authoritative.  In particular, never keep
    # a sourceAccount from the previous slot contents when a local-internet
    # preset has just been verified with an empty sourceAccount.
    preset.source = normalize_source_name(verified_slot.get("source") or source)
    preset.source_account = str(verified_slot.get("source_account") or "")
    preset.location = str(verified_slot.get("location") or location)
    preset.content_item_xml = str(verified_slot.get("content_item_xml") or xml)
    preset.updated_at = utc_now()
    transition_preset_mutation(db, mutation, "LOCAL_COMMIT", commit=False)
    db.commit()
    write_masterlog("preset_apply_complete", device_id=device_id, radio_ip=device.ip_address, button=button, station_id=station.id, source=source, location=location, endpoint="/storePreset", status_code=200, radio_response_preview=_radio_response_preview(str(verified_slot)), xml_preview=_xml_preview(xml), verified=integrity["overall_status"] == "verified")
    return {"dry_run": False, "verified": integrity["overall_status"] == "verified", "device_id": device_id, "button": button, "location": location, "media": media, "radio_slot": verified_slot, "integrity": integrity}


@router.delete("/presets/{device_id}/{button}")
async def delete_preset(device_id: str, button: int, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    enforce_ip_write_guard(db, device)
    preset = db.query(Preset).filter(Preset.device_id == device_id, Preset.button == button).one_or_none()
    if preset is None:
        raise HTTPException(status_code=404, detail="preset not found")
    mutation = prepare_preset_mutation(
        db,
        device_id=device_id,
        button=button,
        operation="DELETE",
        requested_state={"button": button, "deleted": True},
    )
    client = SoundTouchClient(device.ip_address)
    try:
        before_xml = await client.get_xml("/presets")
        backup_ref = f"preset-delete/{button}/before.xml"
        db.add(ConfigBackup(device_id=device_id, path=backup_ref, content=before_xml))
        db.commit()
        transition_preset_mutation(
            db,
            mutation,
            "RADIO_WRITE",
            before_radio=before_xml,
            backup_ref=backup_ref,
        )
        # /removePreset is the radio's confirmed public write operation.  The
        # Marge callback may arrive while this mutation is RADIO_WRITE; the
        # cloud handler stages that callback and exposes a tombstoned view,
        # but this local row is not deleted until /presets proves the result.
        remove_body = f'<preset id="{button}"></preset>'
        await client.post_xml("/removePreset", remove_body)
        transition_preset_mutation(db, mutation, "RADIO_READBACK")
        final_xml = before_xml
        for _ in range(12):
            await asyncio.sleep(0.5)
            final_xml = await client.get_xml("/presets")
            rows = preset_summaries_from_xml(final_xml)
            if not any(item["button"] == button for item in rows):
                transition_preset_mutation(
                    db,
                    mutation,
                    "VERIFIED",
                    after_radio=final_xml,
                    diverged=False,
                )
                preset = db.query(Preset).filter(
                    Preset.device_id == device_id, Preset.button == button
                ).one()
                db.delete(preset)
                transition_preset_mutation(
                    db, mutation, "LOCAL_COMMIT", commit=False
                )
                record_action(
                    db,
                    job_id=mutation.mutation_id,
                    device_id=device_id,
                    ip_address=device.ip_address,
                    action="preset_delete",
                    trigger="stations_presets",
                    phase="VERIFIED",
                    requested_state={"button": button, "deleted": True},
                    backup_ref=backup_ref,
                    before_state={
                        "presets_sha256": sha256(before_xml.encode("utf-8")).hexdigest()
                    },
                    result="radio_readback_verified",
                    readback={
                        "presets_sha256": sha256(final_xml.encode("utf-8")).hexdigest(),
                        "slot_absent": True,
                    },
                    rollback_ref=backup_ref,
                    verified=True,
                )
                db.commit()
                return {
                    "device_id": device_id,
                    "button": button,
                    "deleted": True,
                    "verified": True,
                    "mutation_id": mutation.mutation_id,
                    "revision": mutation.revision,
                }
        raise RuntimeError("radio did not confirm preset deletion")
    except Exception as exc:
        db.rollback()
        mutation = db.query(PresetMutation).filter(
            PresetMutation.mutation_id == mutation.mutation_id
        ).one()
        failure_state = (
            "FAILED" if mutation.state == "PREPARED" else "RECONCILE"
        )
        if mutation.state not in {"LOCAL_COMMIT", "ROLLBACK"}:
            transition_preset_mutation(
                db,
                mutation,
                failure_state,
                error=str(exc),
                diverged=mutation.state in {"RADIO_WRITE", "RADIO_READBACK", "VERIFIED"},
            )
        record_action(
            db,
            job_id=mutation.mutation_id,
            device_id=device_id,
            ip_address=device.ip_address,
            action="preset_delete",
            trigger="stations_presets",
            phase=mutation.state,
            requested_state={"button": button, "deleted": True},
            backup_ref=mutation.backup_ref,
            result="radio_readback_not_verified",
            rollback_ref=mutation.backup_ref,
            error_category=exc.__class__.__name__,
            verified=False,
        )
        db.commit()
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Das Radio hat die Preset-Löschung nicht bestätigt; der lokale Slot blieb erhalten.",
                "local_saved": True,
                "local_deleted": False,
                "mutation_id": mutation.mutation_id,
                "state": mutation.state,
            },
        ) from exc


@router.post("/presets/clone")
async def clone_presets(payload: dict, db: Session = Depends(get_db)) -> dict:
    source_device_id = payload.get("source_device_id")
    target_device_id = payload.get("target_device_id")
    if not source_device_id or not target_device_id:
        raise HTTPException(status_code=400, detail="source_device_id and target_device_id are required")
    target_device = device_or_404(db, target_device_id)
    enforce_ip_write_guard(db, target_device)
    source_rows = db.query(Preset).filter(Preset.device_id == source_device_id).order_by(Preset.button).all()
    if not source_rows:
        raise HTTPException(status_code=404, detail="source device has no presets")
    cloned = []
    prepared: dict[int, dict] = {}
    mutations: dict[int, PresetMutation] = {}
    for source in source_rows:
        prepared[source.button] = {
            "station_id": source.station_id,
            "source": source.source,
            "source_account": source.source_account,
            "location": source.location,
            "content_item_xml": source.content_item_xml,
        }
        mutations[source.button] = prepare_preset_mutation(
            db,
            device_id=target_device_id,
            button=source.button,
            operation="CLONE",
            requested_state=prepared[source.button],
        )
        cloned.append(source.button)
    expected = {row.button: row.location for row in source_rows if row.location}
    try:
        radio_rows = await sync_presets_to_radio(
            target_device,
            expected,
            db,
            "preset-clone",
            prepared_presets=prepared,
            mutations=mutations,
        )
    except Exception as exc:
        db.rollback()
        for mutation_id in [row.mutation_id for row in mutations.values()]:
            mutation = db.query(PresetMutation).filter(
                PresetMutation.mutation_id == mutation_id
            ).one()
            if mutation.state not in {"LOCAL_COMMIT", "ROLLBACK"}:
                failure_state = "FAILED" if mutation.state == "PREPARED" else "RECONCILE"
                transition_preset_mutation(
                    db,
                    mutation,
                    failure_state,
                    error=str(exc),
                    diverged=mutation.state in {"RADIO_WRITE", "RADIO_READBACK", "VERIFIED"},
                )
        raise
    for source in source_rows:
        target = db.query(Preset).filter(Preset.device_id == target_device_id, Preset.button == source.button).one_or_none()
        if target is None:
            target = Preset(device_id=target_device_id, button=source.button)
            db.add(target)
        target.station_id = source.station_id
        target.source = source.source
        target.source_account = source.source_account
        target.location = source.location
        target.content_item_xml = source.content_item_xml
        target.updated_at = utc_now()
        transition_preset_mutation(
            db, mutations[source.button], "LOCAL_COMMIT", commit=False
        )
    db.commit()
    return {"source_device_id": source_device_id, "target_device_id": target_device_id, "buttons": cloned, "verified": True, "radio_slots": radio_rows}
