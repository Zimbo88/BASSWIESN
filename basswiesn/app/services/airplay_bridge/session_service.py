"""Long-lived, local session coordinator; never provisions or discovers radios.

A trusted network owner supplies a fresh PilotConfig per native session. This
service keeps one-shot relay grants one-shot, reserves targets before invoking
the existing verified runner, and requires reconciliation after a crash. It
does not turn the bounded pilot into an always-on receiver or bypass its TTL.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Protocol

from .pilot_runner import PilotConfig, _private_directory, _state_file

LIVE = frozenset({'STARTING', 'WAITING', 'PREPARING', 'ACTIVE', 'RESTORING', 'STOPPING'})
TERMINAL = frozenset({'FINISHED', 'FAILED', 'RECOVERY_REQUIRED'})


class SessionWorker(Protocol):
    stop: asyncio.Event
    receipt: dict[str, object]

    async def run(self) -> dict[str, object]: ...


class SessionConflict(ValueError):
    """Fixed diagnostic only; never stringify a configuration or relay URL."""


def configuration_digest(config: PilotConfig) -> str:
    # A digest binds retries to the complete private configuration; capability
    # URLs and nonces never enter the journal or the human status response.
    value = json.dumps(asdict(config), default=str, sort_keys=True, allow_nan=False)
    return sha256(value.encode()).hexdigest()


def grant_digests(config: PilotConfig) -> list[str]:
    # Changing a timeout, filename or volume does not make a consumed relay or
    # controller capability fresh. Persist only domain-separated hashes.
    return [sha256((kind + '\0' + value).encode()).hexdigest()
            for kind, value in (('relay', config.relay_url), ('controller', config.controller_nonce))]


class SessionService:
    """Single event-loop owner, with a process-lifetime cross-process flock.

    The caller owns startup/shutdown signals. Workers must not install their
    own process signal handlers. No worker is resumed from disk on startup.
    """

    def __init__(self, root: Path, factory: Callable[[PilotConfig], SessionWorker], *,
                 preflight: Callable[[PilotConfig], object], capacity: int = 128) -> None:
        _private_directory(root)
        if type(capacity) is not int or not 1 <= capacity <= 1024:
            raise ValueError('AIRPLAY_SERVICE_CAPACITY')
        self.root, self.factory, self.preflight, self.capacity = root, factory, preflight, capacity
        self.journal = root / 'sessions.json'
        self.entries: dict[str, dict[str, Any]] = {}
        self.workers: dict[str, SessionWorker] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.configs: dict[str, PilotConfig] = {}
        self.closed = False
        self.storage_failed = False
        self._closing: asyncio.Task[None] | None = None
        self.fd = os.open(root / 'service.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError('AIRPLAY_SERVICE_LOCK_UNSAFE')
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._load()
        except BaseException:
            os.close(self.fd)
            raise

    def _load(self) -> None:
        if not self.journal.exists() and not self.journal.is_symlink():
            return
        from .native_group import read_native_group
        raw = read_native_group(self.journal, maximum=16384)
        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in items:
                if key in result:
                    raise ValueError('AIRPLAY_SERVICE_JOURNAL_INVALID')
                result[key] = value
            return result
        value = json.loads(raw, object_pairs_hook=pairs)
        if (not isinstance(value, dict) or set(value) != {'version', 'sessions'}
                or type(value['version']) is not int or value['version'] != 1
                or not isinstance(value['sessions'], dict) or len(value['sessions']) > self.capacity):
            raise ValueError('AIRPLAY_SERVICE_JOURNAL_INVALID')
        for key, entry in value['sessions'].items():
            self._key(key)
            if (not isinstance(entry, dict) or set(entry) != {'state', 'targets', 'digest', 'grants', 'receipt'}
                    or not isinstance(entry['state'], str) or entry['state'] not in LIVE | TERMINAL
                    or not isinstance(entry['digest'], str)
                    or not re.fullmatch('[a-f0-9]{64}', entry['digest'])
                    or not isinstance(entry['grants'], list) or len(entry['grants']) != 2
                    or any(not isinstance(item, str) or not re.fullmatch('[a-f0-9]{64}', item)
                           for item in entry['grants'])
                    or not isinstance(entry['targets'], list) or not 1 <= len(entry['targets']) <= 16
                    or any(not isinstance(target, str) or not target or len(target) > 128
                           for target in entry['targets'])
                    or len(set(entry['targets'])) != len(entry['targets'])
                    or not isinstance(entry['receipt'], dict)):
                raise ValueError('AIRPLAY_SERVICE_JOURNAL_INVALID')
            entry['receipt'] = self._safe_receipt(entry['receipt'])
            if entry['state'] in LIVE:
                entry['state'] = 'RECOVERY_REQUIRED'
            self.entries[key] = entry
        self._save()

    @staticmethod
    def _key(key: str) -> None:
        if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{32}', key):
            raise ValueError('AIRPLAY_SERVICE_REQUEST_ID')

    @staticmethod
    def _safe_receipt(receipt: dict[str, object]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, allowed in {
            'status': LIVE | {'FINISHED', 'FAILED', 'MANUAL_REQUIRED'},
            'restore': {'NOT_STARTED', 'NO_RADIO_WRITE_ATTEMPTED', 'CAPTURED_STATE_RESTORED', 'MANUAL_REQUIRED'},
            'station_cleanup': {'NOT_CREATED', 'RETAINED', 'DELETED_EXACT_OWNED_ROW', 'RETAINED_CHECK_FAILED'},
        }.items():
            value = receipt.get(key)
            if isinstance(value, str) and value in allowed:
                result[key] = value
        value = receipt.get('backup_ids')
        if isinstance(value, list) and len(value) <= 128 and all(type(x) is int and x > 0 for x in value):
            result['backup_ids'] = list(value)
        return result

    def _save(self) -> None:
        try:
            value = {'version': 1, 'sessions': self.entries}
            if len(json.dumps(value).encode()) > 16384:
                raise ValueError('AIRPLAY_SERVICE_JOURNAL_FULL')
            _state_file(self.journal, value)
        except Exception:
            self.storage_failed = True
            # Losing the journal must not leave audio running merely because
            # the stop request itself could not be persisted (e.g. disk full).
            for entry in self.entries.values():
                if entry['state'] in LIVE:
                    entry['state'] = 'STOPPING'
            for worker in self.workers.values():
                worker.stop.set()
            raise

    def status(self) -> dict[str, object]:
        # No file, DB, socket or device I/O. Transport success is never ACTIVE.
        sessions = []
        for key, entry in self.entries.items():
            state = entry['state']
            worker = self.workers.get(key)
            if worker and state != 'STOPPING' and worker.receipt.get('status') in LIVE:
                state = str(worker.receipt['status'])
            sessions.append({'request_id': key, 'state': state, 'targets': list(entry['targets']),
                             'receipt': dict(entry['receipt'])})
        return {'version': 1, 'sessions': sessions, 'hardware_acceptance': 'NOT_ESTABLISHED'}

    def start(self, key: str, config: PilotConfig) -> dict[str, object]:
        self._key(key)
        if self.closed or self.storage_failed:
            raise SessionConflict('AIRPLAY_SERVICE_CLOSED')
        config.validate()
        digest = configuration_digest(config)
        grants = grant_digests(config)
        if key in self.entries:
            if self.entries[key]['digest'] != digest:
                raise SessionConflict('AIRPLAY_REQUEST_ID_REUSED')
            return self.status()  # replay cannot restart a finished operation
        targets = {device.device_id for device in config.devices}
        for old_key, entry in self.entries.items():
            if entry['state'] in LIVE | {'RECOVERY_REQUIRED'} and targets.intersection(entry['targets']):
                raise SessionConflict('AIRPLAY_DEVICE_BUSY_OR_UNRECONCILED')
            if entry['digest'] == digest or set(grants).intersection(entry['grants']):
                raise SessionConflict('AIRPLAY_FRESH_PROFILE_REQUIRED')
            old = self.configs.get(old_key)
            if old and entry['state'] in LIVE and (
                    old.listen_port == config.listen_port
                    or old.relay_url == config.relay_url
                    or {old.status_path, old.receipt_path, old.stop_path}
                       & {config.status_path, config.receipt_path, config.stop_path}
                    or {x.receiver_socket for x in old.devices} & {x.receiver_socket for x in config.devices}):
                raise SessionConflict('AIRPLAY_RESOURCE_ALREADY_OWNED')
        if len(self.entries) >= self.capacity:
            raise SessionConflict('AIRPLAY_SERVICE_JOURNAL_FULL')
        self.preflight(config)  # actual cache/profile/protection checks, no transport
        self.entries[key] = {'state': 'STARTING', 'targets': sorted(targets),
                             'digest': digest, 'grants': grants, 'receipt': {}}
        try:
            self._save()  # durable reservation BEFORE any worker/factory side effect
        except BaseException:
            del self.entries[key]
            raise
        self.configs[key] = config
        self.tasks[key] = asyncio.create_task(self._run(key, config), name='ap2-session-' + key)
        return self.status()

    async def _run(self, key: str, config: PilotConfig) -> None:
        entry = self.entries[key]
        try:
            if entry['state'] == 'STOPPING':
                entry.update(state='FINISHED', receipt={'restore': 'NO_RADIO_WRITE_ATTEMPTED',
                    'station_cleanup': 'NOT_CREATED', 'status': 'FINISHED'})
                return
            worker = self.factory(config)
            if getattr(worker, 'manage_signals', False):
                raise ValueError('AIRPLAY_SERVICE_OWNS_SIGNALS')
            self.workers[key] = worker
            receipt = self._safe_receipt(await worker.run())
            clean = (receipt.get('restore') in {'NO_RADIO_WRITE_ATTEMPTED', 'CAPTURED_STATE_RESTORED'}
                and receipt.get('station_cleanup') in {'NOT_CREATED', 'DELETED_EXACT_OWNED_ROW'})
            entry.update(receipt=receipt, state=(
                'FINISHED' if receipt.get('status') == 'FINISHED' else 'FAILED') if clean else 'RECOVERY_REQUIRED')
        except BaseException:
            # Cancellation or a lost result never means that no radio write
            # happened. Reserve those targets until independently reconciled.
            entry['state'] = 'RECOVERY_REQUIRED'
        finally:
            self.workers.pop(key, None)
            self._save()

    def stop(self, key: str) -> dict[str, object]:
        self._key(key)
        if self.closed or key not in self.entries:
            raise SessionConflict('AIRPLAY_SERVICE_SESSION_UNKNOWN')
        if self.entries[key]['state'] in LIVE:
            self.entries[key]['state'] = 'STOPPING'
            self._save()
            if key in self.workers:
                self.workers[key].stop.set()  # never cancel a device transaction
        return self.status()

    async def close(self) -> None:
        if self._closing is None:
            for key in self.entries:
                try:
                    self.stop(key)
                except Exception:
                    self.storage_failed = True
            for worker in self.workers.values():
                worker.stop.set()
            self.closed = True
            async def finish() -> None:
                try:
                    if self.tasks:
                        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
                    if self.storage_failed:
                        raise RuntimeError('AIRPLAY_SERVICE_JOURNAL_FAILED')
                finally:
                    os.close(self.fd)
            self._closing = asyncio.create_task(finish(), name='ap2-service-cleanup')
        # A caller timeout cannot cancel cleanup or leak its process lock.
        await asyncio.shield(self._closing)
