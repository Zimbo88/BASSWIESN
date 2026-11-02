"""Session-only provider catalogs; outbound reads require explicit POST + CSRF."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from basswiesn.app.core.admin_boundary import auth_error
from basswiesn.app.core.admin_store import AdminSession
from basswiesn.app.services.providers.contracts import ProviderError, ProviderErrorCode
from basswiesn.app.services.providers.manager import ProviderManager
from basswiesn.app.services.providers.runtime import build_provider_manager

router = APIRouter(prefix="/api/providers", tags=["provider-catalogs"])
Operation = Literal["search", "browse", "metadata", "resolve", "health"]


def _authorized(request: Request) -> bool:
    settings = getattr(request.app.state, "admin_settings", None)
    return (
        getattr(settings, "admin_auth_mode", "legacy") == "required"
        and isinstance(getattr(request.state, "admin_session", None), AdminSession)
    )


def _manager(request: Request) -> ProviderManager:
    manager = getattr(request.app.state, "provider_manager", None)
    if not isinstance(manager, ProviderManager):
        manager = build_provider_manager()
        request.app.state.provider_manager = manager
    return manager


def _error(error: ProviderError) -> Response:
    statuses = {
        ProviderErrorCode.NOT_FOUND: 404, ProviderErrorCode.DISABLED: 409,
        ProviderErrorCode.UNSUPPORTED: 409, ProviderErrorCode.INVALID_REQUEST: 400,
        ProviderErrorCode.UNSAFE_TARGET: 403, ProviderErrorCode.AUTH_REQUIRED: 503,
        ProviderErrorCode.BUSY: 429, ProviderErrorCode.TIMEOUT: 504,
    }
    return JSONResponse({"error": {"code": error.code.value}}, status_code=statuses.get(error.code, 502),
                        headers={"Cache-Control": "no-store"})


@router.get("")
async def providers(request: Request) -> Response:
    if not _authorized(request):
        return auth_error("AUTH_REQUIRED", 401)
    manager = _manager(request)
    return JSONResponse({"providers": [
        {**asdict(descriptor), "health": asdict(manager.cached_health(descriptor.provider_id))}
        for descriptor in manager.descriptors()
    ]}, headers={"Cache-Control": "no-store"})


@router.get("/{provider_id}/health")
async def cached_health(provider_id: str, request: Request) -> Response:
    if not _authorized(request):
        return auth_error("AUTH_REQUIRED", 401)
    try:
        return JSONResponse(asdict(_manager(request).cached_health(provider_id)), headers={"Cache-Control": "no-store"})
    except ProviderError as exc:
        return _error(exc)


async def _operation(provider_id: str, request: Request, operation: Operation) -> Response:
    if not _authorized(request):
        return auth_error("AUTH_REQUIRED", 401)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4096:
            return _error(ProviderError(ProviderErrorCode.INVALID_REQUEST))
    try:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError
        manager = _manager(request)
        if operation in {"search", "browse"}:
            key = "query" if operation == "search" else "parent"
            if set(body) - {key, "limit", "offset"}:
                raise ValueError
            value = body.get(key, "")
            limit, offset = body.get("limit", 25), body.get("offset", 0)
            if not isinstance(value, str) or type(limit) is not int or type(offset) is not int:
                raise ValueError
            page = await (manager.search(provider_id, value, limit=limit, offset=offset)
                          if operation == "search" else manager.browse(provider_id, value, limit=limit, offset=offset))
            result = asdict(page)
        elif operation in {"metadata", "resolve"}:
            if set(body) != {"item_id"} or not isinstance(body["item_id"], str):
                raise ValueError
            result = (asdict(await manager.metadata(provider_id, body["item_id"])) if operation == "metadata"
                      else asdict(await manager.resolve(provider_id, body["item_id"])))
        else:
            if body:
                raise ValueError
            result = asdict(await manager.health(provider_id))
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except ProviderError as exc:
        return _error(exc)
    except (ValueError, UnicodeError, RecursionError):
        return _error(ProviderError(ProviderErrorCode.INVALID_REQUEST))


@router.post("/{provider_id}/search")
async def search(provider_id: str, request: Request) -> Response:
    return await _operation(provider_id, request, "search")


@router.post("/{provider_id}/browse")
async def browse(provider_id: str, request: Request) -> Response:
    return await _operation(provider_id, request, "browse")


@router.post("/{provider_id}/metadata")
async def metadata(provider_id: str, request: Request) -> Response:
    return await _operation(provider_id, request, "metadata")


@router.post("/{provider_id}/resolve")
async def resolve(provider_id: str, request: Request) -> Response:
    return await _operation(provider_id, request, "resolve")


@router.post("/{provider_id}/health")
async def health(provider_id: str, request: Request) -> Response:
    return await _operation(provider_id, request, "health")
