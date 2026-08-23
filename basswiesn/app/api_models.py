"""Small stable response contracts for local status endpoints."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class StatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    status: str
    version: str


class VersionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str
    build_type: str


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    ready: bool
    status: str
    version: str
    checks: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class HealthCheckItem(BaseModel):
    name: str
    status: str
    message: str


class HealthCheckResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    summary: str
    checks: list[HealthCheckItem]
