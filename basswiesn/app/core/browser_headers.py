"""Human browser restrictions, never a new device-protocol authorization rule."""
from starlette.types import Message

AUTH_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'"
)
BASE_CSP = "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'"
APP_CSP = BASE_CSP + "; script-src 'self'; script-src-attr 'none'; worker-src 'none'"
INTERACTIVE_DOCS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})


def secure_browser_response(message: Message, *, path: str) -> Message:
    if message["type"] != "http.response.start":
        return message
    headers = list(message.get("headers", []))
    replacements = {
        b"x-content-type-options": b"nosniff",
        b"x-frame-options": b"DENY",
        b"referrer-policy": b"no-referrer",
        b"permissions-policy": b"camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        b"cache-control": b"no-store",
    }
    if path in {"/auth/login", "/auth/automation"}:
        replacements[b"content-security-policy"] = AUTH_CSP.encode()
    elif not any(key.lower() == b"content-security-policy" for key, _ in headers):
        # The application loads local scripts and binds event listeners; it
        # needs neither inline script nor eval. Style/image/connect restrictions
        # remain a separate compatibility review. FastAPI's optional docs still
        # use a CDN and inline initializer, not the application's strict scripts.
        replacements[b"content-security-policy"] = (
            BASE_CSP if path in INTERACTIVE_DOCS else APP_CSP
        ).encode()
    headers = [(key, value) for key, value in headers if key.lower() not in replacements]
    headers.extend(replacements.items())
    # No automatic HSTS: browser policy is host-wide across ports and could
    # upgrade this installation's separate HTTP device listener incorrectly.
    return {**message, "headers": headers}
