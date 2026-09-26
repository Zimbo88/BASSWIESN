"""User-selected media servers and bounded same-origin audio relay."""
import asyncio
import hashlib
import json
import re
from uuid import uuid4, UUID

import httpx
import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from basswiesn.app.config import get_settings
from basswiesn.app.db import get_db
from basswiesn.app.models import Setting, Station
from basswiesn.app.services.release_scope import require_lab
from basswiesn.app.services.dlna_library import (
    ContentDirectory, DlnaError, FORMATS, MAX_MEDIA, check_not_radio, endpoint, playable_resource,
)

router = APIRouter(prefix="/api/dlna", tags=["media"])
relay_router = APIRouter(tags=["media"])
SERVER_PREFIX = "dlna_server:"
ITEM_PREFIX = "dlna_item:"
client_factory = ContentDirectory
relay_slots = asyncio.Semaphore(4)


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description_url: str = Field(min_length=1, max_length=2048)
    approve: bool = False


class BrowseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_id: str = Field(default="0", min_length=1, max_length=1024)
    start: int = Field(default=0, ge=0, le=1000000)
    count: int = Field(default=50, ge=1, le=100)
    expected_update_id: int | None = Field(default=None, ge=0, le=2**32 - 1)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_id: str = Field(min_length=1, max_length=1024)
    approve: bool = False


class EnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def failure(error):
    code = str(error)
    return HTTPException(status_code=409 if code in {"SERVER_IDENTITY_CHANGED", "ITEM_CHANGED"} else 400,
                         detail={"code": code})


def enabled(db):
    row = setting(db, "dlna_enabled")
    return row.value == "true" if row else get_settings().experimental_dlna


def require_enabled(db):
    if not enabled(db):
        raise HTTPException(409, detail={"code": "FEATURE_DISABLED"})


def setting(db, key):
    return db.query(Setting).filter(Setting.key == key).one_or_none()


def put(db, key, value):
    row = setting(db, key)
    if row is None:
        row = Setting(key=key, value=value)
        db.add(row)
    else:
        row.value = value


def saved_server(db, identity):
    try:
        if str(UUID(identity)) != identity:
            raise ValueError()
        row = setting(db, SERVER_PREFIX + identity)
        if row is None:
            raise ValueError()
        return json.loads(row.value)
    except (ValueError, TypeError):
        raise HTTPException(404, detail={"code": "SERVER_NOT_FOUND"}) from None


async def connected(db, identity):
    require_lab(db)
    require_enabled(db)
    record = saved_server(db, identity)
    client = client_factory()
    server = await client.connect(record["description_url"], expected_udn=record["udn"])
    return client, server


@router.post("/settings")
def settings(payload: EnabledRequest, db: Session = Depends(get_db)):
    if payload.enabled:
        require_lab(db)
    put(db, "dlna_enabled", "true" if payload.enabled else "false")
    db.commit()
    return {"enabled": enabled(db), "background_services_started": False}


@router.get("/servers")
def servers(db: Session = Depends(get_db)):
    rows = db.query(Setting).filter(Setting.key.like(SERVER_PREFIX + "%")).order_by(Setting.key).all()
    return {"enabled": enabled(db), "servers": [dict(json.loads(row.value), id=row.key[len(SERVER_PREFIX):]) for row in rows]}


@router.post("/servers")
async def connect(payload: ConnectRequest, db: Session = Depends(get_db)):
    require_lab(db)
    require_enabled(db)
    if payload.approve is not True:
        raise HTTPException(409, detail={"code": "APPROVAL_REQUIRED"})
    try:
        server = await client_factory().connect(payload.description_url)
    except DlnaError as error:
        raise failure(error) from None
    existing = servers(db)["servers"]
    identity = next((row["id"] for row in existing if row["udn"] == server.udn), None)
    if identity is None and len(existing) >= 16:
        raise HTTPException(409, detail={"code": "SERVER_LIMIT"})
    identity = identity or str(uuid4())
    record = {"name": server.name, "udn": server.udn, "description_url": server.description_url}
    put(db, SERVER_PREFIX + identity, json.dumps(record))
    db.commit()
    return {"id": identity, **record}


@router.delete("/servers/{server_id}")
def forget(server_id: str, db: Session = Depends(get_db)):
    saved_server(db, server_id)
    db.delete(setting(db, SERVER_PREFIX + server_id))
    for row in db.query(Setting).filter(Setting.key.like(ITEM_PREFIX + server_id + ":%")).all():
        db.delete(row)
    db.commit()
    return {"removed": True, "radio_contacted": False, "imported_stations_retained": True}


@router.post("/servers/{server_id}/browse")
async def browse(server_id: str, payload: BrowseRequest, db: Session = Depends(get_db)):
    try:
        client, server = await connected(db, server_id)
        result = await client.browse(server, payload.object_id, start=payload.start, count=payload.count)
        if payload.start and payload.expected_update_id is not None and result["update_id"] != payload.expected_update_id:
            raise DlnaError("ITEM_CHANGED")
    except DlnaError as error:
        raise failure(error) from None
    for item in result["items"]:
        item["formats"] = sorted({r["format"] or r["mime"] for r in item.pop("resources")})
    return result


@router.post("/servers/{server_id}/items")
async def import_item(server_id: str, payload: ImportRequest, db: Session = Depends(get_db)):
    if payload.approve is not True:
        raise HTTPException(409, detail={"code": "APPROVAL_REQUIRED"})
    try:
        client, server = await connected(db, server_id)
        item = (await client.browse(server, payload.object_id, count=1, metadata=True))["items"][0]
        if item["kind"] != "item":
            raise DlnaError("UNSUPPORTED_FORMAT")
        resource = playable_resource(item)
    except DlnaError as error:
        raise failure(error) from None
    token = hashlib.sha256(payload.object_id.encode()).hexdigest()
    key = ITEM_PREFIX + server_id + ":" + token
    if setting(db, key) is None and db.query(Setting).filter(Setting.key.like(ITEM_PREFIX + "%")).count() >= 1000:
        raise HTTPException(409, detail={"code": "ITEM_LIMIT"})
    # Persist the object identity, NOT a user-supplied stream URL. Each relay
    # request resolves it again after fresh server identity verification.
    put(db, key, json.dumps({"object_id": payload.object_id, "format": resource["format"]}))
    url = get_settings().local_base_url.rstrip("/") + "/dlna/audio/" + server_id + "/" + token
    row = db.query(Station).filter(Station.stream_url == url).one_or_none()
    if row is None:
        row = Station(name=item["title"], stream_url=url)
        db.add(row)
    row.name = (item["artist"] + " – " if item["artist"] else "") + item["title"]
    row.stream_url_resolved = url
    row.stream_format = resource["format"]
    row.stream_codec = resource["format"]
    row.stream_mime = resource["mime"]
    row.is_direct_audio = 1
    row.provider = "LOCAL_INTERNET_RADIO"
    db.commit()
    db.refresh(row)
    return {"station_id": row.id, "name": row.name, "format": resource["format"],
            "radio_contacted": False, "presets_changed": False, "playback_verified": False}


@relay_router.get("/dlna/audio/{server_id}/{token}")
async def audio(server_id: str, token: str, request: Request, db: Session = Depends(get_db)):
    require_enabled(db)
    saved_server(db, server_id)
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        raise HTTPException(404, detail={"code": "ITEM_NOT_FOUND"})
    stored = setting(db, ITEM_PREFIX + server_id + ":" + token)
    if stored is None:
        raise HTTPException(404, detail={"code": "ITEM_NOT_FOUND"})
    record = json.loads(stored.value)
    headers = {"Accept-Encoding": "identity"}
    if value := request.headers.get("range"):
        if len(value) > 70 or not re.fullmatch(r"bytes=[0-9]{1,12}-[0-9]{0,12}", value):
            raise HTTPException(416)
        headers["Range"] = value
    try:
        async with asyncio.timeout(1):
            await relay_slots.acquire()
    except TimeoutError:
        raise HTTPException(429, detail={"code": "RELAY_BUSY"}) from None
    http = response = None
    released = False
    async def close():
        nonlocal released
        if released:
            return
        released = True
        try:
            with anyio.CancelScope(shield=True):
                async with asyncio.timeout(2):
                    try:
                        if response is not None:
                            await response.aclose()
                    finally:
                        if http is not None:
                            await http.aclose()
        finally:
            relay_slots.release()
    try:
        client, server = await connected(db, server_id)
        item = (await client.browse(server, record["object_id"], count=1, metadata=True))["items"][0]
        resource = playable_resource(item)
        if resource["format"] != record["format"]:
            raise DlnaError("ITEM_CHANGED")
        address = endpoint(resource["url"], origin=server.description_url)
        check_not_radio(address)
        http = client.http()
        response = await http.send(http.build_request("GET", address, headers=headers), stream=True)
        mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
        length = response.headers.get("content-length", "")
        if (response.status_code not in (200, 206) or response.headers.get("content-encoding", "identity") != "identity"
                or mime not in FORMATS and mime not in {"application/octet-stream", ""}
                or mime in FORMATS and FORMATS[mime] != resource["format"]
                or length and (not length.isdigit() or int(length) > MAX_MEDIA)):
            raise DlnaError("MEDIA_RESPONSE_REJECTED")
        outgoing = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if length:
            outgoing["Content-Length"] = length
        if response.status_code == 206:
            content_range = response.headers.get("content-range", "")
            match = re.fullmatch(r"bytes ([0-9]{1,12})-([0-9]{1,12})/([0-9]{1,12})", content_range)
            if match is None or "Range" not in headers:
                raise DlnaError("MEDIA_RESPONSE_REJECTED")
            first, last, total = map(int, match.groups())
            requested_first, requested_last = headers["Range"][6:].split("-", 1)
            if (not first <= last < total <= MAX_MEDIA or first != int(requested_first)
                    or requested_last and last > int(requested_last)
                    or length and int(length) != last - first + 1):
                raise DlnaError("MEDIA_RESPONSE_REJECTED")
            outgoing["Content-Range"] = content_range
        async def chunks():
            total = 0
            try:
                async with asyncio.timeout(3 * 3600):
                    async for chunk in response.aiter_bytes(65536):
                        total += len(chunk)
                        if total > MAX_MEDIA:
                            raise DlnaError("MEDIA_TOO_LARGE")
                        yield chunk
            finally:
                await close()
        return StreamingResponse(chunks(), status_code=response.status_code, media_type=resource["mime"],
                                 headers=outgoing, background=BackgroundTask(close))
    except (DlnaError, httpx.HTTPError, TimeoutError):
        await close()
        raise HTTPException(502, detail={"code": "MEDIA_UNAVAILABLE"}) from None
    except BaseException:
        await close()
        raise
