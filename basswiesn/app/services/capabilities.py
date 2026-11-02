"""Conservative capability parsing across SoundTouch XML variants."""

import re
import xml.etree.ElementTree as ET


CAPABILITY_MARKERS = {
    "battery": (),
    "display": ("display", "clockdisplay", "clocktime"),
    "lightswitch": ("lightswitch",),
    "bluetooth": ("bluetooth",),
    "zone": ("getzone", "setzone", "zone"),
    "dsp": ("basscapabilities", "bass", "speaker", "dsp"),
    "hdmi": ("hdmi",),
    "clockDisplay": ("clockdisplay", "clocktime"),
    "rebroadcastlatencymode": ("rebroadcastlatencymode",),
}


def capability_flags(xml_text: str) -> tuple[dict[str, bool | None], bool]:
    raw = (xml_text or "").strip()
    if not raw:
        return {feature: None for feature in CAPABILITY_MARKERS}, False
    try:
        root = ET.fromstring(raw)
        parts = [node.tag for node in root.iter()]
        parts.extend(value for node in root.iter() for value in node.attrib.values())
        parts.extend((node.text or "") for node in root.iter())
        evidence = re.sub(r"[^a-z0-9]", "", " ".join(parts).lower())
    except ET.ParseError:
        # Firmware/debug captures can be truncated. Recover explicit feature or
        # endpoint names, but never infer a feature from a model name.
        evidence = re.sub(r"[^a-z0-9]", "", raw.lower())
    flags = {feature: any(marker.lower() in evidence for marker in markers) for feature, markers in CAPABILITY_MARKERS.items()}
    return flags, True
