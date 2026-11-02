"""Narrow, bounded Unix client. No host privilege, commands, URLs or secret store.

Only an explicitly mounted, root-owned helper may receive the administrator
capability. HTTP transport/origin checks happen before body parsing in the router.
"""
import asyncio
import json
import socket
import stat
import struct
from pathlib import Path

from basswiesn.update_helper.protocol import MAX_REQUEST_BYTES, decode_request
from basswiesn.update_helper.journal import STATES

SOCKET_PATH = "/run/basswiesn-update/control.sock"
MAX_RESPONSE = 32768


class ControlUnavailable(Exception):
    def __init__(self, *, submitted=False):
        self.submitted = submitted
        super().__init__("SUBMISSION_UNCERTAIN" if submitted else "HELPER_UNAVAILABLE")


def available():
    try:
        info = Path(SOCKET_PATH).lstat()
        return stat.S_ISSOCK(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o007
    except OSError:
        return False


def _safe_result(value):
    """Never forward arbitrary helper text or secret-bearing fields to HTTP."""
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        raise ValueError()
    if not value["ok"]:
        from basswiesn.update_helper.protocol import Code
        return {"ok": False, "code": Code(value["code"]).value}
    job = value["result"]
    if not isinstance(job, dict) or job.get("state") not in STATES | {"IDLE"}:
        raise ValueError()
    from basswiesn.update_helper.protocol import require_request_id, require_version, Code
    result = {"state": job["state"], "recovery_required": job.get("recovery_required") is True,
              "executor_available": job.get("executor_available") is True}
    if "request_id" in job:
        result.update(request_id=require_request_id(job["request_id"]),
                      target_version=require_version(job["target_version"]),
                      current_version_before=require_version(job["current_version_before"]))
    if job.get("code") is not None:
        result["code"] = Code(job["code"]).value
    return {"ok": True, "result": result}


async def exchange(body):
    data = json.dumps(body, separators=(",", ":")).encode("ascii")
    decode_request(data)
    if len(data) > MAX_REQUEST_BYTES or not available():
        raise ControlUnavailable()
    writer = None
    submitted = False
    try:
        async with asyncio.timeout(4):
            reader, writer = await asyncio.open_unix_connection(SOCKET_PATH)
            peer = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if struct.unpack("3i", peer)[1] != 0:
                raise ValueError()  # reject a replaced socket BEFORE sending secret
            submitted = body.get("action") == "submit"
            writer.write(struct.pack("!I", len(data)) + data)
            await writer.drain()
            count = struct.unpack("!I", await reader.readexactly(4))[0]
            if not 0 < count <= MAX_RESPONSE:
                raise ValueError()
            return _safe_result(json.loads(await reader.readexactly(count)))
    except (OSError, ValueError, KeyError, TypeError, TimeoutError, asyncio.IncompleteReadError):
        raise ControlUnavailable(submitted=submitted) from None
    finally:
        if writer is not None:
            writer.close()


async def status():
    return await exchange({"protocol": 1, "action": "status"})
