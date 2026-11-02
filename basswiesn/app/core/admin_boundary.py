"""Default-deny human boundary with explicitly registered protocol exceptions."""

from collections.abc import Sequence
import secrets
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Mount
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from basswiesn.app.config import Settings
from basswiesn.app.core.admin_store import AdminSession, AdminStore, AuthUnavailable
from basswiesn.app.core.api_tokens import ApiTokenStore, TokenRecord
from basswiesn.app.core.token_policy import required_scope
from basswiesn.app.core.browser_headers import secure_browser_response
from basswiesn.app.core.route_policy import explicitly_registered, selected_route
from basswiesn.app.core.device_ingress import DeviceIngress

COOKIE = "basswiesn_admin_session"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
Listener = Literal["web", "cloud", "debug"]


def auth_error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": {
        "AUTH_REQUIRED": "Administrator login required",
        "AUTH_UNAVAILABLE": "Local administrator configuration/storage unavailable",
        "AUTH_ORIGIN": "This administration origin is not permitted",
        "AUTH_CSRF": "Request verification failed; reload before trying again",
        "AUTH_TLS_REQUIRED": "Administrator access requires HTTPS",
        "DEVICE_BROWSER_WRITE_DENIED": "Use the authenticated administration API",
    }.get(code, code)}}, status_code=status, headers={"Cache-Control": "no-store"})


def origin(value: str) -> str:
    """Canonical configured/browser origin, without DNS or forwarded headers."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            return ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        host = parsed.hostname.lower()
        if any(char.isspace() for char in host) or "\\" in host:
            return ""
        if ":" in host:
            host = f"[{host}]"
        return f"{parsed.scheme}://{host}:{port}"
    except ValueError:
        return ""


def request_origin(request: Request) -> str:
    return origin(f"{request.scope['scheme']}://{request.headers.get('host', '')}")


class AdminBoundary:
    """Installed outermost, before handlers and before any device transport.

    Device route objects come from explicit registrations, not a path prefix or
    the listener name. Method mismatches, docs and unknown routes remain human.
    Configuration is frozen with the app; changing mode requires a restart.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings, listener: Listener,
                 anonymous: Sequence[BaseRoute], store: AdminStore,
                 routes: Sequence[BaseRoute] = (), public_health: Sequence[BaseRoute] = (),
                 public_login: Sequence[BaseRoute] = ()) -> None:
        self.app = app
        self.settings = settings
        self.listener = listener
        self.anonymous = anonymous
        self.store = store
        self.routes = routes
        self.public_health = public_health
        self.public_login = public_login
        self.device_ingress = DeviceIngress(settings.device_allowed_peers)
        self.origins = frozenset(origin(item) for item in settings.admin_origins)

    def _anonymous(self, scope: Scope) -> bool:
        selected = selected_route(self.routes, scope)
        return explicitly_registered(selected, self.anonymous) and (
            selected is not None and (not isinstance(selected.route, Mount)
                                      or scope.get("method") in {"GET", "HEAD"}))

    async def _authorize(self, request: Request) -> Response | None:
        path, method = request.url.path, request.method
        if method == "GET" and explicitly_registered(selected_route(self.routes, request.scope), self.public_health):
            return None
        if self._anonymous(request.scope):
            if request.headers.getlist("authorization"):
                return auth_error("TOKEN_ROUTE_DENIED", 403)
            if method not in SAFE_METHODS and (request.headers.get("origin") or request.headers.get("sec-fetch-site")):
                return auth_error("DEVICE_BROWSER_WRITE_DENIED", 403)
            return None
        if not self.origins or "" in self.origins:
            return auth_error("AUTH_UNAVAILABLE", 503)
        current_origin = request_origin(request)
        if current_origin not in self.origins:
            return auth_error("AUTH_ORIGIN", 403)
        if request.scope["scheme"] != "https" and not self.settings.admin_allow_http:
            return auth_error("AUTH_TLS_REQUIRED", 403)
        authorization = request.headers.getlist("authorization")
        if authorization:
            # Never downgrade malformed/insufficient bearer credentials to an
            # accompanying administrator cookie. Automation is not browser auth.
            if (self.listener != "web" or len(authorization) != 1
                    or "cookie" in request.headers or "origin" in request.headers
                    or "sec-fetch-site" in request.headers):
                return auth_error("TOKEN_CONTEXT_DENIED", 403)
            parts = authorization[0].split(" ")
            if len(parts) != 2 or parts[0].lower() != "bearer":
                return auth_error("TOKEN_INVALID", 401)
            tokens = ApiTokenStore(self.store)
            try:
                principal = await run_in_threadpool(tokens.verify, parts[1], touch=False)
                if principal is None:
                    return auth_error("TOKEN_INVALID", 401)
                needed = required_scope(self.routes, request.scope)
                if needed is None or needed not in principal.scopes:
                    return auth_error("TOKEN_SCOPE_DENIED", 403)
                principal = await run_in_threadpool(tokens.verify, parts[1])
                if principal is None:
                    return auth_error("TOKEN_INVALID", 401)
            except AuthUnavailable:
                return auth_error("AUTH_UNAVAILABLE", 503)
            request.scope.setdefault("state", {})["api_token"] = principal
            return None
        # Login CSRF is also checked; a cross-origin form cannot log a victim
        # into an attacker's account. Do not infer trust from CORS or Referer.
        if method not in SAFE_METHODS:
            if origin(request.headers.get("origin", "")) != current_origin:
                return auth_error("AUTH_ORIGIN", 403)
        if self.listener == "web" and explicitly_registered(selected_route(self.routes, request.scope), self.public_login):
            return None
        try:
            session = await run_in_threadpool(self.store.session, request.cookies.get(COOKIE, ""), touch=False)
        except AuthUnavailable:
            return auth_error("AUTH_UNAVAILABLE", 503)
        if session is None:
            if self.listener == "web" and method == "GET" and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse("/auth/login", status_code=303, headers={"Cache-Control": "no-store"})
            return auth_error("AUTH_REQUIRED", 401)
        if method not in SAFE_METHODS and not secrets.compare_digest(request.headers.get("x-basswiesn-csrf", "").encode(), session.csrf_token.encode()):
            return auth_error("AUTH_CSRF", 403)
        try:
            # Invalid origins/CSRF must not extend the idle lifetime.
            active = await run_in_threadpool(self.store.session, request.cookies.get(COOKIE, ""))
        except AuthUnavailable:
            return auth_error("AUTH_UNAVAILABLE", 503)
        if active is None:
            return auth_error("AUTH_REQUIRED", 401)
        request.scope.setdefault("state", {})["admin_session"] = active
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.settings.admin_auth_mode != "required" or scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # No current human websocket contract; future additions must opt in
            # with a reviewed session/origin policy, never inherit anonymous use.
            await send({"type": "websocket.close", "code": 1008})
            return
        request = Request(scope, receive)
        human = not self._anonymous(scope)

        async def browser_send(message: Message) -> None:
            await send(secure_browser_response(message, path=request.url.path) if human else message)

        response = await self._authorize(request)
        if response is not None:
            await response(scope, receive, browser_send)
            return

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer")])
                if (isinstance(scope.get("state", {}).get("admin_session"), AdminSession)
                        or isinstance(scope.get("state", {}).get("api_token"), TokenRecord)):
                    headers = [(key, value) for key, value in headers if key.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await browser_send(message)
        selected = selected_route(self.routes, scope)
        if not human and selected is not None and not isinstance(selected.route, Mount):
            params = selected.child.get("path_params", {})
            await self.device_ingress(
                self.app, scope, receive, secure_send,
                device_id=params.get("device_id"), account_id=params.get("account_id"),
                registration=getattr(selected.route, "path", "") == "/streaming/account/{account_id}/device/",
            )
        else:
            await self.app(scope, receive, secure_send)


def install_admin_boundary(app: FastAPI, *, settings: Settings, listener: Listener,
                           anonymous: Sequence[BaseRoute]) -> None:
    from basswiesn.app.routers.admin_auth import router as login_router
    login_routes = [route for route in login_router.routes if getattr(route, "path", None) == "/auth/login"]
    store = AdminStore(settings.data_dir / "secrets" / "admin" / "auth.sqlite3")
    app.state.admin_store = store
    app.state.admin_settings = settings
    app.add_middleware(AdminBoundary, settings=settings, listener=listener, anonymous=tuple(anonymous), store=store,
                       routes=app.routes, public_health=getattr(app.state, "public_health_routes", ()),
                       public_login=login_routes)
