"""Request-local history identity projection, never a persistent state cache."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from basswiesn.app.models import PlayHistory
from basswiesn.app.services.playback_identity import PlaybackIdentity, clean_station_name, resolve_playback_identity


@dataclass(frozen=True)
class HistoryIdentityInput:
    station_id: int | None
    station_name: str
    stream_url: str
    source: str
    source_account: str
    content_item_name: str
    device_id: str
    preset_button: int | None
    internal_event: bool
    is_confirmed: bool

    @classmethod
    def from_row(cls, row: PlayHistory) -> HistoryIdentityInput:
        snapshot_name = getattr(row, "station_display_name", "")
        if not clean_station_name(snapshot_name):
            snapshot_name = getattr(row, "station_name", "")
        return cls(
            station_id=getattr(row, "station_id", None), station_name=snapshot_name,
            stream_url=getattr(row, "stream_url", ""), source=getattr(row, "source", ""),
            source_account=getattr(row, "source_account", ""),
            content_item_name=getattr(row, "content_item_name", "") or getattr(row, "preset_name", ""),
            device_id=getattr(row, "device_id", ""), preset_button=getattr(row, "preset_button", None),
            internal_event=bool(getattr(row, "internal_event", False) or getattr(row, "is_internal", False)),
            is_confirmed=bool(getattr(row, "is_confirmed", True)),
        )

    def resolve(self, db: Session) -> PlaybackIdentity:
        return resolve_playback_identity(db, station_id=self.station_id, station_name=self.station_name,
            stream_url=self.stream_url, source=self.source, source_account=self.source_account,
            content_item_name=self.content_item_name, device_id=self.device_id, preset_button=self.preset_button,
            internal_event=self.internal_event, is_confirmed=self.is_confirmed)


class HistoryIdentityProjection:
    """Own one bounded projection per read request; discard it before any write.

    All identity-relevant inputs form the key. Current station/preset lookups
    are reused only within this projection, never across requests or sessions.
    Time/duration/volume fields remain independently serialized from every row.
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        self._resolved: dict[HistoryIdentityInput, PlaybackIdentity] = {}

    def resolve(self, row: PlayHistory) -> PlaybackIdentity:
        key = HistoryIdentityInput.from_row(row)
        if key not in self._resolved:
            if len(self._resolved) >= 512:
                # No unbounded cache if another caller uses the projection for
                # a large export. Keeping no entry preserves resolution semantics.
                return key.resolve(self.db)
            self._resolved[key] = key.resolve(self.db)
        return self._resolved[key]
