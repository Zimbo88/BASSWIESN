"""Explicit bounded SSDP media-server search; no description follow-up or writes.

Discovery only yields untrusted candidates. Connecting one is a separate user
action handled by the existing ContentDirectory identity and target policy.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from urllib.parse import urlsplit

from basswiesn.app.services.dlna_library import DlnaError, check_not_radio, endpoint
from basswiesn.app.services.protected_devices import protected_device_ids

SEARCH_TARGET = "urn:schemas-upnp-org:device:MediaServer:1"
SEARCH_REQUEST = (
    'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\n'
    f"MX: 2\r\nST: {SEARCH_TARGET}\r\n\r\n"
).encode("ascii")
MAX_PACKETS = 256
MAX_CANDIDATES = 32
COOLDOWN_SECONDS = 30
_search_lock = asyncio.Lock()
_last_search = float("-inf")


def candidate(data: bytes, sender: str) -> dict[str, str] | None:
    """Reject duplicate headers, cross-host LOCATION, protected and radio peers."""
    try:
        if len(data) > 8192 or b"\0" in data or not data.endswith(b"\r\n\r\n"):
            return None
        lines = data.decode("ascii").split("\r\n")
        if lines[0] != "HTTP/1.1 200 OK" or len(lines) > 48:
            return None
        headers: dict[str, str] = {}
        for line in lines[1:-2]:
            key, separator, value = line.partition(":")
            if (not separator or not re.fullmatch(r"[A-Za-z0-9-]+", key)
                    or any(ord(c) < 32 or ord(c) == 127 for c in value) or key.lower() in headers):
                return None
            headers[key.lower()] = value.strip()
        if headers.get("st") != SEARCH_TARGET:
            return None
        usn = headers.get("usn", "")
        identity, separator, target = usn.partition("::")
        if (not re.fullmatch(r"uuid:[A-Za-z0-9-]{1,128}", identity)
                or separator and target != SEARCH_TARGET):
            return None
        address = endpoint(headers.get("location", ""))
        if urlsplit(address).hostname != str(ipaddress.IPv4Address(sender)):
            return None
        compact_id = re.sub(r"[^A-Z0-9]", "", identity.upper())
        if any(value in compact_id for value in protected_device_ids()):
            return None
        check_not_radio(address)
        return {"description_url": address, "advertised_udn": identity, "verification": "UNVERIFIED"}
    except (ValueError, UnicodeError):
        return None


async def search() -> list[dict[str, str]]:
    """One IPv4 multicast query, TTL 1, three seconds, no HTTP follow-up.

    Socket ownership is scoped to this call and closes on cancellation. A
    process-local cooldown bounds accidental repeated button clicks; this is
    not a cross-worker authentication or global quota boundary.
    """
    global _last_search
    if _search_lock.locked() or time.monotonic() - _last_search < COOLDOWN_SECONDS:
        raise DlnaError("DISCOVERY_BUSY")
    async with _search_lock:
        _last_search = time.monotonic()
        found: dict[str, dict[str, str]] = {}
        loop = asyncio.get_running_loop()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setblocking(False)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
                sock.bind(("0.0.0.0", 0))
                try:
                    async with asyncio.timeout(3):
                        await loop.sock_sendto(sock, SEARCH_REQUEST, ("239.255.255.250", 1900))
                        for _ in range(MAX_PACKETS):
                            data, sender = await loop.sock_recvfrom(sock, 8193)
                            item = candidate(data, sender[0])
                            if item is not None:
                                found[item["description_url"]] = item
                            if len(found) >= MAX_CANDIDATES:
                                break
                except TimeoutError:
                    pass
        except OSError:
            raise DlnaError("DISCOVERY_UNAVAILABLE") from None
        return sorted(found.values(), key=lambda item: item["description_url"])
