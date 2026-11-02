"""Bounded POSIX pipe adapter; owns no descriptor and starts no processes.

The pinned native receiver opens its output pipe in blocking mode (common.c,
try_to_open_pipe_for_writing). Its pipe backend has no delay callback and can
write a lead-in/burst. Backpressure belongs at the read boundary, not after
unconditionally consuming a chunk into a full PCM queue. No audio is retained
here, and hold/pause draining remains the relay's responsibility.
"""
from __future__ import annotations

import asyncio
import os
from typing import Protocol


class PCMInputSink(Protocol):
    def input_read_budget(self) -> int: ...
    def feed(self, chunk: bytes) -> None: ...


class FIFOInput:
    def __init__(self, fd: int, sink: PCMInputSink) -> None:
        self._loop = asyncio.get_running_loop()
        self._fd = fd
        self._sink = sink
        self._timer: asyncio.TimerHandle | None = None
        self._closed = False
        self._registered = False
        self.backpressure_events = 0
        self.failure: str | None = None

    def start(self) -> None:
        if self._closed or self._registered or self._timer is not None:
            raise RuntimeError('FIFO reader cannot restart')
        if os.get_blocking(self._fd):
            raise ValueError('FIFO read descriptor must be nonblocking')
        self._arm()

    def _arm(self) -> None:
        self._timer = None
        if not self._closed:
            self._loop.add_reader(self._fd, self._consume)
            self._registered = True

    def _defer(self, seconds: float) -> None:
        self._loop.remove_reader(self._fd)
        self._registered = False
        self._timer = self._loop.call_later(seconds, self._arm)

    def _consume(self) -> None:
        try:
            budget = self._sink.input_read_budget()
            if type(budget) is not int or not 0 <= budget <= 8192:
                raise ValueError('invalid PCM read budget')
            if budget == 0:
                self.backpressure_events += 1
                # Give the 20 ms encoder clock and safety heartbeat a turn.
                # A readable but full pipe must not spin the event loop.
                self._defer(.005)
                return
            chunk = os.read(self._fd, budget)
            if chunk:
                self._sink.feed(chunk)
            else:
                # FIFO EOF is not proof that the Apple session ended. The
                # authenticated lifecycle observation owns that decision.
                self._defer(.02)
        except BlockingIOError:
            return
        except (OSError, ValueError, BufferError):
            self.failure = 'PCM_INPUT_FAILED'
            self.close()

    def close(self) -> None:
        self._closed = True
        if self._registered:
            self._loop.remove_reader(self._fd)
            self._registered = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
