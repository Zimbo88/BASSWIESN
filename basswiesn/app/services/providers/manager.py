"""Per-provider bounded operations and independent health observations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime
from typing import TypeVar

from .contracts import (
    MediaItem, MediaPage, PlayableURI, ProviderAdapter, ProviderDescriptor,
    ProviderError, ProviderErrorCode, ProviderHealth, ProviderState,
    validate_page, validate_text,
)

T = TypeVar("T")


class ProviderManager:
    """No tasks, startup probes, retry loops, recovery writes or shared fate."""

    def __init__(self, adapters: Iterable[ProviderAdapter], *, timeout: float = 10.0) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}
        self._health: dict[str, ProviderHealth] = {}
        self._busy: set[str] = set()
        if not 0 < timeout <= 30:
            raise ValueError("invalid provider deadline")
        self._timeout = timeout
        for adapter in adapters:
            descriptor = adapter.descriptor
            if descriptor.provider_id in self._adapters:
                raise ValueError("duplicate provider identity")
            self._adapters[descriptor.provider_id] = adapter
            self._health[descriptor.provider_id] = ProviderHealth(
                provider_id=descriptor.provider_id,
                provider=ProviderState.UNKNOWN if descriptor.enabled else ProviderState.DISABLED,
            )

    def descriptors(self) -> tuple[ProviderDescriptor, ...]:
        return tuple(adapter.descriptor for adapter in self._adapters.values())

    def cached_health(self, provider_id: str) -> ProviderHealth:
        if provider_id not in self._health:
            raise ProviderError(ProviderErrorCode.NOT_FOUND)
        return self._health[provider_id]

    def _adapter(self, provider_id: str, operation: str) -> ProviderAdapter:
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            raise ProviderError(ProviderErrorCode.NOT_FOUND)
        if not adapter.descriptor.enabled:
            raise ProviderError(ProviderErrorCode.DISABLED)
        if operation != "health" and not getattr(adapter.descriptor.capabilities, operation, False):
            raise ProviderError(ProviderErrorCode.UNSUPPORTED)
        return adapter

    async def _call(self, provider_id: str, invoke: Callable[[], Awaitable[T]]) -> T:
        # Fail fast instead of accumulating an unbounded queue behind a dead peer.
        if provider_id in self._busy:
            raise ProviderError(ProviderErrorCode.BUSY)
        self._busy.add(provider_id)
        try:
            async with asyncio.timeout(self._timeout):
                result = await invoke()
            self._health[provider_id] = replace(
                self._health[provider_id], provider=ProviderState.HEALTHY,
                error=None, checked_at=datetime.now(UTC).isoformat(),
            )
            return result
        except TimeoutError:
            error = ProviderError(ProviderErrorCode.TIMEOUT)
        except ProviderError as exc:
            error = exc
        except Exception:
            error = ProviderError(ProviderErrorCode.UNAVAILABLE)
        finally:
            self._busy.discard(provider_id)
        # Cancellation (BaseException) is deliberately propagated above.
        if error.code not in {ProviderErrorCode.INVALID_REQUEST, ProviderErrorCode.NOT_FOUND,
                              ProviderErrorCode.UNSUPPORTED}:
            state = ProviderState.AUTH_REQUIRED if error.code == ProviderErrorCode.AUTH_REQUIRED else ProviderState.UNAVAILABLE
            self._health[provider_id] = replace(
                self._health[provider_id], provider=state, error=error.code,
                checked_at=datetime.now(UTC).isoformat(),
            )
        raise error from None

    async def health(self, provider_id: str) -> ProviderHealth:
        adapter = self._adapter(provider_id, "health")
        result = await self._call(provider_id, adapter.health)
        self._health[provider_id] = replace(result, checked_at=datetime.now(UTC).isoformat())
        return self._health[provider_id]

    async def search(self, provider_id: str, query: str, *, limit: int = 25, offset: int = 0) -> MediaPage:
        adapter = self._adapter(provider_id, "search")
        validate_page(limit, offset)
        query = validate_text(query, maximum=200)
        return await self._call(provider_id, lambda: adapter.search(query, limit=limit, offset=offset))

    async def browse(self, provider_id: str, parent: str = "", *, limit: int = 25, offset: int = 0) -> MediaPage:
        adapter = self._adapter(provider_id, "browse")
        validate_page(limit, offset)
        parent = validate_text(parent, allow_empty=True)
        return await self._call(provider_id, lambda: adapter.browse(parent, limit=limit, offset=offset))

    async def metadata(self, provider_id: str, item_id: str) -> MediaItem:
        adapter = self._adapter(provider_id, "metadata")
        item_id = validate_text(item_id)
        return await self._call(provider_id, lambda: adapter.metadata(item_id))

    async def resolve(self, provider_id: str, item_id: str) -> PlayableURI:
        adapter = self._adapter(provider_id, "resolve")
        item_id = validate_text(item_id)
        return await self._call(provider_id, lambda: adapter.resolve(item_id))
