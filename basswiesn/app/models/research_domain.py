from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit


def _now() -> datetime:
    return datetime.now(UTC)


def _redacted_secret(value: str | None) -> str | None:
    return "<redacted>" if value else None


def redact_url(value: str | None) -> str | None:
    """Return a useful diagnostic URL without credentials, query or fragment."""

    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            return "<redacted>"
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit(SplitResult(parsed.scheme, f"{host}{port}", parsed.path, "", ""))
    except (TypeError, ValueError):
        return "<redacted>"


@dataclass(frozen=True, slots=True)
class DeviceCapabilities:
    """Normalized research capability view; ``None`` means not observed."""

    sources: tuple[str, ...] = ()
    has_display: bool | None = None
    has_clock: bool | None = None
    has_battery: bool | None = None
    supports_multiroom: bool | None = None
    supports_bluetooth: bool | None = None
    supports_airplay: bool | None = None
    metadata_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sources"] = list(self.sources)
        data["metadata_fields"] = list(self.metadata_fields)
        return data


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    source_id: str
    display_name: str | None = None
    source_account: str | None = None
    provider_id: str | None = None
    available: bool = False
    presetable: bool | None = None
    multiroom_allowed: bool | None = None
    metadata_capability: str = "UNKNOWN"

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("source_id is required")

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if redact:
            data["source_account"] = _redacted_secret(self.source_account)
        return data


@dataclass(frozen=True, slots=True)
class StreamDescriptor:
    url: str
    has_playlist: bool = False
    realtime: bool = False
    auto_select: bool = False
    connection_timeout_s: int = 20
    buffering_timeout_s: int = 30
    metadata_mode: str = "IGNORE"
    start_at_live_point: bool = True
    probe_content_type: str | None = None
    probe_codec: str | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("stream url must be an absolute HTTP(S) URL")
        if self.connection_timeout_s < 0 or self.buffering_timeout_s < 0:
            raise ValueError("stream timeouts must not be negative")

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if redact:
            data["url"] = redact_url(self.url)
        return data


@dataclass(frozen=True, slots=True)
class PresetDescriptor:
    slot: int
    source: str
    location: str
    source_account: str | None = None
    item_name: str | None = None
    container_art: str | None = None
    normalized_at: datetime | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.slot <= 255:
            raise ValueError("preset slot must fit uint8")
        if not self.source.strip() or not self.location.strip():
            raise ValueError("preset source and location are required")

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if redact:
            data["source_account"] = _redacted_secret(self.source_account)
            data["location"] = redact_url(self.location) or "<redacted>"
            data["container_art"] = redact_url(self.container_art)
        if self.normalized_at is not None:
            data["normalized_at"] = self.normalized_at.isoformat()
        return data


@dataclass(frozen=True, slots=True)
class ZoneMember:
    device_id: str
    role: str = "NORMAL"
    ip_address: str | None = None
    registered: bool = False
    connected: bool = False
    volume: int | None = None
    mute: bool | None = None
    output_latency_ms: int | None = None
    last_seen: datetime | None = None

    def __post_init__(self) -> None:
        if not self.device_id.strip():
            raise ValueError("zone member device_id is required")
        if self.volume is not None and not 0 <= self.volume <= 100:
            raise ValueError("volume must be between 0 and 100")

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if redact and self.ip_address:
            data["ip_address"] = "<redacted>"
        if self.last_seen is not None:
            data["last_seen"] = self.last_seen.isoformat()
        return data


@dataclass(frozen=True, slots=True)
class ZoneState:
    group_id: str | None = None
    master_device_id: str | None = None
    members: tuple[ZoneMember, ...] = field(default_factory=tuple)
    source: SourceDescriptor | None = None
    clock_master: str | None = None
    state: str = "UNGROUPED"
    updated_at: datetime = field(default_factory=_now)

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "master_device_id": self.master_device_id,
            "members": [member.to_dict(redact=redact) for member in self.members],
            "source": self.source.to_dict(redact=redact) if self.source else None,
            "clock_master": "<redacted>" if redact and self.clock_master else self.clock_master,
            "state": self.state,
            "updated_at": self.updated_at.isoformat(),
        }
