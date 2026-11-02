"""Protocol projections must preserve unknown data and reject false integrity."""
import xml.etree.ElementTree as ET

from hypothesis import given, strategies as st
import pytest

from basswiesn.app.services import preset_contract as contract

pytestmark = pytest.mark.unit


@given(st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=120))
def test_unknown_fields_survive_content_edit_and_names_are_xml_escaped(name):
    original = '<ContentItem xmlns:x="urn:fixture" custom="keep"><x:unknown value="keep"/><itemName>Old</itemName><containerArt>old</containerArt></ContentItem>'
    xml = contract._preset_content_item_xml(
        {"content_item_xml": original}, location="http://example.invalid/live", source="LOCAL_INTERNET_RADIO",
        source_account="stale", name=name, art="http://example.invalid/cover", include_art=True,
    )
    root = ET.fromstring(xml)
    assert root.get("custom") == "keep"
    assert root.find("{urn:fixture}unknown").get("value") == "keep"
    assert root.findtext("itemName") == (name or "Unbenanntes Preset")
    assert root.get("sourceAccount") == ""
    assert root.findtext("containerArt") == "http://example.invalid/cover"


@pytest.mark.parametrize("existing,empty,expected", [(False, True, True), (False, False, False), (True, False, False)])
def test_artwork_absence_and_explicit_empty_are_different(existing, empty, expected):
    old = '<ContentItem><containerArt>old</containerArt></ContentItem>' if existing else "malformed"
    xml = contract._preset_content_item_xml({"content_item_xml": old}, location="example", source="TUNEIN",
        source_account="account", name="Example", art="", empty_art=empty, canonicalize_source_account=False)
    root = ET.fromstring(xml)
    assert (root.find("containerArt") is not None) is expected
    assert root.get("sourceAccount") == "account"


def test_malformed_preset_snapshot_cannot_verify_expected_target():
    report = contract.compare_preset_snapshots("<presets/>", "<broken", {1: "example"})
    assert report["overall_status"] == "partial_failure"
    assert report["manual_review_required"]


def test_unrequested_slot_addition_requires_reconciliation():
    after = '<presets><preset id="2"><ContentItem location="example"/></preset></presets>'
    report = contract.compare_preset_snapshots("<presets/>", after, {})
    assert report["overall_status"] == "integrity_failure"
    assert report["unexpected_changes"][0]["reason"] == "unexpected slot added"
    assert report["rollback_recommended"]


@pytest.mark.parametrize("location", ["http://[", "http://example.invalid/other", "http://example.invalid/core02/svc-bmx-adapter-orion/prod/orion/station", "http://example.invalid/core02/svc-bmx-adapter-orion/prod/orion/station?data=bad"])
def test_unparseable_station_hint_does_not_prove_equivalence(location):
    assert contract._decoded_orion_station(location) == {}
    assert not contract._locations_select_same_station(location, "")


def test_empty_or_invalid_locations_do_not_verify_equality():
    assert not contract._locations_match("", "")
    assert not contract._locations_match("http://[", "example")
    assert not contract._preset_contract_matches(None, {"location": "example"})
    assert contract._child_text(None, "itemName") == ""
    assert contract._canonical_xml("  <broken  ") == "<broken"


def test_new_artwork_node_is_added_without_losing_unknown_fields():
    xml = contract._preset_content_item_xml({"content_item_xml": '<ContentItem custom="keep"/>'},
        location="example", source="TUNEIN", source_account="account", name="Example",
        art="http://example.invalid/image.png", include_art=True)
    root = ET.fromstring(xml)
    assert root.findtext("containerArt") == "http://example.invalid/image.png"
    assert root.get("custom") == "keep"


def test_legacy_list_only_result_does_not_claim_full_integrity():
    result = contract._integrity_for_sync_result([{"button": 1, "location": "example"}], {1: "example"})
    assert result["target_verified"] is True
    assert result["untouched_slots_verified"] is False
    assert result["overall_status"] == "manual_review_required"
