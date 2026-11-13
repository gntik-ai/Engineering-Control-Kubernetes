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
    # Removing a project needs the password again.
    current_password: str = Field(min_length=1, max_length=128)


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
    # What the item asks for, in the administrator's words.
    description: str | None = Field(default=None, max_length=40000)


class ItemEditInput(DashboardModel):
    """New words for an item that is not an issue of the repository. A field left out is
    left as it is."""

    csrf_token: str = Field(min_length=20, max_length=200)
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    description: str | None = Field(default=None, max_length=40000)


class SectionRunInput(DashboardModel):
    """Start what a section runs by itself. ``scope`` narrows it; empty is everything."""

    csrf_token: str = Field(min_length=20, max_length=200)
    scope: str = Field(default="", max_length=2000)


class ItemRouteInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    section: str | None = Field(default=None, max_length=40)


class ItemStateInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    state: str = Field(pattern=r"^(proposed|accepted|skipped|done)$")


class ItemBulkInput(DashboardModel):
    """One change to every item the filters match. It needs the password again."""

    csrf_token: str = Field(min_length=20, max_length=200)
    current_password: str = Field(min_length=1, max_length=128)
    # The filters, as on the page: a section key or "none", a state, a search.
    section: str | None = Field(default=None, max_length=40)
    state: str | None = Field(default=None, pattern=r"^(proposed|accepted|skipped|done)$")
    search: str | None = Field(default=None, max_length=200)
    # How many items the administrator was shown.
    expected: int = Field(ge=1, le=5000)
    # Exactly one of the three.
    route_to: str | None = Field(default=None, max_length=40)
    clear_route: bool = False
    state_to: str | None = Field(default=None, pattern=r"^(proposed|accepted|skipped|done)$")


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


class QueueCreateInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    items: list[UUID] = Field(min_length=1, max_length=200)
    at_once: int = Field(default=1, ge=1, le=4)


class QueueItemsInput(DashboardModel):
    """Items to add to a queue, or the items that still wait in their new order."""

    csrf_token: str = Field(min_length=20, max_length=200)
    items: list[UUID] = Field(min_length=1, max_length=200)


class QueueUpdateInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    at_once: int | None = Field(default=None, ge=1, le=4)


class BudgetInput(DashboardModel):
    """The limits of one scope. A limit left out does not limit."""

    csrf_token: str = Field(min_length=20, max_length=200)
    scope: str = Field(pattern=r"^(platform|project|account)$")
    scope_id: UUID | None = None
    at_once: int | None = Field(default=None, ge=1, le=100)
    per_day: int | None = Field(default=None, ge=1, le=10000)


class SupervisorMessageInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    text: str = Field(min_length=1, max_length=8000)


class SupervisorDecisionInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    accept: bool


class SupervisorDefinitionInput(DashboardModel):
    """A new version of the supervisor's definition. It decides what the supervisor does
    without asking, so saving it needs the password again."""

    csrf_token: str = Field(min_length=20, max_length=200)
    current_password: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=20000)
    skills: list[dict[str, Any]] = Field(max_length=20)
    tools: dict[str, dict[str, Any]]
    providers: list[str] = Field(min_length=1, max_length=4)
    account_id: UUID | None = None
    exclusive: bool = False
    models: dict[str, str] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=300)
    # Whether the supervisor looks at failed and stuck work by itself, and how often
    # within an hour. Left out: the platform's default.
    operations: dict[str, Any] | None = None


class AccountCreateInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(pattern=r"^[a-z]{2,16}$")


class AccountUpdateInput(DashboardModel):
    """A field left out is left as it is."""

    csrf_token: str = Field(min_length=20, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    max_concurrent: int | None = Field(default=None, ge=1, le=8)
    paused: bool | None = None


class AccountSignInInput(DashboardModel):
    """What the provider's client produced when the administrator signed in."""

    csrf_token: str = Field(min_length=20, max_length=200)
    credential: str = Field(min_length=1, max_length=70000)


class PlainChangeInput(DashboardModel):
    """A change that needs nothing but the session's token."""

    csrf_token: str = Field(min_length=20, max_length=200)


class RunDecisionInput(DashboardModel):
    """What to do with the change of a run that waits. It needs the password again."""

    csrf_token: str = Field(min_length=20, max_length=200)
    current_password: str = Field(min_length=1, max_length=128)
    decision: str = Field(pattern=r"^(publish|discard)$")


class AccountRemoveInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    # Removing an account needs the password again.
    current_password: str = Field(min_length=1, max_length=128)
