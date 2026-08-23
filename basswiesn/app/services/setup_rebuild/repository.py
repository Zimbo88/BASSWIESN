"""SQLAlchemy persistence for the setup rebuild coordinator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from basswiesn.app.models import (
    SetupRebuildCoordinatorLease,
    SetupRebuildDeviceState,
    SetupRebuildJob,
    utc_now,
)
from basswiesn.app.services.setup_rebuild.candidates import selected_setup_devices
from .states import MultiDeviceSetupPhase, SetupState, multi_device_phase


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: str, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


class SetupRepository:
    """Small transaction-oriented repository; callers commit each checkpoint."""

    def create_job(
        self,
        db: Session,
        *,
        device_ids: list[str],
        target_server: dict[str, Any],
        plan: dict[str, Any],
    ) -> SetupRebuildJob:
        normalized = [str(item or "").strip().upper() for item in device_ids]
        if not normalized or len(normalized) != len(set(normalized)):
            raise ValueError("Bitte mindestens ein Radio genau einmal auswählen.")
        selected = selected_setup_devices(db, normalized, require_eligible=True)
        by_id = {
            str(row.device_id).strip().upper(): (row, candidate)
            for row, candidate in selected
        }
        job = SetupRebuildJob(
            job_id=str(uuid4()),
            status="pending",
            selected_device_ids_json=_json(normalized),
            plan_json=_json(plan),
            target_server_json=_json(target_server),
            current_state=SetupState.UNKNOWN.value,
        )
        db.add(job)
        for device_id in normalized:
            row, candidate = by_id[device_id]
            db.add(
                SetupRebuildDeviceState(
                    job_id=job.job_id,
                    device_id=device_id,
                    ip_address=candidate.ip_address,
                    expected_model=str(row.model or candidate.model),
                    state=SetupState.UNKNOWN.value,
                )
            )
        db.flush()
        return job

    def job(self, db: Session, job_id: str) -> SetupRebuildJob | None:
        return db.query(SetupRebuildJob).filter(SetupRebuildJob.job_id == job_id).one_or_none()

    def states(self, db: Session, job_id: str) -> list[SetupRebuildDeviceState]:
        return (
            db.query(SetupRebuildDeviceState)
            .filter(SetupRebuildDeviceState.job_id == job_id)
            .order_by(SetupRebuildDeviceState.id)
            .all()
        )

    def state(self, db: Session, job_id: str, device_id: str) -> SetupRebuildDeviceState | None:
        return (
            db.query(SetupRebuildDeviceState)
            .filter(
                SetupRebuildDeviceState.job_id == job_id,
                SetupRebuildDeviceState.device_id == device_id,
            )
            .one_or_none()
        )

    def transition_state(
        self,
        db: Session,
        row: SetupRebuildDeviceState,
        target: SetupState,
        *,
        error: str = "",
    ) -> None:
        from .states import transition

        row.state = transition(row.state, target).value
        row.last_error = error
        row.updated_at = utc_now()

    def acquire_lease(
        self,
        db: Session,
        *,
        owner_id: str,
        job_id: str,
        ttl_seconds: int = 90,
    ) -> bool:
        now = datetime.now(UTC)
        row = (
            db.query(SetupRebuildCoordinatorLease)
            .filter(SetupRebuildCoordinatorLease.lease_key == "global")
            .one_or_none()
        )
        if row is None:
            row = SetupRebuildCoordinatorLease(lease_key="global")
            db.add(row)
            db.flush()
        expires_at = row.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at and expires_at > now and row.owner_id not in {"", owner_id}:
            # A cancelled/completed job may have been stopped between a
            # checkpoint and lease release (for example during a container
            # restart). Its lease must not block the next explicit setup run.
            # A merely cancel-requested *running* job is kept locked until its
            # executor reaches a checkpoint and releases the lease itself.
            existing = (
                db.query(SetupRebuildJob)
                .filter(SetupRebuildJob.job_id == row.job_id)
                .one_or_none()
            )
            if existing is None or existing.status in {
                "cancelled",
                "completed",
                "failed",
                "rolled_back",
            }:
                row.owner_id = ""
                row.job_id = ""
                row.expires_at = None
                row.updated_at = now
            else:
                return False
        row.owner_id = owner_id
        row.job_id = job_id
        row.expires_at = now + timedelta(seconds=max(10, ttl_seconds))
        row.updated_at = now
        db.commit()
        return True

    def renew_lease(self, db: Session, *, owner_id: str, job_id: str, ttl_seconds: int = 90) -> bool:
        row = (
            db.query(SetupRebuildCoordinatorLease)
            .filter(
                SetupRebuildCoordinatorLease.lease_key == "global",
                SetupRebuildCoordinatorLease.owner_id == owner_id,
                SetupRebuildCoordinatorLease.job_id == job_id,
            )
            .one_or_none()
        )
        if row is None:
            return False
        row.expires_at = datetime.now(UTC) + timedelta(seconds=max(10, ttl_seconds))
        row.updated_at = datetime.now(UTC)
        db.commit()
        return True

    def release_lease(self, db: Session, *, owner_id: str, job_id: str) -> None:
        row = (
            db.query(SetupRebuildCoordinatorLease)
            .filter(
                SetupRebuildCoordinatorLease.lease_key == "global",
                SetupRebuildCoordinatorLease.owner_id == owner_id,
                SetupRebuildCoordinatorLease.job_id == job_id,
            )
            .one_or_none()
        )
        if row is not None:
            row.owner_id = ""
            row.job_id = ""
            row.expires_at = None
            row.updated_at = datetime.now(UTC)
            db.commit()

    def public_job(self, db: Session, job: SetupRebuildJob) -> dict[str, Any]:
        states = self.states(db, job.job_id)
        verified = [row.device_id for row in states if row.state == SetupState.VERIFIED.value]
        failed = [row.device_id for row in states if row.state == SetupState.FAILED.value]
        if job.status in {"completed", "partial_failure"}:
            phase = MultiDeviceSetupPhase.COMPLETE.value
        elif job.status == "failed":
            phase = MultiDeviceSetupPhase.FAILED.value
        elif job.status.startswith("rollback") or job.status == "rolled_back":
            phase = MultiDeviceSetupPhase.ROLLBACK.value
        else:
            try:
                phase = multi_device_phase(job.current_state).value
            except ValueError:
                phase = MultiDeviceSetupPhase.QUEUED.value
        return {
            "job_type": "MultiDeviceSetupJob",
            "job_id": job.job_id,
            "status": job.status,
            "phase": phase,
            "progress": job.progress,
            "current_device_id": job.current_device_id,
            "current_state": job.current_state,
            "selected_device_ids": _load(job.selected_device_ids_json, []),
            "target_server": _load(job.target_server_json, {}),
            "plan": _load(job.plan_json, {}),
            "result": _load(job.result_json, {}),
            "error": job.error,
            "cancel_requested": bool(job.cancel_requested),
            "created_at": job.created_at.isoformat() if job.created_at else "",
            "started_at": job.started_at.isoformat() if job.started_at else "",
            "ended_at": job.ended_at.isoformat() if job.ended_at else "",
            "summary": {
                "total": len(states),
                "verified": len(verified),
                "failed": len(failed),
                "pending": max(0, len(states) - len(verified) - len(failed)),
                "verified_device_ids": verified,
                "failed_device_ids": failed,
            },
            "devices": [
                {
                    "device_id": row.device_id,
                    "ip_address": row.ip_address,
                    "expected_model": row.expected_model,
                    "state": row.state,
                    "phase": multi_device_phase(row.state).value,
                    "ssh_status": row.ssh_status,
                    "ssh_profile_key": row.ssh_profile_key,
                    "routing_status": row.routing_status,
                    "backup_path": row.backup_path,
                    "backup_sha256": _load(row.backup_sha256_json, {}),
                    "evidence": _load(row.evidence_json, {}),
                    "last_error": row.last_error,
                    "recovery_status": row.recovery_status,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                }
                for row in states
            ],
        }
