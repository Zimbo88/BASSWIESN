"""Fixed-name owner IPC files; never accepts a path from the network."""
from __future__ import annotations

import os
from pathlib import Path
import secrets

from .native_group import read_native_group


def publish_control(root: Path, raw: bytes) -> None:
    if len(raw) > 2048:
        raise ValueError("AIRPLAY_IPC_EXCESSIVE")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".controller-" + secrets.token_hex(16)
    created = False
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("AIRPLAY_IPC_PRIVATE_DIRECTORY_REQUIRED")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        created = True
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
        # Replaces the name itself, never follows an existing target symlink.
        os.replace(temporary, 'controller.json', src_dir_fd=directory, dst_dir_fd=directory)
        created = False
    finally:
        if created:
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)


class FileAudioControlPort:
    """Same-UID/mount-namespace adapter; remote namespaces need trusted IPC."""
    def __init__(self, root: Path) -> None:
        self.root = root

    async def publish(self, raw: bytes) -> None:
        publish_control(self.root, raw)

    async def acknowledgement(self) -> bytes:
        return read_native_group(self.root / 'controller-ack.json')
