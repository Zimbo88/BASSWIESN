"""Mock HTTPS/DNS and synthetic subprocesses only; never download real assets."""
from email.message import Message
import io
import ipaddress
import multiprocessing
import os
import socket
from types import SimpleNamespace
import time

import pytest

from basswiesn.update_helper import official_transport as transport


pytestmark = pytest.mark.unit
URL = transport.API + "tags/v3.0.0"
ASSET = transport.API + "assets/201"
REDIRECT = "https://" + transport.CDN + "/github-production-release-asset/12345/1234-abcd?sig=SYNTHETIC_SIGNATURE"


@pytest.mark.parametrize("url", [
    "http://api.github.com/repos/Zimbo88/BASSWIESN/releases/tags/v3.0.0",
    "https://example.invalid/release", "https://api.github.com.evil.invalid/path",
    transport.API + "latest", transport.API + "tags/v03.0.0", transport.API + "tags/v3.0.0-rc1",
    transport.API + "tags/v3.0.0?token=anything", transport.API + "assets/0",
    transport.API + "assets/-1", transport.API + "assets/1/../2",
    transport.API + "assets/1#anything", transport.API + "assets/1\n", None,
])
def test_only_exact_official_initial_routes(url, monkeypatch):
    monkeypatch.setattr(transport.multiprocessing, "get_context", lambda *a: pytest.fail("worker started for invalid URL"))
    with pytest.raises(transport.DownloadRejected, match="^DOWNLOAD_TARGET$"):
        transport.fetch_into(url, io.BytesIO(), maximum=100, deadline=time.monotonic() + 2)


@pytest.mark.parametrize("url", [
    REDIRECT.replace("https:", "http:"), REDIRECT.replace(transport.CDN, "example.invalid"),
    REDIRECT.replace(transport.CDN, transport.CDN + ".example.invalid"),
    REDIRECT.replace(transport.CDN, "user:password@" + transport.CDN),
    REDIRECT.replace(transport.CDN, transport.CDN + ":443"), REDIRECT + "#fragment",
    REDIRECT + "\n", REDIRECT + " ", REDIRECT.replace("/12345/", "/../"),
    REDIRECT.replace("github-production-release-asset", "other"), REDIRECT + "x" * 8192,
])
def test_redirect_cannot_escape_exact_cdn_or_smuggle_headers(url):
    with pytest.raises(transport.DownloadRejected, match="^DOWNLOAD_TARGET$"):
        transport.redirected_url(url)


def record(ip):
    v6 = ":" in ip
    return (socket.AF_INET6 if v6 else socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
            "", (ip, 443, 0, 0) if v6 else (ip, 443))


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.1.1", "0.0.0.0", "224.0.0.1",
    "192.0.2.42", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1",
    "64:ff9b::a00:1", "2002:7f00:1::", "2001::1"])
def test_dns_rejects_private_special_or_translated_targets_before_connect(ip, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [record(ip)])
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("connected to unsafe target"))
    with pytest.raises(transport.DownloadRejected, match="^DOWNLOAD_TARGET$"):
        transport.public_addresses("api.github.com")


def test_mixed_public_private_resolution_rejected_as_a_whole(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [record("1.1.1.1"), record("10.0.0.1")])
    with pytest.raises(transport.DownloadRejected):
        transport.public_addresses("api.github.com")


@pytest.mark.parametrize("records", [[], [record("1.1.1.1")] * 33,
    [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 80))],
    [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:4700:4700::1111", 443, 0, 1))]])
def test_resolver_count_port_scope_and_family_are_bounded(records, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: records)
    with pytest.raises(transport.DownloadRejected):
        transport.public_addresses("api.github.com")


def test_public_resolution_deduplicates_and_preserves_dual_stack(monkeypatch):
    records = [record("1.1.1.1"), record("2606:4700:4700::1111"), record("1.1.1.1")]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: records)
    result = transport.public_addresses("api.github.com")
    assert len(result) == 2
    assert {item[0] for item in result} == {socket.AF_INET, socket.AF_INET6}


def test_pinned_connection_uses_numeric_addresses_and_real_host_sni(monkeypatch):
    calls, sockets = [], []
    addresses = [(socket.AF_INET6, 6, ("2606:4700:4700::1111", 443, 0, 0)),
                 (socket.AF_INET, 6, ("1.1.1.1", 443))]
    monkeypatch.setattr(transport, "public_addresses", lambda host: addresses)
    class FakeSocket:
        def __init__(self, *args):
            self.closed = False
            sockets.append(self)
        def settimeout(self, timeout):
            assert timeout == 5
        def connect(self, endpoint):
            calls.append(endpoint)
            if len(calls) == 1:
                raise OSError("synthetic unavailable first family")
        def close(self):
            self.closed = True
    monkeypatch.setattr(socket, "socket", FakeSocket)
    connection = transport._PinnedConnection("api.github.com", timeout=5)
    def wrap(raw, server_hostname):
        assert server_hostname == "api.github.com"
        return raw
    connection._context = SimpleNamespace(wrap_socket=wrap)
    connection.connect()
    assert calls == [value[2] for value in addresses]
    assert sockets[0].closed and connection.sock is sockets[1]
    connection.close()


class Response:
    def __init__(self, status=200, data=b"fixture", headers=None):
        self.status = status
        self.headers = Message()
        for name, value in headers or []:
            self.headers[name] = value
        self.body = io.BytesIO(data)
    def read1(self, size):
        return self.body.read(size)


class Sender:
    def __init__(self):
        self.messages = []
        self.closed = False
    def send_bytes(self, data):
        self.messages.append(data)
    def close(self):
        self.closed = True


def worker(monkeypatch, responses, *, url=URL, maximum=100):
    # Clear only this synthetic dictionary, not the actual test runner env.
    monkeypatch.setattr(transport.os, "environ", {"HTTPS_PROXY": "synthetic", "SSL_CERT_FILE": "synthetic"})
    calls = []
    def context():
        assert not os.environ
        return object()
    monkeypatch.setattr(transport.ssl, "create_default_context", context)
    class Connection:
        def __init__(self, host, port, **kwargs):
            self.host, self.port = host, port
        def request(self, method, path, headers):
            calls.append((self.host, self.port, method, path, headers))
        def getresponse(self):
            return responses.pop(0)
        def close(self):
            pass
    monkeypatch.setattr(transport, "_PinnedConnection", Connection)
    sender = Sender()
    transport._network_worker(url, maximum, sender)
    assert sender.closed
    return sender.messages, calls


def test_worker_fixed_headers_no_auth_cookie_proxy_or_environment(monkeypatch):
    messages, calls = worker(monkeypatch, [Response(headers=[("Content-Length", "7")])])
    assert messages == [b"Dfixture", b"OK"]
    headers = calls[0][4]
    assert set(headers) == {"Accept", "Accept-Encoding", "User-Agent", "Cache-Control"}
    assert headers["Accept-Encoding"] == "identity"


def test_worker_handles_one_valid_asset_redirect_without_returning_query(monkeypatch):
    messages, calls = worker(monkeypatch, [Response(302, headers=[("Location", REDIRECT)]), Response()],
                             url=ASSET)
    assert messages == [b"Dfixture", b"OK"]
    assert len(calls) == 2 and calls[1][0] == transport.CDN
    assert b"SYNTHETIC_SIGNATURE" not in b"".join(messages)
    assert calls[1][4]["Accept"] == "application/octet-stream"


@pytest.mark.parametrize("status", [206, 301, 303, 307, 308, 400, 401, 403, 404, 429, 500])
def test_unexpected_status_has_no_body_or_raw_error_output(monkeypatch, status):
    messages, _ = worker(monkeypatch, [Response(status, b"SECRET_UPSTREAM_ERROR")], url=ASSET)
    assert messages == [b"EDOWNLOAD_FAILED"]


def test_metadata_redirect_is_not_followed(monkeypatch):
    messages, calls = worker(monkeypatch, [Response(302, headers=[("Location", REDIRECT)])])
    assert messages == [b"EDOWNLOAD_FAILED"] and len(calls) == 1


def test_second_redirect_cannot_extend_chain(monkeypatch):
    messages, calls = worker(monkeypatch, [Response(302, headers=[("Location", REDIRECT)]),
                                          Response(302, headers=[("Location", REDIRECT)])], url=ASSET)
    assert messages == [b"EDOWNLOAD_FAILED"] and len(calls) == 2


@pytest.mark.parametrize("headers", [
    [("Content-Length", "8")], [("Content-Length", "6")], [("Content-Length", "0")],
    [("Content-Length", "999")], [("Content-Length", "bad")], [("Content-Length", "07")],
    [("Content-Length", "7"), ("Content-Length", "7")],
    [("Content-Encoding", "gzip")], [("Transfer-Encoding", "unexpected")],
    [("Transfer-Encoding", "chunked"), ("Content-Length", "7")],
])
def test_body_header_mismatch_limits_and_ambiguity(monkeypatch, headers):
    messages, _ = worker(monkeypatch, [Response(headers=headers)])
    assert messages[-1].startswith(b"E") and b"OK" not in messages


def test_stream_without_content_length_still_bounded(monkeypatch):
    messages, _ = worker(monkeypatch, [Response(data=b"x" * 101)], maximum=100)
    assert messages == [b"EDOWNLOAD_LIMIT"]


def test_worker_error_output_does_not_fail_when_parent_is_gone(monkeypatch):
    class Disconnected:
        def send_bytes(self, data):
            raise BrokenPipeError("synthetic")
    transport._send_error(Disconnected(), "DOWNLOAD_FAILED")


# Spawn targets: no network, no application initialization, no unbounded wait.
def success_worker(url, maximum, sender):
    sender.send_bytes(b"Dabc")
    sender.send_bytes(b"OK")
    sender.close()


def slow_worker(url, maximum, sender):
    time.sleep(10)
    sender.close()


def no_completion_worker(url, maximum, sender):
    sender.send_bytes(b"Dabc")
    sender.close()


def raw_error_worker(url, maximum, sender):
    sender.send_bytes(b"ENEVER_PRINT_RAW_FAILURE")
    sender.close()


def oversized_worker(url, maximum, sender):
    sender.send_bytes(b"Dabcd")
    sender.close()


def invalid_frame_worker(url, maximum, sender):
    sender.send_bytes(b"?invalid")
    sender.close()


def giant_frame_worker(url, maximum, sender):
    try:
        sender.send_bytes(b"D" + b"x" * (transport.CHUNK + 1))
    except (OSError, ValueError):
        pass
    finally:
        sender.close()


def false_completion_worker(url, maximum, sender):
    sender.send_bytes(b"Dabc")
    sender.send_bytes(b"OK")
    time.sleep(10)


def test_completion_frame_requires_clean_worker_exit():
    before = {child.pid for child in multiprocessing.active_children()}
    with pytest.raises(transport.DownloadRejected, match="^DOWNLOAD_FAILED$"):
        transport.fetch_into(URL, io.BytesIO(), maximum=3, deadline=time.monotonic() + 4,
                             _worker=false_completion_worker)
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_parent_collects_bounded_frames_and_reaps_worker():
    before = {child.pid for child in multiprocessing.active_children()}
    output = io.BytesIO()
    assert transport.fetch_into(URL, output, maximum=3, deadline=time.monotonic() + 5, _worker=success_worker) == 3
    assert output.getvalue() == b"abc"
    assert {child.pid for child in multiprocessing.active_children()} == before


@pytest.mark.parametrize("target,code", [(no_completion_worker, "DOWNLOAD_FAILED"),
    (raw_error_worker, "DOWNLOAD_FAILED"), (oversized_worker, "DOWNLOAD_LIMIT"),
    (invalid_frame_worker, "DOWNLOAD_FAILED"), (giant_frame_worker, "DOWNLOAD_FAILED")])
def test_worker_failures_never_succeed_or_leak_details(target, code):
    before = {child.pid for child in multiprocessing.active_children()}
    with pytest.raises(transport.DownloadRejected, match="^" + code + "$"):
        transport.fetch_into(URL, io.BytesIO(), maximum=3, deadline=time.monotonic() + 5, _worker=target)
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_hard_deadline_reaps_stuck_worker():
    before = {child.pid for child in multiprocessing.active_children()}
    start = time.monotonic()
    with pytest.raises(transport.DownloadRejected, match="^DOWNLOAD_TIMEOUT$"):
        transport.fetch_into(URL, io.BytesIO(), maximum=3, deadline=start + 0.6, _worker=slow_worker)
    assert time.monotonic() - start < 3
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_partial_output_writes_are_completed():
    class ShortWriter(io.BytesIO):
        def write(self, data):
            return super().write(data[:1])
    output = ShortWriter()
    transport.fetch_into(URL, output, maximum=3, deadline=time.monotonic() + 5, _worker=success_worker)
    assert output.getvalue() == b"abc"


def test_disk_write_failure_closes_and_reaps_worker():
    class BadWriter:
        def write(self, data):
            return 0
    with pytest.raises(transport.DownloadRejected, match="^DOWNLOAD_IO$"):
        transport.fetch_into(URL, BadWriter(), maximum=3, deadline=time.monotonic() + 5, _worker=success_worker)


@pytest.mark.parametrize("maximum", [0, -1, True, transport.MAX_BODY + 1])
def test_parent_budget_invalid_before_worker(maximum, monkeypatch):
    monkeypatch.setattr(transport.multiprocessing, "get_context", lambda *a: pytest.fail("unexpected worker"))
    with pytest.raises(transport.DownloadRejected, match="^DOWNLOAD_LIMIT$"):
        transport.fetch_into(URL, io.BytesIO(), maximum=maximum, deadline=time.monotonic() + 1)


@pytest.mark.parametrize("deadline", [0, float("inf"), float("nan"), None, True, "tomorrow"])
def test_bad_deadline_never_spawns_worker(deadline, monkeypatch):
    monkeypatch.setattr(transport.multiprocessing, "get_context", lambda *a: pytest.fail("unexpected worker"))
    with pytest.raises(transport.DownloadRejected, match="^DOWNLOAD_TIMEOUT$"):
        transport.fetch_into(URL, io.BytesIO(), maximum=3, deadline=deadline)
