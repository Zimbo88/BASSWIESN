"""Publication dates, not build dates. Normal reads are entirely offline.

Only an explicit refresh reads the official GitHub API. No credentials, user
URLs, redirects, release bodies or asset downloads are accepted by this path.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import re

import httpx
from sqlalchemy.orm import Session

from basswiesn.app.models import RuntimeState
from basswiesn.app.services.network_security import pinned_http_target, validate_outbound_http_url

REPOSITORY = "https://github.com/Zimbo88/BASSWIESN"
API = "https://api.github.com/repos/Zimbo88/BASSWIESN/releases/tags/"
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$")
MAX_BYTES = 128 * 1024
# Verified against GitHub published_at, not the tag/commit/build timestamp.
# An unreleased version MUST NOT inherit the preceding release's date.
BUNDLED_PUBLICATIONS = {"2.6.5": "2026-09-19T13:36:41Z"}


def _published(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value if stamp <= datetime.now(UTC) else None
    except ValueError:
        return None


def publication_info(db: Session, version: str) -> dict:
    result = {"published_at": None, "release_url": None,
              "publication_status": "UNKNOWN", "publication_source": "NONE"}
    if not VERSION.fullmatch(version):
        return result
    stamp = None
    row = db.query(RuntimeState).filter(RuntimeState.key == "release_publication:" + version).one_or_none()
    if row:
        try:
            cached = json.loads(row.value)
            if isinstance(cached, dict) and cached.get("version") == version:
                stamp = _published(cached.get("published_at"))
        except (TypeError, ValueError):
            pass
    source = "GITHUB_CACHE" if stamp else "BUNDLED_GITHUB_OBSERVATION"
    stamp = stamp or _published(BUNDLED_PUBLICATIONS.get(version))
    if stamp:
        result.update(published_at=stamp, release_url=f"{REPOSITORY}/releases/tag/v{version}",
                      publication_status="PUBLISHED", publication_source=source)
    return result


async def fetch_publication(version: str, *, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    if not VERSION.fullmatch(version):
        return {"refresh_status": "INVALID_VERSION"}
    url = API + "v" + version
    try:
        async with asyncio.timeout(8):
            validation = await asyncio.to_thread(validate_outbound_http_url, url,
                                                  allowed_hosts={"api.github.com"}, public_only=True)
            if not validation.ok:
                return {"refresh_status": "TARGET_REJECTED"}
            pinned, headers, extensions = pinned_http_target(url, validation)
            async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False, transport=transport) as client:
                async with client.stream("GET", pinned, headers={**headers, "Accept": "application/vnd.github+json",
                        "Accept-Encoding": "identity", "User-Agent": "BASSWIESN-Release-Info"}, extensions=extensions) as response:
                    if response.status_code != 200:
                        return {"refresh_status": "NOT_FOUND" if response.status_code == 404 else
                                "RATE_LIMITED" if response.status_code in {403, 429} else "UNAVAILABLE"}
                    if response.headers.get("content-encoding", "identity") != "identity":
                        return {"refresh_status": "INVALID_RESPONSE"}
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > MAX_BYTES:
                            return {"refresh_status": "RESPONSE_TOO_LARGE"}
                        data.extend(chunk)
            payload = json.loads(data)
            if (not isinstance(payload, dict) or payload.get("tag_name") != "v" + version
                    or payload.get("draft") is not False
                    or payload.get("html_url") != f"{REPOSITORY}/releases/tag/v{version}"
                    or not _published(payload.get("published_at"))):
                return {"refresh_status": "INVALID_RESPONSE"}
            return {"refresh_status": "OK", "version": version, "published_at": payload["published_at"]}
    except (httpx.HTTPError, OSError, ValueError, TimeoutError):
        # Never expose free exception/header/body data in a public status.
        return {"refresh_status": "UNAVAILABLE"}


async def refresh_publication(db: Session, version: str) -> dict:
    result = await fetch_publication(version)
    if result.get("refresh_status") == "OK":
        key = "release_publication:" + version
        row = db.query(RuntimeState).filter(RuntimeState.key == key).one_or_none()
        if row is None:
            row = RuntimeState(key=key)
            db.add(row)
        row.value = json.dumps({"version": version, "published_at": result["published_at"]})
        row.updated_at = datetime.now(UTC)
        db.commit()
    return {**publication_info(db, version), "refresh_status": result["refresh_status"]}
