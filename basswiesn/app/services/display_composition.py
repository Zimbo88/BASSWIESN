"""Pure display text contract, shared by runtime and the offline emulator.

This describes provider text, not physical OLED geometry. No database, settings,
transport or application startup may be imported here.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

DISPLAY_FIELDS = ("station", "artist", "title", "clock", "other")


def _text(value: object, limit: int = 256) -> str:
    return "".join(c for c in str(value or "") if c.isprintable()).strip()[:limit]


@dataclass(frozen=True)
class DisplayPreference:
    mode: str = "STATION"
    show_other_info: bool = False
    fields: tuple[str, ...] | None = None
    field_order: tuple[str, ...] = DISPLAY_FIELDS

    @property
    def needs_metadata(self) -> bool:
        if self.mode == "CUSTOM":
            return bool(set(self.fields or ()) & {"artist", "title", "other"})
        return self.mode == "TRACK_ARTIST" or self.show_other_info

    def as_public_dict(self, *, legacy_clock_enabled: bool) -> dict[str, object]:
        if self.mode == "CUSTOM":
            selected = set(self.fields or ())
        else:
            selected = {"artist", "title"} if self.mode == "TRACK_ARTIST" else {"station"}
            if self.show_other_info:
                selected.add("other")
            if legacy_clock_enabled:
                selected.add("clock")
        return {"mode": self.mode, "show_other_info": "other" in selected,
                "fields": [field for field in self.field_order if field in selected],
                "field_order": list(self.field_order)}


def validate_display_preference(preference: DisplayPreference) -> None:
    if preference.mode not in {"STATION", "TRACK_ARTIST", "CUSTOM"} or not isinstance(preference.show_other_info, bool):
        raise ValueError("invalid display preference")
    if preference.mode == "CUSTOM":
        selected = preference.fields
        order = preference.field_order
        if (selected is None or any(not isinstance(field, str) for field in (*selected, *order))
                or len(set(selected)) != len(selected) or not set(selected) <= set(DISPLAY_FIELDS)
                or len(order) != len(DISPLAY_FIELDS) or set(order) != set(DISPLAY_FIELDS)):
            raise ValueError("invalid display fields or order")


def display_fields(preference: DisplayPreference, station_name: str, metadata: dict[str, object], *,
                   clock_text: str = "", allow_partial_track: bool = False) -> dict[str, str]:
    track, artist = station_name, ""
    if preference.mode == "TRACK_ARTIST" and metadata.get("track") and (metadata.get("artist") or allow_partial_track):
        track, artist = str(metadata["track"]), str(metadata.get("artist") or "")
    # Album is not a programme field. Preserve the existing provider contract.
    def normalize(text: str) -> str:
        return re.sub(r"[^\w]", "", text.casefold())
    station = normalize(station_name)
    other = ""
    for field in ("other_info", "description", "genre"):
        candidate = _text(metadata.get(field))
        key = normalize(candidate)
        if key and key not in {"unspecifieddescription", "unspecified", "unknown", "none", "various"} and key != station:
            if field == "description" and key in station:
                continue
            other = candidate
            break
    if preference.mode == "CUSTOM":
        values = {"station": _text(station_name), "artist": _text(metadata.get("artist")),
                  "title": _text(metadata.get("track")), "clock": _text(clock_text, 5), "other": other}
        selected = set(preference.fields or ())
        parts = [(field, values[field]) for field in preference.field_order if field in selected and values[field]]
        separators = sum(1 if field == "clock" else 3 for field, _ in parts[1:])
        text_count = sum(field != "clock" for field, _ in parts)
        clock_size = sum(len(text) for field, text in parts if field == "clock")
        text_budget = max(1, (256 - separators - clock_size) // max(1, text_count))
        rendered = ""
        for field, text in parts:
            if field != "clock" and len(text) > text_budget:
                text = text[:text_budget - 1].rstrip() + "…"
            separator = (" " if field == "clock" else " — ") if rendered else ""
            rendered += separator + text
        # Combined title is intentional: no native page switching required.
        # 256 is our conservative text budget, not an established OLED limit.
        return {"track": rendered, "artist": ""}
    if preference.show_other_info and other:
        track = f"{track} — {other}"
    return {"track": _text(track), "artist": _text(artist)}
