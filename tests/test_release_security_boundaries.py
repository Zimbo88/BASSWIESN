"""Regression checks for the final static-analysis findings; no device I/O."""
from html import unescape
from xml.etree.ElementTree import ParseError

import pytest

from basswiesn.app.config import get_settings
from basswiesn.app.core.xml_safety import parse_xml
from basswiesn.app.services import local_updates, telemetry_analysis
from basswiesn.app.services.preset_contract import preset_summaries_from_xml
from basswiesn.app.services.settings_contract import read_setting
from basswiesn.app.adapters.soundtouch_client import _xml_root_name

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
@pytest.mark.parametrize("declaration", [
    '<!DOCTYPE volume [<!ENTITY secret "injected">]>',
    '<!DOCTYPE volume SYSTEM "file:///private-not-read">',
    '<!DOCTYPE volume>',
])
def test_protocol_parser_rejects_dtd_and_entities(encoding, declaration):
    value = (declaration + '<volume><actualvolume>5</actualvolume></volume>').encode(encoding)
    with pytest.raises(ParseError, match="Unsafe XML declarations"):
        parse_xml(value)


def test_protocol_parser_keeps_namespaces_unicode_and_standard_entities():
    root = parse_xml('<?xml version="1.0"?><info xmlns="urn:example"><name>Küche &amp; Büro</name></info>')
    assert root.findtext("{urn:example}name") == "Küche & Büro"
    assert read_setting("volume", "<volume><actualvolume>5</actualvolume></volume>") == 5


def test_rejected_protocol_xml_preserves_existing_fail_closed_contracts():
    value = '<!DOCTYPE presets [<!ENTITY x "bad">]><presets>&x;</presets>'
    assert preset_summaries_from_xml(value) == []
    assert _xml_root_name(value) == ""
    with pytest.raises(ParseError):
        read_setting("volume", value)


def test_telemetry_html_escapes_both_query_and_stored_values(monkeypatch):
    attack = '</pre><script>alert("not executable")</script><img src=x onerror=alert(1)>'
    monkeypatch.setattr(telemetry_analysis, "telemetry_summary", lambda *args: {"station": attack})
    page = telemetry_analysis.telemetry_report_html(None, attack)
    assert "<script>" not in page and "<img " not in page
    assert "&lt;script&gt;" in page
    assert attack in unescape(page)


def test_discovery_diagnostics_keep_fixed_identity_errors_not_exception_payloads():
    from basswiesn.app.routers.setup_rebuild import _discovery_failure_reason
    message = "Die Geräte-ID aus /info stimmt nicht mit dem SSDP-Ergebnis überein."
    assert _discovery_failure_reason(ValueError(message)) == message
    assert _discovery_failure_reason(OSError("password=private-fixture /private/path")) == "IDENTITY_READ_FAILED (OSError)"


@pytest.mark.parametrize("kind", ["absolute", "relative", "symlink", "wrong_suffix"])
def test_update_preview_rejects_unstaged_files_before_hashing(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")
    root = get_settings().data_dir / "uploads" / "updates"
    root.mkdir(parents=True)
    outside = tmp_path / "private.tar.gz"
    outside.write_bytes(b"not permitted to read")
    target = outside
    if kind == "relative":
        target = root / ".." / ".." / ".." / ".." / "private.tar.gz"
    elif kind == "symlink":
        target = root / "linked.tar.gz"
        target.symlink_to(outside)
    elif kind == "wrong_suffix":
        target = root / "secret.txt"
        target.write_text("not an archive")
    def forbidden(*args):
        pytest.fail("rejected path reached file hashing")
    monkeypatch.setattr(local_updates, "_sha256", forbidden)
    result = local_updates.inspect_local_release_archive(str(target))
    assert result["ok"] is False and "sha256" not in result
