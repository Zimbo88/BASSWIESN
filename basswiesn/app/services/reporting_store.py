"""SQLAlchemy persistence adapter for :mod:`reporting_scheduler`.

The adapter persists one scheduler session at a time.  It never starts a
polling loop and never has a dependency on playback or radio transports.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import hmac
import json
import re
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from basswiesn.app.models import ReportingQueueEntry, ReportingState, utc_now
from basswiesn.app.repositories.research_state_repository import (
    ResearchStateRepository,
    aware_utc,
    dump_evidence,
    sanitize_operational_url,
)
from basswiesn.app.services.reporting_scheduler import (
    ReportPayload,
    ReportingQueueItem,
    ReportingSession,
    ReportingStatus,
)
REPORT_FIELDS = (
    "timeStamp",
    "eventType",
    "reason",
    "timeIntoTrack",
    "playbackDelay",
    "absolutePlayPoint",
    "reasonSubCode",
)

PERSISTED_REPORT_FORMAT = "basswiesn-bmx-report-v1"
PERSISTED_REPORT_REACQUIRE_REQUIRED = "PERSISTED_REPORT_REACQUIRE_REQUIRED"
_KNOWN_EVENT_TYPES = {"start", "stop", "timed"}
_PROTOCOL_TEXT = re.compile(r"^[A-Za-z0-9_.:+ -]{0,128}$")
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:https?://|(?:token|secret|password|passwd|credential|authorization|cookie|api[_-]?key)\s*[=:])"
)


def reporting_session_key(device_id: str, provider_id: str) -> str:
    if "::" in device_id or "::" in provider_id:
        raise ValueError("reporting key components must not contain '::'")
    return f"{device_id}::{provider_id}"


def split_reporting_session_key(key: str) -> tuple[str, str]:
    device_id, separator, provider_id = str(key).partition("::")
    if not separator or not device_id or not provider_id:
        raise ValueError("reporting session key must be '<device_id>::<provider_id>'")
    return device_id, provider_id


def _diagnostic_origin(value: str | None) -> str | None:
    """Persist only an origin hint, never a dynamic reporting path/query."""

    sanitized = sanitize_operational_url(value)
    if not sanitized:
        return None
    try:
        parsed = urlsplit(sanitized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        hostname = parsed.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, f"{hostname}{port}", "", "", ""))
    except (TypeError, ValueError):
        return None


def _safe_protocol_text(value: object, *, required: bool = False) -> bool:
    if not isinstance(value, str) or (required and not value):
        return False
    if not _PROTOCOL_TEXT.fullmatch(value):
        return False
    return _SENSITIVE_TEXT.search(value) is None


def _safe_wire_payload(payload: ReportPayload) -> dict | None:
    """Return an exact persistable payload, or ``None`` to fail closed.

    The seven BMX fields do not contain account/session identity.  We only
    persist their confirmed, bounded protocol shapes.  Arbitrary strings are
    not redacted and reused: they are rejected because redaction would change
    the provider contract and opaque strings could contain credentials.
    """

    wire = payload.as_dict()
    if set(wire) != set(REPORT_FIELDS):
        return None
    timestamp = wire.get("timeStamp")
    if not _safe_protocol_text(timestamp, required=True):
        return None
    try:
        datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    event_type = wire.get("eventType")
    if not _safe_protocol_text(event_type, required=True) or str(event_type).lower() not in _KNOWN_EVENT_TYPES:
        return None
    for name in ("reason", "absolutePlayPoint", "reasonSubCode"):
        if not _safe_protocol_text(wire.get(name)):
            return None
    for name in ("timeIntoTrack", "playbackDelay"):
        value = wire.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not -(1 << 31) <= value <= (1 << 31) - 1:
            return None
    return wire


def _canonical_wire(wire: dict) -> str:
    return json.dumps(wire, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _serialized_payload(payload: ReportPayload) -> tuple[str, bool]:
    wire = _safe_wire_payload(payload)
    if wire is None:
        return (
            json.dumps(
                {
                    "format": PERSISTED_REPORT_FORMAT,
                    "persistable": False,
                    "reason": PERSISTED_REPORT_REACQUIRE_REQUIRED,
                },
                separators=(",", ":"),
            ),
            False,
        )
    canonical = _canonical_wire(wire)
    return (
        json.dumps(
            {
                "format": PERSISTED_REPORT_FORMAT,
                "persistable": True,
                "wire_payload": wire,
                "sha256": sha256(canonical.encode("utf-8")).hexdigest(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        True,
    )


def _persisted_item_id(value: str) -> str:
    if _safe_protocol_text(value, required=True):
        return value
    return f"report:{sha256(str(value).encode('utf-8')).hexdigest()}"


class SqlAlchemyReportingStore:
    """Durable store implementing the scheduler's async ``save`` protocol."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    @classmethod
    def from_application_database(cls) -> "SqlAlchemyReportingStore":
        # Late import keeps tests and alternate deployments free to inject a
        # session factory before constructing the store.
        from basswiesn.app.db import SessionLocal

        return cls(SessionLocal)

    async def save(self, session: ReportingSession) -> None:
        device_id, provider_id = split_reporting_session_key(session.key)
        db = self._session_factory()
        try:
            row = (
                db.query(ReportingState)
                .filter(
                    ReportingState.device_id == device_id,
                    ReportingState.provider_id == provider_id,
                )
                .one_or_none()
            )
            if row is None:
                row = ReportingState(device_id=device_id, provider_id=provider_id)
                db.add(row)
            previous_state = row.state
            row.state = session.status.value
            if session.report_url is not None:
                row.report_url = _diagnostic_origin(session.report_url)
            row.queue_depth = min(20, session.queue_depth)
            row.retry_count = min(5, session.retry_count)
            row.next_due_at = (
                aware_utc(session.next_due_at) if session.next_due_at else None
            )
            row.last_http_status = session.last_http_status
            row.last_success_at = (
                aware_utc(session.last_success_at) if session.last_success_at else None
            )
            row.last_failure_json = (
                dump_evidence(
                    {
                        "code": session.last_failure,
                        "semantic_persistent": session.semantic_persistent_error,
                    }
                )
                if session.last_failure
                else None
            )
            row.generation = session.generation
            row.updated_at = utc_now()
            if previous_state != row.state:
                ResearchStateRepository(db).record_event(
                    device_id=device_id,
                    domain="REPORTING",
                    code=f"REPORT_{row.state}",
                    message=f"Reporting state changed to {row.state}.",
                    severity=(
                        "ERROR"
                        if row.state == ReportingStatus.FAILED.value
                        else "WARNING"
                        if row.state
                        in {
                            ReportingStatus.DEGRADED.value,
                            ReportingStatus.RETRY_WAIT.value,
                        }
                        else "INFO"
                    ),
                    evidence={
                        "previous_state": previous_state,
                        "queue_depth": row.queue_depth,
                        "retry_count": row.retry_count,
                        "next_due_at": row.next_due_at,
                        "last_http_status": row.last_http_status,
                    },
                    occurred_at=row.updated_at,
                )

            db.query(ReportingQueueEntry).filter(
                ReportingQueueEntry.device_id == device_id,
                ReportingQueueEntry.provider_id == provider_id,
            ).delete(synchronize_session=False)
            for slot, item in enumerate(session.queue[:20]):
                payload_json, persistable = _serialized_payload(item.payload)
                db.add(
                    ReportingQueueEntry(
                        item_id=_persisted_item_id(item.item_id),
                        device_id=device_id,
                        provider_id=provider_id,
                        generation=session.generation,
                        queue_slot=slot,
                        status=(
                            session.status.value if slot == 0 else "QUEUED"
                        ) if persistable else "REACQUIRE_REQUIRED",
                        event_type=item.payload.eventType if persistable else "NOT_PERSISTED",
                        reason=item.payload.reason if persistable else "NOT_PERSISTED",
                        payload_json=payload_json,
                        retry_count=min(5, item.retry_count),
                        next_attempt_at=(
                            aware_utc(item.due_at) if item.due_at else None
                        ),
                        last_http_status=session.last_http_status if slot == 0 else None,
                        last_error_json=(
                            dump_evidence({"code": item.last_error})
                            if item.last_error
                            else None
                        ),
                        # ``False`` means the persisted wire object is exact;
                        # unsafe values are represented by a content-free
                        # fail-closed marker and never restored.
                        redacted=not persistable,
                        created_at=aware_utc(item.created_at),
                        updated_at=utc_now(),
                    )
                )
            db.commit()
        finally:
            db.close()

    def load_sessions(self) -> list[ReportingSession]:
        """Load persisted due times and queues without scheduling them."""

        db = self._session_factory()
        sessions: list[ReportingSession] = []
        try:
            for row in db.query(ReportingState).order_by(ReportingState.id).all():
                try:
                    status = ReportingStatus(row.state)
                except ValueError:
                    status = ReportingStatus.DEGRADED
                session = ReportingSession(
                    key=reporting_session_key(row.device_id, row.provider_id),
                    # Only a redacted diagnostic URL is durable.  A restart
                    # must reacquire the current dynamic bmx_reporting link;
                    # sending to a stripped/tokenless URL would invent a
                    # provider contract and could leak traffic to the wrong
                    # endpoint.
                    report_url=None,
                    status=status,
                    next_due_at=row.next_due_at,
                    last_http_status=row.last_http_status,
                    last_success_at=row.last_success_at,
                    last_failure=_failure_code(row.last_failure_json),
                    semantic_persistent_error=_semantic_error(row.last_failure_json),
                    generation=row.generation,
                )
                # Operational dynamic links are deliberately never durable.
                # Any pending queue/timer therefore needs a fresh link even
                # if a previous restore already removed the diagnostic origin.
                if row.queue_depth or row.next_due_at:
                    session.status = ReportingStatus.DEGRADED
                    session.last_failure = "REPORT_URL_REFRESH_REQUIRED"
                queue_rows = (
                    db.query(ReportingQueueEntry)
                    .filter(
                        ReportingQueueEntry.device_id == row.device_id,
                        ReportingQueueEntry.provider_id == row.provider_id,
                    )
                    .order_by(ReportingQueueEntry.queue_slot)
                    .limit(20)
                    .all()
                )
                restored_queue: list[ReportingQueueItem] = []
                persisted_queue_invalid = False
                for queue_row in queue_rows:
                    payload = _payload(queue_row.payload_json)
                    if payload is None:
                        persisted_queue_invalid = True
                        continue
                    attempts = queue_row.retry_count
                    if queue_row.status in {
                        ReportingStatus.RETRY_WAIT.value,
                        ReportingStatus.SENDING.value,
                        ReportingStatus.DEGRADED.value,
                        ReportingStatus.FAILED.value,
                    }:
                        attempts += 1
                    restored_queue.append(
                        ReportingQueueItem(
                            item_id=queue_row.item_id,
                            payload=payload,
                            attempts=attempts,
                            due_at=queue_row.next_attempt_at,
                            last_error=_failure_code(queue_row.last_error_json),
                            created_at=queue_row.created_at or datetime.now(),
                        )
                    )
                if persisted_queue_invalid:
                    # Mixing retained valid entries with one altered/redacted
                    # entry would silently change ordering and reporting
                    # semantics.  Reacquire the provider contract instead.
                    session.queue.clear()
                    session.next_due_at = None
                    session.report_url = None
                    session.status = ReportingStatus.DEGRADED
                    session.last_failure = PERSISTED_REPORT_REACQUIRE_REQUIRED
                else:
                    session.queue.extend(restored_queue)
                sessions.append(session)
            return sessions
        finally:
            db.close()


def _stored_object(value: str | None) -> dict:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    if isinstance(decoded, list) and decoded and isinstance(decoded[0], dict):
        return decoded[0]
    return decoded if isinstance(decoded, dict) else {}


def _failure_code(value: str | None) -> str | None:
    code = _stored_object(value).get("code")
    return str(code) if code else None


def _semantic_error(value: str | None) -> bool:
    return bool(_stored_object(value).get("semantic_persistent", False))


def _payload(value: str | None) -> ReportPayload | None:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict) or set(decoded) != {
        "format",
        "persistable",
        "wire_payload",
        "sha256",
    }:
        return None
    if decoded.get("format") != PERSISTED_REPORT_FORMAT or decoded.get("persistable") is not True:
        return None
    wire = decoded.get("wire_payload")
    digest = decoded.get("sha256")
    if not isinstance(wire, dict) or not isinstance(digest, str):
        return None
    canonical = _canonical_wire(wire)
    if not hmac.compare_digest(sha256(canonical.encode("utf-8")).hexdigest(), digest):
        return None
    if set(wire) != set(REPORT_FIELDS):
        return None
    try:
        payload = ReportPayload(**wire)
    except (TypeError, ValueError):
        return None
    return payload if _safe_wire_payload(payload) == wire else None
