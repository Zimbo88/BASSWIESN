"""Human login only. Account creation/recovery is a separate local command."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from basswiesn.app.config import Settings
from basswiesn.app.core.admin_boundary import COOKIE, auth_error
from basswiesn.app.core.admin_store import AdminStore, AuthUnavailable, LoginRejected, LoginThrottled
from basswiesn.app.core.admin_requests import read_admin_object

router = APIRouter(tags=["administration-auth"])


@router.get("/auth/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    return HTMLResponse('''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>BASSWIESN · Sign in</title>
<link rel="stylesheet" href="/static/app.css"><script src="/static/js/admin-login.js" defer></script>
</head><body><main class="panel"><h1>BASSWIESN</h1><h2 id="login-title">Administrator sign in</h2>
<form id="admin-login"><p><label for="username" id="username-label">Account</label>
<input id="username" name="username" autocomplete="username" maxlength="64" required></p>
<p><label for="password" id="password-label">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" maxlength="1024" required></p>
<button id="login-submit" type="submit">Sign in</button></form><p id="login-status" role="status" aria-live="polite"></p>
<p id="bootstrap-help">Create or recover the account locally with python -m basswiesn.admin. No default password exists.</p>
</main></body></html>''', headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})


@router.post("/auth/login")
async def login(request: Request) -> Response:
    settings: Settings = request.app.state.admin_settings
    if settings.admin_auth_mode != "required":
        return auth_error("AUTH_DISABLED", 404)
    try:
        body = await read_admin_object(request, limit=8192)
        if set(body) != {"username", "password"}:
            raise ValueError
        username, password = body["username"], body["password"]
        if not isinstance(username, str) or not username.isascii() or not isinstance(password, str) or len(username) > 64 or len(password.encode("utf-8")) > 1024:
            raise ValueError
    except (ValueError, UnicodeError):
        return auth_error("AUTH_REQUEST_INVALID", 400)
    store: AdminStore = request.app.state.admin_store
    try:
        result = await run_in_threadpool(store.login, username, password,
                                        peer=request.client.host if request.client else "unknown",
                                        previous_cookie=request.cookies.get(COOKIE, ""))
    except LoginThrottled:
        response = auth_error("AUTH_RATE_LIMITED", 429)
        response.headers["Retry-After"] = "60"
        return response
    except LoginRejected:
        return auth_error("AUTH_LOGIN_REJECTED", 401)
    except AuthUnavailable:
        return auth_error("AUTH_UNAVAILABLE", 503)
    response = JSONResponse({"authenticated": True, "username": result.session.username,
                             "csrf_token": result.session.csrf_token}, headers={"Cache-Control": "no-store"})
    response.set_cookie(COOKIE, result.cookie, httponly=True, secure=request.scope["scheme"] == "https",
                        samesite="strict", path="/")
    return response


@router.get("/auth/session")
def session(request: Request) -> Response:
    settings: Settings = request.app.state.admin_settings
    if settings.admin_auth_mode != "required":
        return JSONResponse({"enabled": False}, headers={"Cache-Control": "no-store"})
    current = request.state.admin_session
    return JSONResponse({"enabled": True, "authenticated": True, "username": current.username,
                         "csrf_token": current.csrf_token, "expires_at": current.expires_at},
                        headers={"Cache-Control": "no-store"})


@router.post("/auth/logout")
async def logout(request: Request) -> Response:
    settings: Settings = request.app.state.admin_settings
    if settings.admin_auth_mode != "required":
        return auth_error("AUTH_DISABLED", 404)
    store: AdminStore = request.app.state.admin_store
    try:
        await run_in_threadpool(store.logout, request.cookies.get(COOKIE, ""))
    except AuthUnavailable:
        return auth_error("AUTH_UNAVAILABLE", 503)
    response = JSONResponse({"authenticated": False}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(COOKIE, path="/", httponly=True, secure=request.scope["scheme"] == "https", samesite="strict")
    return response
