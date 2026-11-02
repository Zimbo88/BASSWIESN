"""Bound credential-bearing bodies before parsing or password/token operations."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from starlette.requests import ClientDisconnect, Request

BODY_TIMEOUT = 5.0


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('ambiguous object')
        value[key] = item
    return value


def _constant(value: str) -> None:
    raise ValueError('non-JSON number')


async def read_admin_object(request: Request, *, limit: int) -> dict[str, Any]:
    lengths = request.headers.getlist('content-length')
    if (len(lengths) > 1 or any(not value.isascii() or not value.isdecimal()
                              or len(value) > 10 or int(value) > limit for value in lengths)
            or any(value.strip().lower() != 'identity'
                   for value in request.headers.getlist('content-encoding'))):
        raise ValueError('invalid administration request')
    raw = bytearray()
    try:
        # One deadline for the whole body: trickling bytes must not reset it.
        async with asyncio.timeout(BODY_TIMEOUT):
            async for chunk in request.stream():
                if len(raw) + len(chunk) > limit:
                    raise ValueError('invalid administration request')
                raw.extend(chunk)
        if lengths and len(raw) != int(lengths[0]):
            raise ValueError('invalid administration request')
        body = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
        if not isinstance(body, dict):
            raise ValueError('invalid administration request')
        return body
    except (TimeoutError, ClientDisconnect, ValueError, RecursionError):
        # Never echo a password, submitted token or a parser's source fragment.
        raise ValueError('invalid administration request') from None
