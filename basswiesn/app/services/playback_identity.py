from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from urllib.parse import parse_qs, urlparse

from sqlalchemy.orm import Session

from basswiesn.app.models import PlayHistory, Preset, Station
from basswiesn.app.services.orion import decode_orion_data


UNKNOWN_STATION = "Unbekannter Sender"
BAD_NAMES = {"", "unknown", "unbekannt", "unbekannter sender", "local_internet_radio", "internet_radio"}


@dataclass(frozen=True)
class PlaybackIdentity:
    station_display_name: str
    station_id: int | None
    identity_source: str
    identity_confidence: int
    source_display_name: str
    stream_host: str
    is_internal: bool
    is_confirmed: bool
    station_name_normalized: str

    def to_dict(self) -> dict:
        return {
            "station_display_name": self.station_display_name,
            "station_id": self.station_id,
            "identity_source": self.identity_source,
            "identity_confidence": self.identity_confidence,
            "source_display_name": self.source_display_name,
            "stream_host": self.stream_host,
            "is_internal": self.is_internal,
            "is_confirmed": self.is_confirmed,
            "station_name_normalized": self.station_name_normalized,
        }


def normalize_station_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text.casefold()


def clean_station_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if normalize_station_name(text) in BAD_NAMES:
        return ""
    if text.startswith(("http://", "https://")):
        return ""
    return text[:255]


def stream_host(value: str) -> str:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host[:255]


def canonical_stream_id(value: str) -> str:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return ""
    host = stream_host(value)
    path = re.sub(r"/+", "/", parsed.path or "/")
    return f"{host}{path}".strip("/")[:512]


def _orion_descriptor_from_url(value: str) -> dict[str, str]:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return {}
    data = parse_qs(parsed.query or "").get("data", [""])[0]
    if not data:
        return {}
    try:
        decoded = decode_orion_data(data)
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _station_stream_keys(station: Station) -> set[str]:
    keys: set[str] = set()
    for value in (station.stream_url, station.stream_url_original, station.stream_url_resolved):
        key = canonical_stream_id(value)
        if key:
            keys.add(key)
    return keys


def _match_station_by_stream(db: Session, value: str) -> Station | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    exact = (
        db.query(Station)
        .filter(
            (Station.stream_url == raw)
            | (Station.stream_url_original == raw)
            | (Station.stream_url_resolved == raw)
            | (Station.provider_station_id == raw)
        )
        .order_by(Station.id)
        .first()
    )
    if exact is not None:
        return exact
    descriptor = _orion_descriptor_from_url(raw)
    candidates = [
        raw,
        descriptor.get("streamUrl", ""),
        descriptor.get("streamUrlResolved", ""),
        descriptor.get("tuneinId", ""),
    ]
    provider_id = clean_station_name(descriptor.get("tuneinId", "")) or raw
    if provider_id:
        station = db.query(Station).filter(Station.provider_station_id == provider_id).order_by(Station.id).first()
        if station is not None:
            return station
    wanted = {canonical_stream_id(item) for item in candidates if item}
    wanted.discard("")
    if not wanted:
        return None
    for station in db.query(Station).order_by(Station.id).all():
        if _station_stream_keys(station) & wanted:
            return station
    return None


def _station_candidate_from_indirect_fields(
    db: Session,
    *,
    stream_url: str,
    source_account: str,
) -> tuple[str, int | None, str, int]:
    for label, value in (("station_stream_url", stream_url), ("station_source_account", source_account)):
        station = _match_station_by_stream(db, value)
        if station and clean_station_name(station.name):
            return clean_station_name(station.name), station.id, label, 94
    descriptor = _orion_descriptor_from_url(stream_url) or _orion_descriptor_from_url(source_account)
    descriptor_name = clean_station_name(str(descriptor.get("name", "")))
    if descriptor_name:
        return descriptor_name, None, "orion_descriptor", 84
    return "", None, "", 0


SOURCE_LABELS = {
    "LOCAL_INTERNET_RADIO": "Lokales Internetradio",
    "INTERNET_RADIO": "Internetradio",
    "RADIO_BROWSER": "Radio Browser",
    "TUNEIN": "TuneIn",
    "PRESET": "Preset",
    "AUX": "AUX",
    "STANDBY": "Standby",
}


def source_display_name(source: str) -> str:
    key = str(source or "").strip().upper()
    return SOURCE_LABELS.get(key, key.replace("_", " ").title() if key else "")


def _preset_identity(db: Session, *, device_id: str, preset_button: int | None) -> tuple[str, int | None, str, int]:
    if not device_id or not preset_button:
        return "", None, "", 0
    preset = db.query(Preset).filter(Preset.device_id == device_id, Preset.button == preset_button).one_or_none()
    if preset is None:
        return "", None, "", 0
    if preset.station_id:
        station = db.query(Station).filter(Station.id == preset.station_id).one_or_none()
        if station and clean_station_name(station.name):
            return clean_station_name(station.name), station.id, "preset_station", 88
    name_match = re.search(r"<itemName>(.*?)</itemName>", preset.content_item_xml or "", re.IGNORECASE | re.DOTALL)
    name = clean_station_name(re.sub(r"<[^>]+>", "", name_match.group(1)) if name_match else "")
    return name, preset.station_id, "preset_content_item", 72 if name else 0


def resolve_playback_identity(
    db: Session,
    *,
    station_id: int | None = None,
    station_name: str = "",
    stream_url: str = "",
    source: str = "",
    source_account: str = "",
    content_item_name: str = "",
    device_id: str = "",
    preset_button: int | None = None,
    now_playing: dict | None = None,
    internal_event: bool = False,
    is_confirmed: bool = True,
) -> PlaybackIdentity:
    now_playing = now_playing or {}
    station = db.query(Station).filter(Station.id == station_id).one_or_none() if station_id else None
    candidates: list[tuple[str, int | None, str, int]] = []
    if station and clean_station_name(station.name):
        candidates.append((clean_station_name(station.name), station.id, "station", 100))
    indirect_name, indirect_station_id, indirect_source, indirect_confidence = _station_candidate_from_indirect_fields(
        db,
        stream_url=stream_url,
        source_account=source_account,
    )
    if indirect_name:
        candidates.append((indirect_name, indirect_station_id or station_id, indirect_source, indirect_confidence))
    if clean_station_name(station_name):
        candidates.append((clean_station_name(station_name), station_id, "snapshot", 92))
    preset_name, preset_station_id, preset_source, preset_confidence = _preset_identity(db, device_id=device_id, preset_button=preset_button)
    if preset_name:
        candidates.append((preset_name, preset_station_id or station_id, preset_source, preset_confidence))
    if clean_station_name(content_item_name):
        candidates.append((clean_station_name(content_item_name), station_id, "content_item", 70))
    for key, confidence in (("stationName", 65), ("station_name", 65), ("sourceTitle", 58), ("itemName", 55)):
        value = now_playing.get(key)
        if clean_station_name(str(value or "")):
            candidates.append((clean_station_name(str(value)), station_id, key, confidence))
    if not candidates:
        host = stream_host(stream_url)
        if host:
            candidates.append((host, station_id, "stream_host", 30))
    if not candidates:
        candidates.append((UNKNOWN_STATION, station_id, "unknown", 0))
    display_name, resolved_station_id, identity_source, confidence = candidates[0]
    source_key = str(source or "").upper()
    internal = bool(
        internal_event
        or (station and getattr(station, "internal", False))
        or "activation" in normalize_station_name(display_name)
        or source_key in {"STANDBY", "INVALID_SOURCE", "KEEPALIVE_INTERNAL", "SETUP_ACTIVATION"}
    )
    return PlaybackIdentity(
        station_display_name=display_name,
        station_id=resolved_station_id,
        identity_source=identity_source,
        identity_confidence=confidence,
        source_display_name=source_display_name(source),
        stream_host=stream_host(stream_url),
        is_internal=internal,
        is_confirmed=bool(is_confirmed),
        station_name_normalized=normalize_station_name(display_name),
    )


def apply_identity(row: PlayHistory, identity: PlaybackIdentity) -> None:
    row.station_id = identity.station_id
    row.station_display_name = identity.station_display_name
    row.station_name_normalized = identity.station_name_normalized
    row.identity_source = identity.identity_source
    row.identity_confidence = identity.identity_confidence
    row.source_display_name = identity.source_display_name
    row.stream_host = identity.stream_host
    row.is_internal = identity.is_internal
    row.is_confirmed = identity.is_confirmed
    if not row.station_name and identity.identity_source != "stream_host":
        row.station_name = identity.station_display_name


def identity_for_history(db: Session, row: PlayHistory) -> PlaybackIdentity:
    snapshot_name = getattr(row, "station_display_name", "")
    if not clean_station_name(snapshot_name):
        snapshot_name = getattr(row, "station_name", "")
    return resolve_playback_identity(
        db,
        station_id=getattr(row, "station_id", None),
        station_name=snapshot_name,
        stream_url=getattr(row, "stream_url", ""),
        source=getattr(row, "source", ""),
        source_account=getattr(row, "source_account", ""),
        content_item_name=getattr(row, "content_item_name", "") or getattr(row, "preset_name", ""),
        device_id=getattr(row, "device_id", ""),
        preset_button=getattr(row, "preset_button", None),
        internal_event=bool(getattr(row, "internal_event", False) or getattr(row, "is_internal", False)),
        is_confirmed=bool(getattr(row, "is_confirmed", True)),
    )
