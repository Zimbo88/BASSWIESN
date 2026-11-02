"""One explicit, bounded LAB owner and provider; never application startup.

The private launcher supplies pinned receivers and an already provisioned relay.
This process does not discover, install, migrate, start collectors or use Docker.
Native paired selection remains the only route into source preparation.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import stat
import time
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4
from xml.etree import ElementTree as ET

from sqlalchemy import Engine, create_engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
import uvicorn

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.config import get_settings
from basswiesn.app.models import (
    ConfigBackup, Device, DeviceActionJournal, DeviceInteraction, PlayHistory,
    Preset, RequestLog, RuntimeState, Setting, Station,
)
from basswiesn.app.routers.shared import enforce_ip_write_guard
from basswiesn.app.services.device_settings_writer import verify_settings_identity
from basswiesn.app.services.device_transactions import device_fingerprint, device_transaction
from basswiesn.app.services.zone_changes import ZonePorts
from basswiesn.app.services.zone_creation import read_zone_value
from .control_owner import AudioControl
from .group_controller import GroupController, OutputOwner
from .group_session import GroupSession
from .group_zone import GroupZoneResult
from .pilot_provider import PilotProvider
from .pilot_source import PilotSource, pilot_source_scope
from .receiver_port import ReceiverPort
from .unix_ipc import UnixReceiverIPC


def safe_error(exc: BaseException) -> dict[str, object]:
    """Fixed codes only: never SQL, transport URLs, exception text or frames."""
    def describe(error: BaseException) -> dict[str, object]:
        result: dict[str, object] = {'error_type': type(error).__name__}
        if re.fullmatch(r'AIRPLAY_[A-Z0-9_]+', str(error)):
            result['error_code'] = str(error)
        if isinstance(error, OSError) and type(error.errno) is int:
            result['error_errno'] = error.errno
        if isinstance(error, sqlite3.Error):
            name = getattr(error, 'sqlite_errorname', None)
            code = getattr(error, 'sqlite_errorcode', None)
            if isinstance(name, str) and re.fullmatch(r'SQLITE_[A-Z0-9_]+', name):
                result['sqlite_error_name'] = name
            if type(code) is int:
                result['sqlite_error_code'] = code
        return result

    result = describe(exc)
    current: BaseException | None = exc
    seen: set[int] = set()
    chain: list[dict[str, object]] = []
    while current is not None and id(current) not in seen and len(chain) < 4:
        seen.add(id(current))
        chain.append(describe(current))
        current = (current.orig if isinstance(current, DBAPIError)
                   else current.__cause__ or current.__context__)
    if len(chain) > 1:
        result['error_chain'] = chain
    return result


@dataclass(frozen=True)
class PilotDevice:
    device_id: str
    ip_address: str
    fingerprint: tuple[str, ...]
    receiver_socket: Path


@dataclass(frozen=True, repr=False)
class PilotConfig:
    data_dir: Path
    database_path: Path
    devices: tuple[PilotDevice, ...]
    master: str
    receiver_binary_sha256: str
    controller_nonce: str
    relay_url: str
    base_url: str
    listen_host: str
    listen_port: int
    lifetime_seconds: float
    wait_seconds: float
    test_volume: int
    ipc_peer_uid: int
    status_path: Path
    receipt_path: Path
    stop_path: Path
    radio_volume_forwarding: bool = False

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> PilotConfig:
        fields = set(cls.__dataclass_fields__)
        if (set(raw) not in (fields | {'version'}, (fields - {'radio_volume_forwarding'}) | {'version'})
                or type(raw['version']) is not int or raw['version'] != 1):
            raise ValueError('AIRPLAY_PILOT_CONFIG_SCHEMA')
        values = {key: raw[key] for key in fields if key in raw}
        for key in ('data_dir', 'database_path', 'status_path', 'receipt_path', 'stop_path'):
            values[key] = Path(values[key])
        devices = []
        for item in values['devices']:
            if (set(item) != {'device_id', 'ip_address', 'fingerprint', 'receiver_socket'}
                    or not isinstance(item['fingerprint'], list)
                    or len(item['fingerprint']) != 5
                    or not all(isinstance(value, str) for value in item['fingerprint'])):
                raise ValueError('AIRPLAY_PILOT_DEVICE_SCHEMA')
            devices.append(PilotDevice(item['device_id'], item['ip_address'],
                tuple(item['fingerprint']), Path(item['receiver_socket'])))
        values['devices'] = tuple(devices)
        config = cls(**values)
        config.validate()
        return config

    def validate(self) -> None:
        ids = {item.device_id for item in self.devices}
        paths = (self.data_dir, self.database_path, self.status_path, self.receipt_path,
                 self.stop_path, *(item.receiver_socket for item in self.devices))
        count = len(self.devices)
        if (not 1 <= count <= 16 or len(ids) != count or self.master not in ids
                or len({item.ip_address for item in self.devices}) != count
                or len({item.receiver_socket for item in self.devices}) != count
                or any(not path.is_absolute() or '..' in path.parts for path in paths)
                or not self.database_path.is_relative_to(self.data_dir)
                or len({self.status_path, self.receipt_path, self.stop_path}) != 3
                or any(path == self.database_path for path in paths[2:])
                or type(self.test_volume) is not int or not 1 <= self.test_volume <= 10
                or type(self.radio_volume_forwarding) is not bool
                or type(self.listen_port) is not int or not 1024 <= self.listen_port <= 65535
                or type(self.ipc_peer_uid) is not int or self.ipc_peer_uid < 0
                or type(self.lifetime_seconds) not in (int, float) or not 1 <= self.lifetime_seconds <= 600
                or type(self.wait_seconds) not in (int, float) or not 0 < self.wait_seconds <= 300
                or not re.fullmatch('[a-f0-9]{64}', self.receiver_binary_sha256)
                or not re.fullmatch('[a-f0-9]{32}', self.controller_nonce)):
            raise ValueError('AIRPLAY_EXPLICIT_BOUNDED_PAIR_REQUIRED')
        for item in self.devices:
            if (not item.device_id or item.device_id != item.device_id.strip().upper()
                    or len(item.device_id) > 128 or item.fingerprint[:2] != (item.device_id, item.ip_address)):
                raise ValueError('AIRPLAY_EXACT_DEVICE_FINGERPRINT_REQUIRED')
            address = ipaddress.IPv4Address(item.ip_address)
            if not address.is_private or address.is_loopback or address.is_link_local:
                raise ValueError('AIRPLAY_PRIVATE_TARGET_REQUIRED')
        ipaddress.IPv4Address(self.listen_host)
        for url in (self.base_url, self.relay_url):
            parsed = urlsplit(url)
            address = ipaddress.IPv4Address(parsed.hostname or '')
            if (parsed.scheme != 'http' or not parsed.port or not address.is_private
                    or address.is_loopback or parsed.username or parsed.password or parsed.fragment):
                raise ValueError('AIRPLAY_EXPLICIT_PRIVATE_URL_REQUIRED')
        base, relay = urlsplit(self.base_url), urlsplit(self.relay_url)
        if base.path or base.query or not relay.path or relay.query:
            raise ValueError('AIRPLAY_EXPLICIT_PRIVATE_URL_REQUIRED')


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700 or path.resolve() != path):
        raise ValueError('AIRPLAY_PRIVATE_DIRECTORY_REQUIRED')


def _state_file(path: Path, value: Mapping[str, object], *, exclusive: bool = False) -> None:
    _private_directory(path.parent)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (exclusive or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('AIRPLAY_FRESH_PRIVATE_STATE_REQUIRED')
    temporary = path.with_name('.' + path.name + '-' + uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_config(path: Path) -> PilotConfig:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o027 or info.st_size > 131072 or info.st_nlink != 1):
            raise ValueError('AIRPLAY_PRIVATE_CONFIG_REQUIRED')
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError('AIRPLAY_DUPLICATE_CONFIG_FIELD')
                result[key] = value
            return result
        return PilotConfig.parse(json.load(stream, object_pairs_hook=pairs))


def database(config: PilotConfig, *, readonly: bool) -> Engine:
    config.validate()
    if (os.geteuid() == 0 or not config.database_path.is_file()
            or config.database_path.is_symlink()
            or get_settings().data_dir.resolve() != config.data_dir):
        raise ValueError('AIRPLAY_NONROOT_SHARED_DATA_REQUIRED')
    _private_directory(config.data_dir / 'device-locks')
    for path in (config.status_path, config.receipt_path, config.stop_path):
        _private_directory(path.parent)
    mode = 'ro' if readonly else 'rw'
    return create_engine(f'sqlite+pysqlite:///file:{quote(str(config.database_path))}?mode={mode}&uri=true',
                         connect_args={'check_same_thread': False})


def preflight(db: Session, config: PilotConfig) -> list[Device]:
    """Read-only compatibility and exact DB targeting before any IPC/transport."""
    inspector = inspect(db.get_bind())
    for model in (Device, Station, Preset, Setting, ConfigBackup, DeviceActionJournal,
                  DeviceInteraction, RuntimeState, PlayHistory, RequestLog):
        columns = {item['name'] for item in inspector.get_columns(model.__tablename__)}
        if not set(model.__table__.columns.keys()) <= columns:
            raise ValueError('AIRPLAY_DATABASE_SCHEMA_MISMATCH')
    selected = []
    for target in config.devices:
        row = db.query(Device).filter_by(device_id=target.device_id).one_or_none()
        if row is None or device_fingerprint(row) != target.fingerprint:
            raise ValueError('AIRPLAY_DATABASE_TARGET_MISMATCH')
        enforce_ip_write_guard(db, row)
        verify_settings_identity(row, row.info_xml)
        selected.append(row)
    return selected


def _station_snapshot(station: Station) -> tuple[object, ...]:
    return tuple(getattr(station, key) for key in Station.__table__.columns.keys())


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield  # Only the runner owns process signals, including during restore.

    def install_signal_handlers(self) -> None:
        pass  # Compatibility with the production uvicorn API.


class ProviderServer:
    def __init__(self, provider: PilotProvider, config: PilotConfig) -> None:
        self.server = _Server(uvicorn.Config(provider, host=config.listen_host, port=config.listen_port,
            lifespan='off', access_log=False, log_config=None, log_level='critical',
            proxy_headers=False, server_header=False, date_header=False, timeout_graceful_shutdown=5))
        self.config = config
        self.socket: socket.socket | None = None
        self.task: asyncio.Task[None] | None = None

    @property
    def live(self) -> bool:
        return bool(self.server.started and self.task is not None and not self.task.done())

    async def start(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.bind((self.config.listen_host, self.config.listen_port))
            self.socket.listen(32)
            self.socket.setblocking(False)
            self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]), name='ap2-private-provider')
            async with asyncio.timeout(5):
                while not self.live:
                    if self.task.done():
                        await self.task
                        raise RuntimeError('AIRPLAY_PROVIDER_START_FAILED')
                    await asyncio.sleep(.025)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        self.server.should_exit = True
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, timeout=8)
            except (Exception, asyncio.CancelledError):
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        if self.socket is not None:
            self.socket.close()


class PilotRunner:
    def __init__(self, config: PilotConfig, factory: Callable[[], Session], *,
                 ports: ZonePorts | None = None, manage_signals: bool = True) -> None:
        self.config, self.factory = config, factory
        self.manage_signals = manage_signals
        base_ports = ports or ZonePorts(client=lambda device: SoundTouchClient(device.ip_address,
            device_id=device.device_id, request_purpose='airplay_pilot', trigger='explicit_airplay_test'),
            guard=enforce_ip_write_guard)
        frozen = {item.device_id:item.fingerprint for item in config.devices}
        def guard(db: Session, device: Device) -> None:
            db.refresh(device)
            if device_fingerprint(device) != frozen.get(device.device_id):
                raise ValueError('AIRPLAY_DATABASE_TARGET_MISMATCH')
            base_ports.guard(db, device)
            verify_settings_identity(device, device.info_xml)
        self.ports = ZonePorts(client=base_ports.client, guard=guard)
        self.stop = asyncio.Event()
        self.run_id = 'ap2-pilot-' + uuid4().hex
        self.receipt: dict[str, object] = {'version': 1, 'run_id': self.run_id,
            'status': 'STARTING', 'startup_phase': 'DB_PREFLIGHT',
            'restore': 'NOT_STARTED', 'station_cleanup': 'NOT_CREATED',
            'backup_ids': [], 'audible_audio_verified': False}
        self.source: PilotSource | None = None
        self.controller: GroupController | None = None
        self.server: ProviderServer | None = None
        self.owner: asyncio.Task[GroupZoneResult] | None = None

    def status(self, state: str) -> None:
        self.receipt['status'] = state
        _state_file(self.config.status_path, self.receipt)

    def guard_provider(self) -> None:
        if self.controller is None or self.owner is None or self.owner.done():
            raise RuntimeError('AIRPLAY_OWNER_UNAVAILABLE')
        self.controller.check()

    @asynccontextmanager
    async def prepare(self, db: Session, devices: list[Device], station: Station,
                      xml: str, guard: Callable[[], None]) -> AsyncIterator[OutputOwner]:
        def preparing() -> None:
            if self.stop.is_set() or self.config.stop_path.exists():
                raise RuntimeError('AIRPLAY_PILOT_STOP_REQUESTED')
            guard()
        preparing()
        self.receipt['startup_phase'] = 'LIVE_CAPABILITY'
        async with device_transaction(item.device_id for item in devices):
            # Cached capabilities may be absent. The established profile and
            # fresh identity-bound endpoint parsers are the authority. Validate
            # every participant before the source can make its first write.
            for device in devices:
                preparing()
                self.ports.guard(db, device)
            for device in devices:
                client = self.ports.client(device)
                for path in ('/info', '/getZone', '/rebroadcastlatencymode'):
                    preparing()
                    self.ports.guard(db, device)
                    read_zone_value(path, await client.get_xml(path), device)
                    preparing()
                    self.ports.guard(db, device)
            self.receipt['capability_check'] = 'LIVE_READBACK_VERIFIED'
            baseline = db.scalar(select(func.max(ConfigBackup.id))) or 0
            self.receipt['restore'] = 'MANUAL_REQUIRED'
            self.receipt['startup_phase'] = 'SOURCE_PREPARE'
            self.status('PREPARING')
            try:
                async with pilot_source_scope(db, devices, master=self.config.master, station=station,
                        xml=xml, ports=self.ports, guard_audio=preparing, test_volume=self.config.test_volume,
                        radio_volume_forwarding=self.config.radio_volume_forwarding) as source:
                    self.source = source
                    yield source
            finally:
                db.rollback()
                rows = db.query(ConfigBackup).filter(ConfigBackup.id > baseline,
                    ConfigBackup.device_id.in_([item.device_id for item in devices])).all()
                self.receipt['backup_ids'] = [row.id for row in rows]
                if self.source is not None:
                    self.receipt['restore'] = self.source.restore_result
                else:
                    # Preparation may fail before the source scope yields.
                    backups = [row for row in rows if row.path.startswith('ap2-source-')]
                    if not backups:
                        self.receipt['restore'] = 'NO_RADIO_WRITE_ATTEMPTED'
                    else:
                        job = backups[0].path.split('/')[0]
                        restored = db.query(DeviceActionJournal).filter_by(job_id=job,
                            phase='AIRPLAY_SOURCE_RESTORED', verified=True).first()
                        self.receipt['restore'] = 'CAPTURED_STATE_RESTORED' if restored else 'MANUAL_REQUIRED'

    def result(self, result: GroupZoneResult) -> None:
        self.receipt['startup_phase'] = 'GROUP_RUN'
        self.receipt['group_status'] = result.status
        self.receipt['group_backup_ref'] = result.backup_ref
        self.receipt['members'] = list(result.members)
        self.status('ACTIVE' if result.status == 'ACTIVE' else 'RESTORING')

    async def _watch(self) -> str:
        deadline = time.monotonic() + self.config.lifetime_seconds
        ready = False
        while self.owner is not None and not self.owner.done():
            if self.stop.is_set() or self.config.stop_path.exists():
                return 'STOP_REQUESTED'
            if time.monotonic() >= deadline:
                return 'LIFETIME_EXPIRED'
            if self.server is None or not self.server.live:
                return 'PROVIDER_LOST'
            if not ready and self.controller is not None and self.controller.frames:
                self.controller.check()
                ready = True
                # PREPARING may already have begun if both native inputs were
                # selected before startup. Never downgrade that state.
                if self.receipt['status'] == 'STARTING':
                    self.status('WAITING')
            await asyncio.sleep(.05)
        return 'NATIVE_END'

    async def _settle_owner(self) -> None:
        """Preserve the radio owner's task/locks during its graceful unwind.

        Normal stop requests verified teardown. Only exhaustion of the two-minute
        cleanup budget interrupts restoration; that is always manual recovery,
        never permission for the launcher to report success or delete evidence.
        """
        if self.owner is None:
            return
        done, _ = await asyncio.wait({self.owner}, timeout=120)
        if not done:
            self.receipt['cleanup_timeout'] = True
            self.receipt['restore'] = 'MANUAL_REQUIRED'
            self.owner.cancel()
        await asyncio.gather(self.owner, return_exceptions=True)
        if self.receipt.get('cleanup_timeout'):
            self.receipt['restore'] = 'MANUAL_REQUIRED'

    async def run(self) -> dict[str, object]:
        config = self.config
        config.validate()
        for path in (config.status_path, config.receipt_path, config.stop_path):
            if path.exists() or path.is_symlink():
                raise ValueError('AIRPLAY_FRESH_RUN_PATHS_REQUIRED')
        _state_file(config.status_path, self.receipt, exclusive=True)
        station_id: int | None = None
        snapshot: tuple[object, ...] | None = None
        watcher: asyncio.Task[str] | None = None
        loop = asyncio.get_running_loop()
        if self.manage_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.stop.set)
        db = self.factory()
        try:
            devices = preflight(db, config)
            self.receipt['startup_phase'] = 'RECEIVER_BIND'
            receivers = {item.device_id: ReceiverPort(UnixReceiverIPC(item.receiver_socket,
                peer_uid=config.ipc_peer_uid)) for item in config.devices}
            bindings = await asyncio.gather(*(receivers[item.device_id].bind(item.device_id,
                binary_sha256=config.receiver_binary_sha256) for item in config.devices))
            session = GroupSession(tuple(bindings), master=config.master,
                # Playback still stops at lifetime_seconds. Reserve the already
                # bounded cleanup budget under the same live receiver bindings.
                started=time.monotonic(), lifetime=config.lifetime_seconds + 120)
            self.controller = GroupController(session, receivers,
                AudioControl(config.controller_nonce, receivers[config.master]))
            self.receipt['startup_phase'] = 'STATION_CREATE'
            station = Station(name=self.run_id, stream_url=config.relay_url,
                stream_url_resolved=config.relay_url, stream_format='mp3', stream_mime='audio/mpeg',
                provider_station_id=self.run_id, internal=True, lab_only=True, purpose='airplay_bridge')
            db.add(station)
            db.commit()
            db.refresh(station)
            station_id = station.id
            snapshot = _station_snapshot(station)
            self.receipt.update(station_id=station_id, station_cleanup='RETAINED')
            provider = PilotProvider(self.factory, station=station,
                peers={item.ip_address:item.device_id for item in config.devices},
                base_url=config.base_url, guard=self.guard_provider)
            element = ET.Element('ContentItem', source='LOCAL_INTERNET_RADIO', type='stationurl',
                                 location=provider.location)
            ET.SubElement(element, 'itemName').text = station.name
            xml = ET.tostring(element, encoding='unicode')
            self.receipt['startup_phase'] = 'PROVIDER_BIND'
            self.server = ProviderServer(provider, config)
            await self.server.start()
            self.receipt['startup_phase'] = 'INPUT_WAIT'
            self.owner = asyncio.create_task(self.controller.run_owned(db, devices, ports=self.ports,
                prepare=lambda guard:self.prepare(db, devices, station, xml, guard),
                initial_members=frozenset(session.device_ids - {config.master}),
                wait_seconds=min(config.wait_seconds, config.lifetime_seconds), on_result=self.result),
                name='ap2-private-owned-pilot')
            watcher = asyncio.create_task(self._watch())
            await asyncio.wait({self.owner, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if not self.owner.done():
                self.receipt['stop_reason'] = await watcher
                self.status('RESTORING')
                if self.receipt['stop_reason'] in {'STOP_REQUESTED', 'LIFETIME_EXPIRED'}:
                    self.controller.request_stop()
                else:
                    self.owner.cancel()  # Failed provider is not an orderly end.
                await self._settle_owner()
            try:
                await asyncio.shield(self.owner)
            except asyncio.CancelledError:
                if not self.owner.done():
                    raise
        except asyncio.CancelledError:
            self.receipt['stop_reason'] = 'TASK_CANCELLED'
            if self.owner is not None and not self.owner.done():
                if not self.owner.cancelling():
                    self.owner.cancel()
                await asyncio.shield(self._settle_owner())
        except Exception as exc:
            # Exception strings can contain the relay capability or DB XML.
            self.receipt.update(safe_error(exc))
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if self.owner is not None and not self.owner.done():
                if not self.owner.cancelling():
                    self.owner.cancel()
                await asyncio.shield(self._settle_owner())
            if self.server is not None:
                await self.server.close()
            db.close()
            if self.receipt['restore'] == 'NOT_STARTED':
                self.receipt['restore'] = 'NO_RADIO_WRITE_ATTEMPTED'
            if station_id is not None and snapshot is not None:
                self._cleanup_station(station_id, snapshot)
            state = 'MANUAL_REQUIRED' if self.receipt['restore'] not in {
                'NO_RADIO_WRITE_ATTEMPTED', 'CAPTURED_STATE_RESTORED'} else (
                'FAILED' if 'error_type' in self.receipt else 'FINISHED')
            self.status(state)
            _state_file(config.receipt_path, self.receipt, exclusive=True)
            if self.manage_signals:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.remove_signal_handler(sig)
        return self.receipt

    def _cleanup_station(self, station_id: int, snapshot: tuple[object, ...]) -> None:
        if self.receipt['restore'] not in {'NO_RADIO_WRITE_ATTEMPTED', 'CAPTURED_STATE_RESTORED'}:
            return
        try:
            with self.factory() as db:
                db.execute(text('BEGIN IMMEDIATE'))
                station = db.get(Station, station_id)
                if (station is not None and _station_snapshot(station) == snapshot
                        and station.provider_station_id == self.run_id
                        and db.query(Preset).filter_by(station_id=station_id).first() is None):
                    db.delete(station)
                    db.commit()
                    self.receipt['station_cleanup'] = 'DELETED_EXACT_OWNED_ROW'
        except Exception:
            self.receipt['station_cleanup'] = 'RETAINED_CHECK_FAILED'


def check_only(config: PilotConfig) -> dict[str, object]:
    engine = database(config, readonly=True)
    try:
        with Session(engine) as db:
            devices = preflight(db, config)
        return {'version': 1, 'status': 'PREFLIGHT_OK', 'device_ids': [item.device_id for item in devices],
                'radio_transport_opened': False, 'schema_changed': False,
                'capability_check': 'PENDING_LIVE_READBACK'}
    finally:
        engine.dispose()


async def run_pilot(config: PilotConfig) -> dict[str, object]:
    engine = database(config, readonly=False)
    try:
        return await PilotRunner(config, sessionmaker(bind=engine, expire_on_commit=False)).run()
    finally:
        engine.dispose()
