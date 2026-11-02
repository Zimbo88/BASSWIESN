"""Account-isolated LAB group records. No radio command or topology assertion.

Required-mode callbacks may change these local records only after inventory
binding. They are not the SoundTouch Zone coordinator and cannot prove audio
synchronization. Legacy in-memory compatibility remains separate.
"""
from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
import re
from typing import Literal, TypedDict, cast

from sqlalchemy import delete, func, literal, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from basswiesn.app.core.device_ingress import canonical_peer
from basswiesn.app.core.errors import BasswiesnError
from basswiesn.app.core.protocol_identity import account_from_info
from basswiesn.app.models import Device, Setting
from basswiesn.app.services.protected_devices import is_device_access_protected

PREFIX = "lab.cloud-group.v1:"
MAX_GROUPS = 128
Action = Literal["state", "create", "update", "delete"]


class GroupError(BasswiesnError):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code, self.status_code = code, status
        super().__init__("Local LAB group state could not be authorized or updated; no radio command was sent")


class Group(TypedDict):
    group_id: str
    leader: str
    members: list[str]
    source: str
    status: str
    created_at: str
    updated_at: str
    revision: int


def identifier(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        raise GroupError("GROUP_SCHEMA_INVALID")
    return value


def text_field(value: object, *, limit: int = 256) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value):
        raise GroupError("GROUP_SCHEMA_INVALID")
    return value


def inventory(db: Session, peer: str) -> tuple[str, str, dict[str, Device]]:
    devices = list(db.scalars(select(Device)))
    senders = [device for device in devices if canonical_peer(device.ip_address) == peer]
    if not peer or len(senders) != 1:
        raise GroupError("GROUP_IDENTITY_DENIED", 403)
    sender = senders[0]
    account = account_from_info(sender.info_xml or "")
    if not account or is_device_access_protected(sender.ip_address, sender.device_id):
        raise GroupError("GROUP_IDENTITY_DENIED", 403)
    by_id = {str(device.device_id).upper(): device for device in devices}
    if len(by_id) != len(devices):
        raise GroupError("GROUP_IDENTITY_AMBIGUOUS", 403)
    return identifier(sender.device_id.upper()), account, by_id


def validate_members(group: Group, account: str, devices: dict[str, Device]) -> None:
    members = group["members"]
    if (not isinstance(members, list) or len(members) > 32
            or any(not isinstance(item, str) for item in members)):
        raise GroupError("GROUP_SCHEMA_INVALID")
    ids = [identifier(group["leader"]).upper(), *(identifier(item).upper() for item in members)]
    if len(set(ids)) != len(ids):
        raise GroupError("GROUP_SCHEMA_INVALID")
    for identity in ids:
        device = devices.get(identity)
        if (device is None or account_from_info(device.info_xml or "") != account
                or is_device_access_protected(device.ip_address, device.device_id)):
            raise GroupError("GROUP_MEMBER_DENIED", 403)
    group["leader"], group["members"] = ids[0], ids[1:]


def load_group(raw: str, account: str) -> Group:
    try:
        if len(raw) > 32768:
            raise ValueError
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("schema") != 1 or value.get("account") != account
                or not isinstance(value.get("group"), dict)):
            raise ValueError
        group = value["group"]
        if set(group) != {"group_id", "leader", "members", "source", "status", "created_at", "updated_at", "revision"}:
            raise ValueError
        identifier(group["group_id"])
        identifier(group["leader"])
        for key in ("source", "status", "created_at", "updated_at"):
            text_field(group[key])
        if type(group["revision"]) is not int or group["revision"] < 1:
            raise ValueError
        if (not isinstance(group["members"], list) or len(group["members"]) > 32
                or any(not isinstance(member, str) for member in group["members"])):
            raise ValueError
        identifiers = [identifier(group["leader"]).upper(), *(identifier(member).upper() for member in group["members"])]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError
        return cast(Group, group)
    except (ValueError, TypeError, GroupError):
        raise GroupError("GROUP_STORED_STATE_INVALID", 503) from None


def _operation(db: Session, *, peer: str, action: Action, payload: object) -> dict[str, object]:
    sender, account, devices = inventory(db, peer)
    prefix = PREFIX + sha256(account.encode()).hexdigest() + ":"
    if action == "state":
        rows = list(db.execute(select(Setting.value).where(Setting.key.startswith(prefix)).limit(MAX_GROUPS + 1)))
        if len(rows) > MAX_GROUPS:
            raise GroupError("GROUP_STORED_STATE_INVALID", 503)
        groups = [load_group(str(row[0]), account) for row in rows]
        for group in groups:
            validate_members(group, account, devices)
        return {"groups": groups, "clockSyncAssist": "lab", "leaderFailover": "state-only",
                "state_only": True, "hardware_verified": False}
    if not isinstance(payload, dict) or set(payload) - {
        "group_id", "id", "leader", "master", "members", "source", "status", "expected_revision",
    }:
        raise GroupError("GROUP_SCHEMA_INVALID")
    if action == "delete" and set(payload) - {"group_id", "id", "expected_revision"}:
        raise GroupError("GROUP_SCHEMA_INVALID")
    if "group_id" in payload and "id" in payload and payload["group_id"] != payload["id"]:
        raise GroupError("GROUP_SCHEMA_INVALID")
    if "leader" in payload and "master" in payload and payload["leader"] != payload["master"]:
        raise GroupError("GROUP_SCHEMA_INVALID")
    group_id = identifier(payload.get("group_id", payload.get("id")))
    key = prefix + group_id
    raw = db.execute(select(Setting.value).where(Setting.key == key)).scalar_one_or_none()
    if action == "create":
        if raw is not None:
            raise GroupError("GROUP_ALREADY_EXISTS", 409)
        now = datetime.now(UTC).isoformat()
        group = Group(group_id=group_id, leader=identifier(payload.get("leader", payload.get("master"))).upper(),
                      members=payload.get("members", []), source=text_field(payload.get("source", "")),
                      status=text_field(payload.get("status", "active"), limit=32),
                      created_at=now, updated_at=now, revision=1)
    else:
        if raw is None:
            raise GroupError("GROUP_NOT_FOUND", 404)
        group = load_group(str(raw), account)
        if group["group_id"] != group_id:
            raise GroupError("GROUP_STORED_STATE_INVALID", 503)
        if group["leader"] != sender:
            raise GroupError("GROUP_OWNER_DENIED", 403)
        if "expected_revision" in payload and (
            type(payload["expected_revision"]) is not int or payload["expected_revision"] != group["revision"]
        ):
            raise GroupError("GROUP_REVISION_MISMATCH", 409)
        if action == "delete":
            changed = db.execute(delete(Setting).where(Setting.key == key, Setting.value == raw)
                                 .returning(Setting.id).execution_options(synchronize_session=False)).scalar_one_or_none()
            if changed is None:
                raise GroupError("GROUP_CONCURRENT_CHANGE", 409)
            db.commit()
            return {"ok": True, "deleted": True, "group_id": group_id, "state_only": True, "hardware_verified": False}
        group = {**group, "revision": group["revision"] + 1, "updated_at": datetime.now(UTC).isoformat()}
        if "leader" in payload or "master" in payload:
            group["leader"] = identifier(payload.get("leader", payload.get("master"))).upper()
        if "members" in payload:
            group["members"] = payload["members"]
        if "source" in payload:
            group["source"] = text_field(payload["source"])
        if "status" in payload:
            group["status"] = text_field(payload["status"], limit=32)
    if group["leader"] != sender:
        raise GroupError("GROUP_OWNER_DENIED", 403)
    validate_members(group, account, devices)
    encoded = json.dumps({"schema": 1, "account": account, "group": group}, sort_keys=True, separators=(",", ":"))
    if action == "create":
        # Capacity and uniqueness are decided by one SQL statement, not a
        # racy count-then-insert across web/cloud workers.
        count = select(func.count()).select_from(Setting).where(Setting.key.startswith(PREFIX)).scalar_subquery()
        create_statement = insert(Setting).from_select(
            ["key", "value"], select(literal(key), literal(encoded)).where(count < MAX_GROUPS)
        ).on_conflict_do_nothing(index_elements=["key"]).returning(Setting.id)
        changed = db.execute(create_statement).scalar_one_or_none()
    else:
        change_statement = update(Setting).where(Setting.key == key, Setting.value == raw).values(value=encoded).returning(Setting.id)
        changed = db.execute(change_statement.execution_options(synchronize_session=False)).scalar_one_or_none()
    if changed is None:
        raise GroupError("GROUP_CAPACITY_OR_CONCURRENT_CHANGE", 409)
    db.commit()
    return {"ok": True, "group": group, "state_only": True, "hardware_verified": False}


def group_operation(db: Session, *, peer: str, action: Action, payload: object = None) -> dict[str, object]:
    try:
        return _operation(db, peer=peer, action=action, payload=payload)
    except SQLAlchemyError:
        db.rollback()
        raise GroupError("GROUP_STORAGE_UNAVAILABLE", 503) from None
    except GroupError:
        db.rollback()
        raise
