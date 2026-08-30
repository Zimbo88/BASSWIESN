from html import escape

from basswiesn.app.models import Preset, Station
from basswiesn.app.services.provider_registry import persistence_sources_xml


def content_item_xml(
    station: Station,
    location: str,
    include_container_art: bool = True,
    source: str = "LOCAL_INTERNET_RADIO",
    *,
    empty_container_art: bool = False,
) -> str:
    if (location or "").startswith("/core02/svc-bmx-adapter-orion/"):
        raise ValueError("BASSWIESN Host IP setzen: Orion ContentItem location darf nicht relativ sein.")
    art = (
        "<containerArt></containerArt>"
        if empty_container_art
        else f"<containerArt>{escape(station.image_url)}</containerArt>"
        if include_container_art and station.image_url
        else ""
    )
    source = source or "LOCAL_INTERNET_RADIO"
    return (
        f'<ContentItem source="{escape(source, quote=True)}" type="stationurl" '
        f'location="{escape(location, quote=True)}" sourceAccount="" '
        f'isPresetable="true">'
        f"<itemName>{escape(station.name)}</itemName>"
        f"{art}"
        f"</ContentItem>"
    )


def _strip_container_art(content_xml: str) -> str:
    marker = "<containerArt"
    if marker not in (content_xml or ""):
        return content_xml
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(content_xml)
        for child in list(root):
            if child.tag.rsplit("}", 1)[-1] == "containerArt":
                root.remove(child)
        return ET.tostring(root, encoding="unicode")
    except ET.ParseError:
        return content_xml


def presets_xml(presets: list[Preset], *, include_container_art: bool = True) -> str:
    rows = []
    for preset in presets:
        content_xml = preset.content_item_xml
        if not include_container_art:
            content_xml = _strip_container_art(content_xml)
        rows.append(
            f'<preset id="{preset.button}" createdOn="" updatedOn="">'
            f"{content_xml}"
            f"</preset>"
        )
    return '<?xml version="1.0" encoding="UTF-8"?><presets>' + "".join(rows) + "</presets>"


def sources_xml() -> str:
    return persistence_sources_xml()
