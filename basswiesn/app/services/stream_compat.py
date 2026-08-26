from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from urllib.parse import urljoin

import httpx

from basswiesn.app.services.network_security import (
    UrlValidation,
    pinned_http_target,
    validate_outbound_http_url,
)


HLS_MIME_TYPES = {"application/vnd.apple.mpegurl", "application/x-mpegurl"}
DIRECT_AUDIO_MIME_PREFIXES = ("audio/mpeg", "audio/aac", "audio/aacp", "audio/ogg", "application/ogg", "audio/flac", "audio/x-flac")
REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
MAX_STREAM_REDIRECTS = 5


class ProtectedStreamTarget(ValueError):
    """Raised before a stream transport can target a protected radio."""


def _validate_stream_target(url: str) -> UrlValidation | None:
    validation = validate_outbound_http_url(url)
    if validation.ok:
        return validation
    if "protected device" in validation.reason:
        raise ProtectedStreamTarget("stream URL resolves to a protected device")
    return None


@dataclass(frozen=True)
class StreamAnalysis:
    stream_url_original: str
    stream_url_resolved: str
    stream_format: str
    stream_mime: str
    stream_codec: str
    compatibility_score: int
    compatibility_warning: str
    is_hls: bool
    is_direct_audio: bool
    stream_bitrate: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _path(url: str) -> str:
    return (url or "").split("?", 1)[0].lower()


def is_hls_stream(url: str = "", mime: str = "", body_preview: str = "") -> bool:
    mime_value = (mime or "").split(";", 1)[0].strip().lower()
    preview = (body_preview or "").lstrip()
    return (
        _path(url).endswith(".m3u8")
        or "/hls/" in (url or "").lower()
        or mime_value in HLS_MIME_TYPES
        or (preview.startswith("#EXTM3U") and "#EXT-X-" in preview[:4096])
    )


def _format_from_url(url: str, mime: str = "") -> tuple[str, str]:
    value = _path(url)
    mime_value = (mime or "").split(";", 1)[0].strip().lower()
    segments = [segment for segment in value.split("/") if segment]
    if value.endswith(".mp3") or "mp3" in segments or mime_value in {"audio/mpeg", "audio/mp3"}:
        return "mp3", "mp3"
    if value.endswith((".aac", ".aacp")) or "aac" in segments or "aacp" in segments or mime_value in {"audio/aac", "audio/aacp", "audio/x-aac"}:
        return "aac", "aac"
    if value.endswith((".ogg", ".oga")) or "ogg" in segments or mime_value in {"audio/ogg", "application/ogg"}:
        return "ogg", "ogg"
    if value.endswith(".flac") or mime_value in {"audio/flac", "audio/x-flac"}:
        return "flac", "flac"
    if value.endswith((".m3u", ".pls")):
        return "playlist", ""
    if is_hls_stream(url, mime):
        return "hls", "hls"
    return "unknown", ""


def _bitrate_from_url(url: str) -> int:
    value = _path(url)
    candidates = []
    for token in re.split(r"[^0-9]+", value):
        if not token:
            continue
        number = int(token)
        if 24 <= number <= 512:
            candidates.append(number)
    return candidates[-1] if candidates else 0


def _bitrate_value(value: object, url: str) -> int:
    try:
        bitrate = int(value or 0)
    except (TypeError, ValueError):
        bitrate = 0
    return bitrate or _bitrate_from_url(url)


def analyze_stream_url(url: str, mime: str = "", body_preview: str = "", resolved_url: str = "", bitrate: object = 0) -> StreamAnalysis:
    original = (url or "").strip()
    resolved = (resolved_url or original).strip()
    is_hls = is_hls_stream(resolved or original, mime, body_preview)
    fmt, codec = _format_from_url(resolved or original, mime)
    mime_value = (mime or "").split(";", 1)[0].strip().lower()
    stream_bitrate = _bitrate_value(bitrate, resolved or original)
    is_direct = bool(fmt in {"mp3", "aac", "ogg", "flac"} or mime_value.startswith(DIRECT_AUDIO_MIME_PREFIXES))
    score = {
        "mp3": 100,
        "aac": 90,
        "ogg": 80,
        "flac": 55,
        "unknown": 40,
        "playlist": 35,
        "hls": 5,
    }.get(fmt, 30)
    warning = ""
    if is_hls:
        fmt = "hls"
        codec = "hls"
        is_direct = False
        score = min(score, 5)
        warning = "Bose SoundTouch unterstuetzt HLS/m3u8 oft nicht. Direkter MP3/AAC Stream empfohlen."
    elif fmt == "aac" and stream_bitrate >= 320:
        score = min(score, 65)
        warning = "AAC 320 kbps kann auf manchen SoundTouch-Geraeten haken."
    elif fmt == "aac" and stream_bitrate >= 256:
        score = min(score, 75)
        warning = "Eingeschraenkt moeglich - hohe AAC-Bitrate."
    elif fmt == "mp3" and stream_bitrate >= 320:
        score = min(score, 92)
        warning = "Hohe Bitrate - bei schwachem WLAN eventuell instabil."
    elif fmt == "playlist":
        warning = "Playlist-URL sollte vor Preset-Schreiben zu direktem Audio aufgeloest werden."
    return StreamAnalysis(original, resolved, fmt, mime_value, codec, score, warning, is_hls, is_direct, stream_bitrate)


def _candidate_urls_from_m3u(text: str, base_url: str) -> list[str]:
    return [urljoin(base_url, line.strip()) for line in (text or "").splitlines() if line.strip() and not line.strip().startswith("#")]


def _candidate_urls_from_pls(text: str, base_url: str) -> list[str]:
    urls = []
    for line in (text or "").splitlines():
        if line.lower().startswith("file") and "=" in line:
            urls.append(urljoin(base_url, line.split("=", 1)[1].strip()))
    return urls


def _best_url(urls: list[str]) -> str:
    if not urls:
        return ""
    return sorted(urls, key=lambda value: (-analyze_stream_url(value).compatibility_score, value))[0]


def _safe_best_url(urls: list[str]) -> str:
    allowed: list[str] = []
    for value in urls:
        # Playlist entries are not fetched here.  A protected target is still
        # rejected before it can be persisted and handed to a radio.
        validation = validate_outbound_http_url(value)
        if not validation.ok and "protected device" in validation.reason:
            raise ProtectedStreamTarget("playlist resolves to a protected device")
        if validation.ok or "DNS resolution failed" in validation.reason:
            allowed.append(value)
    return _best_url(allowed)


async def resolve_stream_url(url: str, timeout: float = 2.5) -> StreamAnalysis:
    original = (url or "").strip()
    if not original:
        return analyze_stream_url("")
    initial = analyze_stream_url(original)
    target_validation = _validate_stream_target(original)
    if initial.is_direct_audio and not initial.is_hls:
        return initial
    if target_validation is None:
        # Resolution/syntax failure is a compatibility result, never a reason
        # to attempt an unvalidated server-side request.
        return initial
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            current_url = original
            response = None
            for _hop in range(MAX_STREAM_REDIRECTS + 1):
                validation = _validate_stream_target(current_url)
                if validation is None:
                    return initial
                pinned_url, pinned_headers, extensions = pinned_http_target(
                    current_url, validation
                )
                response = await client.get(
                    pinned_url,
                    headers={
                        **pinned_headers,
                        "User-Agent": "basswiesn/1.0",
                        "Range": "bytes=0-4095",
                    },
                    extensions=extensions,
                )
                status_code = int(getattr(response, "status_code", 200))
                if status_code not in REDIRECT_STATUS_CODES:
                    break
                location = str(response.headers.get("location") or "").strip()
                if not location:
                    return initial
                next_url = urljoin(current_url, location)
                # Validate the Location before a second request.  A protected
                # redirect is a hard block, not a compatibility fallback.
                if _validate_stream_target(next_url) is None:
                    return initial
                current_url = next_url
            else:
                return initial
            if response is None:
                return initial
            mime = response.headers.get("content-type", "")
            body = response.text[:4096] if response.content else ""
            final_url = current_url
            if is_hls_stream(final_url, mime, body):
                return analyze_stream_url(original, mime, body, final_url)
            path = _path(final_url)
            if path.endswith(".m3u") or body.lstrip().startswith("#EXTM3U"):
                best = _safe_best_url(_candidate_urls_from_m3u(body, final_url))
                return analyze_stream_url(original, "", "", best or final_url)
            if path.endswith(".pls") or "[playlist]" in body[:256].lower():
                best = _safe_best_url(_candidate_urls_from_pls(body, final_url))
                return analyze_stream_url(original, "", "", best or final_url)
            return analyze_stream_url(original, mime, body, final_url)
    except ProtectedStreamTarget:
        raise
    except Exception:
        return analyze_stream_url(original)


async def probe_stream_reachability(url: str, timeout: float = 3.0) -> dict:
    """Perform one explicit, SSRF-safe stream probe with bounded redirects."""

    original = str(url or "").strip()
    if not original:
        return {"status": "BROKEN", "reachable": False, "reason": "stream URL missing"}
    try:
        validation = _validate_stream_target(original)
    except ProtectedStreamTarget as exc:
        return {"status": "BROKEN", "reachable": False, "reason": str(exc), "protected": True}
    if validation is None:
        return {"status": "BROKEN", "reachable": False, "reason": "stream URL is invalid or unresolved"}
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            current_url = original
            for hop in range(MAX_STREAM_REDIRECTS + 1):
                validation = _validate_stream_target(current_url)
                if validation is None:
                    return {"status": "BROKEN", "reachable": False, "reason": "redirect target is invalid or unresolved"}
                pinned_url, pinned_headers, extensions = pinned_http_target(
                    current_url, validation
                )
                # A radio stream is intentionally endless and many stations
                # ignore Range. ``client.get`` would therefore wait for the
                # complete body and make the Preset Checker hang. Read at most
                # one 4 KiB preview and close the connection immediately.
                async with client.stream(
                    "GET",
                    pinned_url,
                    headers={
                        **pinned_headers,
                        "User-Agent": "basswiesn-preset-checker/2.5",
                        "Range": "bytes=0-4095",
                    },
                    extensions=extensions,
                ) as response:
                    if response.status_code in REDIRECT_STATUS_CODES:
                        location = str(response.headers.get("location") or "").strip()
                        if not location:
                            return {"status": "BROKEN", "reachable": False, "reason": "redirect has no Location", "http_status": response.status_code}
                        current_url = urljoin(current_url, location)
                        continue
                    preview = b""
                    async for chunk in response.aiter_bytes():
                        preview += chunk[: 4096 - len(preview)]
                        if len(preview) >= 4096 or chunk:
                            break
                    analysis = analyze_stream_url(
                        original,
                        response.headers.get("content-type", ""),
                        preview.decode("utf-8", errors="replace"),
                        current_url,
                    )
                    reachable = response.status_code < 400
                    response_status = response.status_code
                status = (
                    "BROKEN"
                    if not reachable or analysis.is_hls
                    else "WARNING"
                    if not analysis.is_direct_audio or analysis.compatibility_score < 70
                    else "VALID"
                )
                return {
                    "status": status,
                    "reachable": reachable,
                    "reason": analysis.compatibility_warning or ("HTTP response accepted" if reachable else "HTTP request failed"),
                    "http_status": response_status,
                    "resolved_url": current_url,
                    "mime": analysis.stream_mime,
                    "codec": analysis.stream_codec,
                    "compatibility_score": analysis.compatibility_score,
                    "redirects": hop,
                }
            return {"status": "BROKEN", "reachable": False, "reason": "too many redirects"}
    except ProtectedStreamTarget as exc:
        return {"status": "BROKEN", "reachable": False, "reason": str(exc), "protected": True}
    except httpx.TimeoutException:
        return {"status": "BROKEN", "reachable": False, "reason": "stream probe timed out"}
    except (httpx.HTTPError, OSError) as exc:
        return {"status": "BROKEN", "reachable": False, "reason": str(exc) or type(exc).__name__}
