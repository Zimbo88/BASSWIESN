"""Offline acceptance-record validation. Never grants a runtime capability.

Hashes establish file integrity, not hardware provenance. Even a complete record
requires human review against the witnessed experiment before production use.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from basswiesn.cli_contract import OfflineError
from basswiesn.cli_storage import checked_path

Text = Annotated[str, Field(min_length=1, max_length=200)]
Scalar = str | int | float | bool | None


class RecordModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Identity(RecordModel):
    device_id: Annotated[str, Field(pattern=r"^[A-F0-9]{12}$")]
    model: Text
    firmware: Text
    variant: Text
    platform: Text

    @model_validator(mode="after")
    def exact(self) -> Identity:
        for value in self.model_dump().values():
            if value.upper() in {"UNKNOWN", "ANY", "UNVERIFIED", "N/A"} or "*" in value or "?" in value:
                raise ValueError("Exact identity fields required")
        return self


class Evidence(RecordModel):
    role: Literal["START", "END", "COMMAND", "READBACK", "BACKUP", "OBSERVATION"]
    file: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class AcceptanceRecord(RecordModel):
    schema_version: Literal[1]
    test_id: Text
    feature: Text
    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    origin: Literal["SIMULATED", "SOFTWARE_TESTED", "HARDWARE_TESTED"]
    software_result: Literal["PASS", "FAIL", "NOT_RUN"]
    hardware_result: Literal["PASS", "FAIL", "NOT_RUN"]
    writes_performed: bool = False
    expected: Identity | None = None
    observed_start: Identity | None = None
    observed_end: Identity | None = None
    expected_state: dict[str, Scalar] = Field(default_factory=dict, max_length=100)
    readback_state: dict[str, Scalar] = Field(default_factory=dict, max_length=100)
    restore: Literal["NOT_APPLICABLE", "EXACT", "PARTIAL", "MANUAL_REQUIRED"] = "NOT_APPLICABLE"
    observer: Text | None = None
    observed_at: Text | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=16)

    @field_validator("schema_version", mode="before")
    @classmethod
    def literal_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Schema version requires an integer, not coercion")
        return value

    @model_validator(mode="after")
    def evidence_boundary(self) -> AcceptanceRecord:
        if self.origin != "HARDWARE_TESTED":
            if self.hardware_result != "NOT_RUN" or self.writes_performed:
                raise ValueError("Software evidence cannot certify a hardware action")
            return self
        if self.hardware_result == "NOT_RUN":
            raise ValueError("Unperformed hardware tests are not hardware evidence")
        if not self.observer or not self.observed_at:
            raise ValueError("Hardware evidence requires an explicit observer and time")
        stamp = datetime.fromisoformat(self.observed_at)
        if stamp.tzinfo is None:
            raise ValueError("Observation time requires a timezone")
        if not self.expected or not self.observed_start:
            raise ValueError("Expected and actual identity are mandatory")
        roles = {item.role for item in self.evidence}
        if not {"START", "END", "READBACK", "OBSERVATION"}.issubset(roles):
            raise ValueError("Hardware evidence is incomplete")
        if len({item.file for item in self.evidence}) != len(self.evidence):
            raise ValueError("Evidence roles require distinct files")
        if self.hardware_result == "PASS":
            if self.expected != self.observed_start or self.expected != self.observed_end:
                raise ValueError("Identity mismatch cannot be a successful acceptance")
            if (not self.expected_state or self.expected_state != self.readback_state
                    or any(type(value) is not type(self.readback_state[key])
                           for key, value in self.expected_state.items())):
                raise ValueError("Transport success is not matching state readback")
            if self.software_result != "PASS":
                raise ValueError("Hardware acceptance cannot override failed software gates")
        if self.writes_performed:
            if self.expected != self.observed_start:
                raise ValueError("A write after identity mismatch violates the safety contract")
            if not {"BACKUP", "COMMAND"}.issubset(roles) or self.restore == "NOT_APPLICABLE":
                raise ValueError("Writes require backup, command evidence and restore outcome")
            if self.hardware_result == "PASS" and self.restore != "EXACT":
                raise ValueError("Incomplete restore cannot close hardware acceptance")
        return self


def _read(path: Path, maximum: int) -> bytes:
    before = checked_path(path)
    if before.st_size > maximum:
        raise ValueError("Evidence file exceeds its bound")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        pinned = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise ValueError("Evidence changed during open")
        content = handle.read(maximum + 1)
    after = checked_path(path)
    if len(content) > maximum or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("Evidence changed during read")
    return content


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def validate_record(path: Path) -> dict[str, object]:
    record = AcceptanceRecord.model_validate(json.loads(_read(path, 1024 * 1024), object_pairs_hook=_unique_object))
    for evidence in record.evidence:
        if ".." in evidence.file or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", evidence.file):
            raise ValueError("Unsafe evidence filename")
        digest = hashlib.sha256(_read(path.parent / evidence.file, 16 * 1024 * 1024)).hexdigest()
        if digest != evidence.sha256:
            raise ValueError("Evidence hash mismatch")
    # Deliberately no addresses, identities, observer names, filenames or payloads.
    return {"record_validation": "PASS", "origin": record.origin,
            "software_result_declared": record.software_result,
            "hardware_result_declared": record.hardware_result, "files_checked": len(record.evidence),
            "hardware_verified_by_tool": False, "runtime_capability_changed": False,
            "acceptance": "MANUAL_REVIEW_REQUIRED" if record.origin == "HARDWARE_TESTED" else "HARDWARE_NOT_RUN"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path, help="Explicit private local JSON record; no network access")
    args = parser.parse_args(argv)
    try:
        result = validate_record(args.record)
    except (OSError, ValueError, ValidationError, OfflineError, RecursionError):
        print(json.dumps({"record_validation": "FAIL", "code": "INVALID_ACCEPTANCE_EVIDENCE",
                          "hardware_verified_by_tool": False, "runtime_capability_changed": False}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
