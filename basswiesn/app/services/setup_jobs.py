from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from basswiesn.app.models import SetupJob, SetupJobStep
from basswiesn.app.services.events import create_event


SETUP_JOB_STATUSES = {
    "pending", "validating", "running", "waiting", "verifying", "succeeded",
    "failed", "cancelled", "rollback_running", "rolled_back",
}


def _job_to_dict(job: SetupJob, steps: list[SetupJobStep] | None = None) -> dict[str, Any]:
    def load(value: str) -> Any:
        try:
            return json.loads(value or "{}")
        except ValueError:
            return {}
    return {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "device_id": job.device_id,
        "status": job.status,
        "user_request": load(job.user_request_json),
        "current_step": job.current_step,
        "progress": job.progress,
        "result": load(job.result_json),
        "error": job.error,
        "previous_state": load(job.previous_state_json),
        "after_state": load(job.after_state_json),
        "readback": load(job.readback_json),
        "rollback_status": job.rollback_status,
        "started_at": job.started_at.isoformat() if job.started_at else "",
        "ended_at": job.ended_at.isoformat() if job.ended_at else "",
        "created_at": job.created_at.isoformat() if job.created_at else "",
        "updated_at": job.updated_at.isoformat() if job.updated_at else "",
        "steps": [
            {
                "step_id": step.step_id,
                "name": step.name,
                "status": step.status,
                "order_index": step.order_index,
                "started_at": step.started_at.isoformat() if step.started_at else "",
                "ended_at": step.ended_at.isoformat() if step.ended_at else "",
                "error": step.error,
            }
            for step in (steps or [])
        ],
        "auto_resume": False,
    }


def create_setup_job(db: Session, payload: dict[str, Any]) -> dict:
    job_id = str(uuid4())
    steps = payload.get("steps") or [
        {"step_id": "validate", "name": "Vorprüfung"},
        {"step_id": "snapshot", "name": "Snapshot"},
        {"step_id": "apply", "name": "Änderung"},
        {"step_id": "readback", "name": "Read-back"},
        {"step_id": "verify", "name": "Verifikation"},
    ]
    job = SetupJob(
        job_id=job_id,
        job_type=str(payload.get("job_type") or "setup"),
        device_id=str(payload.get("device_id") or ""),
        status="pending",
        user_request_json=json.dumps(payload, ensure_ascii=False),
        progress=0,
    )
    db.add(job)
    for index, step in enumerate(steps):
        db.add(SetupJobStep(
            job_id=job_id,
            step_id=str(step.get("step_id") or f"step-{index + 1}"),
            name=str(step.get("name") or f"Schritt {index + 1}"),
            order_index=index,
        ))
    create_event(db, "setup_job_started", device_id=job.device_id, payload={"job_id": job_id, "status": "pending"})
    db.flush()
    persisted_steps = db.query(SetupJobStep).filter(SetupJobStep.job_id == job_id).order_by(SetupJobStep.order_index).all()
    return _job_to_dict(job, persisted_steps)


def list_setup_jobs(db: Session, *, limit: int = 100) -> list[dict]:
    jobs = db.query(SetupJob).order_by(SetupJob.updated_at.desc()).limit(min(max(limit, 1), 500)).all()
    result = []
    for job in jobs:
        steps = db.query(SetupJobStep).filter(SetupJobStep.job_id == job.job_id).order_by(SetupJobStep.order_index).all()
        result.append(_job_to_dict(job, steps))
    return result


def update_setup_job_status(db: Session, job_id: str, status: str, *, result: dict | None = None, error: str = "") -> dict:
    if status not in SETUP_JOB_STATUSES:
        raise ValueError("unsupported setup job status")
    job = db.query(SetupJob).filter(SetupJob.job_id == job_id).one_or_none()
    if job is None:
        raise ValueError("setup job not found")
    job.status = status
    job.updated_at = datetime.now(UTC)
    if status in {"running", "validating"} and job.started_at is None:
        job.started_at = datetime.now(UTC)
    if status in {"succeeded", "failed", "cancelled", "rolled_back"}:
        job.ended_at = datetime.now(UTC)
    if result is not None:
        job.result_json = json.dumps(result, ensure_ascii=False)
    if error:
        job.error = error
    if status == "succeeded":
        create_event(db, "setup_job_completed", device_id=job.device_id, payload={"job_id": job_id})
    elif status == "failed":
        create_event(db, "setup_job_failed", device_id=job.device_id, severity="error", payload={"job_id": job_id, "error": error})
    steps = db.query(SetupJobStep).filter(SetupJobStep.job_id == job.job_id).order_by(SetupJobStep.order_index).all()
    return _job_to_dict(job, steps)
