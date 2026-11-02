"""LAB catalog-only bridge to the verified Music Assistant 2.7.4 HTTP API.

The pinned source exposes no independent authenticated stream-resolution
command. Never synthesize queue URLs, mutate queues or copy provider secrets.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from .contracts import (
    MediaItem, MediaPage, PlayableURI, ProviderCapabilities, ProviderDescriptor,
    ProviderError, ProviderErrorCode, ProviderHealth, ProviderState, validate_page, validate_text,
)
from .transport import BoundedJSONClient, JSONValue

VERIFIED_VERSION = "2.7.4"
VERIFIED_COMMIT = "00fa04213156b5a2825bf4f83bcd05370a4b1bee"
_MEDIA_TYPES = ("track", "radio", "album", "artist", "playlist")
_URI = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}://(?:track|radio|album|artist|playlist)/[A-Za-z0-9_.~-]{1,256}\Z")
_BROWSE = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}://[A-Za-z0-9_./~-]{0,350}\Z")


def _identity(value: str, *, browse: bool = False) -> str:
    validate_text(value)
    if browse and value == "root":
        return value
    if not (_BROWSE if browse else _URI).fullmatch(value):
        raise ProviderError(ProviderErrorCode.INVALID_REQUEST)
    if value.split(":", 1)[0] in {"http", "https", "file", "builtin"} or ".." in value.split("://", 1)[1].split("/"):
        raise ProviderError(ProviderErrorCode.INVALID_REQUEST)
    return value


def _text(value: JSONValue, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
    return value


class MusicAssistantAdapter:
    def __init__(
        self, client: BoundedJSONClient | None = None, *, enabled: bool = False,
        configuration_error: ProviderErrorCode | None = None,
    ) -> None:
        self._client = client
        self._enabled = enabled
        self._configuration_error = configuration_error

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            "music_assistant", "Music Assistant (catalog only)", self._enabled, "LAB",
            ProviderCapabilities(search=True, browse=True, metadata=True), VERIFIED_VERSION,
        )

    async def _command(self, command: str, args: Mapping[str, JSONValue]) -> JSONValue:
        if not self._enabled:
            raise ProviderError(ProviderErrorCode.DISABLED)
        if self._configuration_error:
            raise ProviderError(self._configuration_error)
        if self._client is None:
            raise ProviderError(ProviderErrorCode.UNAVAILABLE)
        if command not in {"music/search", "music/browse", "music/item_by_uri"}:
            raise ProviderError(ProviderErrorCode.UNSUPPORTED)
        info = await self._client.request("/info")
        if not isinstance(info, dict) or info.get("server_version") != VERIFIED_VERSION:
            raise ProviderError(ProviderErrorCode.VERSION_UNSUPPORTED)
        # HTTP responses are raw results, not WebSocket result envelopes.
        return await self._client.request("/api", {
            "message_id": "basswiesn-catalog", "command": command, "args": dict(args),
        })

    @staticmethod
    def _item(raw: JSONValue) -> MediaItem:
        if not isinstance(raw, dict):
            raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
        media_type = _text(raw.get("media_type"), maximum=30)
        if media_type not in {*_MEDIA_TYPES, "folder"}:
            raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
        uri = _text(raw.get("path") if media_type == "folder" else raw.get("uri"))
        try:
            _identity(uri, browse=media_type == "folder")
        except ProviderError:
            raise ProviderError(ProviderErrorCode.INVALID_RESPONSE) from None
        artists: list[str] = []
        raw_artists = raw.get("artists", [])
        if not isinstance(raw_artists, list) or len(raw_artists) > 50:
            raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
        for artist in raw_artists:
            if not isinstance(artist, dict):
                raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
            artists.append(_text(artist.get("name")))
        album = raw.get("album")
        album_name = _text(album.get("name")) if isinstance(album, dict) else ""
        duration = raw.get("duration")
        if duration is not None and (type(duration) is not int or not 0 <= duration <= 604800):
            raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
        return MediaItem(
            "music_assistant", uri, _text(raw.get("name")), media_type,
            tuple(artists), album_name, duration if isinstance(duration, int) else None,
            browsable=media_type == "folder", resolvable=False,
        )

    async def health(self) -> ProviderHealth:
        result = await self._command("music/browse", {"path": "root"})
        if not isinstance(result, list):
            raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
        return ProviderHealth("music_assistant", ProviderState.HEALTHY)

    async def search(self, query: str, *, limit: int, offset: int) -> MediaPage:
        validate_page(limit, offset)
        query = validate_text(query, maximum=200)
        # Avoid MA's URL auto-detection path and arbitrary builtin URL parsing.
        if "://" in query:
            raise ProviderError(ProviderErrorCode.INVALID_REQUEST)
        # This upstream version has per-media-type limits, but no search offset.
        if offset:
            raise ProviderError(ProviderErrorCode.UNSUPPORTED)
        result = await self._command("music/search", {
            "search_query": query, "media_types": list(_MEDIA_TYPES),
            "limit": limit, "library_only": False,
        })
        if not isinstance(result, dict):
            raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
        items: list[MediaItem] = []
        potentially_limited = False
        for media_type in _MEDIA_TYPES:
            # SearchResults.radio is deliberately singular in models 1.1.86.
            group = result.get("radio" if media_type == "radio" else f"{media_type}s", [])
            if not isinstance(group, list) or len(group) > 100:
                raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
            potentially_limited = potentially_limited or len(group) >= limit
            items.extend(self._item(raw) for raw in group)
        # No invented pagination token: the returned set is a bounded preview.
        return MediaPage(tuple(items[:limit]), limit, 0, None, False, potentially_limited or len(items) > limit)

    async def browse(self, parent: str, *, limit: int, offset: int) -> MediaPage:
        validate_page(limit, offset)
        parent = _identity(parent or "root", browse=True)
        result = await self._command("music/browse", {"path": parent})
        if not isinstance(result, list) or len(result) > 5000:
            raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
        items = tuple(self._item(raw) for raw in result[offset:offset + limit])
        return MediaPage(items, limit, offset, len(result) > offset + limit)

    async def metadata(self, item_id: str) -> MediaItem:
        item_id = _identity(item_id)
        result = self._item(await self._command("music/item_by_uri", {"uri": item_id}))
        if result.item_id != item_id:
            raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
        return result

    async def resolve(self, item_id: str) -> PlayableURI:
        raise ProviderError(ProviderErrorCode.UNSUPPORTED)
