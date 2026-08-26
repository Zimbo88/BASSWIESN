import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_cloud_app, create_web_app
from basswiesn.app.models import Device, Preset, RuntimeState, Setting, Station
from basswiesn.app.routers import stations_presets
from basswiesn.app.routers.stations_presets import _stream_source_from_sources_xml, import_presets_from_radio_backup
from basswiesn.app.services.orion import ORION_STATION_PATH, StationDescriptor, decode_orion_data, encode_orion_data
from basswiesn.app.services.provider_registry import RECOMMENDED_SOURCE_TYPES, persistence_sources_xml
from basswiesn.app.services.stream_compat import StreamAnalysis
from basswiesn.app.services.xml import content_item_xml


def _orion_payload(location: str) -> dict:
    data = parse_qs(urlparse(location).query).get("data", [""])[0]
    return decode_orion_data(data) if data else {}


def test_cloud_orion_rejects_malformed_descriptor_as_client_error():
    with TestClient(create_cloud_app()) as client:
        response = client.get(f"{ORION_STATION_PATH}?data=p2JhZA")

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid Orion station descriptor"


def test_device_presets_cloud_route_returns_marge_xml_and_etag():
    db = app_db.SessionLocal()
    station = Station(name="Test Radio", stream_url="http://example.test/live.mp3", image_url="http://example.test/logo.png")
    db.add_all([
        Device(device_id="RADIO1", ip_address="127.0.0.1"),
        station,
        Setting(key="lan_host", value="192.168.50.77"),
    ])
    db.flush()
    db.add(Preset(
        device_id="RADIO1",
        button=1,
        station_id=station.id,
        source="LOCAL_INTERNET_RADIO",
        location="http://cloud.test/station/1",
        content_item_xml='<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://cloud.test/station/1"><itemName>Test Radio</itemName></ContentItem>',
    ))
    db.commit()
    db.close()

    with TestClient(create_cloud_app()) as client:
        response = client.get("/streaming/account/123/device/RADIO1/presets")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.bose.streaming-v1.2+xml")
    assert response.headers["etag"]
    root = ET.fromstring(response.text)
    preset = root.find("preset")
    assert preset is not None
    assert preset.attrib["buttonNumber"] == "1"
    assert preset.findtext("location").startswith(f"http://192.168.50.77:1516{ORION_STATION_PATH}?data=")
    assert _orion_payload(preset.findtext("location"))["imageUrl"] == ""
    assert preset.findtext("name") == "Test Radio"
    assert preset.findtext("containerArt", default="") == ""
    assert preset.find("source").attrib["id"] == "10003"
    assert preset.findtext("source/username", default="") == ""


def test_device_presets_cloud_route_sends_logo_only_when_enabled():
    db = app_db.SessionLocal()
    station = Station(name="Logo Radio", stream_url="http://example.test/logo.mp3", image_url="http://example.test/logo.png")
    db.add_all([
        Device(device_id="RADIO_LOGO", ip_address="127.0.0.1"),
        station,
        Setting(key="station_art_mode:RADIO_LOGO", value="station_logo"),
    ])
    db.flush()
    db.add(Preset(
        device_id="RADIO_LOGO",
        button=2,
        station_id=station.id,
        source="LOCAL_INTERNET_RADIO",
        location="http://cloud.test/station/logo",
        content_item_xml='<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://cloud.test/station/logo"><itemName>Logo Radio</itemName></ContentItem>',
    ))
    db.commit()
    db.close()

    with TestClient(create_cloud_app()) as client:
        streaming_response = client.get("/streaming/account/123/device/RADIO_LOGO/presets")
        marge_response = client.get("/v1/systems/devices/RADIO_LOGO/presets")

    assert streaming_response.status_code == 200
    assert marge_response.status_code == 200
    assert ET.fromstring(streaming_response.text).findtext("preset/containerArt") == "http://example.test/logo.png"
    streaming_location = ET.fromstring(streaming_response.text).findtext("preset/location")
    assert _orion_payload(streaming_location)["imageUrl"] == "http://example.test/logo.png"
    marge_item = ET.fromstring(marge_response.text).find("preset/ContentItem")
    assert marge_item is not None
    assert marge_item.findtext("containerArt") == "http://example.test/logo.png"
    assert _orion_payload(marge_item.attrib["location"])["imageUrl"] == "http://example.test/logo.png"


def test_marge_presets_strip_stored_logo_when_radio_symbol_is_selected():
    db = app_db.SessionLocal()
    db.add_all([
        Device(device_id="RADIO_SYMBOL", ip_address="127.0.0.1"),
        Preset(
            device_id="RADIO_SYMBOL",
            button=1,
            source="LOCAL_INTERNET_RADIO",
            location="http://cloud.test/station/1",
            content_item_xml=(
                '<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://cloud.test/station/1">'
                '<itemName>Symbol Radio</itemName><containerArt>http://example.test/logo.png</containerArt></ContentItem>'
            ),
        ),
    ])
    db.commit()
    db.close()

    with TestClient(create_cloud_app()) as client:
        response = client.get("/v1/systems/devices/RADIO_SYMBOL/presets")

    assert response.status_code == 200
    item = ET.fromstring(response.text).find("preset/ContentItem")
    assert item is not None
    assert item.find("containerArt") is None
    assert item.attrib["location"] == "http://cloud.test/station/1"


def test_preset_checker_matches_absolute_orion_location_with_encoded_data(monkeypatch):
    radio_xml = (
        '<presets><preset id="1"><wrapper><ContentItem source="LOCAL_INTERNET_RADIO" '
        'sourceAccount="" type="stationurl" '
        'location="http://basswiesn.test:1516/core02/svc-bmx-adapter-orion/prod/orion/station?data=station-1">'
        '<itemName>Radio Eins</itemName><containerArt>http://example.test/logo.png</containerArt>'
        '</ContentItem></wrapper></preset></presets>'
    )

    class Client:
        def __init__(self, _ip):
            pass

        async def get_xml(self, path):
            if path == "/presets":
                return radio_xml
            if path == "/sources":
                return '<sources><source source="LOCAL_INTERNET_RADIO" status="READY" /></sources>'
            if path == "/serviceAvailability":
                return '<services><service service="LOCAL_INTERNET_RADIO" status="READY" /></services>'
            raise AssertionError(path)

    async def reachable_stream(_url, timeout=3.0):
        return {
            "status": "VALID",
            "reachable": True,
            "reason": "test stream reachable",
            "http_status": 206,
            "codec": "mp3",
            "compatibility_score": 100,
        }

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    monkeypatch.setattr(stations_presets, "probe_stream_reachability", reachable_stream)
    db = app_db.SessionLocal()
    station = Station(name="Radio Eins", stream_url="http://example.test/live.mp3")
    db.add_all([Device(device_id="PRESETCHECK", ip_address="192.0.2.10"), station])
    db.flush()
    location = "http://other-host.test:1516/core02/svc-bmx-adapter-orion/prod/orion/station?data=station-1"
    db.add(Preset(device_id="PRESETCHECK", button=1, station_id=station.id, source="LOCAL_INTERNET_RADIO", location=location, content_item_xml=f'<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="{location}"><itemName>Radio Eins</itemName></ContentItem>'))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.get("/api/presets/PRESETCHECK/status?probe=true")

    assert response.status_code == 200
    slot = response.json()["slots"][0]
    assert slot["verdict"] == "VALID"
    assert slot["state"] == "valid"
    assert slot["location_match"] is True
    assert slot["radio"]["source_account"] == ""
    assert response.json()["slots"][1]["verdict"] == "VALID"


def test_passive_preset_status_uses_only_persisted_snapshot(monkeypatch):
    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("passive preset status constructed a radio transport")

    monkeypatch.setattr(stations_presets, "SoundTouchClient", ForbiddenClient)
    db = app_db.SessionLocal()
    db.add(Device(device_id="PRESET-PASSIVE", ip_address="192.0.2.44"))
    db.commit()
    db.close()

    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.get("/api/presets/PRESET-PASSIVE/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["probe_performed"] is False
    assert payload["radio_snapshot_source"] == ""
    assert "nicht ausdrücklich gelesen" in payload["radio_error"]


def test_import_presets_from_pre_redirect_orion_backup_rewrites_to_current_basswiesn_host():
    db = app_db.SessionLocal()
    db.add_all([
        Device(device_id="IMPORT1", ip_address="192.0.2.10"),
        Setting(key="lan_host", value="192.168.50.77"),
    ])
    descriptor = StationDescriptor(
        name="Bayern 3",
        stream_url="https://dispatcher.rndfnk.com/br/br3/live/mp3/mid",
        image_url="https://example.test/bayern3.png",
    )
    old_location = f"http://192.168.50.200:1516{ORION_STATION_PATH}?data={encode_orion_data(descriptor)}"
    xml = (
        '<?xml version="1.0" encoding="UTF-8" ?><presets>'
        f'<preset id="3"><ContentItem source="10003" type="stationurl" '
        f'location="{old_location}" sourceAccount="" isPresetable="true">'
        "<itemName>Bayern 3</itemName><containerArt>https://example.test/bayern3.png</containerArt>"
        "</ContentItem></preset></presets>"
    )

    result = import_presets_from_radio_backup(db, "IMPORT1", xml)
    db.commit()

    preset = db.query(Preset).filter(Preset.device_id == "IMPORT1", Preset.button == 3).one()
    station = db.query(Station).filter(Station.id == preset.station_id).one()
    db.close()

    assert result["source_count"] == 1
    assert result["imported_count"] == 1
    assert station.name == "Bayern 3"
    assert station.stream_url == "https://dispatcher.rndfnk.com/br/br3/live/mp3/mid"
    assert preset.source == "LOCAL_INTERNET_RADIO"
    assert 'source="LOCAL_INTERNET_RADIO"' in preset.content_item_xml
    assert 'source="10003"' not in preset.content_item_xml
    assert preset.location.startswith(f"http://192.168.50.77:1516{ORION_STATION_PATH}?data=")
    assert "192.168.50.200" not in preset.location
    assert "192.168.50.200" not in preset.content_item_xml
    assert "containerArt" not in preset.content_item_xml
    assert _orion_payload(preset.location)["imageUrl"] == ""


def test_import_presets_from_backup_keeps_logo_when_station_logo_enabled():
    db = app_db.SessionLocal()
    db.add_all([
        Device(device_id="IMPORT_LOGO", ip_address="192.0.2.10"),
        Setting(key="lan_host", value="192.168.50.77"),
        Setting(key="station_art_mode:IMPORT_LOGO", value="station_logo"),
    ])
    db.commit()
    descriptor = StationDescriptor(
        name="Bayern 3",
        stream_url="https://dispatcher.rndfnk.com/br/br3/live/mp3/mid",
        image_url="https://example.test/bayern3.png",
    )
    old_location = f"http://192.168.50.200:1516{ORION_STATION_PATH}?data={encode_orion_data(descriptor)}"
    xml = (
        '<?xml version="1.0" encoding="UTF-8" ?><presets>'
        f'<preset id="3"><ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" '
        f'location="{old_location}" sourceAccount="" isPresetable="true">'
        "<itemName>Bayern 3</itemName><containerArt>https://example.test/bayern3.png</containerArt>"
        "</ContentItem></preset></presets>"
    )

    result = import_presets_from_radio_backup(db, "IMPORT_LOGO", xml)
    db.commit()

    preset = db.query(Preset).filter(Preset.device_id == "IMPORT_LOGO", Preset.button == 3).one()
    db.close()

    assert result["imported_count"] == 1
    assert "containerArt" in preset.content_item_xml
    assert "https://example.test/bayern3.png" in preset.content_item_xml
    assert _orion_payload(preset.location)["imageUrl"] == "https://example.test/bayern3.png"


def test_account_sources_returns_canonical_local_radio_source():
    with TestClient(create_cloud_app()) as client:
        response = client.get("/streaming/account/123/sources")

    assert response.status_code == 200
    root = ET.fromstring(response.text)
    local_radio = next(source for source in root.findall("source") if source.findtext("name") == "LOCAL_INTERNET_RADIO")
    assert local_radio.attrib["id"] == "10003"
    assert local_radio.attrib["type"] == "Audio"
    assert local_radio.findtext("sourceproviderid") == "11"


def test_account_full_is_account_specific_and_contains_presets_for_reboot_sync():
    db = app_db.SessionLocal()
    station = Station(name="Boot Radio", stream_url="http://example.test/boot.mp3")
    matching = Device(
        device_id="RADIO_BOOT",
        name="Boot Device",
        ip_address="10.20.30.10",
        info_xml='<info deviceID="RADIO_BOOT"><margeAccountUUID>123</margeAccountUUID></info>',
    )
    other = Device(
        device_id="RADIO_OTHER",
        name="Other Device",
        ip_address="10.20.30.11",
        info_xml='<info deviceID="RADIO_OTHER"><margeAccountUUID>999</margeAccountUUID></info>',
    )
    db.add_all([
        station,
        matching,
        other,
        Setting(key="lan_host", value="192.168.50.77"),
    ])
    db.flush()
    db.add(Preset(device_id="RADIO_BOOT", button=3, station_id=station.id, source="LOCAL_INTERNET_RADIO", location="http://cloud.test/boot"))
    db.commit()
    db.close()

    with TestClient(create_cloud_app()) as client:
        response = client.get("/streaming/account/123/full")

    assert response.status_code == 200
    root = ET.fromstring(response.text)
    devices = root.findall("./devices/device")
    assert [device.attrib["deviceid"] for device in devices] == ["RADIO_BOOT"]
    preset = devices[0].find("./presets/preset")
    assert preset is not None
    assert preset.attrib["buttonNumber"] == "3"
    assert preset.findtext("location").startswith(f"http://192.168.50.77:1516{ORION_STATION_PATH}?data=")
    assert _orion_payload(preset.findtext("location"))["name"] == "Boot Radio"


def test_device_recent_routes_acknowledge_playback_without_404():
    with TestClient(create_cloud_app()) as client:
        posted = client.post(
            "/streaming/account/123/device/RADIO1/recent",
            content='<recent><name>Test Radio</name></recent>',
            headers={"Content-Type": "application/vnd.bose.streaming-v1.2+xml"},
        )
        fetched = client.get("/streaming/account/123/device/RADIO1/recents")

    assert posted.status_code == 200
    assert ET.fromstring(posted.text).tag == "recents"
    assert fetched.status_code == 200
    assert ET.fromstring(fetched.text).tag == "recents"
    assert fetched.headers["etag"]


def test_station_add_returns_station_and_keeps_dispatcher_url(monkeypatch):
    async def resolver(url: str):
        return StreamAnalysis(url, url, "mp3", "", "mp3", 100, "", False, True)

    monkeypatch.setattr(stations_presets, "resolve_stream_url", resolver)

    with TestClient(create_web_app()) as client:
        response = client.post("/api/stations", json={"name": "BR Dispatcher", "stream_url": "https://dispatcher.rndfnk.com/br/br1/obb/mp3/mid"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"]
    assert payload["created"] is True
    assert payload["station"]["stream_url"] == "https://dispatcher.rndfnk.com/br/br1/obb/mp3/mid"
    assert payload["station"]["stream_format"] == "mp3"
    assert payload["compatibility"]["compatibility_score"] == 100


def test_station_add_does_not_replace_with_uncertain_resolved_url(monkeypatch):
    async def resolver(url: str):
        return StreamAnalysis(url, "https://cdn.example/unknown", "unknown", "", "", 40, "", False, False)

    monkeypatch.setattr(stations_presets, "resolve_stream_url", resolver)

    with TestClient(create_web_app()) as client:
        response = client.post("/api/stations", json={"name": "Unknown Direct", "stream_url": "https://example.test/live"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["stream_url"] == "https://example.test/live"
    assert payload["station"]["stream_url_original"] == "https://example.test/live"


def test_station_add_keeps_original_url_even_when_resolver_finds_direct_audio(monkeypatch):
    async def resolver(url: str):
        return StreamAnalysis(url, "https://cdn.example/live.mp3", "mp3", "", "mp3", 100, "", False, True)

    monkeypatch.setattr(stations_presets, "resolve_stream_url", resolver)

    with TestClient(create_web_app()) as client:
        response = client.post("/api/stations", json={"name": "Playlist Source", "stream_url": "https://example.test/list.m3u"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["stream_url"] == "https://example.test/list.m3u"
    assert payload["station"]["stream_url"] == "https://example.test/list.m3u"
    assert payload["station"]["stream_url_resolved"] == "https://cdn.example/live.mp3"


def test_station_add_allows_hls_without_blocking(monkeypatch):
    async def resolver(url: str):
        return StreamAnalysis(url, url, "hls", "", "hls", 5, "HLS/m3u8 kann auf manchen SoundTouch-Geraeten Probleme machen.", True, False)

    monkeypatch.setattr(stations_presets, "resolve_stream_url", resolver)

    with TestClient(create_web_app()) as client:
        response = client.post("/api/stations", json={"name": "HLS Radio", "stream_url": "https://example.test/live.m3u8"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["created"] is True
    assert payload["station"]["stream_url"] == "https://example.test/live.m3u8"
    assert payload["station"]["is_hls"] is True


def test_station_add_aac_320_keeps_url_and_returns_warning(monkeypatch):
    async def resolver(url: str):
        return StreamAnalysis(url, url, "aac", "", "aac", 65, "AAC 320 kbps kann auf manchen SoundTouch-Geraeten haken.", False, True, 320)

    monkeypatch.setattr(stations_presets, "resolve_stream_url", resolver)

    with TestClient(create_web_app()) as client:
        response = client.post("/api/stations", json={"name": "AAC High", "stream_url": "https://example.test/radio/aac/320"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["station"]["stream_url"] == "https://example.test/radio/aac/320"
    assert payload["compatibility"]["compatibility_warning"].startswith("AAC 320")


def test_playback_source_fails_closed_when_confirmed_local_source_is_missing():
    sources = '<sources><sourceItem source="AUX" status="READY"/><sourceItem source="TUNEIN" status="READY"/><sourceItem source="SPOTIFY" status="READY"/></sources>'
    assert _stream_source_from_sources_xml(sources) == ""


def test_playback_source_prefers_local_radio_when_available():
    sources = '<sources><sourceItem source="TUNEIN" status="READY"/><sourceItem source="LOCAL_INTERNET_RADIO" status="READY"/></sources>'
    assert _stream_source_from_sources_xml(sources) == "LOCAL_INTERNET_RADIO"


def test_persistence_sources_xml_contains_all_recommended_sources():
    root = ET.fromstring(persistence_sources_xml())
    types = [node.attrib["type"] for node in root.findall("./source/sourceKey")]
    assert types == list(RECOMMENDED_SOURCE_TYPES)
    assert set(types) >= {
        "AUX", "AIRPLAY", "ALEXA", "AMAZON", "BLUETOOTH", "DEEZER", "IHEART",
        "INTERNET_RADIO", "JUKE", "LOCAL_INTERNET_RADIO", "LOCAL_MUSIC",
        "NOTIFICATION", "PANDORA", "QPLAY", "RADIO_BROWSER", "SIRIUSXM",
        "SOUNDCLOUD", "SPOTIFY", "STORED_MUSIC", "STORED_MUSIC_MEDIA_RENDERER",
        "TUNEIN", "UPNP", "WBMX",
    }
    aux = root.find("./source/sourceKey[@type='AUX']")
    assert aux is not None
    assert aux.attrib["account"] == "AUX"


def test_playback_source_does_not_select_unconfirmed_tunein_source_key():
    sources = '<sources><source displayName="TuneIn"><sourceKey type="TUNEIN" account=""/></source></sources>'
    assert _stream_source_from_sources_xml(sources) == ""


def test_playback_source_does_not_select_unconfirmed_radio_browser():
    sources = '<sources><source type="RADIO_BROWSER"/></sources>'
    assert _stream_source_from_sources_xml(sources) == ""


def test_playback_source_rejects_non_stream_sources():
    sources = '<sources><sourceItem source="AUX" status="READY"/><sourceItem source="SPOTIFY" status="READY"/></sources>'
    assert _stream_source_from_sources_xml(sources) == ""


def test_content_item_xml_defaults_to_local_internet_radio_even_with_empty_source():
    station = Station(name="Compat Radio", stream_url="http://example.test/live.mp3", provider="TUNEIN")

    xml = content_item_xml(station, "http://cloud.test/station/compat", source="")

    root = ET.fromstring(xml)
    assert root.attrib["source"] == "LOCAL_INTERNET_RADIO"
    assert root.attrib["location"] == "http://cloud.test/station/compat"


def test_play_station_fails_closed_when_no_stream_source_is_ready(monkeypatch):
    calls = []

    class Client:
        def __init__(self, _ip_address: str):
            pass

        async def get_xml(self, path: str) -> str:
            if path == "/sources":
                return '<sources><sourceItem source="AUX" status="READY"/></sources>'
            if path == "/now_playing":
                return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus></nowPlaying>'
            raise AssertionError(path)

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            calls.append((path, body))
            return "<status>OK</status>"

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    station = Station(name="Play Compat", stream_url="http://example.test/play.mp3", provider="TUNEIN")
    db.add_all([Device(device_id="PLAYCOMPAT", ip_address="192.0.2.91"), station, Setting(key="lan_host", value="192.168.50.77")])
    db.commit()
    station_id = station.id
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.post(f"/api/devices/PLAYCOMPAT/stations/{station_id}/play", json={"dry_run": False})

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "no stream-capable source is READY on the radio"
    assert calls == []


def test_play_station_confirms_safe_volume_before_and_after_select(monkeypatch):
    calls = []

    class Client:
        volume = 27
        muted = False

        def __init__(self, _ip_address: str, **_kwargs):
            pass

        async def get_xml(self, path: str) -> str:
            calls.append(("get", path, ""))
            if path == "/volume":
                return f"<volume><actualvolume>{Client.volume}</actualvolume><muteenabled>{str(Client.muted).lower()}</muteenabled></volume>"
            if path == "/now_playing":
                return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus></nowPlaying>'
            raise AssertionError(path)

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            calls.append(("post", path, body))
            if path == "/volume":
                Client.volume = int(body.replace("<volume>", "").replace("</volume>", ""))
            if path == "/key" and "MUTE" in body and 'state="release"' in body:
                Client.muted = not Client.muted
            if path == "/select":
                # Reproduce the real Portable behaviour: source selection
                # restores its remembered volume and drops mute.  The local
                # provider gate must remain closed while these are restored.
                Client.volume = 30
                Client.muted = False
            return "<status>OK</status>"

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    station = Station(name="Safe Browser Radio", stream_url="http://example.test/safe.mp3", provider="LOCAL_INTERNET_RADIO")
    db.add_all([Device(device_id="PLAYSAFE", ip_address="192.0.2.96"), station, Setting(key="lan_host", value="192.168.50.77")])
    db.commit()
    station_id = station.id
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.post(
            f"/api/devices/PLAYSAFE/stations/{station_id}/play",
            json={"dry_run": False, "safe_volume": 1, "trigger": "webui"},
        )

    assert response.status_code == 200
    assert response.json()["confirmed_volume"] == 1
    select_index = next(index for index, item in enumerate(calls) if item[0] == "post" and item[1] == "/select")
    assert ("post", "/volume", "<volume>1</volume>") in calls[:select_index]
    assert calls[select_index + 1 :].count(("post", "/volume", "<volume>1</volume>")) >= 1
    assert Client.volume == 1


def test_play_station_treats_invalid_source_after_select_as_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(stations_presets, "SOURCE_ACCEPTANCE_ATTEMPTS", 2)
    monkeypatch.setattr(stations_presets, "SOURCE_ACCEPTANCE_INTERVAL_SECONDS", 0)

    class Client:
        def __init__(self, _ip_address: str):
            pass

        async def get_xml(self, path: str) -> str:
            if path == "/sources":
                return '<sources><sourceItem source="LOCAL_INTERNET_RADIO" status="READY"/></sources>'
            if path == "/now_playing":
                return '<nowPlaying source="INVALID_SOURCE"><playStatus>STOP_STATE</playStatus></nowPlaying>'
            raise AssertionError(path)

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            calls.append((path, body))
            return "<status>OK</status>"

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    station = Station(name="Invalid Source", stream_url="http://example.test/invalid.mp3")
    db.add_all([Device(device_id="INVALIDSRC", ip_address="192.0.2.93"), station, Setting(key="lan_host", value="192.168.50.77")])
    db.commit()
    station_id = station.id
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.post(f"/api/devices/INVALIDSRC/stations/{station_id}/play", json={"dry_run": False})

    assert response.status_code == 502
    assert response.json()["detail"]["error"] == "Radio hat Preset angenommen, aber Wiedergabe abgelehnt. Streamformat oder Source nicht kompatibel."
    assert all(path == "/select" for path, _body in calls)
    assert "http://192.168.50.77:1516/core02/" in calls[0][1]


def test_play_station_waits_for_async_provider_readback(monkeypatch):
    monkeypatch.setattr(stations_presets, "SOURCE_ACCEPTANCE_ATTEMPTS", 4)
    monkeypatch.setattr(stations_presets, "SOURCE_ACCEPTANCE_INTERVAL_SECONDS", 0)

    class Client:
        reads = 0

        def __init__(self, _ip_address: str, **_kwargs):
            pass

        async def get_xml(self, path: str) -> str:
            if path == "/sources":
                return '<sources><sourceItem source="LOCAL_INTERNET_RADIO" status="READY"/></sources>'
            if path == "/now_playing":
                Client.reads += 1
                if Client.reads == 1:
                    return '<nowPlaying source="INVALID_SOURCE"><playStatus>INVALID_PLAY_STATUS</playStatus></nowPlaying>'
                if Client.reads == 2:
                    return '<nowPlaying source="LOCAL_INTERNET_RADIO"/>'
                return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>BUFFERING_STATE</playStatus></nowPlaying>'
            raise AssertionError(path)

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            assert path == "/select"
            return "<status>/select</status>"

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    station = Station(name="Async Provider", stream_url="http://example.test/async.mp3")
    db.add_all(
        [
            Device(device_id="ASYNCSELECT", ip_address="192.0.2.98"),
            station,
            Setting(key="lan_host", value="192.168.50.77"),
        ]
    )
    db.commit()
    station_id = station.id
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.post(
            f"/api/devices/ASYNCSELECT/stations/{station_id}/play",
            json={"dry_run": False},
        )

    assert response.status_code == 200
    assert Client.reads == 3


def test_play_station_does_not_guess_unimplemented_provider_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(stations_presets, "SOURCE_ACCEPTANCE_ATTEMPTS", 2)
    monkeypatch.setattr(stations_presets, "SOURCE_ACCEPTANCE_INTERVAL_SECONDS", 0)

    class Client:
        def __init__(self, _ip_address: str):
            self.selected_source = ""

        async def get_xml(self, path: str) -> str:
            if path == "/sources":
                return '<sources><sourceItem source="LOCAL_INTERNET_RADIO" status="READY"/><sourceItem source="TUNEIN" status="READY"/></sources>'
            if path == "/now_playing":
                if self.selected_source == "TUNEIN":
                    return '<nowPlaying source="TUNEIN"><playStatus>PLAY_STATE</playStatus></nowPlaying>'
                return '<nowPlaying source="INVALID_SOURCE"><playStatus>STOP_STATE</playStatus></nowPlaying>'
            raise AssertionError(path)

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            content = ET.fromstring(body)
            self.selected_source = content.attrib.get("source", "")
            calls.append((path, body))
            return "<status>OK</status>"

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    station = Station(name="Fallback Source", stream_url="http://example.test/fallback.mp3")
    db.add_all([Device(device_id="FALLBACKSRC", ip_address="192.0.2.94"), station, Setting(key="lan_host", value="192.168.50.77")])
    db.commit()
    station_id = station.id
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.post(f"/api/devices/FALLBACKSRC/stations/{station_id}/play", json={"dry_run": False})

    assert response.status_code == 502
    assert [ET.fromstring(body).attrib["source"] for _path, body in calls] == ["LOCAL_INTERNET_RADIO"]
    db = app_db.SessionLocal()
    learned = db.query(Setting).filter(Setting.key == "playback_source:FALLBACKSRC").one_or_none()
    db.close()
    assert learned is None


def test_failed_safe_select_restores_volume_standby_and_persists_lock(monkeypatch):
    calls = []

    class Client:
        volume = 30
        muted = False

        def __init__(self, _ip_address: str, **_kwargs):
            pass

        async def get_xml(self, path: str) -> str:
            calls.append(("get", path, ""))
            if path == "/volume":
                return (
                    f"<volume><actualvolume>{Client.volume}</actualvolume>"
                    f"<muteenabled>{str(Client.muted).lower()}</muteenabled></volume>"
                )
            if path == "/sources":
                return '<sources><sourceItem source="LOCAL_INTERNET_RADIO" status="READY"/></sources>'
            if path == "/standby":
                return "<status>/standby</status>"
            raise AssertionError(path)

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            calls.append(("post", path, body))
            if path == "/volume":
                Client.volume = int(body.replace("<volume>", "").replace("</volume>", ""))
                return "<status>/volume</status>"
            if path == "/key":
                if "MUTE" in body and 'state="release"' in body:
                    Client.muted = not Client.muted
                return "<status>/key</status>"
            if path == "/select":
                # Firmware can restore a remembered source volume before
                # rejecting the ContentItem.  The endpoint must still prove
                # STOP/STANDBY and volume 1 before returning the error.
                Client.volume = 30
                Client.muted = False
                request = httpx.Request("POST", "http://192.0.2.97:8090/select")
                response = httpx.Response(500, request=request, text="<error>UNKNOWN_SOURCE_ERROR</error>")
                raise httpx.HTTPStatusError("rejected", request=request, response=response)
            raise AssertionError(path)

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    station = Station(name="Rejected Safe Radio", stream_url="http://example.test/safe-fail.mp3")
    db.add_all(
        [
            Device(device_id="PLAYSAFEFAIL", ip_address="192.0.2.97"),
            station,
            Setting(key="lan_host", value="192.168.50.77"),
        ]
    )
    db.commit()
    station_id = station.id
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.post(
            f"/api/devices/PLAYSAFEFAIL/stations/{station_id}/play",
            json={"dry_run": False, "safe_volume": 1, "trigger": "webui"},
        )

    assert response.status_code == 502
    safety = response.json()["detail"]["audio_safety"]
    assert safety["observed_before_cleanup"] == 30
    assert safety["confirmed_volume"] == 1
    assert safety["stopped_and_standby"] is True
    assert safety["locked"] is True
    assert Client.volume == 1
    assert ("get", "/standby", "") in calls
    db = app_db.SessionLocal()
    safety_row = db.query(RuntimeState).filter(
        RuntimeState.key == "device:PLAYSAFEFAIL:audio_safety"
    ).one()
    assert '"locked": true' in safety_row.value
    db.close()


def test_set_preset_keeps_compatible_xml_and_does_not_read_sources(monkeypatch):
    calls = []
    stored_xml = ""

    class Client:
        def __init__(self, _ip_address: str):
            self.reads = 0

        async def get_xml(self, path: str) -> str:
            assert path == "/presets"
            self.reads += 1
            if stored_xml:
                return f'<presets><preset id="2">{stored_xml}</preset></presets>'
            return "<presets/>"

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            nonlocal stored_xml
            if path == "/notification":
                return "<ok/>"
            calls.append((path, body))
            stored_xml = ET.tostring(ET.fromstring(body).find("ContentItem"), encoding="unicode")
            return "<status>OK</status>"

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    station = Station(name="Preset Compat", stream_url="http://example.test/preset.mp3", provider="TUNEIN")
    db.add_all([Device(device_id="PRESETCOMPAT", ip_address="192.0.2.92"), station, Setting(key="lan_host", value="192.168.50.77")])
    db.flush()
    db.add(Preset(
        device_id="PRESETCOMPAT",
        button=2,
        source="TUNEIN",
        source_account="stale-account-from-previous-preset",
        location="old:location",
        content_item_xml='<ContentItem source="TUNEIN" sourceAccount="stale-account-from-previous-preset" location="old:location"><itemName>Old</itemName></ContentItem>',
    ))
    db.commit()
    station_id = station.id
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.post("/api/presets/PRESETCOMPAT/2", json={"station_id": station_id, "dry_run": False, "memory_checked": True})

    assert response.status_code == 200
    db = app_db.SessionLocal()
    preset = db.query(Preset).filter(Preset.device_id == "PRESETCOMPAT", Preset.button == 2).one()
    db.close()
    assert preset.source == "LOCAL_INTERNET_RADIO"
    assert preset.source_account == ""
    content = ET.fromstring(preset.content_item_xml)
    assert content.attrib["source"] == "LOCAL_INTERNET_RADIO"
    assert content.attrib["sourceAccount"] == ""
    assert content.attrib["location"].startswith("http://192.168.50.77:1516/core02/")
    assert not content.attrib["location"].startswith("/core02")
    assert calls == [("/storePreset", calls[0][1])]
    assert "LOCAL_INTERNET_RADIO" in calls[0][1]
    assert "http://192.168.50.77:1516/core02/" in calls[0][1]


def test_set_preset_ignores_learned_playback_source_for_compatible_xml(monkeypatch):
    calls = []
    stored_xml = ""

    class Client:
        def __init__(self, _ip_address: str):
            pass

        async def get_xml(self, path: str) -> str:
            assert path == "/presets"
            if stored_xml:
                return f'<presets><preset id="4">{stored_xml}</preset></presets>'
            return "<presets/>"

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            nonlocal stored_xml
            if path == "/notification":
                return "<ok/>"
            calls.append((path, body))
            stored_xml = ET.tostring(ET.fromstring(body).find("ContentItem"), encoding="unicode")
            return "<status>OK</status>"

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    station = Station(name="Preset Learned", stream_url="http://example.test/learned.mp3")
    db.add_all([
        Device(device_id="PRESETLEARNED", ip_address="192.0.2.95"),
        station,
        Setting(key="lan_host", value="192.168.50.77"),
        Setting(key="playback_source:PRESETLEARNED", value="TUNEIN"),
    ])
    db.commit()
    station_id = station.id
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.post("/api/presets/PRESETLEARNED/4", json={"station_id": station_id, "dry_run": False, "memory_checked": True})

    assert response.status_code == 200
    db = app_db.SessionLocal()
    preset = db.query(Preset).filter(Preset.device_id == "PRESETLEARNED", Preset.button == 4).one()
    db.close()
    assert preset.source == "LOCAL_INTERNET_RADIO"
    assert ET.fromstring(preset.content_item_xml).attrib["source"] == "LOCAL_INTERNET_RADIO"


def test_unconfirmed_provider_contracts_fail_closed_instead_of_faking_success():
    with TestClient(create_cloud_app()) as client:
        responses = [
            client.get("/streaming/device/RADIO1/streaming_token"),
            client.get("/v1/blacklist/RADIO1"),
            client.post("/v1/blacklist/RADIO1", json={"blacklist": ["ignored"]}),
            client.get("/streaming/account/123/provider_settings"),
            client.get("/bmx/tunein/v1/playback/station/bayern1"),
            client.get("/bmx/tunein/v1/now-playing/station/bayern1"),
            client.post("/bmx/tunein/v1/reporting/station/bayern1", json={"event": "start"}),
            client.get("/bmx/tunein/v1/favorite/bayern1"),
            client.post("/bmx/tunein/v1/favorite/bayern1", json={"favorite": True}),
            client.get("/bmx/radiobrowser/v1/playback/station/abc"),
            client.get("/bmx/radiobrowser/v1/now-playing/station/abc"),
            client.post("/bmx/radiobrowser/v1/reporting/station/abc", json={"event": "start"}),
            client.get("/bmx/resolve?url=http://example.test/live.mp3"),
        ]

    assert [response.status_code for response in responses[:4]] == [200] * 4
    assert [response.status_code for response in responses[4:]] == [501] * 9
    token = ET.fromstring(responses[0].text)
    assert token.attrib["value"] == "Bearer st-local-token-RADIO1"
    for response in responses[4:]:
        assert response.json()["title"] == "Provider contract unsupported"


def test_web_port_also_serves_cloud_compat_routes_after_radio_port_drift():
    with TestClient(create_web_app()) as client:
        sourceproviders = client.get("/streaming/sourceproviders")
        registry = client.get("/bmx/registry/v1/services")

    assert sourceproviders.status_code == 200
    assert ET.fromstring(sourceproviders.text).tag == "sourceProviders"
    assert registry.status_code == 200


def test_preset_sync_falls_back_to_store_preset(monkeypatch):
    calls = []

    class Client:
        def __init__(self, _ip_address: str):
            self.reads = 0

        async def get_xml(self, path: str) -> str:
            assert path == "/presets"
            self.reads += 1
            if calls:
                return '<presets><preset id="1"><ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://cloud.test/live"><itemName>Live</itemName></ContentItem></preset></presets>'
            return "<presets/>"

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            if path == "/notification":
                return "<ok/>"
            calls.append((path, body))
            return "<presets/>"

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    device = Device(device_id="STORE1", ip_address="192.0.2.90")
    station = Station(name="Live", stream_url="http://example.test/live.mp3")
    db.add_all([device, station])
    db.flush()
    db.add(Preset(device_id="STORE1", button=1, station_id=station.id, source="10003", location="http://cloud.test/live", content_item_xml='<ContentItem source="10003" type="stationurl" location="http://cloud.test/live"><itemName>Live</itemName></ContentItem>'))
    db.commit()

    rows = __import__("asyncio").run(stations_presets.sync_presets_to_radio(device, {1: "http://cloud.test/live"}, db))
    db.close()

    assert rows[0]["button"] == 1
    assert calls == [("/storePreset", '<preset id="1"><ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://cloud.test/live" sourceAccount="" isPresetable="true"><itemName>Live</itemName></ContentItem></preset>')]


def test_preset_sync_retries_store_preset_verify(monkeypatch):
    reads = {"count": 0}
    calls = []

    async def no_sleep(_seconds):
        return None

    class Client:
        def __init__(self, _ip_address: str):
            pass

        async def get_xml(self, path: str) -> str:
            assert path == "/presets"
            reads["count"] += 1
            if reads["count"] >= 14:
                return '<presets><preset id="1"><ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://cloud.test/retry"><itemName>Retry</itemName></ContentItem></preset></presets>'
            return "<presets/>"

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            if path == "/storePreset":
                calls.append((path, body))
            return "<ok/>"

    monkeypatch.setattr(stations_presets.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    device = Device(device_id="RETRY1", ip_address="192.0.2.95")
    station = Station(name="Retry", stream_url="http://example.test/retry.mp3")
    db.add_all([device, station])
    db.flush()
    db.add(Preset(device_id="RETRY1", button=1, station_id=station.id, source="LOCAL_INTERNET_RADIO", location="http://cloud.test/retry", content_item_xml='<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://cloud.test/retry"><itemName>Retry</itemName></ContentItem>'))
    db.commit()

    rows = __import__("asyncio").run(stations_presets.sync_presets_to_radio(device, {1: "http://cloud.test/retry"}, db))
    db.close()

    assert rows[0]["button"] == 1
    assert calls and calls[0][0] == "/storePreset"
    assert reads["count"] >= 14


def test_add_device_callback_enables_factory_reset_account_pairing():
    payload = '<device deviceid="PAIR1"><name>Kitchen</name><macaddress>PAIR1</macaddress></device>'
    with TestClient(create_cloud_app()) as client:
        response = client.post("/streaming/account/7654321/device/", content=payload)

    assert response.status_code == 201
    root = ET.fromstring(response.text)
    assert root.attrib["deviceid"] == "PAIR1"
    assert root.findtext("name") == "Kitchen"
    assert response.headers["location"].endswith("/streaming/account/7654321/device/PAIR1")
import pytest as _pytest_marker
pytestmark = [_pytest_marker.mark.integration, _pytest_marker.mark.slow]
