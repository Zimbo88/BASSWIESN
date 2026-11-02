from basswiesn.app.models import Device
from basswiesn.app.services.device_service import classify_marge_url, device_summary


def test_marge_url_mixed_and_bose_classification():
    assert classify_marge_url("http://content.api.bose.io:1328")[0] == "mixed"
    assert classify_marge_url("http://content.api.bose.io:1516")[0] == "mixed"
    assert classify_marge_url("https://content.api.bose.io")[0] == "bose"
    assert classify_marge_url("https://streaming.bose.com")[0] == "bose"


def test_marge_url_lan_classification():
    assert classify_marge_url("http://192.168.1.20:1516")[0] == "basswiesn"
    assert classify_marge_url("http://192.168.1.20:7777")[0] == "other"
    assert classify_marge_url("")[0] == "unknown"


def test_device_summary_exposes_mixed_status_and_reachable():
    info = '<info deviceID="MIX1"><name>Mix</name><type>SoundTouch 20</type><margeURL>http://content.api.bose.io:1328</margeURL></info>'
    summary = device_summary(Device(device_id="MIX1", ip_address="192.0.2.12", info_xml=info, reachable=True))

    assert summary["configured_for"] == "mixed"
    assert summary["config_status"] == "mixed route / invalid cloud target"
    assert summary["reachable"] is True
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
