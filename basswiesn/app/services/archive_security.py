"""Shared validation for untrusted ZIP and tar archive metadata."""

from __future__ import annotations

import stat
from pathlib import PurePosixPath


class UnsafeArchive(ValueError):
    pass


def validate_archive_members(members, *, max_entries: int = 4096, max_uncompressed_bytes: int = 512 * 1024 * 1024) -> list[str]:
    members = list(members)
    if len(members) > max_entries:
        raise UnsafeArchive("archive contains too many entries")
    total = 0
    names: list[str] = []
    for member in members:
        name = str(getattr(member, "filename", getattr(member, "name", "")) or "")
        path = PurePosixPath(name)
        if not name or path.is_absolute() or ".." in path.parts or "\x00" in name:
            raise UnsafeArchive("archive contains an unsafe path")
        if name in names:
            raise UnsafeArchive("archive contains duplicate member names")
        names.append(name)
        if hasattr(member, "is_dir"):
            mode = (int(getattr(member, "external_attr", 0)) >> 16) & 0o170000
            if mode in {stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK}:
                raise UnsafeArchive("archive contains a symlink or special file")
            size = int(getattr(member, "file_size", 0) or 0)
        else:
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise UnsafeArchive("archive contains a symlink or special file")
            size = int(getattr(member, "size", 0) or 0)
        total += max(0, size)
        if total > max_uncompressed_bytes:
            raise UnsafeArchive("archive expanded size exceeds the configured limit")
    return names
