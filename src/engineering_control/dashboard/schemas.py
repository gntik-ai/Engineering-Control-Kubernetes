from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from engineering_control.projects import PROJECT_ALTERNATION


class DashboardModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActivityEventInput(DashboardModel):
    level: str = Field(pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    event_type: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]+$")
    source: str = Field(pattern=r"^(controller|worker|dashboard|hermes)$")
    host: str | None = Field(default=None, max_length=253)
    worker_kind: str | None = Field(default=None, max_length=32)
    project: str | None = Field(default=None, pattern=rf"^({PROJECT_ALTERNATION}|platform)$")
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


class ActionCandidateInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    command: dict[str, Any]


class R3CandidateCreateInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)


class ProjectCreateInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=32)
    display_name: str | None = Field(default=None, max_length=200)
    repository: str = Field(min_length=3, max_length=200)


class ProjectRenameInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)


class ProjectArchiveInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)


class SectionGuideInput(DashboardModel):
    """The routing guide of a section. A field left out is left as it is."""

    csrf_token: str = Field(min_length=20, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    use_when: str | None = Field(default=None, max_length=2000)
    avoid_when: str | None = Field(default=None, max_length=2000)
    labels: list[str] | None = Field(default=None, max_length=50)
    examples: list[str] | None = Field(default=None, max_length=50)


class SectionCreateInput(SectionGuideInput):
    name: str = Field(min_length=1, max_length=200)


class SectionUpdateInput(SectionGuideInput):
    name: str | None = Field(default=None, min_length=1, max_length=200)


class SectionRemoveInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)


class SectionFlowInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    # None leaves the section without a flow.
    flow: str | None = Field(default=None, max_length=80)


class ItemCreateInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    title: str = Field(min_length=1, max_length=1000)
    # The key of a section of the project; None leaves the item unrouted.
    section: str | None = Field(default=None, max_length=40)


class ItemRouteInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    section: str | None = Field(default=None, max_length=40)


class ItemStateInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    state: str = Field(pattern=r"^(proposed|accepted|skipped|done)$")


class BacklogSyncInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)


class ActionReauthenticationInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    candidate_id: UUID
    current_password: str = Field(min_length=1, max_length=128)


class ActionExecuteInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    candidate_id: UUID
    reauthentication_token: str = Field(min_length=32, max_length=200)
    confirmation_phrase: str | None = Field(default=None, max_length=120)
