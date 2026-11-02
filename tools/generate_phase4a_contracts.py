#!/usr/bin/env python3
"""Generate deterministic Phase 4A inventories using static source analysis.

The tool never imports the application and never opens a network connection.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from typing import Any

APP_ROOT = Path("basswiesn/app")
TEST_ROOT = Path("tests")
ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "api_route", "websocket"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
APPLICATIONS = {
    "webgui": {"title": "basswiesn WebGUI", "port": 1328, "enabled_by_default": True},
    "cloud": {"title": "basswiesn Cloud Emulator", "port": 1516, "enabled_by_default": True},
    "diagnostics": {"title": "basswiesn Diagnostics", "port": 1860, "enabled_by_default": True},
    "https-webgui": {
        "title": "basswiesn WebGUI over optional HTTPS",
        "port": 1329,
        "enabled_by_default": False,
        "enable_setting": "BASSWIESN_ENABLE_HTTPS",
    },
}
ENV_RE = re.compile(r"\b(?:BASSWIESN_[A-Z0-9_]+|MUSIC_ASSISTANT_[A-Z0-9_]+|PROTECTED_DEVICE_IPS|PROTECTED_DEVICE_IDS|LC_ALL|LC_MESSAGES|LANG)\b")
NETWORK_NAMES = {
    "AsyncClient", "Client", "create_connection", "create_datagram_endpoint",
    "open_connection", "getaddrinfo", "urlopen", "run", "Popen",
    "create_subprocess_exec",
}
HARDWARE_TERMS = (
    "SoundTouchClient", "get_xml", "post_xml", "send_key", "send_cli",
    "action_preflight", "port_open", "telnet", "ssh", "setZone", "select",
    "storePreset", "notification",
)
KNOWN_ROUTE_DUPLICATES = {
    ("/api/devices/{device_id}/telnet/reboot", "POST"): (
        "documentierter Uebergangspfad: routers/api.py und routers/fulltest.py "
        "montieren denselben Legacy-/LAB-Endpunkt"
    )
}


def parse_file(path: Path) -> tuple[ast.Module, str]:
    source = path.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(path)), source


def expr(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def project_path(path: Path) -> str:
    parts = path.parts
    try:
        index = parts.index("basswiesn")
    except ValueError:
        return path.as_posix()
    return Path(*parts[index:]).as_posix()


def project_module(path: Path) -> str:
    return project_path(path).replace("/", ".").removesuffix(".py")


def literal(node: ast.AST | None, default: Any = None) -> Any:
    if node is None:
        return default
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return default


def function_ranges(tree: ast.AST) -> list[tuple[int, int, str]]:
    return [
        (node.lineno, getattr(node, "end_lineno", node.lineno), node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def enclosing_function(ranges: list[tuple[int, int, str]], line: int) -> str:
    matches = [item for item in ranges if item[0] <= line <= item[1]]
    return max(matches, key=lambda item: item[0])[2] if matches else "<module>"


def request_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict[str, str]]:
    args = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
    return [
        {"name": item.arg, "annotation": expr(item.annotation)}
        for item in args
        if item.arg not in {"self", "cls"}
    ]


def http_exception_codes(node: ast.AST) -> set[int]:
    result: set[int] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call) or expr(child.func) != "HTTPException":
            continue
        for keyword in child.keywords:
            if keyword.arg == "status_code":
                value = literal(keyword.value)
                if isinstance(value, int):
                    result.add(value)
    return result


def router_prefix(tree: ast.Module, router_name: str = "router") -> str:
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if expr(node.value.func) != "APIRouter":
            continue
        if not any(isinstance(target, ast.Name) and target.id == router_name for target in node.targets):
            continue
        for keyword in node.value.keywords:
            if keyword.arg == "prefix":
                return str(literal(keyword.value, ""))
    return ""


def declarations(path: Path, *, router_name: str = "router") -> list[dict[str, Any]]:
    tree, source = parse_file(path)
    prefix = router_prefix(tree, router_name)
    ranges = function_ranges(tree)
    result: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        function_source = ast.get_source_segment(source, node) or ""
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if not isinstance(decorator.func.value, ast.Name):
                continue
            if decorator.func.value.id not in {router_name, "app"} or decorator.func.attr not in ROUTE_METHODS:
                continue
            path_value = literal(decorator.args[0], "") if decorator.args else ""
            if not isinstance(path_value, str):
                continue
            methods: list[str] = []
            for keyword in decorator.keywords:
                if keyword.arg == "methods":
                    raw = literal(keyword.value, [])
                    if isinstance(raw, list):
                        methods = [str(item).upper() for item in raw]
            if not methods:
                methods = [decorator.func.attr.upper()]
            status_code = literal(
                next((item.value for item in decorator.keywords if item.arg == "status_code"), None),
                200,
            )
            response_class = expr(
                next((item.value for item in decorator.keywords if item.arg == "response_class"), None)
            )
            route_path = path_value if decorator.func.value.id == "app" else prefix + path_value
            result.append({
                "module": project_module(path),
                "file": project_path(path),
                "line": node.lineno,
                "handler": node.name,
                "path": route_path or "/",
                "methods": sorted(set(methods)),
                "status_code": status_code,
                "response_class": response_class,
                "function_source": function_source,
                "request_type": request_signature(node),
                "response_type": expr(node.returns),
                "http_exception_codes": sorted(http_exception_codes(node)),
                "enclosing_function": enclosing_function(ranges, node.lineno),
            })
    return result


def all_sources(root: Path) -> dict[str, tuple[ast.Module, str]]:
    return {
        path.relative_to(root).as_posix(): parse_file(path)
        for path in sorted((root / APP_ROOT).rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def handler_sources(sources: dict[str, tuple[ast.Module, str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for tree, source in sources.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                result.setdefault(node.name, ast.get_source_segment(source, node) or "")
    return result


def manual_cloud_mounts(sources: dict[str, tuple[ast.Module, str]], root: Path) -> list[dict[str, Any]]:
    path = "basswiesn/app/main.py"
    tree, _ = sources[path]
    handlers = handler_sources(sources)
    result: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or expr(node.func) != "app.add_api_route":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        endpoint = expr(node.args[1]) if len(node.args) > 1 else ""
        methods: list[str] = []
        for keyword in node.keywords:
            if keyword.arg == "methods":
                raw = literal(keyword.value, [])
                if isinstance(raw, list):
                    methods = [str(item).upper() for item in raw]
        result.append({
            "module": "basswiesn.app.main",
            "file": path,
            "line": node.lineno,
            "handler": endpoint.rsplit(".", 1)[-1],
            "path": str(node.args[0].value),
            "methods": sorted(set(methods)),
            "status_code": literal(
                next((item.value for item in node.keywords if item.arg == "status_code"), None),
                200,
            ),
            "response_class": "",
            "function_source": handlers.get(endpoint.rsplit(".", 1)[-1], ""),
            "request_type": [],
            "response_type": "",
            "http_exception_codes": [],
            "enclosing_function": "_mount_web_cloud_compat",
            "legacy_manual_mount": True,
        })
    return result


def router_declarations(root: Path, module: str, router_name: str,
                        ancestors: tuple[tuple[str, str], ...] = ()) -> list[dict[str, Any]]:
    """Follow nested routers as source, including every effective prefix."""
    key = (module, router_name)
    if key in ancestors:
        raise ValueError("Cyclic router inclusion cannot be inventoried")
    path = root / (module.replace(".", "/") + ".py")
    tree, _ = parse_file(path)
    result = declarations(path, router_name=router_name)
    imports = {
        alias.asname or alias.name: f"{node.module}.{alias.name}"
        for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    for statement in tree.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        if expr(call.func) != f"{router_name}.include_router":
            continue
        target = call.args[0] if call.args else None
        if not isinstance(target, ast.Attribute) or not isinstance(target.value, ast.Name):
            raise ValueError("Unresolved nested router mount: inspect the trust boundary")
        child = imports.get(target.value.id)
        if not child:
            raise ValueError("Nested router import cannot be resolved")
        prefix_node = next((item.value for item in call.keywords if item.arg == "prefix"), None)
        prefix = literal(prefix_node, "" if prefix_node is None else None)
        if not isinstance(prefix, str):
            raise ValueError("Dynamic nested prefix cannot be inventoried safely")
        inherited = router_prefix(tree, router_name) + prefix
        rows = router_declarations(root, child, target.attr, (*ancestors, key))
        result.extend({**row, "path": inherited + row["path"]} for row in rows)
    return result


def mounted_routes(sources: dict[str, tuple[ast.Module, str]], root: Path, factory: str) -> list[dict[str, Any]]:
    """Follow actual include_router mounts, not a second hand-maintained list.

    Unknown dynamic mounts fail the inventory build so a new auth surface cannot
    silently disappear from the trust-boundary review. Application imports and
    startup side effects remain forbidden in this static generator.
    """
    tree, _ = sources["basswiesn/app/main.py"]
    imports = {
        alias.asname or alias.name: f"{node.module}.{alias.name}"
        for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == factory)
    result: list[dict[str, Any]] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or expr(node.func) != "app.include_router":
            continue
        target = node.args[0] if node.args else None
        if not isinstance(target, ast.Attribute) or not isinstance(target.value, ast.Name):
            raise ValueError("Unresolved dynamic router mount: inspect the trust boundary")
        module = imports.get(target.value.id)
        if not module:
            raise ValueError("Router import cannot be resolved")
        prefix_node = next((item.value for item in node.keywords if item.arg == "prefix"), None)
        prefix = literal(prefix_node, "" if prefix_node is None else None)
        if not isinstance(prefix, str):
            raise ValueError("Dynamic router prefix cannot be inventoried safely")
        rows = router_declarations(root, module, target.attr)
        result.extend({**row, "path": prefix + row["path"]} for row in rows)
    return result


def web_routes(sources: dict[str, tuple[ast.Module, str]], root: Path) -> list[dict[str, Any]]:
    result = mounted_routes(sources, root, "create_web_app")
    result.extend(
        item for item in declarations(root / "basswiesn/app/main.py")
        if item["enclosing_function"] in {"create_web_app", "remote"}
    )
    result.extend(manual_cloud_mounts(sources, root))
    return result


def test_evidence(path_value: str, handler: str, root: Path) -> list[str]:
    result: list[str] = []
    for path in sorted((root / TEST_ROOT).glob("test_*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if handler in text or (path_value not in {"/", "/{path:path}"} and path_value in text):
            result.append(path.relative_to(root).as_posix())
    return result


def route_record(route: dict[str, Any], application: str, method: str, root: Path,
                 device_contracts: dict[str, str], device_handlers: set[str],
                 token_contracts: dict[tuple[str, str, str, str], str]) -> dict[str, Any]:
    source = route["function_source"]
    write = method not in SAFE_METHODS
    hardware = any(term in source or term in route["path"] for term in HARDWARE_TERMS)
    database = any(term in source for term in ("db.commit", "db.add", "db.delete", "db.flush", "db.merge"))
    policy = any(term in source for term in (
        "require_unprotected_device", "reject_protected_device_access",
        "action_preflight", "protected_device", "DeviceInteraction",
    ))
    workbench = route["module"] == "basswiesn.app.routers.lab_workbench"
    learning = route["module"] == "basswiesn.app.routers.lab_learning"
    settings_write = route["module"] == "basswiesn.app.routers.api" and route["handler"] in {
        "rename_device", "set_device_setting", "apply_changed_device_settings",
    }
    airplay_runtime_write = route["module"] == "basswiesn.app.routers.airplay_bridge" and route["handler"] in {
        "runtime_start", "runtime_stop",
    }
    if airplay_runtime_write:
        if "_command" not in source:
            raise ValueError("AirPlay lifecycle delegation changed; review the trust inventory")
        # The local service owns radio selection and verified restoration.
        # A Unix socket is a trust boundary, not evidence of no hardware I/O.
        hardware = database = policy = True
    if settings_write:
        # Reviewed delegation boundary: a thin router is not a no-hardware
        # route merely because its transport calls moved into a service.
        if "_apply_settings_transaction" not in source:
            raise ValueError("Settings write delegation changed; review the trust inventory")
        hardware = database = policy = True
    if route["module"] == "basswiesn.app.routers.api" and route["path"] == "/api/play-history/event":
        # The unverified client event is still a local database mutation after
        # delegation. It neither observes nor commands the physical radio.
        if "record_client_report" not in source:
            raise ValueError("History report delegation changed; review the trust inventory")
        database = True
        hardware = False
    if route["module"] == "basswiesn.app.routers.api" and route["handler"] in {
        "device_power_action", "wireless_profiles", "wireless_survey",
    }:
        delegate = "execute_power" if route["handler"] == "device_power_action" else "wireless_diagnostics"
        if delegate not in source:
            raise ValueError("Power/wireless delegation changed; review the trust inventory")
        hardware = policy = True
        database = route["handler"] != "wireless_profiles"
    if route["module"] == "basswiesn.app.routers.multiroom" and route["handler"] in {
        "multiroom_clear", "multiroom_clear_all", "multiroom_remove_device",
    }:
        if "_clear_zone_transaction" not in source and "change_zone" not in source:
            raise ValueError("Zone write delegation changed; review the trust inventory")
        hardware = database = policy = True
    if route["module"] == "basswiesn.app.routers.multiroom" and route["handler"] == "multiroom_latency":
        if "apply_settings_batch" not in source:
            raise ValueError("Latency settings delegation changed; review the trust inventory")
        hardware = database = policy = True
    manual_command = route["module"] == "basswiesn.app.routers.api" and route["handler"] in {
        "native_station_add", "add_wireless_profile",
    }
    if manual_command:
        if "_manual_command_response" not in source:
            raise ValueError("Manual command delegation changed; review the trust inventory")
        hardware = database = policy = True
    native_search = route["module"] == "basswiesn.app.routers.api" and route["handler"] == "native_station_search"
    if native_search:
        if "search_native" not in source:
            raise ValueError("Native search delegation changed; review the trust inventory")
        hardware = policy = True
        # A provider query uses POST, but does not change stored radio settings.
        database = False
    preset_delegate = {
        ("basswiesn.app.routers.stations_presets", "play_station_on_device"): "_play_station_on_device",
        ("basswiesn.app.routers.stations_presets", "set_preset"): "_apply_prepared",
        ("basswiesn.app.routers.stations_presets", "clone_presets"): "_apply_prepared",
        ("basswiesn.app.routers.stations_presets", "sync_local_presets"): "_apply_prepared",
        ("basswiesn.app.routers.stations_presets", "download_device_presets"): "download_presets",
        ("basswiesn.app.routers.stations_presets", "delete_preset"): "remove_preset",
        ("basswiesn.app.routers.api", "apply_preset_profile"): "apply_prepared_presets",
    }.get((route["module"], route["handler"]))
    if preset_delegate:
        if preset_delegate not in source:
            raise ValueError("Preset delegation changed; review the trust inventory")
        hardware = database = policy = True
    if route["module"] == "basswiesn.app.routers.multiroom" and route["handler"] == "multiroom_set":
        if "_multiroom_set" not in source:
            raise ValueError("Zone creation delegation changed; review the trust inventory")
        hardware = database = policy = True
    if learning:
        hardware = False
        database = route["handler"] in {"save_profile", "remove_profile"}
        policy = False  # LAB dependency, no device identity or transport
    if workbench:
        # Delegated playback is invisible to this function-local scanner;
        # conversely, selecting a saved snapshot does not contact a radio.
        hardware = route["handler"] == "replay"
        database = route["handler"] in {"replay", "capture", "remove"}
        policy = "{device_id}" in route["path"]
    legacy = (route["module"] == "basswiesn.app.routers.cloud" or route.get("legacy_manual_mount", False)) and route["handler"] in device_handlers and method in device_contracts.get(route["path"], "").split()
    minimal_health = route["path"] in {"/live", "/ready"} and method == "GET"
    auth_route = route["module"] == "basswiesn.app.routers.admin_auth"
    token_management = route["module"] == "basswiesn.app.routers.api_tokens"
    provider_catalog = route["module"] == "basswiesn.app.routers.providers"
    dlna_library = route["module"] == "basswiesn.app.routers.dlna"
    provider_io = (provider_catalog and method == "POST") or (
        dlna_library and route["handler"] in {"discover", "connect", "browse", "import_item", "audio"}
    )
    token_scope = token_contracts.get((route["module"].removeprefix("basswiesn.app.routers."),
                                      route["handler"], route["path"], method)) if application == "webgui" else None
    access = "DEVICE_CONTRACT" if legacy else "PUBLIC_MINIMAL_HEALTH" if minimal_health else "LOGIN_ORIGIN" if auth_route and route["path"] == "/auth/login" else "ADMIN_SESSION"
    if auth_route:
        hardware = policy = False
        database = route["handler"] in {"login", "logout"}  # separate credential store
    if token_management:
        hardware = policy = False
        database = method != "GET"  # separate credential store, session-only
    if provider_catalog:
        # Explicit POST operations may perform bounded provider I/O, never a
        # radio write. GET returns descriptors/cached health without a probe.
        hardware = database = False
        policy = method == "POST"
    if dlna_library:
        # These handlers delegate bounded NAS I/O; importing a media item only
        # changes our catalog, never presets or playback on a radio.
        hardware = False
        database = route["handler"] in {"settings", "connect", "forget", "import_item"}
        policy = provider_io
    if legacy:
        purpose = "Bose-/SoundTouch-Cloud-Kompatibilitaet"
    elif route["path"] == "/":
        purpose = "WebGUI-Shell"
    elif route["path"].startswith("/remote/"):
        purpose = "Mobile/Remote-WebGUI-Shell"
    elif hardware:
        purpose = "Geraete-, Hardware- oder Diagnoseablauf"
    elif write:
        purpose = "Management-Mutation oder vorbereitender Ablauf"
    else:
        purpose = "Management-Abfrage oder Statusdarstellung"
    statuses = {int(route["status_code"] or 200)}
    statuses.update(route["http_exception_codes"])
    if airplay_runtime_write:
        statuses.update({202, 400, 401, 403, 409})
    if access in {"ADMIN_SESSION", "LOGIN_ORIGIN"}:
        statuses.update({401, 403, 503})
    if auth_route and route["handler"] == "login":
        statuses.update({400, 404, 429})
    if token_management:
        statuses.update({400, 401, 403, 404, 503})
        if route["handler"] == "issue_token":
            statuses.add(201)
    internal_attestation = minimal_health and route["path"] == "/ready"
    if internal_attestation:
        statuses.update({403, 503})
    if settings_write:
        statuses.update({400, 403, 404, 409, 422, 503})
    if manual_command:
        statuses.update({202, 400, 403, 404, 409, 502})
    if native_search:
        statuses.update({400, 403, 404, 409, 502})
    if dlna_library:
        statuses.update({400, 404, 409, 422, 503})
        if route["handler"] == "audio":
            statuses.update({206, 416, 429})
    if workbench or learning:
        statuses.add(409)  # require_lab dependency, including read-only routes
        if "{device_id}" in route["path"]:
            statuses.update({403, 404, 422})
    if route["path"] == "/{path:path}":
        statuses.update({200, 204})
    if legacy:
        error_form = "Bose-kompatible XML/JSON-Antwort; Catch-all kann 200/204 liefern"
    elif application == "diagnostics":
        error_form = "FastAPI HTTPException oder handler-definierte Diagnoseantwort"
    else:
        error_form = "FastAPI HTTPException detail oder handler-definierte JSON-Antwort"
    return {
        "application": application,
        "application_title": APPLICATIONS[application]["title"],
        "port": APPLICATIONS[application]["port"],
        "path": route["path"],
        "method": method,
        "handler": route["handler"],
        "module": route["module"],
        "source": f'{route["file"]}:{route["line"]}',
        "purpose": purpose,
        "request_type": route["request_type"],
        "response_type": route["response_class"] or route["response_type"] or "handler-defined JSON",
        "expected_status_codes": sorted(statuses),
        "current_error_form": error_form,
        "write_operation": write,
        "hardware_contact_possible": hardware,
        "provider_contact_possible": provider_io,
        "database_change_possible": database,
        "future_authentication_required": access == "ADMIN_SESSION",  # retained legacy field
        "required_mode_access": access,
        "automation_token_scope": token_scope,
        "automation_token_constraints": "setting must equal volume" if token_scope == "playback" else "",
        "internal_readonly_attestation": internal_attestation,
        "authentication_state_change_possible": access == "ADMIN_SESSION" or (auth_route and method == "POST"),
        "device_policy_required": policy or hardware,
        "legacy_bose_compatibility_route": legacy,
        "publicity": "legacy-compatibility" if legacy else "internal-management" if application in {"webgui", "https-webgui"} else "internal-diagnostics",
        "test_evidence": sorted(set(test_evidence(route["path"], route["handler"], root)) | (
            {"tests/test_settings_write_safety.py", "tests/test_settings_contract.py"} if settings_write else set()
        )),
        "legacy_manual_mount": bool(route.get("legacy_manual_mount", False)),
        "documented_duplicate_reason": KNOWN_ROUTE_DUPLICATES.get((route["path"], method), ""),
        "notes": (
            "Required mode trennt explizite Geraetevertraege von Human-Routen; Legacy mode ist nicht authentifiziert."
            if application == "cloud"
            else "HTTPS nutzt dieselbe WebGUI-App und ist standardmaessig deaktiviert."
            if application == "https-webgui"
            else ""
        ),
    }


def build_api_inventory(root: Path) -> dict[str, Any]:
    sources = all_sources(root)
    # Read policy as source data, without importing application startup.
    policy_tree = ast.parse((root / "basswiesn/app/core/admin_protocols.py").read_text(encoding="utf-8"))
    assignments = {node.targets[0].id: node.value for node in policy_tree.body
                   if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    device_contracts = ast.literal_eval(assignments["DEVICE_CONTRACTS"])
    device_handlers = set(ast.literal_eval(assignments["DEVICE_HANDLERS"].args[0]))
    token_tree = ast.parse((root / "basswiesn/app/core/token_policy.py").read_text(encoding="utf-8"))
    token_contracts = next(ast.literal_eval(node.value) for node in token_tree.body
                           if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                           and node.targets[0].id == "CONTRACTS")
    declarations_by_app = {
        "webgui": web_routes(sources, root),
        "cloud": mounted_routes(sources, root, "create_cloud_app"),
        "diagnostics": mounted_routes(sources, root, "create_debug_app"),
    }
    routes: list[dict[str, Any]] = []
    main_tree = sources["basswiesn/app/main.py"][0]
    health_factory = next(node for node in main_tree.body if isinstance(node, ast.FunctionDef) and node.name == "_mount_minimal_health")
    health_routes = [item for item in declarations(root / "basswiesn/app/main.py")
                     if health_factory.lineno < item["line"] <= health_factory.end_lineno]
    for application, declaration_list in declarations_by_app.items():
        for route in [*declaration_list, *health_routes]:
            for method in route["methods"]:
                routes.append(route_record(route, application, method, root, device_contracts, device_handlers, token_contracts))
    routes.extend(
        {**route, "application": "https-webgui", "application_title": APPLICATIONS["https-webgui"]["title"],
         "port": APPLICATIONS["https-webgui"]["port"], "notes": "HTTPS nutzt dieselbe WebGUI-App und ist standardmaessig deaktiviert."}
        for route in routes if route["application"] == "webgui"
    )
    routes.sort(key=lambda item: (item["application"], item["path"], item["method"], item["handler"]))
    return {
        "schema_version": 1,
        "generator": "tools/generate_phase4a_contracts.py",
        "applications": APPLICATIONS,
        "mounts": [
            {"application": "webgui", "path": "/static", "type": "StaticFiles", "source": "basswiesn/app/static"},
            {"application": "cloud", "path": "/static", "type": "StaticFiles", "source": "basswiesn/app/static"},
            {"application": "cloud", "path": "/media", "type": "StaticFiles", "source": "settings.data_dir/media"},
        ],
        "routes": routes,
    }


def access_record(path: Path, node: ast.Call, function: str, source: str) -> dict[str, Any] | None:
    name = expr(node.func)
    attr = node.func.attr if isinstance(node.func, ast.Attribute) else ""
    terminal = name.rsplit(".", 1)[-1]
    soundtouch = name.endswith("SoundTouchClient") or attr in {"get_xml", "post_xml"}
    subprocess_call = terminal in {"run", "Popen", "create_subprocess_exec"} and "subprocess" in name
    ssdp = "ssdp" in path.name.lower() or "SSDP" in source[max(0, node.lineno - 1): node.lineno + 1]
    network = soundtouch or subprocess_call or ssdp or terminal in NETWORK_NAMES
    if not network:
        return None
    if attr == "get_xml":
        operation, direction, transport = "SoundTouch XML request", "GET", "HTTP/XML"
        target = literal(node.args[0], expr(node.args[0])) if node.args else ""
        if target in {"/standby", "/lowPowerStandby"} or (path.name == "device_power.py" and target == "path"):
            operation, direction = "State-changing SoundTouch GET (one-shot power action)", "WRITE"
    elif attr == "post_xml":
        operation, direction, transport = "SoundTouch XML request", "WRITE", "HTTP/XML"
    elif subprocess_call:
        operation, direction, transport = "Process/SSH/CLI invocation", "EXEC", "subprocess/SSH/CLI"
    elif ssdp:
        operation, direction, transport = "SSDP discovery", "DISCOVERY", "SSDP/UDP"
    else:
        operation, direction, transport = "HTTP/socket/network request", "NETWORK", "HTTP/socket"
    protected = any(term in source for term in (
        "require_unprotected_device", "reject_protected_device_access",
        "protected_device", "filter_protected_devices",
    ))
    coordinator = "DeviceInteractionCoordinator" in source or path.name == "device_interactions.py"
    return {
        "file": project_path(path),
        "line": node.lineno,
        "column": node.col_offset,
        "function": function,
        "call": name,
        "operation": operation,
        "transport": transport,
        "target_expression": literal(node.args[0], expr(node.args[0])) if (soundtouch and node.args) else expr(node.func),
        "direction": direction,
        "direct_client_access": soundtouch and not coordinator,
        "coordinator_path": coordinator,
        "device_policy_present_in_function": protected,
        "timeout_visible_in_function": any(term in source for term in ("timeout", "wait_for", "get_timeout", "post_timeout")),
        "retry_visible_in_function": any(term in source for term in ("retry", "backoff", "attempt", "circuit")),
        "logging_visible_in_function": any(term in source for term in ("write_masterlog", "logger.", "record_action")),
        "readback_or_verification_visible": any(term in source for term in ("readback", "read-back", "verify", "getZone", "get_xml")),
        "hardware_effect_possible": direction == "WRITE" or any(term in source for term in ("send_key", "post_xml", "setZone", "volume", "reboot", "telnet", "ssh")),
        "recommended_target_architecture": (
            "bestehende Policy-/Coordinator-Schicht" if coordinator and protected
            else "DeviceInteractionCoordinator mit zentralem Policy-Check" if soundtouch
            else "SSDP-/Discovery-Service mit Schutz- und URL-Validierung" if ssdp
            else "dedizierter CLI-/SSH-Service mit Allowlist" if subprocess_call
            else "dedizierter Netzwerk-Service mit Timeout und Zielvalidierung"
        ),
        "analysis_basis": "static AST call and enclosing-function source",
    }


def build_device_inventory(root: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for path in sorted((root / APP_ROOT).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree, source = parse_file(path)
        ranges = function_ranges(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                record = access_record(path, node, enclosing_function(ranges, node.lineno), source)
                if record:
                    entries.append(record)
    entries.sort(key=lambda item: (item["file"], item["line"], item["column"], item["call"]))
    summary = {
        "total_entries": len(entries),
        "direct_soundtouch_entries": sum(item["direct_client_access"] for item in entries),
        "coordinator_entries": sum(item["coordinator_path"] for item in entries),
        "write_or_effect_entries": sum(item["hardware_effect_possible"] for item in entries),
        "policy_visible_entries": sum(item["device_policy_present_in_function"] for item in entries),
        "network_entries_without_visible_policy": sum(
            item["transport"] in {"HTTP/XML", "SSDP/UDP", "HTTP/socket"} and not item["device_policy_present_in_function"]
            for item in entries
        ),
    }
    return {"schema_version": 1, "generator": "tools/generate_phase4a_contracts.py", "summary": summary, "entries": entries}


def config_service(name: str) -> str:
    value = name.upper()
    if value.startswith("MUSIC_ASSISTANT_"):
        return "Optional provider catalog / LAB"
    if any(term in value for term in ("PLAYBACK", "SSDP", "DEVICE", "PROTECTED")):
        return "WebGUI/Device Runtime"
    if any(term in value for term in ("TELNET", "SSH", "STANDBY")):
        return "LAB/Recovery"
    if any(term in value for term in ("WEBHOOK", "UPDATE", "OFFLINE")):
        return "WebGUI/External Services"
    if any(term in value for term in ("MEDIA", "DLNA", "ANNOUN")):
        return "Experimental/LAB"
    if any(term in value for term in ("HTTPS", "TLS", "CERT")):
        return "WebGUI/HTTPS"
    if any(term in value for term in ("RETENTION", "BACKUP", "DIAGNOSTIC", "MASTERLOG")):
        return "Persistence/Diagnostics"
    return "Global Runtime"


def build_config_inventory(root: Path) -> list[dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    example = (root / ".env.example").read_text(encoding="utf-8") if (root / ".env.example").exists() else ""
    for match in ENV_RE.finditer(example):
        values.setdefault(match.group(), {"sources": set()})["sources"].add(".env.example")
    for path in sorted((root / APP_ROOT).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree, source = parse_file(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = expr(node.func)
            provider_config = (path.relative_to(root).as_posix() == "basswiesn/app/services/providers/runtime.py"
                               and name == "source.get")
            if name not in {"_env_bool", "_env_int", "_env_text", "os.getenv"} and not provider_config:
                continue
            env_name = literal(node.args[0] if node.args else None)
            if not isinstance(env_name, str) or not ENV_RE.fullmatch(env_name):
                continue
            item = values.setdefault(env_name, {"sources": set()})
            item["sources"].add(project_path(path))
            if name in {"_env_bool", "_env_int", "_env_text"}:
                item["type"] = {"_env_bool": "bool", "_env_int": "int", "_env_text": "string"}[name]
                if len(node.args) > 1:
                    item["default"] = literal(node.args[1], expr(node.args[1]))
            elif len(node.args) > 1:
                item.setdefault("default", literal(node.args[1], expr(node.args[1])))
    result: list[dict[str, Any]] = []
    for name, item in sorted(values.items()):
        default = item.get("default", "")
        if isinstance(default, tuple):
            default = list(default)
        if any(name.endswith(suffix) for suffix in ("_IPS", "_HOSTS", "_ROOTS", "_IDS")):
            value_type, allowed = "csv", "Kommagetrennte Werte"
        elif name == "BASSWIESN_OFFLINE_MODE":
            value_type, allowed = "enum", "off | auto | strict"
        elif name == "BASSWIESN_UPDATE_CHANNEL":
            value_type, allowed = "enum", "manual | stable | beta"
        else:
            value_type = item.get("type", "string")
            allowed = (
                "true/false, 1/0, yes/no, on/off" if value_type == "bool"
                else "Ganzzahl; _env_int verwendet Mindestwert 1" if value_type == "int"
                else "freier Text gemaess Code"
            )
        security = "hoch" if any(term in name for term in ("PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL")) else "mittel" if any(term in name for term in ("PROTECTED", "TLS", "HTTPS", "UPDATE", "WEBHOOK", "SSH", "TELNET", "OFFLINE")) else "niedrig"
        note = "Alias/Quelle fuer geschuetzte Geraete; fail-closed relevant" if name in {"PROTECTED_DEVICE_IPS", "BASSWIESN_PROTECTED_DEVICE_IPS"} else "Secret-Datei; Inhalt nie in UI/Reports ausgeben" if name == "BASSWIESN_TELNET_PASSWORD_FILE" else ""
        if name.startswith("MUSIC_ASSISTANT_"):
            security = "hoch" if name.endswith("TOKEN") else "mittel"
            note = "Disabled by default; explicit authenticated catalog operations only; no startup probe or radio write."
            if name.endswith("ENABLED"):
                value_type, allowed = "bool", "true | 1 enables; any other value disables"
            elif name.endswith("TOKEN"):
                default = ""
                note += " Private server credential; never returned in API or diagnostic configuration."
        result.append({
            "name": name,
            "type": value_type,
            "default": default,
            "required": False,
            "service": config_service(name),
            "security_relevance": security,
            "documented_in_env_example": name in example,
            "deprecated": False,
            "restart_required": True,
            "allowed_values": allowed,
            "sources": sorted(item["sources"]),
            "notes": note,
        })
    return result


def render_api(inventory: dict[str, Any]) -> str:
    lines = [
        "# Phase 4A API-Vertragsmatrix",
        "",
        "Automatisch aus montierten FastAPI-Routen und manuellen Cloud-Montagen",
        "erzeugt. Zweck, Hardwarewirkung und Authbedarf sind statische",
        "Auditklassifikationen; bestehende API-Vertraege wurden nicht veraendert.",
        "",
        f"Routenmethoden: {len(inventory['routes'])}",
        "",
        "| Anwendung | Methode | Pfad | Handler | Antwort | Status | Write | Hardware | DB | Policy | Tests |",
        "|---|---|---|---|---|---|---:|---:|---:|---:|---|",
    ]
    for route in inventory["routes"]:
        lines.append("| {application} | {method} | {path} | {handler} | {response} | {status} | {write} | {hardware} | {db} | {policy} | {tests} |".format(
            application=route["application"], method=route["method"], path=route["path"], handler=route["handler"],
            response=route["response_type"], status=",".join(map(str, route["expected_status_codes"])),
            write="ja" if route["write_operation"] else "nein",
            hardware="moeglich" if route["hardware_contact_possible"] else "nein",
            db="moeglich" if route["database_change_possible"] else "nein",
            policy="ja" if route["device_policy_required"] else "nein",
            tests=", ".join(route["test_evidence"]) or "kein statischer Treffer",
        ))
    lines.extend([
        "",
        "## Sonderfaelle",
        "",
        "- Der Cloud-Catch-all /{path:path} ist kompatibilitaetsorientiert und",
        "  kann unbekannte Pfade mit 200/204 beantworten.",
        "- /serviceSettings und /getServiceSettings sind bestehende Aliasse.",
        "- POST /api/devices/{device_id}/telnet/reboot ist aktuell doppelt in",
        "  api.py und fulltest.py montiert; dies ist als Uebergangspfad markiert.",
        "- Die optionale HTTPS-App nutzt dieselbe WebGUI-Routenmenge und ist",
        "  nur bei BASSWIESN_ENABLE_HTTPS=true aktiv.",
        "- Die Phase 4A implementiert keine Authentifizierung.",
    ])
    return "\n".join(lines) + "\n"


def render_device(inventory: dict[str, Any]) -> str:
    summary = inventory["summary"]
    lines = [
        "# Phase 4A Geraetezugriffs-Inventar",
        "",
        "Statische AST-/Quelltextauswertung. Es wurden keine Radios, Sockets zu",
        "SoundTouch-Zielen oder externen Dienste kontaktiert.",
        "",
        f"Fundstellen: {summary['total_entries']}",
        f"Direkte SoundTouch-Fundstellen: {summary['direct_soundtouch_entries']}",
        f"Coordinator-Fundstellen: {summary['coordinator_entries']}",
        "",
        "| Datei:Zeile | Funktion | Transport | Operation | Ziel | Direkt | Policy | Timeout | Retry | Readback | Zielarchitektur |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in inventory["entries"]:
        lines.append("| {file}:{line} | {function} | {transport} | {operation}/{direction} | {target} | {direct} | {policy} | {timeout} | {retry} | {readback} | {recommendation} |".format(
            file=item["file"], line=item["line"], function=item["function"], transport=item["transport"],
            operation=item["operation"], direction=item["direction"], target=item["target_expression"],
            direct="ja" if item["direct_client_access"] else "nein",
            policy="ja" if item["device_policy_present_in_function"] else "nein/unklar",
            timeout="sichtbar" if item["timeout_visible_in_function"] else "unklar",
            retry="sichtbar" if item["retry_visible_in_function"] else "unklar",
            readback="sichtbar" if item["readback_or_verification_visible"] else "nein",
            recommendation=item["recommended_target_architecture"],
        ))
    return "\n".join(lines) + "\n"


def render_config(records: list[dict[str, Any]]) -> str:
    lines = [
        "# Phase 4A Konfigurationsmatrix",
        "",
        "Statisch aus config.py, den Python-Quellen und .env.example erzeugt.",
        "Environment-Settings werden ueber die gecachte get_settings()-Instanz",
        "gelesen; fuer eine sichere Wirksamkeit ist ein Prozessneustart anzunehmen.",
        "",
        "| Variable | Typ | Standardwert | Erforderlich | Dienst | Sicherheit | .env | Neustart | Erlaubte Werte | Hinweis |",
        "|---|---|---|---:|---|---|---:|---:|---|---|",
    ]
    for item in records:
        lines.append("| {name} | {type} | {default} | {required} | {service} | {security} | {documented} | {restart} | {allowed} | {notes} |".format(
            name=item["name"], type=item["type"], default=json.dumps(item["default"], ensure_ascii=True),
            required="ja" if item["required"] else "nein", service=item["service"],
            security=item["security_relevance"], documented="ja" if item["documented_in_env_example"] else "nein",
            restart="ja" if item["restart_required"] else "nein", allowed=item["allowed_values"], notes=item["notes"],
        ))
    return "\n".join(lines) + "\n"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def generate(root: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    return build_api_inventory(root), build_device_inventory(root), build_config_inventory(root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true", help="Reject stale generated contracts before lengthy tests")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.write and args.check:
        parser.error("choose --write or --check")
    api, device, config = generate(root)
    if args.check:
        def encoded(value: Any) -> str:
            return json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        expected = {
            "docs/generated/api-contract-matrix.json": encoded(api),
            "docs/generated/device-access-inventory.json": encoded(device),
            "docs/generated/configuration-inventory.json": encoded(config),
            "docs/generated/api-contract-matrix.md": render_api(api),
            "docs/generated/device-access-inventory.md": render_device(device),
            "docs/PHASE_4A_API_CONTRACT_MATRIX.md": render_api(api),
            "docs/PHASE_4A_DEVICE_ACCESS_INVENTORY.md": render_device(device),
            "docs/PHASE_4A_CONFIGURATION_MATRIX.md": render_config(config),
        }
        stale = [name for name, text in expected.items()
                 if not (root / name).is_file() or (root / name).read_text(encoding="utf-8") != text]
        for name in stale:
            print(f"FAIL: generated contract requires regeneration: {name}")
        if not stale:
            print("PASS: generated API/device/configuration contracts match source")
        return int(bool(stale))
    if args.write:
        generated = root / "docs/generated"
        write_json(generated / "api-contract-matrix.json", api)
        write_json(generated / "device-access-inventory.json", device)
        write_json(generated / "configuration-inventory.json", config)
        (generated / "api-contract-matrix.md").write_text(render_api(api), encoding="utf-8")
        (generated / "device-access-inventory.md").write_text(render_device(device), encoding="utf-8")
        (root / "docs/PHASE_4A_API_CONTRACT_MATRIX.md").write_text(render_api(api), encoding="utf-8")
        (root / "docs/PHASE_4A_DEVICE_ACCESS_INVENTORY.md").write_text(render_device(device), encoding="utf-8")
        (root / "docs/PHASE_4A_CONFIGURATION_MATRIX.md").write_text(render_config(config), encoding="utf-8")
    else:
        print(json.dumps({"routes": len(api["routes"]), "device_access_entries": len(device["entries"]), "configuration_variables": len(config)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
