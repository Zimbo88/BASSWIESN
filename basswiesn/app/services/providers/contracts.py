"""Typed provider contracts independent of Bose's persisted source manifest."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class ProviderErrorCode(StrEnum):
    DISABLED = "PROVIDER_DISABLED"
    NOT_FOUND = "PROVIDER_NOT_FOUND"
    UNSUPPORTED = "PROVIDER_OPERATION_UNSUPPORTED"
    INVALID_REQUEST = "PROVIDER_INVALID_REQUEST"
    UNSAFE_TARGET = "PROVIDER_UNSAFE_TARGET"
    AUTH_REQUIRED = "PROVIDER_AUTH_REQUIRED"
    UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    TIMEOUT = "PROVIDER_TIMEOUT"
    INVALID_RESPONSE = "PROVIDER_INVALID_RESPONSE"
    VERSION_UNSUPPORTED = "PROVIDER_VERSION_UNSUPPORTED"
    BUSY = "PROVIDER_BUSY"


class ProviderError(Exception):
    """Fixed codes only: upstream payloads, URLs and credentials never escape."""

    def __init__(self, code: ProviderErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class ProviderState(StrEnum):
    DISABLED = "DISABLED"
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ProviderCapabilities:
    search: bool = False
    browse: bool = False
    metadata: bool = False
    resolve: bool = False
    # Catalog support never grants these hardware/remote-write capabilities.
    playback: bool = False
    reporting: bool = False


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    name: str
    enabled: bool
    stage: str
    capabilities: ProviderCapabilities
    contract_version: str


@dataclass(frozen=True)
class ProviderHealth:
    provider_id: str
    provider: ProviderState = ProviderState.UNKNOWN
    # These planes require independent evidence, not a successful catalog call.
    playback: str = "UNKNOWN"
    reporting: str = "NOT_APPLICABLE"
    error: ProviderErrorCode | None = None
    checked_at: str | None = None


@dataclass(frozen=True)
class MediaItem:
    provider_id: str
    item_id: str
    name: str
    media_type: str
    artists: tuple[str, ...] = ()
    album: str = ""
    duration_seconds: int | None = None
    # Provider URIs are opaque identities, not stream URLs.
    browsable: bool = False
    resolvable: bool = False


@dataclass(frozen=True)
class MediaPage:
    items: tuple[MediaItem, ...]
    limit: int
    offset: int
    has_more: bool | None = False
    offset_supported: bool = True
    truncated: bool = False


@dataclass(frozen=True)
class PlayableURI:
    provider_id: str
    item_id: str
    uri: str = field(repr=False)
    mime_type: str = ""
    validated_addresses: tuple[str, ...] = ()
    # A URI is a candidate, not evidence of codec support or audible playback.
    playback_verified: bool = False
    policy_recheck_required: bool = True


class ProviderAdapter(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    async def health(self) -> ProviderHealth: ...

    async def search(self, query: str, *, limit: int, offset: int) -> MediaPage: ...

    async def browse(self, parent: str, *, limit: int, offset: int) -> MediaPage: ...

    async def metadata(self, item_id: str) -> MediaItem: ...

    async def resolve(self, item_id: str) -> PlayableURI: ...


def validate_page(limit: int, offset: int) -> None:
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or not 0 <= offset <= 1000:
        raise ProviderError(ProviderErrorCode.INVALID_REQUEST)


def validate_text(value: str, *, maximum: int = 512, allow_empty: bool = False) -> str:
    if (not allow_empty and not value.strip()) or len(value) > maximum:
        raise ProviderError(ProviderErrorCode.INVALID_REQUEST)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ProviderError(ProviderErrorCode.INVALID_REQUEST)
    return value.strip()
