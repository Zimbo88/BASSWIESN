"""No socket is opened: injected UDP results and HTTP fixtures only."""

import asyncio
import socket

import httpx
import pytest
from hypothesis import given, settings, strategies as st

from basswiesn.app import db as database
from basswiesn.app.models import Device
from basswiesn.app.services import dlna_discovery as discovery
from basswiesn.app.services import dlna_library as library
from test_dlna_library_300 import MediaFixture, BASE, UDN, fixture, web  # noqa: F401

pytestmark = pytest.mark.unit


def packet(location=BASE + "/root.xml", **extra):
    headers = {"ST": discovery.SEARCH_TARGET, "USN": UDN + "::" + discovery.SEARCH_TARGET,
               "LOCATION": location, **extra}
    return ("HTTP/1.1 200 OK\r\n" + "\r\n".join(f"{key}: {value}" for key, value in headers.items()) + "\r\n\r\n").encode()


def test_candidate_is_unverified_and_same_as_sender():
    found = discovery.candidate(packet(), "192.0.2.42")
    assert found["description_url"] == BASE + "/root.xml"
    assert found["verification"] == "UNVERIFIED"
    assert discovery.candidate(packet(), "192.0.2.43") is None


@pytest.mark.parametrize("data", [
    packet(ST="ssdp:all"), packet(ST="urn:schemas-upnp-org:device:MediaRenderer:1"),
    packet(USN="invalid"), packet(location="http://example.invalid/root.xml"),
    packet(location="http://127.0.0.1/root.xml"), packet(location="http://192.0.2.42:22/"),
    packet().replace(b"ST:", b" ST:"), packet() + b"junk", packet() + b"\0", b"x" * 8193,
    b"",
])
def test_malformed_or_non_media_candidates_are_ignored(data):
    assert discovery.candidate(data, "192.0.2.42") is None


def test_duplicate_headers_and_protected_targets_are_ignored(monkeypatch):
    duplicate = packet().replace(b"\r\n\r\n", b"\r\nlocation: http://192.0.2.42/other\r\n\r\n")
    assert discovery.candidate(duplicate, "192.0.2.42") is None
    monkeypatch.setattr(library, "is_device_access_protected", lambda _: True)
    assert discovery.candidate(packet(), "192.0.2.42") is None


def test_registered_radio_is_not_a_nas():
    with database.SessionLocal() as db:
        db.add(Device(device_id="EXAMPLE-RADIO", name="Radio", ip_address="192.0.2.42"))
        db.commit()
    assert discovery.candidate(packet(), "192.0.2.42") is None


@given(st.binary(max_size=9000))
@settings(max_examples=100, deadline=None)
def test_untrusted_datagram_parser_is_total(data):
    assert discovery.candidate(data, "192.0.2.42") is None


def test_discovery_requires_lab_enabled_and_explicit_action(web, monkeypatch):
    calls = []
    async def search():
        calls.append(1)
        return [discovery.candidate(packet(), "192.0.2.42")]
    monkeypatch.setattr(discovery, "search", search)
    assert web.get("/api/dlna/servers").status_code == 200
    assert calls == []
    assert web.post("/api/dlna/library/discover", json={"approve": True}).status_code == 409
    web.post("/api/dlna/settings", json={"enabled": True})
    assert web.post("/api/dlna/library/discover", json={}).status_code == 409
    assert calls == []
    result = web.post("/api/dlna/library/discover", json={"approve": True}).json()
    assert result["verification"] == "UNVERIFIED" and not result["description_fetched"]
    assert calls == [1]


def test_search_closes_socket_bounds_packets_and_never_fetches_description(monkeypatch):
    class FakeSocket:
        closed = False
        def __enter__(self): return self
        def __exit__(self, *_): self.closed = True
        def setblocking(self, value): assert value is False
        def setsockopt(self, *_): pass
        def bind(self, value): assert value == ("0.0.0.0", 0)
    fake = FakeSocket()
    async def run():
        loop = asyncio.get_running_loop()
        sent = []
        async def send(sock, data, address):
            assert sock is fake
            sent.append((data, address))
        async def receive(sock, size):
            assert size == 8193
            return packet(), ("192.0.2.42", 1900)
        monkeypatch.setattr(discovery.socket, "socket", lambda *_: fake)
        monkeypatch.setattr(loop, "sock_sendto", send)
        monkeypatch.setattr(loop, "sock_recvfrom", receive)
        monkeypatch.setattr(discovery, "_last_search", float("-inf"))
        monkeypatch.setattr(discovery, "MAX_PACKETS", 3)
        found = await discovery.search()
        assert len(found) == 1 and fake.closed
        assert sent == [(discovery.SEARCH_REQUEST, ("239.255.255.250", 1900))]
        with pytest.raises(library.DlnaError, match="DISCOVERY_BUSY"):
            await discovery.search()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["timeout", "send-timeout", "cancel", "socket-error", "limit"])
def test_discovery_failure_and_cancel_close_the_socket(monkeypatch, outcome):
    class FakeSocket:
        closed = False
        def __enter__(self): return self
        def __exit__(self, *_): self.closed = True
        def setblocking(self, _): pass
        def setsockopt(self, *_): pass
        def bind(self, _): pass

    fake = FakeSocket()
    async def run():
        loop = asyncio.get_running_loop()
        async def send(*_):
            if outcome == "send-timeout":
                raise TimeoutError()
            if outcome == "socket-error":
                raise OSError("synthetic private socket detail")
        async def receive(*_):
            if outcome == "timeout":
                raise TimeoutError()
            if outcome == "cancel":
                raise asyncio.CancelledError()
            return packet(), ("192.0.2.42", 1900)
        monkeypatch.setattr(discovery.socket, "socket", lambda *_: fake)
        monkeypatch.setattr(loop, "sock_sendto", send)
        monkeypatch.setattr(loop, "sock_recvfrom", receive)
        monkeypatch.setattr(discovery, "_last_search", float("-inf"))
        monkeypatch.setattr(discovery, "MAX_CANDIDATES", 1)
        monkeypatch.setattr(discovery, "_search_lock", asyncio.Lock())
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await discovery.search()
        elif outcome == "socket-error":
            with pytest.raises(library.DlnaError, match="^DISCOVERY_UNAVAILABLE$"):
                await discovery.search()
        else:
            result = await discovery.search()
            assert len(result) == (1 if outcome == "limit" else 0)
        assert fake.closed and not discovery._search_lock.locked()
    asyncio.run(run())


def test_wrong_status_and_protected_advertised_identity_are_rejected(monkeypatch):
    assert discovery.candidate(packet().replace(b"200 OK", b"404 Not Found"), "192.0.2.42") is None
    monkeypatch.setattr(discovery, "protected_device_ids", lambda: ["AA11BB22CC33"])
    assert discovery.candidate(packet(USN="uuid:AA11BB22CC33"), "192.0.2.42") is None


@pytest.mark.parametrize("url,expected", [
    ("http://[fd12:3456::1]:8200/root.xml", "http://[fd12:3456::1]:8200/root.xml"),
    ("http://[2001:db8::1]/root.xml", "http://[2001:db8::1]/root.xml"),
    ("http://[::ffff:192.0.2.42]:8200/root.xml", BASE + "/root.xml"),
])
def test_literal_ipv6_and_mapped_address_normalization(url, expected):
    assert library.endpoint(url) == expected


@pytest.mark.parametrize("host", ["::1", "::", "fe80::1", "ff02::1", "fe80::1%25eth0",
                                  "2002:c000:022a::", "64:ff9b::c000:22a", "2606:4700::1111"])
def test_unsafe_or_outside_lan_ipv6_is_rejected(host):
    with pytest.raises(library.DlnaError):
        library.endpoint(f"http://[{host}]:8200/root.xml")


def test_ipv6_inventory_spelling_cannot_bypass_radio_rejection():
    with database.SessionLocal() as db:
        db.add(Device(device_id="EXAMPLE-IPV6", name="Radio", ip_address="fd12:3456:0:0:0:0:0:1"))
        db.commit()
    with pytest.raises(library.DlnaError, match="RADIO_TARGET"):
        library.check_not_radio("http://[fd12:3456::1]:8200/root.xml")


@pytest.mark.parametrize("peer", [None, ("192.0.2.43", 8200), ("192.0.2.42", 80), ("invalid", 8200)])
def test_actual_connected_peer_is_verified(peer):
    class Stream:
        def get_extra_info(self, name):
            assert name == "server_addr"
            return peer
    with pytest.raises(library.DlnaError, match="CONNECTED_PEER_REJECTED"):
        library.ContentDirectory().verify_peer(httpx.Response(200, extensions={"network_stream": Stream()}), BASE)


def test_missing_actual_peer_is_not_accepted_in_runtime():
    with pytest.raises(library.DlnaError, match="CONNECTED_PEER_REJECTED"):
        library.ContentDirectory().verify_peer(httpx.Response(200), BASE)


def test_cover_uri_is_metadata_only_and_off_origin_cover_does_not_break_audio():
    from html import escape
    model = MediaFixture()
    server = library.description(model.description(), BASE + "/root.xml")
    for url, expected in (("/cover.jpg", BASE + "/cover.jpg"), ("http://example.invalid/private", "")):
        raw = model.soap().replace(escape("</item>").encode(),
            escape(f"<upnp:albumArtURI>{url}</upnp:albumArtURI></item>").encode())
        item = library.browse_response(raw, server, 0, 50)["items"][0]
        assert item["artwork_url"] == expected
        assert item["importable"] is True
