"""Nonblocking process-shared ownership of local artwork file/row changes.

Network awaits happen before this lock. The stable lock leaf is never removed:
unlinking a live lock would allow another process to acquire a different inode.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from typing import Iterator


class ArtworkCacheBusy(OSError):
    """A cache mutation cannot safely own the filesystem/DB critical section."""


@contextmanager
def artwork_cache_lock(root: Path) -> Iterator[None]:
    """Fail closed on busy, non-private, non-regular or symlinked lock paths."""
    path = Path(os.path.abspath(root))
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd: int | None = None
    owned = False
    try:
        for index, part in enumerate(path.parts[1:], start=1):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                if index != len(path.parts) - 1:
                    raise
                # Only the named cache root may be created; never arbitrary
                # ancestors and never through a substituted symlink.
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        lock_fd = os.open(".retention.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                          0o600, dir_fd=directory)
        info = os.fstat(lock_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ArtworkCacheBusy("artwork cache lock is unsafe")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ArtworkCacheBusy("artwork cache is busy") from None
        owned = True
        yield
    except OSError:
        if not owned:
            raise ArtworkCacheBusy("artwork cache lock unavailable") from None
        raise
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(directory)
