"""Explicit UPnP ContentDirectory client; no multicast, renderer or radio writes.

Only literal LAN IPv4 HTTP endpoints are accepted. Description, SOAP and media
must share one approved origin. No DNS, redirects, proxy environment or cookies.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import ipaddress
import re
from urllib.parse import urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as XML

from defusedxml import ElementTree as SafeXML
import httpx

from basswiesn.app.services.protected_devices import is_device_access_protected, protected_device_ids

DEVICE = "urn:schemas-upnp-org:device-1-0"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
DIDL = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
DC = "http://purl.org/dc/elements/1.1/"
UPNP = "urn:schemas-upnp-org:metadata-1-0/upnp/"
MAX_XML = 2 * 1024 * 1024
MAX_MEDIA = 512 * 1024 * 1024
LAN = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
FORMATS = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/aac": "aac", "audio/aacp": "aac"}


class DlnaError(ValueError):
    """Fixed codes only: never upstream XML, arbitrary URLs or exception text."""
    pass


def endpoint(value: str, *, origin: str | None = None) -> str:
    try:
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value) or "\\" in value:
            raise ValueError()
        u = urlsplit(value)
        host = str(ipaddress.IPv4Address(u.hostname or ""))
        port = u.port if u.port is not None else 80
        if (u.scheme != "http" or u.username is not None or u.password is not None
                or u.fragment or not any(ipaddress.ip_address(host) in n for n in LAN)
                or port != 80 and not 1024 <= port <= 65535):
            raise ValueError()
        normalized = urlunsplit(("http", host + (":" + str(port) if port != 80 else ""), u.path or "/", u.query, ""))
        if origin and urlsplit(normalized).netloc != urlsplit(endpoint(origin)).netloc:
            raise DlnaError("CROSS_ORIGIN")
        if is_device_access_protected(host):
            raise DlnaError("PROTECTED_TARGET")
        return normalized
    except DlnaError:
        raise
    except (ValueError, TypeError):
        raise DlnaError("INVALID_ENDPOINT") from None


def check_not_radio(url: str) -> None:
    """A registered radio is not an approved NAS, even if its IP has changed."""
    from basswiesn.app.db import SessionLocal
    from basswiesn.app.models import Device
    endpoint(url)
    try:
        with SessionLocal() as db:
            row = db.query(Device).filter(Device.ip_address == urlsplit(url).hostname).first()
            if row is not None:
                raise DlnaError("RADIO_TARGET")
    except DlnaError:
        raise
    except Exception:
        raise DlnaError("POLICY_UNAVAILABLE") from None


def xml(data: bytes | str):
    if len(data) > MAX_XML:
        raise DlnaError("RESPONSE_TOO_LARGE")
    try:
        root = SafeXML.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        nodes = list(root.iter())
        if len(nodes) > 10000:
            raise ValueError()
        return root
    except Exception:
        raise DlnaError("MALFORMED_XML") from None


def text(node, tag: str, *, required=False, maximum=1024) -> str:
    values = node.findall(tag)
    if len(values) > 1 or required and len(values) != 1:
        raise DlnaError("INVALID_RESPONSE")
    value = (values[0].text or "").strip() if values else ""
    if len(value) > maximum or required and not value:
        raise DlnaError("INVALID_RESPONSE")
    return value


@dataclass(frozen=True)
class Server:
    description_url: str
    udn: str
    name: str
    service_type: str
    control_url: str

    def to_dict(self):
        return asdict(self)


def description(data: bytes, url: str) -> Server:
    url = endpoint(url)
    root = xml(data)
    if root.tag != f"{{{DEVICE}}}root":
        raise DlnaError("INVALID_DESCRIPTION")
    base = text(root, f"{{{DEVICE}}}URLBase") or url
    base = endpoint(urljoin(url, base), origin=url)
    servers = []
    for device in root.iter(f"{{{DEVICE}}}device"):
        kind = text(device, f"{{{DEVICE}}}deviceType")
        if not re.fullmatch(r"urn:schemas-upnp-org:device:MediaServer:[1-4]", kind):
            continue
        udn = text(device, f"{{{DEVICE}}}UDN", required=True, maximum=160)
        if not re.fullmatch(r"uuid:[A-Za-z0-9-]{1,150}", udn):
            raise DlnaError("INVALID_DESCRIPTION")
        compact = re.sub(r"[^A-Z0-9]", "", udn.upper())
        if any(identity and identity in compact for identity in protected_device_ids()):
            raise DlnaError("PROTECTED_TARGET")
        for service in device.findall(f"{{{DEVICE}}}serviceList/{{{DEVICE}}}service"):
            service_type = text(service, f"{{{DEVICE}}}serviceType")
            if re.fullmatch(r"urn:schemas-upnp-org:service:ContentDirectory:[1-4]", service_type):
                control = text(service, f"{{{DEVICE}}}controlURL", required=True)
                servers.append(Server(url, udn, text(device, f"{{{DEVICE}}}friendlyName") or "MediaServer",
                                      service_type, endpoint(urljoin(base, control), origin=url)))
    if len(servers) != 1:
        raise DlnaError("CONTENT_DIRECTORY_NOT_UNIQUE")
    return servers[0]


def browse_request(server: Server, object_id: str, start: int, count: int, metadata=False) -> bytes:
    if (not isinstance(object_id, str) or not object_id or len(object_id) > 1024
            or any(ord(c) < 32 for c in object_id) or type(start) is not int or not 0 <= start <= 1000000
            or type(count) is not int or not 1 <= count <= 100):
        raise DlnaError("INVALID_BROWSE")
    root = XML.Element(f"{{{SOAP}}}Envelope", {f"{{{SOAP}}}encodingStyle": "http://schemas.xmlsoap.org/soap/encoding/"})
    body = XML.SubElement(root, f"{{{SOAP}}}Body")
    action = XML.SubElement(body, f"{{{server.service_type}}}Browse")
    for key, value in {"ObjectID": object_id, "BrowseFlag": "BrowseMetadata" if metadata else "BrowseDirectChildren",
                       "Filter": "*", "StartingIndex": str(start), "RequestedCount": str(count), "SortCriteria": ""}.items():
        XML.SubElement(action, key).text = value
    return XML.tostring(root, encoding="utf-8", xml_declaration=True)


def browse_response(data: bytes, server: Server, start: int, count: int) -> dict:
    root = xml(data)
    response = root.findall(f"{{{SOAP}}}Body/{{{server.service_type}}}BrowseResponse")
    if root.tag != f"{{{SOAP}}}Envelope" or len(response) != 1:
        raise DlnaError("SOAP_FAULT" if root.find(f"{{{SOAP}}}Body/{{{SOAP}}}Fault") is not None else "INVALID_RESPONSE")
    response = response[0]
    def number(key):
        value = text(response, key, required=True, maximum=10)
        if not re.fullmatch(r"[0-9]{1,10}", value) or int(value) > 2**32 - 1:
            raise DlnaError("INVALID_RESPONSE")
        return int(value)
    returned, total, update = number("NumberReturned"), number("TotalMatches"), number("UpdateID")
    # ContentDirectory permits TotalMatches=0 with non-empty results when the
    # server cannot compute the total. It does NOT mean an empty catalogue.
    # Keep the actual returned-count and explicit positive-total checks strict.
    total_unknown = total == 0 and returned > 0
    # Empty Result is legal when no objects match.
    raw = text(response, "Result", maximum=MAX_XML)
    didl = xml(raw) if raw else XML.Element(f"{{{DIDL}}}DIDL-Lite")
    if (didl.tag != f"{{{DIDL}}}DIDL-Lite" or len(didl) != returned or returned > count
            or returned and not total_unknown and total < start + returned
            or not returned and start < total):
        raise DlnaError("INVALID_PAGINATION")
    items, ids = [], set()
    for node in didl:
        if node.tag not in {f"{{{DIDL}}}container", f"{{{DIDL}}}item"}:
            raise DlnaError("INVALID_RESPONSE")
        identity = node.get("id", "")
        if not identity or len(identity) > 1024 or identity in ids:
            raise DlnaError("INVALID_RESPONSE")
        ids.add(identity)
        kind = "container" if node.tag.endswith("}container") else "item"
        resources = []
        for resource in node.findall(f"{{{DIDL}}}res")[:16]:
            fields = resource.get("protocolInfo", "").split(":", 3)
            if len(fields) != 4 or fields[0] != "http-get":
                continue
            try:
                address = endpoint(urljoin(server.description_url, (resource.text or "").strip()), origin=server.description_url)
            except DlnaError:
                continue  # never fetch off-origin/protected advertised resources
            mime = fields[2].lower().split(";", 1)[0]
            resources.append({"url": address, "mime": mime, "format": FORMATS.get(mime, ""),
                              "duration": resource.get("duration", "")[:32]})
        items.append({"id": identity, "parent_id": node.get("parentID", "")[:1024], "kind": kind,
                      "title": text(node, f"{{{DC}}}title") or identity,
                      "artist": text(node, f"{{{UPNP}}}artist"), "album": text(node, f"{{{UPNP}}}album"),
                      "class": text(node, f"{{{UPNP}}}class"), "resources": resources,
                      "importable": kind == "item" and any(r["format"] for r in resources)})
    # A short page need not exhaust an unknown total. Offer one user-requested
    # continuation, never an automatic unbounded scan. An empty page ends it.
    more = returned > 0 and (total_unknown or start + returned < total)
    limited = more and start + returned > 1000000
    return {"items": items, "start": start, "returned": returned,
            "total": None if total_unknown else total, "update_id": update,
            "pagination_limited": limited,
            "next_start": start + returned if more and not limited else None}


class ContentDirectory:
    def __init__(self, *, transport=None):
        self.transport = transport  # injectable offline HTTP fixture, not a Web option

    def http(self):
        return httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False,
                                 timeout=httpx.Timeout(8), limits=httpx.Limits(max_connections=2))

    async def exchange(self, url, *, body=None, service_type=""):
        endpoint(url)
        check_not_radio(url)
        headers = {"Accept-Encoding": "identity", "Accept": "text/xml"}
        if body is not None:
            headers.update({"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{service_type}#Browse"'})
        try:
            async with asyncio.timeout(10), self.http() as client:
                async with client.stream("POST" if body is not None else "GET", url, content=body, headers=headers) as response:
                    if response.status_code != 200:
                        raise DlnaError("SOAP_FAULT" if response.status_code == 500 else "HTTP_ERROR")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise DlnaError("ENCODING_REJECTED")
                    data = bytearray()
                    async for chunk in response.aiter_bytes(8192):
                        data.extend(chunk)
                        if len(data) > MAX_XML:
                            raise DlnaError("RESPONSE_TOO_LARGE")
                    return bytes(data)
        except (httpx.HTTPError, TimeoutError):
            raise DlnaError("TRANSPORT_ERROR") from None

    async def connect(self, url: str, *, expected_udn="") -> Server:
        url = endpoint(url)
        server = description(await self.exchange(url), url)
        if expected_udn and server.udn != expected_udn:
            raise DlnaError("SERVER_IDENTITY_CHANGED")
        return server

    async def browse(self, server: Server, object_id="0", *, start=0, count=50, metadata=False):
        body = browse_request(server, object_id, start, count, metadata)
        raw = await self.exchange(server.control_url, body=body, service_type=server.service_type)
        result = browse_response(raw, server, start, count)
        if metadata and (len(result["items"]) != 1 or result["total"] != 1
                         or result["items"][0]["id"] != object_id):
            raise DlnaError("ITEM_CHANGED")
        return result


def playable_resource(item):
    # MP3 preferred; codec compatibility is still model/readback dependent.
    for codec in ("mp3", "aac"):
        for resource in item["resources"]:
            if resource["format"] == codec:
                return resource
    raise DlnaError("UNSUPPORTED_FORMAT")
