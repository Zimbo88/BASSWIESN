"""Bounded preset observations and identity/DB preconditions, never radio writes."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from basswiesn.app.models import Device, Preset, PresetMutation
from basswiesn.app.services.device_settings_writer import ClientFactory, IdentityMismatch, verify_settings_identity
from basswiesn.app.services.device_transactions import device_fingerprint
from basswiesn.app.services.maintenance_reboot import _canonical, _root
from basswiesn.app.services.preset_contract import _xml_local_name, preset_summaries_from_xml
from basswiesn.app.services.preset_transactions import _digest, preset_snapshot


@dataclass(frozen=True)
class PresetObservation:
    raw: str
    rows: list[dict[str, Any]]
    slots: dict[int, tuple[Any, ...]]

    @classmethod
    def parse(cls, xml: str, identity: str) -> PresetObservation:
        root = _root(xml, "presets", identity)
        slots: dict[int, tuple[Any, ...]] = {}
        for node in root:
            if _xml_local_name(node.tag) != "preset":
                raise ValueError("unsupported preset response structure")
            identifiers = [node.get(key) for key in ("id", "button", "index") if node.get(key) is not None]
            if not identifiers or len(set(identifiers)) != 1 or identifiers[0] not in {"1", "2", "3", "4", "5", "6"}:
                raise ValueError("missing or ambiguous preset slot")
            button = int(str(identifiers[0]))
            contents = [child for child in node if _xml_local_name(child.tag) == "ContentItem"]
            if button in slots or len(contents) != 1:
                raise ValueError("duplicate slot or ambiguous ContentItem")
            content = contents[0]
            if not content.get("source") or not content.get("location"):
                raise ValueError("incomplete preset selection")
            # Ignore wrapper timestamps, not unknown ContentItem fields.
            slots[button] = _canonical(content)
        return cls(xml, preset_summaries_from_xml(xml), slots)

    def without(self, button: int) -> dict[int, tuple[Any, ...]]:
        return {slot: value for slot, value in self.slots.items() if slot != button}


def requested_observation(button: int, requested: dict[str, Any], identity: str) -> PresetObservation:
    content = requested.get("content_item_xml")
    if not isinstance(content, str) or not content:
        raise ValueError("original complete ContentItem intent unavailable")
    result = PresetObservation.parse(f'<presets><preset id="{button}">{content}</preset></presets>', identity)
    row = result.rows[0]
    for key in ("source", "source_account", "location"):
        if key in requested and requested[key] != row[key]:
            raise ValueError("requested fields contradict original ContentItem")
    return result


def local_version(db: Session, identity: str, *, exclude_mutations: set[str] | None = None) -> str:
    presets = db.query(Preset).filter_by(device_id=identity).order_by(Preset.button).populate_existing().all()
    mutations = db.query(PresetMutation).filter_by(device_id=identity).order_by(PresetMutation.id).populate_existing().all()
    return _digest({"presets": [preset_snapshot(row) for row in presets], "mutations": [
        {key: getattr(row, key) for key in ("mutation_id", "button", "operation", "revision", "state", "diverged",
            "requested_sha256", "expected_previous_sha256", "before_radio_sha256", "after_radio_sha256", "backup_ref")}
        for row in mutations if row.mutation_id not in (exclude_mutations or set())]})


class PresetAccess:
    """Use the existing profile and protection authorities, not another policy."""
    def __init__(self, db: Session, device: Device, *, client_factory: ClientFactory,
                 guard: Callable[[Session, Device], None], write: bool) -> None:
        self.db, self.device, self.guard, self.write = db, device, guard, write
        self.frozen = device_fingerprint(device)
        self.identity = self.frozen[0]
        guard(db, device)
        if write:
            _root(device.info_xml, "info", self.identity)
            verify_settings_identity(device, device.info_xml)
        self.client = client_factory(self.frozen[1], device_id=self.identity,
            request_purpose="preset_recovery", trigger="explicit_preset_action")

    def unchanged(self) -> None:
        self.db.refresh(self.device)
        if device_fingerprint(self.device) != self.frozen:
            raise IdentityMismatch("stored preset target changed")
        self.guard(self.db, self.device)

    async def observe(self) -> PresetObservation:
        self.unchanged()
        xml = await self.client.get_xml("/info")
        node = _root(xml, "info", self.identity)
        if node.get("deviceID", "").upper() != self.identity.upper():
            raise IdentityMismatch("live preset target missing or changed")
        if self.write:
            verify_settings_identity(self.device, xml)
        self.unchanged()
        result = PresetObservation.parse(await self.client.get_xml("/presets"), self.identity)
        self.unchanged()
        return result

    def commit_window(self, expected: str, *, exclude_mutations: set[str] | None = None) -> None:
        # The application uses SQLite. Hold its write reservation only during
        # the local compare/commit, NEVER while awaiting radio I/O/callbacks.
        # Radio callbacks deliberately do not take device ownership; doing so
        # while a radio waits for its callback response could deadlock a write.
        if self.db.new or self.db.dirty or self.db.deleted:
            raise ValueError("unexpected pending changes before local commit")
        self.db.rollback()
        if self.db.get_bind().dialect.name != "sqlite":
            raise ValueError("preset commit isolation not established for this database")
        self.db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        self.unchanged()
        if local_version(self.db, self.identity, exclude_mutations=exclude_mutations) != expected:
            raise ValueError("local preset state/revision changed")
