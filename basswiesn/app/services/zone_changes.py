"""Verified zone removal, with ownership of every known affected radio.

One POST may affect several speakers. A lost response is not permission to retry
it; every participant must provide an identity-bound final topology. Locks only
coordinate BASSWIESN workers, never physical buttons or external controllers.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, cast
from uuid import uuid4
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeXML  # type: ignore[import-untyped]
import httpx
from sqlalchemy.orm import Session

from basswiesn.app.models import ConfigBackup, Device
from basswiesn.app.core.device_ingress import canonical_peer
from basswiesn.app.services.action_journal import record_action
from basswiesn.app.services.device_settings_writer import XmlClient, verify_settings_identity
from basswiesn.app.services.device_transactions import device_fingerprint, device_transaction
from basswiesn.app.services.protected_devices import require_unprotected_device


class ZoneChangeError(Exception):
    def __init__(self, code: str, *, attempted: bool = False, evidence: dict[str, Any] | None = None) -> None:
        self.detail = {"error": code, "status": "DIVERGED" if attempted else "BLOCKED",
                       "reconciliation_required": attempted, **(evidence or {})}
        super().__init__(code)


@dataclass(frozen=True)
class Zone:
    master: str
    members: tuple[tuple[str, str], ...]

    @classmethod
    def parse(cls, xml: str, *, device_id: str = '') -> Zone:
        root = cast(ET.Element, SafeXML.fromstring(xml, forbid_dtd=True))
        if root.tag != "zone":
            raise ValueError("expected zone XML")
        claimed = root.get('deviceID')
        if device_id and claimed is not None and claimed.strip().upper() != device_id.upper():
            raise ValueError('zone response identity differs')
        members = tuple(sorted(((node.text or "").strip().upper(), node.get("ipaddress", ""))
                               for node in root.findall("member")))
        ids = [item[0] for item in members]
        if any(not identity for identity in ids) or len(ids) != len(set(ids)):
            raise ValueError("ambiguous member identity")
        master = root.get("master", "").strip().upper()
        if members and not master:
            raise ValueError("members without coordinator")
        return cls(master, members)

    def standalone(self, identity: str) -> bool:
        return self.master in {"", identity.upper()} and not any(
            member != identity.upper() for member, _ in self.members)

    def summary(self) -> dict[str, Any]:
        return {"active": bool(self.master), "master_device_id": self.master,
                "members": [{"device_id": identity, "ip_address": ip} for identity, ip in self.members]}


@dataclass(frozen=True)
class ZonePorts:
    client: Callable[[Device], XmlClient]
    guard: Callable[[Session, Device], None]


def require_selected_scope(zone: Zone, selected: Mapping[str, str]) -> None:
    """A new zone cannot silently detach an unowned former coordinator/member."""
    peers = {identity.upper(): canonical_peer(ip) for identity, ip in selected.items()}
    if zone.master and zone.master not in peers:
        raise ZoneChangeError("ZONE_FORMER_COORDINATOR_NOT_SELECTED")
    for identity, ip in zone.members:
        if identity not in peers or (ip and canonical_peer(ip) != peers[identity]):
            raise ZoneChangeError("ZONE_UNSELECTED_PARTICIPANT")


async def read_zone(device: Device, ports: ZonePorts) -> tuple[Zone, dict[str, str]]:
    require_unprotected_device(device, action="zone_readback", requester="zone_transaction")
    client = ports.client(device)
    info = await client.get_xml("/info")
    verify_settings_identity(device, info)  # reuse the exact-profile authority
    xml = await client.get_xml("/getZone")
    return Zone.parse(xml, device_id=device.device_id), {"/info": info, "/getZone": xml}


def _participants(db: Session, master: Device, expected: Zone) -> list[Device]:
    if expected.master not in {"", master.device_id.upper()}:
        raise ZoneChangeError("ZONE_COORDINATOR_CHANGED")
    known = {device.device_id.upper(): device for device in db.query(Device).all()}
    participants = {master.device_id.upper(): master}
    for identity, ip in expected.members:
        device = known.get(identity)
        if device is None or (ip and canonical_peer(ip) != canonical_peer(device.ip_address)):
            raise ZoneChangeError("ZONE_UNKNOWN_PARTICIPANT")
        participants[identity] = device
    return list(participants.values())


def payload(master: Device, remaining: list[Device]) -> str:
    root = ET.Element("zone", {"master": master.device_id})
    for device in remaining:
        ET.SubElement(root, "member", {"ipaddress": device.ip_address}).text = device.device_id
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)


async def change_zone(db: Session, master: Device, expected: Zone, *, ports: ZonePorts,
                      remove: Device | None = None) -> dict[str, Any]:
    """Clear a whole zone or remove exactly one member. Never change volume."""
    participants = _participants(db, master, expected)
    if remove is not None and remove.device_id.upper() == master.device_id.upper():
        raise ZoneChangeError("ZONE_CANNOT_REMOVE_COORDINATOR")
    if remove is not None and remove.device_id not in {item.device_id for item in participants}:
        raise ZoneChangeError("ZONE_MEMBER_NOT_PRESENT")
    remaining = [item for item in participants if item.device_id != master.device_id
                 and remove is not None and item.device_id != remove.device_id]
    fingerprints = {id(device): device_fingerprint(device) for device in participants}
    targets = {device.device_id: device.ip_address for device in participants}

    def guard_current(device: Device) -> None:
        ports.guard(db, device)
        if device_fingerprint(device) != fingerprints[id(device)]:
            raise ZoneChangeError('ZONE_TARGET_CHANGED')

    for device in participants:
        guard_current(device)  # including indirect/protected members, before transport
    async with device_transaction(item.device_id for item in participants):
        before: dict[str, dict[str, str]] = {}
        try:
            for device in participants:
                guard_current(device)
                zone, before[device.device_id] = await read_zone(device, ports)
                require_selected_scope(zone, targets)
                if device.device_id == master.device_id and zone != expected:
                    raise ZoneChangeError("ZONE_PRECONDITION_CHANGED")
                if zone.master != master.device_id.upper() and not zone.standalone(device.device_id):
                    raise ZoneChangeError("ZONE_CONFLICTING_TOPOLOGY")
        except ZoneChangeError:
            raise
        except Exception as exc:
            raise ZoneChangeError("ZONE_BACKUP_UNAVAILABLE", evidence={"cause": type(exc).__name__}) from None
        job = f"zone-{uuid4().hex}"
        content = json.dumps(before, sort_keys=True)
        digest = sha256(content.encode()).hexdigest()
        backup = ConfigBackup(device_id=master.device_id, path=f"{job}/before.json", content=content)
        db.add(backup)
        db.flush()
        backup_id = backup.id
        backup_ref = f"config_backup:{backup_id}"
        record_action(db, job_id=job, device_id=master.device_id, ip_address=master.ip_address,
                      action="zone_remove" if remove else "zone_clear", trigger="webui", phase="ZONE_INTENT",
                      backup_ref=backup_ref, before_state={"sha256": digest},
                      requested_state={"members": [item.device_id for item in remaining]})
        db.commit()  # no write if the complete START/intent cannot be persisted
        stored = db.get(ConfigBackup, backup_id, populate_existing=True)
        if stored is None or sha256(stored.content.encode()).hexdigest() != digest:
            raise ZoneChangeError('ZONE_BACKUP_INTEGRITY', evidence={'backup_ref': backup_ref})
        try:
            for device in participants:
                guard_current(device)
                current, _ = await read_zone(device, ports)
                if current != Zone.parse(before[device.device_id]["/getZone"]):
                    raise ZoneChangeError("ZONE_PRECONDITION_CHANGED")
        except ZoneChangeError:
            raise
        except Exception as exc:
            raise ZoneChangeError("ZONE_PRECONDITION_UNAVAILABLE", evidence={"cause": type(exc).__name__}) from None
        response, transport_error, rejected = "", "", False
        xml = payload(master, remaining)
        # The final participant read yields control. Recheck frozen inputs for
        # every participant immediately before constructing the write client.
        for device in participants:
            guard_current(device)
        try:
            response = await ports.client(master).post_xml("/setZone", xml)
        except (httpx.TransportError, OSError) as exc:
            transport_error = type(exc).__name__
        except Exception as exc:
            transport_error, rejected = type(exc).__name__, True
        observations: dict[str, dict[str, Any]] = {}
        attempts: list[dict[str, Any]] = []
        verified = False
        desired = {item.device_id.upper() for item in remaining}
        for attempt in range(1, 11):
            await asyncio.sleep(0.35 if attempt == 1 else 0.5)
            all_ok = True
            for device in participants:
                try:
                    guard_current(device)
                    zone, _ = await read_zone(device, ports)
                    require_selected_scope(zone, targets)
                    if device.device_id == master.device_id:
                        ok = (zone.master == master.device_id.upper()
                              and {item for item, _ in zone.members} - {master.device_id.upper()} == desired
                              if desired else zone.standalone(device.device_id))
                    elif device.device_id.upper() in desired:
                        ok = (zone.master == master.device_id.upper()
                              and all(identity in desired | {master.device_id.upper()} for identity, _ in zone.members))
                    else:
                        ok = zone.standalone(device.device_id)
                    observations[device.device_id] = {**zone.summary(), "ok": ok}
                except Exception as exc:
                    ok = False
                    observations[device.device_id] = {"ok": False, "error": type(exc).__name__}
                all_ok = all_ok and ok
            target_ok = observations.get(remove.device_id, {}).get("ok", False) if remove else all_ok
            attempts.append({"attempt": attempt, "master_excludes_target": observations[master.device_id]["ok"],
                             "target_is_standalone": target_ok, "all_participants_verified": all_ok})
            verified = all_ok and not rejected
            if all_ok:
                break
        result = {"verified": verified, "response": response, "xml": xml, "backup_ref": backup_ref,
                  "remaining": [item.name for item in remaining], "readback_attempts": attempts,
                  "verification": observations, "transport_error": transport_error,
                  "automatic_retry": False, "automatic_rollback": False}
        try:
            record_action(db, job_id=job, device_id=master.device_id, ip_address=master.ip_address,
                          action="zone_remove" if remove else "zone_clear", trigger="webui",
                          phase="VERIFIED" if verified else "READBACK_MISMATCH", backup_ref=backup_ref,
                          readback=observations, error_category=transport_error, verified=verified)
            db.commit()
        except Exception:
            db.rollback()
            # The START/intent remains durable. Never replay a radio write
            # because its final audit record could not be committed.
            raise ZoneChangeError('ZONE_FINAL_RECORD_FAILED', attempted=True, evidence={
                **result, 'readback_verified': verified, 'verified': False,
                'commit_confirmed': False,
            }) from None
        if not verified:
            raise ZoneChangeError("ZONE_READBACK_MISMATCH", attempted=True, evidence=result)
        return result
