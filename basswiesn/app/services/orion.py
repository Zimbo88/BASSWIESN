import base64
import hashlib
import json
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings, is_safe_radio_host
from basswiesn.app.models import Setting
from basswiesn.app.services.stream_compat import analyze_stream_url


ORION_STATION_PATH = "/core02/svc-bmx-adapter-orion/prod/orion/station"
INFINITE_STREAMLIST_SIZE = 1000


class OrionLocationError(ValueError):
    pass


@dataclass(frozen=True)
class StationDescriptor:
    name: str
    stream_url: str
    image_url: str = ""
    tunein_id: str = ""
    stream_url_resolved: str = ""
    stream_format: str = ""
    stream_mime: str = ""
    compatibility_warning: str = ""


def encode_orion_data(descriptor: StationDescriptor) -> str:
    payload = {
        "name": descriptor.name,
        "streamUrl": descriptor.stream_url,
        "imageUrl": descriptor.image_url,
    }
    if descriptor.stream_url_resolved:
        payload["streamUrlResolved"] = descriptor.stream_url_resolved
    if descriptor.stream_format:
        payload["streamFormat"] = descriptor.stream_format
    if descriptor.stream_mime:
        payload["streamMime"] = descriptor.stream_mime
    if descriptor.compatibility_warning:
        payload["compatibilityWarning"] = descriptor.compatibility_warning
    if descriptor.tunein_id:
        payload["tuneinId"] = descriptor.tunein_id
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return quote(base64.urlsafe_b64encode(raw).decode("ascii"))


def decode_orion_data(data: str) -> dict[str, str]:
    decoded_data = unquote(data)
    padded = decoded_data + "=" * (-len(decoded_data) % 4)
    raw = base64.urlsafe_b64decode(padded)
    return json.loads(raw.decode("utf-8"))


def _host_from_request_header(value: str) -> str:
    host = (value or "").split(",", 1)[0].strip()
    if not host:
        return ""
    parsed = urlparse(f"http://{host}" if "://" not in host else host)
    return parsed.hostname or ""


def _saved_lan_host(db: Session | None) -> str:
    if db is None:
        return ""
    row = db.query(Setting).filter(Setting.key == "lan_host").one_or_none()
    return (row.value if row and row.value else "").strip()


def _host_from_base_url(value: str) -> str:
    return (urlparse(value or "").hostname or "").strip()


def _orion_base_url(db: Session | None = None, request_host: str = "") -> str:
    settings = get_settings()
    candidates = (
        # A safe request host is direct evidence for the currently reachable
        # BASSWIESN instance.  Explicit process configuration comes next.
        # A UI-saved host from an older LAN remains a fallback only.
        _host_from_request_header(request_host),
        settings.lan_host if settings.lan_host_configured else "",
        _host_from_base_url(settings.local_base_url) if settings.local_base_url_configured else "",
        _saved_lan_host(db),
        settings.lan_host,
        _host_from_base_url(settings.local_base_url),
    )
    for host in candidates:
        if host and is_safe_radio_host(host):
            return f"http://{host}:{settings.cloud_port}"
    raise OrionLocationError("BASSWIESN Host IP setzen: keine sichere LAN Host-IP fuer Orion-Playback erkannt.")


def station_location(descriptor: StationDescriptor, db: Session | None = None, request_host: str = "") -> str:
    base_url = _orion_base_url(db=db, request_host=request_host)
    return f"{base_url}{ORION_STATION_PATH}?data={encode_orion_data(descriptor)}"


def _is_playlist_url(url: str) -> bool:
    path = url.split("?", 1)[0].lower()
    return path.endswith((".m3u", ".m3u8", ".pls"))


def station_contract_key(descriptor: StationDescriptor) -> str:
    supplied = (descriptor.tunein_id or "").strip()
    if supplied:
        return supplied
    identity = f"{descriptor.name}\0{descriptor.stream_url}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:20]


def playback_response(
    descriptor: StationDescriptor, *, base_url: str | None = None
) -> dict:
    stream_url = descriptor.stream_url_resolved or descriptor.stream_url
    analysis = analyze_stream_url(descriptor.stream_url, descriptor.stream_mime, resolved_url=stream_url)
    has_playlist = _is_playlist_url(stream_url) or analysis.is_hls
    stream = {
        "streamUrl": stream_url,
        "hasPlaylist": has_playlist,
        "isRealtime": True,
        "maxTimeout": 60,
        "bufferingTimeout": 20,
        "connectingTimeout": 10,
        "contentType": descriptor.stream_mime or analysis.stream_mime,
        "codec": descriptor.stream_format or analysis.stream_codec,
        "_links": {},
    }
    provider_base = (base_url or get_settings().local_base_url).rstrip("/")
    station_key = quote(station_contract_key(descriptor), safe="")
    return {
        "name": descriptor.name,
        "imageUrl": descriptor.image_url,
        "streamType": "liveRadio",
        "isFavorite": False,
        "audio": {
            "streamUrl": stream_url,
            "hasPlaylist": has_playlist,
            "isRealtime": True,
            "maxTimeout": 60,
            "streams": [dict(stream) for _ in range(INFINITE_STREAMLIST_SIZE)],
        },
        "_links": {
            "bmx_nowplaying": {
                "href": f"{provider_base}/bmx/orion/now-playing/station/{station_key}",
                "useInternalClient": "ALWAYS",
            },
            "bmx_reporting": {
                "href": f"{provider_base}/bmx/orion/reporting/station/{station_key}"
            },
        },
        # BASSWIESN's local provider explicitly disables provider inactivity.
        # This is a server policy value, not a guessed Bose firmware default.
        "restrictions": {"inactivityTimeout": 0},
    }
