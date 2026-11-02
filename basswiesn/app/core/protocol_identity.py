"""Identity fields actually consumed by the Marge compatibility handlers.

These are inventory bindings, not radio authentication. An incoming callback
must not create the authority that is subsequently used to authorize itself.
"""
from __future__ import annotations

from typing import cast
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree as SafeXML  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]


def account_from_info(xml: str) -> str:
    try:
        root = cast(Element, SafeXML.fromstring(xml, forbid_dtd=True))
    except (ParseError, DefusedXmlException):
        return ""
    accounts = root.findall("margeAccountUUID")
    if root.tag != "info" or len(accounts) != 1:
        return ""
    value = (accounts[0].text or "").strip()
    return value if len(value) <= 128 else ""


def registration_device_id(body: bytes) -> str:
    """Read the exact root attribute consumed by streaming_add_device.

    No guessing at nested IDs: a group member is not necessarily the sender.
    Unknown schemas must be reviewed instead of recursively trusting ID-like
    strings. DTDs/entities are forbidden, including UTF-16 encoded documents.
    """
    try:
        root = cast(Element, SafeXML.fromstring(body, forbid_dtd=True))
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise ValueError("invalid registration XML") from exc
    identifier = root.attrib.get("deviceid", "").strip()
    if root.tag != "device" or not identifier or len(identifier) > 128:
        raise ValueError("invalid registration identity")
    return str(identifier)


def cached_account_binding(peer: str, account_id: str, device_id: str | None) -> bool:
    # Import here to keep the field parsers pure and independently testable.
    from basswiesn.app import db as app_db
    from basswiesn.app.core.device_ingress import canonical_peer
    from basswiesn.app.models import Device

    with app_db.SessionLocal() as db:
        devices = [device for device in db.query(Device).all() if canonical_peer(device.ip_address) == peer]
        # An IP reused by multiple cached identities is not a usable binding.
        return (len(devices) == 1 and bool(account_id)
                and (device_id is None or devices[0].device_id.upper() == device_id.upper())
                and account_from_info(devices[0].info_xml or "") == account_id)
