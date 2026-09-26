"""Bounded, explicit checks of official stable releases. No installation."""
import asyncio
import json
import re

import httpx

from basswiesn.app.services.network_security import pinned_http_target, validate_outbound_http_url
from basswiesn.app.services.release_publication import REPOSITORY, _published

LATEST = "https://api.github.com/repos/Zimbo88/BASSWIESN/releases/latest"
STABLE = re.compile(r"^v(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})$")
MAX_BYTES = 128 * 1024


def installation_status():
    # Never infer host permissions from a container UID or an environment flag.
    return {"installation_available": False, "installation_status": "HOST_HELPER_NOT_CHECKED",
            "backup_restore_verified": False, "automatic_installation": False}


def parse_release(payload, local_version):
    if not isinstance(payload, dict):
        raise ValueError("shape")
    tag = payload.get("tag_name")
    version = STABLE.fullmatch(tag) if isinstance(tag, str) else None
    if (not version or payload.get("draft") is not False or payload.get("prerelease") is not False
            or payload.get("html_url") != f"{REPOSITORY}/releases/tag/{tag}"
            or not _published(payload.get("published_at"))):
        raise ValueError("not an official stable release")
    name = f"basswiesn-docker-release-{tag[1:]}.tar.gz"
    expected = {name, "SHA256SUMS"}
    assets = payload.get("assets")
    if not isinstance(assets, list) or len(assets) > 100:
        raise ValueError("assets")
    matched = {}
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") not in expected:
            continue
        filename = asset["name"]
        if (filename in matched or asset.get("state") != "uploaded"
                or type(asset.get("size")) is not int or not 0 < asset["size"] <= 512 * 1024 * 1024
                or asset.get("browser_download_url") != f"{REPOSITORY}/releases/download/{tag}/{filename}"):
            raise ValueError("asset provenance")
        matched[filename] = asset["browser_download_url"]
    local = STABLE.fullmatch("v" + local_version.removeprefix("v"))
    status = ("local_version_unknown" if not local else "update_available" if
              tuple(map(int, version.groups())) > tuple(map(int, local.groups())) else "up_to_date")
    return {"status": status, "local_version": local_version, "remote_version": tag[1:],
            "published_at": payload["published_at"], "release_url": payload["html_url"],
            "package_assets_present": expected == matched.keys(),
            "integrity_verified": False, **installation_status()}


async def check_official_release(local_version, *, transport=None):
    base = {"local_version": local_version, **installation_status()}
    try:
        async with asyncio.timeout(8):
            validation = await asyncio.to_thread(validate_outbound_http_url, LATEST,
                                                  allowed_hosts={"api.github.com"}, public_only=True)
            if not validation.ok:
                return {**base, "status": "target_rejected"}
            url, headers, extensions = pinned_http_target(LATEST, validation)
            async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False, transport=transport) as client:
                async with client.stream("GET", url, headers={**headers, "Accept": "application/vnd.github+json",
                        "Accept-Encoding": "identity", "User-Agent": "BASSWIESN-Release-Check"}, extensions=extensions) as response:
                    if response.status_code != 200:
                        return {**base, "status": "rate_limited" if response.status_code in {403, 429} else "unavailable"}
                    if response.headers.get("content-encoding", "identity") != "identity":
                        return {**base, "status": "invalid_response"}
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > MAX_BYTES:
                            return {**base, "status": "response_too_large"}
                        data.extend(chunk)
            return parse_release(json.loads(data), local_version)
    except (httpx.HTTPError, OSError, ValueError, TimeoutError, TypeError):
        return {**base, "status": "unavailable"}
