from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import httpx

from basswiesn.app.config import get_settings
from basswiesn.app.services.network_security import (
    pinned_http_target,
    validate_public_callback_url,
)


MAX_LOGO_BYTES = 2 * 1024 * 1024
IMAGE_MIME_PREFIX = "image/"


def _safe_local_media_path(url: str) -> Path | None:
    parsed = urlparse(url)
    settings = get_settings()
    if parsed.hostname != settings.lan_host or not parsed.path.startswith("/media/"):
        return None
    root = (settings.data_dir / "media").resolve()
    relative = (root / Path(parsed.path.removeprefix("/media/"))).resolve()
    if relative == root or root not in relative.parents or ".." in Path(parsed.path).parts:
        return None
    return relative


def validate_logo_reference(value: str) -> dict:
    """Validate a logo reference without making a network request."""
    url = str(value or "").strip()
    base = {
        "configured": bool(url),
        "valid": False,
        "verification": "not_configured" if not url else "syntax_only",
        "content_type": "",
        "content_type_verified": False,
        "size_bytes": None,
        "fallback": "radio_symbol",
        "probe_available": False,
        "reason": "Logo nicht konfiguriert" if not url else "",
    }
    if not url:
        return base
    try:
        parsed = urlparse(url)
    except ValueError:
        base["reason"] = "Logo-URL ist syntaktisch ungueltig"
        return base
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        base["reason"] = "Nur HTTP/HTTPS-URLs ohne Zugangsdaten sind erlaubt"
        return base
    local_path = _safe_local_media_path(url)
    if local_path is not None:
        if not local_path.is_file():
            base["reason"] = "Lokale Logo-Datei ist nicht vorhanden"
            return base
        size = local_path.stat().st_size
        mime = mimetypes.guess_type(local_path.name)[0] or ""
        base.update({"valid": mime.startswith(IMAGE_MIME_PREFIX) and size <= MAX_LOGO_BYTES, "verification": "local_file", "content_type": mime, "content_type_verified": True, "size_bytes": size, "probe_available": False})
        if not base["valid"]:
            base["reason"] = "Lokale Logo-Datei ist kein unterstütztes Bild oder zu groß"
        return base
    base.update({"valid": True, "probe_available": True, "reason": "URL sicher formatiert; Content-Type und Größe noch nicht serverseitig geprüft"})
    return base


async def probe_logo_reference(value: str) -> dict:
    result = validate_logo_reference(value)
    if not result["valid"] or not result["probe_available"]:
        return result
    parsed = urlparse(str(value).strip())
    if parsed.hostname == get_settings().lan_host:
        return {**result, "verification": "not_probed", "reason": "Lokaler BASSWIESN-Host wird nur über bekannte lokale Dateien geprüft"}
    validation = validate_public_callback_url(str(value).strip())
    if not validation.ok:
        return {**result, "valid": False, "verification": "probe_blocked", "reason": validation.reason, "probe_available": False}
    try:
        pinned_url, pinned_headers, extensions = pinned_http_target(
            str(value).strip(), validation
        )
        async with httpx.AsyncClient(timeout=3.0, follow_redirects=False, trust_env=False) as client:
            async with client.stream(
                "GET",
                pinned_url,
                headers={**pinned_headers, "Range": f"bytes=0-{MAX_LOGO_BYTES}"},
                extensions=extensions,
            ) as response:
                content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                declared_size = int(response.headers.get("content-length") or 0)
                if response.status_code >= 400 or declared_size > MAX_LOGO_BYTES:
                    return {**result, "valid": False, "verification": "probed", "content_type": content_type, "content_type_verified": content_type.startswith(IMAGE_MIME_PREFIX), "size_bytes": declared_size or None, "reason": "Logo-Server lieferte keinen erlaubten Bildstatus"}
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_LOGO_BYTES:
                        break
                valid = response.status_code < 400 and content_type.startswith(IMAGE_MIME_PREFIX) and total <= MAX_LOGO_BYTES
                return {**result, "valid": valid, "verification": "probed", "content_type": content_type, "content_type_verified": content_type.startswith(IMAGE_MIME_PREFIX), "size_bytes": total, "reason": "Logo geprüft" if valid else "Logo ist kein erlaubtes Bild oder zu groß"}
    except (httpx.HTTPError, ValueError) as exc:
        return {**result, "valid": False, "verification": "probe_failed", "reason": f"Logo konnte nicht geprüft werden: {exc.__class__.__name__}"}
