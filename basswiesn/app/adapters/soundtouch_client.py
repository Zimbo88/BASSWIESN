"""HTTP adapter for the local SoundTouch device API."""

from datetime import UTC, datetime
import re
import time
from xml.etree import ElementTree as ET

import httpx

from basswiesn.app.config import get_settings
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.services.network_security import (
    pinned_http_target,
    validate_outbound_http_url,
)
from basswiesn.app.services.protected_devices import reject_protected_device_access
from basswiesn.app.services.support_export import redact_text


SECRET_RE = re.compile(
    r"(?i)(password|passphrase|token|secret|authorization|cookie|api_key|private_key|ssh_key|credential)([\"'=:\s]+)([^\s\"',}<]+)"
)


def _redact_text(value: str, limit: int = 240) -> str:
    text = " ".join((value or "").split())
    text = redact_text(text, anonymize_ips=False)
    text = SECRET_RE.sub(r"\1\2***REDACTED***", text)
    return text[:limit]


def _xml_root_name(value: str) -> str:
    try:
        return ET.fromstring(value or "").tag.rsplit("}", 1)[-1]
    except ET.ParseError:
        return ""


class SoundTouchClient:
    """Access one SoundTouch device through its local HTTP/XML API.

    An externally managed ``http_client`` can be injected for tests and for a
    future application-level connection pool. When omitted, request lifecycle
    behavior remains compatible with the previous implementation.
    """

    def __init__(
        self,
        ip_address: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        radio_port: int | None = None,
        get_timeout: float = 5.0,
        post_timeout: float = 8.0,
        device_id: str = "",
        request_purpose: str = "unspecified",
        trigger: str = "",
        retry_number: int = 0,
        policy_context: dict | None = None,
    ) -> None:
        self.ip_address = ip_address
        self.base_url = f"http://{ip_address}:{radio_port or get_settings().radio_port}"
        self._http_client = http_client
        self.get_timeout = get_timeout
        self.post_timeout = post_timeout
        self.device_id = device_id
        self.request_purpose = request_purpose
        self.trigger = trigger
        self.retry_number = retry_number
        self.policy_context = policy_context or {}

    def _context(self, path: str, method: str, timeout: float, body: str = "") -> dict:
        policy = self.policy_context
        return {
            "device_id": self.device_id or policy.get("device_id", ""),
            "radio_ip": self.ip_address,
            "device_class": policy.get("device_class", "unknown"),
            "request_purpose": self.request_purpose,
            "method": method,
            "endpoint": path,
            "timeout": timeout,
            "retry_number": self.retry_number,
            "trigger": self.trigger,
            "polling_profile": policy.get("polling_profile", ""),
            "safe_mode_active": policy.get("safe_mode_active", False),
            "circuit_breaker_state": policy.get("circuit_state", ""),
            "body_root": _xml_root_name(body) if body else "",
            "body_preview": _redact_text(body) if body else "",
        }

    def _request_target(self, path: str, method: str) -> tuple[str, dict[str, str], dict[str, str]]:
        url = f"{self.base_url}{path}"
        validation = validate_outbound_http_url(url)
        if not validation.ok:
            for address in validation.addresses:
                reject_protected_device_access(
                    address,
                    device_id=self.device_id,
                    action=self.request_purpose or "device access",
                    requester=self.trigger or "soundtouch_client",
                    method=method,
                    endpoint=path,
                )
            raise OSError(validation.reason)
        target = validation.addresses[0]
        reject_protected_device_access(
            target,
            device_id=self.device_id,
            action=self.request_purpose or "device access",
            requester=self.trigger or "soundtouch_client",
            method=method,
            endpoint=path,
        )
        return pinned_http_target(url, validation)

    def _log_request(
        self,
        *,
        path: str,
        method: str,
        timeout: float,
        started_at: datetime,
        duration_ms: int,
        result: str,
        status_code: int = 0,
        error: BaseException | None = None,
        body: str = "",
    ) -> None:
        payload = self._context(path, method, timeout, body)
        payload.update({
            "started_at": started_at.isoformat(),
            "duration_ms": duration_ms,
            "result": result,
            "status_code": status_code,
            "error_class": error.__class__.__name__ if error is not None else "",
            "error": _redact_text(str(error or "")),
        })
        write_masterlog("device_interaction", **payload)

    def _write_ledger(
        self,
        *,
        path: str,
        body: str,
        result: str,
        duration_ms: int,
        status_code: int = 0,
        error: BaseException | None = None,
    ) -> None:
        """Persist one append-only row without making transport success depend on DB I/O."""

        try:
            from basswiesn.app import db as app_db
            from basswiesn.app.models import Device
            from basswiesn.app.services.action_journal import record_action

            db = app_db.SessionLocal()
            try:
                device_id = str(self.device_id or "").strip().upper()
                if not device_id:
                    row = db.query(Device).filter(Device.ip_address == self.ip_address).one_or_none()
                    device_id = str(row.device_id if row is not None else "").strip().upper()
                record_action(
                    db,
                    job_id=str(self.policy_context.get("job_id") or ""),
                    device_id=device_id,
                    ip_address=self.ip_address,
                    action=f"HTTP_POST {path}",
                    trigger=self.trigger or self.request_purpose or "unknown",
                    phase=self.request_purpose or "device_write",
                    requested_state={"xml": _redact_text(body, limit=4096)},
                    result=f"{result};status={status_code}",
                    duration_ms=duration_ms,
                    error_category=error.__class__.__name__ if error is not None else "",
                    verified=False,
                )
                db.commit()
            finally:
                db.close()
        except Exception as ledger_error:
            write_masterlog(
                "write_ledger_failed",
                device_id=self.device_id,
                radio_ip=self.ip_address,
                endpoint=path,
                error_type=ledger_error.__class__.__name__,
            )

    async def get_xml(self, path: str) -> str:
        reject_protected_device_access(
            self.ip_address,
            device_id=self.device_id,
            action=self.request_purpose or "device read",
            requester=self.trigger or "soundtouch_client",
            method="GET",
            endpoint=path,
        )
        pinned_url, pinned_headers, extensions = self._request_target(path, "GET")
        started_at = datetime.now(UTC)
        started = time.monotonic()
        status_code = 0
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    pinned_url,
                    headers=pinned_headers,
                    extensions=extensions,
                    timeout=self.get_timeout,
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self.get_timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    response = await client.get(
                        pinned_url,
                        headers=pinned_headers,
                        extensions=extensions,
                    )
            status_code = response.status_code
            response.raise_for_status()
            duration_ms = int((time.monotonic() - started) * 1000)
            self._log_request(
                path=path,
                method="GET",
                timeout=self.get_timeout,
                started_at=started_at,
                duration_ms=duration_ms,
                result="ok",
                status_code=status_code,
            )
            return response.text
        except Exception as exc:
            self._log_request(
                path=path,
                method="GET",
                timeout=self.get_timeout,
                started_at=started_at,
                duration_ms=int((time.monotonic() - started) * 1000),
                result="error",
                status_code=status_code,
                error=exc,
            )
            raise

    async def post_xml(self, path: str, body: str, headers: dict[str, str] | None = None) -> str:
        reject_protected_device_access(
            self.ip_address,
            device_id=self.device_id,
            action=self.request_purpose or path,
            requester=self.trigger,
            method="POST",
            endpoint=path,
        )
        pinned_url, pinned_headers, extensions = self._request_target(path, "POST")
        request_headers = {"Content-Type": "application/xml"}
        request_headers.update(pinned_headers)
        if headers:
            request_headers.update(headers)

        started_at = datetime.now(UTC)
        started = time.monotonic()
        status_code = 0
        try:
            if self._http_client is not None:
                response = await self._http_client.post(
                    pinned_url,
                    content=body,
                    headers=request_headers,
                    extensions=extensions,
                    timeout=self.post_timeout,
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self.post_timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    response = await client.post(
                        pinned_url,
                        content=body,
                        headers=request_headers,
                        extensions=extensions,
                    )
            status_code = response.status_code
            response.raise_for_status()
            duration_ms = int((time.monotonic() - started) * 1000)
            self._log_request(
                path=path,
                method="POST",
                timeout=self.post_timeout,
                started_at=started_at,
                duration_ms=duration_ms,
                result="ok",
                status_code=status_code,
                body=body,
            )
            self._write_ledger(
                path=path,
                body=body,
                result="success",
                duration_ms=duration_ms,
                status_code=status_code,
            )
            return response.text
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._log_request(
                path=path,
                method="POST",
                timeout=self.post_timeout,
                started_at=started_at,
                duration_ms=duration_ms,
                result="error",
                status_code=status_code,
                error=exc,
                body=body,
            )
            self._write_ledger(
                path=path,
                body=body,
                result="failed",
                duration_ms=duration_ms,
                status_code=status_code,
                error=exc,
            )
            raise

    async def info(self) -> dict[str, str]:
        xml_text = await self.get_xml("/info")
        root = ET.fromstring(xml_text)
        return {
            "device_id": root.attrib.get("deviceID", ""),
            "ip_address": self.ip_address,
            "name": root.findtext("name", ""),
            "model": root.findtext("type", ""),
            "marge_url": root.findtext("margeURL", ""),
            "raw": xml_text,
        }
