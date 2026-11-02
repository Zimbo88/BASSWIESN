"""Local-only control of preinstalled session profiles; no shell/network targets.

This server belongs to an explicitly launched dedicated process, never the web
app lifespan. Same-UID Unix credentials protect the service; human auth/CSRF and
LAB restrictions belong to the web boundary. Profile configuration is not a
request field. Neither GET status nor construction starts a receiver or radio.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
from typing import Any

from .pilot_runner import PilotConfig
from .session_service import LIVE, TERMINAL, SessionConflict, SessionService, configuration_digest, grant_digests
from .unix_ipc import UnixReceiverIPC

ERRORS = frozenset({'AIRPLAY_SERVICE_CLOSED', 'AIRPLAY_REQUEST_ID_REUSED',
    'AIRPLAY_DEVICE_BUSY_OR_UNRECONCILED', 'AIRPLAY_RESOURCE_ALREADY_OWNED',
    'AIRPLAY_SERVICE_JOURNAL_FULL', 'AIRPLAY_SERVICE_SESSION_UNKNOWN',
    'AIRPLAY_FRESH_PROFILE_REQUIRED', 'AIRPLAY_CONTROL_INVALID',
    'AIRPLAY_PROFILE_UNKNOWN', 'AIRPLAY_PREFLIGHT_REJECTED', 'AIRPLAY_CONTROL_UNAVAILABLE'})


def decode_request(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError('AIRPLAY_CONTROL_INVALID')
            result[key] = value
        return result
    if len(raw) > 2048:
        raise ValueError('AIRPLAY_CONTROL_INVALID')
    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        raise ValueError('AIRPLAY_CONTROL_INVALID')
    action = value.get('action')
    if not isinstance(action, str) or action not in {'status', 'start', 'stop'}:
        raise ValueError('AIRPLAY_CONTROL_INVALID')
    expected = {'version', 'action'} | ({'request_id'} if action != 'status' else set())
    if action == 'start':
        expected.add('profile')
    if set(value) != expected:
        raise ValueError('AIRPLAY_CONTROL_INVALID')
    if action != 'status':
        SessionService._key(value['request_id'])
    if action == 'start' and (not isinstance(value['profile'], str)
            or not re.fullmatch('[a-z0-9][a-z0-9-]{0,47}', value['profile'])):
        raise ValueError('AIRPLAY_CONTROL_INVALID')
    return value


class ServiceControl:
    def __init__(self, service: SessionService, profiles: dict[str, PilotConfig]) -> None:
        if not 1 <= len(profiles) <= 16:
            raise ValueError('AIRPLAY_PROFILE_LIMIT')
        for name, profile in profiles.items():
            decode_request(json.dumps({'version': 1, 'action': 'start', 'profile': name,
                                       'request_id': '0' * 32}).encode())
            profile.validate()
        self.service, self.profiles = service, dict(profiles)
        self.server: asyncio.Server | None = None
        self.path = service.root / 'control.sock'
        self._socket_identity: tuple[int, int] | None = None
        self.connections: set[asyncio.Task[Any]] = set()

    def status(self) -> dict[str, object]:
        used = {entry['digest'] for entry in self.service.entries.values()}
        grants = {item for entry in self.service.entries.values() for item in entry['grants']}
        return {**self.service.status(), 'available': not self.service.closed and not self.service.storage_failed, 'maturity': 'LAB',
            'profiles': [{'id': name, 'targets': [device.device_id for device in config.devices],
                          'consumed': configuration_digest(config) in used or bool(grants.intersection(grant_digests(config))),
                          'volume_limit': config.test_volume, 'maximum_seconds': config.lifetime_seconds}
                         for name, config in self.profiles.items()]}

    def dispatch(self, raw: bytes) -> dict[str, object]:
        try:
            request = decode_request(raw)
        except (ValueError, TypeError, UnicodeError):
            return {'ok': False, 'code': 'AIRPLAY_CONTROL_INVALID'}
        try:
            if request['action'] == 'start':
                config = self.profiles.get(request['profile'])
                if config is None:
                    return {'ok': False, 'code': 'AIRPLAY_PROFILE_UNKNOWN'}
                self.service.start(request['request_id'], config)
            elif request['action'] == 'stop':
                self.service.stop(request['request_id'])
            return {'ok': True, 'result': self.status()}
        except SessionConflict as error:
            return {'ok': False, 'code': str(error) if str(error) in ERRORS else 'AIRPLAY_PREFLIGHT_REJECTED'}
        except Exception:
            return {'ok': False, 'code': 'AIRPLAY_PREFLIGHT_REJECTED'}

    async def start(self) -> None:
        # Never unlink a socket belonging to another service or reuse a stale
        # path automatically. The dedicated service lock must already be held.
        if self.server or self.path.exists() or self.path.is_symlink():
            raise ValueError('AIRPLAY_CONTROL_SOCKET_EXISTS')
        self.server = await asyncio.start_unix_server(self._handle, path=self.path, limit=2049)
        self.path.chmod(0o600)
        info = self.path.lstat()
        self._socket_identity = (info.st_dev, info.st_ino)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        try:
            peer = writer.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.geteuid() or len(self.connections) >= 8:
                return
            self.connections.add(task)
            raw = await asyncio.wait_for(reader.readuntil(b'\n'), 1)
            result = self.dispatch(raw[:-1])
            data = json.dumps(result, allow_nan=False).encode()
            if len(data) > 16383:
                data = b'{"ok":false,"code":"AIRPLAY_CONTROL_UNAVAILABLE"}'
            writer.write(data + b'\n')
            await asyncio.wait_for(writer.drain(), 1)
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            self.connections.discard(task)
            writer.close()
            await writer.wait_closed()

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for task in tuple(self.connections):
            task.cancel()
        await asyncio.gather(*self.connections, return_exceptions=True)
        try:
            await self.service.close()
        finally:
            try:
                info = self.path.lstat()
                if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self._socket_identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass


async def exchange(root: Path, raw: bytes) -> dict[str, Any]:
    decode_request(raw)
    response = await UnixReceiverIPC(root / 'control.sock', peer_uid=os.geteuid()).exchange(raw)
    result = json.loads(response)
    if (not isinstance(result, dict) or type(result.get('ok')) is not bool
            or (result['ok'] and set(result) != {'ok', 'result'})
            or (not result['ok'] and (set(result) != {'ok', 'code'}
                or not isinstance(result.get('code'), str) or result['code'] not in ERRORS))):
        raise ValueError('AIRPLAY_CONTROL_INVALID')
    if result['ok']:
        value = result['result']
        if (not isinstance(value, dict) or set(value) != {'version', 'sessions', 'hardware_acceptance',
                'available', 'maturity', 'profiles'} or value['version'] != 1
                or type(value['available']) is not bool or value['maturity'] != 'LAB'
                or value['hardware_acceptance'] != 'NOT_ESTABLISHED'
                or not isinstance(value['sessions'], list) or len(value['sessions']) > 128
                or not isinstance(value['profiles'], list) or len(value['profiles']) > 16):
            raise ValueError('AIRPLAY_CONTROL_INVALID')
        def targets(items: object) -> list[str]:
            if (not isinstance(items, list) or not 1 <= len(items) <= 16
                    or any(not isinstance(item, str) or not item or len(item) > 128 for item in items)
                    or len(set(items)) != len(items)):
                raise ValueError('AIRPLAY_CONTROL_INVALID')
            return list(items)
        sessions = []
        for item in value['sessions']:
            if (not isinstance(item, dict) or set(item) != {'request_id', 'state', 'targets', 'receipt'}
                    or not isinstance(item['state'], str) or item['state'] not in LIVE | TERMINAL
                    or not isinstance(item['receipt'], dict)):
                raise ValueError('AIRPLAY_CONTROL_INVALID')
            SessionService._key(item['request_id'])
            sessions.append({'request_id': item['request_id'], 'state': item['state'],
                'targets': targets(item['targets']), 'receipt': SessionService._safe_receipt(item['receipt'])})
        profiles = []
        for item in value['profiles']:
            if (not isinstance(item, dict) or set(item) != {'id', 'targets', 'consumed', 'volume_limit', 'maximum_seconds'}
                    or not isinstance(item['id'], str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,47}', item['id'])
                    or type(item['consumed']) is not bool or type(item['volume_limit']) is not int
                    or not 1 <= item['volume_limit'] <= 10
                    or type(item['maximum_seconds']) not in (int, float)
                    or not 1 <= item['maximum_seconds'] <= 600):
                raise ValueError('AIRPLAY_CONTROL_INVALID')
            profiles.append({**item, 'targets': targets(item['targets'])})
        result['result'] = {**value, 'sessions': sessions, 'profiles': profiles}
    return result
