import pytest
from fastapi import HTTPException

from basswiesn.app.routers.api import (
    _battery_metadata,
    _build_display_status_label,
    _direct_metadata_select_xml,
    _add_station_xml,
    _name_source_plan_xml,
    _network_signal_metadata,
    _parse_bass_capabilities,
    _parse_search_station_results,
    _parse_zone_summary,
    _search_station_xml,
    _now_playing_metadata,
    _setting_payload,
    _validate_device_name,
)


def test_bass_payload_stockholm_range():
    path, xml = _setting_payload("bass", -9)
    assert path == "/bass"
    assert xml == "<bass>-9</bass>"
    path, xml = _setting_payload("bass", 0)
    assert xml == "<bass>0</bass>"


def test_bass_payload_rejects_positive_values():
    with pytest.raises(HTTPException):
        _setting_payload("bass", 1)


def test_clock_display_payload_uses_clock_config():
    path, xml = _setting_payload("clockDisplay", "false")
    assert path == "/clockDisplay"
    assert xml == '<clockDisplay><clockConfig userEnable="false" /></clockDisplay>'


def test_power_saving_payload_uses_systemtimeout():
    path, xml = _setting_payload("powersaving", "false")
    assert path == "/systemtimeout"
    assert xml == "<systemtimeout><powersaving_enabled>false</powersaving_enabled></systemtimeout>"


def test_device_name_validation_rejects_stockholm_unsafe_chars():
    assert _validate_device_name("Wohnzimmer") == "Wohnzimmer"
    for value in ["Bad<Name", "Bad>Name", "Bad&Name", 'Bad"Name', ""]:
        with pytest.raises(HTTPException):
            _validate_device_name(value)


def test_language_payload_accepts_stockholm_language_and_rejects_unknown():
    path, xml = _setting_payload("language", "zh_hans")
    assert path == "/language"
    assert xml == "<sysLanguage>10</sysLanguage>"
    with pytest.raises(HTTPException):
        _setting_payload("language", "xx")


def test_display_nowplaying_parser_prefers_station_and_content_item():
    xml = '<nowPlaying source="LOCAL_INTERNET_RADIO" preset="2"><ContentItem><itemName>Fallback</itemName></ContentItem><stationName>Radio Eins</stationName><track>Song</track><artist>Artist</artist><album>Album</album><art artImageStatus="IMAGE_PRESENT">http://radio.example/art.svg</art><playStatus>PLAY_STATE</playStatus></nowPlaying>'
    parsed = _now_playing_metadata(xml)
    assert parsed["source"] == "LOCAL_INTERNET_RADIO"
    assert parsed["station"] == "Radio Eins"
    assert parsed["preset"] == "2"
    assert parsed["album"] == "Album"
    assert parsed["image_url"] == "http://radio.example/art.svg"
    assert parsed["playback_state"] == "playing"
    assert parsed["label"] == "Radio Eins"


def test_display_network_parser_maps_wifi_signal():
    xml = '<networkInfo><interface type="WIFI_INTERFACE" state="NETWORK_WIFI_CONNECTED" ssid="Bass" signal="GOOD_SIGNAL" /></networkInfo>'
    parsed = _network_signal_metadata(xml)
    assert parsed["kind"] == "wifi"
    assert parsed["percent"] == 80
    assert parsed["label"] == "WiFi 80%"


def test_display_battery_parser_handles_tag_and_warns_about_portable_reliability():
    parsed = _battery_metadata('<powerManagement><percentCharge>94</percentCharge><runningOnBattery>true</runningOnBattery></powerManagement>')
    assert parsed["percent"] == "94"
    assert parsed["running_on_battery"] == "true"
    assert parsed["level_bucket"] == 100
    assert "ba 8" in parsed["reliability"]


def test_direct_metadata_select_uses_normal_station_name_without_fake_overlay():
    xml = _direct_metadata_select_xml("Radio Eins", "Radio Eins | 16.06 20:15 | WiFi 80% | Bat 94%", "http://example.test/live.mp3")
    assert xml.startswith('<ContentItem source="LOCAL_INTERNET_RADIO"')
    assert '<itemName>Radio Eins</itemName>' in xml
    assert 'WiFi 80%' not in xml
    assert 'vm btesttext' not in xml


def test_display_status_builder_combines_clock_and_wifi_without_battery_mode():
    text = _build_display_status_label(
        "station_clock_wifi",
        {"label": "Radio Eins"},
        {"label": "WiFi 80%"},
        {"label": "Bat 94%"},
        include_date=False,
    )
    assert text.startswith("Radio Eins | ")
    assert "WiFi 80%" in text
    assert "Bat 94%" not in text


def test_native_station_search_xml_escapes_query_and_account():
    xml = _search_station_xml("TUNEIN", "", "Rock & Roll")
    assert xml == '<search source="TUNEIN">Rock &amp; Roll</search>'


def test_native_add_station_xml_matches_confirmed_shape():
    xml = _add_station_xml("TUNEIN", "", "station123", "Radio Station")
    assert xml == '<addStation source="TUNEIN" token="station123"><name>Radio Station</name></addStation>'


def test_native_station_result_parser_groups_results():
    parsed = _parse_search_station_results('<results source="TUNEIN"><stations><searchResult source="TUNEIN" token="s1"><name>Jazz FM</name><logo>http://logo</logo></searchResult></stations></results>')
    assert parsed["stations"][0]["token"] == "s1"
    assert parsed["stations"][0]["name"] == "Jazz FM"


def test_bass_capabilities_parser_reads_model_range():
    parsed = _parse_bass_capabilities('<bassCapabilities deviceID="abc"><bassAvailable>true</bassAvailable><bassMin>-9</bassMin><bassMax>0</bassMax><bassDefault>-2</bassDefault></bassCapabilities>')
    assert parsed["bassAvailable"] is True
    assert parsed["bassMin"] == -9
    assert parsed["bassMax"] == 0
    assert parsed["bassDefault"] == -2


def test_zone_summary_parser_reads_members():
    parsed = _parse_zone_summary('<zone master="M1"><member ipaddress="192.168.1.20">S1</member></zone>')
    assert parsed["master"] == "M1"
    assert parsed["members"] == [{"deviceID": "S1", "ipaddress": "192.168.1.20"}]


def test_name_source_plan_is_plan_xml_only():
    xml = _name_source_plan_xml("AUX", "Plattenspieler")
    assert xml == '<nameSource source="AUX"><name>Plattenspieler</name></nameSource>'
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
