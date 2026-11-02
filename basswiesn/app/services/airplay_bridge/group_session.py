"""One explicitly enabled, bounded Apple-input group; no radio transport.

Native group labels are observations, not grants. The local owner chooses the
exact receiver/device bindings before accepting any input. Missing input is not
an empty group. A dead owner cannot be kept alive by healthy receiver processes.
Each instance is one-shot: reconnect and master departure need a new relay grant.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping

from .heartbeat import ReceiverHeartbeat
from .native_group import NativeGroup, NativeGroupCursor


class GroupSessionRejected(ValueError):
    def __init__(self, code: str = "AIRPLAY_GROUP_SESSION_UNVERIFIED") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ReceiverBinding:
    device_id: str
    nonce: str
    pid: int
    started: float


@dataclass(frozen=True)
class ReceiverSample:
    native: bytes
    heartbeat_sequence: int
    observed_monotonic: float
    process_alive: bool


@dataclass(frozen=True)
class GroupPlan:
    master: str
    members: tuple[str, ...]
    status: str


class GroupSession:
    """Local IPC consumer, not an HTTP request schema or discovery API.

The supervisor must verify the pinned executable, original child/process
identity and namespace lease before producing each sample. Its heartbeat must
advance independently of native revision: a paused live group can be unchanged.
An owner heartbeat is separate and never derived from receiver activity.
"""

    def __init__(self, bindings: tuple[ReceiverBinding, ...], *, master: str,
                 started: float, lifetime: float = 900) -> None:
        ids = tuple(item.device_id for item in bindings)
        if (not 1 <= len(ids) <= 16 or len(set(ids)) != len(ids) or master not in ids
                or any(not value or value != value.strip().upper() or len(value) > 128 for value in ids)
                or len({item.nonce for item in bindings}) != len(bindings)
                or type(started) not in (int, float) or not math.isfinite(started) or started < 0
                or type(lifetime) not in (int, float) or not math.isfinite(lifetime)
                or not 1 <= lifetime <= 900):
            raise GroupSessionRejected()
        self.device_ids, self.master = frozenset(ids), master
        self._cursors = {item.device_id: NativeGroupCursor(
            nonce=item.nonce, pid=item.pid, started=item.started) for item in bindings}
        if any(item.started > started for item in bindings):
            raise GroupSessionRejected()
        self._heartbeats = {identity: ReceiverHeartbeat() for identity in ids}
        self._owner = ReceiverHeartbeat()
        self._owner_observation: dict[str, int | float] | None = None
        self._samples: dict[str, ReceiverSample] = {}
        self._connections: dict[str, int] = {}
        self._group = ""
        self._now, self._deadline = started, started + lifetime
        self._failed = False
        self._terminal = False
        self._locally_stopped = False
        self.plan: GroupPlan | None = None

    def request_stop(self, *, now: float) -> GroupPlan:
        """Retire this grant without inventing a native disconnect observation.

        Only the existing local owner can request cleanup. The same receiver
        identities and fresh heartbeats remain required throughout teardown.
        No deadline extension or new connection is authorized here.
        """
        self.require_live(now=now)
        if not self._terminal:
            self._terminal = self._locally_stopped = True
            self.plan = GroupPlan(self.master, (), 'STOPPED')
        assert self.plan is not None
        return self.plan

    def _check_clock(self, now: float) -> None:
        if (self._failed or type(now) not in (int, float) or not math.isfinite(now)
                or not self._now <= now < self._deadline):
            raise GroupSessionRejected("AIRPLAY_GROUP_OWNER_EXPIRED")
        self._now = now

    def renew_owner(self, *, sequence: int, observed_monotonic: float, now: float) -> None:
        try:
            self._check_clock(now)
            # A terminal plan may still need bounded teardown readback. This
            # renews liveness, never a new session or a consumed relay grant.
            value = {"sequence": sequence, "observed_monotonic": observed_monotonic}
            # Expired observations cannot be revived by a later fresh heartbeat.
            if self._owner_observation is not None:
                self._owner.check(self._owner_observation, now=now)
            self._owner.check(value, now=now)
            self._owner_observation = value
        except ValueError:
            self._failed = True
            raise GroupSessionRejected("AIRPLAY_GROUP_OWNER_EXPIRED") from None

    def require_live(self, *, now: float) -> None:
        """Recheck before every awaited radio step, not only at plan creation."""
        try:
            self._check_clock(now)
            self._owner.check(self._owner_observation, now=now)
            if set(self._samples) != self.device_ids:
                raise GroupSessionRejected()
            for identity, sample in self._samples.items():
                self._heartbeats[identity].check({"sequence": sample.heartbeat_sequence,
                    "observed_monotonic": sample.observed_monotonic}, now=now)
        except ValueError:
            self._failed = True
            raise GroupSessionRejected() from None

    def observe(self, samples: Mapping[str, ReceiverSample], *, now: float) -> GroupPlan:
        try:
            self._check_clock(now)
            self._owner.check(self._owner_observation, now=now)
            if self._terminal or set(samples) != self.device_ids:
                raise GroupSessionRejected()
            records: dict[str, NativeGroup] = {}
            for identity, sample in samples.items():
                self._heartbeats[identity].check({"sequence": sample.heartbeat_sequence,
                    "observed_monotonic": sample.observed_monotonic}, now=now)
                record = self._cursors[identity].accept(sample.native, now=now,
                                                       process_alive=sample.process_alive)
                if record.connection:
                    old = self._connections.setdefault(identity, record.connection)
                    if old != record.connection:
                        raise GroupSessionRejected("AIRPLAY_NEW_RELAY_GRANT_REQUIRED")
                records[identity] = record
            self._samples = dict(samples)
            leader = records[self.master]
            if leader.state == "UNASSIGNED" and self._group:
                # Losing an established group can never continue its old grant.
                raise GroupSessionRejected("AIRPLAY_MASTER_GROUP_UNKNOWN")
            if leader.state in {"IDLE", "UNASSIGNED"}:
                # The native observer publishes ownership before SETUP supplies
                # its group UUID. Keep this initial transition held/waiting;
                # no group is inferred and the caller's bounded wait still
                # applies. All native and heartbeat checks above remain required.
                if self._group:
                    self._terminal = True
                    status = "HANDOFF_REQUIRED" if any(
                        item.state != "IDLE" for item in records.values()) else "STOPPED"
                else:
                    status = "WAITING"
                plan = GroupPlan(self.master, (), status)
            else:
                if self._group and leader.group_hash != self._group:
                    raise GroupSessionRejected("AIRPLAY_NEW_RELAY_GRANT_REQUIRED")
                self._group = leader.group_hash
                members = tuple(sorted(identity for identity, record in records.items()
                    if identity != self.master and record.state == "GROUPED"
                    and record.group_hash == self._group))
                plan = GroupPlan(self.master, members, "ACTIVE")
            # Fresh heartbeats must not invalidate a transaction whose native
            # membership is unchanged. A different plan still changes identity.
            if plan != self.plan:
                self.plan = plan
            assert self.plan is not None
            return self.plan
        except GroupSessionRejected:
            self._failed = True
            raise
        except (ValueError, TypeError):
            self._failed = True
            raise GroupSessionRejected() from None

    def refresh(self, samples: Mapping[str, ReceiverSample], *, now: float) -> GroupPlan:
        """Supervisor liveness during a radio await, including final teardown.

        Terminal native observations may stay the same or drain an already
        observed member of this group to IDLE. Native disconnects are not
        atomic across receivers. The terminal plan remains unchanged; a new
        connection/group or master resurrection can never revive its grant.
        Active membership changes invalidate the old plan.
        """
        if not self._terminal:
            return self.observe(samples, now=now)
        try:
            self._check_clock(now)
            self._owner.check(self._owner_observation, now=now)
            if set(samples) != self.device_ids or self.plan is None:
                raise GroupSessionRejected()
            for identity, sample in samples.items():
                cursor = self._cursors[identity]
                old = cursor.previous
                record = cursor.accept(sample.native, now=now, process_alive=sample.process_alive)
                departed = ((identity != self.master or self._locally_stopped) and old is not None
                    and old.state == 'GROUPED' and old.group_hash == self._group
                    and old.connection == self._connections.get(identity)
                    and record.state == 'IDLE')
                if record != old and not departed:
                    raise GroupSessionRejected("AIRPLAY_NEW_RELAY_GRANT_REQUIRED")
                self._heartbeats[identity].check({'sequence': sample.heartbeat_sequence,
                    'observed_monotonic': sample.observed_monotonic}, now=now)
            self._samples = dict(samples)
            return self.plan
        except ValueError:
            self._failed = True
            raise GroupSessionRejected() from None
