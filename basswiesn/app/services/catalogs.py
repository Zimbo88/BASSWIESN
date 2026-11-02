SETTINGS_CATALOG = [
    {"endpoint": "/name", "area": "device", "status": "implemented", "note": "Rename with Stockholm-safe validation"},
    {"endpoint": "/volume", "area": "device", "status": "implemented", "note": "0..100"},
    {"endpoint": "/bass", "area": "device", "status": "implemented", "note": "Stockholm range -9..0"},
    {"endpoint": "/clockDisplay", "area": "device", "status": "implemented", "note": "clockConfig userEnable"},
    {"endpoint": "/language", "area": "device", "status": "implemented", "note": "Stockholm language list"},
    {"endpoint": "/systemtimeout", "area": "device", "status": "implemented", "note": "timeout and powersaving payloads"},
    {"endpoint": "/rebroadcastlatencymode", "area": "multiroom", "status": "implemented", "note": "SYNC_TO_ZONE / SYNC_TO_ROOM"},
    {"endpoint": "/netStats", "area": "telemetry", "status": "implemented-read-only", "note": "Live telemetry read"},
    {"endpoint": "/networkInfo", "area": "telemetry", "status": "implemented-read-only", "note": "Live network information read"},
    {"endpoint": "/now_playing", "area": "playback", "status": "implemented-confirmed", "note": "Live playback state"},
    {"endpoint": "/sources", "area": "source", "status": "implemented-confirmed", "note": "Live source state including multiroomallowed"},
    {"endpoint": "/searchStation", "area": "station", "status": "implemented-guarded", "note": "Native radio search via /api/devices/{id}/station/search-native; Preview only by default"},
    {"endpoint": "/addStation", "area": "station", "status": "implemented-guarded", "note": "Native add/play station via /api/devices/{id}/station/add-native; may start playback"},
    {"endpoint": "/nameSource", "area": "source", "status": "implemented-plan", "note": "Plan-only XML builder in Device Settings until model-safe write is captured"},
    {"endpoint": "/bassCapabilities", "area": "device", "status": "implemented-read-only", "note": "Read-only probe available in Device Settings"},
    {"endpoint": "/powerManagement", "area": "device", "status": "removed-1.5.0", "note": "Batterieabfragen werden von BASSWIESN 1.5.0 nicht mehr aktiv ausgeführt."},
    {"endpoint": "/standby", "area": "power", "status": "implemented-confirmed", "note": "Action-style GET; live-confirmed on Portable, explicit confirmation required"},
    {"endpoint": "/lowPowerStandby", "area": "power", "status": "implemented-guarded", "note": "Action-style GET; can drop network and require hardware wake, explicit confirmation required"},
    {"endpoint": "/getZone + /setZone", "area": "multiroom", "status": "implemented-confirmed", "note": "Verified local zone creation, readback and clear"},
    {"endpoint": "/factoryDefault", "area": "recovery", "status": "retired-1.6.0", "note": "Inventory only: BASSWIESN 1.6.0 exposes no executable Factory Reset path; retired recovery actions return HTTP 410."},
]


TELNET_COMMANDS = [
    {"key": "get_current_config", "label": "Read CurrentSystemConfiguration", "command": "getpdo CurrentSystemConfiguration", "mode": "read-only", "note": "Useful before any URL/config rewrite."},
    {"key": "read_persistence", "label": "List persistence store", "command": "ls -la /mnt/nv; find /mnt/nv -maxdepth 3 -type f 2>/dev/null", "mode": "read-only", "note": "Old-shell safe inventory before backup."},
    {"key": "read_bose_urls", "label": "Read Bose URL layer", "command": "envswitch boseurls get 2>/dev/null || true; getpdo CurrentSystemConfiguration 2>/dev/null || true", "mode": "read-only", "note": "Shows cloud/marge/BMX routing when available."},
    {"key": "sys_reboot", "label": "Reboot", "command": "sys reboot", "mode": "danger", "note": "Manual execution requires a validated firmware profile and the BASSWIESN TELNET REBOOT confirmation phrase."},
    {"key": "set_bmx_registry", "label": "Set BMX registry candidate", "command": "sys configuration bmxRegistryUrl http://<BASSWIESN-LAN-IP>:1516/bmx/registry/v1/services", "mode": "write-plan", "note": "Research command. Normal setup should prefer confirmed config rewrite path."},
]

MEDIA_LIBRARY_CAPABILITIES = {
    "status": "research-backed-plan",
    "soundtouch_endpoints": ["/listMediaServers", "/selectLocalSource", "/trackInfo", "/stationInfo", "/playbackRequest", "/sources"],
    "source_tokens": ["LOCAL_MUSIC", "stored_music", "local_music", "RMS_UUID"],
    "preset_rule": "DLNA/NAS entries must store source identity plus content item metadata; do not treat them as simple stream URLs.",
    "safe_first_step": "Probe /listMediaServers, then capture the exact XML for server UUID, container/item id and playback source before enabling preset write.",
}

SERVICE_CATALOG = [
    {"key": "tunein", "label": "TuneIn / Internet Radio", "status": "partly implemented", "path": "LOCAL_INTERNET_RADIO + local BMX/TuneIn facade", "note": "Best candidate for old behavior. Needs station/provider metadata and model tests for presets."},
    {"key": "spotify", "label": "Spotify", "status": "firmware-present research", "path": "native Spotify Connect module or external bridge", "note": "Firmware references exist, but native auth/service flow is likely cloud/licence dependent. Practical fallback is bridge to a supported local source."},
    {"key": "dlna", "label": "DLNA / NAS", "status": "planned", "path": "/listMediaServers and local_music/stored_music source runtime", "note": "Good fit for NAS playlists, kids stories and local collections after XML captures are validated."},
    {"key": "lms_upnp", "label": "LMS / UPnP Player", "status": "planned", "path": "Logitech Media Server UPnP/DLNA bridge -> SoundTouch UPNP/STORED_MUSIC", "note": "Feasible if LMS exposes a UPnP/DLNA server or bridge. Preset support needs captured selectLocalSource/playbackRequest XML."},
    {"key": "tidal", "label": "TIDAL", "status": "bridge only unless firmware/service API found", "path": "external streamer -> HTTP/DLNA/AirPlay/Bluetooth", "note": "Direct native integration usually needs authenticated licensed service APIs."},
    {"key": "apple_music", "label": "Apple Music", "status": "bridge only", "path": "AirPlay/Bluetooth/external streamer", "note": "DRM/native account playback cannot be emulated as a plain SoundTouch preset."},
]

STEREO_PAIRING_RESEARCH = {
    "status": "firmware-confirmed",
    "model_matrix": {"ST10/rhino": "supported: capability=true plus left/right DSP images", "ST20/spotty": "disabled: capability=false and no L/R DSP images", "ST30/taigan": "disabled in FW 27.0.6: no L/R DSP assets"},
    "candidate_endpoints": ["/addGroup", "/removeGroup", "/getGroup", "/updateGroup", "/addZoneSlave", "/removeZoneSlave"],
    "rule": "Do not expose stereo pairing as normal setup until model-specific ST10 capability XML proves it and rollback is tested.",
}

RADIO_LOG_HTTP_ENDPOINTS = [
    "/info",
    "/supportedURLs",
    "/capabilities",
    "/sources",
    "/presets",
    "/now_playing",
    "/nowPlaying",
    "/volume",
    "/bass",
    "/bassCapabilities",
    "/clockDisplay",
    "/clockTime",
    "/language",
    "/systemtimeout",
    "/getZone",
    "/networkInfo",
    "/netStats",
    "/bluetoothInfo",
    "/listMediaServers",
    "/recents",
    "/serviceAvailability",
    "/soundTouchConfigurationStatus",
    "/speaker",
    "/swUpdateQuery",
]

# These endpoints are real but are not safe generic GET probes: /getGroup can
# block on an ungrouped Portable, while introspect/sourceDiscoveryStatus need a
# request shape. Keep them visible without stalling routine captures.
RADIO_LOG_GUARDED_HTTP_ENDPOINTS = ["/getGroup", "/introspect", "/sourceDiscoveryStatus"]

RADIO_LOG_CLI17000_COMMANDS = [
    "getpdo CurrentSystemConfiguration",
    "envswitch boseurls get",
    "sys configuration",
]

RADIO_LOG_SSH_PLAN = [
    "date; hostname; uptime",
    "logread | tail -300",
    "logread | grep -i -E 'marge|boseurls|remote_services|sshd|factory|reset|zone|stereo|account|uuid|error|warning' | tail -300 || true",
    "dmesg | tail -200",
    "ps aux 2>/dev/null || ps",
    "netstat -lntp 2>/dev/null || netstat -lnt 2>/dev/null || true",
    "ifconfig 2>/dev/null || ip addr 2>/dev/null || true",
    "mount",
    "cat /proc/net/wireless 2>/dev/null || true",
    "ls -lah /mnt/nv 2>/dev/null || true",
    "find /mnt/nv /opt/Bose /etc -maxdepth 5 -type f -iname '*.xml' 2>/dev/null | sort || true",
    "find /tmp /mnt/nv \\( -type f -name '*.log' -o -type f -name '*log*' \\) 2>/dev/null | sort",
    "tail -120 /tmp/*.log /mnt/nv/*.log /mnt/nv/*/*.log 2>/dev/null || true",
]


KEY_COMMANDS = [
    {"key": "POWER", "label": "Power"},
    {"key": "PLAY", "label": "Play"},
    {"key": "PAUSE", "label": "Pause"},
    {"key": "PLAY_PAUSE", "label": "Play/Pause"},
    {"key": "STOP", "label": "Stop"},
    {"key": "PREV_TRACK", "label": "Previous"},
    {"key": "NEXT_TRACK", "label": "Next"},
    {"key": "VOLUME_UP", "label": "Volume up"},
    {"key": "VOLUME_DOWN", "label": "Volume down"},
    {"key": "MUTE", "label": "Mute"},
    {"key": "PRESET_1", "label": "Preset 1"},
    {"key": "PRESET_2", "label": "Preset 2"},
    {"key": "PRESET_3", "label": "Preset 3"},
    {"key": "PRESET_4", "label": "Preset 4"},
    {"key": "PRESET_5", "label": "Preset 5"},
    {"key": "PRESET_6", "label": "Preset 6"},
    {"key": "AUX_INPUT", "label": "AUX"},
    {"key": "SHUFFLE_OFF", "label": "Shuffle off"},
    {"key": "SHUFFLE_ON", "label": "Shuffle on"},
    {"key": "REPEAT_OFF", "label": "Repeat off"},
    {"key": "REPEAT_ONE", "label": "Repeat one"},
    {"key": "REPEAT_ALL", "label": "Repeat all"},
]

DISPLAY_METADATA_MODES = [
    {"key": "off", "label": "Off", "fields": [], "writes_radio": False},
    {"key": "station", "label": "Radio metadata", "fields": ["station", "artist", "track"], "writes_radio": False},
    {"key": "station_clock", "label": "Radio metadata + clock", "fields": ["station", "artist", "track", "clock"], "writes_radio": False},
    {"key": "station_clock_wifi", "label": "Radio metadata + clock + WiFi", "fields": ["station", "artist", "track", "clock", "wifi"], "writes_radio": False},
    {"key": "clock_wifi", "label": "Clock + WiFi", "fields": ["clock", "wifi"], "writes_radio": False},
]

SUPPORTED_MEDIA_TYPES = [
    {"key": "direct_mp3", "label": "Direct MP3 stream", "status": "confirmed", "extensions": [".mp3"], "mime_types": ["audio/mpeg", "audio/mp3"], "note": "Confirmed by Mathias captures: LOCAL_INTERNET_RADIO stationurl with direct .mp3 URLs."},
    {"key": "http_live_radio", "label": "HTTP/HTTPS live radio URL", "status": "confirmed", "extensions": [], "mime_types": ["audio/*"], "note": "LOCAL_INTERNET_RADIO introspect exposes streamTypes=liveRadio; exact codec still depends on radio firmware."},
    {"key": "playlist_m3u_pls", "label": "M3U/PLS playlist URL", "status": "candidate", "extensions": [".m3u", ".m3u8", ".pls"], "mime_types": ["audio/x-mpegurl", "application/pls+xml"], "note": "Often used by radio stations, but must be resolved/tested before writing a preset."},
    {"key": "aac", "label": "AAC/AAC+ stream", "status": "candidate", "extensions": [".aac"], "mime_types": ["audio/aac", "audio/aacp"], "note": "Candidate only until model-specific playback is live-tested."},
    {"key": "local_file", "label": "Local file served by basswiesn", "status": "limited", "extensions": [".mp3"], "mime_types": ["audio/mpeg"], "note": "Use MP3 first. Old SoundTouch firmware is safest with simple HTTP MP3, not modern codecs."},
    {"key": "flac_ogg_opus_wma", "label": "FLAC/OGG/OPUS/WMA", "status": "blocked", "extensions": [".flac", ".ogg", ".opus", ".wma"], "mime_types": [], "note": "Do not offer for presets until firmware/runtime tests prove support."},
]

TIME_ZONES = ["UTC", "Europe/Berlin", "Europe/Vienna", "Europe/Zurich", "Europe/London", "Europe/Paris", "Europe/Rome", "Europe/Madrid", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "Asia/Tokyo", "Australia/Sydney"]
WEBGUI_LANGUAGES = [
    {"code": "en", "label": "English"},
    {"code": "de", "label": "Deutsch"},
    {"code": "fr", "label": "Français"},
    {"code": "es", "label": "Español"},
    {"code": "it", "label": "Italiano"},
]

STOCKHOLM_LANGUAGES = [
    {"code": "cs", "label": "Czech"},
    {"code": "da", "label": "Danish"},
    {"code": "de", "label": "Deutsch"},
    {"code": "el", "label": "Greek"},
    {"code": "en", "label": "English"},
    {"code": "es", "label": "Español"},
    {"code": "fi", "label": "Finnish"},
    {"code": "fr", "label": "Français"},
    {"code": "hu", "label": "Hungarian"},
    {"code": "it", "label": "Italiano"},
    {"code": "ja", "label": "Japanese"},
    {"code": "ko", "label": "Korean"},
    {"code": "nb", "label": "Norwegian Bokmål"},
    {"code": "nl", "label": "Nederlands"},
    {"code": "no", "label": "Norwegian"},
    {"code": "pl", "label": "Polish"},
    {"code": "pt", "label": "Português"},
    {"code": "ro", "label": "Romanian"},
    {"code": "ru", "label": "Russian"},
    {"code": "sl", "label": "Slovenian"},
    {"code": "sv", "label": "Swedish"},
    {"code": "th", "label": "Thai"},
    {"code": "tr", "label": "Turkish"},
    {"code": "zh_hans", "label": "Chinese Simplified"},
    {"code": "zh_hant", "label": "Chinese Traditional"},
]
