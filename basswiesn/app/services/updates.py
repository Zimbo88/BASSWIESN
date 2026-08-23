"""Read-only release manifest checks. This module never installs updates."""

import re
from urllib.parse import urljoin

import httpx

from basswiesn.app.services.network_security import (
    pinned_http_target,
    validate_outbound_http_url,
)


REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
MAX_MANIFEST_REDIRECTS = 5


def version_key(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", str(value or ""))
    return tuple(int(item) for item in numbers[:4]) or (0,)


async def fetch_manifest(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    current_url = str(url or "").strip()
    async with httpx.AsyncClient(
        timeout=5.0,
        follow_redirects=False,
        transport=transport,
        trust_env=False,
    ) as client:
        response = None
        for _hop in range(MAX_MANIFEST_REDIRECTS + 1):
            validation = validate_outbound_http_url(current_url)
            if not validation.ok:
                raise ValueError(f"Manifest-Ziel ist blockiert: {validation.reason}")
            pinned_url, headers, extensions = pinned_http_target(
                current_url, validation
            )
            response = await client.get(
                pinned_url,
                headers=headers,
                extensions=extensions,
            )
            if response.status_code not in REDIRECT_STATUS_CODES:
                break
            location = str(response.headers.get("location") or "").strip()
            if not location:
                raise ValueError("Update-Manifest enthält eine ungültige Weiterleitung.")
            next_url = urljoin(current_url, location)
            redirect_validation = validate_outbound_http_url(next_url)
            if not redirect_validation.ok:
                raise ValueError(
                    f"Manifest-Weiterleitung ist blockiert: {redirect_validation.reason}"
                )
            current_url = next_url
        else:
            raise ValueError("Update-Manifest enthält zu viele Weiterleitungen.")
        if response is None:
            raise ValueError("Update-Manifest konnte nicht abgerufen werden.")
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or not str(payload.get("version") or "").strip():
        raise ValueError("Update-Manifest enthält keine gültige Version.")
    return payload


async def check_update(local_version: str, manifest_url: str) -> dict:
    if not manifest_url.strip():
        return {"status": "not_configured", "message": "Updatequelle noch nicht eingerichtet.", "local_version": local_version}
    try:
        manifest = await fetch_manifest(manifest_url.strip())
        remote_version = str(manifest["version"]).strip()
        available = version_key(remote_version) > version_key(local_version)
        return {
            "status": "update_available" if available else "up_to_date",
            "message": "Update verfügbar." if available else "BASSWIESN ist aktuell.",
            "local_version": local_version,
            "remote_version": remote_version,
            "manifest": {key: manifest.get(key, "") for key in ("version", "release_date", "download_url", "sha256", "notes")},
        }
    except Exception as exc:
        return {"status": "error", "message": f"Updateprüfung fehlgeschlagen: {exc}", "local_version": local_version}
