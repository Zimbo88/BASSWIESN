"""Positive automation contract, not GET-is-safe or admin-means-everything.

Other radio write paths require separate transitive safety review before being
added here. A scope never removes the target/backup/readback checks of a service.
"""
from collections.abc import Sequence

from fastapi.routing import APIRoute
from starlette.routing import BaseRoute
from starlette.types import Scope
from basswiesn.app.core.route_policy import selected_route

# (module leaf, function, exact route template, method) -> required scope
CONTRACTS = {
    ("system_api", "api_version", "/api/version", "GET"): "read",
    ("system_api", "health", "/api/health", "GET"): "read",
    ("api", "device_status_badges", "/api/devices/status-badges", "GET"): "read",
    ("api", "device_ui_capabilities", "/api/devices/ui-capabilities", "GET"): "read",
    ("stations_presets", "get_presets", "/api/presets/{device_id}", "GET"): "presets",
    ("multiroom", "multiroom_scenarios", "/api/multiroom/scenarios", "GET"): "multiroom",
    ("multiroom", "multiroom_methods", "/api/multiroom/methods", "GET"): "multiroom",
    ("api", "diagnostics_telemetry_summary", "/api/diagnostics/telemetry/summary", "GET"): "diagnostics",
    ("api", "set_device_setting", "/api/devices/{device_id}/settings/{setting}", "POST"): "playback",
    ("api", "apply_changed_device_settings", "/api/devices/{device_id}/settings-apply", "POST"): "admin",
    ("api", "rename_device", "/api/devices/{device_id}/rename", "POST"): "admin",
}


def required_scope(routes: Sequence[BaseRoute], scope: Scope) -> str | None:
    selected = selected_route(routes, scope)
    if selected is None or not isinstance(selected.route, APIRoute):
        return None
    endpoint = selected.route.endpoint
    module = endpoint.__module__
    if not module.startswith("basswiesn.app.routers."):
        return None
    grant = CONTRACTS.get((module.removeprefix("basswiesn.app.routers."), endpoint.__name__,
                           selected.path, scope.get("method", "")))
    # Volume is the sole reviewed playback write in this first contract.
    # Do not grant language/network/settings through a generic parameter.
    if grant == "playback" and selected.child.get("path_params", {}).get("setting") != "volume":
        return None
    return grant
