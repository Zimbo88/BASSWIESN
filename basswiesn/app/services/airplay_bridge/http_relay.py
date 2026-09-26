"""Explicit one-session HTTP MP3 relay; no radio requests or discovery.

The owner must verify the target identity, take a backup and enforce network
exclusions before starting this server. A token and socket peer check bind the
stream to that preflight. Forwarded headers are never trusted; nothing is logged.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import re

from .relay_runtime import ClockedMP3Relay, RelayFailure


class PCMHTTPRelay:
    def __init__(self, *, bind: str, target: str, token: str, port: int = 8098):
        self.bind = str(ipaddress.IPv4Address(bind))
        self.target = str(ipaddress.IPv4Address(target))
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            raise ValueError("a random 256-bit stream token is required")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("invalid port")
        self.port = port
        self._path = ('/relay/' + token + '.mp3').encode('ascii')
        self._server = None
        self._tasks: set[asyncio.Task] = set()
        self._runtime: ClockedMP3Relay | None = None
        self._used = False
        self._closed = False
        self._input_ended = False
        self._audio_released = False
        self.state = 'WAITING_FOR_RADIO'
        self.failure = None
        self.audio_bytes = 0
        self.nonzero_chunks = 0
        self.sent_bytes = 0
        self.rejected = 0
        self.forwarded_pcm_bytes = 0

    def release_audio(self):
        """Trusted owner only, after post-select volume/mute/identity readback.

        No HTTP request can release this gate. Prior PCM was discarded, so
        opening it never replays audio accumulated while the radio was unsafe.
        """
        if (self._closed or self._input_ended or self.failure or
                self.state != 'STREAMING' or self._runtime is None):
            return False
        self._audio_released = True
        return True

    def feed(self, chunk: bytes):
        if self._closed or self._input_ended:
            return
        if len(chunk) > 8192:
            raise ValueError('PCM reads must be bounded')
        self.audio_bytes += len(chunk)
        self.nonzero_chunks += int(any(chunk))
        if self._runtime is not None and self._audio_released:
            try:
                if self._runtime.feed(self._runtime.generation, chunk):
                    self.forwarded_pcm_bytes += len(chunk)
            except BufferError:
                self.failure = 'PCM_OVERFLOW'

    def end_input(self):
        # No historical PCM is retained for a reconnect or later Apple sender.
        self._input_ended = True
        self._audio_released = False
        if self._runtime is not None:
            self._runtime.stop()
        elif self.state == 'WAITING_FOR_RADIO':
            self.state = 'ENDED'

    def snapshot(self):
        return {'state': self.state, 'failure': self.failure,
                'audio_bytes': self.audio_bytes, 'nonzero_chunks': self.nonzero_chunks,
                'sent_bytes': self.sent_bytes, 'rejected': self.rejected,
                'audio_released': self._audio_released,
                'forwarded_pcm_bytes': self.forwarded_pcm_bytes}

    async def start(self):
        if self._server is not None or self._closed:
            raise RuntimeError('server cannot restart')
        self._server = await asyncio.start_server(
            self._handle, host=self.bind, port=self.port, limit=4096, backlog=4)
        self.port = self._server.sockets[0].getsockname()[1]

    @staticmethod
    async def _reject(writer, status):
        writer.write(b'HTTP/1.0 ' + status + b'\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        await asyncio.wait_for(writer.drain(), timeout=1)

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        if len(self._tasks) >= 4 or self._closed:
            writer.close()
            return
        self._tasks.add(task)
        children = []
        owns_stream = False
        try:
            peer = writer.get_extra_info('peername')
            if not peer or peer[0] != self.target:
                self.rejected += 1
                await self._reject(writer, b'403 Forbidden')
                return
            try:
                request = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=3)
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, TimeoutError):
                self.rejected += 1
                await self._reject(writer, b'400 Bad Request')
                return
            parts = request.split(b'\r\n', 1)[0].split(b' ')
            if (len(parts) != 3 or parts[0] not in (b'GET', b'HEAD')
                    or parts[2] not in (b'HTTP/1.0', b'HTTP/1.1')
                    or not hmac.compare_digest(parts[1], self._path)):
                self.rejected += 1
                await self._reject(writer, b'404 Not Found')
                return
            if self._used or self._input_ended:
                await self._reject(writer, b'410 Gone')
                return
            headers = (b'HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n'
                       b'Cache-Control: no-store\r\nConnection: close\r\n\r\n')
            if parts[0] == b'HEAD':
                writer.write(headers)
                await asyncio.wait_for(writer.drain(), timeout=1)
                return
            self._used = owns_stream = True
            self._runtime = ClockedMP3Relay()
            subscriber = self._runtime.subscribe()
            self.state = 'STREAMING'
            writer.write(headers)
            await asyncio.wait_for(writer.drain(), timeout=1)

            async def forward():
                async for chunk in subscriber:
                    writer.write(chunk)
                    await asyncio.wait_for(writer.drain(), timeout=1)
                    self.sent_bytes += len(chunk)

            async def disconnected():
                # No request body or pipelining is supported for this live URL.
                await reader.read(1)
                raise RelayFailure('RADIO_DISCONNECTED')

            encode = asyncio.create_task(self._runtime.run())
            sending = asyncio.create_task(forward())
            watch = asyncio.create_task(disconnected())
            children = [encode, sending, watch]
            done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                finished.result()
            await encode
            await sending
            self.state = 'ENDED'
        except RelayFailure as exc:
            self.failure = str(exc)  # fixed library codes, not sender values
            self.state = 'FAILED'
        except (ConnectionError, TimeoutError, OSError):
            if owns_stream:
                self.failure = 'HTTP_OUTPUT_IO'
                self.state = 'FAILED'
        finally:
            if owns_stream and self._runtime is not None:
                self._runtime.stop()
            for child in children:
                if not child.done():
                    child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            if owns_stream:
                self._runtime = None
                self._audio_released = False
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1)
            except (ConnectionError, TimeoutError, OSError):
                pass
            self._tasks.discard(task)

    async def close(self):
        self._closed = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self.end_input()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.state = 'CLOSED'
