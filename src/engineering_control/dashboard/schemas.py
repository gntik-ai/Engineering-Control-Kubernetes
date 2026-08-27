from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DashboardModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActivityEventInput(DashboardModel):
    level: str = Field(pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    event_type: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]+$")
    source: str = Field(pattern=r"^(controller|worker|dashboard)$")
    host: str | None = Field(default=None, max_length=253)
    worker_kind: str | None = Field(default=None, max_length=32)
    project: str | None = Field(default=None, pattern=r"^(falcone|musematic|llmwiki|platform)$")
    workflow_id: str | None = Field(default=None, max_length=240)
    temporal_run_id: str | None = Field(default=None, max_length=240)
    issue_number: int | None = Field(default=None, ge=1)
    campaign_id: str | None = Field(default=None, max_length=240)
    entity_type: str | None = Field(default=None, max_length=64)
    entity_id: str | None = Field(default=None, max_length=240)
    current_phase: str | None = Field(default=None, max_length=120)
    message: str = Field(min_length=1, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None


class PasswordChangeInput(DashboardModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=14, max_length=128)
    confirmation: str = Field(min_length=14, max_length=128)
