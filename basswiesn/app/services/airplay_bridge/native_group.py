"""Consume local pinned-receiver observations, never Bonjour or RTSP logs.

These observations identify only the owned receiver's connection and group.
They do not authenticate a remote sender or authorize a SoundTouch operation.
The namespace owner must verify the exact receiver binary before starting it.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat


class GroupObservationRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("AIRPLAY_GROUP_OBSERVATION_UNVERIFIED")


@dataclass(frozen=True)
class NativeGroup:
    revision: int
    connection: int
    state: str
    group_hash: str
    event_monotonic: float


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise GroupObservationRejected()
        value[key] = item
    return value


def read_native_group(path: Path, *, maximum: int = 2048) -> bytes:
    """Fixed private tmpfs file, bounded, regular, owned, no symlink following."""
    if type(maximum) is not int or not 1 <= maximum <= 16384:
        raise GroupObservationRejected()
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise GroupObservationRejected()
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > maximum):
                raise GroupObservationRejected()
            raw = os.read(fd, maximum + 1)
            if len(raw) > maximum:
                raise GroupObservationRejected()
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(directory)


class NativeGroupCursor:
    def __init__(self, *, nonce: str, pid: int, started: float) -> None:
        if (not re.fullmatch(r"[a-f0-9]{32}", nonce) or type(pid) is not int or pid <= 0
                or type(started) not in (int, float) or not math.isfinite(started) or started < 0):
            raise GroupObservationRejected()
        self.nonce, self.pid, self.started = nonce, pid, started
        self.previous: NativeGroup | None = None
        self.now = started
        self.failed = False
        self.retired: set[int] = set()

    def accept(self, raw: bytes, *, now: float, process_alive: bool) -> NativeGroup:
        """Caller checks the original Popen child and lease before each read.

        Event age is not heartbeat age: a paused/live group may be unchanged.
        Replayed revisions cannot change state. Missing file, dead process or
        lease loss must stop the supervisor; no cached state is an alternative.
        """
        try:
            if (self.failed or process_alive is not True or len(raw) > 2048
                    or type(now) not in (int, float) or not math.isfinite(now) or now < self.now):
                raise GroupObservationRejected()
            self.now = now
            value = json.loads(raw, object_pairs_hook=_pairs)
            keys = {"version", "nonce", "pid", "revision", "connection", "state",
                    "group_hash", "event_monotonic"}
            if type(value) is not dict or set(value) != keys:
                raise GroupObservationRejected()
            if (type(value['version']) is not int or value['version'] != 1
                    or value['nonce'] != self.nonce or type(value['pid']) is not int
                    or value['pid'] != self.pid or type(value['revision']) is not int
                    or not 1 <= value['revision'] <= 2**53 - 1
                    or type(value['connection']) is not int or not 0 <= value['connection'] <= 2**31 - 1
                    or type(value['event_monotonic']) not in (int, float)
                    or not math.isfinite(value['event_monotonic'])
                    or not self.started <= value['event_monotonic'] <= now
                    or type(value['state']) is not str or value['state'] not in
                    {'IDLE', 'UNASSIGNED', 'GROUPED'} or type(value['group_hash']) is not str):
                raise GroupObservationRejected()
            record = NativeGroup(value['revision'], value['connection'], value['state'],
                                 value['group_hash'], value['event_monotonic'])
            if ((record.state == 'IDLE') != (record.connection == 0)
                    or (record.state == 'GROUPED' and not re.fullmatch(r'[a-f0-9]{64}', record.group_hash))
                    or (record.state != 'GROUPED' and record.group_hash)
                    or record.connection in self.retired):
                raise GroupObservationRejected()
            previous = self.previous
            if previous is not None:
                if (record.revision < previous.revision
                        or record.event_monotonic < previous.event_monotonic
                        or (record.revision == previous.revision and record != previous)):
                    raise GroupObservationRejected()
                if record.connection != previous.connection and previous.connection:
                    self.retired.add(previous.connection)
                    if len(self.retired) > 256:
                        raise GroupObservationRejected()
            self.previous = record
            return record
        except (ValueError, TypeError, UnicodeError):
            self.failed = True
            raise GroupObservationRejected() from None
