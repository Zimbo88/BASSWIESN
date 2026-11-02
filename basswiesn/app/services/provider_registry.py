"""Single source of truth for Bose-compatible provider metadata."""

from html import escape


RECOMMENDED_SOURCE_TYPES = (
    "AUX",
    "AIRPLAY",
    "ALEXA",
    "AMAZON",
    "BLUETOOTH",
    "DEEZER",
    "IHEART",
    "INTERNET_RADIO",
    "JUKE",
    "LOCAL_INTERNET_RADIO",
    "LOCAL_MUSIC",
    "NOTIFICATION",
    "PANDORA",
    "QPLAY",
    "RADIO_BROWSER",
    "SIRIUSXM",
    "SOUNDCLOUD",
    "SPOTIFY",
    "STORED_MUSIC",
    "STORED_MUSIC_MEDIA_RENDERER",
    "TUNEIN",
    "UPNP",
    "WBMX",
)

STREAM_SOURCE_PRIORITY = (
    "LOCAL_INTERNET_RADIO",
)

SOURCE_ID_ALIASES = {
    "10002": "INTERNET_RADIO",
    "10003": "LOCAL_INTERNET_RADIO",
    "10004": "TUNEIN",
    "10005": "RADIO_BROWSER",
    "10006": "WBMX",
}

SERVICE_MANIFEST = {
    "AUX": {"provider_id": 9, "auth_model": "local", "stream_types": ["audio"], "can_add": False, "can_remove": False, "adapter": "", "visible": False},
    "AIRPLAY": {"provider_id": 31, "auth_model": "local", "stream_types": ["audio"], "can_add": False, "can_remove": False, "adapter": "", "visible": False},
    "ALEXA": {"provider_id": 32, "auth_model": "oauth", "stream_types": ["audio"], "can_add": True, "can_remove": True, "adapter": "", "visible": False},
    "AMAZON": {"provider_id": 15, "auth_model": "oauth", "stream_types": ["onDemand"], "can_add": True, "can_remove": True, "adapter": "", "visible": False},
    "BLUETOOTH": {"provider_id": 33, "auth_model": "local", "stream_types": ["audio"], "can_add": False, "can_remove": False, "adapter": "", "visible": False},
    "DEEZER": {"provider_id": 12, "auth_model": "oauth", "stream_types": ["onDemand"], "can_add": True, "can_remove": True, "adapter": "", "visible": False},
    "IHEART": {"provider_id": 13, "auth_model": "bearer", "stream_types": ["liveRadio"], "can_add": True, "can_remove": True, "adapter": "", "visible": False},
    "INTERNET_RADIO": {"provider_id": 2, "auth_model": "token", "stream_types": ["liveRadio"], "can_add": False, "can_remove": False, "adapter": "tunein", "visible": False, "source_id": "10002", "source_visible": False, "credential": "", "contract_status": "UNSUPPORTED", "experimental": True},
    "JUKE": {"provider_id": 18, "auth_model": "oauth", "stream_types": ["onDemand"], "can_add": True, "can_remove": True, "adapter": "", "visible": False},
    "LOCAL_INTERNET_RADIO": {"provider_id": 11, "auth_model": "anonymous", "stream_types": ["mp3", "aac", "liveRadio"], "can_add": True, "can_remove": False, "adapter": "orion", "visible": True, "source_id": "10003", "source_visible": True, "credential": "eyJzZXJpYWwiOiJsb2NhbC1pbnRlcm5ldC1yYWRpbyJ9", "contract_status": "CONFIRMED", "experimental": False},
    "LOCAL_MUSIC": {"provider_id": 34, "auth_model": "local", "stream_types": ["audio"], "can_add": False, "can_remove": False, "adapter": "", "visible": False},
    "NOTIFICATION": {"provider_id": 35, "auth_model": "local", "stream_types": ["audio"], "can_add": False, "can_remove": False, "adapter": "", "visible": False},
    "PANDORA": {"provider_id": 1, "auth_model": "credentials", "stream_types": ["liveRadio"], "can_add": True, "can_remove": True, "adapter": "", "visible": False},
    "QPLAY": {"provider_id": 36, "auth_model": "local", "stream_types": ["audio"], "can_add": False, "can_remove": False, "adapter": "", "visible": False},
    "RADIO_BROWSER": {"provider_id": 39, "auth_model": "anonymous", "stream_types": ["mp3", "aac", "liveRadio"], "can_add": False, "can_remove": False, "adapter": "radiobrowser", "visible": False, "source_id": "10005", "source_visible": False, "credential": "", "contract_status": "UNSUPPORTED", "experimental": True},
    "SIRIUSXM": {"provider_id": 16, "auth_model": "oauth", "stream_types": ["liveRadio"], "can_add": True, "can_remove": True, "adapter": "", "visible": False},
    "SOUNDCLOUD": {"provider_id": 17, "auth_model": "oauth", "stream_types": ["onDemand"], "can_add": True, "can_remove": True, "adapter": "", "visible": False},
    "SPOTIFY": {"provider_id": 14, "auth_model": "oauth", "stream_types": ["onDemand"], "can_add": True, "can_remove": True, "adapter": "", "visible": False},
    "STORED_MUSIC": {"provider_id": 37, "auth_model": "local", "stream_types": ["audio"], "can_add": False, "can_remove": False, "adapter": "", "visible": False},
    "STORED_MUSIC_MEDIA_RENDERER": {"provider_id": 38, "auth_model": "local", "stream_types": ["audio"], "can_add": False, "can_remove": False, "adapter": "", "visible": False},
    "TUNEIN": {"provider_id": 25, "auth_model": "anonymous", "stream_types": ["liveRadio", "onDemand"], "can_add": False, "can_remove": False, "adapter": "tunein", "visible": False, "source_id": "10004", "source_visible": False, "credential": "", "contract_status": "UNSUPPORTED", "experimental": True},
    "UPNP": {"provider_id": 40, "auth_model": "local", "stream_types": ["audio"], "can_add": False, "can_remove": False, "adapter": "", "visible": False},
    "WBMX": {"provider_id": 19, "auth_model": "oauth", "stream_types": ["liveRadio"], "can_add": False, "can_remove": False, "adapter": "orion", "visible": False, "source_id": "10006", "source_visible": False, "credential": "", "contract_status": "UNSUPPORTED", "experimental": True},
}


def provider(name: str) -> dict:
    return SERVICE_MANIFEST.get(
        name,
        {
            "provider_id": 0,
            "auth_model": "unsupported",
            "stream_types": [],
            "can_add": False,
            "can_remove": False,
            "adapter": "",
            "visible": False,
            "source_visible": False,
            "contract_status": "UNSUPPORTED",
            "experimental": False,
        },
    )


def normalize_source_name(value: str | None, *, fallback: str = "LOCAL_INTERNET_RADIO") -> str:
    source = str(value or "").strip().upper().replace(" ", "_")
    if not source:
        return fallback
    return SOURCE_ID_ALIASES.get(source, source)


def provider_rows() -> list[dict]:
    return [{"name": name, **config} for name, config in SERVICE_MANIFEST.items()]


def persistence_sources_xml(source_types: tuple[str, ...] = RECOMMENDED_SOURCE_TYPES) -> str:
    rows = []
    for source in source_types:
        account = "AUX" if source == "AUX" else ""
        secret_type = "" if source == "AUX" else "token"
        rows.append(
            f'  <source displayName="{escape(source, quote=True)}" secret="" secretType="{secret_type}">'
            f'<sourceKey type="{escape(source, quote=True)}" account="{escape(account, quote=True)}" />'
            "</source>"
        )
    return '<?xml version="1.0" encoding="UTF-8" ?>\n<sources>\n' + "\n".join(rows) + "\n</sources>\n"
