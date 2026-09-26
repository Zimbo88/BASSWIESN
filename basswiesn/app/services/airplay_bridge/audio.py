"""Bounded PCM/silence switching for a future continuous encoded relay.

No subprocess, timer, HTTP listener or hardware is started here. A clocked
backend must call frame() every 20 ms. This does NOT preserve AirPlay PTP
timestamps through a Bose HTTP player's buffer and is not a sync algorithm.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RelayState(StrEnum):
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class PCMFormat:
    # Shairport Sync 5 AP2 pipe defaults; receiver configuration must still
    # explicitly agree. Classic S16/44100 is supported only when selected.
    sample_rate: int = 48000
    sample_bytes: int = 4
    channels: int = 2

    def __post_init__(self):
        if (self.sample_rate not in (44100, 48000) or self.sample_bytes not in (2, 4)
                or self.channels != 2):
            raise ValueError("unsupported PCM format")

    @property
    def frame_bytes(self) -> int:
        return self.sample_rate // 50 * self.sample_bytes * self.channels

    @property
    def ffmpeg_format(self) -> str:
        return "s16le" if self.sample_bytes == 2 else "s32le"


class PCMRelay:
    """Single-owner sample buffer. Full buffers fail; stale audio is not replayed.

    frame() returns valid digital silence between incoming chunks or while
    paused, never an empty audio stream. Silence is bounded (30 seconds by
    default). An ended session returns None so the runtime can close/restore.
    No audio is persisted. A new session invalidates previously queued input.
    """

    def __init__(self, pcm: PCMFormat = PCMFormat(), *, max_buffer_ms: int = 200,
                 max_silence_ms: int = 30000):
        if type(max_buffer_ms) is not int or not 20 <= max_buffer_ms <= 2000 or max_buffer_ms % 20:
            raise ValueError("buffer must be 20..2000 ms in 20 ms steps")
        if type(max_silence_ms) is not int or not 20 <= max_silence_ms <= 30000 or max_silence_ms % 20:
            raise ValueError("silence must be 20..30000 ms in 20 ms steps")
        self.pcm = pcm
        self.state = RelayState.IDLE
        self.generation = 0
        self._limit = pcm.frame_bytes * (max_buffer_ms // 20)
        self._silence_limit = max_silence_ms // 20
        self._silence_frames = 0
        self._buffer = bytearray()
        self.silence_frames = 0
        self.audio_frames = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def begin(self) -> int:
        self.generation += 1
        self.state = RelayState.ACTIVE
        self._buffer.clear()
        self._silence_frames = self.silence_frames = self.audio_frames = 0
        return self.generation

    def feed(self, generation: int, pcm_bytes: bytes) -> bool:
        if generation != self.generation or self.state != RelayState.ACTIVE:
            return False
        if len(self._buffer) + len(pcm_bytes) > self._limit:
            self._buffer.clear()
            self.state = RelayState.FAILED
            raise BufferError("PCM relay overflow; clock/input contract must be checked")
        # Pipe reads may split PCM frames. Partial sample bytes are retained
        # until a complete 20 ms frame is available, not padded mid-sample.
        self._buffer.extend(pcm_bytes)
        return True

    def pause(self, generation: int) -> bool:
        if generation != self.generation or self.state != RelayState.ACTIVE:
            return False
        self.state = RelayState.PAUSED
        self._buffer.clear()
        # Repeated pause/resume must not renew an otherwise silent lease.
        return True

    def resume(self, generation: int) -> bool:
        if generation != self.generation or self.state != RelayState.PAUSED:
            return False
        self.state = RelayState.ACTIVE
        return True

    def end(self, generation: int) -> bool:
        if generation != self.generation:
            return False
        self._buffer.clear()
        self.state = RelayState.IDLE
        return True

    def frame(self) -> bytes | None:
        if self.state not in (RelayState.ACTIVE, RelayState.PAUSED):
            return None
        size = self.pcm.frame_bytes
        if self.state == RelayState.ACTIVE and len(self._buffer) >= size:
            data = bytes(self._buffer[:size])
            del self._buffer[:size]
            self._silence_frames = 0
            self.audio_frames += 1
            return data
        if self._silence_frames >= self._silence_limit:
            self._buffer.clear()
            self.state = RelayState.EXPIRED
            return None
        self._silence_frames += 1
        self.silence_frames += 1
        return bytes(size)


def mp3_encoder_command(pcm: PCMFormat = PCMFormat()) -> list[str]:
    """Argument vector only. No shell, URLs or externally supplied arguments.

    Input must be clocked by the runtime. MP3 is a compatibility relay, not
    lossless AirPlay. One encoder must span silence/audio/pause transitions.
    """
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", pcm.ffmpeg_format, "-ar", str(pcm.sample_rate), "-ac", "2",
            "-i", "pipe:0", "-map_metadata", "-1", "-codec:a", "libmp3lame",
            "-b:a", "192k", "-write_xing", "0", "-id3v2_version", "0",
            "-flush_packets", "1", "-f", "mp3", "pipe:1"]
