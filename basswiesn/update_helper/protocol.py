"""Strict, bounded local protocol for the host update service.

There are no URL, filesystem path, shell command, environment or Docker options
in a request. Peer identity must come from SO_PEERCRED, never request JSON.
An update credential is a generated 256-bit administrator capability, NOT the
ordinary unauthenticated Web UI's confirmation phrase. Never persist it in a
journal/response or include it in a repr/exception. Initial provisioning retains
it only in a separate administrator-private credential file; it is not delivered
to the Web container. An administrator supplies it for an explicit HTTPS action.
HTTP origin/CSRF/TLS enforcement belongs at the Web boundary, not this protocol.
"""
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import hmac
import json
import re


MAX_REQUEST_BYTES = 2048
VERSION = re.compile(r"(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})")
REQUEST_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
DIGEST = re.compile(r"[0-9a-f]{64}")
CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{43}")


class Code(StrEnum):
    BAD_REQUEST = "BAD_REQUEST"
    PEER_DENIED = "PEER_DENIED"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    BUSY = "BUSY"
    REQUEST_ID_REUSED = "REQUEST_ID_REUSED"
    CURRENT_VERSION_CHANGED = "CURRENT_VERSION_CHANGED"
    NOT_AN_UPGRADE = "NOT_AN_UPGRADE"
    JOURNAL_CAPACITY = "JOURNAL_CAPACITY"
    JOURNAL_UNSAFE = "JOURNAL_UNSAFE"
    JOURNAL_CORRUPT = "JOURNAL_CORRUPT"
    JOURNAL_IO = "JOURNAL_IO"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    INTERRUPTED_BEFORE_CUTOVER = "INTERRUPTED_BEFORE_CUTOVER"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    SOURCE_VERIFICATION_FAILED = "SOURCE_VERIFICATION_FAILED"
    STAGING_FAILED = "STAGING_FAILED"
    STOP_FAILED = "STOP_FAILED"
    BACKUP_FAILED = "BACKUP_FAILED"
    START_FAILED = "START_FAILED"
    HEALTH_FAILED = "HEALTH_FAILED"
    RESTORE_FAILED = "RESTORE_FAILED"
    EXECUTOR_UNAVAILABLE = "EXECUTOR_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class UpdateRejected(ValueError):
    def __init__(self, code: Code):
        self.code = Code(code)
        super().__init__(self.code.value)


def require_version(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise UpdateRejected(Code.BAD_REQUEST)
    return value


def require_digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise UpdateRejected(Code.BAD_REQUEST)
    return value


def require_request_id(value):
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise UpdateRejected(Code.BAD_REQUEST)
    return value


def newer(target: str, current: str) -> bool:
    require_version(target)
    require_version(current)
    return tuple(map(int, target.split("."))) > tuple(map(int, current.split(".")))


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise UpdateRejected(Code.BAD_REQUEST)
        value[key] = item
    return value


@dataclass(frozen=True)
class Request:
    action: str
    request_id: str = ""
    target_version: str = ""
    expected_current_version: str = ""
    credential: str = field(default="", repr=False, compare=False)


@dataclass(frozen=True)
class Authorization:
    allowed_peer_uid: int
    credential_sha256: str = field(repr=False)

    def __post_init__(self):
        if type(self.allowed_peer_uid) is not int or self.allowed_peer_uid < 0:
            raise UpdateRejected(Code.PEER_DENIED)
        require_digest(self.credential_sha256)

    def check(self, request: Request, peer_uid: int):
        if type(peer_uid) is not int or peer_uid != self.allowed_peer_uid:
            raise UpdateRejected(Code.PEER_DENIED)
        if request.action not in {"install", "submit", "status"}:
            raise UpdateRejected(Code.BAD_REQUEST)
        if request.action in {"install", "submit"}:
            if not isinstance(request.credential, str) or not CREDENTIAL.fullmatch(request.credential):
                raise UpdateRejected(Code.AUTHORIZATION_REQUIRED)
            digest = hashlib.sha256(request.credential.encode("ascii")).hexdigest()
            if not hmac.compare_digest(digest, self.credential_sha256):
                raise UpdateRejected(Code.AUTHORIZATION_REQUIRED)


def decode_request(data: bytes) -> Request:
    if type(data) is not bytes or not 0 < len(data) <= MAX_REQUEST_BYTES:
        raise UpdateRejected(Code.BAD_REQUEST)
    try:
        value = json.loads(data, object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError):
        raise UpdateRejected(Code.BAD_REQUEST) from None
    if not isinstance(value, dict) or type(value.get("protocol")) is not int or value["protocol"] != 1:
        raise UpdateRejected(Code.BAD_REQUEST)
    if value.get("action") == "status":
        if set(value) != {"protocol", "action"}:
            raise UpdateRejected(Code.BAD_REQUEST)
        return Request("status")
    if (set(value) != {"protocol", "action", "request_id", "target_version",
                      "expected_current_version", "approve", "credential"}
            or value["action"] not in {"install", "submit"} or value["approve"] is not True):
        raise UpdateRejected(Code.BAD_REQUEST)
    if not isinstance(value["credential"], str) or not CREDENTIAL.fullmatch(value["credential"]):
        raise UpdateRejected(Code.AUTHORIZATION_REQUIRED)
    return Request(value["action"], require_request_id(value["request_id"]),
                   require_version(value["target_version"]), require_version(value["expected_current_version"]),
                   value["credential"])
