"""Private session-bound display projection shared by web/cloud workers.

Only the local playback owner publishes here, after its verified selection.
No HTTP ingress, receiver provisioning, radio transport or artwork fetch is
provided. Runtime rows are not playback evidence or a capability to control a
radio. Owner death/restart, expiry and selection changes invalidate the text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import time
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from basswiesn.app.core.errors import BasswiesnError
from basswiesn.app.models import Device, RuntimeState, Station
from basswiesn.app.services import device_ownership
from basswiesn.app.services.device_ownership import DeviceOwnership
from basswiesn.app.services.protected_devices import is_device_access_protected
from .heartbeat import ReceiverHeartbeat

PURPOSE = "airplay_bridge"
TEXT_FIELDS = frozenset(("track", "artist", "album", "genre"))
LEASE_SECONDS = 10.0


class DisplaySessionRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("AIRPLAY_DISPLAY_SESSION_UNVERIFIED")


class DisplayProjectionExpired(DisplaySessionRejected):
    """Only the local text lease expired; not receiver/identity validation."""


class _Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    version: Literal[1] = 1
    session: str = Field(pattern=r"^[a-f0-9]{32}$")
    device_id: str = Field(min_length=1, max_length=128)
    station_id: int = Field(gt=0)
    selection_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    owner_pid: int = Field(gt=0)
    owner_identity: str = Field(pattern=r"^[a-f0-9]{64}$")
    sequence: int = Field(ge=0, le=2**53 - 1)
    active: bool
    observed_monotonic: FiniteFloat = Field(ge=0)
    fields: dict[str, str]
    receiver_session: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, value: dict[str, str]) -> dict[str, str]:
        if (not set(value) <= TEXT_FIELDS or any(len(text) > 256 or
                any(not char.isprintable() for char in text) for text in value.values())):
            raise ValueError("invalid display fields")
        return value


class _ReceiverObservation(BaseModel):
    """Versioned private IPC only; arbitrary HTTP clients cannot publish it."""
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    version: Literal[1]
    session: str = Field(pattern=r"^[a-f0-9]{32}$")
    sequence: int = Field(ge=0, le=2**53 - 1)
    observed_monotonic: FiniteFloat = Field(ge=0)
    active: bool
    fields: dict[str, str]

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, value: dict[str, str]) -> dict[str, str]:
        return _Record.validate_fields(value)


def is_bridge_station(station: Station | None) -> bool:
    return bool(station is not None and getattr(station, "internal", False) is True
                and getattr(station, "lab_only", False) is True and station.purpose == PURPOSE)


def _key(device_id: str) -> str:
    return "ap2-display:" + sha256(device_id.strip().upper().encode()).hexdigest()


def _process_identity(pid: int) -> str:
    # The workers are separate processes in one container/PID namespace.
    # PID alone is reusable; bind to Linux boot and actual process start time.
    stat = Path(f"/proc/{pid}/stat").read_text()
    rest = stat[stat.rindex(")") + 2:].split()
    if rest[0] in {"Z", "X"}:
        raise DisplaySessionRejected()
    return sha256((Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                   + ":" + str(pid) + ":" + rest[19]).encode()).hexdigest()


def _fingerprint(device: Device, station: Station) -> str:
    return sha256(json.dumps([device.device_id, device.ip_address, station.id,
        station.name, station.stream_url, station.stream_url_resolved,
        station.provider, station.provider_station_id], separators=(",", ":")).encode()).hexdigest()


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


def _parse(raw: str) -> _Record:
    if len(raw) > 16384:
        raise DisplaySessionRejected()
    return _Record.model_validate(json.loads(raw, object_pairs_hook=_pairs))


def _live(record: _Record, now: float) -> bool:
    try:
        return bool(record.active and 0 <= now - record.observed_monotonic < LEASE_SECONDS
                    and record.owner_identity == _process_identity(record.owner_pid))
    except (OSError, ValueError, IndexError):
        return False


def _require_live(record: _Record, now: float) -> None:
    if _live(record, now):
        return
    try:
        if (record.active and now - record.observed_monotonic >= LEASE_SECONDS
                and record.owner_identity == _process_identity(record.owner_pid)):
            raise DisplayProjectionExpired()
    except (OSError, IndexError):
        pass
    raise DisplaySessionRejected()


def display_metadata(db: Session, device: Device, station: Station) -> dict[str, object] | None:
    """None: ordinary station; empty: AP2 session unavailable, never radio echo.

    This is a read-only optional projection. It neither schedules an ICY probe
    nor writes MetadataState. A healthy reporting response is not proof that
    the physical display rendered these fields.
    """
    if not is_bridge_station(station):
        return None
    try:
        if is_device_access_protected(device.ip_address, device.device_id):
            return {}
        raw = db.scalar(select(RuntimeState.value).where(RuntimeState.key == _key(device.device_id)))
        if raw is None:
            return {}
        record = _parse(raw)
        if (record.device_id != device.device_id or record.station_id != station.id
                or record.selection_hash != _fingerprint(device, station)
                or not _live(record, time.monotonic())):
            return {}
        return dict(record.fields)
    except (ValueError, TypeError, BasswiesnError):
        # Never echo invalid persisted metadata or fall back to an old song.
        return {}


@dataclass(repr=False)
class DisplayPublisher:
    """Local orchestration API, not a browser/device-facing write endpoint.

    Caller retains the central device lock and must publish/renew only while
    its verified receiver/session/selection remains healthy. The 10-second
    lease cannot be revived by late events. New sessions need new bindings.
    Text writes use compare-and-swap so an old END cannot erase a replacement.
    """

    _device_id: str
    _station_id: int
    _ownership: DeviceOwnership
    _record: _Record
    _serialized: str
    _heartbeat: ReceiverHeartbeat = field(default_factory=ReceiverHeartbeat, init=False)
    _receiver_previous: _ReceiverObservation | None = field(default=None, init=False)
    _receiver_rejected: bool = field(default=False, init=False)

    @classmethod
    def begin(cls, db: Session, device_id: str, station_id: int,
              ownership: DeviceOwnership, *, receiver_session: str | None = None) -> DisplayPublisher:
        device = db.scalar(select(Device).where(Device.device_id == device_id))
        station = db.get(Station, station_id)
        cls._guard(device, station, ownership)
        assert device is not None and station is not None
        now = time.monotonic()
        key = _key(device_id)
        existing = db.scalar(select(RuntimeState.value).where(RuntimeState.key == key))
        if existing is not None:
            try:
                if _live(_parse(existing), now):
                    raise DisplaySessionRejected()
            except (ValueError, TypeError):
                raise DisplaySessionRejected() from None
        try:
            record = _Record(session=secrets.token_hex(16), device_id=device_id,
                station_id=station_id, selection_hash=_fingerprint(device, station),
                owner_pid=os.getpid(), owner_identity=_process_identity(os.getpid()),
                sequence=0, active=True, observed_monotonic=now, fields={}, receiver_session=receiver_session)
        except (OSError, ValueError, IndexError):
            raise DisplaySessionRejected() from None
        raw = record.model_dump_json()
        if existing is None:
            db.add(RuntimeState(key=key, value=raw))
        else:
            count = db.query(RuntimeState).filter(RuntimeState.key == key,
                RuntimeState.value == existing).update({RuntimeState.value: raw}, synchronize_session=False)
            if count != 1:
                raise DisplaySessionRejected()
        db.commit()
        return cls(device_id, station_id, ownership, record, raw)

    @staticmethod
    def _guard(device: Device | None, station: Station | None, ownership: DeviceOwnership) -> None:
        if (device is None or not device.ip_address or not is_bridge_station(station)
                or not ownership.owned_by_current_task()
                or ownership.root != Path(os.path.abspath(device_ownership.lock_root()))
                or ownership.leaf != sha256(device.device_id.strip().upper().encode()).hexdigest() + ".lock"
                or is_device_access_protected(device.ip_address, device.device_id)):
            raise DisplaySessionRejected()

    def _write(self, db: Session, fields: dict[str, str], *, active: bool) -> None:
        if not self._ownership.owned_by_current_task() or not self._record.active:
            raise DisplaySessionRejected()
        if active:
            device = db.scalar(select(Device).where(Device.device_id == self._device_id))
            station = db.get(Station, self._station_id)
            if device is not None:
                db.refresh(device)
            if station is not None:
                db.refresh(station)
            self._guard(device, station, self._ownership)
            assert device is not None and station is not None
            if _fingerprint(device, station) != self._record.selection_hash:
                raise DisplaySessionRejected()
            _require_live(self._record, time.monotonic())
        record = _Record.model_validate({**self._record.model_dump(), "fields": fields,
            "active": active, "sequence": self._record.sequence + 1,
            "observed_monotonic": time.monotonic()})
        raw = record.model_dump_json()
        count = db.query(RuntimeState).filter(RuntimeState.key == _key(self._device_id),
            RuntimeState.value == self._serialized).update({RuntimeState.value: raw}, synchronize_session=False)
        if count != 1:
            raise DisplaySessionRejected()
        db.commit()
        self._record, self._serialized = record, raw

    def publish(self, db: Session, fields: Mapping[str, object]) -> None:
        """Replace one complete sanitized batch; absent fields clear old text."""
        if not set(fields) <= TEXT_FIELDS:
            raise DisplaySessionRejected()
        sanitized: dict[str, str] = {}
        for key, value in fields.items():
            if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
                raise DisplaySessionRejected()
            sanitized[key] = "".join(c for c in value if c.isprintable()).strip()[:256]
        self._write(db, sanitized, active=True)

    def renew(self, db: Session) -> None:
        self._write(db, dict(self._record.fields), active=True)

    def end(self, db: Session) -> None:
        self._write(db, {}, active=False)

    def accept_after_verified_readback(self, db: Session, observation: object) -> DisplayPublisher:
        """Owner-only projection rollover after fresh device/zone verification.

        Slow hardware transactions may outlive the optional text lease. Never
        revive that lease or lengthen it: retire it and create a new projection
        using the still-bound, fresh receiver observation. Keep replay cursors.
        Other failures (identity, receiver, ownership, CAS) remain fatal.
        This method performs no radio I/O and cannot establish readback itself.
        """
        try:
            self.accept_receiver(db, observation)
            return self
        except DisplayProjectionExpired:
            device = db.scalar(select(Device).where(Device.device_id == self._device_id))
            station = db.get(Station, self._station_id)
            if device is not None:
                db.refresh(device)
            if station is not None:
                db.refresh(station)
            self._guard(device, station, self._ownership)
            assert device is not None and station is not None
            if _fingerprint(device, station) != self._record.selection_hash:
                raise DisplaySessionRejected() from None
            self.end(db)  # CAS: cannot erase another publisher's replacement.
            renewed = self.begin(db, self._device_id, self._station_id, self._ownership,
                                 receiver_session=self._record.receiver_session)
            renewed._heartbeat = self._heartbeat
            renewed._receiver_previous = self._receiver_previous
            renewed.accept_receiver(db, observation)
            return renewed

    def accept_receiver(self, db: Session, observation: object) -> bool:
        """Project a versioned private receiver observation after live readback.

        The orchestrator calls this only while the owned radio selection is
        still verified. Fresh receiver text alone cannot establish that fact.
        The nonce comes from the exact privately provisioned receiver, never
        from track text, Bonjour identity, an HTTP header or an Apple group ID.
        Unchanged text is persisted at most every five seconds; stale or other
        sessions cannot keep a projection alive. True means DB text updated,
        NOT that a physical display was observed.
        """
        try:
            if (self._receiver_rejected or not self._record.active
                    or not self._ownership.owned_by_current_task()
                    or self._record.receiver_session is None):
                raise DisplaySessionRejected()
            value = _ReceiverObservation.model_validate(observation)
            if value.session != self._record.receiver_session:
                raise DisplaySessionRejected()
            previous = self._receiver_previous
            if previous is not None and value.sequence == previous.sequence and value != previous:
                raise DisplaySessionRejected()
            now = time.monotonic()
            self._heartbeat.check({"sequence": value.sequence,
                "observed_monotonic": value.observed_monotonic}, now=now)
            self._receiver_previous = value
            if not value.active:
                if value.fields:
                    raise DisplaySessionRejected()
                self.end(db)
                return True
            _require_live(self._record, now)
            if dict(value.fields) != self._record.fields or now - self._record.observed_monotonic >= 5:
                self.publish(db, value.fields)
                return True
            return False
        except DisplayProjectionExpired:
            self._receiver_rejected = True
            raise
        except (ValueError, TypeError):
            self._receiver_rejected = True
            raise DisplaySessionRejected() from None
