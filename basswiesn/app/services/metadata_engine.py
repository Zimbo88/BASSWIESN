"""Live metadata normalization and per-session scheduling.

The scheduler owns no playback-control dependency.  Its callbacks can fetch
provider metadata and persist/project changed fields, but cannot select a
source, call SetURL, stop or rebuffer a radio.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping


METADATA_POLL_FLOOR_SECONDS = 5
BOSEAPP_COALESCE_SECONDS = 2
DEFAULT_METADATA_STALE_AFTER_SECONDS = 300


class MetadataProvenance(StrEnum):
    RADIO = "RADIO"
    PROVIDER = "PROVIDER"
    STREAM = "STREAM"
    PRESET = "PRESET"
    BASSWIESN = "BASSWIESN"
    UNKNOWN = "UNKNOWN"


class ClockMetadataMode(StrEnum):
    OFF = "OFF"
    MISSING_TITLE = "MISSING_TITLE"
    APPEND = "APPEND"


@dataclass(frozen=True)
class MetadataSnapshot:
    station_name: str | None = None
    station_id: str | None = None
    track: str | None = None
    artist: str | None = None
    album: str | None = None
    image_url: str | None = None
    provider: str | None = None
    source: str | None = None
    updated_at: datetime | None = None
    provenance: MetadataProvenance = MetadataProvenance.UNKNOWN
    confidence: int = 0
    ask_again_after_s: int | None = None
    next_due_at: datetime | None = None
    stale: bool = True
    display_projection: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["provenance"] = self.provenance.value
        for name in ("updated_at", "next_due_at"):
            item = value[name]
            value[name] = item.isoformat() if item else None
        return value


def _aware(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _server_hint(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or parsed > (1 << 32) - 1:
        return None
    return parsed


def _runtime_field(
    payload: Mapping[str, Any], names: tuple[str, ...], previous: str | None
) -> str | None:
    """Keep a field when a partial update omits it; explicit null clears it."""

    for name in names:
        if name in payload:
            return _optional_text(payload[name])
    return previous


def normalize_metadata(
    payload: Mapping[str, Any],
    *,
    previous: MetadataSnapshot | None = None,
    provenance: MetadataProvenance = MetadataProvenance.PROVIDER,
    confidence: int = 90,
    observed_at: datetime | None = None,
    station_name: str | None = None,
    station_id: str | None = None,
    provider: str | None = None,
    source: str | None = None,
) -> MetadataSnapshot:
    """Normalize BMX-style metadata while preserving selection identity.

    Station/source/provider fields are retained from the current selection
    unless explicitly supplied by the caller.  Periodic BMX updates are only
    allowed to replace track, artist, album and image URL.
    """

    before = previous or MetadataSnapshot()
    selected_station_name = (
        _optional_text(station_name) if station_name is not None else before.station_name
    )
    selected_station_id = (
        _optional_text(station_id) if station_id is not None else before.station_id
    )
    selected_provider = provider if provider is not None else before.provider
    selected_source = source if source is not None else before.source

    # Runtime fields belong to one selection identity.  A scheduler may race
    # with a station/source change, so an explicitly changed identity starts
    # from empty runtime metadata rather than inheriting the previous track or
    # artwork.  Omitted identity arguments still mean a partial update of the
    # same selection.
    identity_changed = previous is not None and any(
        candidate is not None and _optional_text(candidate) != current
        for candidate, current in (
            (station_name, before.station_name),
            (station_id, before.station_id),
            (provider, before.provider),
            (source, before.source),
        )
    )
    runtime_before = MetadataSnapshot() if identity_changed else before
    embedded = payload.get("_embedded")
    if isinstance(embedded, Mapping):
        candidate = embedded.get("bmx_nowplaying") or embedded.get("nowPlaying")
        if isinstance(candidate, Mapping):
            payload = candidate
    updated = _aware(observed_at)
    hint = _server_hint(payload.get("askAgainAfter"))
    due = updated + timedelta(seconds=max(hint, METADATA_POLL_FLOOR_SECONDS)) if hint else None
    return MetadataSnapshot(
        station_name=selected_station_name,
        station_id=selected_station_id,
        track=_runtime_field(payload, ("track", "title"), runtime_before.track),
        artist=_runtime_field(payload, ("artist",), runtime_before.artist),
        album=_runtime_field(payload, ("album",), runtime_before.album),
        image_url=_runtime_field(
            payload, ("imageUrl", "artUrl"), runtime_before.image_url
        ),
        provider=selected_provider,
        source=selected_source,
        updated_at=updated,
        provenance=provenance,
        confidence=max(0, min(100, int(confidence))),
        ask_again_after_s=hint,
        next_due_at=due,
        stale=False,
        display_projection=(
            None if identity_changed else before.display_projection
        ),
    )


def metadata_changes(
    before: MetadataSnapshot | None, after: MetadataSnapshot
) -> tuple[str, ...]:
    if before is None:
        return tuple(
            name
            for name in ("track", "artist", "album", "image_url")
            if getattr(after, name) is not None
        )
    return tuple(
        name
        for name in ("track", "artist", "album", "image_url")
        if getattr(before, name) != getattr(after, name)
    )


def mark_metadata_stale(
    snapshot: MetadataSnapshot,
    *,
    now: datetime | None = None,
    stale_after_s: int = DEFAULT_METADATA_STALE_AFTER_SECONDS,
) -> MetadataSnapshot:
    observed = _aware(now)
    stale = snapshot.updated_at is None or (
        observed - _aware(snapshot.updated_at)
    ).total_seconds() >= max(METADATA_POLL_FLOOR_SECONDS, stale_after_s)
    return replace(snapshot, stale=stale)


def clock_display_projection(
    snapshot: MetadataSnapshot,
    *,
    mode: ClockMetadataMode = ClockMetadataMode.OFF,
    now: datetime | None = None,
) -> str | None:
    """Build the LAB-only track projection without mutating real metadata."""

    if mode == ClockMetadataMode.OFF:
        return snapshot.track
    clock = _aware(now).astimezone().strftime("%H:%M")
    track = (snapshot.track or "").strip()
    artist = (snapshot.artist or "").strip()
    if mode == ClockMetadataMode.MISSING_TITLE:
        return track or clock
    if track:
        title = f"{artist} – {track}" if artist else track
        return f"{title} · {clock}"
    return clock


FetchMetadata = Callable[[], Awaitable[Mapping[str, Any]]]
PublishMetadata = Callable[[MetadataSnapshot, tuple[str, ...]], Awaitable[None]]


class MetadataCoalescer:
    """Publish only the latest visible state inside the BoseApp ~2 s window."""

    def __init__(self, *, delay_seconds: float = BOSEAPP_COALESCE_SECONDS) -> None:
        self._delay = max(0.0, float(delay_seconds))
        self._pending: dict[str, tuple[MetadataSnapshot, set[str], PublishMetadata]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def submit(
        self,
        key: str,
        snapshot: MetadataSnapshot,
        changed: tuple[str, ...],
        publish: PublishMetadata,
    ) -> None:
        if not changed:
            return
        previous = self._pending.get(key)
        combined = set(changed)
        if previous is not None:
            combined.update(previous[1])
        self._pending[key] = (snapshot, combined, publish)
        task = self._tasks.get(key)
        if task is not None and not task.done():
            return

        self._start_runner(key)

    def _start_runner(self, key: str) -> None:
        """Start one publisher and drain updates that arrive during publish.

        Previously the pending value was removed before awaiting the callback.
        An update submitted while that callback was in flight saw an active
        task and was left pending forever.  The runner now drains a later
        generation after its own coalescing window.
        """

        async def runner() -> None:
            current_task = asyncio.current_task()
            try:
                while True:
                    await asyncio.sleep(self._delay)
                    current = self._pending.pop(key, None)
                    if current is None:
                        return
                    value, fields, callback = current
                    await callback(value, tuple(sorted(fields)))
                    if key not in self._pending:
                        return
            finally:
                if self._tasks.get(key) is current_task:
                    self._tasks.pop(key, None)
                # Preserve an update even when the previous publish callback
                # failed.  It receives a fresh coalescing window and task.
                if key in self._pending and key not in self._tasks:
                    self._start_runner(key)

        self._tasks[key] = asyncio.create_task(runner(), name=f"metadata-coalesce:{key}")

    def cancel(self, key: str) -> None:
        self._pending.pop(key, None)
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for key in list(self._tasks):
            self.cancel(key)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class MetadataScheduler:
    """Per-source, generation-safe scheduler with no global polling loop."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._generations: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def generation(self, key: str) -> int:
        return self._generations.get(key, 0)

    def cancel(self, key: str) -> None:
        self._generations[key] = self.generation(key) + 1
        task = self._tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    async def refresh_once(
        self,
        key: str,
        *,
        previous: MetadataSnapshot,
        fetch: FetchMetadata,
        publish: PublishMetadata,
        provenance: MetadataProvenance = MetadataProvenance.PROVIDER,
        observed_at: datetime | None = None,
    ) -> MetadataSnapshot:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            payload = await fetch()
            current = normalize_metadata(
                payload,
                previous=previous,
                provenance=provenance,
                observed_at=observed_at,
            )
            changed = metadata_changes(previous, current)
            if changed:
                await publish(current, changed)
            return current

    def schedule(
        self,
        key: str,
        *,
        due_at: datetime,
        callback: Callable[[], Awaitable[None]],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """Schedule one absolute due time, replacing only the same key."""

        self.cancel(key)
        generation = self.generation(key)
        clock = now or (lambda: datetime.now(UTC))

        async def runner() -> None:
            current_task = asyncio.current_task()
            try:
                delay = max(0.0, (_aware(due_at) - _aware(clock())).total_seconds())
                await asyncio.sleep(delay)
                if self.generation(key) != generation:
                    return
                # Release ownership before invoking the callback so it can
                # safely schedule the next absolute due time for this key.
                if self._tasks.get(key) is current_task:
                    self._tasks.pop(key, None)
                await callback()
            finally:
                if self._tasks.get(key) is current_task:
                    self._tasks.pop(key, None)

        self._tasks[key] = asyncio.create_task(runner(), name=f"metadata:{key}")

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for key in list(self._tasks):
            self.cancel(key)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
