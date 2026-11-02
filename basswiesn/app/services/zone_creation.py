"""Durable preparation and verified writes for the existing zone-create flow.

This adds a transaction boundary around the established orchestration rather
than replacing its playback or volume policy. Shared ownership is acquired by
the caller. Radio-side atomicity and locking out physical buttons are impossible.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
from typing import Any, cast
from uuid import uuid4
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeXML  # type: ignore[import-untyped]
import httpx
from sqlalchemy.orm import Session

from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.models import ConfigBackup, Device
from basswiesn.app.services.action_journal import record_action
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.device_transactions import device_fingerprint
from basswiesn.app.services.device_settings_writer import IdentityMismatch, XmlClient, verify_settings_identity
from basswiesn.app.services.settings_contract import read_setting
from basswiesn.app.services.zone_changes import Zone, ZoneChangeError, ZonePorts, require_selected_scope

READS = ('/info', '/getZone', '/volume', '/rebroadcastlatencymode')
_active: ContextVar[Creation | None] = ContextVar('zone_creation', default=None)


class ZoneCreationError(Exception):
    def __init__(self, code: str, *, attempted: bool = False, backup_ref: str = '',
                 job_id: str = '', cause: str = '') -> None:
        self.detail: dict[str, Any] = {
            'error': code, 'status': 'DIVERGED' if attempted else 'BLOCKED',
            'reconciliation_required': attempted, 'automatic_retry': False,
            'automatic_rollback': False, 'backup_ref': backup_ref, 'job_id': job_id,
            'cause': cause,
        }
        super().__init__(code)


def _root(xml: str) -> ET.Element:
    return cast(ET.Element, SafeXML.fromstring(xml, forbid_dtd=True))


def _value(path: str, xml: str, device: Device) -> object:
    root = _root(xml)
    claimed = root.get('deviceID')
    if claimed is not None and claimed.strip().upper() != device.device_id.upper():
        raise IdentityMismatch('response identity differs')
    if path == '/info':
        return verify_settings_identity(device, xml)
    if path == '/getZone':
        return Zone.parse(xml)
    value = read_setting(path[1:], xml)
    if path == '/volume' and (type(value) is not int or not 0 <= value <= 100):
        raise ValueError('invalid actual volume')
    return value


def read_zone_value(path: str, xml: str, device: Device) -> object:
    """Shared state checks for an explicitly owned AP2 zone's readback ports.

    Retain the existing exact hardware and XML authority; do not create an
    alternate AP2 interpretation of identity, volume, latency or topology.
    """
    if path not in READS:
        raise ValueError("unplanned zone read")
    if path == '/volume' and len(_root(xml).findall('actualvolume')) != 1:
        raise ValueError("ambiguous actual volume")
    return _value(path, xml, device)


class Creation:
    def __init__(self, db: Session, devices: Sequence[Device], ports: ZonePorts,
                 requested: dict[str, Any]) -> None:
        self.db, self.ports = db, ports
        self.devices = {device.device_id: device for device in devices}
        self.master = devices[0]
        self.targets = {device.device_id: device.ip_address for device in devices}
        self.fingerprints = {device.device_id: device_fingerprint(device) for device in devices}
        self.clients: dict[str, XmlClient] = {}
        self.before: dict[str, dict[str, str]] = {}
        self.requested = requested
        self.job_id, self.backup_ref = f'zone-create-{uuid4().hex}', ''
        self.attempted = False
        self.verification: list[dict[str, Any]] = []
        self.observations: dict[str, dict[str, str]] = {}

    def fail(self, code: str, cause: BaseException | None = None) -> ZoneCreationError:
        error = ZoneCreationError(code, attempted=self.attempted, backup_ref=self.backup_ref,
                                  job_id=self.job_id, cause=type(cause).__name__ if cause else '')
        error.detail['verification'] = self.verification
        return error

    def guard(self, identity: str) -> Device:
        device = self.devices[identity]
        self.ports.guard(self.db, device)
        if device_fingerprint(device) != self.fingerprints[identity]:
            raise self.fail('ZONE_CREATION_TARGET_CHANGED')
        if not device_lock(identity).owned_by_current_task():
            raise self.fail('ZONE_CREATION_OWNERSHIP_REQUIRED')
        return device

    async def read(self, identity: str, path: str) -> str:
        device = self.guard(identity)
        info = await self.clients[identity].get_xml('/info')
        _value('/info', info, device)
        if path == '/info':
            return info
        result = await self.clients[identity].get_xml(path)
        if path in READS:
            _value(path, result, device)
            self.observations.setdefault(identity, {})[path] = result
        return result

    async def prepare(self) -> None:
        for identity, device in self.devices.items():
            self.guard(identity)
            self.clients[identity] = self.ports.client(device)
        for identity, device in self.devices.items():
            state = {path: await self.read(identity, path) for path in READS}
            require_selected_scope(Zone.parse(state['/getZone']), self.targets)
            self.before[identity] = state
        content = json.dumps({'devices': self.before}, sort_keys=True)
        digest = sha256(content.encode()).hexdigest()
        backup = ConfigBackup(device_id=self.master.device_id, path=f'{self.job_id}/before.json', content=content)
        self.db.add(backup)
        self.db.flush()
        identifier = backup.id
        self.backup_ref = f'config_backup:{identifier}'
        record_action(self.db, job_id=self.job_id, device_id=self.master.device_id,
                      ip_address=self.master.ip_address, action='multiroom_create', trigger='webui',
                      phase='ZONE_CREATION_INTENT', backup_ref=self.backup_ref,
                      before_state={'sha256': digest}, requested_state=self.requested)
        self.db.commit()
        stored = self.db.get(ConfigBackup, identifier, populate_existing=True)
        if stored is None or sha256(stored.content.encode()).hexdigest() != digest:
            raise self.fail('ZONE_CREATION_BACKUP_INTEGRITY')
        # Recheck all participants after durable persistence, before any POST.
        # This narrows the external-controller race; it cannot make it atomic.
        for identity, device in self.devices.items():
            for path in READS:
                current = await self.read(identity, path)
                if _value(path, current, device) != _value(path, self.before[identity][path], device):
                    raise self.fail('ZONE_CREATION_PRECONDITION_CHANGED')

    async def zone_matches(self, expected: Zone) -> bool:
        if expected.master != self.master.device_id.upper():
            raise self.fail('ZONE_CREATION_COORDINATOR_CHANGED')
        desired = {identity.upper() for identity in self.devices} - {expected.master}
        if {identity for identity, _ in expected.members} != desired:
            raise self.fail('ZONE_CREATION_TARGET_SET_CHANGED')
        all_ok = True
        self.verification = []
        for identity in self.devices:
            actual = Zone.parse(await self.read(identity, '/getZone'))
            ok = actual.master == expected.master
            try:
                require_selected_scope(actual, self.targets)
            except (ValueError, ZoneChangeError):
                ok = False
            if identity.upper() == expected.master:
                ok = ok and {item for item, _ in actual.members} - {expected.master} == desired
            self.verification.append({'device_id': identity, 'zone_ok': ok, 'ok': ok,
                                      'zone': actual.summary()})
            all_ok = all_ok and ok
        return all_ok

    async def post(self, identity: str, path: str, xml: str) -> str:
        device = self.guard(identity)
        request = _root(xml)
        if path == '/setZone':
            expected: object = Zone.parse(xml)
            require_selected_scope(cast(Zone, expected), self.targets)
            if identity != self.master.device_id:
                raise self.fail('ZONE_CREATION_WRONG_COORDINATOR')
            wanted = {key.upper() for key in self.devices} - {self.master.device_id.upper()}
            if (cast(Zone, expected).master != self.master.device_id.upper()
                    or {key for key, _ in cast(Zone, expected).members} != wanted):
                raise self.fail('ZONE_CREATION_TARGET_SET_CHANGED')
        elif path == '/volume' and request.tag == 'volume':
            expected = int(request.text or '')
            if not 0 <= expected <= 100:
                raise self.fail('ZONE_CREATION_INVALID_VOLUME')
        elif path == '/rebroadcastlatencymode':
            expected = _value(path, xml, device)
        else:
            raise self.fail('ZONE_CREATION_UNPLANNED_WRITE')
        # Identity and selected topology are checked immediately before every
        # step, including volume/latency writes that precede group creation.
        for participant in self.devices:
            current = Zone.parse(await self.read(participant, '/getZone'))
            require_selected_scope(current, self.targets)
        record_action(self.db, job_id=self.job_id, device_id=identity,
                      ip_address=device.ip_address, action='multiroom_create', trigger='webui',
                      phase='ZONE_CREATION_WRITE_INTENT', backup_ref=self.backup_ref,
                      requested_state={'path': path, 'xml': xml})
        self.db.commit()
        for participant in self.devices:
            self.guard(participant)
        await self.read(identity, '/info')
        self.attempted = True
        response, error, rejected = '', '', False
        try:
            response = await self.clients[identity].post_xml(path, xml)
        except (httpx.TransportError, OSError) as exc:
            error = type(exc).__name__
        except Exception as exc:
            error, rejected = type(exc).__name__, True
        matched = False
        read_path = '/getZone' if path == '/setZone' else path
        for attempt in range(4):
            try:
                matched = (await self.zone_matches(cast(Zone, expected)) if path == '/setZone'
                           else _value(path, await self.read(identity, read_path), device) == expected)
            except (httpx.TransportError, OSError):
                matched = False  # Only GETs are retried, never the POST.
            if matched:
                break
            if attempt < 3:
                await asyncio.sleep(0.2)
        if not matched or rejected:
            raise self.fail('ZONE_CREATION_READBACK_MISMATCH')
        record_action(self.db, job_id=self.job_id, device_id=identity,
                      ip_address=device.ip_address, action='multiroom_create', trigger='webui',
                      phase='ZONE_CREATION_STEP_VERIFIED', backup_ref=self.backup_ref,
                      requested_state={'path': path}, error_category=error, verified=True)
        self.db.commit()
        return response

    def record_failure(self, exc: BaseException) -> None:
        self.db.rollback()
        try:
            record_action(self.db, job_id=self.job_id, device_id=self.master.device_id,
                          ip_address=self.master.ip_address, action='multiroom_create', trigger='webui',
                          phase='ZONE_CREATION_DIVERGED' if self.attempted else 'ZONE_CREATION_BLOCKED',
                          backup_ref=self.backup_ref, error_category=type(exc).__name__,
                          readback=self.observations, verified=False)
            self.db.commit()
        except Exception:
            self.db.rollback()
            write_masterlog('zone_creation_journal_failed', job_id=self.job_id,
                            attempted=self.attempted, automatic_retry=False)


class CheckedClient:
    def __init__(self, creation: Creation, identity: str) -> None:
        self.creation, self.identity = creation, identity

    async def get_xml(self, path: str) -> str:
        return await self.creation.read(self.identity, path)

    async def post_xml(self, path: str, xml: str) -> str:
        return await self.creation.post(self.identity, path, xml)


def creation_client(device: Device, client: XmlClient) -> XmlClient:
    creation = _active.get()
    if creation is None:
        return client
    if device.device_id not in creation.devices:
        raise creation.fail('ZONE_CREATION_UNSELECTED_TARGET')
    return CheckedClient(creation, device.device_id)


@asynccontextmanager
async def creation_scope(db: Session, devices: Sequence[Device], *, ports: ZonePorts,
                         requested: dict[str, Any]) -> AsyncIterator[Creation]:
    # Keep existing policy exceptions intact and run them before constructing
    # any client. The context is entered only after shared ownership is held.
    for device in devices:
        ports.guard(db, device)
    if not devices or len({device.device_id for device in devices}) != len(devices) or _active.get():
        raise ZoneCreationError('ZONE_CREATION_INVALID_SCOPE')
    creation = Creation(db, devices, ports, requested)
    try:
        await creation.prepare()
    except Exception as exc:
        creation.record_failure(exc)
        if isinstance(exc, ZoneCreationError):
            raise
        raise creation.fail('ZONE_CREATION_PREFLIGHT_FAILED', exc) from None
    token = _active.set(creation)
    try:
        yield creation
    except BaseException as exc:
        creation.record_failure(exc)
        if not isinstance(exc, Exception) or isinstance(exc, ZoneCreationError):
            raise
        raise creation.fail('ZONE_CREATION_FAILED', exc) from None
    finally:
        _active.reset(token)
