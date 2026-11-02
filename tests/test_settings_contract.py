"""Pure, typed settings contract: actual state, strict shapes and partial clocks."""

from xml.etree import ElementTree as ET

import pytest
from hypothesis import given, strategies as st

from basswiesn.app.services.settings_contract import device_name, plan_setting, read_setting

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("setting,xml", [
    ("volume", "<volume/>"), ("volume", "<volume><targetvolume>1</targetvolume></volume>"),
    ("bass", "<bass><targetbass>-2</targetbass></bass>"), ("clockDisplay", "<clockDisplay/>"),
    ("clockDisplay", "<clockDisplay><clockConfig/></clockDisplay>"),
    ("clockConfig", "<clockDisplay/>"), ("powersaving", "<systemtimeout/>"),
    ("language", "<sysLanguage/>"), ("name", "<info/>"),
    ("rebroadcastlatencymode", "<rebroadcastlatencymode/>"),
    ("volume", "<error/>"), ("systemtimeout", "<systemtimeout><unsupported/></systemtimeout>"),
    ("UNKNOWN", "<UNKNOWN/>"),
])
def test_absent_fields_are_never_manufactured_as_zero_or_false(setting, xml):
    with pytest.raises(ValueError):
        read_setting(setting, xml)


@pytest.mark.parametrize("setting,value,xml,expected", [
    ("volume", 0, "<volume><targetvolume>10</targetvolume><actualvolume>0</actualvolume></volume>", 0),
    ("bass", -9, "<bass><actualbass>-9</actualbass></bass>", -9),
    ("powersaving", False, "<systemtimeout><powersaving_enabled>false</powersaving_enabled></systemtimeout>", False),
    ("language", "nb", "<sysLanguage>18</sysLanguage>", "no"),
    ("clockDisplay", False, '<clockDisplay><clockConfig userEnable="false"/></clockDisplay>', False),
    ("rebroadcastlatencymode", "SYNC_TO_ROOM", '<rebroadcastlatencymode mode="SYNC_TO_ROOM"/>', "SYNC_TO_ROOM"),
    ("auto_standby_minutes", 0, "<systemtimeout>0</systemtimeout>", 0),
    ("name", "Example Radio", "<info><name>Example Radio</name></info>", "Example Radio"),
])
def test_valid_shapes_roundtrip(setting, value, xml, expected):
    plan = plan_setting(setting, value)
    assert plan.matches(read_setting(setting, xml))
    assert plan.expected == expected


def test_partial_clock_config_never_adds_unrequested_defaults():
    plan = plan_setting("clockConfig", {"brightnessLevel": 0, "userEnable": False})
    cfg = ET.fromstring(plan.xml).find("clockConfig")
    assert cfg.attrib == {"brightnessLevel": "0", "userEnable": "false"}
    assert plan.matches({**cfg.attrib, "timezoneInfo": "Europe/Berlin"})
    assert not plan.matches({"brightnessLevel": "0"})
    assert not plan.matches(False)


@pytest.mark.parametrize("previous,accepted", [
    ({"brightnessLevel": "70"}, True),
    ({"brightnessLevel": "70", "userUtcTime": "1001"}, True),
    ({"brightnessLevel": "70", "userUtcTime": "1000"}, False),
    ({"brightnessLevel": 70}, False),
    ({"brightnessLevel": "71"}, False), ({}, False),
    ({"timezoneInfo": "Europe/Berlin"}, False), (None, False),
])
def test_clock_preconditions_cover_every_overwritten_attribute(previous, accepted):
    plan = plan_setting("clockConfig", {"brightnessLevel": 0})
    actual = {"brightnessLevel": "70", "userUtcTime": "1001", "timezoneInfo": "Europe/Berlin"}
    assert plan.matches_previous(actual, previous) is accepted
    assert not plan.matches_previous(False, previous)


def test_previous_values_preserve_exact_types():
    assert plan_setting("volume", 1).matches_previous(0, 0)
    assert not plan_setting("volume", 1).matches_previous(0, False)


@pytest.mark.parametrize("value", ["bad\uffff", "bad\ufffe", "bad\ud800", "bad\x00", "bad\nname"])
def test_names_reject_xml_invalid_codepoints(value):
    with pytest.raises(ValueError):
        device_name(value)


@pytest.mark.parametrize("value", [{"unexpected": 1}, {"timeFormat": "unknown"}, {"userEnable": "maybe"}, {"userUtcTime": 1.5}, {}])
def test_invalid_clock_fields_rejected(value):
    with pytest.raises(ValueError):
        plan_setting("clockConfig", value)


@given(st.integers(min_value=0, max_value=100))
def test_volume_roundtrip_and_actual_target_disagreement(value):
    plan = plan_setting("volume", value)
    assert plan.xml == f"<volume>{value}</volume>"
    other = (value + 1) % 101
    assert not plan.matches(read_setting("volume", f"<volume><targetvolume>{value}</targetvolume><actualvolume>{other}</actualvolume></volume>"))


@given(st.text(max_size=80))
def test_accepted_names_are_xml_text_not_injection(value):
    try:
        accepted = device_name(value)
    except ValueError:
        return
    # XML rejects surrogate/control/noncharacters, even when not markup.
    plan = plan_setting("name", accepted)
    assert ET.fromstring(plan.xml).text == accepted
