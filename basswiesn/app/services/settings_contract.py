"""Fixed WebAPI setting shapes. No transport, persistence or firmware edits.

Missing actual state is not zero/false, and a target value is not readback.
The router keeps compatibility wrappers; new callers use these domain contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from xml.etree import ElementTree as ET

from basswiesn.app.core.xml_safety import parse_xml
from basswiesn.app.services.catalogs import TIME_ZONES

LANGUAGE_IDS = {
    "da": 1, "de": 2, "en": 3, "es": 4, "fr": 5, "it": 6,
    "nl": 7, "sv": 8, "ja": 9, "zh_hans": 10, "zh_hant": 11,
    "ko": 12, "th": 13, "cs": 15, "fi": 16, "el": 17,
    "no": 18, "nb": 18, "pl": 19, "pt": 20, "ro": 21,
    "ru": 22, "sl": 23, "tr": 24, "hu": 25,
}
LANGUAGE_CODES_BY_ID = {value: key for key, value in LANGUAGE_IDS.items() if key != "nb"}
SettingValue = str | int | bool | dict[str, str]


@dataclass(frozen=True)
class SettingPlan:
    setting: str
    path: str
    read_path: str
    xml: str
    expected: SettingValue

    def matches(self, actual: SettingValue) -> bool:
        if isinstance(self.expected, dict):
            return isinstance(actual, dict) and all(actual.get(k) == v for k, v in self.expected.items())
        return type(actual) is type(self.expected) and actual == self.expected

    def matches_previous(self, actual: SettingValue, previous: object) -> bool:
        """Check every overwritten clock attribute without binding advancing UTC.

        A caller may send a subset snapshot, but cannot omit an attribute it
        intends to overwrite. A full older snapshot still compares every field.
        Attribute values retain their XML string types; false is not zero.
        """
        if self.setting == "clockConfig":
            return (
                isinstance(self.expected, dict) and isinstance(actual, dict)
                and isinstance(previous, dict) and bool(previous)
                and set(self.expected).issubset(previous)
                and all(isinstance(value, str) and actual.get(key) == value for key, value in previous.items())
            )
        return type(actual) is type(previous) and actual == previous


def device_name(value: object) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("device name is required")
    if not re.fullmatch(r'[^<>"&]{1,63}', name) or any(
        not (0x20 <= ord(ch) <= 0xD7FF or 0xE000 <= ord(ch) <= 0xFFFD or 0x10000 <= ord(ch) <= 0x10FFFF)
        for ch in name
    ):
        raise ValueError("device name contains invalid characters")
    return name


def _integer(value: object) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"-?[0-9]+", str(value)):
        raise ValueError("setting requires an integer")
    return int(str(value))


def _boolean(value: object) -> bool:
    text = str(value).lower()
    if text not in {"true", "on", "1", "yes", "false", "off", "0", "no"}:
        raise ValueError("setting requires true or false")
    return text in {"true", "on", "1", "yes"}


def plan_setting(setting: str, value: object) -> SettingPlan:
    path = f"/{setting}"
    expected: SettingValue
    if setting == "name":
        expected = device_name(value)
        return SettingPlan(setting, "/name", "/info", f"<name>{expected}</name>", expected)
    if setting in {"volume", "bass", "systemtimeout", "auto_standby_minutes"}:
        expected = _integer(value)
        minimum, maximum = {"volume": (0, 100), "bass": (-9, 0)}.get(setting, (0, 1440))
        if not minimum <= expected <= maximum:
            raise ValueError(f"{setting} must be {minimum}..{maximum}")
        tag = "systemtimeout" if setting == "auto_standby_minutes" else setting
        path = f"/{tag}"
        xml = f"<{tag}>{expected}</{tag}>"
    elif setting in {"clockDisplay", "powersaving"}:
        expected = _boolean(value)
        text = str(expected).lower()
        if setting == "clockDisplay":
            xml = f'<clockDisplay><clockConfig userEnable="{text}" /></clockDisplay>'
        else:
            path = "/systemtimeout"
            xml = f"<systemtimeout><powersaving_enabled>{text}</powersaving_enabled></systemtimeout>"
    elif setting == "clockConfig":
        if not isinstance(value, dict) or not value:
            raise ValueError("clockConfig requires an object with explicit fields")
        allowed = {"timezoneInfo", "timeFormat", "userOffsetMinute", "brightnessLevel", "userUtcTime", "userEnable"}
        if set(value) - allowed:
            raise ValueError("unsupported clockConfig field")
        cfg: dict[str, str] = {}
        for key, item in value.items():
            if key == "timezoneInfo":
                if str(item) not in TIME_ZONES:
                    raise ValueError("unsupported timezone")
                cfg[key] = str(item)
            elif key == "timeFormat":
                if str(item) not in {"TIME_FORMAT_24HOUR_ID", "TIME_FORMAT_12HOUR_ID"}:
                    raise ValueError("unsupported time format")
                cfg[key] = str(item)
            elif key == "userEnable":
                cfg[key] = str(_boolean(item)).lower()
            else:
                cfg[key] = str(_integer(item))
        # Partial clock updates must not reset unspecified fields or turn a
        # deliberate brightness of zero into the old default of seven.
        root = ET.Element("clockDisplay")
        ET.SubElement(root, "clockConfig", cfg)
        path = "/clockDisplay"
        xml = ET.tostring(root, encoding="unicode")
        expected = cfg
    elif setting == "language":
        code = str(value)
        if code not in LANGUAGE_IDS:
            raise ValueError("unsupported Stockholm language")
        number = LANGUAGE_IDS[code]
        expected = LANGUAGE_CODES_BY_ID[number]
        xml = f"<sysLanguage>{number}</sysLanguage>"
    elif setting == "rebroadcastlatencymode":
        if value not in ("SYNC_TO_ZONE", "SYNC_TO_ROOM"):
            raise ValueError("unsupported rebroadcast latency mode")
        expected = str(value)
        xml = f'<rebroadcastlatencymode mode="{expected}" />'
    else:
        raise ValueError(f"unsupported setting: {setting}")
    return SettingPlan(setting, path, path, xml, expected)


def read_setting(setting: str, xml_text: str) -> SettingValue:
    root = parse_xml(xml_text)
    tag = {"name": "info", "language": "sysLanguage", "clockConfig": "clockDisplay",
           "powersaving": "systemtimeout", "auto_standby_minutes": "systemtimeout"}.get(setting, setting)
    if root.tag != tag:
        raise ValueError("unexpected setting response root")
    if setting in {"volume", "bass"}:
        return _integer(root.findtext(f"actual{setting}"))
    if setting == "name":
        text = root.findtext("name")
        if text is None:
            raise ValueError("name missing from readback")
        return text
    if setting in {"clockDisplay", "clockConfig"}:
        cfg = root.find("clockConfig")
        if cfg is None:
            raise ValueError("clockConfig missing from readback")
        return _boolean(cfg.get("userEnable")) if setting == "clockDisplay" else dict(cfg.attrib)
    if setting == "language":
        number = _integer((root.text or "").strip())
        return LANGUAGE_CODES_BY_ID.get(number, f"id:{number}")
    if setting == "powersaving":
        return _boolean(root.findtext("powersaving_enabled"))
    if setting == "rebroadcastlatencymode":
        mode = root.get("mode")
        if mode not in {"SYNC_TO_ZONE", "SYNC_TO_ROOM"}:
            raise ValueError("latency mode missing or unknown")
        return mode
    if setting in {"systemtimeout", "auto_standby_minutes"}:
        # Structured timeout responses vary; do not invent a numeric default.
        return _integer((root.text or "").strip())
    raise ValueError(f"unsupported setting: {setting}")
