from __future__ import annotations

from basswiesn.app.config import get_settings
from basswiesn.app.models import Setting


def dlna_status(db=None) -> dict:
    setting = db.query(Setting).filter(Setting.key == "dlna_enabled").one_or_none() if db is not None else None
    enabled = setting.value == "true" if setting else get_settings().experimental_dlna
    return {
        "enabled": enabled,
        "experimental": True,
        # The flag is configuration, not proof of an implemented protocol path.
        "implementation_status": "EXPLICIT_CONTENT_DIRECTORY",
        "ready": enabled,
        "hardware_verified": False,
        "background_services_started": False,
        "renderer_discovery": "not_supported",
        "server_connection": "explicit_description_url",
        "content_browse": "implemented",
        "media_relay": "same_origin_mp3_aac",
        "renderer_control": "existing_soundtouch_station_controls",
        "transcoding": "not_implemented",
        "limitations": [
            "Only explicitly selected HTTP ContentDirectory servers, without authentication, are supported.",
            "MP3/AAC relay and browsing do not prove audible playback on every radio model.",
            "No multicast discovery, generic UPnP renderer control, transcoding or background scan.",
        ],
    }


async def discover_renderers(db=None) -> dict:
    status = dlna_status(db)
    if not status["enabled"]:
        return {**status, "renderers": [], "skipped": True, "reason_code": "FEATURE_DISABLED", "reason": "Media library is disabled."}
    return {**status, "renderers": [], "skipped": True, "reason_code": "EXPLICIT_SERVER_REQUIRED", "reason": "Connect an explicit media-server description URL; no network scan was made."}
