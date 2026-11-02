from collections import Counter
from pathlib import Path
import json

import pytest

from tools.generate_phase4a_contracts import (
    build_api_inventory,
    build_config_inventory,
    build_device_inventory,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_api_matrix_covers_mounted_applications_and_known_duplicate():
    inventory = build_api_inventory(ROOT)
    applications = set(inventory["applications"])
    assert applications == {"webgui", "cloud", "diagnostics", "https-webgui"}
    routes = inventory["routes"]
    assert len(routes) > 500
    assert any(route["path"] == "/{path:path}" and route["application"] == "cloud" for route in routes)
    assert any(route["path"] == "/api/health" and route["application"] == "webgui" for route in routes)
    assert any(route["path"] == "/api/health" and route["application"] == "https-webgui" for route in routes)
    ap2 = [route for route in routes if route["path"] == "/api/airplay-bridge/status"]
    assert {route["application"] for route in ap2} == {"webgui", "https-webgui"}
    assert all(route["method"] == "GET" and not route["hardware_contact_possible"]
               and not route["database_change_possible"] for route in ap2)
    assert {r["application"] for r in routes if r["path"] == "/api/dlna/servers"} == {"webgui", "https-webgui"}
    assert {r["application"] for r in routes if r["path"] == "/dlna/audio/{server_id}/{token}"} == {"cloud"}
    assert not any(r["path"].startswith("/api/dlna/dlna/") for r in routes)
    workbench = [r for r in routes if r["path"].startswith("/api/lab/workbench/")]
    assert len(workbench) == 22  # eleven endpoints, mounted for HTTP and HTTPS
    assert {r["application"] for r in workbench} == {"webgui", "https-webgui"}
    assert all(r["hardware_contact_possible"] == (r["handler"] == "replay") for r in workbench)
    assert all(409 in r["expected_status_codes"] for r in workbench)

    history = [r for r in routes if r["path"].startswith("/api/play-history")]
    assert history
    for row in history:
        assert not row["hardware_contact_possible"]
        assert row["database_change_possible"] == (row["path"] == "/api/play-history/event")
        assert row["required_mode_access"] == "ADMIN_SESSION"
    latency = [row for row in routes if row["path"] == "/api/multiroom/latency"]
    assert len(latency) == 2
    assert all(row["hardware_contact_possible"] and row["database_change_possible"]
               and row["device_policy_required"] for row in latency)

    for application in applications:
        rows = [route for route in routes if route["application"] == application]
        duplicates = Counter((route["path"], route["method"]) for route in rows)
        for key, count in duplicates.items():
            if count > 1:
                assert all(route["documented_duplicate_reason"] for route in rows if (route["path"], route["method"]) == key)


def test_device_inventory_classifies_direct_clients_without_network_access():
    inventory = build_device_inventory(ROOT)
    assert inventory["entries"]
    assert inventory["summary"]["direct_soundtouch_entries"] > 0
    assert any(item["transport"] == "HTTP/XML" and item["direction"] == "WRITE" for item in inventory["entries"])
    assert any(item["transport"] == "SSDP/UDP" for item in inventory["entries"])
    assert all(item["analysis_basis"].startswith("static") for item in inventory["entries"])


def test_airplay_local_service_is_not_misclassified_as_transport_free_write():
    rows = [row for row in build_api_inventory(ROOT)['routes']
            if row['path'].startswith('/api/airplay-bridge/runtime/')]
    assert len(rows) == 6
    for row in rows:
        writing = row['method'] == 'POST'
        assert row['hardware_contact_possible'] == writing
        assert row['database_change_possible'] == writing
        assert row['device_policy_required'] == writing
        assert row['required_mode_access'] == 'ADMIN_SESSION'
        assert row['automation_token_scope'] is None
        if writing:
            assert {202, 400, 401, 403, 409} <= set(row['expected_status_codes'])


def test_preset_service_delegation_retains_trust_boundaries():
    rows = [row for row in build_api_inventory(ROOT)["routes"] if row["handler"] in {
        "delete_preset", "download_device_presets", "apply_preset_profile"}]
    assert len(rows) == 6
    for row in rows:
        assert row["hardware_contact_possible"] and row["database_change_possible"]
        assert row["device_policy_required"] and row["write_operation"]
        assert row["required_mode_access"] == "ADMIN_SESSION"


def test_power_and_wireless_delegation_keeps_truthful_trust_inventory():
    routes = build_api_inventory(ROOT)["routes"]
    affected = [row for row in routes if row["handler"] in {
        "device_power_action", "wireless_profiles", "wireless_survey",
        "native_station_add", "add_wireless_profile",
    }]
    assert len(affected) == 10
    for row in affected:
        assert row["hardware_contact_possible"] and row["device_policy_required"]
        assert row["required_mode_access"] == "ADMIN_SESSION"
        assert row["automation_token_scope"] is None
        assert row["database_change_possible"] == (row["handler"] != "wireless_profiles")
        assert row["write_operation"] == (row["handler"] != "wireless_profiles")
        if row["handler"] in {"native_station_add", "add_wireless_profile"}:
            assert {202, 400, 403, 409, 502} <= set(row["expected_status_codes"])
    accesses = build_device_inventory(ROOT)["entries"]
    commands = [entry for entry in accesses if entry["file"].endswith("/device_power.py")
                and entry["target_expression"] == "path"]
    assert len(commands) == 1 and commands[0]["direction"] == "WRITE"


def test_nested_system_routes_retain_prefix_and_token_contract():
    rows = build_api_inventory(ROOT)["routes"]
    versions = [row for row in rows if row["handler"] == "api_version"]
    assert len(versions) == 2
    assert all(row["path"] == "/api/version" for row in versions)
    assert all(row["automation_token_scope"] == "read" for row in versions)
    assert all(row["module"] == "basswiesn.app.routers.system_api" for row in versions)


def test_delegated_settings_routes_retain_write_trust_boundary():
    routes = build_api_inventory(ROOT)["routes"]
    writes = [row for row in routes if row["handler"] in {
        "rename_device", "set_device_setting", "apply_changed_device_settings",
    }]
    assert len(writes) == 6  # HTTP and optional HTTPS, same safety service
    for row in writes:
        assert row["hardware_contact_possible"] and row["database_change_possible"]
        assert row["device_policy_required"] and row["write_operation"]
        assert {400, 403, 409, 503} <= set(row["expected_status_codes"])
        assert "tests/test_settings_write_safety.py" in row["test_evidence"]


def test_configuration_inventory_contains_aliases_and_secret_boundaries():
    records = {item["name"]: item for item in build_config_inventory(ROOT)}
    assert "PROTECTED_DEVICE_IPS" in records
    assert "BASSWIESN_PROTECTED_DEVICE_IPS" in records
    assert records["BASSWIESN_TELNET_PASSWORD_FILE"]["security_relevance"] == "hoch"
    assert records["BASSWIESN_TELNET_PASSWORD_FILE"]["notes"]
    assert records["BASSWIESN_OFFLINE_MODE"]["allowed_values"] == "off | auto | strict"
    assert "PROTECTED_DEVICE_IDS" in records
    assert records["MUSIC_ASSISTANT_TOKEN"]["security_relevance"] == "hoch"
    assert records["MUSIC_ASSISTANT_TOKEN"]["default"] == ""
    assert "basswiesn/app/services/providers/runtime.py" in records["MUSIC_ASSISTANT_TOKEN"]["sources"]


def test_provider_catalog_inventory_distinguishes_outbound_reads_from_radio_writes():
    rows = [row for row in build_api_inventory(ROOT)["routes"] if row["module"].endswith(".providers")]
    assert len(rows) == 14  # Seven web routes, on HTTP and optional HTTPS.
    for row in rows:
        assert not row["hardware_contact_possible"]
        assert not row["database_change_possible"]
        assert row["provider_contact_possible"] == (row["method"] == "POST")
        assert row["device_policy_required"] == (row["method"] == "POST")
        assert row["required_mode_access"] == "ADMIN_SESSION"
        assert row["automation_token_scope"] is None


def test_dlna_library_discovery_preserves_renderer_route_and_provider_boundary():
    from basswiesn.app.main import create_web_app
    from basswiesn.app.routers import dlna, fulltest
    from fastapi.routing import iter_route_contexts
    from starlette.routing import Match

    app = create_web_app(background_tasks=False)
    for path, handler in (("/api/dlna/discover", fulltest.dlna_discover),
                          ("/api/dlna/library/discover", dlna.discover)):
        scope = {"type": "http", "method": "POST", "path": path, "root_path": ""}
        matches = [route.matches(scope)[1] for route in iter_route_contexts(app.routes)
                   if route.matches(scope)[0] is Match.FULL]
        assert len(matches) == 1
        assert matches[0]["endpoint"] is handler
    rows = [row for row in build_api_inventory(ROOT)["routes"]
            if row["module"] == "basswiesn.app.routers.dlna"]
    for row in rows:
        assert not row["hardware_contact_possible"]
        assert row["provider_contact_possible"] == (row["handler"] in {
            "discover", "connect", "browse", "import_item", "audio",
        })
        assert row["database_change_possible"] == (row["handler"] in {
            "settings", "connect", "forget", "import_item",
        })
        if row["handler"] == "discover":
            assert row["required_mode_access"] == "ADMIN_SESSION"
            assert row["automation_token_scope"] is None
            assert row["device_policy_required"]
            assert {400, 409, 422, 503} <= set(row["expected_status_codes"])


def test_hardware_script_is_explicitly_gated_and_never_uses_ad_hoc_clients():
    script = (ROOT / "tools/test_hardware.sh").read_text(encoding="utf-8")
    assert "Hardware tests were safely blocked" in script
    assert "BASSWIESN_HARDWARE_CONFIRM" in script
    assert "PROTECTED_DEVICE_IPS" in script
    assert "PROTECTED_DEVICE_IDS" in script
    assert "Do not source .env" in script
    assert "-m hardware" in script
    assert "curl" not in script


def test_native_search_remains_a_guarded_radio_query_after_extraction():
    rows = [row for row in build_api_inventory(ROOT)["routes"] if row["handler"] == "native_station_search"]
    assert len(rows) == 2
    for row in rows:
        assert row["hardware_contact_possible"] and row["device_policy_required"]
        assert not row["database_change_possible"]
        assert row["required_mode_access"] == "ADMIN_SESSION"
        assert row["automation_token_scope"] is None
        assert {400, 403, 409, 502} <= set(row["expected_status_codes"])


def test_generated_json_is_valid_and_has_no_secret_values():
    generated = {
        "api-contract-matrix.json": build_api_inventory(ROOT),
        "device-access-inventory.json": build_device_inventory(ROOT),
        "configuration-inventory.json": build_config_inventory(ROOT),
    }
    for name, expected in generated.items():
        path = ROOT / "docs/generated" / name
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
        assert payload == expected
        assert "secret-token" not in text
        assert "topsecret" not in text
        assert str(ROOT) not in text


def test_contract_preflight_rejects_missing_or_stale_output(monkeypatch, tmp_path):
    import sys
    import tools.generate_phase4a_contracts as generator

    monkeypatch.setattr(generator, "generate", lambda root: ({}, {}, []))
    for name in ("render_api", "render_device", "render_config"):
        monkeypatch.setattr(generator, name, lambda value: "synthetic contract\n")
    def invoke(mode):
        monkeypatch.setattr(sys, "argv", ["contract-generator", "--root", str(tmp_path), mode])
        return generator.main()
    assert invoke("--check") == 1
    assert invoke("--write") == 0
    assert invoke("--check") == 0
    target = tmp_path / "docs/generated/api-contract-matrix.json"
    target.write_text("stale contract\n")
    assert invoke("--check") == 1
    assert target.read_text() == "stale contract\n"  # A check must not repair silently.


def test_static_inventory_covers_all_mounted_management_routes():
    from basswiesn.app.main import create_web_app

    app = create_web_app(background_tasks=False)
    documented = {
        (row["path"], row["method"])
        for row in build_api_inventory(ROOT)["routes"] if row["application"] == "webgui"
    }
    actual = {
        (path, method.upper())
        for path, methods in app.openapi()["paths"].items() if path.startswith("/api/")
        for method in methods if method in {"get", "post", "put", "patch", "delete"}
    }
    assert actual <= documented, f"Mounted management endpoints missing from trust inventory: {sorted(actual - documented)}"


def test_auth_inventory_never_exempts_a_whole_listener():
    rows = build_api_inventory(ROOT)["routes"]
    by_route = {(row["application"], row["method"], row["path"]): row for row in rows}
    assert by_route["cloud", "GET", "/api/cloud/stations"]["required_mode_access"] == "ADMIN_SESSION"
    assert by_route["diagnostics", "GET", "/requests"]["required_mode_access"] == "ADMIN_SESSION"
    assert by_route["webgui", "POST", "/auth/login"]["required_mode_access"] == "LOGIN_ORIGIN"
    assert by_route["webgui", "POST", "/auth/logout"]["required_mode_access"] == "ADMIN_SESSION"
    for listener in ("webgui", "https-webgui", "cloud", "diagnostics"):
        assert by_route[listener, "GET", "/ready"]["required_mode_access"] == "PUBLIC_MINIMAL_HEALTH"
        assert by_route[listener, "GET", "/ready"]["internal_readonly_attestation"] is True
        assert by_route[listener, "GET", "/live"]["internal_readonly_attestation"] is False
        assert by_route[listener, "GET", "/ready"]["expected_status_codes"] == [200, 403, 503]
    for listener in ("webgui", "cloud"):
        assert by_route[listener, "POST", "/streaming/support/power_on"]["required_mode_access"] == "DEVICE_CONTRACT"
