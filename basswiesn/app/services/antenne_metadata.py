"""Bounded official programme metadata for the exact ANTENNE BAYERN main feed.

The observed ICY feeds repeat a slogan. The station's own web player obtains
separate artist/title fields from this endpoint. This is programme metadata,
not proof of sample-accurate alignment with a delayed/personalised audio feed.
No preset rewriting, credentials, cookies, playback control or generic scraper.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import time
from urllib.parse import urlsplit

import httpx

from basswiesn.app.services.display_composition import _text
from basswiesn.app.services.network_security import pinned_http_target, validate_outbound_http_url

ENDPOINT = "https://www.antenne.de/api/metadata/now/antenne"
MAX_RESPONSE = 32768


def supports(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.port not in {None, 80, 443}):
            return False
        return (parsed.hostname, parsed.path) in {
            ("mp3channels.webradio.antenne.de", "/antenne.aac"),
            ("stream.antenne.de", "/antenne/stream/aacp"),
            ("stream.antenne.de", "/antenne/stream/mp3"),
            ("stream.antenne.de", "/antenne/stream"),
        }
    except ValueError:
        return False


def timestamp(value: object) -> float:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid programme timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("programme timestamp has no zone")
    return stamp.astimezone(UTC).timestamp()


def parse(data: object, *, now: float) -> dict[str, object]:
    if not isinstance(data, dict) or data.get("success") is not True:
        raise ValueError("programme response unsuccessful")
    generated = timestamp(data.get("generatedon"))
    row = data.get("data")
    if not -5 <= now - generated <= 120 or not isinstance(row, dict) or row.get("mountpoint") != "antenne":
        raise ValueError("stale or wrong programme")
    result: dict[str, object] = {"status": "EMPTY_METADATA", "track": "", "artist": "", "other_info": "",
        "source": "ANTENNE_PROGRAMME_API", "classification": "PROGRAMME_METADATA",
        "programme_generated_timestamp": generated, "audio_alignment_verified": False}
    kind = row.get("class")
    if not isinstance(kind, str) or not kind:
        raise ValueError("missing programme class")
    if kind.casefold() != "music":
        # Moderation/advertisement must clear old song fields, not carry a
        # previous title into the present. Do not classify the spoken text.
        return result
    started = timestamp(row.get("starttime"))
    if not -5 <= now - started <= 1200:
        raise ValueError("song start is stale or in the future")
    if not isinstance(row.get("title"), str) or not isinstance(row.get("artist"), str):
        raise ValueError("wrong song field types")
    title, artist = _text(row["title"]), _text(row["artist"])
    if title and artist:
        result.update(status="AVAILABLE", track=title, artist=artist,
                      classification="ARTIST_TITLE_EXPLICIT", programme_started_timestamp=started)
    return result


async def probe() -> dict[str, object] | None:
    try:
        async with asyncio.timeout(5):
            validation = await asyncio.to_thread(validate_outbound_http_url, ENDPOINT, public_only=True)
            if not validation.ok:
                return None
            pinned, headers, extensions = pinned_http_target(ENDPOINT, validation)
            async with httpx.AsyncClient(timeout=4, follow_redirects=False, trust_env=False) as client:
                async with client.stream("GET", pinned, headers={**headers, "Accept": "application/json",
                        "Accept-Encoding": "identity", "User-Agent": "BASSWIESN-Metadata/1"},
                        extensions=extensions) as response:
                    if (response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity"
                            or response.headers.get("content-type", "").split(";", 1)[0] != "application/json"):
                        return None
                    raw = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(raw) + len(chunk) > MAX_RESPONSE:
                            return None
                        raw.extend(chunk)
                    return parse(json.loads(raw), now=time.time())
    except (httpx.HTTPError, OSError, ValueError, TimeoutError):
        return None
