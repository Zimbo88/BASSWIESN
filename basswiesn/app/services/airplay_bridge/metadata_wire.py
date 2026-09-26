"""Bounded Shairport XML FIFO decoder. No logs, network or secret retention."""
from __future__ import annotations

import base64
import binascii
import math
import re
from xml.etree import ElementTree as ET


DISPLAY_CODES = {("core", code) for code in ("minm", "asar", "asal", "asgn")}
LIFECYCLE_CODES = {("ssnc", code) for code in ("mdst", "mden", "pbeg", "pend", "pfls", "prsm", "abeg", "aend")}
VOLUME_CODE = ('ssnc', 'pvol')
ALLOWED = DISPLAY_CODES | LIFECYCLE_CODES | {VOLUME_CODE}


class MetadataWireDecoder:
    """Consume item fragments; return only approved decoded text/event tuples.

    A too-large or malformed item is discarded. Unknown fields are never
    decoded. Raw buffers exist only while framing an item, never in diagnostics.
    """

    LIMIT = 16384

    def __init__(self):
        self._buffer = bytearray()
        self.discarded = 0

    def feed(self, chunk: bytes) -> list[tuple[str, str, bytes]]:
        if len(chunk) > self.LIMIT or len(self._buffer) + len(chunk) > self.LIMIT:
            self._buffer.clear()
            self.discarded += 1
            return []
        self._buffer.extend(chunk)
        events = []
        while b"</item>" in self._buffer:
            end = self._buffer.index(b"</item>") + len(b"</item>")
            raw = bytes(self._buffer[:end])
            del self._buffer[:end]
            start = raw.find(b"<item>")
            if start < 0 or b"<!" in raw:
                self.discarded += 1
                continue
            try:
                node = ET.fromstring(raw[start:])
                kind = bytes.fromhex(node.findtext("type", "")).decode("ascii")
                code = bytes.fromhex(node.findtext("code", "")).decode("ascii")
                if (kind, code) not in ALLOWED:
                    continue
                if (kind, code) in LIFECYCLE_CODES:
                    # Timestamps and arbitrary event values are unnecessary
                    # here. The clock-aware session adapter owns alignment.
                    events.append((kind, code, b""))
                    continue
                length = int(node.findtext("length", "-1"))
                if not 0 <= length <= 4096:
                    raise ValueError("metadata field too large")
                data_node = node.find("data")
                if data_node is None:
                    if length:
                        raise ValueError("missing data")
                    value = b""
                else:
                    if data_node.get("encoding") != "base64":
                        raise ValueError("unsupported encoding")
                    encoded = "".join((data_node.text or "").split())
                    value = base64.b64decode(encoded, validate=True)
                if len(value) != length:
                    raise ValueError("length mismatch")
                text = value.decode("utf8", errors="strict")
                if (kind, code) == VOLUME_CODE:
                    # Four numeric fields only; never return arbitrary event
                    # strings or backend-specific gain values. Source range
                    # and mute sentinel follow the upstream pvol contract.
                    fields = text.split(',')
                    if len(value) > 128 or len(fields) != 4 or any(
                            not re.fullmatch(r'\s*-?\d{1,3}(?:\.\d{1,8})?\s*', f) for f in fields):
                        raise ValueError('invalid volume event')
                    numbers = [float(f) for f in fields]
                    if any(not math.isfinite(n) or not -200 <= n <= 100 for n in numbers):
                        raise ValueError('invalid gain field')
                    source = numbers[0]
                    if not (-30 <= source <= 0 or source == -144):
                        raise ValueError('invalid source volume')
                    events.append((kind, code, format(source, '.6f').encode('ascii')))
                    continue
                clean = "".join(c for c in text if c.isprintable()).strip()[:256]
                events.append((kind, code, clean.encode("utf8")))
            except (ValueError, UnicodeError, ET.ParseError, binascii.Error):
                self.discarded += 1
        return events
