"""Deterministic radio defaults for an explicit setup, not startup writes."""
from __future__ import annotations

import os
from sqlalchemy.orm import Session

from basswiesn.app.models import Setting
from basswiesn.app.services.settings_contract import LANGUAGE_IDS


def radio_language(locale: str) -> str:
    """Use only established firmware IDs; unsupported UI languages use English."""
    code = locale.strip().lower().split(".", 1)[0].split("@", 1)[0].replace("_", "-")
    parts = code.split("-")
    if parts[0] == "zh":
        return "zh_hant" if "hant" in parts or ("hans" not in parts and any(p in {"tw", "hk", "mo"} for p in parts)) else "zh_hans"
    code = "no" if parts[0] == "nb" else parts[0]
    return code if code in LANGUAGE_IDS else "en"


def setup_defaults(db: Session) -> dict[str, str | bool]:
    row = db.query(Setting).filter(Setting.key == "web_language").one_or_none()
    locale = row.value if row and row.value else (
        os.getenv("LC_ALL") or os.getenv("LC_MESSAGES") or os.getenv("LANG") or "en"
    )
    return {"language": radio_language(locale), "clockDisplay": True,
            "display_mode": "TRACK_ARTIST", "provider_clock": False,
            "preserve_name_presets_battery": True,
            "preserve_timezone_brightness_volume": True}
