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


def test_configuration_inventory_contains_aliases_and_secret_boundaries():
    records = {item["name"]: item for item in build_config_inventory(ROOT)}
    assert "PROTECTED_DEVICE_IPS" in records
    assert "BASSWIESN_PROTECTED_DEVICE_IPS" in records
    assert records["BASSWIESN_TELNET_PASSWORD_FILE"]["security_relevance"] == "hoch"
    assert records["BASSWIESN_TELNET_PASSWORD_FILE"]["notes"]
    assert records["BASSWIESN_OFFLINE_MODE"]["allowed_values"] == "off | auto | strict"


def test_hardware_script_is_explicitly_gated_and_never_uses_ad_hoc_clients():
    script = (ROOT / "tools/test_hardware.sh").read_text(encoding="utf-8")
    assert "Hardware tests were safely blocked" in script
    assert "BASSWIESN_HARDWARE_CONFIRM" in script
    assert "PROTECTED_DEVICE_IPS" in script
    assert "PROTECTED_DEVICE_IDS" in script
    assert "Do not source .env" in script
    assert "-m hardware" in script
    assert "curl" not in script


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
