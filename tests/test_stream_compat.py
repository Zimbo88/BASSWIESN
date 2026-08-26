import asyncio

from basswiesn.app.services.network_security import UrlValidation, pinned_http_target
from basswiesn.app.services.stream_compat import (
    ProtectedStreamTarget,
    analyze_stream_url,
    is_hls_stream,
    probe_stream_reachability,
    resolve_stream_url,
)


def test_hls_detection_by_url_path_and_mime_and_content():
    assert is_hls_stream("https://example.test/live.m3u8")
    assert is_hls_stream("https://example.test/hls/192/seglist")
    assert is_hls_stream("https://example.test/live", "application/vnd.apple.mpegurl")
    assert is_hls_stream("https://example.test/live", body_preview="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1")


def test_direct_audio_scores_above_hls():
    assert analyze_stream_url("https://example.test/live.mp3").compatibility_score > analyze_stream_url("https://example.test/live.m3u8").compatibility_score
    assert analyze_stream_url("https://example.test/live.aac").compatibility_score > analyze_stream_url("https://example.test/hls/192/seglist.m3u8").compatibility_score


def test_high_bitrate_aac_is_warned_but_not_blocked():
    restricted = analyze_stream_url("https://example.test/live/aac/256")
    high = analyze_stream_url("https://example.test/live/aac/320")
    mp3_high = analyze_stream_url("https://example.test/live/mp3/320")

    assert restricted.stream_format == "aac"
    assert restricted.compatibility_score < analyze_stream_url("https://example.test/live/aac/128").compatibility_score
    assert "hohe AAC-Bitrate" in restricted.compatibility_warning
    assert high.compatibility_score < restricted.compatibility_score
    assert "AAC 320 kbps" in high.compatibility_warning
    assert mp3_high.stream_format == "mp3"
    assert mp3_high.is_direct_audio is True
    assert "Hohe Bitrate" in mp3_high.compatibility_warning


def test_dispatcher_path_segments_are_detected_as_direct_audio():
    mp3 = analyze_stream_url("https://dispatcher.rndfnk.com/br/br1/obb/mp3/mid")
    aac = analyze_stream_url("https://dispatcher.example/radio/aac/high")
    ogg = analyze_stream_url("https://dispatcher.example/radio/ogg/high")

    assert mp3.stream_format == "mp3"
    assert mp3.compatibility_score == 100
    assert mp3.is_direct_audio is True
    assert aac.stream_format == "aac"
    assert ogg.stream_format == "ogg"


def test_m3u_playlist_resolves_to_mp3(monkeypatch):
    monkeypatch.setattr(
        "basswiesn.app.services.network_security.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    class Response:
        content = b"#EXTM3U\nhttp://cdn.example/live.mp3\n"
        text = content.decode()
        headers = {"content-type": "audio/x-mpegurl"}
        url = "http://example.test/list.m3u"

    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("basswiesn.app.services.stream_compat.httpx.AsyncClient", Client)
    result = asyncio.run(resolve_stream_url("http://example.test/list.m3u"))

    assert result.stream_url_resolved == "http://cdn.example/live.mp3"
    assert result.stream_format == "mp3"


def test_pls_playlist_resolves_to_aac(monkeypatch):
    monkeypatch.setattr(
        "basswiesn.app.services.network_security.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    class Response:
        content = b"[playlist]\nFile1=http://cdn.example/live.aac\n"
        text = content.decode()
        headers = {"content-type": "audio/x-scpls"}
        url = "http://example.test/list.pls"

    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("basswiesn.app.services.stream_compat.httpx.AsyncClient", Client)
    result = asyncio.run(resolve_stream_url("http://example.test/list.pls"))

    assert result.stream_url_resolved == "http://cdn.example/live.aac"
    assert result.stream_format == "aac"


def test_resolver_timeout_does_not_crash(monkeypatch):
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def get(self, *args, **kwargs):
            raise TimeoutError("slow")

    monkeypatch.setattr("basswiesn.app.services.stream_compat.httpx.AsyncClient", Client)
    result = asyncio.run(resolve_stream_url("http://example.test/live.mp3"))

    assert result.stream_url_resolved == "http://example.test/live.mp3"
    assert result.stream_format == "mp3"


def test_pinned_target_prefers_validated_ipv4_but_supports_ipv6_only():
    dual = UrlValidation(
        True,
        "ok",
        hostname="radio.example",
        addresses=("2001:db8::42", "93.184.216.34"),
        scheme="https",
        port=443,
    )
    ipv6 = UrlValidation(
        True,
        "ok",
        hostname="radio.example",
        addresses=("2001:db8::42",),
        scheme="https",
        port=443,
    )

    dual_url, dual_headers, _extensions = pinned_http_target(
        "https://radio.example/live.mp3", dual
    )
    ipv6_url, _, _ = pinned_http_target(
        "https://radio.example/live.mp3", ipv6
    )

    assert dual_url == "https://93.184.216.34/live.mp3"
    assert dual_headers["Host"] == "radio.example"
    assert ipv6_url == "https://[2001:db8::42]/live.mp3"


def test_explicit_stream_probe_reports_direct_audio(monkeypatch):
    monkeypatch.setattr(
        "basswiesn.app.services.stream_compat._validate_stream_target",
        lambda _url: UrlValidation(
            True,
            "ok",
            hostname="radio.example",
            addresses=("93.184.216.34",),
            scheme="http",
            port=80,
        ),
    )

    class Response:
        status_code = 206
        headers = {"content-type": "audio/mpeg"}
        content = b"ID3"
        text = "ID3"
        async def aiter_bytes(self):
            yield self.content

    class StreamContext:
        async def __aenter__(self):
            return Response()
        async def __aexit__(self, *args):
            return False

    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def stream(self, *args, **kwargs):
            return StreamContext()

    monkeypatch.setattr(
        "basswiesn.app.services.stream_compat.httpx.AsyncClient", Client
    )
    result = asyncio.run(
        probe_stream_reachability("http://radio.example/live.mp3")
    )

    assert result["status"] == "VALID"
    assert result["reachable"] is True
    assert result["codec"] == "mp3"


def test_explicit_stream_probe_stops_after_first_chunk_when_range_is_ignored(monkeypatch):
    monkeypatch.setattr(
        "basswiesn.app.services.stream_compat._validate_stream_target",
        lambda _url: UrlValidation(True, "ok", hostname="radio.example", addresses=("93.184.216.34",), scheme="http", port=80),
    )
    chunks_requested = []

    class Response:
        status_code = 200
        headers = {"content-type": "audio/mpeg"}
        async def aiter_bytes(self):
            chunks_requested.append(1)
            yield b"ID3" + (b"x" * 8192)
            raise AssertionError("the checker must close an endless stream after the preview")

    class StreamContext:
        async def __aenter__(self):
            return Response()
        async def __aexit__(self, *args):
            return False

    class Client:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def stream(self, *args, **kwargs): return StreamContext()

    monkeypatch.setattr("basswiesn.app.services.stream_compat.httpx.AsyncClient", Client)
    result = asyncio.run(probe_stream_reachability("http://radio.example/live.mp3"))

    assert result["status"] == "VALID"
    assert chunks_requested == [1]


def test_explicit_stream_probe_blocks_protected_target_before_transport(monkeypatch):
    def protected(_url):
        raise ProtectedStreamTarget("stream URL resolves to a protected device")

    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("protected stream must be blocked before HTTP")

    monkeypatch.setattr(
        "basswiesn.app.services.stream_compat._validate_stream_target", protected
    )
    monkeypatch.setattr(
        "basswiesn.app.services.stream_compat.httpx.AsyncClient", ForbiddenClient
    )
    result = asyncio.run(
        probe_stream_reachability("http://protected.example/live.mp3")
    )

    assert result["status"] == "BROKEN"
    assert result["reachable"] is False
    assert result["protected"] is True
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
