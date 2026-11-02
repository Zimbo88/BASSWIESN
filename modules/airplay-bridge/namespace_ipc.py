"""Opt-in host bridge for one immutable receiver container.

Not installed or launched automatically. A separate namespace owner supplies
the exact container ID, image ID, lifetime and allowed application UID. Only
four fixed receiver-IPC operations are possible; no arbitrary Docker command
or arguments cross the socket. There is no TCP listener or app Docker socket.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import time


class NamespaceBridge:
    def __init__(self, container_id, image_id, uid, *, lifetime=900):
        if (not re.fullmatch('[a-f0-9]{64}', container_id)
                or not re.fullmatch('sha256:[a-f0-9]{64}', image_id)
                or type(uid) is not int or uid <= 0
                or type(lifetime) is not int or not 1 <= lifetime <= 900):
            raise ValueError('exact owned instance required')
        self.container_id, self.image_id, self.uid = container_id, image_id, uid
        self.deadline = time.monotonic() + lifetime
        self.lock = asyncio.Lock()
        self.failed = False

    async def command(self, *args, data=None):
        process = await asyncio.create_subprocess_exec('docker', *args,
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            async with asyncio.timeout(1.5):
                if data is not None:
                    process.stdin.write(data)
                    await process.stdin.drain()
                    process.stdin.close()
                chunks, size = [], 0
                while True:
                    chunk = await process.stdout.read(8192)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > 32768:
                        raise ValueError('excessive private receiver response')
                await process.wait()
                out = b''.join(chunks)
            if process.returncode:
                raise ValueError('private receiver command failed')
            return out
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def verify(self):
        value = json.loads(await self.command('inspect', self.container_id))[0]
        host = value['HostConfig']
        if (value['Id'] != self.container_id or value['Image'] != self.image_id
                or value['State']['Running'] is not True or value['State']['Restarting'] is not False
                or value['RestartCount'] != 0 or value['Config']['User'] != 'avahi'
                or host['ReadonlyRootfs'] is not True or host['Privileged'] is not False
                or host['NetworkMode'] != 'none' or host['PidMode'] == 'host' or host['IpcMode'] == 'host'
                or host['CapAdd'] or 'ALL' not in host['CapDrop']
                or not any(value in host['SecurityOpt'] for value in
                           ('no-new-privileges', 'no-new-privileges:true'))
                or any('docker.sock' in mount.get('Destination', '') for mount in value['Mounts'])):
            raise ValueError('receiver isolation changed')

    async def exchange(self, raw):
        async with self.lock:
            if self.failed or time.monotonic() >= self.deadline or len(raw) > 4096:
                raise ValueError('private receiver lifetime ended')
            try:
                value = json.loads(raw)
                if (type(value) is not dict or value.get('operation') not in {'observe', 'bind', 'control', 'ack'}
                        or set(value) != ({'operation', 'record'} if value['operation'] == 'control'
                                         else {'operation'})):
                    raise ValueError('unplanned IPC operation')
                await self.verify()
                return await self.command('exec', '-i', '--user', 'avahi', self.container_id,
                    'python3', '/opt/basswiesn-ap2/receiver_ipc.py', data=raw)
            except BaseException:
                self.failed = True
                raise

    async def connection(self, reader, writer):
        try:
            peer = writer.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != self.uid:
                raise ValueError('unauthorized peer')
            async with asyncio.timeout(3):
                raw = await reader.readuntil(b'\n')
                result = await self.exchange(raw.strip())
                writer.write(result.rstrip(b'\n') + b'\n')
                await writer.drain()
        except Exception:
            writer.write(b'{"error":"AIRPLAY_NAMESPACE_IPC_REJECTED"}\n')
        finally:
            writer.close()
            await writer.wait_closed()


@asynccontextmanager
async def private_server(path, uid, connection):
    """Pin the directory and socket inode before privileged ownership changes.

    The application owns this directory and may rename entries. Never chown or
    chmod through its mutable pathname (or follow a replacement symlink).
    """
    if not path.is_absolute():
        raise ValueError('private application directory required')
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    endpoint, node, inode, server = None, None, None, None
    try:
        parent = os.fstat(directory)
        if parent.st_uid != uid or parent.st_mode & 0o077:
            raise ValueError('private application directory required')
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        endpoint.setblocking(False)
        # bind() itself refuses an existing entry; no stale-socket unlink.
        endpoint.bind(f'/proc/self/fd/{directory}/{path.name}')
        node = os.open(path.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=directory)
        info = os.fstat(node)
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1):
            raise ValueError('private socket replaced')
        inode = info.st_ino
        pinned = f'/proc/self/fd/{node}'
        os.chmod(pinned, 0o600)
        os.chown(pinned, uid, -1)
        server = await asyncio.start_unix_server(connection, sock=endpoint, limit=4097)
        yield server
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        elif endpoint is not None:
            endpoint.close()
        try:
            if inode is not None and os.stat(path.name, dir_fd=directory, follow_symlinks=False).st_ino == inode:
                os.unlink(path.name, dir_fd=directory)
        except FileNotFoundError:
            pass
        if node is not None:
            os.close(node)
        os.close(directory)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container-id', required=True)
    parser.add_argument('--image-id', required=True)
    parser.add_argument('--application-uid', type=int, required=True)
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=900)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('explicit host namespace owner required')
    os.umask(0o077)
    bridge = NamespaceBridge(args.container_id, args.image_id, args.application_uid, lifetime=args.seconds)
    await bridge.verify()
    async with private_server(args.socket, args.application_uid, bridge.connection):
        await asyncio.sleep(max(0, bridge.deadline-time.monotonic()))


if __name__ == '__main__':
    asyncio.run(main())
