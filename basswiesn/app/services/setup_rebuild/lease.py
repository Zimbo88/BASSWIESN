"""Atomic SQLite ownership for setup/reboot coordination, not device success.

The application's database is SQLite. Decisions belong in conditional SQL,
never an ORM identity-map snapshot. A missing job is not proof its owner died:
reboot and startup-recovery workers intentionally have no setup-job row.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from basswiesn.app.models import SetupRebuildCoordinatorLease as Lease


class SetupLeaseLost(RuntimeError):
    """Stop at the next operation boundary; never replay the in-flight write."""


def acquire(db: Session, *, owner_id: str, job_id: str, ttl_seconds: int = 90) -> bool:
    if not owner_id or not job_id:
        raise ValueError("lease requires an owner and a job")
    now = datetime.now(UTC)
    db.execute(insert(Lease).values(lease_key="global").on_conflict_do_nothing(index_elements=["lease_key"]))
    claimed = db.execute(
        update(Lease).where(
            Lease.lease_key == "global",
            or_(
                and_(Lease.owner_id == "", Lease.job_id == ""),
                Lease.expires_at <= now,
                and_(Lease.owner_id == owner_id, Lease.job_id == job_id, Lease.expires_at > now),
            ),
        ).values(owner_id=owner_id, job_id=job_id,
                 expires_at=now + timedelta(seconds=max(10, ttl_seconds)), updated_at=now)
        .returning(Lease.id).execution_options(synchronize_session=False)
    ).scalar_one_or_none() is not None
    if claimed:
        db.commit()
    return claimed


def renew(db: Session, *, owner_id: str, job_id: str, ttl_seconds: int = 90) -> bool:
    now = datetime.now(UTC)
    renewed = db.execute(
        update(Lease).where(Lease.lease_key == "global", Lease.owner_id == owner_id,
                            Lease.job_id == job_id, Lease.expires_at > now)
        .values(expires_at=now + timedelta(seconds=max(10, ttl_seconds)), updated_at=now)
        .returning(Lease.id).execution_options(synchronize_session=False)
    ).scalar_one_or_none() is not None
    db.commit()
    return renewed


def release(db: Session, *, owner_id: str, job_id: str) -> None:
    db.execute(
        update(Lease).where(Lease.lease_key == "global", Lease.owner_id == owner_id, Lease.job_id == job_id)
        .values(owner_id="", job_id="", expires_at=None, updated_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    db.commit()


class LeaseWatch:
    """Fresh control sessions cannot commit an executor's partially staged state."""

    def __init__(self, session_factory: Callable[[], Session], owner_id: str, job_id: str) -> None:
        self.session_factory = session_factory
        self.owner_id = owner_id
        self.job_id = job_id
        self.lost = False

    def checkpoint(self) -> None:
        try:
            with self.session_factory() as db:
                owned = db.execute(select(Lease.id).where(
                    Lease.lease_key == "global", Lease.owner_id == self.owner_id,
                    Lease.job_id == self.job_id, Lease.expires_at > datetime.now(UTC),
                )).scalar_one_or_none() is not None
        except Exception:
            owned = False
        if self.lost or not owned:
            self.lost = True
            raise SetupLeaseLost("SETUP_LEASE_LOST: no following device operation; readback/manual review required")

    async def heartbeat(self, *, interval: float = 20.0) -> None:
        while not self.lost:
            await asyncio.sleep(interval)
            try:
                with self.session_factory() as db:
                    self.lost = not renew(db, owner_id=self.owner_id, job_id=self.job_id)
            except Exception:
                self.lost = True
