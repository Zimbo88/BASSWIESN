"""Bounded loopback liveness checks, separate from radio/provider health."""
import asyncio
import json
from urllib.parse import urlsplit

import httpx


def _local_liveness_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
                and parsed.port is not None and 0 < parsed.port <= 65535
                and parsed.path == "/live" and not parsed.query and not parsed.fragment
                and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


async def http_service_status(name: str, url: str) -> dict[str, object]:
    if not _local_liveness_url(url):
        return {"service": name, "online": False, "error": "InvalidLivenessTarget"}
    try:
        async with asyncio.timeout(1.0):
            async with httpx.AsyncClient(timeout=0.8, trust_env=False, follow_redirects=False) as client:
                async with client.stream("GET", url) as response:
                    result: dict[str, object] = {
                        "service": name, "online": False,
                        "status_code": response.status_code, "url": url,
                    }
                    if response.status_code != 200:
                        return result
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1024:
                            result["error"] = "InvalidLivenessResponse"
                            return result
                    try:
                        value = json.loads(body)
                    except (ValueError, UnicodeError):
                        value = None
                    # A login/error page (even HTTP 200), a redirect, or a
                    # readiness response must never count as process liveness.
                    result["online"] = (
                        isinstance(value, dict) and len(value) == 1 and value.get("live") is True)
                    return result
    except Exception as exc:
        # Network exception text can contain credentials or response contents.
        return {"service": name, "online": False, "error": type(exc).__name__, "url": url}
