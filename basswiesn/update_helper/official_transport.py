"""Fixed-origin HTTPS acquisition; no proxies, credentials or private targets.

The network worker is spawned, bounded and reaped. Its DNS, TLS/header parsing
and slow body reads all sit inside the parent's hard deadline. Signed CDN query
values stay in the worker and never become command-line arguments or reports.
"""
import http.client
import ipaddress
import multiprocessing
import math
import os
import re
import socket
import ssl
import time
from urllib.parse import urlsplit


API = "https://api.github.com/repos/Zimbo88/BASSWIESN/releases/"
CDN = "release-assets.githubusercontent.com"
CHUNK = 65536
MAX_BODY = 128 * 1024 * 1024
MAX_URL = 8192
VERSION = r"(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})"


class DownloadRejected(ValueError):
    """Fixed content-free code; never include a URL or upstream response."""
    def __init__(self, code="DOWNLOAD_FAILED"):
        if code not in {"DOWNLOAD_FAILED", "DOWNLOAD_TIMEOUT", "DOWNLOAD_LIMIT", "DOWNLOAD_TARGET", "DOWNLOAD_IO"}:
            code = "DOWNLOAD_FAILED"
        self.code = code
        super().__init__(code)


def initial_url(url):
    if (not isinstance(url, str) or not url.startswith(API)
            or not re.fullmatch(r"(?:tags/v" + VERSION + r"|assets/[1-9][0-9]{0,19})", url[len(API):])):
        raise DownloadRejected("DOWNLOAD_TARGET")
    return urlsplit(url)


def redirected_url(url):
    if (not isinstance(url, str) or not 0 < len(url) <= MAX_URL
            or any(ord(c) < 33 or ord(c) > 126 for c in url) or "\\" in url):
        raise DownloadRejected("DOWNLOAD_TARGET")
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != CDN or parts.fragment
            or not re.fullmatch(r"/github-production-release-asset/[1-9][0-9]{0,19}/[a-fA-F0-9-]{1,80}", parts.path)):
        raise DownloadRejected("DOWNLOAD_TARGET")
    return parts


def public_addresses(host):
    records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not records or len(records) > 32:
        raise DownloadRejected("DOWNLOAD_TARGET")
    result = []
    for family, kind, protocol, _, endpoint in records:
        address = ipaddress.ip_address(endpoint[0])
        if (family not in {socket.AF_INET, socket.AF_INET6} or kind != socket.SOCK_STREAM
                or not address.is_global or address.is_multicast or address.is_unspecified
                or address.is_reserved or endpoint[1] != 443
                or getattr(address, "ipv4_mapped", None) is not None
                or (family == socket.AF_INET6 and (
                    address not in ipaddress.ip_network("2000::/3")
                    or address.sixtofour is not None or address.teredo is not None))
                or (family == socket.AF_INET6 and (endpoint[2] or endpoint[3]))):
            raise DownloadRejected("DOWNLOAD_TARGET")
        item = (family, protocol, endpoint)
        if item not in result:
            result.append(item)
    return result


class _PinnedConnection(http.client.HTTPSConnection):
    def connect(self):
        # No fallback DNS resolution after validation. Try IPv4 AND IPv6 from
        # the same validated snapshot; TLS SNI/certificate remain the real host.
        for family, protocol, endpoint in public_addresses(self.host):
            raw = socket.socket(family, socket.SOCK_STREAM, protocol)
            try:
                raw.settimeout(self.timeout)
                raw.connect(endpoint)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except (OSError, ssl.SSLError):
                raw.close()
        raise DownloadRejected()


def _body_headers(response, maximum):
    def single(name):
        values = response.headers.get_all(name, [])
        if len(values) > 1:
            raise DownloadRejected()
        return values[0] if values else None
    encoding = single("Content-Encoding")
    transfer, length = single("Transfer-Encoding"), single("Content-Length")
    if (encoding not in {None, "identity"} or transfer not in {None, "chunked"}
            or (transfer is not None and length is not None)):
        raise DownloadRejected()
    if length is not None:
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,12})", length):
            raise DownloadRejected()
        if not 0 < int(length) <= maximum:
            raise DownloadRejected("DOWNLOAD_LIMIT")
    return int(length) if length is not None else None


def _network_worker(url, maximum, sender):
    """Only safe bounded body frames or a fixed error cross back to the parent."""
    connection = None
    try:
        os.environ.clear()  # excludes proxy and SSL_CERT_FILE/DIR overrides
        context = ssl.create_default_context()
        parts = initial_url(url)
        allow_redirect = "/assets/" in parts.path
        for hop in range(2):
            connection = _PinnedConnection(parts.hostname, 443, timeout=5, context=context)
            path = parts.path + ("?" + parts.query if parts.query else "")
            connection.request("GET", path, headers={
                "Accept": "application/octet-stream" if allow_redirect else "application/vnd.github+json",
                "Accept-Encoding": "identity", "User-Agent": "BASSWIESN-Host-Update",
                "Cache-Control": "no-cache",
            })
            response = connection.getresponse()
            if response.status == 302 and hop == 0 and allow_redirect:
                locations = response.headers.get_all("Location", [])
                if len(locations) != 1:
                    raise DownloadRejected("DOWNLOAD_TARGET")
                parts = redirected_url(locations[0])
                connection.close()
                connection = None
                continue
            if response.status != 200:
                raise DownloadRejected()
            length = _body_headers(response, maximum)
            total = 0
            while block := response.read1(CHUNK):
                total += len(block)
                if total > maximum:
                    raise DownloadRejected("DOWNLOAD_LIMIT")
                sender.send_bytes(b"D" + block)
            if total == 0 or (length is not None and total != length):
                raise DownloadRejected()
            sender.send_bytes(b"OK")
            return
        raise DownloadRejected("DOWNLOAD_TARGET")
    except DownloadRejected as error:
        _send_error(sender, error.code)
    except Exception:
        _send_error(sender, "DOWNLOAD_FAILED")
    finally:
        try:
            if connection is not None:
                connection.close()
        except Exception:
            pass
        try:
            sender.close()
        except (OSError, ValueError):
            pass


def _send_error(sender, code):
    try:
        sender.send_bytes(b"E" + code.encode("ascii"))
    except (OSError, ValueError):
        pass  # Parent timed out/disconnected; no raw traceback or retry.


def fetch_into(url, output, *, maximum, deadline, _worker=_network_worker):
    """Internal host API. Request data cannot select arbitrary URLs or workers."""
    initial_url(url)
    if type(maximum) is not int or not 0 < maximum <= MAX_BODY:
        raise DownloadRejected("DOWNLOAD_LIMIT")
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise DownloadRejected("DOWNLOAD_TIMEOUT")
    remaining = deadline - time.monotonic()
    if not 0 < remaining <= 120:
        raise DownloadRejected("DOWNLOAD_TIMEOUT")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(url, maximum, sender), daemon=True)
    started = False
    try:
        process.start()
        started = True
        sender.close()
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not receiver.poll(remaining):
                raise DownloadRejected("DOWNLOAD_TIMEOUT")
            message = receiver.recv_bytes(CHUNK + 1)
            if message == b"OK":
                if total == 0:
                    raise DownloadRejected()
                process.join(timeout=min(1, max(0, deadline - time.monotonic())))
                if process.exitcode != 0:
                    raise DownloadRejected()
                return total
            if message.startswith(b"E"):
                raise DownloadRejected(message[1:].decode("ascii", errors="replace"))
            if not message.startswith(b"D") or len(message) <= 1:
                raise DownloadRejected()
            total += len(message) - 1
            if total > maximum:
                raise DownloadRejected("DOWNLOAD_LIMIT")
            view = memoryview(message)[1:]
            while view:
                written = output.write(view)
                if not isinstance(written, int) or written <= 0 or written > len(view):
                    raise DownloadRejected("DOWNLOAD_IO")
                view = view[written:]
    except DownloadRejected:
        raise
    except (OSError, ValueError, EOFError, RuntimeError):
        raise DownloadRejected() from None
    finally:
        receiver.close()
        sender.close()
        if started:
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
            if process.is_alive():
                process.kill()
            process.join(timeout=1)
        process.close()
