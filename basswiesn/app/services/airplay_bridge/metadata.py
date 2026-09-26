"""Bounded, session-scoped Shairport metadata adapter.

The backend supplies decoded (type, code, data) events through a future private
IPC adapter, never raw RTSP headers. Only display text is retained. Client names,
addresses, authentication fields, artwork and unknown codes are ignored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from basswiesn.app.services.metadata_engine import MetadataSnapshot, normalize_metadata
from basswiesn.app.services.station_metadata import DisplayPreference, display_fields


TEXT_CODES = {"minm": "track", "asar": "artist", "asal": "album", "asgn": "genre"}
MAX_TEXT_BYTES = 4096
MAX_TEXT_CHARS = 256


def _text(data: bytes) -> str:
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError("metadata text exceeds limit")
    value = data.decode("utf-8", errors="strict")
    return "".join(char for char in value if char.isprintable()).strip()[:MAX_TEXT_CHARS]


@dataclass
class BridgeMetadata:
    """One receiver, used by one event-loop owner; no persistence or network.

    The caller's generation comes from begin(), not from an Apple identifier.
    Queued events from a previous session cannot update a newer session. Every
    mdst/mden batch is a complete track snapshot: missing fields clear rather
    than borrowing the previous song's artist. Unbatched updates fail closed.
    """

    receiver_name: str
    _generation: int = field(default=0, init=False, repr=False)
    _active: bool = field(default=False, init=False, repr=False)
    _pending: dict[str, str] | None = field(default=None, init=False, repr=False)
    _visible: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _last_sequence: int = field(default=-1, init=False, repr=False)
    _observed_at: datetime | None = field(default=None, init=False, repr=False)

    def begin(self) -> int:
        self._generation += 1
        self._active = True
        self._pending = None
        self._visible = {}
        self._last_sequence = -1
        self._observed_at = None
        return self._generation

    def end(self, generation: int) -> bool:
        if generation != self._generation or not self._active:
            return False
        self._active = False
        self._pending = None
        self._visible = {}
        self._observed_at = None
        return True

    def apply(
        self, generation: int, sequence: int, kind: str, code: str, data: bytes = b"",
        *, observed_at: datetime | None = None,
    ) -> bool:
        """Return True only when a complete visible snapshot is committed.

        Malformed text aborts its whole pending batch. No partial track update
        or payload logging occurs. Artwork needs a separate validated cache;
        no sender-supplied URL is fetched by this adapter.
        """
        if (not self._active or generation != self._generation
                or type(sequence) is not int or sequence <= self._last_sequence):
            return False
        self._last_sequence = sequence
        if kind == "ssnc" and code == "mdst":
            self._pending = {}
        elif kind == "core" and code in TEXT_CODES and self._pending is not None:
            try:
                self._pending[TEXT_CODES[code]] = _text(data)
            except (UnicodeError, ValueError):
                self._pending = None
        elif kind == "ssnc" and code == "mden" and self._pending is not None:
            self._visible = self._pending
            self._pending = None
            self._observed_at = observed_at or datetime.now(UTC)
            return True
        return False

    def snapshot(self) -> MetadataSnapshot:
        if not self._active or self._observed_at is None:
            return MetadataSnapshot(station_name=self.receiver_name, provider="AIRPLAY_BRIDGE")
        return normalize_metadata(
            self._visible, station_name=self.receiver_name, provider="AIRPLAY_BRIDGE",
            # Relay playback is a Bose internet-radio source, not the native
            # AIRPLAY source. Do not report native receiver activation.
            source="LOCAL_INTERNET_RADIO", observed_at=self._observed_at,
        )

    def display(self, preference: DisplayPreference, *, clock_text: str = "") -> dict:
        """Use the existing ordered station/artist/title/clock display contract.

        This prepares fields only; delivering them to the provider/radio is a
        separate integration gate. Album remains a real metadata field.
        """
        fields = self._visible if self._active else {}
        return {**display_fields(preference, self.receiver_name, fields, clock_text=clock_text),
                "album": fields.get("album", "")}
