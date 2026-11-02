import asyncio
from datetime import UTC, datetime
import json

import httpx
import pytest

from basswiesn.app.services import antenne_metadata as am, station_metadata as sm
from basswiesn.app.services.network_security import UrlValidation

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC).timestamp()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data): self.data = data
    async def __aiter__(self): yield self.data


def fixture():
    return {"success": True, "generatedon": "2026-01-01T12:00:00Z", "data": {
        "mountpoint": "antenne", "class": "Music", "starttime": "2026-01-01T11:59:00Z",
        "artist": "Example Artist", "title": "Example Song"}}


@pytest.mark.parametrize("url,expected", [
    ("http://mp3channels.webradio.antenne.de/antenne.aac", True),
    ("https://stream.antenne.de/antenne/stream/aacp", True),
    ("https://stream.antenne.de/antenne/stream/mp3", True),
    ("https://stream.antenne.de/other/stream/aacp", False),
    ("https://stream.antenne.de.example.invalid/antenne/stream/aacp", False),
    ("https://stream.antenne.de/antenne/stream/aacp?user=x", False),
    ("https://name:pass@stream.antenne.de/antenne/stream/aacp", False),
    ("http://192.0.2.42/antenne.aac", False),
])
def test_mapping_is_exact_main_feed_only(url, expected):
    assert am.supports(url) is expected


def test_explicit_fields_and_moderation_clear_old_song():
    data = fixture()
    result = am.parse(data, now=NOW)
    assert (result["artist"], result["track"]) == ("Example Artist", "Example Song")
    assert result["classification"] == "ARTIST_TITLE_EXPLICIT"
    assert result["audio_alignment_verified"] is False
    data["data"]["class"] = "Moderation"
    result = am.parse(data, now=NOW)
    assert result["status"] == "EMPTY_METADATA"
    assert result["track"] == result["artist"] == ""


@pytest.mark.parametrize("path,value", [
    ("success", False), ("generatedon", "2025-12-31T12:00:00Z"),
    ("generatedon", "2026-01-01T12:01:00Z"), ("generatedon", "2026-01-01T12:00:00"),
    ("mountpoint", "other"), ("title", {}), ("class", None),
    ("starttime", "2026-01-01T11:00:00Z"), ("starttime", "2026-01-01T12:10:00Z"),
])
def test_stale_malformed_wrong_channel_rejected(path, value):
    data = fixture()
    (data if path in data else data["data"])[path] = value
    with pytest.raises(ValueError): am.parse(data, now=NOW)


def test_transport_is_pinned_bounded_no_redirect_credentials_or_compression(monkeypatch):
    client = httpx.AsyncClient
    calls = []
    def respond(request):
        calls.append(request)
        assert request.url.host == "203.0.113.42"
        assert request.headers["host"] == "www.antenne.de"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(200, headers={"content-type":"application/json"}, stream=Chunks(json.dumps(fixture()).encode()))
    monkeypatch.setattr(am, "validate_outbound_http_url", lambda *a, **k: UrlValidation(True,"ok",hostname="www.antenne.de",scheme="https",port=443,addresses=("203.0.113.42",)))
    monkeypatch.setattr(am.httpx,"AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(am.time,"time",lambda:NOW)
    assert asyncio.run(am.probe())["track"] == "Example Song"
    assert len(calls) == 1


@pytest.mark.parametrize("status,headers,body", [
    (302, {"location":"http://127.0.0.1/secret"}, b""),
    (200, {"content-type":"application/json"}, b"x" * 32769),
    (200, {"content-type":"text/html"}, b"<html>"),
    (200, {"content-type":"application/json"}, b"[bad"),
], ids=["redirect", "oversized", "html", "malformed"])
def test_response_failures_never_follow_redirect(monkeypatch, status, headers, body):
    client = httpx.AsyncClient
    calls=[]
    def respond(request):
        calls.append(request)
        return httpx.Response(status,headers=headers,stream=Chunks(body))
    monkeypatch.setattr(am, "validate_outbound_http_url", lambda *a, **k: UrlValidation(True,"ok",hostname="www.antenne.de",scheme="https",port=443,addresses=("203.0.113.42",)))
    monkeypatch.setattr(am.httpx,"AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    assert asyncio.run(am.probe()) is None
    assert len(calls)==1


def test_known_station_programme_api_and_failure_fallback(monkeypatch):
    calls=[]
    async def programme():return am.parse(fixture(),now=NOW)
    async def icy(url,name):calls.append(url);return {"status":"AVAILABLE","track":"","artist":"","other_info":"Slogan"}
    monkeypatch.setattr(am,"probe",programme);monkeypatch.setattr(sm,"probe_icy",icy)
    url="http://mp3channels.webradio.antenne.de/antenne.aac"
    assert asyncio.run(sm.probe_station_metadata(url,"ANTENNE"))["track"]=="Example Song"
    assert not calls
    async def unavailable():return None
    monkeypatch.setattr(am,"probe",unavailable)
    assert asyncio.run(sm.probe_station_metadata(url,"ANTENNE"))["track"]==""
    assert calls==[url]
