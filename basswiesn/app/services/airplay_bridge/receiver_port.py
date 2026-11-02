"""Typed client for the fixed, private receiver IPC command set.

The host supervisor supplies an already pinned namespace/process channel.
This module cannot start Docker, discover devices or open an Internet listener.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Protocol

from .group_session import ReceiverBinding, ReceiverSample
from .lifecycle import PlaybackCursor
from .volume import project_volume


class ReceiverIPC(Protocol):
    async def exchange(self, request: bytes) -> bytes: ...


def _object(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in items:
            if key in value:
                raise ValueError('AIRPLAY_IPC_DUPLICATE')
            value[key] = item
        return value
    if len(raw) > 16384:
        raise ValueError('AIRPLAY_IPC_EXCESSIVE')
    value = json.loads(raw, object_pairs_hook=pairs)
    if type(value) is not dict:
        raise ValueError('AIRPLAY_IPC_OBJECT_REQUIRED')
    return value


@dataclass(frozen=True)
class ReceiverFrame:
    sample: ReceiverSample
    playback: dict[str, Any]
    display: dict[str, Any]
    volume: dict[str, Any] | None = None
    volume_mode: str = 'software'


class ReceiverPort:
    def __init__(self, ipc: ReceiverIPC) -> None:
        self.ipc = ipc
        self.binding: ReceiverBinding | None = None
        self.playback = PlaybackCursor()
        self._volume: dict[str, Any] | None = None

    async def _call(self, operation: str, **values: object) -> dict[str, Any]:
        return _object(await self.ipc.exchange(json.dumps(dict(operation=operation, **values)).encode()))

    async def bind(self, device_id: str, *, binary_sha256: str) -> ReceiverBinding:
        if self.binding is not None or not re.fullmatch('[a-f0-9]{64}', binary_sha256):
            raise ValueError('AIRPLAY_RECEIVER_BINDING_REQUIRED')
        value = await self._call('bind')
        if (set(value) != {'version', 'nonce', 'pid', 'started', 'contract', 'binary_sha256'}
                or type(value['version']) is not int or value['version'] != 1
                or value['contract'] != 'basswiesn-group-v1' or value['binary_sha256'] != binary_sha256):
            raise ValueError('AIRPLAY_RECEIVER_BUILD_UNVERIFIED')
        # Reuse the authoritative cursor's strict binding validation.
        from .native_group import NativeGroupCursor
        NativeGroupCursor(nonce=value['nonce'], pid=value['pid'], started=value['started'])
        self.binding = ReceiverBinding(device_id, value['nonce'], value['pid'], value['started'])
        return self.binding

    async def read(self) -> ReceiverFrame:
        if self.binding is None:
            raise ValueError('AIRPLAY_RECEIVER_NOT_BOUND')
        value = await self._call('observe')
        if set(value) != {'native', 'observation'}:
            raise ValueError('AIRPLAY_RECEIVER_OBSERVATION_UNVERIFIED')
        obs = value['observation']
        fields = {'version', 'heartbeat_sequence', 'observed_monotonic', 'process_alive', 'playback', 'display'}
        if (type(obs) is not dict or set(obs) not in (fields, fields | {'volume', 'volume_mode'})
                or type(obs['version']) is not int or obs['version'] != 1
                or obs['process_alive'] is not True or type(obs['display']) is not dict
                or obs['display'].get('session') != self.binding.nonce):
            raise ValueError('AIRPLAY_RECEIVER_OBSERVATION_UNVERIFIED')
        mode, volume = obs.get('volume_mode', 'software'), obs.get('volume')
        if mode not in {'software', 'radio'}:
            raise ValueError('AIRPLAY_RECEIVER_VOLUME_MODE')
        if volume is not None:
            if (type(volume) is not dict or set(volume) != {'sequence', 'airplay_db'}
                    or type(volume['sequence']) is not int or not 1 <= volume['sequence'] <= 2**53-1):
                raise ValueError('AIRPLAY_RECEIVER_VOLUME_INVALID')
            project_volume(volume['airplay_db'], maximum=100)
            old = self._volume
            if old is not None and (volume['sequence'] < old['sequence'] or
                    (volume['sequence'] == old['sequence'] and volume != old)):
                raise ValueError('AIRPLAY_RECEIVER_VOLUME_REPLAY')
            self._volume = dict(volume)
        self.playback.accept(obs['playback'])
        return ReceiverFrame(ReceiverSample(json.dumps(value['native'], allow_nan=False).encode(),
            obs['heartbeat_sequence'], obs['observed_monotonic'], obs['process_alive']),
            obs['playback'], obs['display'], volume, mode)

    async def publish(self, raw: bytes) -> None:
        if await self._call('control', record=_object(raw)) != {'published': True}:
            raise ValueError('AIRPLAY_CONTROL_NOT_PUBLISHED')

    async def acknowledgement(self) -> bytes:
        value = await self._call('ack')
        if value == {'pending': True}:
            raise FileNotFoundError
        return json.dumps(value, allow_nan=False).encode()
