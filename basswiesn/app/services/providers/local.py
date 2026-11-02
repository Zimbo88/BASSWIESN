"""Existing saved stations as a provider; no schema changes or stream probes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .contracts import (
    MediaItem, MediaPage, PlayableURI, ProviderCapabilities, ProviderDescriptor,
    ProviderError, ProviderErrorCode, ProviderHealth, ProviderState, validate_page, validate_text,
)
from .transport import TargetPolicy


@dataclass(frozen=True)
class LocalStation:
    item_id: str
    name: str
    stream_url: str = field(repr=False)
    mime_type: str = ""


StationReader = Callable[[str, str, int, int], tuple[LocalStation, ...]]


class LocalStationAdapter:
    def __init__(self, reader: StationReader, policy: TargetPolicy) -> None:
        self._reader = reader
        self._policy = policy

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            "local_stations", "Saved stations", True, "SOFTWARE_TESTED",
            ProviderCapabilities(search=True, browse=True, metadata=True, resolve=True), "1",
        )

    @staticmethod
    def _item(station: LocalStation) -> MediaItem:
        return MediaItem("local_stations", station.item_id, station.name[:512], "radio", resolvable=True)

    async def health(self) -> ProviderHealth:
        self._reader("", "", 1, 0)
        return ProviderHealth("local_stations", ProviderState.HEALTHY)

    async def search(self, query: str, *, limit: int, offset: int) -> MediaPage:
        validate_page(limit, offset)
        query = validate_text(query, maximum=200)
        stations = self._reader(query, "", limit + 1, offset)
        return MediaPage(tuple(self._item(station) for station in stations[:limit]), limit, offset, len(stations) > limit)

    async def browse(self, parent: str, *, limit: int, offset: int) -> MediaPage:
        validate_page(limit, offset)
        if parent not in {"", "root"}:
            raise ProviderError(ProviderErrorCode.NOT_FOUND)
        stations = self._reader("", "", limit + 1, offset)
        return MediaPage(tuple(self._item(station) for station in stations[:limit]), limit, offset, len(stations) > limit)

    def _station(self, item_id: str) -> LocalStation:
        if not item_id.isascii() or not item_id.isdecimal() or len(item_id) > 18 or int(item_id) < 1:
            raise ProviderError(ProviderErrorCode.INVALID_REQUEST)
        stations = self._reader("", item_id, 1, 0)
        if not stations:
            raise ProviderError(ProviderErrorCode.NOT_FOUND)
        return stations[0]

    async def metadata(self, item_id: str) -> MediaItem:
        return self._item(self._station(item_id))

    async def resolve(self, item_id: str) -> PlayableURI:
        station = self._station(item_id)
        try:
            parsed = urlsplit(station.stream_url)
        except ValueError:
            raise ProviderError(ProviderErrorCode.UNSAFE_TARGET) from None
        # The catalog endpoint never exposes signed URLs or URL credentials.
        # Existing station playback is unchanged and owns its separate policy.
        if parsed.query:
            raise ProviderError(ProviderErrorCode.UNSAFE_TARGET)
        target = await self._policy.prepare(station.stream_url)
        return PlayableURI(
            "local_stations", station.item_id, station.stream_url, station.mime_type[:100],
            target.addresses,
        )
