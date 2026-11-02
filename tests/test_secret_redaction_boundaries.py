"""Synthetic canaries prove redaction at logs, API errors and support exports."""
from __future__ import annotations

import json
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from basswiesn.app.core import masterlog
from basswiesn.app.core.errors import DeviceUnavailable, error_response
from basswiesn.app.services import device_interactions, support_export

pytestmark = pytest.mark.unit


def _dummy_key_envelope(kind: str, *, complete: bool = True) -> str:
    # Construct the synthetic envelope at runtime, keeping full private-key
    # scanning enabled for committed fixtures. This is not valid key material.
    label = f"{kind} KEY"
    parts = [f"-----BEGIN {label}-----", "canary-sensitive-value"]
    if complete:
        parts.append(f"-----END {label}-----")
    return "\n".join(parts)


@pytest.mark.parametrize("payload", [
    "secret: canary-sensitive-value",
    'token="canary-sensitive-value with spaces"',
    '{"api_key":"canary-sensitive-value"}',
    '{"refresh_token":"canary-sensitive-value"}',
    "Authorization: Bearer canary-sensitive-value",
    "Cookie: session=canary-sensitive-value; other=value",
    "https://user:canary-sensitive-value@example.invalid/stream",
    _dummy_key_envelope("PRIVATE"),
    _dummy_key_envelope("OPENSSH PRIVATE", complete=False),
    '<pairingSecret>canary-sensitive-value</pairingSecret>',
    "https://example.invalid/audio?token=canary-sensitive-value",
    'password="canary-sensitive-value with no closing quote',
    "'password': 'canary-sensitive-value with no closing quote",
    'password="canary-sensitive-value with trailing escape\\',
    "password='canary-sensitive-value with trailing escape\\",
    'password="canary-sensitive-value with escaped \\" quote and no closing quote',
    'password="canary-sensitive-value\nmultiline truncated',
    '<password>canary-sensitive-value truncated XML',
    '<root password="canary-sensitive-value truncated attribute',
    'Authorization: Bearer fixture\n canary-sensitive-value folded header',
])
@pytest.mark.parametrize("sink", [
    support_export.redact_text,
    device_interactions._redact,
    lambda text: json.dumps(masterlog._sanitize({"error": text})),
])
def test_free_form_secret_values_never_reach_diagnostic_sink(payload, sink):
    assert "canary-sensitive-value" not in sink(payload)


@pytest.mark.parametrize("key", ["Cookie", "api-key", "refresh_token", "private_key", "sessionCookie"])
def test_nested_secret_keys_are_redacted(key):
    payload = {"event": {key: "canary-sensitive-value", "ok": True}}
    assert "canary-sensitive-value" not in json.dumps(masterlog._sanitize(payload))
    assert "canary-sensitive-value" not in json.dumps(support_export.redact_payload(payload))


def test_safe_protocol_text_and_structured_fields_are_preserved():
    payload = {"status": "PLAY_STATE", "volume": 1, "verified": False, "error": "readback timeout"}
    assert masterlog._sanitize(payload) == payload
    assert support_export.redact_payload(payload) == payload


@pytest.mark.parametrize("address", ["192.0.2.42", "2001:db8::42", "::ffff:192.0.2.42"])
def test_support_export_redacts_both_ip_families(address):
    assert address not in support_export.redact_text(f"peer=[{address}]")
    assert address in support_export.redact_text(f"peer=[{address}]", anonymize_ips=False)


def test_redacted_structured_text_remains_parseable():
    source = {"token": "canary-sensitive-value", "url": "https://example.invalid/secret/canary-sensitive-value"}
    parsed = json.loads(support_export.redact_text(json.dumps(source)))
    assert parsed["token"] == "***REDACTED***"
    root = ET.fromstring(support_export.redact_text('<root token="canary-sensitive-value"><secret>canary-sensitive-value</secret></root>'))
    assert root.attrib["token"] == "***REDACTED***"
    assert root.findtext("secret") == "***REDACTED***"


def test_api_error_does_not_expose_credential_in_transport_exception():
    response = error_response(DeviceUnavailable("https://user:canary-sensitive-value@example.invalid/ failed"))
    assert b"canary-sensitive-value" not in response.body
    assert json.loads(response.body)["error"]["code"] == "DEVICE_UNREACHABLE"


def test_byte_payload_cannot_bypass_support_redaction():
    bundle = support_export.build_support_bundle({"error.txt": b"secret: canary-sensitive-value"}, max_bytes=4096)
    with ZipFile(bundle) as archive:
        assert b"canary-sensitive-value" not in archive.read("error.txt")


def test_support_json_remains_valid_and_redacts_non_string_secrets():
    bundle = support_export.build_support_bundle({"state.json": '{"password":123456,"status":"ok"}'}, max_bytes=4096)
    with ZipFile(bundle) as archive:
        data = json.loads(archive.read("state.json"))
    assert data == {"password": "***REDACTED***", "status": "ok"}


def test_uninspectable_binary_is_not_a_safe_support_payload():
    with pytest.raises(ValueError, match="UTF-8"):
        support_export.build_support_bundle({"opaque.bin": b"\xff\x00\xfe"}, max_bytes=4096)
