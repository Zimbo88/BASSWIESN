from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
import socket
import time
from urllib.parse import urlparse

import httpx
from defusedxml import ElementTree as SafeET
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import Device, DiscoveryEvent
from basswiesn.app.services.events import create_event
from basswiesn.app.services.network_security import (
    pinned_http_target,
    validate_local_soundtouch_url,
)
from basswiesn.app.services.protected_devices import (
    is_explicit_discovery_target_protected,
    is_protected_ip,
)


SSDP_GROUP = ("239.255.255.250", 1900)
SSDP_ST_VALUES = (
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "ssdp:all",
)
MAX_SSDP_RESPONSE_BYTES = 8192
MAX_DESCRIPTOR_BYTES = 128 * 1024
SOUNDTOUCH_DESCRIPTOR_PORTS = {80, 8090, 8091}
SOUNDTOUCH_UPNP_UUID = re.compile(
    r"^BO5EBO5E-F00D-F00D-FEED-([0-9A-F]{12})$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SSDPCandidate:
    location: str
    usn: str
    server: str
    st: str
    remote_ip: str
    interface: str = ""

    def to_dict(self) -> dict:
        return {
            "location": self.location,
            "usn": self.usn,
            "server": self.server,
            "st": self.st,
            "remote_ip": self.remote_ip,
            "interface": self.interface,
        }


@dataclass(frozen=True)
class DiscoveryDevice:
    device_id: str
    ip_address: str
    name: str
    model: str
    location: str
    method: str
    confidence: int
    descriptor_validated: bool
    identity_verified: bool
    interface: str = ""
    raw: dict | None = None

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "ip_address": self.ip_address,
            "name": self.name,
            "model": self.model,
            "location": self.location,
            "method": self.method,
            "confidence": self.confidence,
            "descriptor_validated": self.descriptor_validated,
            "identity_verified": self.identity_verified,
            "interface": self.interface,
            "raw": self.raw or {},
        }


def parse_ssdp_response(data: bytes, remote_ip: str, interface: str = "") -> SSDPCandidate | None:
    if len(data) > MAX_SSDP_RESPONSE_BYTES:
        return None
    try:
        text = data.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return None
    headers: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    location = headers.get("location", "")
    if not location:
        return None
    return SSDPCandidate(
        location=location,
        usn=headers.get("usn", ""),
        server=headers.get("server", ""),
        st=headers.get("st", ""),
        remote_ip=remote_ip,
        interface=interface,
    )


def _msearch_packet(st: str) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {st}\r\n"
        "\r\n"
    ).encode("ascii")


def _send_msearch(timeout_seconds: float, interface: str = "") -> list[SSDPCandidate]:
    candidates: list[SSDPCandidate] = []
    deadline = time.monotonic() + max(timeout_seconds, 0.1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.25)
        if interface:
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
            except OSError:
                return candidates
        for st in SSDP_ST_VALUES:
            sock.sendto(_msearch_packet(st), SSDP_GROUP)
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(MAX_SSDP_RESPONSE_BYTES + 1)
            except socket.timeout:
                continue
            candidate = parse_ssdp_response(data, addr[0], interface=interface)
            if candidate is not None:
                candidates.append(candidate)
    return candidates


async def _fetch_descriptor(candidate: SSDPCandidate, *, timeout_seconds: int) -> tuple[bool, dict, str]:
    location_host = urlparse(candidate.location).hostname or candidate.remote_ip
    if is_protected_ip(location_host) or is_protected_ip(candidate.remote_ip):
        return False, {"protected": True, "host": location_host}, "protected device access blocked"
    # SoundTouch radios advertise their UPnP device descriptor on 8091 while
    # the public control API remains on 8090. Keep this narrowly scoped to the
    # descriptor fetch instead of widening every local HTTP validation call.
    validation = validate_local_soundtouch_url(
        candidate.location,
        allowed_ports=SOUNDTOUCH_DESCRIPTOR_PORTS,
    )
    if not validation.ok:
        return False, {"validation": validation.to_dict()}, validation.reason
    try:
        pinned_url, headers, extensions = pinned_http_target(
            candidate.location, validation
        )
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(
                pinned_url,
                headers=headers,
                extensions=extensions,
            )
            if response.status_code >= 300:
                return False, {"status_code": response.status_code}, "descriptor returned redirect or error"
            content = response.content
    except Exception as exc:
        return False, {"error_class": exc.__class__.__name__, "error": str(exc)}, "descriptor request failed"
    if len(content) > MAX_DESCRIPTOR_BYTES:
        return False, {"size": len(content)}, "descriptor response too large"
    try:
        root = SafeET.fromstring(content)
    except Exception as exc:
        return False, {"error_class": exc.__class__.__name__}, "descriptor XML invalid"
    fields = {
        "friendlyName": root.findtext(".//{*}friendlyName", default=""),
        "manufacturer": root.findtext(".//{*}manufacturer", default=""),
        "modelName": root.findtext(".//{*}modelName", default=""),
        "modelDescription": root.findtext(".//{*}modelDescription", default=""),
        "UDN": root.findtext(".//{*}UDN", default=""),
    }
    marker = " ".join(fields.values()).lower()
    if not any(token in marker for token in ("bose", "soundtouch")):
        return False, fields, "descriptor is not a Bose SoundTouch descriptor"
    return True, fields, "ok"


def _device_id_from_descriptor(fields: dict, candidate: SSDPCandidate) -> str:
    udn = str(fields.get("UDN") or candidate.usn or "")
    marker = "uuid:"
    if marker in udn:
        value = udn.split(marker, 1)[1].split("::", 1)[0].strip().upper()
        # Bose wraps the authoritative 12-hex SoundTouch device ID in this
        # fixed UPnP UUID prefix. Setup, protection and /info all use the
        # trailing device ID, so normalize before any identity decision.
        match = SOUNDTOUCH_UPNP_UUID.fullmatch(value)
        return match.group(1).upper() if match else value
    return ""


def _device_id_from_ssdp(candidate: SSDPCandidate) -> str:
    """Extract an advertised identity without opening a unicast transport."""

    return _device_id_from_descriptor({}, candidate)


def _ip_from_location(location: str, fallback: str) -> str:
    return urlparse(location).hostname or fallback


def record_discovery_event(db: Session, result: dict) -> None:
    db.add(DiscoveryEvent(
        device_id=result.get("device_id", ""),
        ip_address=result.get("ip_address", ""),
        method=result.get("method", "ssdp"),
        confidence=int(result.get("confidence", 0) or 0),
        location=result.get("location", ""),
        interface=result.get("interface", ""),
        descriptor_url=result.get("location", ""),
        descriptor_validated=bool(result.get("descriptor_validated")),
        identity_verified=bool(result.get("identity_verified")),
        result=result.get("result", "ok"),
        details_json=json.dumps(result.get("raw", {}), ensure_ascii=False),
    ))


def upsert_discovered_device(db: Session, discovered: DiscoveryDevice) -> Device:
    device = None
    if discovered.device_id:
        device = db.query(Device).filter(Device.device_id == discovered.device_id).one_or_none()
    if device is None:
        device = db.query(Device).filter(Device.ip_address == discovered.ip_address, Device.device_id == "").one_or_none()
    if device is None:
        device = Device(device_id=discovered.device_id or f"unverified-{discovered.ip_address}", ip_address=discovered.ip_address)
        db.add(device)
    old_ip = device.ip_address
    device.ip_address = discovered.ip_address
    device.name = discovered.name or device.name
    device.model = discovered.model or device.model
    device.last_seen = datetime.now(UTC)
    device.reachable = True
    device.discovery_method = discovered.method
    device.discovery_confidence = discovered.confidence
    device.discovery_last_seen = datetime.now(UTC)
    device.discovery_location = discovered.location
    device.discovered_interface = discovered.interface
    device.descriptor_url = discovered.location
    device.descriptor_validated = discovered.descriptor_validated
    device.identity_verified = discovered.identity_verified
    if old_ip and old_ip != discovered.ip_address:
        create_event(db, "device_ip_changed", device_id=device.device_id, payload={"old_ip": old_ip, "new_ip": discovered.ip_address, "method": discovered.method})
    else:
        create_event(db, "device_discovered", device_id=device.device_id, payload={"ip": discovered.ip_address, "method": discovered.method})
    return device


async def discover_ssdp(db: Session, *, interface: str = "", timeout_seconds: int | None = None, candidates: list[SSDPCandidate] | None = None) -> dict:
    settings = get_settings()
    timeout = timeout_seconds or settings.ssdp_timeout_seconds
    if not settings.ssdp_enabled and candidates is None:
        return {"enabled": False, "devices": [], "candidates": [], "errors": ["SSDP disabled"]}
    # A user-triggered M-SEARCH targets the multicast group, not a protected
    # radio. Responses from protected addresses are discarded below before a
    # descriptor fetch or any other unicast follow-up can be created.
    raw_candidates = candidates if candidates is not None else await asyncio.to_thread(_send_msearch, timeout, interface)
    deduped: dict[str, SSDPCandidate] = {}
    for candidate in raw_candidates:
        candidate_host = urlparse(candidate.location).hostname or candidate.remote_ip
        advertised_id = _device_id_from_ssdp(candidate)
        if not advertised_id:
            record_discovery_event(db, {**candidate.to_dict(), "method": "ssdp", "result": "blocked", "confidence": 0, "raw": {"reason": "SSDP identity missing; unicast follow-up refused"}})
            continue
        if (
            is_protected_ip(candidate_host)
            or is_protected_ip(candidate.remote_ip)
            or is_explicit_discovery_target_protected(candidate.remote_ip, advertised_id)
        ):
            record_discovery_event(db, {**candidate.to_dict(), "method": "ssdp", "result": "blocked", "confidence": 0, "raw": {"reason": "protected device access blocked"}})
            continue
        validation = validate_local_soundtouch_url(
            candidate.location,
            allowed_ports=SOUNDTOUCH_DESCRIPTOR_PORTS,
        )
        if validation.ok:
            deduped[candidate.location] = candidate
        else:
            record_discovery_event(db, {**candidate.to_dict(), "method": "ssdp", "result": "blocked", "confidence": 0, "raw": {"validation": validation.to_dict()}})
    devices: list[dict] = []
    errors: list[dict] = []
    for candidate in deduped.values():
        advertised_id = _device_id_from_ssdp(candidate)
        ok, fields, reason = await _fetch_descriptor(candidate, timeout_seconds=timeout)
        if not ok:
            errors.append({**candidate.to_dict(), "reason": reason, "details": fields})
            record_discovery_event(db, {**candidate.to_dict(), "method": "ssdp", "result": "descriptor_failed", "confidence": 10, "raw": {"reason": reason, "fields": fields}})
            continue
        device_id = _device_id_from_descriptor(fields, candidate)
        ip_address = _ip_from_location(candidate.location, candidate.remote_ip)
        if device_id != advertised_id:
            errors.append({**candidate.to_dict(), "reason": "descriptor identity does not match SSDP identity"})
            record_discovery_event(db, {**candidate.to_dict(), "method": "ssdp", "result": "identity_mismatch", "confidence": 0, "raw": {"advertised_device_id": advertised_id}})
            continue
        if is_explicit_discovery_target_protected(ip_address, device_id):
            record_discovery_event(db, {**candidate.to_dict(), "method": "ssdp", "result": "blocked", "confidence": 0, "raw": {"reason": "protected device access blocked after descriptor validation"}})
            continue
        discovered = DiscoveryDevice(
            device_id=device_id,
            ip_address=ip_address,
            name=str(fields.get("friendlyName") or ""),
            model=str(fields.get("modelName") or fields.get("modelDescription") or "SoundTouch"),
            location=candidate.location,
            method="ssdp",
            confidence=90 if device_id else 65,
            descriptor_validated=True,
            identity_verified=bool(device_id),
            interface=candidate.interface,
            raw={"ssdp": candidate.to_dict(), "descriptor": fields},
        )
        upsert_discovered_device(db, discovered)
        record_discovery_event(db, {**discovered.to_dict(), "result": "ok"})
        devices.append(discovered.to_dict())
    return {"enabled": True, "devices": devices, "candidates": [item.to_dict() for item in deduped.values()], "errors": errors}


async def manual_discovery_test(db: Session, *, interface: str = "", timeout_seconds: int | None = None) -> dict:
    return await discover_ssdp(db, interface=interface, timeout_seconds=timeout_seconds)
