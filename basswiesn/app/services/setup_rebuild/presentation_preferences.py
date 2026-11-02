"""Native clock and provider text settings for the three-region layout.

Set the system language and enable the standby clock. Timezone, brightness
and other observed clock attributes are preserved and verified on readback.
"""
from __future__ import annotations

from dataclasses import asdict
import json
from xml.etree import ElementTree as ET
from typing import Any

from defusedxml import ElementTree as SafeET  # type: ignore[import-untyped]
from basswiesn.app import db as app_db
from basswiesn.app.models import Setting, SetupRebuildJob
from basswiesn.app.services.display_composition import DisplayPreference
from basswiesn.app.services.settings_contract import LANGUAGE_IDS
from .defaults import setup_defaults
from .backup import write_json_artifact
from .presentation import SetupPresentation, read_private
from .presentation_model import PresentationError


def clock(raw: str) -> dict[str, str]:
    root = SafeET.fromstring(raw)
    nodes = root.findall("clockConfig")
    if root.tag != "clockDisplay" or len(nodes) != 1 or nodes[0].get("userEnable") not in {"true", "false"}:
        raise PresentationError("PRESENTATION_CLOCK_SCHEMA_UNKNOWN")
    return dict(nodes[0].attrib)


def language(raw: str) -> int:
    root = SafeET.fromstring(raw)
    value = (root.text or "").strip()
    # Zero is a real, uninitialized factory state and must be backed up, not
    # mistaken for English. Never guess an unknown firmware enum.
    if root.tag != "sysLanguage" or len(root) or not value.isdecimal() or int(value) not in range(26):
        raise PresentationError("PRESENTATION_LANGUAGE_SCHEMA_UNKNOWN")
    return int(value)


async def configure(service: SetupPresentation, row: Any, *, restore: bool = False, patched: bool = True) -> None:
    root = service.root(row)
    path = root / "preferences.json"
    if restore and not path.exists():
        return
    identity = await service.adapter.identify(row)
    client = service.adapter._client(row)
    # ST10 has no display. Do not send it clock configuration or infer display
    # capability from sharing a firmware family with display-equipped radios.
    has_clock = identity.get("model") != "SoundTouch 10"
    current_clock = clock(await client.get_xml("/clockDisplay")) if has_clock else None
    keys = ("radio_display:" + row.device_id.upper(), "research.clock_metadata." + row.device_id.upper())
    db = app_db.SessionLocal()
    try:
        def values() -> dict[str, str | None]:
            return {key: (item.value if (item := db.query(Setting).filter(Setting.key == key).one_or_none()) else None) for key in keys}
        previous = values()
        current_language = language(await client.get_xml("/language"))
        if restore:
            saved = json.loads(read_private(path, 65536))
            if saved["identity"] != row.device_id.upper():
                raise PresentationError("PRESENTATION_PREFERENCE_IDENTITY_MISMATCH")
            if previous != saved["before"] and previous != saved["desired"]:
                raise PresentationError("PRESENTATION_PREFERENCE_DIVERGED")
            if current_clock != saved["clock_before"] and current_clock != saved["clock_desired"]:
                raise PresentationError("PRESENTATION_CLOCK_DIVERGED")
            desired, desired_clock = saved["before"], saved["clock_before"]
            # Old backups did not change the language. Leave it alone.
            desired_language = saved.get("language_before", current_language)
            if current_language not in (desired_language, saved.get("language_desired", current_language)):
                raise PresentationError("PRESENTATION_LANGUAGE_DIVERGED")
        else:
            if path.exists():
                raise PresentationError("PRESENTATION_PREFERENCE_REPLAY_REFUSED")
            provider_clock = json.loads(previous[keys[1]] or "{}")
            provider_clock.update(enabled=False, mode="OFF")
            desired = {keys[0]: json.dumps(asdict(DisplayPreference("TRACK_ARTIST"))), keys[1]: json.dumps(provider_clock, sort_keys=True)} if patched else previous.copy()
            desired_clock = {**current_clock, "userEnable": "true"} if current_clock is not None else None
            defaults = setup_defaults(db)
            # A multi-radio run uses one published plan, even if another admin
            # changes the UI language while a preceding radio is rebooting.
            job_id = getattr(row, "job_id", None)
            if job_id:
                job = db.query(SetupRebuildJob).filter(SetupRebuildJob.job_id == job_id).one()
                defaults = json.loads(job.plan_json).get("radio_defaults", defaults)
                if defaults.get("language") not in LANGUAGE_IDS or defaults.get("clockDisplay") is not True:
                    raise PresentationError("PRESENTATION_DEFAULTS_INVALID")
            desired_language = LANGUAGE_IDS[str(defaults["language"])]
            write_json_artifact(root, "preferences.json", {"identity": row.device_id.upper(), "before": previous,
                "desired": desired, "clock_before": current_clock, "clock_desired": desired_clock,
                "language_before": current_language, "language_desired": desired_language,
                "setup_defaults": defaults})
        if current_language != desired_language:
            await service.adapter.identify(row)
            if language(await client.get_xml("/language")) != current_language:
                raise PresentationError("PRESENTATION_LANGUAGE_DIVERGED")
            try:
                await client.post_xml("/language", f"<sysLanguage>{desired_language}</sysLanguage>")
            except Exception:
                # The reply can be lost even when the radio accepted a write.
                pass
            if language(await client.get_xml("/language")) != desired_language:
                raise PresentationError("PRESENTATION_LANGUAGE_READBACK_MISMATCH")
        if current_clock != desired_clock:
            if current_clock is None or desired_clock is None:
                raise PresentationError("PRESENTATION_CLOCK_CAPABILITY_CHANGED")
            await service.adapter.identify(row)
            if clock(await client.get_xml("/clockDisplay")) != current_clock:
                raise PresentationError("PRESENTATION_CLOCK_DIVERGED")
            xml = ET.Element("clockDisplay")
            ET.SubElement(xml, "clockConfig", {"userEnable": desired_clock["userEnable"]})
            try:
                await client.post_xml("/clockDisplay", ET.tostring(xml, encoding="unicode"))
            except Exception:
                # A lost write reply is ambiguous. Read once, never replay.
                pass
            if clock(await client.get_xml("/clockDisplay")) != desired_clock:
                raise PresentationError("PRESENTATION_CLOCK_READBACK_MISMATCH")
        db.expire_all()
        if values() != previous:
            raise PresentationError("PRESENTATION_PREFERENCE_CONCURRENT_CHANGE")
        for key, value in desired.items():
            item = db.query(Setting).filter(Setting.key == key).one_or_none()
            if value is None:
                if item is not None:
                    db.delete(item)
            elif item is None:
                db.add(Setting(key=key, value=value))
            else:
                item.value = value
        db.commit()
        db.expire_all()
        if values() != desired:
            raise PresentationError("PRESENTATION_PREFERENCE_READBACK_MISMATCH")
    finally:
        db.close()
