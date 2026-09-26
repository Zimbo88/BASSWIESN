"""Offline ContentDirectory/relay contracts. No NAS, SSDP or radio access."""
import asyncio
from html import escape
import xml.etree.ElementTree as ET
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from basswiesn.app import db as database
from basswiesn.app.models import Device, Station, Setting
from basswiesn.app.routers import dlna as routes
from basswiesn.app.services import dlna_library as library

pytestmark = pytest.mark.unit
BASE = "http://192.0.2.42:8200"
UDN = "uuid:11223344-1234-4234-8234-123456789abc"


class MediaFixture:
    def __init__(self):
        self.calls = []
        self.udn = UDN
        self.resource = BASE + "/audio.mp3"
        self.content = b"ID3" + b"synthetic-audio" * 80  # transport, not codec proof
        self.fail_media = False
        self.mime = "audio/mpeg"

    def description(self):
        return f'''<root xmlns="{library.DEVICE}"><specVersion><major>1</major><minor>0</minor></specVersion>
        <device><deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
        <friendlyName>Example Library</friendlyName><UDN>{self.udn}</UDN><serviceList><service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId><controlURL>/ctl/ContentDir</controlURL>
        </service></serviceList></device></root>'''.encode()

    def didl(self, container=False):
        obj = ('<container id="music" parentID="0" restricted="1"><dc:title>Music</dc:title>'
               '<upnp:class>object.container</upnp:class></container>') if container else (
               '<item id="song&amp;1" parentID="music" restricted="1"><dc:title>Example Track</dc:title>'
               '<upnp:artist>Example Artist</upnp:artist><upnp:album>Example Album</upnp:album>'
               '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
               f'<res protocolInfo="http-get:*:{self.mime}:*" duration="00:00:03">{escape(self.resource)}</res></item>')
        return f'<DIDL-Lite xmlns="{library.DIDL}" xmlns:dc="{library.DC}" xmlns:upnp="{library.UPNP}">{obj}</DIDL-Lite>'

    def soap(self, container=False):
        return f'''<s:Envelope xmlns:s="{library.SOAP}"><s:Body>
        <u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
        <Result>{escape(self.didl(container))}</Result><NumberReturned>1</NumberReturned>
        <TotalMatches>1</TotalMatches><UpdateID>7</UpdateID></u:BrowseResponse></s:Body></s:Envelope>'''.encode()

    def handle(self, request):
        self.calls.append((request.method, request.url.path))
        assert request.url.host == httpx.URL(BASE).host and request.url.port == httpx.URL(BASE).port
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if request.url.path == "/root.xml":
            assert request.method == "GET"
            return httpx.Response(200, content=self.description())
        if request.url.path == "/ctl/ContentDir":
            assert request.method == "POST"
            assert request.headers["soapaction"] == '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"'
            action = ET.fromstring(request.content).find(f"{{{library.SOAP}}}Body")[0]
            assert action.findtext("BrowseFlag") in {"BrowseMetadata", "BrowseDirectChildren"}
            assert action.findtext("Filter") == "*"
            return httpx.Response(200, content=self.soap(action.findtext("ObjectID") == "0"))
        if request.url.path == "/audio.mp3":
            assert request.method == "GET"
            if self.fail_media:
                return httpx.Response(302, headers={"location": "http://192.0.2.99/private"})
            if request.headers.get("range") == "bytes=0-9":
                return httpx.Response(206, content=self.content[:10], headers={"content-type": "audio/mpeg", "content-range": f"bytes 0-9/{len(self.content)}"})
            return httpx.Response(200, content=self.content, headers={"content-type": "audio/mpeg"})
        raise AssertionError("Unexpected fixture target")


@pytest.fixture
def fixture(monkeypatch):
    model = MediaFixture()
    monkeypatch.setattr(routes, "client_factory", lambda: library.ContentDirectory(transport=httpx.MockTransport(model.handle)))
    monkeypatch.setattr(routes, "relay_slots", asyncio.Semaphore(4))
    return model


@pytest.fixture
def web(fixture):
    with database.SessionLocal() as db:
        db.add(Setting(key="ui_mode", value="lab"))
        db.commit()
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(routes.relay_router)
    with TestClient(app) as client:
        yield client


def connect(web):
    assert web.post("/api/dlna/settings", json={"enabled": True}).json()["enabled"] is True
    response = web.post("/api/dlna/servers", json={"description_url": BASE + "/root.xml", "approve": True})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_connect_browse_import_relay_range_and_forget(web, fixture):
    assert web.get("/api/dlna/servers").json()["servers"] == [] and fixture.calls == []
    identity = connect(web)
    base = "/api/dlna/servers/" + identity
    assert web.post(base + "/browse", json={}).json()["items"][0]["kind"] == "container"
    page = web.post(base + "/browse", json={"object_id": "music"}).json()
    assert page["update_id"] == 7 and page["next_start"] is None
    assert page["items"][0]["title"] == "Example Track"
    assert page["items"][0]["artist"] == "Example Artist"
    assert page["items"][0]["importable"] and "resources" not in page["items"][0]
    saved = web.post(base + "/items", json={"object_id": "song&1", "approve": True}).json()
    assert saved["station_id"] and not saved["radio_contacted"] and not saved["playback_verified"]
    again = web.post(base + "/items", json={"object_id": "song&1", "approve": True}).json()
    assert again["station_id"] == saved["station_id"]
    with database.SessionLocal() as db:
        row = db.get(Station, saved["station_id"])
        path = "/dlna/audio/" + row.stream_url.split("/dlna/audio/", 1)[1]
        assert row.stream_codec == "mp3"
    before = len(fixture.calls)
    response = web.get(path)
    assert response.status_code == 200 and response.content == fixture.content
    assert fixture.calls[before:] == [("GET", "/root.xml"), ("POST", "/ctl/ContentDir"), ("GET", "/audio.mp3")]
    partial = web.get(path, headers={"range": "bytes=0-9"})
    assert partial.status_code == 206 and partial.content == fixture.content[:10]
    assert web.get(path, headers={"range": "bytes=0-1,3-8"}).status_code == 416
    fixture.fail_media = True
    assert web.get(path).status_code == 502 and routes.relay_slots._value == 4
    assert web.delete(base).status_code == 200 and web.get(path).status_code == 404


def test_flag_and_approval_never_contact_media_server(web, fixture):
    web.post("/api/dlna/settings", json={"enabled": False})
    assert web.post("/api/dlna/servers", json={"description_url": BASE + "/root.xml", "approve": True}).status_code == 409
    web.post("/api/dlna/settings", json={"enabled": True})
    assert web.post("/api/dlna/servers", json={"description_url": BASE + "/root.xml"}).status_code == 409
    assert fixture.calls == []


def test_identity_change_stops_before_soap(web, fixture):
    identity = connect(web)
    fixture.calls.clear()
    fixture.udn = "uuid:different-server"
    response = web.post(f"/api/dlna/servers/{identity}/browse", json={})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SERVER_IDENTITY_CHANGED"
    assert fixture.calls == [("GET", "/root.xml")]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/root.xml", "http://169.254.169.254/latest",
    "http://224.0.0.1/root.xml", "http://example.invalid/root.xml", "http://192.0.2.42:22/", "http://user:pass@192.0.2.42/",
    "http://192.0.2.42/#fragment", "http://192.0.2.42/\nheader", "http://[::1]/", "https://192.0.2.42/root.xml"])
def test_endpoint_rejections_before_transport(url):
    with pytest.raises(library.DlnaError):
        library.endpoint(url)


def test_protected_ip_or_known_radio_never_contacted(web, fixture, monkeypatch):
    web.post("/api/dlna/settings", json={"enabled": True})
    monkeypatch.setattr(library, "is_device_access_protected", lambda host: host == "192.0.2.42")
    assert web.post("/api/dlna/servers", json={"description_url": BASE + "/root.xml", "approve": True}).status_code == 400
    assert fixture.calls == []
    monkeypatch.setattr(library, "is_device_access_protected", lambda host: False)
    with database.SessionLocal() as db:
        db.add(Device(device_id="EXAMPLE-RADIO", name="Example", ip_address="192.0.2.42"))
        db.commit()
    response = web.post("/api/dlna/servers", json={"description_url": BASE + "/root.xml", "approve": True})
    assert response.json()["detail"]["code"] == "RADIO_TARGET" and fixture.calls == []


@pytest.mark.parametrize("edit", ["entity", "doctype", "control", "base", "protected-udn"])
def test_untrusted_description_rejected(fixture, monkeypatch, edit):
    raw = fixture.description()
    if edit == "entity": raw = b'<!DOCTYPE root [<!ENTITY x SYSTEM "file:///etc/passwd">]>' + raw
    if edit == "doctype": raw = b'<!DOCTYPE root>' + raw
    if edit == "control": raw = raw.replace(b"/ctl/ContentDir", b"http://192.0.2.99:8200/control")
    if edit == "base": raw = raw.replace(b"<device>", b"<URLBase>http://192.0.2.99:8200/</URLBase><device>")
    if edit == "protected-udn": monkeypatch.setattr(library, "protected_device_ids", lambda: {"123456789ABC"})
    with pytest.raises(library.DlnaError):
        library.description(raw, BASE + "/root.xml")


@pytest.mark.parametrize("change", ["count", "negative", "wrong-root"])
def test_pagination_and_didl_checked(fixture, change):
    server = library.description(fixture.description(), BASE + "/root.xml")
    raw = fixture.soap()
    if change == "count": raw = raw.replace(b"<NumberReturned>1", b"<NumberReturned>2")
    if change == "negative": raw = raw.replace(b"<TotalMatches>1", b"<TotalMatches>-1")
    if change == "wrong-root": raw = raw.replace(b"DIDL-Lite", b"unexpected")
    with pytest.raises(library.DlnaError):
        library.browse_response(raw, server, 0, 50)


@pytest.mark.parametrize("resource", ["http://192.0.2.99/private", "http://127.0.0.1/private", "file:///etc/passwd"])
def test_off_origin_resource_not_imported(web, fixture, resource):
    identity = connect(web)
    fixture.resource = resource
    response = web.post(f"/api/dlna/servers/{identity}/items", json={"object_id": "song&1", "approve": True})
    assert response.status_code == 400 and response.json()["detail"]["code"] == "UNSUPPORTED_FORMAT"
    assert not any(path == "/audio.mp3" for _, path in fixture.calls)


def test_unsupported_codec_not_claimed_playable(web, fixture):
    identity = connect(web)
    fixture.mime = "audio/flac"
    response = web.post(f"/api/dlna/servers/{identity}/browse", json={"object_id": "music"}).json()
    assert response["items"][0]["formats"] == ["audio/flac"] and not response["items"][0]["importable"]


def test_xml_size_and_object_injection_bounded(fixture):
    with pytest.raises(library.DlnaError, match="RESPONSE_TOO_LARGE"):
        library.xml(b"x" * (library.MAX_XML + 1))
    server = library.description(fixture.description(), BASE + "/root.xml")
    request = library.browse_request(server, 'track<&"', 0, 10)
    assert ET.fromstring(request).find(f"{{{library.SOAP}}}Body")[0].findtext("ObjectID") == 'track<&"'


def imported_path(web):
    identity = connect(web)
    item = web.post(f"/api/dlna/servers/{identity}/items", json={"object_id": "song&1", "approve": True}).json()
    with database.SessionLocal() as db:
        station = db.get(Station, item["station_id"])
        return identity, "/dlna/audio/" + station.stream_url.split("/dlna/audio/", 1)[1]


@pytest.mark.parametrize("case", ["wrong-mime", "size", "compressed", "bad-range", "unsolicited-range", "redirect", "timeout"])
def test_media_response_fail_closed_and_releases_slot(web, fixture, monkeypatch, case):
    _, path = imported_path(web)
    original = fixture.handle
    def handle(request):
        if request.url.path != "/audio.mp3":
            return original(request)
        if case == "timeout":
            raise httpx.ReadTimeout("private-upstream-error-must-not-be-returned")
        headers = {"content-type": "audio/mpeg"}
        status = 200
        if case == "wrong-mime": headers["content-type"] = "text/html"
        if case == "size": headers["content-length"] = str(library.MAX_MEDIA + 1)
        if case == "compressed": headers["content-encoding"] = "gzip"
        if case == "bad-range": status, headers["content-range"] = 206, "bytes 5-14/100"
        if case == "unsolicited-range": status, headers["content-range"] = 206, "bytes 0-9/100"
        if case == "redirect": status, headers["location"] = 302, "http://192.0.2.99/private"
        return httpx.Response(status, content=b"" if case == "compressed" else b"1234567890", headers=headers)
    monkeypatch.setattr(routes, "client_factory", lambda: library.ContentDirectory(transport=httpx.MockTransport(handle)))
    response = web.get(path, headers={"Range": "bytes=0-9"} if case == "bad-range" else {})
    assert response.status_code == 502 and response.json()["detail"]["code"] == "MEDIA_UNAVAILABLE"
    assert "private" not in response.text and routes.relay_slots._value == 4


def test_disable_and_new_protection_stop_existing_mapping_before_network(web, fixture, monkeypatch):
    _, path = imported_path(web)
    fixture.calls.clear()
    web.post("/api/dlna/settings", json={"enabled": False})
    assert web.get(path).status_code == 409 and fixture.calls == []
    web.post("/api/dlna/settings", json={"enabled": True})
    monkeypatch.setattr(library, "is_device_access_protected", lambda host: True)
    assert web.get(path).status_code == 502 and fixture.calls == []
    assert routes.relay_slots._value == 4


def test_page_update_id_change_is_not_silently_combined(web, fixture, monkeypatch):
    identity = connect(web)
    original = fixture.soap
    monkeypatch.setattr(fixture, "soap", lambda *args: original(*args).replace(b"<TotalMatches>1", b"<TotalMatches>51"))
    response = web.post(f"/api/dlna/servers/{identity}/browse", json={"start":50,"expected_update_id":6})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "ITEM_CHANGED"


def test_empty_folder_and_out_of_range_are_explicit(fixture):
    server = library.description(fixture.description(), BASE + "/root.xml")
    root = ET.fromstring(fixture.soap())
    response = root.find(f"{{{library.SOAP}}}Body")[0]
    response.find("Result").text = ""
    response.find("NumberReturned").text = "0"
    response.find("TotalMatches").text = "0"
    result = library.browse_response(ET.tostring(root), server, 0, 50)
    assert result["items"] == [] and result["next_start"] is None
    assert library.browse_response(ET.tostring(root), server, 100, 50)["items"] == []


@pytest.mark.parametrize("start,count", [(0, 50), (50, 50), (0, 1)])
def test_unknown_total_allows_bounded_manual_continuation(fixture, start, count):
    server = library.description(fixture.description(), BASE + "/root.xml")
    raw = fixture.soap().replace(b"<TotalMatches>1", b"<TotalMatches>0")
    result = library.browse_response(raw, server, start, count)
    assert result["returned"] == 1 and len(result["items"]) == 1
    assert result["total"] is None and result["next_start"] == start + 1
    assert result["pagination_limited"] is False


def test_unknown_total_does_not_hide_count_or_known_total_errors(fixture):
    server = library.description(fixture.description(), BASE + "/root.xml")
    raw = fixture.soap().replace(b"<TotalMatches>1", b"<TotalMatches>0")
    with pytest.raises(library.DlnaError, match="INVALID_PAGINATION"):
        library.browse_response(raw.replace(b"<NumberReturned>1", b"<NumberReturned>2"), server, 0, 50)
    with pytest.raises(library.DlnaError, match="INVALID_PAGINATION"):
        library.browse_response(fixture.soap(), server, 1, 50)


@pytest.mark.parametrize("unknown", [True, False])
def test_continuation_never_exceeds_request_index_limit(fixture, unknown):
    server = library.description(fixture.description(), BASE + "/root.xml")
    raw = fixture.soap().replace(b"<TotalMatches>1", b"<TotalMatches>0" if unknown else b"<TotalMatches>2000000")
    result = library.browse_response(raw, server, 1000000, 50)
    assert result["next_start"] is None and result["pagination_limited"] is True


def test_unknown_total_propagates_through_api_but_metadata_remains_strict(web, fixture, monkeypatch):
    identity = connect(web)
    original = fixture.soap
    monkeypatch.setattr(fixture, "soap", lambda *args: original(*args).replace(b"<TotalMatches>1", b"<TotalMatches>0"))
    result = web.post(f"/api/dlna/servers/{identity}/browse", json={})
    assert result.status_code == 200 and result.json()["total"] is None
    assert result.json()["next_start"] == 1
    result = web.post(f"/api/dlna/servers/{identity}/items", json={"object_id":"song&1", "approve":True})
    assert result.status_code == 409 and result.json()["detail"]["code"] == "ITEM_CHANGED"


def test_changed_object_and_codec_do_not_stream(web, fixture, monkeypatch):
    _, path = imported_path(web)
    original = fixture.didl
    monkeypatch.setattr(fixture, "didl", lambda *args: original(*args).replace("song&amp;1", "different"))
    assert web.get(path).status_code == 502
    monkeypatch.setattr(fixture, "didl", original)
    fixture.mime = "audio/aac"
    assert web.get(path).status_code == 502
    assert not any(p == "/audio.mp3" for _,p in fixture.calls)
    assert routes.relay_slots._value == 4
