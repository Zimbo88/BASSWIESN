"""Explicit LAB owner: native group observations -> existing zone transactions.

No listener, discovery, background startup or automatic feature activation.
The caller owns/provisions the exact one-use audio relay, verifies its selection
and keeps its audio gate closed during topology changes. This service never
selects a source, sets volume, sends keys, or treats zone success as audible audio.
Initial foreign zones are refused; uncertain writes require reconciliation.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import time
from uuid import uuid4

from sqlalchemy.orm import Session

from basswiesn.app.models import ConfigBackup, Device
from basswiesn.app.core.device_ingress import canonical_peer
from basswiesn.app.services.action_journal import record_action
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.device_transactions import device_fingerprint, device_transaction
from basswiesn.app.services.device_settings_writer import XmlClient, apply_settings, verify_settings_identity
from basswiesn.app.services.direct_selection import Selection
from basswiesn.app.services.protected_devices import require_unprotected_device
from basswiesn.app.services.settings_contract import plan_setting, read_setting
from basswiesn.app.services.zone_changes import Zone, ZonePorts, change_zone, payload, read_zone
from basswiesn.app.services.zone_creation import creation_scope, read_zone_value
from .group_session import GroupPlan, GroupSession, ReceiverSample


class GroupZoneRejected(RuntimeError):
    def __init__(self, code: str, *, backup_ref: str = "", attempted: bool = False) -> None:
        self.code, self.backup_ref, self.attempted = code, backup_ref, attempted
        self.reconciliation_required = attempted
        super().__init__(code)


@dataclass(frozen=True)
class GroupZoneResult:
    status: str
    master: str
    members: tuple[str, ...]
    backup_ref: str
    volumes: tuple[tuple[str, int], ...]
    volume_changed: tuple[str, ...]
    audio_released: bool = False


class _CheckedClient:
    def __init__(self, owner: GroupZone, device: Device) -> None:
        self.owner, self.device = owner, device

    async def get_xml(self, path: str) -> str:
        self.owner.guard(self.owner.db, self.device)
        xml = await self.owner.raw[self.device.device_id].get_xml(path)
        if len(xml.encode()) > 131072:
            raise self.owner.reject("AIRPLAY_EXCESSIVE_READBACK")
        # Reuse the actual zone transaction's bounded-profile/XML semantics,
        # including contradictory device IDs, instead of trusting bare volume.
        read_zone_value(path, xml, self.device)
        self.owner.guard(self.owner.db, self.device)
        return xml

    async def post_xml(self, path: str, xml: str) -> str:
        # Only central backup/readback services call this port. Observe current
        # relay selection just before each write; never follow a new user source.
        if path not in {"/setZone", "/rebroadcastlatencymode"}:
            raise self.owner.reject("AIRPLAY_UNPLANNED_WRITE")
        await self.owner.check_expected()
        await self.owner.check_source()
        self.owner.guard_all()
        await self.owner.hold_verified()
        # The acknowledgement is an await boundary: identity, topology and
        # session may have changed while the worker closed its gate.
        await self.owner.check_expected()
        await self.owner.check_source()
        self.owner.guard_all()
        self.owner.attempted = True
        return await self.owner.raw[self.device.device_id].post_xml(path, xml)


class GroupZone:
    def __init__(self, db: Session, devices: Sequence[Device], session: GroupSession, *,
                 ports: ZonePorts, selection: Selection, hold_audio: Callable[[], None],
                 clock: Callable[[], float],
                 hold_acknowledged: Callable[[], Awaitable[object]] | None = None,
                 foreground_progress: Callable[[], None] | None = None) -> None:
        if (set(item.device_id for item in devices) != session.device_ids
                or len(devices) != len(session.device_ids)
                or len({canonical_peer(item.ip_address) for item in devices}) != len(devices)
                or selection.source != "LOCAL_INTERNET_RADIO" or not selection.location):
            raise GroupZoneRejected("AIRPLAY_EXACT_TARGETS_REQUIRED")
        self.db, self.session, self.base_ports = db, session, ports
        self.devices = {item.device_id: item for item in devices}
        self.master = self.devices[session.master]
        self.fingerprints = {key: device_fingerprint(item) for key, item in self.devices.items()}
        self.selection, self.hold_audio, self.clock = selection, hold_audio, clock
        self.hold_acknowledged = hold_acknowledged
        self.foreground_progress = foreground_progress
        self.raw: dict[str, XmlClient] = {}
        self.ports = ZonePorts(client=lambda device: _CheckedClient(self, device), guard=self.guard)
        self.members: tuple[str, ...] = ()
        self.expected: dict[str, Zone] = {}
        self.before: dict[str, dict[str, str]] = {}
        self.latency_written: set[str] = set()
        # One-use readback evidence from this owner's topology write, not
        # permission to overwrite arbitrary later user volume changes.
        self.volume_normalizations: dict[str, int] = {}
        self.job_id, self.backup_ref = f"ap2-zone-{uuid4().hex}", ""
        self.attempted, self.closed, self.failed = False, False, False
        self.operation_plan: GroupPlan | None = session.plan

    def reject(self, code: str) -> GroupZoneRejected:
        return GroupZoneRejected(code, backup_ref=self.backup_ref, attempted=self.attempted)

    def guard(self, db: Session, device: Device) -> None:
        if self.closed or self.failed or device.device_id not in self.devices:
            raise self.reject("AIRPLAY_ZONE_OWNER_CLOSED")
        self.session.require_live(now=self.clock())
        if self.session.plan is not self.operation_plan:
            raise self.reject("AIRPLAY_GROUP_CHANGED_DURING_WRITE")
        db.refresh(device)
        require_unprotected_device(device, action="airplay_zone", requester="airplay_group_owner")
        self.base_ports.guard(db, device)
        if (device_fingerprint(device) != self.fingerprints[device.device_id]
                or not device_lock(device.device_id).owned_by_current_task()):
            raise self.reject("AIRPLAY_ZONE_TARGET_OR_OWNER_CHANGED")
        # Reject unknown stored profiles even before a client is constructed.
        verify_settings_identity(device, device.info_xml)
        if self.foreground_progress is not None:
            self.foreground_progress()

    def guard_all(self) -> None:
        for device in self.devices.values():
            self.guard(self.db, device)

    async def hold_verified(self) -> None:
        if self.hold_acknowledged is None:
            self.hold_audio()  # same-process gate only
        else:
            await self.hold_acknowledged()
        self.guard_all()

    async def check_source(self) -> None:
        self.guard_all()
        client = self.raw[self.master.device_id]
        verify_settings_identity(self.master, await client.get_xml("/info"))
        value = Selection.read(await client.get_xml("/now_playing"), self.master.device_id)
        ending = self.operation_plan is not None and self.operation_plan.status in {'STOPPED', 'HANDOFF_REQUIRED'}
        states = {'PLAY_STATE', 'STOP_STATE', 'PAUSE_STATE'} if ending else {'PLAY_STATE'}
        # Teardown can outlive the HTTP stream. A stopped *owned* ContentItem
        # permits dismantling our verified zone, never selecting another source
        # or treating sparse INVALID_SOURCE/STANDBY as proof of ownership.
        if not value.same_content(self.selection) or value.play_status not in states:
            raise self.reject("AIRPLAY_RELAY_SELECTION_NOT_PLAYING")

    def record(self, phase: str, *, verified: bool = False, detail: object = None) -> None:
        record_action(self.db, job_id=self.job_id, device_id=self.master.device_id,
            ip_address=self.master.ip_address, action="airplay_zone", trigger="explicit_airplay_session",
            phase=phase, backup_ref=self.backup_ref, readback=detail, verified=verified)
        self.db.commit()

    async def prepare(self) -> None:
        self.guard_all()  # all indirect targets, before constructing any transport
        self.raw = {key: self.base_ports.client(device) for key, device in self.devices.items()}
        for identity, device in self.devices.items():
            zone, reads = await read_zone(device, self.ports)
            if not zone.standalone(identity):
                raise self.reject("AIRPLAY_EXISTING_ZONE_NOT_OWNED")
            for path in ("/volume", "/rebroadcastlatencymode"):
                reads[path] = await self.ports.client(device).get_xml(path)
                value = read_setting(path[1:], reads[path])
                if path == "/volume" and (type(value) is not int or not 0 <= value <= 100):
                    raise self.reject("AIRPLAY_VOLUME_UNKNOWN")
            self.before[identity], self.expected[identity] = reads, zone
        await self.check_source()
        content = json.dumps(self.before, sort_keys=True)
        digest = sha256(content.encode()).hexdigest()
        backup = ConfigBackup(device_id=self.master.device_id, path=f"{self.job_id}/before.json", content=content)
        self.db.add(backup)
        self.db.flush()
        identifier = backup.id
        self.backup_ref = f"config_backup:{identifier}"
        self.record("AIRPLAY_ZONE_START", detail={"sha256": digest, "targets": sorted(self.devices)})
        stored = self.db.get(ConfigBackup, identifier, populate_existing=True)
        if stored is None or sha256(stored.content.encode()).hexdigest() != digest:
            raise self.reject("AIRPLAY_ZONE_BACKUP_INTEGRITY")

    async def check_expected(self) -> None:
        self.guard_all()
        for identity, device in self.devices.items():
            actual, _ = await read_zone(device, self.ports)
            if actual != self.expected[identity]:
                raise self.reject("AIRPLAY_ZONE_EXTERNALLY_CHANGED")

    async def remember_layout(self, members: tuple[str, ...]) -> None:
        expected_ids = set(members) | {self.master.device_id}
        observed: dict[str, Zone] = {}
        for identity, device in self.devices.items():
            zone, _ = await read_zone(device, self.ports)
            if identity in expected_ids and members:
                if (zone.master != self.master.device_id or
                        any(key not in expected_ids or (ip and ip != self.devices[key].ip_address)
                            for key, ip in zone.members) or
                        (identity == self.master.device_id and
                         {key for key, _ in zone.members} - {identity} != set(members))):
                    raise self.reject("AIRPLAY_ZONE_LAYOUT_DIVERGED")
            elif not zone.standalone(identity):
                raise self.reject("AIRPLAY_ZONE_LAYOUT_DIVERGED")
            observed[identity] = zone
        self.expected, self.members = observed, members

    async def volumes(self) -> tuple[tuple[str, int], ...]:
        values = []
        for identity, device in self.devices.items():
            value = read_setting("volume", await self.ports.client(device).get_xml("/volume"))
            if type(value) is not int or not 0 <= value <= 100:
                raise self.reject("AIRPLAY_VOLUME_UNKNOWN")
            values.append((identity, value))
        return tuple(sorted(values))

    async def restore_latency(self, identities: Sequence[str]) -> None:
        for identity in identities:
            if identity not in self.latency_written:
                continue
            device = self.devices[identity]
            original = read_setting("rebroadcastlatencymode", self.before[identity]["/rebroadcastlatencymode"])

            def factory(ip_address: str, *, device_id: str, request_purpose: str, trigger: str) -> XmlClient:
                if device_id != identity or ip_address != device.ip_address:
                    raise self.reject("AIRPLAY_RESTORE_TARGET_CHANGED")
                return self.ports.client(device)

            await apply_settings(self.db, device, [plan_setting("rebroadcastlatencymode", original)],
                client_factory=factory, guard=self.guard,
                expected_previous={"rebroadcastlatencymode": "SYNC_TO_ZONE"})
            self.latency_written.remove(identity)

    async def update(self, samples: Mapping[str, ReceiverSample], *, now: float) -> GroupZoneResult:
        """No implicit replay: any exception closes this owner for reconciliation."""
        try:
            self.session.observe(samples, now=now)
            return await self.apply_current()
        except BaseException as exc:
            if not self.failed:
                self.fail(exc)
            raise

    async def apply_current(self) -> GroupZoneResult:
        """Use the live supervisor's current plan without replaying its input.

        Membership remains pinned across awaits. The supervisor may refresh
        unchanged input concurrently, but a different plan aborts this write.
        """
        try:
            self.volume_normalizations.clear()
            created = False
            plan = self.session.plan
            if plan is None:
                raise self.reject("AIRPLAY_GROUP_NOT_OBSERVED")
            self.operation_plan = plan
            self.guard_all()
            await self.check_expected()
            await self.check_source()
            if plan.status == "WAITING":
                raise self.reject("AIRPLAY_MASTER_NOT_SELECTED")
            wanted = plan.members if plan.status == "ACTIVE" else ()
            if wanted != self.members or plan.status != "ACTIVE":
                await self.hold_verified()
                await self.check_source()
                if plan.status != "ACTIVE" and self.members:
                    await change_zone(self.db, self.master, self.expected[self.master.device_id], ports=self.ports)
                    await self.remember_layout(())
                else:
                    for identity in tuple(key for key in self.members if key not in wanted):
                        await change_zone(self.db, self.master, self.expected[self.master.device_id],
                                          ports=self.ports, remove=self.devices[identity])
                        await self.remember_layout(tuple(key for key in self.members if key != identity))
                await self.restore_latency(tuple(key for key in self.latency_written
                    if key not in ({self.master.device_id, *wanted} if wanted else set())))
                if wanted != self.members:
                    selected = [self.master, *(self.devices[key] for key in wanted)]
                    async with creation_scope(self.db, selected, ports=self.ports, requested={
                        "origin": "owned_airplay_group", "master": self.master.device_id,
                        "members": list(wanted), "preserve_volumes": True,
                    }) as creation:
                        for device in selected:
                            current = await creation.read(device.device_id, "/rebroadcastlatencymode")
                            if read_setting("rebroadcastlatencymode", current) != "SYNC_TO_ZONE":
                                await creation.post(device.device_id, "/rebroadcastlatencymode",
                                                    '<rebroadcastlatencymode mode="SYNC_TO_ZONE" />')
                                self.latency_written.add(device.device_id)
                        await creation.post(self.master.device_id, "/setZone", payload(self.master, selected[1:]))
                    created = True
                    await self.remember_layout(wanted)
            actual = await self.volumes()
            changed = tuple(identity for identity, volume in actual if
                volume != read_setting("volume", self.before[identity]["/volume"]))
            result = GroupZoneResult(plan.status, self.master.device_id, self.members,
                                     self.backup_ref, actual, changed)
            self.record("AIRPLAY_ZONE_READBACK", verified=True, detail={
                "status": result.status, "members": list(result.members),
                "volumes": list(actual), "volume_changed": list(changed), "audio_released": False})
            if created:
                self.volume_normalizations = {identity: volume for identity, volume in actual
                                              if identity in changed}
            if plan.status != "ACTIVE":
                self.closed = True
            return result
        except BaseException as exc:
            self.fail(exc)
            raise

    def fail(self, exc: BaseException) -> None:
        # Never turn a failed readback into blind automatic rollback. Invalidate
        # PCM locally and leave the durable START/step journal for reconciliation.
        self.failed = True
        self.hold_audio()
        self.db.rollback()
        try:
            self.record("AIRPLAY_ZONE_RECONCILIATION", detail={"cause": type(exc).__name__,
                "attempted": self.attempted, "automatic_retry": False, "automatic_rollback": False})
        except Exception:
            self.db.rollback()  # START/underlying write intent remains the recovery anchor


@asynccontextmanager
async def group_zone_scope(db: Session, devices: Sequence[Device], session: GroupSession, *,
                           ports: ZonePorts, selection: Selection, hold_audio: Callable[[], None],
                           clock: Callable[[], float] = time.monotonic,
                           hold_acknowledged: Callable[[], Awaitable[object]] | None = None,
                           foreground_progress: Callable[[], None] | None = None,
                           ) -> AsyncIterator[GroupZone]:
    """All targets must be explicitly granted and owned for the whole pilot.

Call update with an observed ended master to dissolve an owned zone and restore
its latency settings. Unexpected exit closes the PCM gate, records uncertainty,
and never attempts writes using expired ownership. Playback/volume restoration
belongs to the outer source owner; this is NOT an unattended full installer.
"""
    owner = GroupZone(db, devices, session, ports=ports, selection=selection,
                      hold_audio=hold_audio, clock=clock, hold_acknowledged=hold_acknowledged,
                      foreground_progress=foreground_progress)
    async with device_transaction(session.device_ids):
        try:
            await owner.prepare()
            yield owner
        except BaseException as exc:
            if not owner.failed:
                owner.fail(exc)
            raise
        finally:
            if not owner.closed and not owner.failed:
                owner.fail(owner.reject("AIRPLAY_ZONE_OWNER_INTERRUPTED"))
            owner.closed = True
            owner.hold_audio()
