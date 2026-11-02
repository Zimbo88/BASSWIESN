"""Manual LAB recovery with durable START and identity-bound readback.

This legacy route may restore explicitly captured playback. The newer reboot
manager deliberately does not resume playback. Neither schedules this legacy
operation. Command acceptance is never verified reboot/restore success.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from time import monotonic
from typing import Any, cast
from uuid import uuid4
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeXML  # type: ignore[import-untyped]
from sqlalchemy.orm import Session

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.config import get_settings
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.models import ConfigBackup, Device, Setting, Station, utc_now
from basswiesn.app.services.action_journal import record_action, record_transport_attempt
from basswiesn.app.services.action_preflight import action_preflight, port_open
from basswiesn.app.services.device_settings_writer import ClientFactory, IdentityMismatch, verify_settings_identity
from basswiesn.app.services.device_transactions import device_fingerprint, device_transaction
from basswiesn.app.services.device_ownership import DeviceBusy, DeviceLockUnavailable
from basswiesn.app.services.direct_selection import Selection
from basswiesn.app.services.playback_state import close_open_sessions, confirm_playback_session
from basswiesn.app.services.protected_devices import require_unprotected_device
from basswiesn.app.services.zone_changes import Zone

CliSender = Callable[..., Awaitable[Any]]
PATHS = {"/now_playing": "nowPlaying", "/volume": "volume", "/getZone": "zone", "/presets": "presets"}


def maintenance_reboot_lab_enabled(db: Session) -> bool:
    row = db.query(Setting).filter(Setting.key == "lab_mode").one_or_none()
    return bool(get_settings().lab_mode or (row is not None and str(row.value).strip().lower() == "true"))


def _root(xml: str, tag: str, identity: str = "") -> ET.Element:
    if not xml or len(xml.encode()) > 131072:
        raise ValueError("missing or excessive XML")
    root = cast(ET.Element, SafeXML.fromstring(xml, forbid_dtd=True))
    if root.tag != tag:
        raise ValueError("unexpected XML root")
    if identity and root.get("deviceID") is not None and root.get("deviceID", "").upper() != identity.upper():
        raise IdentityMismatch("observation belongs to another radio")
    return root


def _volume(xml: str) -> tuple[int, bool]:
    root = _root(xml, "volume")
    if any(len(root.findall(tag)) != 1 for tag in ("actualvolume", "muteenabled")):
        raise ValueError("actual volume and mute must be unambiguous")
    actual, mute = root.findtext("actualvolume", ""), root.findtext("muteenabled", "")
    if not actual.isascii() or not actual.isdecimal() or not 0 <= int(actual) <= 100 or mute not in {"true", "false"}:
        raise ValueError("actual volume or mute unknown")
    return int(actual), mute == "true"


def _canonical(node: ET.Element) -> tuple[Any, ...]:
    return (node.tag, tuple(sorted(node.attrib.items())), (node.text or "").strip(),
            tuple(_canonical(child) for child in node))


@dataclass(frozen=True)
class RebootSnapshot:
    raw: dict[str, str]
    selection: Selection
    volume: tuple[int, bool]
    zone: Zone
    presets: tuple[Any, ...]
    preset_id: int | None
    captured_at: datetime

    @classmethod
    def parse(cls, raw: dict[str, str], identity: str, *, allow_pending: bool = False) -> RebootSnapshot:
        roots = {path: _root(raw[path], tag, identity) for path, tag in PATHS.items()}
        selection = Selection.read(raw["/now_playing"], identity, allow_pending=allow_pending)
        if not selection.play_status:
            raise ValueError("playback state unknown")
        matches = [node for node in roots["/presets"].findall("preset")
                   if len(node.findall("ContentItem")) == 1 and selection.location
                   and Selection.content(node.findall("ContentItem")[0]).same_content(selection)]
        preset_id = None
        if len(matches) == 1:
            slot = matches[0].get("id", matches[0].get("buttonNumber", ""))
            if (slot in {"1", "2", "3", "4", "5", "6"} and
                sum(node.get("id", node.get("buttonNumber", "")) == slot
                    for node in roots["/presets"].findall("preset")) == 1):
                preset_id = int(slot)
        return cls(dict(raw), selection, _volume(raw["/volume"]), Zone.parse(raw["/getZone"], device_id=identity),
                   _canonical(roots["/presets"]), preset_id, utc_now())

    def same_state(self, other: RebootSnapshot) -> bool:
        # Track text/artwork can advance; content/state/actual volume and mute,
        # zone and stored presets may not silently diverge.
        return (self.selection, self.volume, self.zone, self.presets) == (
            other.selection, other.volume, other.zone, other.presets)

    def summary(self, device_id: str) -> dict[str, Any]:
        root = _root(self.raw["/now_playing"], "nowPlaying", device_id)
        content = root.find("ContentItem")
        return {"device_id": device_id, "source": self.selection.source,
                "play_status": self.selection.play_status, "content_item": dict(content.attrib) if content is not None else {},
                "volume": self.volume[0], "muted": self.volume[1],
                "confirmed_playing": self.selection.play_status == "PLAY_STATE",
                "standby": self.selection.source == "STANDBY", "preset_id": self.preset_id,
                "zone_active": not self.zone.standalone(device_id), "zone_master_id": self.zone.master,
                "zone_xml": self.raw["/getZone"][:2048], "reachable": True,
                "captured_at": self.captured_at.isoformat(), "stream_url": self.selection.location,
                "station_name": "", "station_id": None,
                "now_playing": {child.tag: (child.text or "").strip() for child in root if child.text}}


def _summary(snapshot: RebootSnapshot, device_id: str, db: Session) -> dict[str, Any]:
    """Retain the legacy snapshot envelope; catalog labels are not verification."""
    result = snapshot.summary(device_id)
    if snapshot.selection.location:
        station = db.query(Station).filter(Station.stream_url == snapshot.selection.location,
                                           Station.internal.is_(False)).one_or_none()
        if station:
            result.update(station_name=station.name, station_id=station.id)
    return result


async def capture_reboot_snapshot(device: Device, db: Session, *,
                                  client_factory: ClientFactory = SoundTouchClient) -> dict[str, Any]:
    """Read-only compatibility entry; verify identity before reading state."""
    require_unprotected_device(device, action="maintenance_reboot_snapshot", requester="maintenance_reboot")
    client = client_factory(device.ip_address, device_id=device.device_id,
                            request_purpose="maintenance_reboot", trigger="manual_lab")
    raw = {"/info": await client.get_xml("/info")}
    _root(raw["/info"], "info", device.device_id)
    _root(device.info_xml, "info", device.device_id)
    verify_settings_identity(device, raw["/info"])
    for path in PATHS:
        raw[path] = await client.get_xml(path)
    return _summary(RebootSnapshot.parse(raw, device.device_id), device.device_id, db)


async def run_maintenance_reboot(device: Device, db: Session, *, trigger: str = "automatic",
                                send_cli: CliSender | None = None,
                                client_factory: ClientFactory = SoundTouchClient,
                                wait_for_return: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "verified": False, "job_id": f"maintenance-{uuid4().hex}",
                             "automatic_retry": False, "audible_audio_verified": False,
                             "reconciliation_required": False}
    if trigger != "manual_lab":
        return {**result, "code": "AUTOMATIC_RADIO_REBOOT_DISABLED",
                "message": "Radio-Reboot ist ausschliesslich als manuelle LAB-Aktion erlaubt."}
    if not maintenance_reboot_lab_enabled(db):
        return {**result, "code": "LAB_MODE_REQUIRED", "message": "Radio-Reboot erfordert den LAB-Modus."}

    attempted, backup_ref, snapshot = False, "", {}
    code = "MAINTENANCE_PREFLIGHT_FAILED"
    frozen = device_fingerprint(device)
    started = datetime.now(UTC)
    try:
        require_unprotected_device(device, action="maintenance_reboot", requester="maintenance_reboot")
        _root(frozen[4], "info", frozen[0])
        verify_settings_identity(device, frozen[4])
        async with device_transaction([frozen[0]]):
            def guard() -> None:
                db.refresh(device)
                if device_fingerprint(device) != frozen:
                    raise IdentityMismatch("stored target changed")
                require_unprotected_device(device, action="maintenance_reboot", requester="maintenance_reboot")

            guard()
            preflight = await action_preflight(db, device, required_port=17000, action="maintenance_reboot")
            if not preflight["ok"]:
                return {**result, "code": preflight["code"], "message": preflight["message"]}
            guard()
            client = client_factory(frozen[1], device_id=frozen[0], request_purpose="maintenance_reboot", trigger=trigger)

            async def observe() -> RebootSnapshot:
                guard()
                raw = {"/info": await client.get_xml("/info")}
                _root(raw["/info"], "info", frozen[0])
                _root(frozen[4], "info", frozen[0])
                verify_settings_identity(device, raw["/info"])
                for path in PATHS:
                    guard()
                    raw[path] = await client.get_xml(path)
                guard()
                observed = RebootSnapshot.parse(raw, frozen[0])
                if not observed.zone.standalone(frozen[0]):
                    raise ValueError("active multiroom cannot be changed by single-radio recovery")
                return observed

            before = await observe()
            snapshot = _summary(before, frozen[0], db)
            result["snapshot"] = snapshot
            content = json.dumps({"schema": 1, "device_id": frozen[0], "http": before.raw}, sort_keys=True)
            digest = sha256(content.encode()).hexdigest()
            backup = ConfigBackup(device_id=frozen[0], path="http8090:maintenance-reboot-start", content=content)
            db.add(backup)
            db.flush()
            backup_id = backup.id
            backup_ref = f"config-backup:{backup_id}:sha256:{digest}"
            result["backup_ref"] = backup_ref
            device.maintenance_last_attempt_at = started
            device.maintenance_phase = "snapshot_captured"
            record_action(db, job_id=result["job_id"], device_id=frozen[0], ip_address=frozen[1],
                          action="maintenance_reboot", trigger=trigger, phase="snapshot_captured",
                          before_state=snapshot, backup_ref=backup_ref, result="PREPARED")
            db.commit()  # START must survive process loss before any command.

            def check_backup() -> None:
                stored = db.get(ConfigBackup, backup_id, populate_existing=True)
                if stored is None or stored.device_id != frozen[0] or sha256(stored.content.encode()).hexdigest() != digest:
                    raise ValueError("START backup missing or changed")

            def intent(phase: str, requested: object = None) -> None:
                guard()
                check_backup()
                device.maintenance_phase = phase
                record_action(db, job_id=result["job_id"], device_id=frozen[0], ip_address=frozen[1],
                              action="maintenance_reboot", trigger=trigger, phase=phase, before_state=snapshot,
                              requested_state=requested, backup_ref=backup_ref, result="PENDING_READBACK")
                db.commit()
                guard()
                check_backup()

            code = "WRITE_PRECONDITION_FAILED"
            if not before.same_state(await observe()):
                raise ValueError("state changed after START")
            intent("reboot_requested", "sys reboot")
            if not before.same_state(await observe()):
                raise ValueError("state changed while reboot intent was committed")
            if send_cli is None:
                from basswiesn.app.routers.api import _send_cli17000
                send_cli = _send_cli17000
            code = "REBOOT_OUTCOME_UNKNOWN"
            attempted = True
            await send_cli(frozen[1], ["sys reboot"], timeout=10.0)
            if not wait_for_return:
                device.maintenance_phase = "pending_readback"
                device.maintenance_last_result = "reboot_pending_readback"
                db.commit()
                return {**result, "code": "REBOOT_PENDING_READBACK", "phase": "pending_readback",
                        "reconciliation_required": True}

            # A listener that stayed reachable does not prove a reboot. Require
            # an unavailable interval and full identity verification on return.
            deadline = monotonic() + get_settings().maintenance_reboot_return_timeout_seconds
            seen_offline = False
            returned: RebootSnapshot | None = None
            while monotonic() < deadline:
                guard()
                if not await port_open(frozen[1], get_settings().radio_port, 2.0):
                    seen_offline = True
                elif seen_offline:
                    returned = await observe()
                    break
                await asyncio.sleep(2)
            if returned is None:
                raise TimeoutError("offline/identity-return transition not observed")
            result["offline_online_observed"] = True
            code = "RESTORE_READBACK_MISMATCH"

            async def restore_write(path: str, xml: str) -> None:
                current = await observe()
                if current.zone != before.zone or current.presets != before.presets:
                    raise ValueError("topology or presets changed; no restore writes")
                intent("restoring_state", {"path": path})
                if not current.same_state(await observe()):
                    raise ValueError("state changed while restore intent was committed")
                # A lost press/release reply stops the composite; never replay
                # toggles or remaining actions against an uncertain state.
                try:
                    await client.post_xml(path, xml)
                except Exception:
                    # Observe once, without treating a currently matching field
                    # as completion of the whole interrupted restore sequence.
                    try:
                        result["last_observation"] = (await observe()).summary(frozen[0])
                    except Exception:
                        result["readback_unavailable"] = True
                    raise

            async def key(name: str) -> None:
                for state in ("press", "release"):
                    await restore_write("/key", f'<key state="{state}" sender="Gabbo">{name}</key>')

            if returned.zone != before.zone or returned.presets != before.presets:
                raise ValueError("stored presets/topology differ after reboot")
            # Restore volume before playback; matching state needs no write.
            if returned.volume[0] != before.volume[0]:
                await restore_write("/volume", f"<volume>{before.volume[0]}</volume>")
            current = await observe()
            if current.volume[0] != before.volume[0]:
                raise ValueError("actual volume did not return to START")
            if current.volume[1] != before.volume[1]:
                await key("MUTE")
            current = await observe()
            if current.volume != before.volume:
                raise ValueError("actual volume/mute differ from START")
            if current.selection != before.selection:
                if before.selection.source == "STANDBY":
                    if current.selection.source != "STANDBY":
                        await key("POWER")
                elif before.selection.play_status == "PLAY_STATE":
                    if before.preset_id:
                        await key(f"PRESET_{before.preset_id}")
                    elif before.selection.source == "LOCAL_INTERNET_RADIO" and not before.selection.source_account:
                        item = _root(before.raw["/now_playing"], "nowPlaying").find("ContentItem")
                        if item is None or item.get("type") != "stationurl":
                            raise ValueError("unsupported restore ContentItem")
                        # Restore captured identity, not a newly resolved URL or
                        # stale account inferred from a current station row.
                        await restore_write("/select", ET.tostring(item, encoding="unicode"))
            actual = await observe()
            if not before.same_state(actual):
                raise ValueError("final state does not match START")
            check_backup()
            guard()
            code = "MAINTENANCE_RESULT_COMMIT_FAILED"
            close_open_sessions(db, frozen[0], reason="maintenance_reboot", device_last_seen=device.last_seen)
            if before.selection.play_status == "PLAY_STATE":
                station = db.query(Station).filter(Station.stream_url == before.selection.location, Station.internal.is_(False)).one_or_none()
                confirm_playback_session(db, device, observed_at=utc_now(), source=before.selection.source,
                    station_id=station.id if station else None, station_name=station.name if station else "",
                    stream_url=before.selection.location, trigger="maintenance_restore", trigger_type="station", internal_event=False)
            device.maintenance_last_success_at = utc_now()
            device.maintenance_next_run_at = None
            device.maintenance_last_result = "state_restored_verified"
            device.maintenance_phase = "completed"
            device.maintenance_failure_count = 0
            record_action(db, job_id=result["job_id"], device_id=frozen[0], ip_address=frozen[1],
                          action="maintenance_reboot", trigger=trigger, phase="completed", before_state=snapshot,
                          after_state=actual.summary(frozen[0]), backup_ref=backup_ref, result="VERIFIED", verified=True)
            db.commit()
            return {**result, "ok": True, "verified": True, "phase": "completed", "result": "state_restored_verified"}
    except (Exception, asyncio.CancelledError) as exc:
        db.rollback()
        code = "IDENTITY_MISMATCH" if isinstance(exc, IdentityMismatch) else code
        if isinstance(exc, (DeviceBusy, DeviceLockUnavailable)):
            code = "DEVICE_BUSY" if isinstance(exc, DeviceBusy) else "DEVICE_LOCK_UNAVAILABLE"
        phase = "diverged" if attempted else "blocked"
        # Intent above is mandatory; outcome persistence is best effort after
        # failure. Never repeat the radio action because audit storage failed.
        record_transport_attempt(device_id=frozen[0], ip_address=frozen[1], action="maintenance_reboot",
            trigger=trigger, phase=phase, result=phase.upper(), job_id=result["job_id"],
            backup_ref=backup_ref, rollback_ref=backup_ref, error_category=code, verified=False)
        try:
            db.refresh(device)
            if device_fingerprint(device) == frozen:
                device.maintenance_phase = phase
                device.maintenance_last_result = code
                device.maintenance_failure_count = (device.maintenance_failure_count or 0) + 1
                device.maintenance_next_run_at = None
                db.commit()
        except Exception:
            db.rollback()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return {**result, "code": code, "phase": phase, "reconciliation_required": attempted,
                "message": "Zustand nicht bestätigt. Keine automatische Wiederholung; START und Radiozustand prüfen."}


async def run_due_maintenance(db: Session) -> list[dict[str, Any]]:
    """Disable legacy scheduled radio reboots without contacting a device."""
    changed = False
    for device in db.query(Device).filter(Device.maintenance_reboot_enabled.is_(True)).all():
        device.maintenance_reboot_enabled = False
        device.maintenance_next_run_at = None
        device.maintenance_phase = "idle"
        device.maintenance_last_result = "automatic_reboot_disabled_in_1_6"
        changed = True
        write_masterlog("maintenance_reboot_schedule_disabled", device_id=device.device_id,
                        reason="radio reboot is manual LAB only")
    if changed:
        db.commit()
    return []


async def maintenance_reboot_loop(stop_event: asyncio.Event) -> None:
    """Compatibility no-op: legacy maintenance never schedules a radio reboot."""
    del stop_event
    write_masterlog("maintenance_reboot_scheduler_disabled", reason="radio reboot is manual LAB only")
