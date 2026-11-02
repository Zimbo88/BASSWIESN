"""Explicit, backed-up radio reboot batches and opt-in wall-clock schedules.

No factory reset, automatic playback resume, volume write, setup or SSH
enablement. A missed/ambiguous operation is never replayed. All batch backups
must pass before any restart command is attempted.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
from uuid import uuid4
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.config import get_settings
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.models import Device, RuntimeState, Setting, SetupRebuildCoordinatorLease, TelnetJob
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.protected_devices import is_device_access_protected, require_unprotected_device
from basswiesn.app.services.setup_rebuild.cli17000 import read_current_config, extract_route_values, reboot
from basswiesn.app.services.setup_rebuild.profiles import DeviceFacts, detect_profile

SCHEDULE_KEY = "radio_reboots.schedule.v1"
JOB_PREFIX = "radio_reboots.job:"
ACTIVE_KEY = "radio_reboots.active"
MANUAL_CONFIRMATION = "REBOOT SELECTED RADIOS"
SCHEDULE_CONFIRMATION = "SCHEDULE RADIO REBOOTS"
HTTP_SNAPSHOT = ("/info", "/sources", "/capabilities", "/now_playing", "/presets", "/volume", "/bass",
                 "/getZone", "/language", "/clockDisplay", "/systemtimeout", "/rebroadcastlatencymode")
SNAPSHOT_ROOTS = {"/info": "info", "/sources": "sources", "/capabilities": "capabilities", "/now_playing": "nowPlaying",
                  "/presets": "presets", "/volume": "volume", "/bass": "bass", "/getZone": "zone",
                  "/language": "sysLanguage", "/clockDisplay": "clockDisplay", "/systemtimeout": "systemtimeout",
                  "/rebroadcastlatencymode": "rebroadcastlatencymode"}
STABLE_PATHS = ("/presets", "/bass", "/language", "/clockDisplay", "/systemtimeout", "/rebroadcastlatencymode")
logger = logging.getLogger(__name__)


def now_utc():
    return datetime.now(UTC)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def read_value(db, key, model=RuntimeState):
    row = db.query(model).filter(model.key == key).one_or_none()
    return json.loads(row.value) if row else None


def put_value(db, key, value, model=RuntimeState):
    row = db.query(model).filter(model.key == key).one_or_none()
    if row is None:
        row = model(key=key)
        db.add(row)
    row.value = encode(value)
    row.updated_at = now_utc()


def schedule_default():
    return {"enabled": False, "device_ids": [], "time": "04:00", "weekdays": list(range(7)),
            "timezone": "Europe/Berlin", "skip_active": True}


def schedule_occurrence(schedule, instant):
    """At most once per local calendar date, including autumn DST folds.

    No catch-up after a missed minute; spring's missing wall-clock minute is
    skipped. A durable occurrence claim prevents replay after server restart.
    """
    if not schedule.get("enabled"):
        return None
    local = instant.astimezone(ZoneInfo(schedule["timezone"]))
    if local.weekday() not in schedule["weekdays"] or local.strftime("%H:%M") != schedule["time"]:
        return None
    return f"{local.date().isoformat()}@{schedule['timezone']}"


def canonical_xml(raw):
    root = ET.fromstring(raw)
    def node(element):
        return (element.tag, tuple(sorted(element.attrib.items())), (element.text or "").strip(), tuple(node(c) for c in element))
    return node(root)


@dataclass(frozen=True)
class Target:
    device_id: str
    ip_address: str
    name: str


def resolve_targets(db, ids):
    if not 1 <= len(ids) <= 16 or len(set(ids)) != len(ids):
        raise ValueError("Select 1–16 distinct radios")
    result = []
    for identifier in ids:
        device = db.query(Device).filter(Device.device_id == identifier).one_or_none()
        if device is None:
            raise ValueError("A selected radio no longer exists")
        require_unprotected_device(device, action="radio_reboot_selection")
        # A stored hostname cannot conceal another household device here.
        ipaddress.ip_address(device.ip_address)
        result.append(Target(device.device_id, device.ip_address, device.name or device.device_id))
    return result


def write_private(path: Path, data: bytes):
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise PermissionError("Backup path must not contain symlinks")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class RebootRadio:
    def __init__(self, target):
        self.target = target
        self.mismatch = False
        self.client = SoundTouchClient(target.ip_address, device_id=target.device_id,
                                       request_purpose="explicit_reboot", trigger="reboot_manager")

    async def identity(self):
        if self.mismatch:
            raise PermissionError("Identity mismatch latched; target access stopped")
        raw = await self.client.get_xml("/info")
        root = ET.fromstring(raw)
        if root.get("deviceID", "").strip().upper() != self.target.device_id.upper():
            self.mismatch = True
            raise PermissionError("Identity mismatch; target access stopped")
        return raw

    async def read(self, path):
        info = await self.identity()
        if path == "/info":
            return info
        if path not in HTTP_SNAPSHOT:
            raise ValueError("Readback route outside reboot scope")
        return await self.client.get_xml(path)

    async def snapshot(self, root):
        self.backup_root = root
        values = {"/info": await self.read("/info")}
        info = ET.fromstring(values["/info"])
        match = detect_profile(DeviceFacts(self.target.device_id, self.target.ip_address,
            info.findtext("type", ""), info.findtext(".//softwareVersion", ""),
            product_id=info.findtext("productID", ""), variant=info.findtext("variant", ""),
            platform=info.findtext("moduleType", "")))
        if match.profile is None:
            raise PermissionError("No confirmed reboot profile for the live model/firmware")
        for path in HTTP_SNAPSHOT:
            if path != "/info":
                values[path] = await self.read(path)
            element = ET.fromstring(values[path])
            if element.tag != SNAPSHOT_ROOTS[path]:
                raise ValueError("Unexpected backup XML root; no reboot allowed")
        if not ET.fromstring(values["/now_playing"]).get("source"):
            raise ValueError("Current source missing from required backup")
        if not ET.fromstring(values["/sources"]).findall("sourceItem"):
            raise ValueError("Source registry not ready for backup")
        actual = ET.fromstring(values["/volume"]).findtext("actualvolume", "")
        if not actual.isdecimal() or not 0 <= int(actual) <= 100:
            raise ValueError("Volume unavailable for backup")
        await self.identity()
        route = await read_current_config(self.target.ip_address, self.target.device_id)
        routing = extract_route_values(route.output)
        if set(routing) != {"bmxRegistryUrl", "margeServerUrl", "swUpdateUrl", "statsServerUrl"} or not all(routing.values()):
            raise RuntimeError("Routing backup unavailable")
        manifest = {}
        for path, raw in values.items():
            name = path[1:] + ".xml"
            content = raw.encode()
            write_private(root / name, content)
            manifest[name] = hashlib.sha256(content).hexdigest()
        content = encode(routing).encode()
        write_private(root / "routing.json", content)
        manifest["routing.json"] = hashlib.sha256(content).hexdigest()
        text = "".join(f"{digest}  {name}\n" for name, digest in sorted(manifest.items()))
        write_private(root / "SHA256SUMS", text.encode())
        for name, digest in manifest.items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
                raise RuntimeError("Backup checksum verification failed")
        return {"http": values, "routing": routing, "sha256": hashlib.sha256(text.encode()).hexdigest()}

    async def send(self):
        # Latest identity, then exactly the researched internal command. Never
        # retry on ambiguous disconnect and never fall back to another radio.
        await self.identity()
        return await reboot(self.target.ip_address, self.target.device_id)

    async def verify(self, snapshot, *, timeout=180):
        offline = False
        returned = False
        deadline = time.monotonic() + timeout
        await asyncio.sleep(2)
        while time.monotonic() < deadline:
            try:
                await self.identity()
                if offline:
                    returned = True
                    break
            except PermissionError:
                raise
            except (OSError, TimeoutError, ET.ParseError, httpx.HTTPError):
                offline = True
            await asyncio.sleep(5)
        if not offline:
            return {"status": "REBOOT_NOT_OBSERVED", "verified": False}
        if not returned:
            return {"status": "VERIFICATION_FAILED", "verified": False, "reason": "ENDPOINT_DID_NOT_RETURN"}
        # Returning /info alone is not verification of preserved settings.
        end = await self.snapshot(self.backup_root.parent / "END")
        after, routing = end["http"], end["routing"]
        preserved = {path: canonical_xml(snapshot["http"][path]) == canonical_xml(after[path]) for path in STABLE_PATHS}
        preserved["routing"] = routing == snapshot["routing"]
        preserved["/volume"] = canonical_xml(snapshot["http"]["/volume"]) == canonical_xml(after["/volume"])
        # Source availability/order may vary during boot, but account/source
        # identities must not disappear silently.
        def sources(raw):
            return sorted((node.get("source", ""), node.get("sourceAccount", "")) for node in ET.fromstring(raw).iter("sourceItem"))
        preserved["source_accounts"] = sources(snapshot["http"]["/sources"]) == sources(after["/sources"])
        def volume(raw):
            root = ET.fromstring(raw)
            return {"actual": root.findtext("actualvolume"), "target": root.findtext("targetvolume"),
                    "muted": root.findtext("muteenabled", root.get("muted", ""))}
        result = {"status": "VERIFIED" if all(preserved.values()) else "STATE_DIFFERENCE",
                "verified": all(preserved.values()), "preserved": preserved,
                "endpoint_restart_observed": True,
                "volume_before": volume(snapshot["http"]["/volume"]), "volume_after": volume(after["/volume"]),
                "source_registry_changed": not preserved["source_accounts"],
                "source": ET.fromstring(after["/now_playing"]).get("source", ""),
                "end_sha256": end["sha256"], "playback_resumed": False, "volume_commands": 0}
        write_private(self.backup_root.parent / "DIFF.json", encode(result).encode())
        return result


def active_state(snapshot):
    playing = ET.fromstring(snapshot["http"]["/now_playing"])
    zone = ET.fromstring(snapshot["http"]["/getZone"])
    return {"standby": playing.get("source", "").upper() == "STANDBY",
            "zone": bool(zone.get("master") or zone.findall(".//member"))}


class RadioRebootManager:
    def __init__(self, session_factory, *, radio_factory=RebootRadio):
        self.session_factory = session_factory
        self.radio_factory = radio_factory
        self.tasks = set()
        self.stopping = False

    def job(self, identifier):
        with self.session_factory() as db:
            return read_value(db, JOB_PREFIX + identifier)

    def persist(self, job):
        job["updated_at"] = now_utc().isoformat()
        with self.session_factory() as db:
            put_value(db, JOB_PREFIX + job["id"], job)
            db.commit()

    def start(self, ids, *, allow_interrupt=False, scheduled_occurrence=None):
        if self.stopping:
            raise RuntimeError("Reboot manager is stopping")
        identifier = uuid4().hex
        job = {"id": identifier, "status": "PREPARING", "device_ids": list(ids),
               "scheduled": scheduled_occurrence is not None, "created_at": now_utc().isoformat(), "results": []}
        with self.session_factory() as db:
            targets = resolve_targets(db, ids)
            from basswiesn.app.services.telnet_device_control import ACTIVE_JOB_STATES
            if db.query(TelnetJob).filter(TelnetJob.device_id.in_(ids), TelnetJob.status.in_(ACTIVE_JOB_STATES)).first():
                raise ValueError("A selected radio has an unfinished Telnet job")
            if any(device_lock(target.device_id).locked() for target in targets):
                raise ValueError("A selected radio is busy")
            active = db.query(RuntimeState).filter_by(key=ACTIVE_KEY).one_or_none()
            if active is not None:
                lease = json.loads(active.value)
                if datetime.fromisoformat(lease["expires_at"]) > now_utc():
                    raise ValueError("Another reboot batch is active or awaiting review")
                previous = read_value(db, JOB_PREFIX + lease["id"])
                if previous:
                    previous["status"] = "INTERRUPTED_NO_REPLAY"
                    put_value(db, JOB_PREFIX + lease["id"], previous)
                db.delete(active)
                db.flush()
            # Atomic unique lease; separate workers cannot start two batches.
            db.add(RuntimeState(key=ACTIVE_KEY, value=encode({"id": identifier, "expires_at": (now_utc() + timedelta(minutes=20)).isoformat()})))
            owner = "radio-reboot:" + identifier
            lease_data = {"owner_id": owner, "job_id": identifier,
                          "expires_at": now_utc() + timedelta(minutes=20), "updated_at": now_utc()}
            lease = db.query(SetupRebuildCoordinatorLease).filter_by(lease_key="global").one_or_none()
            if lease is None:
                db.add(SetupRebuildCoordinatorLease(lease_key="global", **lease_data))
            else:
                acquired = db.query(SetupRebuildCoordinatorLease).filter(
                    SetupRebuildCoordinatorLease.lease_key == "global",
                    or_(SetupRebuildCoordinatorLease.expires_at.is_(None),
                        SetupRebuildCoordinatorLease.expires_at <= now_utc(), SetupRebuildCoordinatorLease.owner_id == ""),
                ).update(lease_data, synchronize_session=False)
                if acquired != 1:
                    db.rollback()
                    raise ValueError("Setup/restore is active; reboot refused")
            if scheduled_occurrence:
                db.add(RuntimeState(key="radio_reboots.occurrence:" + scheduled_occurrence, value=encode(identifier)))
            put_value(db, JOB_PREFIX + identifier, job)
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise ValueError("Reboot already claimed; not replayed") from exc
        task = asyncio.create_task(self.execute(job, targets, allow_interrupt=allow_interrupt), name="radio-reboot:" + identifier)
        self.tasks.add(task)
        task.add_done_callback(self.finished)
        return job

    def finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Do not echo arbitrary exception values or command output.
            logger.error("Reboot task failed: %s", type(task.exception()).__name__)

    async def execute(self, job, targets, *, allow_interrupt):
        prepared = []
        held_locks = []
        try:
            async with asyncio.timeout(600):
                for target in sorted(targets, key=lambda item: item.device_id):
                    lock = device_lock(target.device_id)
                    await lock.acquire()
                    held_locks.append(lock)
                root = get_settings().data_dir / "backups" / "radio-reboots" / job["id"]
                # Retain backups for review instead of silently deleting them.
                # A full quota blocks a new reboot, not a post-write backup.
                backup_base = root.parent
                if any(parent.is_symlink() for parent in (backup_base, *backup_base.parents)):
                    raise PermissionError("Reboot backup path is unsafe")
                backup_base.mkdir(parents=True, exist_ok=True, mode=0o700)
                used = 0
                for path in backup_base.rglob("*"):
                    if path.is_symlink():
                        raise PermissionError("Reboot backup contains a symlink")
                    if path.is_file():
                        used += path.stat().st_size
                        if used > 512 * 1024 * 1024:
                            raise PermissionError("Reboot backup budget full; export/review existing backups first")
                if shutil.disk_usage(backup_base).free < 128 * 1024 * 1024:
                    raise PermissionError("Insufficient free space for reboot backups")
                # Serial preflight is intentionally bounded; dispatch below
                # happens together only after every mandatory backup succeeds.
                for target in targets:
                    radio = self.radio_factory(target)
                    backup = await radio.snapshot(root / hashlib.sha256(target.device_id.encode()).hexdigest()[:20] / "START")
                    state = active_state(backup)
                    if state["zone"]:
                        raise PermissionError("Active SoundTouch zone: stop the group explicitly first")
                    if not state["standby"] and (job["scheduled"] or not allow_interrupt):
                        raise PermissionError("Active playback/source skipped; interruption was not approved")
                    prepared.append((target, radio, backup))
                    job["results"].append({"device_id": target.device_id, "name": target.name,
                                           "status": "BACKED_UP", "backup_sha256": backup["sha256"]})
                    self.persist(job)
                # A user may have started playback while other backups ran.
                # Recheck all states and identities before arming the batch.
                for target, radio, backup in prepared:
                    fresh = {"http": dict(backup["http"])}
                    for path in ("/now_playing", "/getZone"):
                        fresh["http"][path] = await radio.read(path)
                    state = active_state(fresh)
                    if state["zone"] or (not state["standby"] and (job["scheduled"] or not allow_interrupt)):
                        raise PermissionError("Radio state changed; batch not sent")
                    with self.session_factory() as db:
                        current = resolve_targets(db, [target.device_id])[0]
                        if current.ip_address != target.ip_address:
                            raise PermissionError("Stored endpoint changed; repeat the preview")
                job["status"] = "DISPATCHING"
                self.persist(job)
                semaphore = asyncio.Semaphore(4)
                async def one(index, target, radio, backup):
                    async with semaphore:
                        result = job["results"][index]
                        try:
                            with self.session_factory() as db:
                                lease = read_value(db, ACTIVE_KEY)
                                if not lease or lease["id"] != job["id"] or datetime.fromisoformat(lease["expires_at"]) <= now_utc():
                                    raise PermissionError("Reboot lease lost; command not sent")
                            result["status"] = "COMMAND_ATTEMPTED"
                            self.persist(job)  # durable before any irreversible action
                            await radio.send()
                            result["status"] = "WAITING_FOR_RESTART"
                            self.persist(job)
                        except Exception as exc:
                            result.update(status="COMMAND_UNCONFIRMED_NO_RETRY", error_type=type(exc).__name__)
                            self.persist(job)
                            return
                    try:
                        result.update(await radio.verify(backup))
                    except Exception as exc:
                        result.update(status="VERIFICATION_FAILED", verified=False, error_type=type(exc).__name__)
                    self.persist(job)
                await asyncio.gather(*(one(i, *entry) for i, entry in enumerate(prepared)))
                job["status"] = "VERIFIED" if all(item.get("verified") for item in job["results"]) else "PARTIAL_OR_UNCONFIRMED"
        except asyncio.CancelledError:
            job["status"] = "INTERRUPTED_NO_REPLAY"
            raise
        except Exception as exc:
            job.update(status="BLOCKED_NO_REBOOT" if job["status"] == "PREPARING" else "PARTIAL_OR_UNCONFIRMED",
                       error_type=type(exc).__name__, message=str(exc)[:180])
        finally:
            for lock in reversed(held_locks):
                lock.release()
            self.persist(job)
            with self.session_factory() as db:
                active = db.query(RuntimeState).filter_by(key=ACTIVE_KEY).one_or_none()
                if active and json.loads(active.value).get("id") == job["id"]:
                    db.delete(active)
                db.query(SetupRebuildCoordinatorLease).filter_by(lease_key="global", owner_id="radio-reboot:" + job["id"]).update(
                    {"owner_id": "", "job_id": "", "expires_at": None}, synchronize_session=False)
                db.commit()
            write_masterlog("radio_reboot_batch_finished", job_id=job["id"], status=job["status"], scheduled=job["scheduled"])

    async def tick(self, instant=None):
        with self.session_factory() as db:
            schedule = read_value(db, SCHEDULE_KEY, Setting) or schedule_default()
        occurrence = schedule_occurrence(schedule, instant or now_utc())
        if occurrence is None:
            return
        with self.session_factory() as db:
            if read_value(db, "radio_reboots.occurrence:" + occurrence) is not None:
                return
        try:
            self.start(schedule["device_ids"], scheduled_occurrence=occurrence)
        except (ValueError, PermissionError, IntegrityError, HTTPException):
            # No interval retry storm: the occurrence is durably skipped.
            with self.session_factory() as db:
                db.add(RuntimeState(key="radio_reboots.occurrence:" + occurrence, value=encode({"status": "SKIPPED"})))
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()  # Another worker's claim must remain intact.

    async def loop(self, stop):
        while not stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                logger.error("Reboot schedule skipped: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except TimeoutError:
                pass

    async def shutdown(self):
        self.stopping = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
