"""Construct optional providers lazily; disabled defaults cause no outbound I/O."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from basswiesn.app.models import Device, Station

from .contracts import ProviderError, ProviderErrorCode
from .local import LocalStation, LocalStationAdapter
from .manager import ProviderManager
from .music_assistant import MusicAssistantAdapter
from .transport import BoundedJSONClient, TargetPolicy


@dataclass(frozen=True)
class ProviderRuntimeConfig:
    """Dedicated credentials never enter public Settings or diagnostics dumps."""

    music_assistant_enabled: bool = False
    music_assistant_url: str = field(default="", repr=False)
    music_assistant_token: str = field(default="", repr=False)
    music_assistant_allowed_ips: frozenset[str] = field(default_factory=frozenset, repr=False)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> ProviderRuntimeConfig:
        source = os.environ if environment is None else environment
        return cls(
            music_assistant_enabled=source.get("MUSIC_ASSISTANT_ENABLED", "false").lower() in {"true", "1"},
            music_assistant_url=source.get("MUSIC_ASSISTANT_URL", ""),
            music_assistant_token=source.get("MUSIC_ASSISTANT_TOKEN", ""),
            music_assistant_allowed_ips=frozenset(
                item.strip() for item in source.get("MUSIC_ASSISTANT_ALLOWED_IPS", "").split(",") if item.strip()
            ),
        )


def _session() -> Session:
    # Resolve current runtime/test factory, not a stale import-time connection.
    from basswiesn.app.db import SessionLocal
    result: Session = SessionLocal()
    return result


def build_provider_manager(
    config: ProviderRuntimeConfig | None = None, *, session_factory: Callable[[], Session] = _session,
) -> ProviderManager:
    config = config if config is not None else ProviderRuntimeConfig.from_environment()

    def blocked_addresses() -> frozenset[str]:
        with session_factory() as db:
            # Every known radio is excluded, even if it is not specially protected.
            # A failed inventory read propagates into the fail-closed target gate.
            return frozenset(str(row[0]) for row in db.query(Device.ip_address).all() if row[0])

    def read_stations(query: str, item_id: str, limit: int, offset: int) -> tuple[LocalStation, ...]:
        with session_factory() as db:
            rows = db.query(Station).filter(Station.internal.is_(False), Station.lab_only.is_(False))
            if item_id:
                rows = rows.filter(Station.id == int(item_id))
            if query:
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                rows = rows.filter(Station.name.ilike(f"%{escaped}%", escape="\\"))
            result = rows.order_by(Station.name, Station.id).offset(offset).limit(limit).all()
            return tuple(LocalStation(
                str(row.id), str(row.name), str(row.stream_url_resolved or row.stream_url), str(row.stream_mime or ""),
            ) for row in result)

    local = LocalStationAdapter(read_stations, TargetPolicy(blocked_addresses=blocked_addresses))
    modern = MusicAssistantAdapter()
    if config.music_assistant_enabled:
        try:
            policy = TargetPolicy(
                origin=config.music_assistant_url, allowed_ips=config.music_assistant_allowed_ips,
                blocked_addresses=blocked_addresses,
            )
            modern = MusicAssistantAdapter(
                BoundedJSONClient(policy, token=config.music_assistant_token), enabled=True,
            )
        except ProviderError as exc:
            modern = MusicAssistantAdapter(enabled=True, configuration_error=exc.code)
        except Exception:
            modern = MusicAssistantAdapter(enabled=True, configuration_error=ProviderErrorCode.UNAVAILABLE)
    return ProviderManager((local, modern))
