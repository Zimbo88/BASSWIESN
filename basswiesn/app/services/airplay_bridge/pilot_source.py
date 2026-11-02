"""Explicit standby-only LAB source owner using existing radio transactions.

This is not unattended AirPlay activation. All participants must be explicitly
chosen, standalone and in standby. The caller provisions a fresh target-bound
relay and maintains its held controller lease during preparation. No firmware,
preset, routing, discovery or receiver installation is performed here.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy.orm import Session

from basswiesn.app.models import ConfigBackup, Device, Station
from basswiesn.app.services.action_journal import record_action
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.device_transactions import device_fingerprint, device_transaction
from basswiesn.app.services.device_settings_writer import XmlClient, apply_settings, verify_settings_identity
from basswiesn.app.services.device_power import execute_power
from basswiesn.app.services.direct_selection import Selection, _root
from basswiesn.app.services.maintenance_reboot import PATHS, RebootSnapshot
from basswiesn.app.services.protected_devices import require_unprotected_device
from basswiesn.app.services.settings_contract import plan_setting
from basswiesn.app.services.settings_contract import read_setting
from basswiesn.app.services.station_playback import play_station
from basswiesn.app.services.zone_changes import ZonePorts, require_selected_scope
from .display_session import DisplayPublisher, is_bridge_station
from .group_zone import GroupZone
from .receiver_port import ReceiverFrame
from .volume import project_volume


class PilotSource:
    def __init__(self, db: Session, devices: Sequence[Device], master: str, station: Station,
                 xml: str, ports: ZonePorts, guard_audio: Callable[[], None], test_volume: int,
                 radio_volume_forwarding: bool = False) -> None:
        ids = {item.device_id for item in devices}
        if (not devices or len(ids) != len(devices) or master not in ids
                or type(test_volume) is not int or not 1 <= test_volume <= 10 or not is_bridge_station(station)
                or type(radio_volume_forwarding) is not bool):
            raise ValueError('AIRPLAY_EXPLICIT_PILOT_REQUIRED')
        self.db, self.devices, self.ports = db, {item.device_id:item for item in devices}, ports
        self.master, self.station, self.xml = self.devices[master], station, xml
        content = _root(xml, 'ContentItem')
        self.selection = Selection.content(content)
        if (self.selection.source != 'LOCAL_INTERNET_RADIO' or not self.selection.location
                or self.selection.source_account or content.get('type') != 'stationurl'):
            raise ValueError('AIRPLAY_RELAY_SOURCE_REQUIRED')
        self.guard_audio, self.volume = guard_audio, test_volume
        self.frozen = {key:device_fingerprint(item) for key,item in self.devices.items()}
        self.before: dict[str, RebootSnapshot] = {}
        self.clients: dict[str, XmlClient] = {}
        self.job = 'ap2-source-' + uuid4().hex
        self.backup_id: int | None = None
        self.digest = ''
        self.attempted = False
        self.restore_result = 'NOT_STARTED'
        self.display: DisplayPublisher | None = None
        self.preparing = True
        self.radio_volume_forwarding = radio_volume_forwarding
        self.expected_volume = dict.fromkeys(ids, test_volume)

    def guard(self, db: Session, device: Device) -> None:
        if self.preparing:
            self.guard_audio()
        db.refresh(device)
        require_unprotected_device(device, action='airplay_pilot', requester='explicit_airplay_owner')
        self.ports.guard(db, device)
        if (device.device_id not in self.frozen or device_fingerprint(device) != self.frozen[device.device_id]
                or not device_lock(device.device_id).owned_by_current_task()):
            raise ValueError('AIRPLAY_PILOT_TARGET_CHANGED')
        verify_settings_identity(device, device.info_xml)

    async def read(self, device: Device) -> RebootSnapshot:
        self.guard(self.db, device)
        client = self.clients[device.device_id]
        info = await client.get_xml('/info')
        verify_settings_identity(device, info)
        raw = {'/info':info}
        for path in PATHS:
            raw[path] = await client.get_xml(path)
        self.guard(self.db, device)
        observed = RebootSnapshot.parse(raw, device.device_id)
        require_selected_scope(observed.zone, {key:item.ip_address for key,item in self.devices.items()})
        return observed

    def integrity(self) -> None:
        stored = self.db.get(ConfigBackup, self.backup_id, populate_existing=True)
        if stored is None or sha256(stored.content.encode()).hexdigest() != self.digest:
            raise ValueError('AIRPLAY_PILOT_BACKUP_CHANGED')

    def record(self, phase: str, *, verified: bool = False, detail: object = None) -> None:
        record_action(self.db, job_id=self.job, device_id=self.master.device_id,
            ip_address=self.master.ip_address, action='airplay_pilot', trigger='explicit_airplay_test',
            phase=phase, backup_ref=f'config_backup:{self.backup_id}', readback=detail, verified=verified)
        self.db.commit()

    async def volume_write(self, device: Device, value: int, previous: int) -> None:
        def factory(ip_address: str, *, device_id: str, request_purpose: str, trigger: str) -> XmlClient:
            if ip_address != device.ip_address or device_id != device.device_id:
                raise ValueError('AIRPLAY_PILOT_RETARGETED')
            return self.clients[device_id]
        self.integrity()
        await apply_settings(self.db, device, [plan_setting('volume', value)], client_factory=factory,
                             guard=self.guard, expected_previous={'volume': previous})

    async def prepare(self) -> None:
        # Every target is checked before the first transport is constructed.
        for device in self.devices.values():
            self.guard(self.db, device)
        self.clients = {key:self.ports.client(device) for key,device in self.devices.items()}
        for key, device in self.devices.items():
            before = await self.read(device)
            if (before.selection.source != 'STANDBY' or before.volume[1]
                    or not before.zone.standalone(key)):
                raise ValueError('AIRPLAY_PILOT_REQUIRES_STANDALONE_UNMUTED_STANDBY')
            self.before[key] = before
        content = json.dumps({key:value.raw for key,value in self.before.items()}, sort_keys=True)
        backup = ConfigBackup(device_id=self.master.device_id, path=self.job+'/before.json', content=content)
        self.db.add(backup)
        self.db.flush()
        self.backup_id, self.digest = backup.id, sha256(content.encode()).hexdigest()
        self.record('AIRPLAY_SOURCE_START', detail={'sha256':self.digest, 'test_volume':self.volume})
        self.integrity()
        for key, device in self.devices.items():
            if not (await self.read(device)).same_state(self.before[key]):
                raise ValueError('AIRPLAY_PILOT_PREVIOUS_STATE_CHANGED')
        self.guard_audio()  # Caller must have an acknowledged held gate.
        # Keep the shared station-start safety policy (maximum five) intact.
        # A louder explicitly requested hearing level is applied only after
        # source readiness, while this pilot's PCM gate is still held.
        startup_volume = min(self.volume, 5)
        for key, device in self.devices.items():
            self.attempted = True
            await self.volume_write(device, startup_volume, self.before[key].volume[0])
        self.guard_audio()
        # Selection affects only the verified standalone master. Both radios
        # remain locked/backed up, but reading the idle peer on every guarded
        # master step can exhaust the provider's post-select safety deadline.
        result = await play_station(self.db, self.master, self.station, self.xml,
            client_factory=lambda device:self.clients[device.device_id], guard=self.guard,
            safe_volume=startup_volume, participants=[self.master],
            trigger='explicit_airplay_test', internal_event=True)
        if result.get('verified') is not True and not (
                result.get('status') == 'PENDING_READBACK' and result.get('selection_verified') is True):
            raise ValueError('AIRPLAY_PILOT_SELECTION_UNVERIFIED')
        # Growing live MP3 can buffer after the one accepted select. GET only;
        # never issue another select to turn a pending response into success.
        for _ in range(30):
            self.guard_audio()
            actual = await self.read(self.master)
            if (not actual.selection.same_content(self.selection) or actual.volume != (startup_volume, False)
                    or not actual.zone.standalone(self.master.device_id)):
                raise ValueError('AIRPLAY_PILOT_STARTUP_DIVERGED')
            if actual.selection.play_status == 'PLAY_STATE':
                # Outside the provider-critical interval, recheck the untouched
                # peers before authorizing group preparation. Never adopt a
                # newer source/preset/volume as this pilot's baseline.
                for key, device in self.devices.items():
                    if key == self.master.device_id:
                        continue
                    peer = await self.read(device)
                    before = self.before[key]
                    if (peer.selection != before.selection or peer.presets != before.presets
                            or peer.volume != (startup_volume, False) or not peer.zone.standalone(key)):
                        raise ValueError('AIRPLAY_PILOT_PEER_CHANGED_DURING_SELECTION')
                if self.volume != startup_volume:
                    for key, device in self.devices.items():
                        await self.volume_write(device, self.volume, startup_volume)
                        checked = await self.read(device)
                        if (checked.volume != (self.volume, False) or not checked.zone.standalone(key)
                                or checked.presets != self.before[key].presets):
                            raise ValueError('AIRPLAY_PILOT_HEARING_VOLUME_UNVERIFIED')
                self.record('AIRPLAY_SOURCE_READBACK', verified=True)
                return
            if actual.selection.play_status != 'BUFFERING_STATE':
                raise ValueError('AIRPLAY_PILOT_SOURCE_NOT_BUFFERING')
            await asyncio.sleep(.5)
        raise TimeoutError('AIRPLAY_PILOT_STARTUP_TIMEOUT')

    async def validate_output(self, zone: GroupZone,
                              frames: Callable[[], Mapping[str, ReceiverFrame]]) -> None:
        self.integrity()
        for key, device in self.devices.items():
            observed = await self.read(device)
            if observed.presets != self.before[key].presets or observed.volume[1]:
                raise ValueError('AIRPLAY_PILOT_OUTPUT_VOLUME_OR_PRESETS_CHANGED')
            expected = self.expected_volume[key]
            if observed.volume[0] != expected:
                # A Bose zone join can restore a member's previous group
                # volume. Correct only the exact fresh topology-write readback,
                # while PCM remains held; unrelated volume changes still abort.
                if (zone.volume_normalizations.get(key) != observed.volume[0]
                        or read_setting('volume', zone.before[key]['/volume']) != self.volume):
                    raise ValueError('AIRPLAY_PILOT_OUTPUT_VOLUME_OR_PRESETS_CHANGED')
                await zone.hold_verified()
                await zone.check_expected()
                await zone.check_source()
                await self.volume_write(device, expected, observed.volume[0])
                corrected = await self.read(device)
                if corrected.presets != self.before[key].presets or corrected.volume != (expected, False):
                    raise ValueError('AIRPLAY_PILOT_NORMALIZED_VOLUME_UNVERIFIED')
                await zone.check_expected()
                await zone.check_source()
            zone.volume_normalizations.pop(key, None)
            # A complete verified radio snapshot is foreground progress, not
            # a heartbeat. Recheck the owned live zone before the next device.
            zone.guard_all()
        await zone.check_source()
        if self.radio_volume_forwarding:
            await self.forward_volume(zone, frames)
        # Metadata is a projection after actual-device verification, not proof
        # of audio/display rendering. Existing cloud workers need this consumer.
        # Consume the latest monitor observation only after all awaited radio
        # reads. The supplier rechecks controller liveness and native plan;
        # DisplayPublisher still rejects any observation five seconds old.
        frame = frames()[self.master.device_id]
        if self.display is None:
            self.display = DisplayPublisher.begin(self.db, self.master.device_id, self.station.id,
                device_lock(self.master.device_id), receiver_session=str(frame.display.get('session', '')))
        self.display = self.display.accept_after_verified_readback(self.db, frame.display)

    async def forward_volume(self, zone: GroupZone,
                             frames: Callable[[], Mapping[str, ReceiverFrame]]) -> None:
        # The master PCM must be undamped before independent member levels can
        # be controlled. Input-only members have no output PCM to authorize.
        observed = frames()
        if observed[self.master.device_id].volume_mode != 'radio':
            raise ValueError('AIRPLAY_RADIO_VOLUME_MODE_UNVERIFIED')
        for key in (self.master.device_id, *zone.members):
            frame = frames()[key]
            if frame.volume is None:
                raise ValueError('AIRPLAY_RADIO_VOLUME_MISSING')
            intent = project_volume(frame.volume['airplay_db'], maximum=self.volume)
            previous = self.expected_volume[key]
            if intent.applied == previous:
                continue
            await zone.check_expected()
            await zone.check_source()
            # Re-read the latest intent after awaited guards; never consume a
            # different native group or an old target selected by the browser.
            latest = frames()[key].volume
            if latest is None:
                raise ValueError('AIRPLAY_RADIO_VOLUME_MISSING')
            intent = project_volume(latest['airplay_db'], maximum=self.volume)
            device = self.devices[key]
            # A bounded per-radio level change must not replace the shared
            # PCM with silence for every member. Startup/topology changes
            # remain held. Here both the previously verified and requested
            # values must stay within the explicit session ceiling; the
            # central transaction still validates identity/expected-previous
            # and reads back. Any uncertain outcome closes the shared gate.
            try:
                if not 0 <= previous <= self.volume or not 0 <= intent.applied <= self.volume:
                    raise ValueError('AIRPLAY_RADIO_VOLUME_BOUND')
                await self.volume_write(device, intent.applied, previous)
                actual = await self.read(device)
                if actual.volume != (intent.applied, False) or actual.presets != self.before[key].presets:
                    raise ValueError('AIRPLAY_RADIO_VOLUME_READBACK_MISMATCH')
                self.expected_volume[key] = intent.applied
                await zone.check_expected()
                await zone.check_source()
            except BaseException:
                await zone.hold_verified()
                raise
            self.record('AIRPLAY_VOLUME_READBACK', verified=True, detail={
                'target':key, 'applied':intent.applied, 'limited':intent.limited})

    async def restore(self) -> None:
        # Cleanup may follow owner/receiver loss; it relies on captured actual
        # state, not a dead audio lease, and must never replay source selection.
        self.preparing = False
        if self.display is not None:
            self.display.end(self.db)
        if not self.attempted:
            self.restore_result = 'NO_RADIO_WRITE_ATTEMPTED'
            return
        self.integrity()
        for key, device in self.devices.items():
            value = await self.read(device)
            before = self.before[key]
            if self.restored(key, value):
                continue
            if (not value.zone.standalone(key) or value.presets != before.presets
                    or value.volume != (self.expected_volume[key], False)
                    or (value.selection.source != 'STANDBY'
                        and not value.selection.same_content(self.selection))):
                raise ValueError('AIRPLAY_PILOT_RESTORE_NEEDS_RECONCILIATION')
            if value.selection.source != 'STANDBY':
                def factory(ip_address: str, *, device_id: str, request_purpose: str, trigger: str) -> XmlClient:
                    if device_id != key or ip_address != device.ip_address:
                        raise ValueError('AIRPLAY_PILOT_RETARGETED')
                    return self.clients[key]
                result = await execute_power(self.db, device, 'standby', client_factory=factory, guard=self.guard)
                if result.get('verified') is not True:
                    raise ValueError('AIRPLAY_PILOT_STANDBY_NOT_VERIFIED')
            # Restore a comfortable volume only after actual STANDBY readback.
            if (await self.read(device)).selection.source != 'STANDBY':
                raise ValueError('AIRPLAY_PILOT_RESTORE_NOT_STANDBY')
            await self.volume_write(device, before.volume[0], self.expected_volume[key])
            if not self.restored(key, await self.read(device)):
                raise ValueError('AIRPLAY_PILOT_RESTORE_MISMATCH')
        self.restore_result = 'CAPTURED_STATE_RESTORED'
        self.record('AIRPLAY_SOURCE_RESTORED', verified=True)

    def restored(self, identity: str, value: RebootSnapshot) -> bool:
        before = self.before[identity]
        # A dissolved zone can retain the radio's own coordinator entry. That
        # is standalone, not a failed rollback; peer IDs/addresses are checked
        # by read(). Never normalize an actual multi-device topology away.
        return (value.selection == before.selection and value.volume == before.volume
                and value.presets == before.presets and value.zone.standalone(identity)
                and before.zone.standalone(identity))


@asynccontextmanager
async def pilot_source_scope(db: Session, devices: Sequence[Device], *, master: str, station: Station,
                             xml: str, ports: ZonePorts, guard_audio: Callable[[], None],
                             test_volume: int = 1, radio_volume_forwarding: bool = False) -> AsyncIterator[PilotSource]:
    owner = PilotSource(db, devices, master, station, xml, ports, guard_audio, test_volume, radio_volume_forwarding)
    async with device_transaction(owner.devices):
        try:
            await owner.prepare()
            owner.preparing = False
            yield owner
        finally:
            try:
                await owner.restore()
            except BaseException as exc:
                owner.restore_result = 'MANUAL_REQUIRED'
                db.rollback()
                owner.record('AIRPLAY_SOURCE_RECONCILIATION', detail={'cause':type(exc).__name__,
                    'automatic_retry':False, 'restore':owner.restore_result})
                raise
