import time
import hashlib
import ipaddress
import json
from datetime import UTC, datetime, timedelta
import xml.etree.ElementTree as ET
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from basswiesn.app.db import get_db
from basswiesn.app.models import Device, MetadataState, Preset, Setting, Station
from basswiesn.app.services.preset_transactions import active_delete_buttons
from basswiesn.app.repositories.research_state_repository import (
    LOCAL_PROVIDER_ID,
    ResearchStateRepository,
    redact_url,
    resolve_request_device,
)
from basswiesn.app.services.diagnostics import log_request, redact_support_text
from basswiesn.app.services.health_models import ProviderSignals, reduce_provider_health
from basswiesn.app.services.clock_metadata import clock_metadata_lab_enabled, load_clock_metadata_preference
from basswiesn.app.services.metadata_engine import (
    MetadataProvenance,
    MetadataSnapshot,
    clock_display_projection,
)
from basswiesn.app.services.restrictions import parse_restrictions
from basswiesn.app.services.telemetry_analysis import redact_mapping
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.config import get_settings
from basswiesn.app.services.orion import OrionLocationError, StationDescriptor, decode_orion_data, playback_response, station_contract_key, station_location
from basswiesn.app.services.stream_compat import analyze_stream_url
from basswiesn.app.services.xml import sources_xml
from basswiesn.app.services.provider_registry import SERVICE_MANIFEST, provider, provider_rows, normalize_source_name
from basswiesn.app.services.playback_safety_gate import (
    PlaybackSafetyGateError,
    wait_for_provider_release,
)

router = APIRouter(tags=["cloud"])

STREAMING_MEDIA_TYPE = "application/vnd.bose.streaming-v1.2+xml"
GROUPS: dict[str, dict] = {}


def _json_or_xml_credentials(raw: str, source: str = "") -> dict:
    try:
        root = ET.fromstring(raw or "<empty/>")
    except ET.ParseError:
        root = None
    if root is None:
        return {"source": source, "raw": bool(raw)}
    data = {child.tag: (child.text or "").strip() for child in root}
    data.update(root.attrib)
    if source and not data.get("source"):
        data["source"] = source
    return data


def _auth_model_for_source(source: str) -> str:
    name = (source or "").strip().upper()
    service = provider(name)
    return str(service.get("auth_model") or "anonymous")


def _credential_response(source: str, credential_type: str, payload: dict) -> dict:
    return {
        "status": "ok",
        "source": source,
        "credentialType": credential_type,
        "authModel": _auth_model_for_source(source),
        "loggedInState": True,
        "displayName": payload.get("displayName") or payload.get("username") or payload.get("user") or "BASSWIESN",
    }

def _station_logo_enabled(db: Session, device_id: str) -> bool:
    row = db.query(Setting).filter(Setting.key == f"station_art_mode:{device_id}").one_or_none()
    return bool(row and row.value == "station_logo")


def _preset_values(preset: Preset, station: Station | None, *, include_art: bool = True) -> dict[str, str]:
    name = station.name if station else ""
    art = station.image_url if station and include_art else ""
    item_type = "stationurl"
    if preset.content_item_xml:
        try:
            item = ET.fromstring(preset.content_item_xml)
            name = name or item.findtext("itemName", "")
            if include_art:
                art = art or item.findtext("containerArt", "")
            item_type = item.attrib.get("type", "") or item_type
        except ET.ParseError:
            pass
    return {"name": name, "art": art, "type": item_type, "location": preset.location}


def _effective_station_location(db: Session, station: Station | None, fallback: str, *, include_art: bool) -> str:
    if station is None:
        return fallback
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
        return station_location(descriptor, db=db)
    except OrionLocationError:
        return fallback


def _preset_source_xml(source: str) -> str:
    source_name = normalize_source_name(source)
    service = provider(source_name)
    provider_id = service["provider_id"]
    source_id = service.get("source_id", "10003")
    label = source_name.replace("_", " ").title()
    secret = service.get("credential", "")
    timestamp = "2026-06-20T00:00:00.000+00:00"
    return (
        f'<source id="{source_id}" type="Audio" displayName="">'
        f"<createdOn>{timestamp}</createdOn><credential type=\"token\">{secret}</credential>"
            f"<name>{escape(source_name)}</name><sourceproviderid>{provider_id}</sourceproviderid>"
        f"<sourcename>{escape(label)}</sourcename><sourceSettings></sourceSettings>"
        f"<updatedOn>{timestamp}</updatedOn><username></username></source>"
    )


def marge_presets_xml(db: Session, device_id: str) -> str:
    rows = db.query(Preset).filter(Preset.device_id == device_id).order_by(Preset.button).all()
    include_art = _station_logo_enabled(db, device_id)
    timestamp = "2026-06-20T00:00:00.000+00:00"
    parts = []
    for preset in rows:
        station = db.query(Station).filter(Station.id == preset.station_id).one_or_none() if preset.station_id else None
        values = _preset_values(preset, station, include_art=include_art)
        values["location"] = _effective_station_location(db, station, values["location"], include_art=include_art)
        if not values["location"]:
            continue
        parts.append(
            f'<preset buttonNumber="{preset.button}">'
            f"<containerArt>{escape(values['art'])}</containerArt>"
            f"<contentItemType>{escape(values['type'])}</contentItemType>"
            f"<createdOn>{timestamp}</createdOn><location>{escape(values['location'])}</location>"
            f"<name>{escape(values['name'])}</name>{_preset_source_xml(preset.source)}"
            f"<updatedOn>{timestamp}</updatedOn><username>{escape(values['name'])}</username></preset>"
        )
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><presets>' + "".join(parts) + "</presets>"


def _content_item_xml_for_preset(preset: Preset, station: Station | None, *, include_art: bool, location: str = "") -> str:
    existing_root = None
    try:
        existing_root = ET.fromstring(preset.content_item_xml or "")
        root = existing_root
    except ET.ParseError:
        root = ET.Element("ContentItem")
    root.attrib["source"] = normalize_source_name(preset.source or root.attrib.get("source"))
    root.attrib["type"] = root.attrib.get("type") or "stationurl"
    root.attrib["location"] = location or preset.location or root.attrib.get("location") or ""
    root.attrib["sourceAccount"] = preset.source_account or root.attrib.get("sourceAccount") or ""
    root.attrib["isPresetable"] = "true"

    item_name = next((child for child in root if child.tag.rsplit("}", 1)[-1] == "itemName"), None)
    if item_name is None:
        item_name = ET.SubElement(root, "itemName")
    if station and station.name:
        item_name.text = station.name

    for child in list(root):
        if child.tag.rsplit("}", 1)[-1] == "containerArt":
            root.remove(child)
    if include_art:
        art = (station.image_url if station else "") or ""
        if not art and existing_root is not None:
            art = existing_root.findtext("containerArt", "") or ""
        if art:
            ET.SubElement(root, "containerArt").text = art
    return ET.tostring(root, encoding="unicode")


def _content_presets_xml(db: Session, device_id: str) -> str:
    rows = db.query(Preset).filter(Preset.device_id == device_id).order_by(Preset.button).all()
    staged_deletes = active_delete_buttons(db, device_id)
    include_art = _station_logo_enabled(db, device_id)
    parts = []
    for preset in rows:
        if preset.button in staged_deletes:
            continue
        station = db.query(Station).filter(Station.id == preset.station_id).one_or_none() if preset.station_id else None
        location = _effective_station_location(db, station, preset.location, include_art=include_art)
        parts.append(
            f'<preset id="{preset.button}" createdOn="" updatedOn="">'
            f"{_content_item_xml_for_preset(preset, station, include_art=include_art, location=location)}"
            f"</preset>"
        )
    return '<?xml version="1.0" encoding="UTF-8"?><presets>' + "".join(parts) + "</presets>"


def _preset_etag(body: str) -> str:
    return '"' + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16] + '"'


def sourceproviders_xml() -> str:
    timestamp = "2026-06-20T00:00:00.000+00:00"
    rows = "".join(
        f'<sourceprovider id="{service["provider_id"]}"><createdOn>{timestamp}</createdOn><name>{service["name"]}</name><updatedOn>{timestamp}</updatedOn></sourceprovider>'
        for service in provider_rows()
    )
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sourceProviders>{rows}</sourceProviders>'


def account_source_rows_xml() -> str:
    sources = [(service["source_id"], service["name"], service["provider_id"], service["name"].replace("_", " ").title(), service.get("credential", ""), "2026-06-20T00:00:00.000+00:00") for service in provider_rows() if service.get("source_visible")]
    rows = []
    for source_id, name, provider_id, label, secret, timestamp in sources:
        rows.append(
            f'<source id="{source_id}" type="Audio" displayName="">'
            f"<createdOn>{timestamp}</createdOn>"
            f'<credential type="token">{escape(secret)}</credential>'
            f"<name>{name}</name>"
            f"<sourceproviderid>{provider_id}</sourceproviderid>"
            f"<sourcename>{escape(label)}</sourcename>"
            "<sourceSettings/>"
            f"<updatedOn>{timestamp}</updatedOn>"
            "<username></username>"
            "</source>"
        )
    return "".join(rows)


def _device_account_uuid(device: Device) -> str:
    try:
        root = ET.fromstring(device.info_xml or "")
    except ET.ParseError:
        return ""
    return root.findtext("margeAccountUUID", "").strip()


def _embedded_device_presets_xml(db: Session, device_id: str) -> str:
    root = ET.fromstring(marge_presets_xml(db, device_id))
    return "".join(ET.tostring(child, encoding="unicode") for child in root.findall("preset"))


def account_devices_xml(db: Session, account_id: str = "") -> str:
    rows = []
    devices = db.query(Device).order_by(Device.name).all()
    if account_id:
        matched = [device for device in devices if _device_account_uuid(device) == account_id]
        devices = matched
    for device in devices:
        try:
            device_ip = ipaddress.ip_address(device.ip_address)
        except ValueError:
            continue
        if device_ip.version != 4 or device_ip.is_loopback or device_ip.is_link_local:
            continue
        rows.append(
            f'<device deviceid="{escape(device.device_id, quote=True)}">'
            f'<attachedProduct product_code="{escape(device.model or "SoundTouch", quote=True)}">'
            "<components/>"
            f"<productlabel>{escape(device.model or 'SoundTouch')}</productlabel>"
            f"<serialnumber>{escape(device.device_id)}</serialnumber>"
            "<updatedOn>2026-06-19T00:00:00.000+00:00</updatedOn>"
            "</attachedProduct>"
            "<createdOn>2026-06-19T00:00:00.000+00:00</createdOn>"
            f"<firmwareVersion>{escape(device.firmware or '')}</firmwareVersion>"
            f"<ipaddress>{escape(device.ip_address)}</ipaddress>"
            f"<name>{escape(device.name)}</name>"
            f"<presets>{_embedded_device_presets_xml(db, device.device_id)}</presets>"
            "<recents/>"
            "<updatedOn>2026-06-19T00:00:00.000+00:00</updatedOn>"
            "</device>"
        )
    return "".join(rows)


def account_full_xml(account_id: str, db: Session) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<account id="{escape(account_id, quote=True)}">'
        "<accountStatus>OK</accountStatus>"
        f"<devices>{account_devices_xml(db, account_id)}</devices>"
        "<mode>global</mode>"
        "<preferredLanguage>en</preferredLanguage>"
        "<providerSettings/>"
        f"<sources>{account_source_rows_xml()}</sources>"
        "</account>"
    )


def marge_recents_xml() -> str:
    """Return an empty but schema-valid Marge recents collection."""
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><recents/>'


def provider_settings_xml(account_id: str) -> str:
    safe_account = escape(account_id)
    spotify_id = provider("SPOTIFY")["provider_id"]
    amazon_id = provider("AMAZON")["provider_id"]
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        "<providerSettings>"
        "<providerSetting>"
        f"<boseId>{safe_account}</boseId>"
        "<keyName>ELIGIBLE_FOR_TRIAL</keyName>"
        "<value>false</value>"
        f"<providerId>{spotify_id}</providerId>"
        "</providerSetting>"
        "<providerSetting>"
        f"<boseId>{safe_account}</boseId>"
        "<keyName>STREAMING_QUALITY</keyName>"
        "<value>2</value>"
        f"<providerId>{amazon_id}</providerId>"
        "</providerSetting>"
        "</providerSettings>"
    )


@router.get("/")
async def cloud_root(request: Request, db: Session = Depends(get_db)) -> Response:
    log_request(db, direction="in", service="cloud", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return Response("<status>OK</status>", media_type="application/xml")


@router.get("/about", response_class=HTMLResponse)
async def cloud_about() -> str:
    version = get_settings().version
    display_version = version if str(version).startswith("v") else f"v{version}"
    return f"""<!doctype html><html lang="de"><head><meta name="viewport" content="width=device-width"><title>BASSWIESN Cloud · 1516</title><style>body{{font-family:system-ui;background:#11151d;color:#eef3f8;max-width:850px;margin:40px auto;padding:20px}}a{{color:#ff5bbd}}section{{background:#1b2230;border:1px solid #37445a;border-radius:16px;padding:20px;margin:16px 0}}code{{color:#8fe3ca}}</style></head><body><h1>BASSWIESN Cloud · Port 1516</h1><p>Dieser Dienst ersetzt die für lokale SoundTouch-Funktionen benötigten Marge-, BMX- und Orion-Antworten. Radios greifen direkt auf diesen Port zu.</p><section><h2>Wichtige Bereiche</h2><p><a href="/bmx/registry/v1/services">BMX Registry</a> · <a href="/streaming/sourceproviders">Source Provider</a> · <a href="/docs">API-Dokumentation</a></p></section><section><h2>Was hier passiert</h2><p>Account-Zuordnung, Quellenregistrierung, Preset-Synchronisation und Internetradio-Station-Descriptor. Die normale Bedienung erfolgt weiterhin über Port 1328.</p></section><p>Version {display_version}</p></body></html>"""


@router.get("/streaming/sourceproviders")
async def streaming_sourceproviders(request: Request, db: Session = Depends(get_db)) -> Response:
    body = sourceproviders_xml()
    headers = {"ETag": '"basswiesn-sourceproviders-v1"'}
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return Response(body, media_type=STREAMING_MEDIA_TYPE, headers=headers)


@router.get("/streaming/provider-discovery")
@router.get("/bmx/registry/v1/introspect")
async def provider_discovery(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    services = [{key: value for key, value in service.items() if key != "credential"} for service in provider_rows()]
    log_request(db, direction="in", service="cloud", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return JSONResponse({"services": services})


@router.post("/streaming/support/power_on")
async def streaming_power_on(request: Request, db: Session = Depends(get_db)) -> Response:
    body = (await request.body()).decode("utf-8", errors="replace")
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200, body=body)
    return Response("<status>OK</status>", media_type=STREAMING_MEDIA_TYPE)


@router.get("/streaming/device/{device_id}/streaming_token")
async def streaming_token(device_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    token = f"st-local-token-{device_id}"
    auth = f"Bearer {token}"
    body = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><bearertoken value="{auth}"></bearertoken>'
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return Response(body, media_type=STREAMING_MEDIA_TYPE, headers={"Authorization": auth})


@router.get("/v1/blacklist/{device_id}")
@router.post("/v1/blacklist/{device_id}")
async def device_blacklist(device_id: str, request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    body = (await request.body()).decode("utf-8", errors="replace") if request.method == "POST" else ""
    log_request(db, direction="in", service="blacklist", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200, body=body)
    return JSONResponse({"device_id": device_id, "blacklist": [], "status": "ok"})


@router.get("/streaming/account/{account_id}/full")
async def streaming_account_full(account_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    body = account_full_xml(account_id, db)
    headers = {"ETag": _preset_etag(body)}
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return Response(body, media_type=STREAMING_MEDIA_TYPE, headers=headers)


@router.get("/streaming/account/{account_id}/provider_settings")
async def streaming_provider_settings(account_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    body = provider_settings_xml(account_id)
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return Response(body, media_type=STREAMING_MEDIA_TYPE)


@router.api_route("/serviceSettings", methods=["GET", "POST"])
@router.api_route("/getServiceSettings", methods=["GET", "POST"])
async def service_settings(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    body = (await request.body()).decode("utf-8", errors="replace") if request.method == "POST" else ""
    log_request(db, direction="in", service="cloud", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200, body=redact_support_text(body))
    return JSONResponse({"quality": "high", "region": "DE", "premium": True, "retryAllowed": True, "retryDelay": 5})


@router.get("/stationInfo")
async def station_info(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    station = db.query(Station).order_by(Station.id).first()
    body = {
        "stationId": str(station.id) if station else "basswiesn_radio",
        "name": station.name if station else "BassWiesn Radio",
        "description": "Local Internet Radio",
        "logo": station.image_url if station else "",
        "genre": "Mixed",
    }
    log_request(db, direction="in", service="cloud", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return JSONResponse(body)


@router.post("/setMusicServiceAccount")
async def set_music_service_account(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    raw = (await request.body()).decode("utf-8", errors="replace")
    payload = _json_or_xml_credentials(raw)
    source = str(payload.get("source") or payload.get("sourceName") or payload.get("service") or "PANDORA").upper()
    log_request(db, direction="in", service="credentials", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200, body=redact_support_text(raw))
    return JSONResponse(_credential_response(source, "Credentials", payload))


@router.post("/setMusicServiceOAuthAccount")
async def set_music_service_oauth_account(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    raw = (await request.body()).decode("utf-8", errors="replace")
    payload = _json_or_xml_credentials(raw)
    source = str(payload.get("source") or payload.get("src") or payload.get("service") or "SPOTIFY").upper()
    log_request(db, direction="in", service="oauth", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200, body=redact_support_text(raw))
    return JSONResponse(_credential_response(source, "OAuthCredentials", payload))


@router.get("/group")
async def group_state(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    log_request(db, direction="in", service="group", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return JSONResponse({"groups": list(GROUPS.values()), "clockSyncAssist": "lab", "leaderFailover": "state-only"})


@router.post("/group/create")
async def group_create(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    group_id = str(payload.get("group_id") or payload.get("id") or f"group-{int(time.time())}")
    group = {
        "group_id": group_id,
        "leader": payload.get("leader") or payload.get("master") or "",
        "members": payload.get("members") or [],
        "source": payload.get("source") or "",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "active",
    }
    GROUPS[group_id] = group
    log_request(db, direction="in", service="group", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200, body=str(payload)[:1000])
    return JSONResponse({"ok": True, "group": group})


@router.post("/group/update")
async def group_update(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    group_id = str(payload.get("group_id") or payload.get("id") or "")
    group = GROUPS.setdefault(group_id or f"group-{int(time.time())}", {"group_id": group_id or f"group-{int(time.time())}", "created_at": datetime.now(UTC).isoformat()})
    for key in ("leader", "members", "source", "status"):
        if key in payload:
            group[key] = payload[key]
    group["updated_at"] = datetime.now(UTC).isoformat()
    GROUPS[group["group_id"]] = group
    log_request(db, direction="in", service="group", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200, body=str(payload)[:1000])
    return JSONResponse({"ok": True, "group": group})


@router.post("/group/delete")
async def group_delete(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    group_id = str(payload.get("group_id") or payload.get("id") or "")
    removed = GROUPS.pop(group_id, None) if group_id else None
    log_request(db, direction="in", service="group", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200, body=str(payload)[:1000])
    return JSONResponse({"ok": True, "deleted": bool(removed), "group_id": group_id})


@router.post("/streaming/account/{account_id}/device/", status_code=201)
async def streaming_add_device(account_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    """Accept the AddDevice callback used by `/setMargeAccount`.

    A factory-reset speaker refuses to persist its Marge account when this
    callback returns 404.  The response shape and 201 status follow the native
    streaming-v1.2 service.
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    try:
        incoming = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise HTTPException(status_code=400, detail="invalid device XML") from exc
    device_id = incoming.attrib.get("deviceid", "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceid is required")
    name = incoming.findtext("name", "").strip()
    device = db.query(Device).filter(Device.device_id == device_id).one_or_none()
    if device is not None:
        if name:
            device.name = name
        try:
            info = ET.fromstring(device.info_xml or f'<info deviceID="{escape(device_id, quote=True)}"/>')
            account_node = info.find("margeAccountUUID")
            if account_node is None:
                account_node = ET.SubElement(info, "margeAccountUUID")
            account_node.text = account_id
            device.info_xml = ET.tostring(info, encoding="unicode")
        except ET.ParseError:
            pass
        db.commit()
    remote_ip = request.client.host if request.client else ""
    now = "2026-06-20T00:00:00.000+00:00"
    body = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<device deviceid="{escape(device_id, quote=True)}">'
        f'<createdOn>{now}</createdOn><ipaddress>{escape(remote_ip)}</ipaddress>'
        f'<name>{escape(name)}</name><updatedOn>{now}</updatedOn></device>'
    )
    location = f"/streaming/account/{account_id}/device/{device_id}"
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=201, body=raw)
    return Response(body, status_code=201, media_type=STREAMING_MEDIA_TYPE, headers={"Location": location})


@router.get("/streaming/account/{account_id}/device/{device_id}")
async def streaming_get_device(account_id: str, device_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    body = account_devices_xml(db, account_id) or (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<device deviceid="{escape(device_id, quote=True)}"><createdOn>2026-06-20T00:00:00.000+00:00</createdOn><updatedOn>2026-06-20T00:00:00.000+00:00</updatedOn></device>'
    )
    if not body.strip().startswith("<device"):
        body = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><devices>{body}</devices>'
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return Response(body, media_type=STREAMING_MEDIA_TYPE)


@router.put("/streaming/account/{account_id}/device/{device_id}")
async def streaming_put_device(account_id: str, device_id: str, request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    body = (await request.body()).decode("utf-8", errors="replace")
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200, body=redact_support_text(body))
    return JSONResponse({"status": "ok", "action": "accepted", "account": account_id, "device": device_id})


@router.delete("/streaming/account/{account_id}/device/{device_id}")
async def streaming_delete_device(account_id: str, device_id: str, request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return JSONResponse({"status": "ok", "action": "noop", "account": account_id, "device": device_id})


@router.post("/streaming/account/{account_id}/device/{device_id}/heartbeat")
@router.post("/streaming/account/{account_id}/device/{device_id}/keepalive")
async def streaming_device_keepalive(account_id: str, device_id: str, request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    body = (await request.body()).decode("utf-8", errors="replace")
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200, body=redact_support_text(body))
    return JSONResponse({"status": "ok", "action": "keepalive", "account": account_id, "device": device_id})


@router.get("/streaming/account/{account_id}/sources")
async def streaming_account_sources(account_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    body = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sources>' + account_source_rows_xml() + "</sources>"
    etag = _preset_etag(body)
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(body, media_type=STREAMING_MEDIA_TYPE, headers={"ETag": etag})


@router.get("/streaming/account/{account_id}/device/{device_id}/presets")
async def streaming_device_presets(account_id: str, device_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    body = marge_presets_xml(db, device_id)
    etag = _preset_etag(body)
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(body, media_type=STREAMING_MEDIA_TYPE, headers={"ETag": etag})


@router.get("/streaming/account/{account_id}/device/{device_id}/recents")
async def streaming_device_recents(account_id: str, device_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    body = marge_recents_xml()
    etag = _preset_etag(body)
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(body, media_type=STREAMING_MEDIA_TYPE, headers={"ETag": etag})


@router.post("/streaming/account/{account_id}/device/{device_id}/recent")
async def streaming_device_recent(account_id: str, device_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    body = (await request.body()).decode("utf-8", errors="replace")
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200, body=body)
    return Response(marge_recents_xml(), media_type=STREAMING_MEDIA_TYPE)


@router.get("/streaming/account/{account_id}/presets/all")
async def streaming_account_presets(account_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    device = db.query(Device).order_by(Device.last_seen.desc()).first()
    body = marge_presets_xml(db, device.device_id) if device else '<?xml version="1.0" encoding="UTF-8"?><presets/>'
    etag = _preset_etag(body)
    log_request(db, direction="in", service="streaming", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return Response(body, media_type="application/vnd.bose.streaming-v1.1+xml", headers={"ETag": etag})


@router.post("/core02/svc-bmx-adapter-orion/prod/orion/token")
async def orion_token(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    body = (await request.body()).decode("utf-8", errors="replace")
    token = f"orion-local-token-{int(time.time())}"
    log_request(db, direction="in", service="orion", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200, body=redact_support_text(body))
    return JSONResponse(
        {
            "_embedded": {"bmx_account": {"displayName": "basswiesn", "username": ""}},
            "access_token": token,
            "refresh_token": token,
            "token_type": "Bearer",
        }
    )


def _bmx_service_descriptor(service: dict, base: str, icons: dict[str, str]) -> dict:
    """Build one Bose-compatible descriptor from local provider metadata.

    The links are intentionally service-specific.  In particular, Orion is a
    playback adapter and does not advertise TuneIn-style navigation.  Older
    BASSWIESN builds advertised ``/v1/navigate`` for every adapter; some
    firmware then cached the catch-all response as an unavailable source and
    returned UNKNOWN_SOURCE_ERROR on the next ``/select``.
    """

    adapter = str(service.get("adapter") or "")
    links: dict[str, dict[str, str]] = {"self": {"href": "/"}}
    if adapter == "orion":
        links["bmx_token"] = {"href": "/token"}
    elif adapter == "tunein":
        links["bmx_navigate"] = {"href": "/v1/navigate"}
        links["bmx_token"] = {"href": "/v1/token"}
    elif adapter:
        links["bmx_navigate"] = {"href": "/v1/navigate"}
        links["bmx_token"] = {"href": "/v1/token"}
    return {
        "_links": links,
        "askAdapter": False,
        "id": {"name": service["name"], "value": service["provider_id"]},
        "baseUrl": f"{base}/core02/svc-bmx-adapter-orion/prod/orion" if adapter == "orion" else f"{base}/bmx/{adapter}",
        "assets": {
            "color": "#000000",
            "name": service["name"].replace("_", " ").title(),
            "description": "BASSWIESN local service.",
            "icons": icons,
        },
        "authenticationModel": {
            "anonymousAccount": {
                "enabled": service["auth_model"] == "anonymous",
                "autoCreate": service["auth_model"] == "anonymous",
            }
        },
        "streamTypes": service["stream_types"],
    }


@router.get("/bmx/registry/v1/services")
async def bmx_registry(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    from basswiesn.app.config import get_settings
    base = get_settings().local_base_url
    icons = {
        "largeSvg": f"{base}/static/bmx-icons/orion/monochrome.svg",
        "monochromePng": f"{base}/static/bmx-icons/orion/monochrome_v2.png",
        "monochromeSvg": f"{base}/static/bmx-icons/orion/monochrome.svg",
        "smallSvg": f"{base}/static/bmx-icons/orion/monochrome.svg",
    }
    registry_services = []
    for service in provider_rows():
        if not service["visible"] or not service["adapter"]:
            continue
        registry_services.append(_bmx_service_descriptor(service, base, icons))
    body = {
        "_links": {"bmx_services_availability": {"href": "../servicesAvailability"}},
        "askAgainAfter": 60000,
        "bmx_services": registry_services,
    }
    log_request(db, direction="in", service="cloud", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return JSONResponse(body)


@router.get("/bmx/registry/v1/servicesAvailability")
@router.get("/bmx/registry/servicesAvailability")
async def bmx_services_availability(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    body = {"services": [{"canAdd": service["can_add"], "canRemove": service["can_remove"], "service": service["name"]} for service in provider_rows() if service["visible"]]}
    log_request(db, direction="in", service="cloud", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return JSONResponse(body)


@router.get("/core02/svc-bmx-adapter-orion/prod/orion")
async def orion_service(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    settings = get_settings()
    base = settings.local_base_url.rstrip("/")
    icons = {
        "largeSvg": f"{base}/static/bmx-icons/orion/monochrome.svg",
        "monochromePng": f"{base}/static/bmx-icons/orion/monochrome_v2.png",
        "monochromeSvg": f"{base}/static/bmx-icons/orion/monochrome.svg",
        "smallSvg": f"{base}/static/bmx-icons/orion/monochrome.svg",
    }
    service = next(item for item in provider_rows() if item["name"] == "LOCAL_INTERNET_RADIO")
    body = _bmx_service_descriptor(service, base, icons)
    log_request(db, direction="in", service="cloud", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return JSONResponse(body)


def _provider_interval(db: Session, key: str, default: int, *, minimum: int) -> int:
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    try:
        value = int(row.value) if row is not None else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, min((1 << 32) - 1, value))


def _validated_report_fields(decoded: dict) -> dict:
    text_fields = (
        "timeStamp",
        "eventType",
        "reason",
        "absolutePlayPoint",
        "reasonSubCode",
    )
    int32_fields = ("timeIntoTrack", "playbackDelay")
    result: dict = {}
    for key in text_fields:
        if key not in decoded:
            continue
        if not isinstance(decoded[key], str):
            raise HTTPException(
                status_code=400, detail=f"BMX.Report {key} must be a string"
            )
        result[key] = decoded[key]
    for key in int32_fields:
        if key not in decoded:
            continue
        value = decoded[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not -(1 << 31) <= value < (1 << 31)
        ):
            raise HTTPException(
                status_code=400, detail=f"BMX.Report {key} must be int32"
            )
        result[key] = value
    return result


async def _unsupported_provider_contract(
    request: Request,
    db: Session,
    *,
    provider_id: str,
    contract: str,
) -> JSONResponse:
    """Reject an old experimental provider surface without fake success."""

    raw = await request.body()
    device = resolve_request_device(db, request)
    ResearchStateRepository(db).record_event(
        device_id=device.device_id if device else None,
        domain="PROVIDER",
        code="PROVIDER_CONTRACT_UNSUPPORTED",
        severity="WARNING",
        message="An experimental provider contract was rejected without changing playback.",
        evidence={
            "provider_id": provider_id,
            "contract": contract,
            "method": request.method,
            "path": request.url.path,
            "playback_action": "NONE",
        },
    )
    db.commit()
    log_request(
        db,
        direction="in",
        service=f"bmx-{provider_id.lower()}-unsupported",
        method=request.method,
        path=request.url.path,
        host=request.headers.get("host", ""),
        status_code=501,
        body=redact_support_text(raw.decode("utf-8", errors="replace")),
    )
    return JSONResponse(
        {
            "type": "https://basswiesn.local/problems/provider-contract-unsupported",
            "title": "Provider contract unsupported",
            "status": 501,
            "detail": (
                f"{provider_id} {contract} is not backed by a confirmed product "
                "contract and is disabled in BASSWIESN 2.0.0."
            ),
            "provider": provider_id,
            "contract": contract,
        },
        status_code=501,
        media_type="application/problem+json",
    )


def _station_by_contract_key(db: Session, station_id: str) -> Station | None:
    station = (
        db.query(Station)
        .filter(Station.provider_station_id == str(station_id))
        .one_or_none()
    )
    if station is None and str(station_id).isdigit():
        station = db.query(Station).filter(Station.id == int(station_id)).one_or_none()
    if station is None:
        for candidate in db.query(Station).order_by(Station.id).all():
            descriptor = StationDescriptor(
                candidate.name,
                candidate.stream_url,
                candidate.image_url,
                candidate.provider_station_id,
                stream_url_resolved=candidate.stream_url_resolved,
                stream_format=candidate.stream_format,
                stream_mime=candidate.stream_mime,
                compatibility_warning=candidate.compatibility_warning,
            )
            if station_contract_key(descriptor) == str(station_id):
                return candidate
    return station


def _persist_orion_station_contract(
    db: Session,
    request: Request,
    descriptor: StationDescriptor,
    response: dict,
) -> None:
    """Persist an inbound selection contract without any radio action."""

    device = resolve_request_device(db, request)
    if device is None:
        return
    observed = datetime.now(UTC)
    repository = ResearchStateRepository(db)
    contract_key = station_contract_key(descriptor)
    source_key = f"{LOCAL_PROVIDER_ID}:{contract_key}"
    repository.upsert_restrictions(
        device.device_id,
        source_key,
        parse_restrictions(response, received_at=observed, source="BMX.Station"),
    )
    existing = (
        db.query(MetadataState)
        .filter(MetadataState.device_id == device.device_id)
        .one_or_none()
    )
    same_selection = bool(
        existing is not None
        and existing.station_id == contract_key
        and existing.provider == LOCAL_PROVIDER_ID
        and existing.source == LOCAL_PROVIDER_ID
    )
    repository.upsert_metadata(
        device.device_id,
        MetadataSnapshot(
            station_name=descriptor.name,
            station_id=contract_key,
            track=existing.track if same_selection else None,
            artist=existing.artist if same_selection else None,
            album=existing.album if same_selection else None,
            image_url=descriptor.image_url or (
                existing.artwork_url if same_selection else None
            ),
            provider=LOCAL_PROVIDER_ID,
            source=LOCAL_PROVIDER_ID,
            updated_at=observed,
            provenance=MetadataProvenance.PROVIDER,
            confidence=100,
            stale=False,
        ),
    )
    reporting_url = response.get("_links", {}).get("bmx_reporting", {}).get("href", "")
    if reporting_url:
        repository.observe_reporting_contract(
            device.device_id,
            LOCAL_PROVIDER_ID,
            report_url=reporting_url,
            observed_at=observed,
        )
    repository.upsert_provider_health(
        device.device_id,
        LOCAL_PROVIDER_ID,
        reduce_provider_health(
            ProviderSignals(
                source_visible=True,
                service_available=True,
                account_available=True,
                auth_valid=True,
                last_success=observed,
                evidence=[{"source": "local_bmx_station_response"}],
            ),
            since=observed,
        ),
        source=LOCAL_PROVIDER_ID,
        availability="AVAILABLE",
        association="AVAILABLE",
    )
    db.commit()
    runtime = getattr(request.app.state, "research_runtime", None)
    if runtime is not None:
        # One absolute per-device stale deadline; no provider/radio polling.
        runtime.schedule_metadata_staleness(device.device_id)


@router.get("/core02/svc-bmx-adapter-orion/prod/orion/station")
async def orion_station(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    data = request.query_params.get("data", "")
    decoded = decode_orion_data(data)
    descriptor = StationDescriptor(
        name=decoded.get("name", "Custom Station"),
        stream_url=decoded.get("streamUrl", ""),
        image_url=decoded.get("imageUrl", ""),
        tunein_id=decoded.get("tuneinId", ""),
        stream_url_resolved=decoded.get("streamUrlResolved", ""),
        stream_format=decoded.get("streamFormat", ""),
        stream_mime=decoded.get("streamMime", ""),
        compatibility_warning=decoded.get("compatibilityWarning", ""),
    )
    analysis = analyze_stream_url(descriptor.stream_url, descriptor.stream_mime, resolved_url=descriptor.stream_url_resolved)
    request_device = resolve_request_device(db, request)
    if request_device is not None:
        try:
            gate = await wait_for_provider_release(db, request_device.device_id)
            if gate.get("required"):
                write_masterlog(
                    "playback_provider_safety_released",
                    device_id=request_device.device_id,
                    radio_ip=request_device.ip_address,
                    volume_readback=gate.get("volume_readback"),
                )
        except PlaybackSafetyGateError as exc:
            write_masterlog(
                "playback_provider_safety_blocked",
                device_id=request_device.device_id,
                radio_ip=request_device.ip_address,
                error=str(exc)[:300],
            )
            raise HTTPException(
                status_code=503,
                detail="audio URL withheld because post-select volume 1 and mute were not verified",
            ) from exc
    if analysis.compatibility_warning:
        write_masterlog(
            "stream_compatibility_warning",
            station_id=descriptor.tunein_id,
            stream_url_original=redact_url(analysis.stream_url_original),
            stream_url_resolved=redact_url(analysis.stream_url_resolved),
            stream_format=analysis.stream_format,
            stream_mime=analysis.stream_mime,
            compatibility_score=analysis.compatibility_score,
            compatibility_warning=analysis.compatibility_warning,
        )
    response = playback_response(descriptor, base_url=str(request.base_url).rstrip("/"))
    try:
        _persist_orion_station_contract(db, request, descriptor, response)
    except Exception as exc:
        # Provider playback must remain available if diagnostics persistence is
        # temporarily unavailable.  No device operation is attempted here.
        db.rollback()
        write_masterlog(
            "research_state_persistence_failed",
            domain="provider_contract",
            error=type(exc).__name__,
        )
    log_request(db, direction="in", service="orion", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    write_masterlog("streamlist_infinite_served", station_id=descriptor.tunein_id, station_name=descriptor.name, stream_url=redact_url(analysis.stream_url_resolved or analysis.stream_url_original))
    return JSONResponse(response)


@router.get("/bmx/orion/now-playing")
@router.get("/bmx/orion/now-playing/station/{station_id}")
async def now_playing(
    request: Request,
    station_id: str = "custom",
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Serve mutable metadata without touching playback selection."""

    device = resolve_request_device(db, request)
    body = _orion_now_playing_payload(db, device, station_id)
    log_request(
        db,
        direction="in",
        service="orion-metadata",
        method=request.method,
        path=request.url.path,
        host=request.headers.get("host", ""),
        status_code=200,
    )
    return JSONResponse(body)


def _orion_now_playing_payload(
    db: Session,
    device: Device | None,
    station_id: str,
) -> dict:
    """Build one mutable NowPlaying projection for GET and ReportResponse.

    Phase-12/13 research confirms that ``BMX.ReportResponse._embedded`` may
    carry ``bmx_nowplaying``.  Some 27.0.6 radios fetch the linked resource
    only once and consume subsequent live metadata through reporting, so both
    transports must expose the exact same projection.
    """

    # Firmware 27.0.6 has a 5 s lower scheduling branch, but the recovered
    # contract recommends supplying at least six seconds.  Sending exactly
    # five was accepted for the initial projection on real hardware yet did
    # not schedule a follow-up GET.  Six keeps us above the confirmed floor
    # without accelerating metadata traffic beyond the firmware policy.
    interval = _provider_interval(
        db, "bmx_metadata_interval_seconds", 6, minimum=5
    )
    metadata_row = (
        db.query(MetadataState)
        .filter(MetadataState.device_id == device.device_id)
        .one_or_none()
        if device is not None
        else None
    )
    station = _station_by_contract_key(db, station_id)
    # A late now-playing poll for a newly selected station must never expose
    # the previous selection's track/artist/album/artwork.
    current_metadata = (
        metadata_row
        if metadata_row is not None and metadata_row.station_id == station_id
        else None
    )
    raw_track = (
        current_metadata.track
        if current_metadata is not None and current_metadata.track
        else station.name
        if station is not None
        else ""
    )
    displayed_track = raw_track
    if device is not None:
        preference = load_clock_metadata_preference(db, device.device_id)
        if preference.enabled and clock_metadata_lab_enabled(db):
            displayed_track = clock_display_projection(
                MetadataSnapshot(
                    # Station identity is not a live title.  Keeping it out of
                    # the LAB projection preserves the confirmed "no title →
                    # HH:MM" behavior.
                    track=current_metadata.track if current_metadata is not None else None,
                    artist=current_metadata.artist if current_metadata is not None else None,
                ),
                mode=preference.mode,
            ) or ""
            # Clock projection is deliberately slower than normal live-track
            # metadata.  Enabling the LAB clock must never accelerate it below
            # its explicit >=60 s preference.
            interval = max(interval, preference.interval_seconds)
            if current_metadata is not None:
                current_metadata.display_projection = displayed_track or None
                db.flush()
    return {
        "track": displayed_track,
        "album": (current_metadata.album or "") if current_metadata is not None else "",
        "artist": (current_metadata.artist or "") if current_metadata is not None else "",
        "askAgainAfter": interval,
        "imageUrl": (
            current_metadata.artwork_url
            if current_metadata is not None and current_metadata.artwork_url
            else station.image_url
            if station is not None
            else ""
        ),
        "_links": {},
    }


@router.post("/bmx/orion/reporting")
@router.post("/bmx/orion/reporting/station/{station_id}")
async def reporting(
    request: Request,
    station_id: str = "custom",
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Accept one BMX.Report and return the confirmed ReportResponse schema."""

    return await _accept_reporting_contract(
        request,
        db,
        station_id=station_id,
        provider_id=LOCAL_PROVIDER_ID,
        service="orion-reporting",
    )


async def _accept_reporting_contract(
    request: Request,
    db: Session,
    *,
    station_id: str,
    provider_id: str,
    service: str,
) -> JSONResponse:
    """Shared confirmed POST contract for every advertised local adapter."""

    raw = await request.body()
    if len(raw) > 64 * 1024:
        raise HTTPException(status_code=413, detail="report payload is too large")
    if raw.strip():
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid BMX.Report JSON") from exc
        if not isinstance(decoded, dict):
            raise HTTPException(status_code=400, detail="BMX.Report must be a JSON object")
    else:
        # Firmware messages are optional-field protobufs; the empty object is
        # valid and keeps compatibility with probes that send an empty body.
        decoded = {}
    report_fields = _validated_report_fields(decoded)
    # The local provider uses the confirmed timed-report loop as its reliable
    # runtime-metadata transport.  Portable firmware 27.0.6 consumes the
    # initial bmx_nowplaying link but does not issue follow-up GETs from the
    # returned askAgainAfter hint.  A timed ReportResponse can carry the same
    # projection without selecting a source or touching the stream.  Six
    # seconds stays above the recovered metadata scheduling boundary and is a
    # deliberately local server policy, not a guessed Bose default.
    interval = _provider_interval(
        db, "bmx_reporting_interval_seconds", 6, minimum=1
    )
    observed = datetime.now(UTC)
    next_due = observed + timedelta(seconds=interval)
    device = resolve_request_device(db, request)
    repository = ResearchStateRepository(db)
    if device is not None:
        repository.record_reporting_success(
            device.device_id,
            provider_id,
            next_due_at=next_due,
            report_url=str(request.url),
            evidence={
                "station_id": station_id,
                "fields": report_fields,
                "transport": "HTTP_POST",
                "http_status": 200,
            },
            observed_at=observed,
        )
    else:
        repository.record_event(
            device_id=None,
            domain="REPORTING",
            code="UNATTRIBUTED_REPORT_OK",
            message="Provider report accepted without a known device mapping.",
            evidence={"station_id": station_id, "fields": report_fields},
            occurred_at=observed,
        )
    db.commit()
    log_request(
        db,
        direction="in",
        service=service,
        method=request.method,
        path=request.url.path,
        host=request.headers.get("host", ""),
        status_code=200,
        body=redact_support_text(json.dumps(report_fields, ensure_ascii=False)),
    )
    link = str(request.url.replace(query=""))
    write_masterlog(
        "reporting_ack",
        provider_id=provider_id,
        station_id=station_id,
        nextReportIn=interval,
        device_id=device.device_id if device is not None else "",
    )
    embedded = {}
    if provider_id == LOCAL_PROVIDER_ID and device is not None:
        embedded["bmx_nowplaying"] = _orion_now_playing_payload(
            db, device, station_id
        )
        # The LAB clock projection may update its diagnostic display field.
        db.commit()
    return JSONResponse(
        {
            "nextReportIn": interval,
            "_links": {"bmx_reporting": {"href": link}},
            "_embedded": embedded,
        }
    )


def _bmx_station_payload(station_id: str, source: str) -> dict:
    return {
        "status": "ok",
        "source": source,
        "stationId": station_id,
        "playback": {"streamUrl": "", "contentType": "audio/mpeg"},
        "nowPlaying": {"title": station_id, "artist": "", "album": ""},
    }


@router.get("/bmx/tunein/v1/playback/station/{station_id}")
@router.get("/bmx/tunein/v1/now-playing/station/{station_id}")
@router.post("/bmx/tunein/v1/reporting/station/{station_id}")
@router.get("/bmx/tunein/v1/favorite/{station_id}")
@router.post("/bmx/tunein/v1/favorite/{station_id}")
async def bmx_tunein_station(station_id: str, request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    return await _unsupported_provider_contract(
        request,
        db,
        provider_id="TUNEIN",
        contract=f"station:{station_id}",
    )


@router.api_route("/bmx/tunein/v1/token", methods=["GET", "POST"])
async def bmx_tunein_token(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    return await _unsupported_provider_contract(
        request, db, provider_id="TUNEIN", contract="token"
    )


@router.get("/bmx/tunein/v1/navigate")
@router.get("/bmx/tunein/v1/navigate/{path:path}")
async def bmx_tunein_navigate(request: Request, path: str = "", db: Session = Depends(get_db)) -> JSONResponse:
    return await _unsupported_provider_contract(
        request, db, provider_id="TUNEIN", contract=f"navigate:{path}"
    )


@router.get("/bmx/radiobrowser/v1/playback/station/{uuid}")
@router.get("/bmx/radiobrowser/v1/now-playing/station/{uuid}")
@router.post("/bmx/radiobrowser/v1/reporting/station/{uuid}")
async def bmx_radiobrowser_station(uuid: str, request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    return await _unsupported_provider_contract(
        request,
        db,
        provider_id="RADIO_BROWSER",
        contract=f"station:{uuid}",
    )


@router.api_route("/bmx/resolve", methods=["GET", "POST"])
async def bmx_resolve(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    return await _unsupported_provider_contract(
        request, db, provider_id="BMX", contract="resolve"
    )


@router.get("/v1/systems/devices/{device_id}/presets")
async def marge_presets(device_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    body = _content_presets_xml(db, device_id)
    log_request(db, direction="in", service="marge", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return Response(body, media_type="application/xml")


@router.get("/v1/systems/devices/{device_id}/sources")
async def marge_sources(device_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    log_request(db, direction="in", service="marge", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return Response(sources_xml(), media_type="application/xml")


@router.get("/v1/systems/devices/{device_id}")
async def marge_full(device_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    body = f'<?xml version="1.0" encoding="UTF-8"?><boseAccount>{sources_xml()}{_content_presets_xml(db, device_id)}<recents /></boseAccount>'
    log_request(db, direction="in", service="marge", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200)
    return Response(body, media_type="application/xml")


@router.put("/streaming/account/{account_id}/device/{device_id}/preset/{button}")
@router.put("/streaming/account/{account_id}/device/{device_id}/presets/{button}")
@router.post("/streaming/account/{account_id}/device/{device_id}/presets/{button}")
async def put_preset(account_id: str, device_id: str, button: int, request: Request, db: Session = Depends(get_db)) -> Response:
    body = (await request.body()).decode("utf-8", errors="replace")
    from basswiesn.app.models import ConfigBackup, PresetMutation

    active_mutation = db.query(PresetMutation).filter(
        PresetMutation.device_id == device_id,
        PresetMutation.button == button,
        PresetMutation.state.in_(("PREPARED", "RADIO_WRITE", "RADIO_READBACK", "VERIFIED", "RECONCILE")),
    ).order_by(PresetMutation.revision.desc()).first()
    if active_mutation is not None:
        # This callback is radio evidence for the transaction already in
        # progress.  Committing it here would bypass RADIO_READBACK/VERIFIED.
        db.add(ConfigBackup(
            device_id=device_id,
            path=f"preset-inbound-staged/{active_mutation.mutation_id}.xml",
            content=body,
        ))
        db.commit()
        log_request(db, direction="in", service="marge", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200, body=body)
        response_body = marge_presets_xml(db, device_id)
        return Response(response_body, media_type=STREAMING_MEDIA_TYPE, headers={"ETag": _preset_etag(response_body)})
    preset = db.query(Preset).filter(Preset.device_id == device_id, Preset.button == button).one_or_none()
    if preset is None:
        preset = Preset(device_id=device_id, button=button)
        db.add(preset)
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise HTTPException(status_code=400, detail="invalid preset XML") from exc
    preset.source = normalize_source_name(root.findtext("sourceid", "") or preset.source)
    preset.source_account = root.findtext("username", "") or preset.source_account
    preset.location = root.findtext("location", "") or preset.location
    name = root.findtext("name", "") or root.findtext("username", "")
    art = root.findtext("containerArt", "")
    item_type = root.findtext("contentItemType", "") or "stationurl"
    preset.content_item_xml = (
        f'<ContentItem source="{escape(preset.source, quote=True)}" type="{escape(item_type, quote=True)}" '
        f'location="{escape(preset.location, quote=True)}" sourceAccount="" isPresetable="true">'
        f"<itemName>{escape(name)}</itemName><containerArt>{escape(art)}</containerArt></ContentItem>"
    )
    db.commit()
    log_request(db, direction="in", service="marge", method=request.method, path=request.url.path, host=request.headers.get("host", ""), status_code=200, body=body)
    response_body = marge_presets_xml(db, device_id)
    return Response(response_body, media_type=STREAMING_MEDIA_TYPE, headers={"ETag": _preset_etag(response_body)})


@router.delete("/streaming/account/{account_id}/device/{device_id}/preset/{button}")
@router.delete("/streaming/account/{account_id}/device/{device_id}/presets/{button}")
async def remove_preset(
    account_id: str,
    device_id: str,
    button: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Handle the confirmed Marge preset-removal callback fail-closed.

    An active BASSWIESN mutation owns its local commit.  In that case the
    callback is evidence only; ``_content_presets_xml`` omits the staged
    tombstone while the radio completes /removePreset.  A radio-originated
    callback without an active mutation is itself the authoritative request
    and may update the local mirror.
    """

    if button < 1 or button > 6:
        raise HTTPException(status_code=400, detail="preset button must be between 1 and 6")
    from basswiesn.app.models import ConfigBackup, PresetMutation

    active_mutation = db.query(PresetMutation).filter(
        PresetMutation.device_id == device_id,
        PresetMutation.button == button,
        PresetMutation.operation == "DELETE",
        PresetMutation.state.in_(("PREPARED", "RADIO_WRITE", "RADIO_READBACK", "VERIFIED", "RECONCILE")),
    ).order_by(PresetMutation.revision.desc()).first()
    if active_mutation is not None:
        db.add(ConfigBackup(
            device_id=device_id,
            path=f"preset-inbound-staged/{active_mutation.mutation_id}.delete",
            content=json.dumps({"account_id": account_id, "button": button}, sort_keys=True),
        ))
    else:
        preset = db.query(Preset).filter(
            Preset.device_id == device_id, Preset.button == button
        ).one_or_none()
        if preset is not None:
            db.delete(preset)
    db.commit()
    log_request(
        db,
        direction="in",
        service="marge",
        method=request.method,
        path=request.url.path,
        host=request.headers.get("host", ""),
        status_code=200,
    )
    response_body = marge_presets_xml(db, device_id)
    return Response(
        response_body,
        media_type=STREAMING_MEDIA_TYPE,
        headers={"ETag": _preset_etag(response_body)},
    )


@router.get("/api/cloud/stations")
async def cloud_stations(db: Session = Depends(get_db)) -> list[dict]:
    stations = db.query(Station).order_by(Station.name).all()
    return [{"id": s.id, "name": s.name, "stream_url": s.stream_url} for s in stations]


def _accepts_xml(request: Request, path: str) -> bool:
    accept = request.headers.get("accept", "").lower()
    return "xml" in accept or path.lower().endswith((".xml", "/serviceavailability"))


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
async def cloud_catch_all(path: str, request: Request, db: Session = Depends(get_db)) -> Response:
    url_path = "/" + path
    if url_path in {"/favicon.ico", "/robots.txt"}:
        return Response(status_code=204)
    raw_body = await request.body()
    headers = redact_mapping({
        "host": request.headers.get("host", ""),
        "user-agent": request.headers.get("user-agent", ""),
        "content-type": request.headers.get("content-type", ""),
        "authorization": request.headers.get("authorization", ""),
        "cookie": request.headers.get("cookie", ""),
    })
    remote = request.client.host if request.client else ""
    body_preview = redact_support_text(raw_body[:4096].decode("utf-8", errors="replace"))
    metadata = {
        "event": "unknown_cloud_request",
        "method": request.method,
        "path": url_path,
        "query": str(request.url.query),
        "headers": headers,
        "remote_addr": remote,
        "body_preview": body_preview,
    }
    unsupported_status = 501 if request.method in {"POST", "PUT", "DELETE"} else 404
    log_request(
        db,
        direction="in",
        service="cloud-catchall",
        method=request.method,
        path=url_path,
        host=str(headers.get("host") or ""),
        status_code=204 if request.method == "OPTIONS" else unsupported_status,
        body=json_dumps_safe(metadata),
    )
    write_masterlog("unknown_cloud_request", method=request.method, path=url_path, query=str(request.url.query), host=headers.get("host", ""), remote_addr=remote)
    request_device = resolve_request_device(db, request)
    ResearchStateRepository(db).record_event(
        device_id=request_device.device_id if request_device else None,
        domain="PROVIDER",
        code="UNSUPPORTED_CLOUD_CONTRACT",
        severity="WARNING",
        message="An unknown cloud contract was rejected without changing state.",
        evidence={
            "method": request.method,
            "path": url_path,
            "status_code": unsupported_status,
        },
    )
    db.commit()
    if request.method == "HEAD":
        return Response(status_code=404)
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Allow": "GET,POST,PUT,DELETE,HEAD,OPTIONS", "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,HEAD,OPTIONS"})
    problem = {
        "type": "https://basswiesn.local/problems/unsupported-cloud-contract",
        "title": "Unsupported cloud contract",
        "status": unsupported_status,
        "detail": "BASSWIESN does not implement this cloud request and did not change state.",
        "method": request.method,
        "path": url_path,
    }
    return JSONResponse(problem, status_code=unsupported_status, media_type="application/problem+json")


def json_dumps_safe(value: dict) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
