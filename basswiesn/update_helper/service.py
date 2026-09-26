"""Bounded single-request Unix connection handler, used by the sealed daemon.

This module neither opens a TCP listener nor invokes a host command. The
root-owned Unix socket/service uses this handler after explicit onboarding.
Authentication always precedes reading transaction state or calling an adapter.
"""
import json
import socket
import struct
import time

from .protocol import Authorization, Code, MAX_REQUEST_BYTES, UpdateRejected, decode_request
from .transaction import TransactionRunner


class UpdateService:
    def __init__(self, authorization: Authorization, runner: TransactionRunner):
        self.authorization = authorization
        self.runner = runner

    def handle(self, data: bytes, *, peer_uid: int, acknowledge=None):
        try:
            # Reject unknown peers even before parsing their input.
            if type(peer_uid) is not int or peer_uid != self.authorization.allowed_peer_uid:
                raise UpdateRejected(Code.PEER_DENIED)
            request = decode_request(data)
            self.authorization.check(request, peer_uid)
            result = (self.runner.journal.public_status() if request.action == "status"
                      else self.runner.execute(request, on_reserved=(acknowledge if request.action == "submit" else None)))
            if request.action == "status":
                result = {**result, "executor_available": self.runner.executor is not None}
            return {"ok": True, "result": result}
        except UpdateRejected as error:
            return {"ok": False, "code": error.code.value}
        except Exception:
            return {"ok": False, "code": Code.INTERNAL_ERROR.value}


def _receive(connection, count, deadline):
    data = bytearray()
    while len(data) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UpdateRejected(Code.BAD_REQUEST)
        connection.settimeout(remaining)
        chunk = connection.recv(count - len(data))
        if not chunk:
            raise UpdateRejected(Code.BAD_REQUEST)
        data.extend(chunk)
    return bytes(data)


def serve_connection(connection: socket.socket, service: UpdateService, *, request_timeout=5.0):
    """One 4-byte big-endian length + JSON frame, then close (no keepalive).

    The timeout bounds the entire request, not each byte. Only local kernel peer
    credentials are authoritative. Install execution stays inside the helper
    even if a client disconnects; host adapters enforce their own timeouts.
    There is no exception/output/credential logging in this boundary.
    """
    with connection:
        acknowledged = False

        def reply(value):
            data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
            try:
                connection.settimeout(1.0)
                connection.sendall(struct.pack("!I", len(data)) + data)
            except OSError:
                pass  # A lost reply is never permission to undo/replay.

        def acknowledge(job):
            nonlocal acknowledged
            acknowledged = True
            reply({"ok": True, "result": job})

        try:
            if connection.family != socket.AF_UNIX or connection.type != socket.SOCK_STREAM:
                raise UpdateRejected(Code.PEER_DENIED)
            peer = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _, uid, _ = struct.unpack("3i", peer)
            if uid != service.authorization.allowed_peer_uid:
                raise UpdateRejected(Code.PEER_DENIED)
            deadline = time.monotonic() + min(max(float(request_timeout), 0.01), 5.0)
            size = struct.unpack("!I", _receive(connection, 4, deadline))[0]
            if not 0 < size <= MAX_REQUEST_BYTES:
                raise UpdateRejected(Code.BAD_REQUEST)
            result = service.handle(_receive(connection, size, deadline), peer_uid=uid, acknowledge=acknowledge)
        except UpdateRejected as error:
            result = {"ok": False, "code": error.code.value}
        except (OSError, ValueError, OverflowError):
            result = {"ok": False, "code": Code.BAD_REQUEST.value}
        except Exception:
            result = {"ok": False, "code": Code.INTERNAL_ERROR.value}
        if not acknowledged:
            reply(result)
