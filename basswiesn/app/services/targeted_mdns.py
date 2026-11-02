"""Bounded, selected-device-only mDNS evidence collection.

The normal discovery path is intentionally not used here: an AirPlay
readiness probe sends legacy-unicast PTR questions only to the radio selected
by the user.  The central protected-target gate runs before a UDP socket is
created.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import time
from typing import Any

from basswiesn.app.services.network_safety import assert_transport_allowed


AIRPLAY_MDNS_SERVICES = ("_airplay._tcp.local", "_raop._tcp.local")


def _encode_name(name: str) -> bytes:
    labels = str(name or "").strip(".").split(".")
    encoded = bytearray()
    for label in labels:
        value = label.encode("utf-8")
        if not value or len(value) > 63:
            raise ValueError("invalid DNS label")
        encoded.append(len(value))
        encoded.extend(value)
    encoded.append(0)
    return bytes(encoded)


def _ptr_query(name: str) -> bytes:
    # QU requests a unicast response; the ephemeral source port also selects
    # RFC 6762 legacy-unicast handling. No multicast/subnet scan is emitted.
    return struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + _encode_name(name) + struct.pack(
        "!HH", 12, 0x8001
    )


def _read_name(packet: bytes, offset: int, *, depth: int = 0) -> tuple[str, int]:
    if depth > 20:
        raise ValueError("DNS compression pointer loop")
    labels: list[str] = []
    end = offset
    jumped = False
    while True:
        if offset >= len(packet):
            raise ValueError("truncated DNS name")
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("truncated DNS pointer")
            pointer = ((length & 0x3F) << 8) | packet[offset + 1]
            pointed, _ = _read_name(packet, pointer, depth=depth + 1)
            if pointed:
                labels.append(pointed)
            if not jumped:
                end = offset + 2
            jumped = True
            break
        if length & 0xC0:
            raise ValueError("unsupported DNS label encoding")
        offset += 1
        if length == 0:
            if not jumped:
                end = offset
            break
        if offset + length > len(packet):
            raise ValueError("truncated DNS label")
        labels.append(packet[offset : offset + length].decode("utf-8", errors="replace"))
        offset += length
        if not jumped:
            end = offset
    return ".".join(label for label in labels if label), end


def parse_mdns_packet(packet: bytes) -> list[dict[str, Any]]:
    """Return bounded, non-secret DNS records useful for readiness evidence."""

    if len(packet) < 12:
        raise ValueError("truncated DNS header")
    _ident, _flags, questions, answers, authorities, additional = struct.unpack(
        "!HHHHHH", packet[:12]
    )
    offset = 12
    for _ in range(questions):
        _name, offset = _read_name(packet, offset)
        if offset + 4 > len(packet):
            raise ValueError("truncated DNS question")
        offset += 4

    records: list[dict[str, Any]] = []
    for _ in range(min(answers + authorities + additional, 128)):
        name, offset = _read_name(packet, offset)
        if offset + 10 > len(packet):
            raise ValueError("truncated DNS record")
        record_type, record_class, ttl, length = struct.unpack(
            "!HHIH", packet[offset : offset + 10]
        )
        offset += 10
        end = offset + length
        if end > len(packet):
            raise ValueError("truncated DNS record data")
        record: dict[str, Any] = {
            "name": name,
            "type": record_type,
            "class": record_class & 0x7FFF,
            "ttl": ttl,
        }
        if record_type == 12:  # PTR
            record["target"], _ = _read_name(packet, offset)
        elif record_type == 33 and length >= 6:  # SRV
            priority, weight, port = struct.unpack("!HHH", packet[offset : offset + 6])
            target, _ = _read_name(packet, offset + 6)
            record.update(
                {"priority": priority, "weight": weight, "port": port, "target": target}
            )
        elif record_type == 16:  # TXT
            values: dict[str, str] = {}
            cursor = offset
            while cursor < end and len(values) < 64:
                item_length = packet[cursor]
                cursor += 1
                item = packet[cursor : min(cursor + item_length, end)].decode(
                    "utf-8", errors="replace"
                )
                cursor += item_length
                key, separator, value = item.partition("=")
                if key:
                    values[key[:64]] = value[:512] if separator else ""
            record["txt"] = values
        elif record_type == 1 and length == 4:  # A
            record["address"] = str(ipaddress.ip_address(packet[offset:end]))
        elif record_type == 28 and length == 16:  # AAAA
            record["address"] = str(ipaddress.ip_address(packet[offset:end]))
        records.append(record)
        offset = end
    return records


def probe_targeted_airplay_mdns(
    ip_address: str,
    *,
    device_id: str,
    timeout_seconds: float = 1.5,
) -> dict[str, Any]:
    """Query only one already selected radio, never the multicast group."""

    target = assert_transport_allowed(
        ip_address,
        device_id=device_id,
        transport="targeted_airplay_mdns_udp",
    )
    parsed_target = ipaddress.ip_address(target)
    family = socket.AF_INET6 if parsed_target.version == 6 else socket.AF_INET
    destination = (target, 5353, 0, 0) if family == socket.AF_INET6 else (target, 5353)
    services: dict[str, dict[str, Any]] = {}
    for service in AIRPLAY_MDNS_SERVICES:
        records: list[dict[str, Any]] = []
        response_count = 0
        deadline = time.monotonic() + max(0.1, min(float(timeout_seconds), 3.0))
        with socket.socket(family, socket.SOCK_DGRAM) as udp:
            udp.settimeout(max(0.1, deadline - time.monotonic()))
            udp.sendto(_ptr_query(service), destination)
            while time.monotonic() < deadline and response_count < 8:
                try:
                    packet, source = udp.recvfrom(64 * 1024)
                except TimeoutError:
                    break
                if str(source[0]) != target:
                    continue
                response_count += 1
                try:
                    records.extend(parse_mdns_packet(packet))
                except ValueError:
                    continue
                udp.settimeout(max(0.05, deadline - time.monotonic()))
        service_lower = service.lower()
        visible = any(
            service_lower in str(record.get("name") or "").lower()
            or service_lower in str(record.get("target") or "").lower()
            for record in records
        )
        services[service] = {
            "visible": visible,
            "response_count": response_count,
            "records": records[:64],
        }
    visible_services = [name for name, result in services.items() if result["visible"]]
    observed_ttls = [
        int(record["ttl"])
        for result in services.values()
        for record in result["records"]
        if int(record.get("ttl") or 0) > 0
    ]
    return {
        "targeted": True,
        "transport": "UDP_LEGACY_UNICAST_MDNS",
        "target": target,
        "queried_services": list(AIRPLAY_MDNS_SERVICES),
        "visible_services": visible_services,
        "ttl_seconds": min(observed_ttls) if observed_ttls else None,
        # Silence is not proof that a service is absent; firewalls and mDNS
        # implementations may reject legacy-unicast questions.
        "mdns_visible": True if visible_services else None,
        "services": services,
    }
