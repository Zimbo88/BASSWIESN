import asyncio
from dataclasses import asdict
import json
import time
from types import SimpleNamespace
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.db import get_db
from basswiesn.app.models import Device, MetadataState, RuntimeState, Station
from basswiesn.app.routers import cloud, research_state
from basswiesn.app.services.clock_metadata import save_clock_metadata_preference
from basswiesn.app.services.metadata_engine import clock_display_projection
from basswiesn.app.services import station_metadata as metadata
from basswiesn.app.services.network_security import UrlValidation

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("raw,name,host,track,artist", [
    ("Artist: Title", "Example", "d1.rndfnk.com", "Title", "Artist"),
    ("Artist - Title", "Example", "d1.rndfnk.com", "Title", "Artist"),
    ("Artist - Title: Live", "Example", "d1.rndfnk.com", "Title: Live", "Artist"),
    ("Artist - Title", "Example", "pool.radiopaloma.de", "Title", "Artist"),
    ("Artist - Title", "Example", "audio.stream24.net", "Title", "Artist"),
    ("Artist - Title", "Example", "onair.krone.at", "Title", "Artist"),
    ("Artist - Title", "Example", "audio.antenne.de", "Title", "Artist"),
    ("*** Example - Station slogan", "Example (AAC)", "d1.rndfnk.com", "", ""),
    ("Verkehr: Current information", "Example", "d1.rndfnk.com", "", ""),
    ("Visit our website", "Example", "d1.rndfnk.com", "", ""),
    ("Example - We love music", "Example (AAC)", "audio.stream24.net", "", ""),
    ("Not a known - convention", "Example", "example.invalid", "", ""),
    ("", "Example", "d1.rndfnk.com", "", ""),
])
def test_classification_does_not_invent_song_fields(raw, name, host, track, artist):
    result = metadata.classify_title(raw, name, host)
    assert result["track"] == track
    assert result["artist"] == artist
    assert result["other_info"] == ("" if track else raw)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def block(text, interval=8, encoding="utf-8"):
    data = f"StreamTitle='{text}';".encode(encoding)
    size = (len(data) + 15) // 16
    return b"A" * interval + bytes([size]) + data.ljust(size * 16, b"\0")


def transport(monkeypatch, responder, validate=None):
    original = httpx.AsyncClient
    monkeypatch.setattr(metadata.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(responder), **kwargs))
    monkeypatch.setattr(metadata, "validate_outbound_http_url", validate or (
        lambda url, **kw: UrlValidation(True, "ok", hostname=httpx.URL(url).host, addresses=("1.1.1.1",))))


@pytest.mark.parametrize("encoding", ["utf-8", "latin-1"])
def test_icy_chunk_boundary_and_unicode(monkeypatch, encoding):
    data = block("Artist: Grüße", encoding=encoding)
    requests = []
    def responder(request):
        requests.append(request)
        return httpx.Response(200, headers={"icy-metaint": "8", "content-type": "audio/mpeg"}, stream=Chunks([data[:10], data[10:20], data[20:]]))
    transport(monkeypatch, responder)
    result = asyncio.run(metadata.probe_icy("https://d1.rndfnk.com/live", "Example"))
    assert result["status"] == "AVAILABLE"
    assert (result["artist"], result["track"]) == ("Artist", "Grüße")
    assert requests[0].url.host == "1.1.1.1"
    assert requests[0].headers["Host"] == "d1.rndfnk.com"
    assert requests[0].headers["Icy-MetaData"] == "1"


@pytest.mark.parametrize("header,status", [("0", "NO_METADATA_INTERVAL"), ("bad", "NO_METADATA_INTERVAL"), ("9999999", "NO_METADATA_INTERVAL")])
def test_invalid_interval_does_not_read_audio(monkeypatch, header, status):
    class NeverRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("must not read body")
            yield b""
    transport(monkeypatch, lambda r: httpx.Response(200, headers={"icy-metaint": header}, stream=NeverRead()))
    assert asyncio.run(metadata.probe_icy("https://example.invalid/live", "Example"))["status"] == status


def test_private_redirect_never_creates_a_second_transport(monkeypatch):
    calls = []
    def responder(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://192.168.50.25/"})
    def validate(url, **kwargs):
        assert kwargs == {"public_only": True}
        return UrlValidation("example.invalid" in url, "test", hostname="example.invalid", addresses=("1.1.1.1",))
    transport(monkeypatch, responder, validate)
    assert asyncio.run(metadata.probe_icy("https://example.invalid/live", "Example"))["status"] == "TARGET_REJECTED"
    assert len(calls) == 1


def test_silence_and_empty_metadata_are_bounded(monkeypatch):
    transport(monkeypatch, lambda r: httpx.Response(200, headers={"icy-metaint": "8"}, stream=Chunks([block("") * 4])))
    result = asyncio.run(metadata.probe_icy("https://example.invalid/live", "Example"))
    assert result["status"] == "EMPTY_METADATA"
    assert result["artist"] == result["track"] == ""


def test_startup_zero_length_frames_do_not_mean_absent_metadata(monkeypatch):
    frames = (b"A" * 8 + b"\0") * 5 + block("Artist - Someone Else's Song")
    transport(monkeypatch, lambda r: httpx.Response(200, headers={"icy-metaint": "8"}, stream=Chunks([frames])))
    result = asyncio.run(metadata.probe_icy("https://d1.rndfnk.com/live", "Example"))
    assert result["status"] == "AVAILABLE"
    assert (result["artist"], result["track"]) == ("Artist", "Someone Else's Song")


def test_byte_budget(monkeypatch):
    transport(monkeypatch, lambda r: httpx.Response(200, headers={"icy-metaint": "8"}, stream=Chunks([b"A" * (metadata.MAX_BYTES + 1)])))
    assert asyncio.run(metadata.probe_icy("https://example.invalid/live", "Example"))["status"] == "BYTE_LIMIT"


def test_display_preferences_separate_songs_and_other_info():
    song = {"track": "Song", "artist": "Artist", "other_info": "", "description": "Programme"}
    assert metadata.display_fields(metadata.DisplayPreference("TRACK_ARTIST"), "Station", song) == {"track": "Song", "artist": "Artist"}
    assert metadata.display_fields(metadata.DisplayPreference("TRACK_ARTIST", True), "Station", song) == {"track": "Song — Programme", "artist": "Artist"}
    assert metadata.display_fields(metadata.DisplayPreference("STATION", True), "Station", {"other_info": "Traffic"}) == {"track": "Station — Traffic", "artist": ""}
    assert metadata.display_fields(metadata.DisplayPreference("TRACK_ARTIST"), "Station", {}) == {"track": "Station", "artist": ""}


def test_generic_header_placeholders_are_not_displayed():
    preference=metadata.DisplayPreference("STATION", True)
    result=metadata.display_fields(preference, "Example", {"description":"Unspecified description","genre":"various"})
    assert result=={"track":"Example","artist":""}
    result=metadata.display_fields(preference, "Example (AAC)", {"description":"EXAMPLE","genre":"Pop"})
    assert result=={"track":"Example (AAC) — Pop","artist":""}


@pytest.mark.parametrize("songs", [False,True])
@pytest.mark.parametrize("other", [False,True])
@pytest.mark.parametrize("clock", [False,True])
def test_independent_display_choices_and_clock_do_not_reuse_radio_echoes(monkeypatch,songs,other,clock):
    def projection(snapshot,*,mode,timezone):
        return clock_display_projection(snapshot,mode=mode,timezone=timezone,now=datetime(2030,7,1,18,15,tzinfo=UTC))
    monkeypatch.setattr(cloud,"clock_display_projection",projection)
    with app_db.SessionLocal() as db:
        device=Device(device_id="EXAMPLE-A",name="Example",ip_address="192.0.2.42")
        station=Station(name="Station",stream_url="https://example.invalid/live",provider_station_id="example-station")
        db.add_all([device,station,RuntimeState(key=metadata.cache_key(station.stream_url),value=json.dumps({
            "track":"Song","artist":"Artist","description":"Programme","observed_timestamp":time.time()})),
            MetadataState(device_id=device.device_id,station_id="example-station",
                          track="Old song — Old programme 09:00",artist="Old artist",album="Old album")])
        db.commit()
        metadata.save_display_preference(db,device.device_id,metadata.DisplayPreference("TRACK_ARTIST" if songs else "STATION",other))
        save_clock_metadata_preference(db,device.device_id,enabled=clock,mode="APPEND")
        payload=cloud._orion_now_playing_payload(db,device,"example-station")
        expected="Song" if songs else "Station"
        if other: expected+=" — Programme"
        if clock: expected+=" 20:15"
        assert payload["track"]==expected
        assert payload["artist"]==("Artist" if songs else "")
        assert payload["album"]==""
        assert "·" not in payload["track"]


def test_empty_or_expired_song_cache_falls_back_without_disabling_playback():
    with app_db.SessionLocal() as db:
        device=Device(device_id="EXAMPLE-A",name="Example",ip_address="192.0.2.42")
        db.add_all([device,Station(name="Station",stream_url="https://example.invalid/live",provider_station_id="example-station")])
        db.commit()
        metadata.save_display_preference(db,device.device_id,metadata.DisplayPreference("TRACK_ARTIST"))
        save_clock_metadata_preference(db,device.device_id,enabled=False,mode="APPEND")
        payload=cloud._orion_now_playing_payload(db,device,"example-station")
        assert payload["track"]=="Station" and payload["artist"]==""
        assert db.query(RuntimeState).count()==0


def test_missing_title_clock_mode_retains_fresh_icy_song():
    with app_db.SessionLocal() as db:
        device=Device(device_id="EXAMPLE-A",name="Example",ip_address="192.0.2.42")
        station=Station(name="Station",stream_url="https://example.invalid/live",provider_station_id="example-station")
        db.add_all([device,station,RuntimeState(key=metadata.cache_key(station.stream_url),value=json.dumps({
            "track":"Fresh song","artist":"Artist","observed_timestamp":time.time()}))])
        db.commit()
        metadata.save_display_preference(db,device.device_id,metadata.DisplayPreference("TRACK_ARTIST"))
        save_clock_metadata_preference(db,device.device_id,enabled=True,mode="MISSING_TITLE")
        assert cloud._orion_now_playing_payload(db,device,"example-station")["track"]=="Fresh song"


def test_cache_expiry_and_changed_stream_cannot_reuse_old_title():
    with app_db.SessionLocal() as db:
        db.add(RuntimeState(key=metadata.cache_key("https://example.invalid/one"), value=json.dumps({"track": "Old", "observed_timestamp": 1000})))
        db.commit()
        assert metadata.cached_metadata(db, "https://example.invalid/one", now=1100)["track"] == "Old"
        assert not metadata.cached_metadata(db, "https://example.invalid/one", now=1201)
        assert not metadata.cached_metadata(db, "https://example.invalid/two", now=1100)


def test_collector_requires_opt_in_and_real_report_peer_and_shares_station():
    async def scenario():
        calls = []
        async def probe(url, name):
            calls.append(url)
            return {"status": "AVAILABLE", "track": "Song", "artist": "Artist"}
        collector = metadata.StationMetadataCollector(app_db.SessionLocal, probe)
        station = SimpleNamespace(stream_url="https://example.invalid/live", name="Station")
        device = SimpleNamespace(device_id="EXAMPLE-A", ip_address="192.0.2.42")
        try:
            with app_db.SessionLocal() as db:
                collector.schedule(db, device, station, peer=device.ip_address, event_type="TIMED")
                assert not collector.tasks
                metadata.save_display_preference(db, device.device_id, metadata.DisplayPreference("TRACK_ARTIST"))
                collector.schedule(db, device, station, peer="192.0.2.43", event_type="TIMED")
                collector.schedule(db, device, station, peer=device.ip_address, event_type="STOP")
                assert not collector.tasks
                collector.schedule(db, device, station, peer=device.ip_address, event_type="TIMED")
                collector.schedule(db, device, station, peer=device.ip_address, event_type="TIMED")
                assert len(collector.tasks) == 1
            await asyncio.gather(*list(collector.tasks.values()))
            with app_db.SessionLocal() as db:
                assert metadata.cached_metadata(db, station.stream_url)["track"] == "Song"
                collector.schedule(db, device, station, peer=device.ip_address, event_type="TIMED")
                assert not collector.tasks
            assert len(calls) == 1
        finally:
            await collector.shutdown()
    asyncio.run(scenario())


def test_valid_empty_title_uses_normal_refresh_not_failure_backoff():
    async def scenario():
        async def probe(url,name):
            return {"status":"EMPTY_METADATA","track":"","artist":""}
        collector=metadata.StationMetadataCollector(app_db.SessionLocal,probe)
        key=metadata.cache_key("https://example.invalid/live")
        try:
            before=time.monotonic()
            await collector._collect(key,"https://example.invalid/live","Example")
            assert 59 < collector.next_due[key]-before < 62
            with app_db.SessionLocal() as db:
                assert metadata.cached_metadata(db,"https://example.invalid/live")["status"]=="EMPTY_METADATA"
        finally:
            await collector.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("fail_collector",[False,True])
def test_report_embeds_same_optional_display_and_survives_collector_failure(fail_collector):
    with app_db.SessionLocal() as db:
        device=Device(device_id="EXAMPLE-A",name="Example",ip_address="192.0.2.42")
        station=Station(name="Station",stream_url="https://example.invalid/live",provider_station_id="example-station")
        db.add_all([device,station,RuntimeState(key=metadata.cache_key(station.stream_url),value=json.dumps({
            "track":"Song","artist":"Artist","description":"Programme","observed_timestamp":time.time()}))])
        db.commit()
        metadata.save_display_preference(db,device.device_id,metadata.DisplayPreference("TRACK_ARTIST",True))
        save_clock_metadata_preference(db,device.device_id,enabled=False,mode="APPEND")
    calls=[]
    class Collector:
        def schedule(self,db,device,station,**kwargs):
            calls.append(kwargs["event_type"])
            if fail_collector:
                raise RuntimeError("simulated optional display failure")
    app=FastAPI()
    app.include_router(cloud.router)
    app.state.station_metadata_collector=Collector()
    with TestClient(app) as client:
        headers={"x-basswiesn-device-id":"EXAMPLE-A"}
        response=client.post("/bmx/orion/reporting/station/example-station",json={"eventType":"TIMED","timeIntoTrack":120},headers=headers)
        current=client.get("/bmx/orion/now-playing/station/example-station",headers=headers)
    assert response.status_code==current.status_code==200
    embedded=response.json()["_embedded"]["bmx_nowplaying"]
    assert embedded==current.json()
    assert embedded["track"]=="Song — Programme" and embedded["artist"]=="Artist"
    assert response.json()["nextReportIn"]==6
    assert calls==["TIMED"]


@pytest.mark.parametrize("payload,code", [
    ({"mode": "TRACK_ARTIST", "show_other_info": True}, 200),
    ({"mode": "GUESS"}, 422), ({"mode": "STATION", "show_other_info": "true"}, 422),
])
def test_display_api_saved_readback_is_local_only(payload, code):
    with app_db.SessionLocal() as db:
        db.add(Device(device_id="EXAMPLE-A", name="Example", ip_address="192.0.2.42"))
        db.commit()
    app = FastAPI()
    app.include_router(research_state.router)
    def dependency():
        with app_db.SessionLocal() as db:
            yield db
    app.dependency_overrides[get_db] = dependency
    with TestClient(app) as client:
        assert client.get("/api/devices/EXAMPLE-A/metadata/display").json()["mode"] == "STATION"
        response = client.put("/api/devices/EXAMPLE-A/metadata/display", json=payload)
        assert response.status_code == code
        if code == 200:
            saved = client.get("/api/devices/EXAMPLE-A/metadata/display").json()
            assert saved["mode"] == payload["mode"] and saved["show_other_info"] is True
            assert saved["radio_write"] is False
