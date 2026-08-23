"""Explicit split between persistent radio settings and volatile runtime state."""

from dataclasses import asdict, dataclass, field
import json
import xml.etree.ElementTree as ET

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.models import Device, RuntimeState, utc_now
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.services.provider_registry import SERVICE_MANIFEST

RUNTIME_FIELDS = {"current_source", "now_playing", "playback_state", "selected_content_item", "provider_initialized", "provider_state", "service_availability", "providers", "current_preset", "errors"}


def runtime_state_key(device_id: str) -> str:
    return f"device:{device_id}:runtime_state"


def load_runtime_state(db: Session, device_id: str) -> tuple[RuntimeState | None, dict]:
    row = db.query(RuntimeState).filter(RuntimeState.key == runtime_state_key(device_id)).one_or_none()
    if row is None:
        return None, {}
    try:
        return row, json.loads(row.value or "{}")
    except (TypeError, ValueError):
        return row, {}


def save_runtime_state(db: Session, device_id: str, payload: dict, *, commit: bool = True) -> RuntimeState:
    key = runtime_state_key(device_id)
    row = db.query(RuntimeState).filter(RuntimeState.key == key).one_or_none()
    if row is None:
        row = RuntimeState(key=key)
        db.add(row)
    row.value = json.dumps(payload, ensure_ascii=False)
    row.updated_at = utc_now()
    if commit:
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.query(RuntimeState).filter(RuntimeState.key == key).one()
            row.value = json.dumps(payload, ensure_ascii=False)
            row.updated_at = utc_now()
            db.commit()
    return row


def update_runtime_state(db: Session, device_id: str, **changes) -> dict:
    _row, current = load_runtime_state(db, device_id)
    current.update({key: value for key, value in changes.items() if key in RUNTIME_FIELDS})
    save_runtime_state(db, device_id, current)
    return current


def runtime_from_now_playing(xml: str) -> dict:
    root = _safe_root(xml)
    if root is None:
        return {}
    content = root.find(".//ContentItem")
    metadata_tags = {
        "track",
        "title",
        "artist",
        "album",
        "imageurl",
        "arturl",
        "art",
    }
    now_playing: dict[str, str | None] = {}
    for child in root:
        normalized_tag = child.tag.rsplit("}", 1)[-1].casefold()
        if child.text is not None:
            now_playing[child.tag] = child.text.strip()
        elif normalized_tag in metadata_tags:
            # Presence is meaningful for live metadata: an explicit empty
            # optional field clears the previous value for this selection.
            now_playing[child.tag] = None
    return {
        "current_source": root.attrib.get("source", "") or (content.attrib.get("source", "") if content is not None else ""),
        "playback_state": root.findtext("playStatus", root.attrib.get("playStatus", "")),
        "now_playing": now_playing,
        "selected_content_item": dict(content.attrib) if content is not None else {},
    }


@dataclass
class SettingsState:
    bass: object = None
    language: object = None
    clockDisplay: object = None
    systemtimeout: object = None
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class RuntimeStateSnapshot:
    current_source: str = ""
    now_playing: dict = field(default_factory=dict)
    playback_state: str = ""
    selected_content_item: dict = field(default_factory=dict)
    provider_initialized: bool = False
    provider_state: list[dict] = field(default_factory=list)
    service_availability: list[dict] = field(default_factory=list)
    providers: dict = field(default_factory=dict)
    current_preset: int | None = None
    errors: dict[str, str] = field(default_factory=dict)


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def _safe_root(xml: str) -> ET.Element | None:
    try:
        return ET.fromstring(xml or "")
    except ET.ParseError:
        return None


PROVIDER_ALIASES = {
    "LOCAL INTERNET RADIO": "LOCAL_INTERNET_RADIO",
    "INTERNET RADIO": "INTERNET_RADIO",
    "RADIO BROWSER": "RADIO_BROWSER",
    "SPOTIFY CONNECT": "SPOTIFY",
    "STORED MUSIC": "STORED_MUSIC",
    "LOCAL MUSIC": "LOCAL_MUSIC",
}


def _provider_name(value: str) -> str:
    name = (value or "").strip().upper().replace("-", "_").replace(" ", "_")
    return PROVIDER_ALIASES.get(name.replace("_", " "), PROVIDER_ALIASES.get(name, name or "unknown_provider"))


def _status_ready(value: str) -> bool:
    status = (value or "").strip().upper()
    return status in {"", "READY", "AVAILABLE", "OK", "TRUE", "1"}


def _provider_entry(name: str, last_seen: str) -> dict:
    config = SERVICE_MANIFEST.get(name, {})
    return {
        "available": False,
        "ready": False,
        "visible_in_sources": False,
        "source_observed": False,
        "source_available": None,
        "service_observed": False,
        "service_available": None,
        "can_add": bool(config.get("can_add", False)),
        "can_remove": bool(config.get("can_remove", False)),
        "provider_id": config.get("provider_id"),
        "source_status": "",
        "last_seen": last_seen,
    }


def parse_sources_xml(xml: str, last_seen: str = "") -> tuple[list[dict], dict]:
    root = _safe_root(xml)
    if root is None:
        return [], {}
    rows: list[dict] = []
    providers: dict[str, dict] = {}
    for source in root.findall(".//source"):
        raw_name = source.attrib.get("source") or source.attrib.get("type") or source.findtext("name", "")
        name = _provider_name(raw_name)
        status = source.attrib.get("status") or source.findtext("status", "") or "READY"
        item = {
            "source": name,
            "status": status,
            "source_account": source.attrib.get("sourceAccount", "") or source.findtext("sourceAccount", ""),
            "credential_present": bool(source.attrib.get("secret") or source.findtext("credential", "")),
        }
        rows.append(item)
        entry = providers.setdefault(name, _provider_entry(name, last_seen))
        entry["visible_in_sources"] = True
        entry["source_observed"] = True
        entry["source_available"] = bool(entry["source_available"]) or _status_ready(status)
        entry["available"] = bool(entry["source_available"])
        entry["ready"] = bool(entry["source_available"])
        entry["source_status"] = status
        entry["last_seen"] = last_seen
    return rows, providers


def parse_service_availability_xml(xml: str, last_seen: str = "") -> tuple[list[dict], dict]:
    root = _safe_root(xml)
    if root is None:
        return [], {}
    rows: list[dict] = []
    providers: dict[str, dict] = {}
    for node in root.iter():
        if node is root:
            continue
        raw_name = node.attrib.get("service") or node.attrib.get("name") or node.attrib.get("source") or node.findtext("service", "") or node.tag
        name = _provider_name(raw_name)
        status = node.attrib.get("status") or node.findtext("status", "") or (node.text or "").strip()
        item = {"service": name, "status": status, "raw_tag": node.tag}
        rows.append(item)
        entry = providers.setdefault(name, _provider_entry(name, last_seen))
        entry["service_observed"] = True
        entry["service_available"] = bool(entry["service_available"]) or _status_ready(status)
        entry["available"] = bool(entry["service_available"])
        entry["ready"] = bool(entry["service_available"])
        entry["source_status"] = status or entry["source_status"]
        entry["last_seen"] = last_seen
    return rows, providers


def merge_provider_maps(*maps: dict[str, dict]) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for provider_map in maps:
        for name, item in provider_map.items():
            target = merged.setdefault(name, _provider_entry(name, item.get("last_seen", "")))
            target.update({key: value for key, value in item.items() if value not in ("", None, False)})
            target["visible_in_sources"] = target["visible_in_sources"] or item.get("visible_in_sources", False)
            if item.get("source_observed"):
                target["source_observed"] = True
                target["source_available"] = bool(item.get("source_available"))
            if item.get("service_observed"):
                target["service_observed"] = True
                target["service_available"] = bool(item.get("service_available"))

    for target in merged.values():
        source_ok = target.get("source_available")
        service_ok = target.get("service_available")
        if target.get("service_observed"):
            target["available"] = bool(service_ok)
        elif target.get("source_observed"):
            target["available"] = bool(source_ok)
        else:
            target["available"] = False
        target["ready"] = bool(target["available"]) and not (
            target.get("source_observed") and source_ok is False
        )
    return merged


def _persist_provider_health(
    db: Session,
    device_id: str,
    providers: dict[str, dict],
    *,
    observed_at,
) -> None:
    """Project explicit radio provider evidence into the 1.6 health model."""

    from basswiesn.app.repositories.research_state_repository import (
        ResearchStateRepository,
    )
    from basswiesn.app.services.health_models import (
        ProviderSignals,
        reduce_provider_health,
    )

    repository = ResearchStateRepository(db)
    for provider_id, item in providers.items():
        source_observed = bool(item.get("source_observed"))
        service_observed = bool(item.get("service_observed"))
        source_visible = bool(item.get("visible_in_sources")) if source_observed else None
        service_available = (
            bool(item.get("service_available")) if service_observed else None
        )
        auth_model = str(SERVICE_MANIFEST.get(provider_id, {}).get("auth_model") or "")
        anonymous_contract = auth_model in {"anonymous", "local"}
        account_available = (
            True if anonymous_contract and source_visible is True else None
        )
        auth_valid = True if anonymous_contract and source_visible is True else None
        assessment = reduce_provider_health(
            ProviderSignals(
                source_visible=source_visible,
                service_available=service_available,
                account_available=account_available,
                auth_valid=auth_valid,
                last_success=(
                    observed_at
                    if source_visible is True and service_available is not False
                    else None
                ),
                evidence=[
                    {
                        "type": "AUTHORITATIVE_RADIO_PROVIDER_READBACK",
                        "source_observed": source_observed,
                        "source_visible": source_visible,
                        "source_status": item.get("source_status") or None,
                        "service_observed": service_observed,
                        "service_available": service_available,
                        "auth_model": auth_model or None,
                    }
                ],
            ),
            since=observed_at,
        )
        repository.upsert_provider_health(
            device_id,
            provider_id,
            assessment,
            source=provider_id,
            availability=(
                "AVAILABLE"
                if service_available is True
                else "UNAVAILABLE"
                if service_available is False
                else "UNKNOWN"
            ),
            association=(
                "AVAILABLE"
                if account_available is True
                else "UNAVAILABLE"
                if account_available is False
                else "UNKNOWN"
            ),
        )


def _setting_value(name: str, xml: str):
    root = _root(xml)
    if name == "bass":
        return int(root.findtext("targetbass", root.findtext("actualbass", "0")))
    if name == "language":
        return (root.text or "").strip()
    if name == "clockDisplay":
        config = root.find("clockConfig")
        return dict(config.attrib) if config is not None else {}
    return (root.text or ET.tostring(root, encoding="unicode")).strip()


async def read_device_state(device: Device, db: Session) -> dict:
    from basswiesn.app.services.device_policy import policy_for_device

    policy = policy_for_device(device, db)
    try:
        client = SoundTouchClient(
            device.ip_address,
            device_id=device.device_id,
            request_purpose="manual_diagnostics",
            trigger="api",
            policy_context=policy.to_dict(),
        )
    except TypeError:
        client = SoundTouchClient(device.ip_address)
    settings = SettingsState()
    runtime = RuntimeStateSnapshot()
    raw: dict[str, str] = {}

    for name, endpoint in (("bass", "/bass"), ("language", "/language"), ("clockDisplay", "/clockDisplay"), ("systemtimeout", "/systemtimeout")):
        try:
            raw[endpoint] = await client.get_xml(endpoint)
            setattr(settings, name, _setting_value(name, raw[endpoint]))
        except Exception as exc:
            settings.errors[name] = str(exc)

    write_masterlog("runtime_state_parse_start", device_id=device.device_id)
    for endpoint in ("/sources", "/now_playing", "/serviceAvailability", "/presets"):
        try:
            raw[endpoint] = await client.get_xml(endpoint)
        except Exception as exc:
            runtime.errors[endpoint] = str(exc)

    source_providers: dict = {}
    availability_providers: dict = {}
    last_seen = utc_now().isoformat()
    if raw.get("/sources"):
        runtime.provider_state, source_providers = parse_sources_xml(raw["/sources"], last_seen)
        runtime.provider_initialized = len(runtime.provider_state) > 1
    if raw.get("/serviceAvailability"):
        runtime.service_availability, availability_providers = parse_service_availability_xml(raw["/serviceAvailability"], last_seen)
    runtime.providers = merge_provider_maps(source_providers, availability_providers)
    current_location = ""
    if raw.get("/now_playing"):
        root = _safe_root(raw["/now_playing"])
        if root is None:
            runtime.errors["/now_playing"] = "malformed XML"
        else:
            content = root.find(".//ContentItem")
            runtime.current_source = root.attrib.get("source", "") or (content.attrib.get("source", "") if content is not None else "")
            runtime.playback_state = root.findtext("playStatus", root.attrib.get("playStatus", ""))
            runtime.now_playing = {child.tag: (child.text or "").strip() for child in root if child.text}
            runtime.selected_content_item = dict(content.attrib) if content is not None else {}
            current_location = runtime.selected_content_item.get("location", "")
    if raw.get("/presets") and current_location:
        root = _safe_root(raw["/presets"])
        for preset in root.findall(".//preset") if root is not None else []:
            content = preset.find("ContentItem")
            if content is not None and content.attrib.get("location") == current_location:
                runtime.current_preset = int(preset.attrib.get("id", preset.attrib.get("buttonNumber", "0")) or 0) or None
                break

    payload = {"device_id": device.device_id, "settings_state": asdict(settings), "runtime_state": asdict(runtime)}
    _persist_provider_health(
        db,
        device.device_id,
        runtime.providers,
        observed_at=utc_now(),
    )
    save_runtime_state(db, device.device_id, payload["runtime_state"])
    write_masterlog("runtime_state_parse_complete", device_id=device.device_id, providers=list(runtime.providers))
    return payload
