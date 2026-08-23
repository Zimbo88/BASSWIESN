from __future__ import annotations

import ipaddress
from urllib.parse import urljoin, urlparse

import httpx

from basswiesn.app.config import get_settings
from basswiesn.app.services.network_security import (
    pinned_http_target,
    validate_outbound_http_url,
)
from basswiesn.app.services.stream_compat import analyze_stream_url


REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})


def _host_kind(host: str) -> str:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "hostname"
    if ip.is_loopback:
        return "loopback"
    if ip.is_private or ip.is_link_local:
        return "private_lan"
    return "public_internet"


def _addresses_are_private_or_local(addresses: tuple[str, ...]) -> bool:
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return True
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


async def probe_stream_reference(
    stream_url: str,
    *,
    allowed_hosts: set[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Perform an explicit, bounded server-side stream probe."""
    value = (stream_url or "").strip()
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    result = {"requested": True, "performed": False, "status": "nicht ausgeführt", "host": host, "content_type": "", "sample_bytes": 0, "reason": ""}
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return {**result, "status": "ungueltig", "reason": "Nur HTTP/HTTPS ohne Zugangsdaten ist erlaubt"}
    settings = get_settings()
    configured_hosts = {str(item).lower() for item in (allowed_hosts or set()) if str(item).strip()}
    configured_hosts.update(item for item in (settings.lan_host, urlparse(settings.local_base_url).hostname or "") if item)
    validation = validate_outbound_http_url(value)
    if not validation.ok:
        return {**result, "status": "blockiert", "reason": validation.reason}
    if _addresses_are_private_or_local(validation.addresses) and host not in configured_hosts:
        return {**result, "status": "blockiert", "reason": "Privates oder lokales Ziel ist nicht als Streamhost freigegeben"}
    try:
        pinned_url, pinned_headers, extensions = pinned_http_target(value, validation)
        async with httpx.AsyncClient(
            timeout=3.0,
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        ) as client:
            async with client.stream(
                "GET",
                pinned_url,
                headers={**pinned_headers, "Range": "bytes=0-4095"},
                extensions=extensions,
            ) as response:
                if response.status_code in REDIRECT_STATUS_CODES:
                    location = str(response.headers.get("location") or "").strip()
                    redirected = urljoin(value, location) if location else ""
                    redirect_validation = validate_outbound_http_url(redirected) if redirected else None
                    if redirect_validation is not None and not redirect_validation.ok:
                        reason = f"Stream-Weiterleitung blockiert: {redirect_validation.reason}"
                    else:
                        reason = "Stream-Weiterleitungen werden im sicheren Preflight nicht verfolgt"
                    return {
                        **result,
                        "performed": True,
                        "status": "blockiert",
                        "http_status": response.status_code,
                        "reason": reason,
                    }
                sample = 0
                async for chunk in response.aiter_bytes():
                    sample += len(chunk)
                    if sample >= 4096:
                        break
                content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                return {
                    **result,
                    "performed": True,
                    "status": "erreichbar" if response.status_code < 400 else "fehler",
                    "http_status": response.status_code,
                    "content_type": content_type,
                    "sample_bytes": sample,
                    "reason": "Serverseitiger Stream-Preflight abgeschlossen" if response.status_code < 400 else "Streamserver lieferte einen Fehlerstatus",
                }
    except (httpx.HTTPError, ValueError) as exc:
        return {**result, "performed": True, "status": "fehler", "reason": f"Stream konnte nicht geprüft werden: {exc.__class__.__name__}"}


def build_offline_preflight(*, stream_url: str, location: str = "", probe_requested: bool = False) -> dict:
    """Classify stream dependencies; never contacts a radio or stream by itself."""
    url = (stream_url or "").strip()
    resolved_location = (location or url).strip()
    parsed = urlparse(url)
    location_parsed = urlparse(resolved_location)
    settings = get_settings()
    host = (parsed.hostname or "").lower()
    location_host = (location_parsed.hostname or "").lower()
    local_hosts = {item for item in {settings.lan_host, urlparse(settings.local_base_url).hostname or ""} if item}
    local_server_required = location_host in local_hosts or host in local_hosts or location_parsed.path.startswith("/media/")
    kind = _host_kind(host) if host else "unknown"
    internet_required = kind == "public_internet"
    internet_independent = kind in {"private_lan", "loopback"}
    syntax_valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    stream = analyze_stream_url(url) if syntax_valid else analyze_stream_url("")
    basis = "erkannt" if syntax_valid else "unbekannt"
    dependencies = [
        {"name": "Bose Cloud", "required": False, "value": "unbestätigt", "basis": "nicht aus URL beweisbar"},
        {"name": "Internet", "required": internet_required, "value": "erforderlich" if internet_required else "nicht aus URL erforderlich" if internet_independent else "unbekannt", "basis": basis},
        {"name": "BASSWIESN-Server", "required": local_server_required, "value": "erforderlich" if local_server_required else "nicht aus URL erforderlich", "basis": "konfigurierte lokale Route" if local_server_required else basis},
        {"name": "lokaler Streamserver", "required": kind == "private_lan" and not local_server_required, "value": "möglich" if kind == "private_lan" and not local_server_required else "nicht erkannt", "basis": "private LAN-Adresse" if kind == "private_lan" else "unbekannt"},
    ]
    if not syntax_valid:
        expectation = "URL ist nicht als sicherer HTTP/HTTPS-Stream erkennbar."
    elif local_server_required and internet_required:
        expectation = "Dieses Preset benötigt den laufenden BASSWIESN-Server und Internetzugriff zum externen Senderstream."
    elif local_server_required:
        expectation = "Dieses Preset benötigt den laufenden BASSWIESN-Server; radio-autarker Betrieb ist nicht bestätigt."
    elif kind == "private_lan":
        expectation = "Dieses Preset verwendet einen lokalen LAN-Stream. BASSWIESN und der lokale Streamserver müssen erreichbar sein."
    else:
        expectation = "Dieses Preset verwendet einen externen Stream. Radio-autarker Betrieb ist nicht bestätigt."
    return {
        "stream_url": url,
        "location": resolved_location,
        "source_kind": "external_internet_stream" if internet_required else "local_network_stream" if kind in {"private_lan", "loopback"} else "unknown",
        "basis": basis,
        "stream_analysis": stream.to_dict(),
        "dependencies": dependencies,
        "bose_cloud_independent": {"value": False, "status": "unbestätigt", "basis": "Softwareklassifizierung ersetzt keinen Hardwaretest"},
        "radio_autark": {"value": False, "status": "unbestätigt", "basis": "Kein Hardwaretest nach Serverstopp/Radio-Neustart"},
        "basswiesn_server_current_request": True,
        "basswiesn_server_configured": bool(settings.lan_host or settings.local_base_url),
        "probe": {"requested": bool(probe_requested), "performed": False, "status": "nicht ausgeführt", "reason": "Read-only-Klassifizierung ohne Netzwerkrequest"},
        "expectation": expectation,
        "safe_for_radio": False,
    }
