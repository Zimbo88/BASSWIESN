"""Explicit HTTPS administrator boundary; never log or persist request bodies."""
import asyncio
import json
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.db import get_db
from basswiesn.app.services import update_control
from basswiesn.app.services.release_scope import require_lab
from basswiesn.app.services.offline_mode import external_request_decision
from basswiesn.update_helper.protocol import decode_request, UpdateRejected

router = APIRouter(prefix="/api/update/admin", tags=["update administration"])


def answer(value, status=200):
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})


def secure_origin(request):
    # Do not trust proxy headers to turn a plain HTTP connection into HTTPS.
    # The bundled server disables Uvicorn proxy processing as well.
    if request.scope.get("scheme") != "https" or any(name in request.headers for name in
            ("forwarded", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-for")):
        return False
    try:
        origin = request.headers.get("origin", "")
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment or not parsed.hostname):
            return False
        host = urlsplit("https://" + request.headers.get("host", ""))
        return (len(request.headers.getlist("origin")) == 1 and len(request.headers.getlist("host")) == 1
            and not host.path and not host.query and not host.fragment and host.username is None and host.password is None
            and parsed.netloc == host.netloc and parsed.port == request.scope.get("server", (None, None))[1]
            and request.headers.get("sec-fetch-site", "same-origin") == "same-origin"
            and request.headers.get("x-basswiesn-update") == "1"
            and request.headers.get("content-type", "").lower() == "application/json"
            and not request.url.query)
    except ValueError:
        return False


@router.get("/status")
async def status():
    try:
        value = await update_control.status()
    except update_control.ControlUnavailable:
        return answer({"ok": False, "code": "HELPER_UNAVAILABLE", "installation_available": False})
    result = value.get("result", {})
    return answer({**value, "installation_available": value["ok"] and result.get("executor_available", False)
                   and not result.get("recovery_required", False)})


@router.post("/install")
async def install(request: Request, db: Session = Depends(get_db)):
    if not secure_origin(request):
        return answer({"ok": False, "code": "SECURE_ORIGIN_REQUIRED"}, 403)
    require_lab(db)
    if get_settings().update_validation_mode:
        return answer({"ok": False, "code": "VALIDATION_MODE"}, 409)
    decision = external_request_decision(db, service="update_check", url_or_host="api.github.com",
                                        reason="administrator update", required=False, manual_action=True)
    if not decision.allowed:
        return answer({"ok": False, "code": "OFFLINE_POLICY"}, 409)
    # No Pydantic body model: validation errors must not echo a capability.
    try:
        async with asyncio.timeout(3):
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 2048:
                    raise ValueError()
                raw.extend(chunk)
        parsed = decode_request(bytes(raw))
        if parsed.action != "submit" or parsed.expected_current_version != get_settings().version:
            raise ValueError()
        body = json.loads(raw)
    except (ValueError, UnicodeError, TimeoutError, UpdateRejected):
        return answer({"ok": False, "code": "BAD_REQUEST"}, 400)
    try:
        value = await update_control.exchange(body)
        return answer(value, 202 if value["ok"] else 403)
    except update_control.ControlUnavailable as error:
        # Uncertain means ONLY poll; no POST retry, even after a browser restart.
        return answer({"ok": False, "code": str(error), "request_id": parsed.request_id},
                      202 if error.submitted else 503)
    except Exception:
        # This is a credential-bearing boundary. Never let an unexpected
        # adapter exception reach a generic exception logger with input values.
        # Delivery may have occurred: only status polling is safe now.
        return answer({"ok": False, "code": "SUBMISSION_UNCERTAIN", "request_id": parsed.request_id}, 202)
