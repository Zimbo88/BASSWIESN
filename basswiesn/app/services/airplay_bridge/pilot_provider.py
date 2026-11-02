"""A bounded three-route provider for an explicitly owned AirPlay trial.

No application startup hooks, discovery, metadata probing or administration.
Reuse the real BMX contracts while leaving the production listener untouched.
This is not an installation or an unauthenticated alternate administration API.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, Request
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from basswiesn.app.core.device_ingress import DeviceIngress, canonical_peer, reject
from basswiesn.app.models import Device, Station
from basswiesn.app.routers import cloud
from basswiesn.app.services.orion import ORION_STATION_PATH, StationDescriptor, encode_orion_data
from basswiesn.app.services.protected_devices import is_device_access_protected
from .display_session import is_bridge_station


class PilotProvider:
    def __init__(self, session_factory: Callable[[], Session], *, station: Station,
                 peers: Mapping[str, str], base_url: str, guard: Callable[[], None]) -> None:
        base = urlsplit(base_url)
        if (base.scheme != 'http' or not canonical_peer(base.hostname or '') or not base.port
                or base.path or base.query or base.fragment or base.username or base.password
                or not is_bridge_station(station) or not peers
                or len(set(peers.values())) != len(peers)
                or any(not canonical_peer(ip) or not identity for ip, identity in peers.items())):
            raise ValueError('AIRPLAY_PRIVATE_PROVIDER_SCOPE_REQUIRED')
        self.factory, self.peers, self.guard = session_factory, dict(peers), guard
        self.host, self.station_id = base.netloc, station.id
        self.snapshot = self.fingerprint(station)
        descriptor = StationDescriptor(station.name, station.stream_url, '', str(station.id),
            stream_url_resolved=station.stream_url, stream_format='mp3', stream_mime='audio/mpeg')
        self.encoded = unquote(encode_orion_data(descriptor))
        self.location = f'{base_url}{ORION_STATION_PATH}?data={encode_orion_data(descriptor)}'
        self.now_path = f'/bmx/orion/now-playing/station/{station.id}'
        self.report_path = f'/bmx/orion/reporting/station/{station.id}'
        self.paths = {('GET', ORION_STATION_PATH), ('GET', self.now_path), ('POST', self.report_path)}
        app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

        @app.get(ORION_STATION_PATH)
        async def station_response(request: Request) -> JSONResponse:
            if (list(request.query_params.keys()) != ['data']
                    or len(request.query_params.multi_items()) != 1
                    or request.query_params['data'] != self.encoded):
                return JSONResponse({'error': 'AIRPLAY_STATION_MISMATCH'}, status_code=403)
            with self.factory() as db:
                return await cloud.orion_station(request, db)

        @app.get(self.now_path)
        async def metadata_response(request: Request) -> JSONResponse:
            with self.factory() as db:
                return await cloud.now_playing(request, str(self.station_id), db)

        @app.post(self.report_path)
        async def reporting_response(request: Request) -> JSONResponse:
            with self.factory() as db:
                return await cloud.reporting(request, str(self.station_id), db)

        self.app = app
        self.ingress = DeviceIngress(tuple(peers))

    @staticmethod
    def fingerprint(station: Station) -> tuple[object, ...]:
        return (station.id, station.name, station.stream_url, station.stream_url_resolved,
                station.provider_station_id, station.internal, station.lab_only, station.purpose)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            # Uvicorn is explicitly configured with lifespan="off".
            return
        peer = canonical_peer(str((scope.get('client') or ('', 0))[0]))
        identity = self.peers.get(peer)
        headers = scope.get('headers', [])
        hosts = [value for key, value in headers if key.lower() == b'host']
        claims = [value.decode('ascii', errors='replace').strip().upper() for key, value in headers
                  if key.lower() in {b'x-basswiesn-device-id', b'x-bose-device-id', b'x-device-id'}]
        if ((scope.get('method'), scope.get('path')) not in self.paths
                or identity is None or hosts != [self.host.encode('ascii')]
                or any(value != identity for value in claims)):
            await reject(send, 'AIRPLAY_PROVIDER_SCOPE_DENIED', 403)
            return
        try:
            self.guard()
            if is_device_access_protected(peer, identity):
                raise ValueError('protected')
            with self.factory() as db:
                known = db.query(Device).filter_by(device_id=identity, ip_address=peer).one_or_none()
                row = db.get(Station, self.station_id)
                if known is None or row is None or self.fingerprint(row) != self.snapshot:
                    raise ValueError('scope changed')
        except Exception:
            await reject(send, 'AIRPLAY_PROVIDER_OWNER_UNAVAILABLE', 503)
            return
        await self.ingress(self.app, scope, receive, send, device_id=identity)
