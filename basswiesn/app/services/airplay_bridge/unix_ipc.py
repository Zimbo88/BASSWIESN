"""Application side of a private, fixed-receiver host bridge.

Only AF_UNIX is used. The app never sees a Docker socket, host command, mount
path, or an arbitrary container name. Peer credentials and private ownership
are checked before any request containing a controller nonce is sent.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import socket
import stat
import struct


class UnixReceiverIPC:
    def __init__(self, path: Path, *, peer_uid: int = 0) -> None:
        if type(peer_uid) is not int or peer_uid < 0 or not path.is_absolute():
            raise ValueError('AIRPLAY_IPC_PEER_REQUIRED')
        self.path, self.peer_uid = path, peer_uid

    async def exchange(self, request: bytes) -> bytes:
        if len(request) > 4096 or b'\n' in request:
            raise ValueError('AIRPLAY_IPC_REQUEST_REJECTED')
        parent = self.path.parent.lstat()
        info = self.path.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077
                or not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError('AIRPLAY_IPC_PRIVATE_SOCKET_REQUIRED')
        writer = None
        try:
            async with asyncio.timeout(1.75):
                reader, writer = await asyncio.open_unix_connection(str(self.path), limit=16385)
                endpoint = writer.get_extra_info('socket')
                _, uid, _ = struct.unpack('3i', endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != self.peer_uid:
                    raise ValueError('AIRPLAY_IPC_PEER_MISMATCH')
                writer.write(request + b'\n')
                await writer.drain()
                response = await reader.readuntil(b'\n')
                if len(response) > 16384:
                    raise ValueError('AIRPLAY_IPC_RESPONSE_EXCESSIVE')
                return response.rstrip(b'\n')
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
