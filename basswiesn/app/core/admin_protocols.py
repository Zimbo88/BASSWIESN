"""Reviewed device entry points: new routes are never anonymous by default."""

from collections.abc import Sequence

from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

# These are protocol handlers, not every handler in a module called "cloud".
# In particular cloud_stations, cloud_about, cloud_root and the catch-all are
# intentionally absent. A new device contract requires an explicit review here.
DEVICE_HANDLERS = frozenset({
    "streaming_sourceproviders", "provider_discovery", "streaming_power_on",
    "streaming_token", "device_blacklist", "streaming_account_full",
    "streaming_provider_settings", "service_settings", "station_info",
    "set_music_service_account", "set_music_service_oauth_account", "group_state",
    "group_create", "group_update", "group_delete", "streaming_add_device",
    "streaming_get_device", "streaming_put_device", "streaming_delete_device",
    "streaming_device_keepalive", "streaming_account_sources", "streaming_device_presets",
    "streaming_device_recents", "streaming_device_recent", "streaming_account_presets",
    "orion_token", "bmx_registry", "bmx_services_availability", "orion_service",
    "orion_station", "now_playing", "reporting", "bmx_tunein_station", "bmx_tunein_token",
    "bmx_tunein_navigate", "bmx_radiobrowser_station", "bmx_resolve", "marge_presets",
    "marge_sources", "marge_full", "put_preset", "remove_preset",
})

# Handler reuse is not sufficient: a future administrative alias of the same
# function must not silently become anonymous. Methods are checked as well.
DEVICE_CONTRACTS = {
    "/streaming/sourceproviders": "GET",
    "/streaming/provider-discovery": "GET",
    "/bmx/registry/v1/introspect": "GET",
    "/streaming/support/power_on": "POST",
    "/streaming/device/{device_id}/streaming_token": "GET",
    "/v1/blacklist/{device_id}": "GET POST",
    "/streaming/account/{account_id}/full": "GET",
    "/streaming/account/{account_id}/provider_settings": "GET",
    "/serviceSettings": "GET POST",
    "/getServiceSettings": "GET POST",
    "/stationInfo": "GET",
    "/setMusicServiceAccount": "POST",
    "/setMusicServiceOAuthAccount": "POST",
    "/group": "GET",
    "/group/create": "POST",
    "/group/update": "POST",
    "/group/delete": "POST",
    "/streaming/account/{account_id}/device/": "POST",
    "/streaming/account/{account_id}/device/{device_id}": "GET PUT DELETE",
    "/streaming/account/{account_id}/device/{device_id}/heartbeat": "POST",
    "/streaming/account/{account_id}/device/{device_id}/keepalive": "POST",
    "/streaming/account/{account_id}/sources": "GET",
    "/streaming/account/{account_id}/device/{device_id}/presets": "GET",
    "/streaming/account/{account_id}/device/{device_id}/recents": "GET",
    "/streaming/account/{account_id}/device/{device_id}/recent": "POST",
    "/streaming/account/{account_id}/presets/all": "GET",
    "/core02/svc-bmx-adapter-orion/prod/orion/token": "POST",
    "/bmx/registry/v1/services": "GET",
    "/bmx/registry/v1/servicesAvailability": "GET",
    "/bmx/registry/servicesAvailability": "GET",
    "/core02/svc-bmx-adapter-orion/prod/orion": "GET",
    "/core02/svc-bmx-adapter-orion/prod/orion/station": "GET",
    "/bmx/orion/now-playing": "GET",
    "/bmx/orion/now-playing/station/{station_id}": "GET",
    "/bmx/orion/reporting": "POST",
    "/bmx/orion/reporting/station/{station_id}": "POST",
    "/bmx/tunein/v1/playback/station/{station_id}": "GET",
    "/bmx/tunein/v1/now-playing/station/{station_id}": "GET",
    "/bmx/tunein/v1/reporting/station/{station_id}": "POST",
    "/bmx/tunein/v1/favorite/{station_id}": "GET POST",
    "/bmx/tunein/v1/token": "GET POST",
    "/bmx/tunein/v1/navigate": "GET",
    "/bmx/tunein/v1/navigate/{path:path}": "GET",
    "/bmx/radiobrowser/v1/playback/station/{uuid}": "GET",
    "/bmx/radiobrowser/v1/now-playing/station/{uuid}": "GET",
    "/bmx/radiobrowser/v1/reporting/station/{uuid}": "POST",
    "/bmx/resolve": "GET POST",
    "/v1/systems/devices/{device_id}/presets": "GET",
    "/v1/systems/devices/{device_id}/sources": "GET",
    "/v1/systems/devices/{device_id}": "GET",
    "/streaming/account/{account_id}/device/{device_id}/preset/{button}": "PUT DELETE",
    "/streaming/account/{account_id}/device/{device_id}/presets/{button}": "PUT POST DELETE",
}


def device_routes(routes: Sequence[BaseRoute]) -> list[BaseRoute]:
    return [route for route in routes if isinstance(route, APIRoute)
            and route.endpoint.__module__ == "basswiesn.app.routers.cloud"
            and route.endpoint.__name__ in DEVICE_HANDLERS
            and route.path in DEVICE_CONTRACTS
            and route.methods is not None
            and route.methods <= set(DEVICE_CONTRACTS[route.path].split())]
