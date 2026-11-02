"""Read-only updater attestation, authorized by a runtime-local capability.

The launcher rotates the capability in private tmpfs before HTTP workers start.
It is not an administrator credential, is absent from data-volume snapshots and
authorizes only the extended /ready response. No proxy or redirect is trusted.
"""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import urllib.request
from typing import Literal

from starlette.requests import Request

from basswiesn.app.config import get_settings
from basswiesn.app.core.admin_store import AdminStore, AuthUnavailable
from basswiesn.app.services.task_registry import owned_task_status

TOKEN_FILE = Path("/tmp/basswiesn-runtime/probe-token")
HEADER = "X-BASSWIESN-Probe-Token"
Listener = Literal["web", "cloud", "debug"]


class ProbeRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("RUNTIME_PROBE_UNVERIFIED")


def _directory() -> int:
    fd = os.open(TOKEN_FILE.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise ProbeRejected()
    return fd


def initialize() -> None:
    """Trusted launcher only; never called by a request or health check."""
    TOKEN_FILE.parent.mkdir(mode=0o700, exist_ok=True)
    directory = _directory()
    leaf = "new-" + secrets.token_hex(16)
    try:
        fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            value = secrets.token_hex(32).encode("ascii")
            if os.write(fd, value) != len(value):
                raise ProbeRejected()
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(leaf, TOKEN_FILE.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        # Only this invocation's newly created temporary leaf can be removed.
        try:
            os.unlink(leaf, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _token() -> str:
    directory = _directory()
    try:
        fd = os.open(TOKEN_FILE.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size != 64):
                raise ProbeRejected()
            value = os.read(fd, 65).decode("ascii")
            if not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ProbeRejected()
            return value
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def authorized(request: Request) -> bool:
    try:
        return bool(request.method == "GET" and request.client
            and ipaddress.ip_address(request.client.host).is_loopback
            and not request.headers.get("origin") and not request.headers.get("sec-fetch-site")
            and secrets.compare_digest(request.headers.get(HEADER, "").encode(), _token().encode()))
    except (OSError, ValueError):
        return False


def attestation(listener: Listener) -> dict[str, object]:
    """Called only after authorization, in the actual serving worker."""
    settings = get_settings()
    auth_ready = settings.admin_auth_mode != "required"
    if not auth_ready:
        try:
            with AdminStore(settings.data_dir / "secrets/admin/auth.sqlite3")._db() as db:
                row = db.execute("SELECT username,password_hash FROM administrator WHERE id=1").fetchone()
                auth_ready = bool(row and row["username"] and row["password_hash"])
        except AuthUnavailable:
            auth_ready = False
    storage_ok = all(path.is_dir() and not path.is_symlink() and os.access(path, os.W_OK)
                     for path in (settings.data_dir, settings.data_dir / "logs", settings.data_dir / "tmp"))
    return {"contract": 1, "version": settings.version, "listener": listener,
            "validation_mode": settings.update_validation_mode, "auth_mode": settings.admin_auth_mode,
            "background_tasks": len(owned_task_status()), "storage_ok": storage_ok,
            "auth_store_ready": auth_ready}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: object, code: int, msg: str,
                         headers: object, newurl: str) -> None:
        raise ProbeRejected()


def _read_listener(port: int, token: str) -> object:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(f"http://127.0.0.1:{port}/ready", headers={HEADER: token})
    with opener.open(request, timeout=2) as response:
        raw = response.read(4097)
        if response.status != 200 or len(raw) > 4096:
            raise ProbeRejected()
        return json.loads(raw)


def verify_runtime(*, validation: bool) -> dict[str, object]:
    """Executed INSIDE the inspected container, never from a LAN client.

    Imported configuration alone is insufficient: every live worker must attest
    its version/mode/readiness. Direct SQLite integrity/schema checking follows.
    """
    from sqlalchemy import text
    from basswiesn.app.db import engine, Base
    import basswiesn.app.models  # noqa: F401

    settings = get_settings()
    if settings.update_validation_mode is not validation:
        raise ProbeRejected()
    token = _token()
    for listener, port in (("web", settings.web_port), ("cloud", settings.cloud_port), ("debug", settings.debug_port)):
        body = _read_listener(port, token)
        if (not isinstance(body, dict) or set(body) != {"ready", "contract", "version", "listener",
                "validation_mode", "auth_mode", "background_tasks", "storage_ok", "auth_store_ready"}
                or body["ready"] is not True or type(body["contract"]) is not int or body["contract"] != 1
                or body["version"] != settings.version or body["listener"] != listener
                or body["validation_mode"] is not validation or body["auth_mode"] != settings.admin_auth_mode
                or body["storage_ok"] is not True or body["auth_store_ready"] is not True
                or type(body["background_tasks"]) is not int or body["background_tasks"] < 0
                or (validation and body["background_tasks"] != 0)):
            raise ProbeRejected()
    if engine.dialect.name != "sqlite":
        raise ProbeRejected()
    with engine.connect() as connection:
        if [tuple(row) for row in connection.execute(text("PRAGMA quick_check"))] != [("ok",)]:
            raise ProbeRejected()
        rows = [list(row) for row in connection.execute(text("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))]
        tables = {row[1] for row in rows if row[0] == "table"}
        if not set(Base.metadata.tables) <= tables:
            raise ProbeRejected()
    if not validation:
        return {"ready": True, "version": settings.version}
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"ready": True, "version": settings.version, "validation_mode": True,
            "sqlite_integrity": True, "schema_sha256": digest, "table_count": len(tables)}
