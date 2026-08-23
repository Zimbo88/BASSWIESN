"""Operational policy and diagnostics for SoundTouch discovery."""

import logging
from collections.abc import Callable
from collections import Counter
from time import monotonic

from basswiesn.app.adapters.discovery import DiscoveryScanResult, scan_subnet_detailed


DEFAULT_DISCOVERY_TIMEOUT = 0.7
DEFAULT_DISCOVERY_CONCURRENCY = 64
MAX_DISCOVERY_HOSTS = 512

logger = logging.getLogger(__name__)


class DeviceDiscoveryService:
    def __init__(
        self,
        *,
        scanner: Callable = scan_subnet_detailed,
    ) -> None:
        self.scanner = scanner

    async def discover(
        self,
        cidr: str,
        *,
        timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
        limit: int = 254,
    ) -> DiscoveryScanResult:
        started = monotonic()
        result = await self.scanner(
            cidr,
            limit=min(max(limit, 0), MAX_DISCOVERY_HOSTS),
            timeout=timeout,
            concurrency=DEFAULT_DISCOVERY_CONCURRENCY,
        )
        counts = Counter(failure.code for failure in result.failures)
        logger.info(
            "SoundTouch discovery summary cidr=%s scanned=%s found=%s timeouts=%s unreachable=%s invalid_response=%s duration=%.2fs",
            cidr,
            result.scanned,
            len(result.devices),
            counts.get("timeout", 0),
            counts.get("unreachable", 0),
            counts.get("invalid_response", 0),
            monotonic() - started,
        )
        for failure in result.failures:
            logger.debug(
                "SoundTouch discovery failed ip=%s code=%s message=%s",
                failure.ip_address,
                failure.code,
                failure.message,
            )
        return result
