"""Inventory and explicit LAB control; no provisioning or startup side effects."""
import json
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from basswiesn.app.db import get_db
from basswiesn.app.models import Device
from basswiesn.app.services.airplay_bridge.planning import bridge_status
from basswiesn.app.services.airplay_bridge import service_control
from basswiesn.app.config import get_settings
from basswiesn.app.core.admin_boundary import origin, request_origin
from basswiesn.app.core.admin_requests import read_admin_object
from basswiesn.app.core.admin_store import AdminSession
from basswiesn.app.services.release_scope import require_lab


router = APIRouter(prefix="/api/airplay-bridge", tags=["airplay-bridge"])


@router.get("/status")
def status(db: Session = Depends(get_db)) -> dict[str, Any]:
    devices = db.query(Device).order_by(Device.name, Device.device_id).all()
    return bridge_status(devices)


def _answer(value: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status, headers={'Cache-Control': 'no-store'})


@router.get('/runtime/status')
async def runtime_status() -> JSONResponse:
    try:
        result = await service_control.exchange(get_settings().data_dir.resolve() / 'airplay-runtime',
            b'{"version":1,"action":"status"}')
        return _answer(result)
    except Exception:
        return _answer({'ok': False, 'code': 'AIRPLAY_CONTROL_UNAVAILABLE', 'available': False})


async def _command(action: str, request: Request, db: Session) -> JSONResponse:
    # Stronger than legacy LAN mode: the new local control plane requires a
    # genuine human administrator session, even if ordinary reads are public.
    admin = getattr(request.state, 'admin_session', None)
    if not isinstance(admin, AdminSession):
        return _answer({'ok': False, 'code': 'AUTH_REQUIRED'}, 401)
    if (len(request.headers.getlist('origin')) != 1
            or len(request.headers.getlist('host')) != 1
            or origin(request.headers.get('origin', '')) != request_origin(request)
            or request.headers.get('sec-fetch-site', 'same-origin') != 'same-origin'
            or request.headers.get('content-type', '').lower() != 'application/json'
            or not secrets.compare_digest(request.headers.get('x-basswiesn-csrf', '').encode(),
                                          admin.csrf_token.encode())):
        return _answer({'ok': False, 'code': 'AUTH_CSRF'}, 403)
    require_lab(db)
    try:
        body = await read_admin_object(request, limit=2048)
        if set(body) != ({'request_id', 'profile'} if action == 'start' else {'request_id'}):
            raise ValueError
        raw = json.dumps({'version': 1, 'action': action, **body}).encode()
        service_control.decode_request(raw)
    except (ValueError, TypeError, UnicodeError):
        return _answer({'ok': False, 'code': 'AIRPLAY_CONTROL_INVALID'}, 400)
    try:
        result = await service_control.exchange(get_settings().data_dir.resolve() / 'airplay-runtime', raw)
        # Accepted is not audible playback, nor a verified restoration.
        return _answer(result, 202 if result['ok'] else 409)
    except Exception:
        # Never replay a possibly submitted start. Poll using the same ID.
        return _answer({'ok': False, 'code': 'AIRPLAY_SUBMISSION_UNCERTAIN',
                        'request_id': body['request_id']}, 202)


@router.post('/runtime/start')
async def runtime_start(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    return await _command('start', request, db)


@router.post('/runtime/stop')
async def runtime_stop(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    return await _command('stop', request, db)
