"""Session-only token management; a bearer cannot mint or elevate another grant."""
from pathlib import Path

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse, Response

from basswiesn.app.core.browser_headers import AUTH_CSP

from basswiesn.app.core.admin_boundary import COOKIE, auth_error
from basswiesn.app.core.admin_store import AdminSession, AuthUnavailable, LoginRejected
from basswiesn.app.core.api_tokens import ApiTokenStore
from basswiesn.app.core.admin_requests import read_admin_object

router = APIRouter(tags=["administration-tokens"])


def _store(request: Request) -> ApiTokenStore | None:
    # Defence in depth if the router is ever mounted without its middleware.
    if (request.app.state.admin_settings.admin_auth_mode != "required"
            or not isinstance(getattr(request.state, "admin_session", None), AdminSession)):
        return None
    return ApiTokenStore(request.app.state.admin_store)


@router.get("/auth/automation", include_in_schema=False)
def management_page(request: Request) -> Response:
    if _store(request) is None:
        return auth_error("AUTH_REQUIRED", 401)
    return FileResponse(Path(__file__).parents[1] / "views" / "admin-tokens.html",
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": AUTH_CSP})


@router.get("/auth/tokens")
async def list_tokens(request: Request) -> Response:
    store = _store(request)
    if store is None:
        return auth_error("AUTH_REQUIRED", 401)
    try:
        records = await run_in_threadpool(store.list)
    except AuthUnavailable:
        return auth_error("AUTH_UNAVAILABLE", 503)
    return JSONResponse({"tokens": [r.public() for r in records]}, headers={"Cache-Control": "no-store"})


@router.post("/auth/tokens")
async def issue_token(request: Request) -> Response:
    store = _store(request)
    if store is None:
        return auth_error("AUTH_REQUIRED", 401)
    try:
        body = await read_admin_object(request, limit=4096)
        if set(body) != {"name", "scopes", "lifetime_seconds"}:
            raise ValueError
        issued = await run_in_threadpool(store.issue, body["name"], body["scopes"], body["lifetime_seconds"],
                                        session_cookie=request.cookies.get(COOKIE, ""))
    except (ValueError, UnicodeError, RecursionError):
        return auth_error("TOKEN_REQUEST_INVALID", 400)
    except AuthUnavailable:
        return auth_error("AUTH_UNAVAILABLE", 503)
    except LoginRejected:
        return auth_error("AUTH_REQUIRED", 401)
    return JSONResponse({**issued.record.public(), "token": issued.token}, status_code=201,
                        headers={"Cache-Control": "no-store"})


@router.post("/auth/tokens/{identifier}/rotate")
async def rotate_token(identifier: str, request: Request) -> Response:
    store = _store(request)
    if store is None:
        return auth_error("AUTH_REQUIRED", 401)
    try:
        issued = await run_in_threadpool(store.rotate, identifier, session_cookie=request.cookies.get(COOKIE, ""))
    except AuthUnavailable:
        return auth_error("AUTH_UNAVAILABLE", 503)
    except LoginRejected:
        return auth_error("AUTH_REQUIRED", 401)
    if issued is None:
        return auth_error("TOKEN_NOT_ACTIVE", 404)
    return JSONResponse({**issued.record.public(), "token": issued.token}, headers={"Cache-Control": "no-store"})


@router.delete("/auth/tokens/{identifier}")
async def revoke_token(identifier: str, request: Request) -> Response:
    store = _store(request)
    if store is None:
        return auth_error("AUTH_REQUIRED", 401)
    try:
        revoked = await run_in_threadpool(store.revoke, identifier, session_cookie=request.cookies.get(COOKIE, ""))
    except AuthUnavailable:
        return auth_error("AUTH_UNAVAILABLE", 503)
    except LoginRejected:
        return auth_error("AUTH_REQUIRED", 401)
    return JSONResponse({"revoked": revoked}, headers={"Cache-Control": "no-store"})
