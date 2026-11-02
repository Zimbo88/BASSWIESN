"""Cooperative per-device ownership across local workers, released on process exit.

The filesystem namespace must be shared by every worker. This cannot coordinate
an external controller or make a radio transaction atomic. Never unlink lock
files: a second inode would create a second, independent owner.
"""
from __future__ import annotations

import asyncio
import fcntl
from hashlib import sha256
import os
from pathlib import Path
import stat
import time
from types import TracebackType
from typing import Any

from basswiesn.app.config import get_settings
from basswiesn.app.core.errors import BasswiesnError


class DeviceBusy(BasswiesnError):
    code = "DEVICE_BUSY"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("Another operation owns this device; no operation was started")


class DeviceLockUnavailable(BasswiesnError):
    code = "DEVICE_LOCK_UNAVAILABLE"
    status_code = 503

    def __init__(self) -> None:
        super().__init__("Device ownership could not be verified; no operation was started")


def lock_root() -> Path:
    return get_settings().data_dir / "device-locks"


class DeviceOwnership:
    def __init__(self, root: Path, device_id: str, *, timeout: float = 30.0) -> None:
        identity = device_id.strip().upper()
        if not identity or len(identity) > 128:
            raise DeviceLockUnavailable()
        self.root = Path(os.path.abspath(root))
        self.leaf = sha256(identity.encode()).hexdigest() + ".lock"
        self.timeout = timeout
        self._fd: int | None = None
        self._owner: asyncio.Task[Any] | None = None

    def _open(self) -> int:
        directory = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd: int | None = None
        try:
            for index, part in enumerate(self.root.parts[1:], start=1):
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                except FileNotFoundError:
                    if index != len(self.root.parts) - 1:
                        raise
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=directory)
                    except FileExistsError:
                        pass
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise DeviceLockUnavailable()
            fd = os.open(self.leaf, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=directory)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise DeviceLockUnavailable()
            result, fd = fd, None
            return result
        finally:
            if fd is not None:
                os.close(fd)
            os.close(directory)

    def _same_namespace(self, fd: int) -> bool:
        check = self._open()
        try:
            first, current = os.fstat(fd), os.fstat(check)
            return first.st_nlink == 1 and (first.st_dev, first.st_ino) == (current.st_dev, current.st_ino)
        finally:
            os.close(check)

    def locked(self) -> bool:
        if self._fd is not None:
            return True
        fd: int | None = None
        try:
            fd = self._open()
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return not self._same_namespace(fd)
        except (OSError, DeviceLockUnavailable):
            return True
        finally:
            if fd is not None:
                os.close(fd)

    async def acquire(self) -> bool:
        if self._owner is asyncio.current_task() and self._fd is not None:
            raise DeviceBusy()
        fd: int | None = None
        try:
            fd = self._open()
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise DeviceBusy() from None
                    await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            if not self._same_namespace(fd):
                raise DeviceLockUnavailable()
            self._fd, fd = fd, None
            self._owner = asyncio.current_task()
            return True
        except OSError:
            raise DeviceLockUnavailable() from None
        finally:
            if fd is not None:
                os.close(fd)

    def release(self) -> None:
        if self._fd is None or self._owner is not asyncio.current_task():
            raise RuntimeError("Device ownership is not held by this task")
        os.close(self._fd)
        self._fd, self._owner = None, None

    def owned_by_current_task(self) -> bool:
        """Explicit borrowing is limited to this task, never an inherited context."""
        return self._fd is not None and self._owner is asyncio.current_task()

    async def __aenter__(self) -> DeviceOwnership:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None,
                        exc: BaseException | None, traceback: TracebackType | None) -> None:
        self.release()
