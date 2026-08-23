from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from hashlib import sha256
import ipaddress
import os
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy.orm import Session

from basswiesn.app.models import ArtworkCacheEntry
from basswiesn.app.models.research_domain import redact_url
from basswiesn.app.services.network_security import UrlValidation, validate_public_callback_url


MAX_ARTWORK_BYTES = 2 * 1024 * 1024
DEFAULT_ARTWORK_TTL = timedelta(hours=24)
DEFAULT_SOURCE_ICON = "/static/bmx-icons/orion/monochrome.svg"
SAFE_REMOTE_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


class ArtworkSource(str, Enum):
    IMAGE_URL = "IMAGE_URL"
    PROVIDER = "PROVIDER"
    STATION = "STATION"
    SOURCE_ICON = "SOURCE_ICON"
    FALLBACK = "FALLBACK"


@dataclass(frozen=True, slots=True)
class ArtworkChoice:
    url: str
    source: ArtworkSource
    cacheable: bool
    webui_supported: bool = True
    radio_oled_supported: bool | None = None

    def to_dict(self, *, redact: bool = False) -> dict:
        return {
            "url": redact_url(self.url) if redact and self.cacheable else self.url,
            "source": self.source.value,
            "cacheable": self.cacheable,
            "webui_supported": self.webui_supported,
            # Deliberately unknown: a URL in BMX does not prove that a classic
            # SoundTouch OLED can render arbitrary bitmap artwork.
            "radio_oled_supported": self.radio_oled_supported,
        }


@dataclass(frozen=True, slots=True)
class ArtworkResult:
    choice: ArtworkChoice
    status: str
    public_url: str
    fetched_at: datetime | None = None
    expires_at: datetime | None = None
    failure_status: str | None = None
    cache_key: str | None = None

    def to_dict(self) -> dict:
        return {
            # Remote failures never return an operational URL (or its query
            # credentials) to the browser.  `public_url` is the only value a
            # WebUI should render.
            **self.choice.to_dict(redact=self.status == "FAILED"),
            "status": self.status,
            "public_url": self.public_url,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "failure_status": self.failure_status,
            "cache_key": self.cache_key,
        }


def choose_artwork(
    *,
    image_url: str | None = None,
    provider_artwork_url: str | None = None,
    station_logo_url: str | None = None,
    source_icon_url: str | None = None,
    fallback_icon_url: str = DEFAULT_SOURCE_ICON,
) -> ArtworkChoice:
    """Apply the WebUI-only artwork priority without implying OLED support."""

    for value, source in (
        (image_url, ArtworkSource.IMAGE_URL),
        (provider_artwork_url, ArtworkSource.PROVIDER),
        (station_logo_url, ArtworkSource.STATION),
    ):
        url = str(value or "").strip()
        if url:
            return ArtworkChoice(url=url, source=source, cacheable=True)
    source_icon = str(source_icon_url or "").strip()
    if source_icon:
        return ArtworkChoice(url=source_icon, source=ArtworkSource.SOURCE_ICON, cacheable=False)
    return ArtworkChoice(url=fallback_icon_url, source=ArtworkSource.FALLBACK, cacheable=False)


def artwork_cache_key(choice: ArtworkChoice, *, provider_id: str = "", station_id: str = "") -> str:
    material = "\x00".join((choice.source.value, choice.url, provider_id, station_id))
    return sha256(material.encode("utf-8")).hexdigest()


def _extension(content_type: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(content_type, ".img")


def _pinned_request(
    choice_url: str,
    validation: UrlValidation,
    *,
    address: str | None = None,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Build a request that connects to the address validated moments ago.

    Resolving a hostname once for validation and a second time inside httpx
    leaves a DNS-rebinding window.  Connecting to a validated address closes
    that gap.  The original Host header and TLS SNI keep virtual hosting and
    certificate verification intact.
    """

    parsed = urlsplit(choice_url)
    hostname = (parsed.hostname or "").encode("idna").decode("ascii")
    validated_hostname = str(validation.hostname or "").encode("idna").decode("ascii")
    if not hostname or hostname.lower() != validated_hostname.lower() or not validation.addresses:
        raise ArtworkFetchError("DNS_PIN_REQUIRED", "validated artwork address is unavailable")
    selected_address = address or validation.addresses[0]
    if selected_address not in validation.addresses:
        raise ArtworkFetchError(
            "DNS_PIN_REQUIRED", "artwork address was not part of validation"
        )
    try:
        address_value = ipaddress.ip_address(selected_address)
    except ValueError as exc:
        raise ArtworkFetchError("DNS_PIN_REQUIRED", "validated artwork address is malformed") from exc
    address_text = (
        f"[{address_value.compressed}]"
        if address_value.version == 6
        else address_value.compressed
    )
    explicit_port = parsed.port
    pinned_netloc = f"{address_text}:{explicit_port}" if explicit_port is not None else address_text
    default_port = 443 if parsed.scheme == "https" else 80
    host_header = hostname if explicit_port in {None, default_port} else f"{hostname}:{explicit_port}"
    pinned_url = urlunsplit((parsed.scheme, pinned_netloc, parsed.path or "/", parsed.query, ""))
    extensions = {"sni_hostname": hostname} if parsed.scheme == "https" else {}
    return pinned_url, {"Host": host_header, "Accept": ", ".join(sorted(SAFE_REMOTE_IMAGE_TYPES))}, extensions


def _looks_like_svg(body: bytes) -> bool:
    prefix = body[:4096].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    return prefix.startswith(b"<svg") or b"<svg" in prefix or b"<!doctype svg" in prefix


def _as_utc(value: datetime) -> datetime:
    # SQLite commonly returns naive values even for timezone-aware columns.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _entry_result(entry: ArtworkCacheEntry, choice: ArtworkChoice) -> ArtworkResult:
    path = Path(entry.cached_path) if entry.cached_path else None
    usable_file = bool(path and path.is_file() and not entry.failure_status)
    return ArtworkResult(
        choice=choice,
        status="HIT" if usable_file else "FAILED",
        public_url=f"/media/artwork-cache/{path.name}" if usable_file and path else DEFAULT_SOURCE_ICON,
        fetched_at=entry.fetched_at,
        expires_at=entry.expires_at,
        failure_status=entry.failure_status,
        cache_key=entry.cache_key,
    )


def _upsert_entry(db: Session, cache_key: str, **values: object) -> ArtworkCacheEntry:
    row = db.query(ArtworkCacheEntry).filter(ArtworkCacheEntry.cache_key == cache_key).one_or_none()
    if row is None:
        row = ArtworkCacheEntry(cache_key=cache_key)
        db.add(row)
    for name, value in values.items():
        setattr(row, name, value)
    row.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(row)
    return row


async def cache_artwork(
    db: Session,
    choice: ArtworkChoice,
    *,
    media_dir: Path,
    device_id: str | None = None,
    provider_id: str = "",
    station_id: str = "",
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_ARTWORK_TTL,
    transport: httpx.AsyncBaseTransport | None = None,
    validator: Callable[[str], UrlValidation] = validate_public_callback_url,
) -> ArtworkResult:
    """Fetch one WebUI artwork file with bounded size and persisted outcome.

    Relative source/fallback icons are shipped assets and never fetched. Remote
    redirects are intentionally rejected so each resolved target is validated.
    """

    if not choice.cacheable:
        return ArtworkResult(choice=choice, status="STATIC", public_url=choice.url)

    observed_at = now or datetime.now(UTC)
    key = artwork_cache_key(choice, provider_id=provider_id, station_id=station_id)
    existing = db.query(ArtworkCacheEntry).filter(ArtworkCacheEntry.cache_key == key).one_or_none()
    if existing is not None and existing.expires_at is not None and _as_utc(existing.expires_at) > _as_utc(observed_at):
        if existing.failure_status:
            # Negative-cache failures briefly so a broken/provider-blocked URL
            # cannot create a request loop whenever the page refreshes.
            return _entry_result(existing, choice)
        if existing.cached_path and Path(existing.cached_path).is_file():
            return _entry_result(existing, choice)

    validation = validator(choice.url)
    common = {
        "device_id": device_id,
        "provider_id": provider_id or None,
        "station_id": station_id or None,
        "source": choice.source.value,
        "source_url_hash": sha256(choice.url.encode("utf-8")).hexdigest(),
        "source_url_redacted": redact_url(choice.url),
    }
    if not validation.ok:
        previous_count = existing.failure_count if existing else 0
        row = _upsert_entry(
            db,
            key,
            **common,
            cached_path=None,
            fetched_at=observed_at,
            expires_at=observed_at + min(ttl, timedelta(minutes=15)),
            failure_status="URL_BLOCKED",
            failure_count=previous_count + 1,
            last_error=validation.reason[:300],
        )
        return _entry_result(row, choice)

    # A validator result without the immutable address set cannot close the
    # DNS-rebinding window.  Fail before constructing a client or attempting
    # any network transport; retrying is allowed only across addresses that
    # were part of the same successful validation result.
    if not validation.addresses:
        previous_count = existing.failure_count if existing else 0
        row = _upsert_entry(
            db,
            key,
            **common,
            cached_path=None,
            fetched_at=observed_at,
            expires_at=observed_at + min(ttl, timedelta(minutes=15)),
            failure_status="DNS_PIN_REQUIRED",
            failure_count=previous_count + 1,
            last_error="validated artwork address is unavailable",
        )
        return _entry_result(row, choice)

    cache_dir = media_dir / "artwork-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(
            timeout=5.0,
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        ) as client:
            last_transport_error: httpx.TransportError | OSError | None = None
            for address in validation.addresses:
                pinned_url, request_headers, request_extensions = _pinned_request(
                    choice.url, validation, address=address
                )
                try:
                    async with client.stream(
                        "GET",
                        pinned_url,
                        headers=request_headers,
                        extensions=request_extensions,
                    ) as response:
                        if response.status_code != 200:
                            raise ArtworkFetchError("HTTP_STATUS", f"HTTP {response.status_code}")
                        content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                        # SVG remains active XML content when served by a browser and
                        # can reference remote resources.  Artwork cache files are
                        # therefore restricted to inert raster formats.
                        if content_type == "image/svg+xml":
                            raise ArtworkFetchError("UNSAFE_IMAGE_TYPE", "remote SVG artwork is not allowed")
                        if content_type not in SAFE_REMOTE_IMAGE_TYPES:
                            raise ArtworkFetchError("NOT_IMAGE", "response is not an image")
                        declared = int(response.headers.get("content-length") or 0)
                        if declared > MAX_ARTWORK_BYTES:
                            raise ArtworkFetchError("TOO_LARGE", "declared image size exceeds limit")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > MAX_ARTWORK_BYTES:
                                raise ArtworkFetchError("TOO_LARGE", "image size exceeds limit")
                        if _looks_like_svg(bytes(body)):
                            raise ArtworkFetchError("UNSAFE_IMAGE_TYPE", "SVG content does not match the declared raster type")
                    break
                except (httpx.TransportError, OSError) as exc:
                    # Try only another address from the immutable result that
                    # already passed the public/protected-target policy gate.
                    last_transport_error = exc
            else:
                if last_transport_error is not None:
                    raise last_transport_error
                raise ArtworkFetchError("FETCH_ERROR", "artwork fetch failed")

        target = cache_dir / f"{key}{_extension(content_type)}"
        temporary = cache_dir / f".{key}.part"
        temporary.write_bytes(bytes(body))
        os.replace(temporary, target)
        row = _upsert_entry(
            db,
            key,
            **common,
            cached_path=str(target),
            mime_type=content_type,
            etag=(response.headers.get("etag") or "")[:300] or None,
            fetched_at=observed_at,
            expires_at=observed_at + ttl,
            failure_status=None,
            failure_count=0,
            last_error=None,
        )
        return ArtworkResult(
            choice=choice,
            status="FETCHED",
            public_url=f"/media/artwork-cache/{target.name}",
            fetched_at=row.fetched_at,
            expires_at=row.expires_at,
            cache_key=key,
        )
    except ArtworkFetchError as exc:
        failure_status, detail = exc.status, str(exc)
    except (httpx.HTTPError, OSError, ValueError):
        failure_status, detail = "FETCH_ERROR", "artwork fetch failed"

    previous_count = existing.failure_count if existing else 0
    row = _upsert_entry(
        db,
        key,
        **common,
        cached_path=None,
        fetched_at=observed_at,
        expires_at=observed_at + min(ttl, timedelta(minutes=15)),
        failure_status=failure_status,
        failure_count=previous_count + 1,
        last_error=detail[:300],
    )
    return _entry_result(row, choice)


class ArtworkFetchError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
