"""Explicitly started, bounded PCM -> MP3 runtime; no listener or radio I/O.

One encoder per session preserves the compressed stream across input gaps.
This is not an AirPlay clock translator or proof of inter-room synchronization.
All methods belong to one asyncio loop; callbacks must not run on other threads.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .audio import PCMFormat, PCMRelay, RelayState, mp3_encoder_command


class RelayFailure(RuntimeError):
    """Safe, fixed diagnostic code; never includes subprocess output."""


@dataclass(frozen=True)
class RelayResult:
    reason: str
    pcm_frames: int
    encoded_bytes: int
    encoder_exit: int | None


class MP3Subscription:
    """Bounded live output. No replay, disk recording, or unbounded client lag."""

    def __init__(self, max_chunks: int):
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=max_chunks)
        self._ready = asyncio.Event()
        self._closed = False

    def push(self, chunk: bytes):
        if self._closed:
            return
        self._queue.put_nowait(chunk)
        self._ready.set()

    def close(self, *, discard: bool):
        self._closed = True
        if discard:
            while not self._queue.empty():
                self._queue.get_nowait()
        self._ready.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            if not self._queue.empty():
                return self._queue.get_nowait()
            if self._closed:
                raise StopAsyncIteration
            self._ready.clear()
            await self._ready.wait()


class ClockedMP3Relay:
    """Fail a session instead of accumulating stale audio after backpressure.

    start/run are explicit: importing or constructing this object starts nothing.
    Subscribers must be registered before run(). A future HTTP adapter must
    authenticate/authorize subscribers separately and restore radios on failure.
    """

    def __init__(self, pcm: PCMFormat = PCMFormat(), *, max_silence_ms: int = 30000,
                 subscriber_chunks: int = 32):
        if type(subscriber_chunks) is not int or not 1 <= subscriber_chunks <= 64:
            raise ValueError("subscriber chunks must be 1..64")
        self.pcm = PCMRelay(pcm, max_silence_ms=max_silence_ms)
        self.generation = self.pcm.begin()
        self._chunks = subscriber_chunks
        self._subscribers: list[MP3Subscription] = []
        self._stop = asyncio.Event()
        self._used = False
        self._failure: str | None = None
        self._frames = self._bytes = 0

    def subscribe(self) -> MP3Subscription:
        if self._used or len(self._subscribers) >= 4:
            raise RuntimeError("register at most four subscribers before run")
        subscriber = MP3Subscription(self._chunks)
        self._subscribers.append(subscriber)
        return subscriber

    def feed(self, generation: int, chunk: bytes) -> bool:
        if self._stop.is_set():
            return False
        try:
            return self.pcm.feed(generation, chunk)
        except BufferError:
            self._failure = "PCM_OVERFLOW"
            self._stop.set()
            raise

    def stop(self):
        self._stop.set()

    async def _write_frames(self, process):
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while not self._stop.is_set():
            delay = deadline - loop.time()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    break
                except TimeoutError:
                    pass
            # Do not burst old frames after an overloaded event loop stalls.
            if loop.time() - deadline > .2:
                raise RelayFailure("CLOCK_DEADLINE_MISSED")
            if self._stop.is_set():
                break
            frame = self.pcm.frame()
            if frame is None:
                if self.pcm.state == RelayState.EXPIRED:
                    return "SILENCE_EXPIRED"
                raise RelayFailure("PCM_UNAVAILABLE")
            process.stdin.write(frame)
            try:
                await asyncio.wait_for(process.stdin.drain(), timeout=.5)
            except TimeoutError:
                raise RelayFailure("ENCODER_BACKPRESSURE") from None
            self._frames += 1
            deadline += .02
        return "STOPPED"

    async def _read_encoded(self, process):
        while chunk := await process.stdout.read(8192):
            self._bytes += len(chunk)
            try:
                for subscriber in self._subscribers:
                    subscriber.push(chunk)
            except asyncio.QueueFull:
                raise RelayFailure("SUBSCRIBER_BACKPRESSURE") from None

    @staticmethod
    async def _discard_remaining_output(process):
        # A stopped consumer must not leave a full stdout pipe blocking wait().
        # Discard in bounded chunks; never collect audio during error cleanup.
        while await process.stdout.read(8192):
            pass

    async def run(self) -> RelayResult:
        if self._used or not self._subscribers:
            raise RuntimeError("one run requires at least one subscriber")
        self._used = True
        process = None
        tasks = []
        reason = "FAILED"
        try:
            process = await asyncio.create_subprocess_exec(
                *mp3_encoder_command(self.pcm.pcm), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                limit=16384,
            )
            writer = asyncio.create_task(self._write_frames(process))
            reader = asyncio.create_task(self._read_encoded(process))
            tasks = [writer, reader]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                reader.result()  # expose our fixed code, not encoder output
                if not writer.done():
                    raise RelayFailure("ENCODER_EARLY_EOF")
            reason = await writer
            if self._failure:
                raise RelayFailure(self._failure)
            process.stdin.close()
            try:
                await asyncio.wait_for(reader, timeout=3)
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                raise RelayFailure("ENCODER_CLOSE_TIMEOUT") from None
            if process.returncode != 0:
                raise RelayFailure("ENCODER_EXIT")
            return RelayResult(reason, self._frames, self._bytes, process.returncode)
        except asyncio.CancelledError:
            reason = "CANCELLED"
            raise
        except RelayFailure:
            reason = "FAILED"
            raise
        except (OSError, BrokenPipeError, ConnectionError):
            reason = "FAILED"
            raise RelayFailure("ENCODER_IO") from None
        finally:
            self._stop.set()
            self.pcm.end(self.generation)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for subscriber in self._subscribers:
                subscriber.close(discard=reason not in ("STOPPED", "SILENCE_EXPIRED"))
            if process is not None:
                draining = asyncio.create_task(self._discard_remaining_output(process))
                try:
                    if process.returncode is None:
                        try:
                            process.terminate()
                        except ProcessLookupError:
                            pass
                        try:
                            await asyncio.wait_for(process.wait(), timeout=2)
                        except TimeoutError:
                            try:
                                process.kill()
                            except ProcessLookupError:
                                pass
                            await asyncio.wait_for(process.wait(), timeout=2)
                    await asyncio.wait_for(draining, timeout=2)
                finally:
                    draining.cancel()
                    await asyncio.gather(draining, return_exceptions=True)
