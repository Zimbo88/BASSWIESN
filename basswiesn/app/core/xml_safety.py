"""Protocol XML without document types or user-defined entities.

Keep ElementTree's ParseError contract so existing callers retain their
fail-closed error handling. XML declarations, namespaces and predefined
character references remain supported; SoundTouch contracts need no DTD.
"""
from __future__ import annotations

from typing import cast
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree as SafeXML  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]


def parse_xml(value: str | bytes) -> Element:
    try:
        return cast(Element, SafeXML.fromstring(
            value, forbid_dtd=True, forbid_entities=True, forbid_external=True,
        ))
    except DefusedXmlException:
        # Do not echo declarations, filenames or attacker-controlled entities.
        raise ParseError("Unsafe XML declarations are not allowed") from None
