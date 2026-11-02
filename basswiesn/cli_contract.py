"""Versioned, allowlisted output contract for the offline administration CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from basswiesn import __version__

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
Verdict: TypeAlias = Literal["PASS", "WARN", "FAIL"]
EXIT_CODES: dict[Verdict, int] = {"PASS": 0, "WARN": 1, "FAIL": 2}


class OfflineError(Exception):
    """A constant diagnostic, never a raw OS/SQLite/configuration exception."""

    def __init__(self, code: str, message: str, remediation: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation


@dataclass(frozen=True)
class Check:
    code: str
    status: Verdict
    message: str
    remediation: str = ""

    def as_dict(self) -> dict[str, JsonValue]:
        return {
            "code": self.code,
            "status": self.status,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass
class Report:
    command: str
    checks: list[Check] = field(default_factory=list)
    data: dict[str, JsonValue] = field(default_factory=dict)

    @property
    def status(self) -> Verdict:
        return max((item.status for item in self.checks), key=EXIT_CODES.__getitem__, default="PASS")

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    def as_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "version": __version__,
            "command": self.command,
            "offline": True,
            "status": self.status,
            "checks": [item.as_dict() for item in self.checks],
            "data": self.data,
        }

    def failure(self, exc: OfflineError) -> None:
        self.checks.append(Check(exc.code, "FAIL", exc.message, exc.remediation))
