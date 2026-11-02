"""Bounded automation grants, separate from browser and host capabilities."""
from dataclasses import dataclass, field, replace
import hashlib
import json
import re
import secrets
import sqlite3

from basswiesn.app.core.admin_store import AdminStore, AuthUnavailable, LoginRejected

SCOPES = frozenset({"read", "playback", "presets", "multiroom", "diagnostics", "admin"})
MAX_LIFETIME = 90 * 24 * 60 * 60
MAX_RECORDS = 256
SCHEMA = """CREATE TABLE api_tokens (
    id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    scopes TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL,
    last_used INTEGER, revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)))"""
_TOKEN = re.compile(r"bwapi_[a-f0-9]{32}_[A-Za-z0-9_-]{43}\Z")
_ID = re.compile(r"[a-f0-9]{32}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}\Z")


@dataclass(frozen=True)
class TokenRecord:
    id: str
    name: str
    scopes: frozenset[str]
    created_at: int
    expires_at: int
    last_used: int | None
    revoked: bool

    def public(self) -> dict[str, object]:
        return {"id": self.id, "name": self.name, "scopes": sorted(self.scopes),
                "created_at": self.created_at, "expires_at": self.expires_at,
                "last_used": self.last_used, "revoked": self.revoked}


@dataclass(frozen=True)
class IssuedToken:
    record: TokenRecord
    token: str = field(repr=False)


def _scopes(value: object) -> frozenset[str]:
    if (not isinstance(value, list) or not value or len(value) > len(SCOPES)
            or any(not isinstance(item, str) or item not in SCOPES for item in value)
            or len(set(value)) != len(value)):
        raise ValueError("TOKEN_SCOPES_INVALID")
    return frozenset(value)


def _record(row: sqlite3.Row) -> TokenRecord:
    try:
        scopes = _scopes(json.loads(row["scopes"]))
        if (not _ID.fullmatch(row["id"]) or not _NAME.fullmatch(row["name"])
                or type(row["created"]) is not int or type(row["expires"]) is not int
                or not 60 <= row["expires"] - row["created"] <= MAX_LIFETIME
                or row["revoked"] not in (0, 1)
                or (row["last_used"] is not None and type(row["last_used"]) is not int)):
            raise ValueError
        return TokenRecord(row["id"], row["name"], scopes, row["created"], row["expires"],
                           row["last_used"], bool(row["revoked"]))
    except (TypeError, ValueError, KeyError) as exc:
        raise AuthUnavailable("Automation grant is invalid") from exc


class ApiTokenStore:
    def __init__(self, admin: AdminStore) -> None:
        self.admin = admin

    @staticmethod
    def _require_schema(db: sqlite3.Connection) -> None:
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise AuthUnavailable("Local authentication schema upgrade required")

    def _authority(self, db: sqlite3.Connection, cookie: str | None) -> None:
        # None is the trusted local programming interface, never an HTTP input.
        if cookie is not None and self.admin.session_in_transaction(db, cookie) is None:
            raise LoginRejected("Administrator session no longer active")

    def issue(self, name: str, scopes: object, lifetime: int, *, session_cookie: str | None = None) -> IssuedToken:
        allowed = _scopes(scopes)
        if (not isinstance(name, str) or not _NAME.fullmatch(name)
                or type(lifetime) is not int or not 60 <= lifetime <= MAX_LIFETIME):
            raise ValueError("TOKEN_REQUEST_INVALID")
        now = int(self.admin.clock())
        record = TokenRecord(secrets.token_hex(16), name, allowed, now, now + lifetime, None, False)
        token = f"bwapi_{record.id}_{secrets.token_urlsafe(32)}"
        with self.admin._db() as db:
            self._require_schema(db)
            self._authority(db, session_cookie)
            if not db.execute("SELECT 1 FROM administrator WHERE id=1").fetchone():
                raise AuthUnavailable("Local administrator missing")
            # Retain revoked records for inspection, never silently revive them.
            if db.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] >= MAX_RECORDS:
                raise ValueError("TOKEN_RECORD_LIMIT")
            db.execute("INSERT INTO api_tokens VALUES (?,?,?,?,?,?,NULL,0)",
                       (record.id, hashlib.sha256(token.encode()).hexdigest(), name,
                        json.dumps(sorted(allowed)), now, now + lifetime))
            self.admin._audit(db, "API_TOKEN_ISSUED", now)
        return IssuedToken(record, token)

    def list(self) -> list[TokenRecord]:
        with self.admin._db() as db:
            self._require_schema(db)
            return [_record(row) for row in db.execute("SELECT * FROM api_tokens ORDER BY created,id LIMIT ?", (MAX_RECORDS,))]

    def verify(self, token: str, *, touch: bool = True) -> TokenRecord | None:
        if not _TOKEN.fullmatch(token):
            return None
        now = int(self.admin.clock())
        with self.admin._db() as db:
            self._require_schema(db)
            row = db.execute("SELECT t.* FROM api_tokens t CROSS JOIN administrator a WHERE a.id=1 AND t.digest=?",
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if row is None:
                return None
            record = _record(row)
            if (record.revoked or now >= record.expires_at or now < record.created_at
                    or record.last_used is not None and now < record.last_used):
                return None
            if touch:
                db.execute("UPDATE api_tokens SET last_used=? WHERE id=?", (now, record.id))
                record = replace(record, last_used=now)
            return record

    def revoke(self, identifier: str, *, session_cookie: str | None = None) -> bool:
        if not _ID.fullmatch(identifier):
            return False
        with self.admin._db() as db:
            self._require_schema(db)
            self._authority(db, session_cookie)
            changed = db.execute("UPDATE api_tokens SET revoked=1 WHERE id=? AND revoked=0", (identifier,)).rowcount
            if changed:
                self.admin._audit(db, "API_TOKEN_REVOKED", int(self.admin.clock()))
            return bool(changed)

    def rotate(self, identifier: str, *, session_cookie: str | None = None) -> IssuedToken | None:
        if not _ID.fullmatch(identifier):
            return None
        now = int(self.admin.clock())
        with self.admin._db() as db:
            self._require_schema(db)
            self._authority(db, session_cookie)
            row = db.execute("SELECT * FROM api_tokens WHERE id=?", (identifier,)).fetchone()
            if row is None:
                return None
            record = _record(row)
            if (record.revoked or not record.created_at <= now < record.expires_at
                    or record.last_used is not None and now < record.last_used):
                return None
            token = f"bwapi_{record.id}_{secrets.token_urlsafe(32)}"
            db.execute("UPDATE api_tokens SET digest=?,last_used=NULL WHERE id=?",
                       (hashlib.sha256(token.encode()).hexdigest(), identifier))
            self.admin._audit(db, "API_TOKEN_ROTATED", now)
            return IssuedToken(replace(record, last_used=None), token)
