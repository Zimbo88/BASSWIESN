"""Pure preset protocol projection and integrity contracts; no I/O or persistence.

Keep strict write/readback identity separate from the looser read-only station
comparison. Unknown XML fields survive edits; untouched slots remain in the diff.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from basswiesn.app.core.xml_safety import parse_xml
from basswiesn.app.services.orion import ORION_STATION_PATH, decode_orion_data
from basswiesn.app.services.provider_registry import normalize_source_name


def preset_summaries_from_xml(xml_text: str) -> list[dict[str, Any]]:
    try:
        root = parse_xml(xml_text)
    except ET.ParseError:
        return []
    rows = []
    for index, node in enumerate([item for item in root.iter() if _xml_local_name(item.tag) == "preset"], start=1):
        content = next((item for item in node.iter() if _xml_local_name(item.tag) == "ContentItem"), None)
        button = node.attrib.get("id") or node.attrib.get("button") or node.attrib.get("index") or str(index)
        rows.append({
            "button": int(button or 0),
            "source": content.attrib.get("source", "") if content is not None else "",
            "source_account": content.attrib.get("sourceAccount", "") if content is not None else "",
            "type": content.attrib.get("type", "") if content is not None else "",
            "location": content.attrib.get("location", "") if content is not None else "",
            "item_name": _child_text(content, "itemName") if content is not None else "",
            "container_art": _child_text(content, "containerArt") if content is not None else "",
            "container_art_present": bool(content is not None and any(_xml_local_name(child.tag) == "containerArt" for child in content)),
            "content_item_xml": ET.tostring(content, encoding="unicode") if content is not None else "",
        })
    return rows


def _decoded_orion_station(location: str) -> dict[str, Any]:
    try:
        parsed = urlparse(location or "")
    except ValueError:
        return {}
    if ORION_STATION_PATH not in parsed.path:
        return {}
    data = parse_qs(parsed.query).get("data", [""])[0]
    if not data:
        return {}
    try:
        decoded = decode_orion_data(data)
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _canonical_preset_source_account(source: str, value: str) -> str:
    """Return the firmware contract value, not a stale display-name import."""

    if normalize_source_name(source) == "LOCAL_INTERNET_RADIO":
        return ""
    return str(value or "")


def _preset_content_item_xml(
    item: dict[str, Any],
    *,
    location: str,
    source: str,
    source_account: str,
    name: str,
    art: str,
    include_art: bool = True,
    empty_art: bool = False,
    canonicalize_source_account: bool = True,
) -> str:
    try:
        root = parse_xml(item.get("content_item_xml") or "")
    except ET.ParseError:
        root = ET.Element("ContentItem")
    root.attrib["source"] = normalize_source_name(source)
    root.attrib["type"] = item.get("type") or root.attrib.get("type") or "stationurl"
    root.attrib["location"] = location
    root.attrib["sourceAccount"] = (
        _canonical_preset_source_account(source, source_account)
        if canonicalize_source_account
        else str(source_account or "")
    )
    root.attrib["isPresetable"] = "true"
    item_name = next((child for child in root if _xml_local_name(child.tag) == "itemName"), None)
    if item_name is None:
        item_name = ET.SubElement(root, "itemName")
    item_name.text = name or item.get("item_name") or "Unbenanntes Preset"
    container_art = next((child for child in root if _xml_local_name(child.tag) == "containerArt"), None)
    if include_art and art:
        if container_art is None:
            container_art = ET.SubElement(root, "containerArt")
        container_art.text = art
    elif empty_art:
        if container_art is None:
            container_art = ET.SubElement(root, "containerArt")
        container_art.text = ""
    elif container_art is not None:
        root.remove(container_art)
    return ET.tostring(root, encoding="unicode")


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(node: ET.Element | None, local_name: str) -> str:
    if node is None:
        return ""
    child = next((item for item in node.iter() if _xml_local_name(item.tag) == local_name), None)
    return (child.text or "").strip() if child is not None else ""


def _orion_data_key(location: str) -> str:
    try:
        parsed = urlparse(location or "")
    except ValueError:
        return ""
    data = parse_qs(parsed.query).get("data", [""])[0]
    return unquote(data or "")


def _locations_match(left: str, right: str) -> bool:
    left_clean = (left or "").strip()
    right_clean = (right or "").strip()
    if not left_clean or not right_clean:
        return False
    if left_clean == right_clean:
        return True
    left_data = _orion_data_key(left_clean)
    right_data = _orion_data_key(right_clean)
    return bool(left_data and right_data and left_data == right_data)


def _locations_select_same_station(left: str, right: str) -> bool:
    """Compare selection identity while ignoring server origin/extra hints.

    This deliberately does not replace the strict write/readback comparator.
    It is used only by the read-only Preset Checker so a valid preset hosted
    by another BASSWIESN endpoint is shown as a warning instead of falsely as
    an unplayable station.
    """

    left_item = _decoded_orion_station(left)
    right_item = _decoded_orion_station(right)
    if not left_item or not right_item:
        return False
    for key in ("streamUrl", "stream_url", "tuneinId", "providerStationId"):
        left_value = str(left_item.get(key) or "").strip()
        right_value = str(right_item.get(key) or "").strip()
        if left_value and right_value:
            return left_value == right_value
    return False


def _canonical_xml(xml_text: str) -> str:
    try:
        return ET.tostring(parse_xml(xml_text or ""), encoding="unicode")
    except ET.ParseError:
        return (xml_text or "").strip()


class PresetSyncResult(list[dict[str, Any]]):
    """List-compatible radio rows with a machine-readable integrity report."""

    def __init__(self, rows: list[dict[str, Any]], integrity: dict[str, Any]) -> None:
        super().__init__(rows)
        self.integrity = integrity


def classify_notification_sequence(sequence: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Classify the four notification-order variants without contacting a radio."""
    normalized = tuple(str(item or "").strip() for item in sequence)
    variants = {
        ("storePreset", "readback", "notification", "final_readback"): {
            "variant": "A",
            "safe_for_full_sync": False,
            "write_integrity_verified": False,
            "reason": "Eine clientseitig injizierte presetsUpdated-Notification kann einen gerade geschriebenen Slot aus dem noch alten Presetstand wieder leeren.",
        },
        ("notification", "storePreset", "readback"): {
            "variant": "B",
            "safe_for_full_sync": False,
            "write_integrity_verified": False,
            "reason": "Notification vor dem Store kann Firmware- oder Cache-Normalisierung ausloesen.",
        },
        ("storePreset", "readback"): {
            "variant": "C",
            "safe_for_full_sync": True,
            "write_integrity_verified": True,
            "reason": "Store und stabiler Readback sind der bestaetigte Clientvertrag; presetsUpdated wird vom Radio selbst als Ereignis erzeugt und nicht injiziert.",
        },
        ("notification",): {
            "variant": "D",
            "safe_for_full_sync": False,
            "write_integrity_verified": False,
            "reason": "Eine Notification ohne Store kann keinen Preset-Write bestaetigen.",
        },
    }
    result = variants.get(normalized)
    if result is None:
        return {
            "variant": "unbekannt",
            "safe_for_full_sync": False,
            "write_integrity_verified": False,
            "reason": "Unbekannte oder unvollstaendige Reihenfolge erfordert manuelle Pruefung.",
        }
    return {**result, "sequence": list(normalized)}


def _preset_projection(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "button": int(item.get("button") or 0),
        "source": item.get("source", ""),
        "source_account": item.get("source_account", ""),
        "type": item.get("type", ""),
        "location": item.get("location", ""),
        "item_name": item.get("item_name", ""),
        "container_art": item.get("container_art", ""),
        "container_art_present": bool(item.get("container_art_present", False)),
        "orion_payload": _orion_data_key(item.get("location", "")),
        "content_item_xml": _canonical_xml(item.get("content_item_xml", "")),
    }


def _preset_contract_matches(actual: dict[str, Any] | None, expected: dict[str, Any] | None) -> bool:
    if not actual or not expected:
        return False
    if not _locations_match(actual.get("location", ""), expected.get("location", "")):
        return False
    return all(
        actual.get(field, "") == expected.get(field, "")
        for field in (
            "source",
            "source_account",
            "type",
            "item_name",
            "container_art",
            "container_art_present",
        )
    )


def compare_preset_snapshots(
    before_xml: str,
    after_xml: str,
    expected: dict[int, str],
    *,
    expected_items: dict[int, dict[str, Any]] | None = None,
    allowed_orion_origins: set[str] | None = None,
) -> dict[str, Any]:
    """Compare target slots and every untouched slot without hiding changes."""
    before_rows = {int(item["button"]): _preset_projection(item) for item in preset_summaries_from_xml(before_xml)}
    after_rows = {int(item["button"]): _preset_projection(item) for item in preset_summaries_from_xml(after_xml)}
    target_buttons = {int(button) for button in expected}
    unexpected: list[dict[str, Any]] = []
    slot_results: list[dict[str, Any]] = []
    normalization_detected: list[dict[str, Any]] = []

    target_verified = True
    for button, location in expected.items():
        actual = after_rows.get(int(button))
        target_expected = (expected_items or {}).get(int(button), {"button": button, "location": location})
        target_ok = bool(actual and _locations_match(actual.get("location", ""), location))
        for field in ("source", "source_account", "type", "item_name", "container_art", "container_art_present"):
            expected_value = target_expected.get(field)
            if field in target_expected and expected_value is not None and actual and actual.get(field, "") != expected_value:
                target_ok = False
                unexpected.append({"button": int(button), "scope": "target", "field": field, "before": target_expected.get(field, ""), "after": actual.get(field, "")})
        target_verified = target_verified and target_ok
        slot_results.append({
            "button": int(button),
            "scope": "target",
            "status": "verified" if target_ok else "mismatch" if actual else "missing",
            "expected": {"location": location, "source": target_expected.get("source", ""), "source_account": target_expected.get("source_account", ""), "type": target_expected.get("type", ""), "item_name": target_expected.get("item_name", ""), "container_art": target_expected.get("container_art", ""), "container_art_present": target_expected.get("container_art_present", False)},
            "actual": actual or {},
        })

    untouched_buttons = set(before_rows) - target_buttons
    untouched_verified = True
    for button in sorted(untouched_buttons):
        before = before_rows[button]
        after = after_rows.get(button)
        unchanged = after == before
        if not unchanged and after:
            after_url = urlparse(after.get("location", ""))
            after_origin = f"{after_url.scheme}://{after_url.netloc}" if after_url.scheme and after_url.netloc else ""
            same_contract = (
                _locations_match(before.get("location", ""), after.get("location", ""))
                and all(
                    before.get(field, "") == after.get(field, "")
                    for field in ("source", "source_account", "type", "item_name", "container_art", "container_art_present", "orion_payload")
                )
            )
            if same_contract and after_origin in (allowed_orion_origins or set()):
                unchanged = True
                normalization_detected.append(
                    {
                        "button": button,
                        "kind": "approved_local_orion_origin_migration",
                        "before": before.get("location", ""),
                        "after": after.get("location", ""),
                    }
                )
        untouched_verified = untouched_verified and unchanged
        slot_results.append({"button": button, "scope": "untouched", "status": "normalized" if any(item["button"] == button for item in normalization_detected) else "unchanged" if unchanged else "changed" if after else "missing", "before": before, "actual": after or {}})
        if not unchanged:
            unexpected.append({"button": button, "scope": "untouched", "reason": "slot changed", "before": before, "after": after or {}})

    extra_buttons = set(after_rows) - set(before_rows) - target_buttons
    for button in sorted(extra_buttons):
        untouched_verified = False
        unexpected.append({"button": button, "scope": "untouched", "reason": "unexpected slot added", "after": after_rows[button]})

    if target_verified and untouched_verified and not unexpected:
        overall_status = "verified"
    elif unexpected:
        overall_status = "integrity_failure"
    else:
        overall_status = "partial_failure"
    return {
        "target_verified": target_verified,
        "untouched_slots_verified": untouched_verified,
        "normalization_detected": normalization_detected,
        "unexpected_changes": unexpected,
        "rollback_recommended": bool(unexpected),
        "manual_review_required": bool(unexpected) or not target_verified,
        "overall_status": overall_status,
        "slot_results": slot_results,
        "before_slot_count": len(before_rows),
        "after_slot_count": len(after_rows),
    }


def _integrity_for_sync_result(radio_rows: list[dict[str, Any]], expected: dict[int, str]) -> dict[str, Any]:
    """Keep old list-only test/service doubles honest without claiming proof."""
    report = getattr(radio_rows, "integrity", None)
    if isinstance(report, dict):
        return report
    target_verified = all(
        any(int(row.get("button") or 0) == int(button) and _locations_match(row.get("location", ""), location) for row in radio_rows)
        for button, location in expected.items()
    )
    return {
        "target_verified": target_verified,
        "untouched_slots_verified": False,
        "normalization_detected": [],
        "unexpected_changes": [],
        "rollback_recommended": False,
        "manual_review_required": True,
        "overall_status": "manual_review_required",
        "slot_results": [],
        "integrity_source": "legacy list-only adapter",
    }
