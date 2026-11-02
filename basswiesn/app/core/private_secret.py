"""Create-once local service secret; never rotate or repair an unsafe file.

This is a server-side Marge compatibility credential, not an Apple/MFi secret.
Directory-relative no-follow operations prevent symlink substitution. The local
runtime UID/administrator remain trusted; this cannot isolate their own writes.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import secrets
import stat


class SecretFileUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("LOCAL_SECRET_UNAVAILABLE")


def _directory(path: Path) -> int:
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise SecretFileUnavailable()
        result, directory = directory, -1
        return result
    finally:
        if directory != -1:
            os.close(directory)


def _private_regular(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise SecretFileUnavailable()


def load_or_create(path: Path) -> str:
    """Only an explicit setup operation calls this, never startup or a page GET."""
    path = Path(os.path.abspath(path))
    directory = lock = -1
    temporary = ""
    try:
        directory = _directory(path.parent)
        lock = os.open(path.name + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=directory)
        _private_regular(lock)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            temporary = ".secret-" + secrets.token_hex(16)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            try:
                value = secrets.token_urlsafe(32).encode("ascii")
                if os.write(fd, value) != len(value):
                    raise SecretFileUnavailable()
                os.fsync(fd)
            finally:
                os.close(fd)
            # Never replace a file introduced by a non-cooperating writer.
            os.link(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.unlink(temporary, dir_fd=directory)
            temporary = ""
            os.fsync(directory)
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            _private_regular(fd)
            raw = os.read(fd, 4097)
            if not raw or len(raw) > 4096 or b"\n" in raw or b"\r" in raw or b"\0" in raw:
                raise SecretFileUnavailable()
            value_text = raw.decode("utf-8").strip()
            if not value_text:
                raise SecretFileUnavailable()
            return value_text
        finally:
            os.close(fd)
    except (OSError, UnicodeError) as exc:
        raise SecretFileUnavailable() from exc
    finally:
        if temporary and directory != -1:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        if lock != -1:
            os.close(lock)
        if directory != -1:
            os.close(directory)
