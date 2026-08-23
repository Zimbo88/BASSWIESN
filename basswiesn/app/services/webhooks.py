from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import json
from pathlib import Path
import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import Event, WebhookDelivery, WebhookEndpoint
from basswiesn.app.services.events import event_to_dict
from basswiesn.app.services.network_security import (
    pinned_http_target,
    validate_public_callback_url,
)


MAX_WEBHOOK_PAYLOAD_BYTES = 64 * 1024
MAX_WEBHOOK_RESPONSE_BYTES = 128 * 1024


def _secret_dir() -> Path:
    path = get_settings().data_dir / "secrets" / "webhooks"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _secret_path(endpoint_id: int) -> Path:
    return _secret_dir() / f"{endpoint_id}.secret"


def _write_secret(endpoint_id: int, secret: str) -> str:
    path = _secret_path(endpoint_id)
    path.write_text(secret, encoding="utf-8")
    path.chmod(0o600)
    return f"local-file:{path.name}"


def _read_secret(secret_ref: str, endpoint_id: int) -> str:
    if not secret_ref.startswith("local-file:"):
        return ""
    path = _secret_path(endpoint_id)
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def endpoint_to_dict(endpoint: WebhookEndpoint, *, include_secret_state: bool = True) -> dict[str, Any]:
    try:
        event_types = json.loads(endpoint.event_types_json or "[]")
    except ValueError:
        event_types = []
    try:
        allowlist = json.loads(endpoint.allowlist_json or "[]")
    except ValueError:
        allowlist = []
    return {
        "id": endpoint.id,
        "name": endpoint.name,
        "url": endpoint.url,
        "enabled": endpoint.enabled,
        "event_types": event_types,
        "allowlist": allowlist,
        "secret_configured": bool(endpoint.secret_ref) if include_secret_state else False,
        "created_at": endpoint.created_at.isoformat() if endpoint.created_at else "",
        "updated_at": endpoint.updated_at.isoformat() if endpoint.updated_at else "",
        "last_error": endpoint.last_error,
    }


def validate_webhook_target(url: str, allowed_hosts: set[str] | None = None) -> dict:
    validation = validate_public_callback_url(url, allowed_hosts=allowed_hosts)
    return validation.to_dict()


def upsert_webhook_endpoint(db: Session, payload: dict[str, Any], *, endpoint_id: int | None = None) -> WebhookEndpoint:
    allowed_hosts = set(get_settings().webhook_allowed_hosts)
    validation = validate_public_callback_url(str(payload.get("url", "")), allowed_hosts=allowed_hosts or None)
    if not validation.ok:
        raise ValueError(validation.reason)
    endpoint = db.query(WebhookEndpoint).filter(WebhookEndpoint.id == endpoint_id).one_or_none() if endpoint_id else None
    if endpoint is None:
        endpoint = WebhookEndpoint()
        db.add(endpoint)
        db.flush()
    endpoint.name = str(payload.get("name") or "Webhook").strip()[:120]
    endpoint.url = str(payload.get("url") or "").strip()
    endpoint.enabled = bool(payload.get("enabled", False))
    endpoint.event_types_json = json.dumps(list(payload.get("event_types") or []), ensure_ascii=False)
    endpoint.allowlist_json = json.dumps(list(payload.get("allowlist") or []), ensure_ascii=False)
    endpoint.updated_at = datetime.now(UTC)
    secret = str(payload.get("secret") or "")
    if secret:
        endpoint.secret_ref = _write_secret(endpoint.id, secret)
    return endpoint


def _payload_for_event(event: Event) -> bytes:
    payload = event_to_dict(event)
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_WEBHOOK_PAYLOAD_BYTES:
        raise ValueError("webhook payload too large")
    return data


def _headers(data: bytes, secret: str, event_id: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"basswiesn/{get_settings().version}",
        "X-BASSWIESN-Event-ID": event_id,
        "X-BASSWIESN-Timestamp": datetime.now(UTC).isoformat(),
    }
    if secret:
        digest = hmac.new(secret.encode("utf-8"), data, hashlib.sha256).hexdigest()
        headers["X-BASSWIESN-Signature"] = f"sha256={digest}"
    return headers


async def deliver_webhook(db: Session, endpoint: WebhookEndpoint, event: Event) -> dict:
    delivery = WebhookDelivery(endpoint_id=endpoint.id, event_id=event.event_id, status="pending", attempt=1)
    db.add(delivery)
    db.flush()
    if not get_settings().webhooks_enabled or not endpoint.enabled:
        delivery.status = "skipped"
        delivery.error = "webhooks disabled" if not get_settings().webhooks_enabled else "endpoint disabled"
        return {"ok": False, "skipped": True, "reason": delivery.error}
    validation = validate_public_callback_url(endpoint.url, allowed_hosts=set(get_settings().webhook_allowed_hosts) or None)
    if not validation.ok:
        endpoint.last_error = validation.reason
        delivery.status = "failed"
        delivery.error = validation.reason
        return {"ok": False, "error": validation.reason}
    data = _payload_for_event(event)
    secret = _read_secret(endpoint.secret_ref, endpoint.id)
    started = time.monotonic()
    try:
        pinned_url, pinned_headers, extensions = pinned_http_target(
            endpoint.url, validation
        )
        async with httpx.AsyncClient(
            timeout=get_settings().webhook_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                pinned_url,
                content=data,
                headers={**pinned_headers, **_headers(data, secret, event.event_id)},
                extensions=extensions,
            )
            content = response.content
            if len(content) > MAX_WEBHOOK_RESPONSE_BYTES:
                raise ValueError("webhook response too large")
        delivery.status_code = response.status_code
        delivery.duration_ms = int((time.monotonic() - started) * 1000)
        delivery.status = "delivered" if 200 <= response.status_code < 300 else "failed"
        delivery.delivered_at = datetime.now(UTC) if delivery.status == "delivered" else None
        if delivery.status == "failed":
            endpoint.last_error = f"HTTP {response.status_code}"
        return {"ok": delivery.status == "delivered", "status_code": response.status_code, "duration_ms": delivery.duration_ms}
    except Exception as exc:
        delivery.status = "failed"
        delivery.error_class = exc.__class__.__name__
        delivery.error = str(exc)[:500]
        delivery.duration_ms = int((time.monotonic() - started) * 1000)
        endpoint.last_error = delivery.error
        return {"ok": False, "error_class": delivery.error_class, "error": delivery.error}


async def deliver_event_to_matching_webhooks(db: Session, event: Event) -> list[dict]:
    endpoints = db.query(WebhookEndpoint).filter(WebhookEndpoint.enabled == True).all()  # noqa: E712
    results = []
    for endpoint in endpoints:
        try:
            event_types = set(json.loads(endpoint.event_types_json or "[]"))
        except ValueError:
            event_types = set()
        if event_types and event.event_type not in event_types:
            continue
        results.append(await deliver_webhook(db, endpoint, event))
    return results
