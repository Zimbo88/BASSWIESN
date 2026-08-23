import pytest

from basswiesn.app import config
from basswiesn.app.services.orion import StationDescriptor, OrionLocationError, decode_orion_data, encode_orion_data, playback_response, station_location
from basswiesn.app.services.xml import content_item_xml
from basswiesn.app.db.database import Base
from basswiesn.app.models import Setting
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture(autouse=True)
def clear_settings_cache():
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_orion_descriptor_roundtrip(monkeypatch):
    monkeypatch.setenv("BASSWIESN_LAN_HOST", "192.168.50.77")
    descriptor = StationDescriptor("OPB", "http://example.test/stream.aac", "http://example.test/logo.png")
    encoded = encode_orion_data(descriptor)
    decoded = decode_orion_data(encoded)
    assert decoded["name"] == "OPB"
    assert decoded["streamUrl"] == "http://example.test/stream.aac"
    location = station_location(descriptor)
    assert location.startswith("http://192.168.50.77:1516/")
    assert "core02/svc-bmx-adapter-orion/prod/orion/station?data=" in location
    assert not location.startswith("/core02")


def test_station_location_rejects_missing_safe_host(monkeypatch):
    monkeypatch.delenv("BASSWIESN_LAN_HOST", raising=False)
    monkeypatch.delenv("BASSWIESN_LOCAL_BASE_URL", raising=False)
    monkeypatch.setattr(config, "_default_lan_host", lambda: "")
    config.get_settings.cache_clear()

    with pytest.raises(OrionLocationError, match="BASSWIESN Host IP setzen"):
        station_location(StationDescriptor("OPB", "http://example.test/stream.aac"))


def test_station_location_current_request_host_wins_over_stale_saved_host(monkeypatch):
    monkeypatch.setenv("BASSWIESN_LAN_HOST", "192.168.50.20")
    monkeypatch.setenv("BASSWIESN_LOCAL_BASE_URL", "http://192.168.50.20:1516")
    config.get_settings.cache_clear()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Setting(key="lan_host", value="192.168.50.77"))
    db.commit()

    location = station_location(
        StationDescriptor("Current", "http://example.test/current.mp3"),
        db=db,
        request_host="192.168.50.185:1328",
    )

    assert location.startswith("http://192.168.50.185:1516/")
    db.close()


def test_content_item_rejects_relative_orion_location():
    station = type("StationLike", (), {"name": "Radio", "image_url": ""})()

    with pytest.raises(ValueError, match="BASSWIESN Host IP setzen"):
        content_item_xml(station, "/core02/svc-bmx-adapter-orion/prod/orion/station?data=x")


def test_orion_direct_stream_is_not_marked_as_playlist():
    payload = playback_response(StationDescriptor("Bayern 1", "http://example.test/live.mp3"))

    assert payload["audio"]["hasPlaylist"] is False
    assert payload["audio"]["streams"][0]["hasPlaylist"] is False
    assert len(payload["audio"]["streams"]) >= 1000
    assert {row["streamUrl"] for row in payload["audio"]["streams"]} == {"http://example.test/live.mp3"}


def test_orion_playlist_stream_is_marked_as_playlist():
    payload = playback_response(StationDescriptor("Playlist", "http://example.test/live.m3u8?token=1"))

    assert payload["audio"]["hasPlaylist"] is True
    assert payload["audio"]["streams"][0]["hasPlaylist"] is True
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
