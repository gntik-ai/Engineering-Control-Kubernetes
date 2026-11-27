from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

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


class ProjectCreateInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=32)
    display_name: str | None = Field(default=None, max_length=200)
    repository: str = Field(min_length=3, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ProjectRenameInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)


class ProjectDescriptionInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    description: str | None = Field(max_length=2000)


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
    version: int | None = Field(default=None, ge=0)


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


class ProjectApplicationInput(DashboardModel):
    """Where a project's application runs, and the test identity a step signs in with.
    The identity is stored in the cluster and never read back."""

    csrf_token: str = Field(min_length=20, max_length=200)
    url: str = Field(default="", max_length=300)
    # The other hosts the application uses: its API, where it signs in.
    hosts: list[str] | None = Field(default=None, max_length=16)
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=400)
    forget_identity: bool = False


class ProjectCommandsInput(DashboardModel):
    """How a project is installed and how its tests are run, as command lines; or, with
    ``platform``, the commands of the platform's own file again. Confirmed with the
    password: the commands are run from the next run on."""

    csrf_token: str = Field(min_length=20, max_length=200)
    current_password: str = Field(min_length=1, max_length=128)
    install: list[str] = Field(default_factory=list, max_length=12)
    tests: list[str] = Field(default_factory=list, max_length=12)
    platform: bool = False


class CommandProfileInput(ProjectCommandsInput):
    name: str = Field(min_length=1, max_length=80)
    manifest: str | None = Field(default=None, max_length=80)


class CommandSelectionInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    current_password: str = Field(min_length=1, max_length=128)
    profile_id: UUID | None = None


class ProjectPauseInput(DashboardModel):
    """Start no further work on a project, with an optional word on why."""

    csrf_token: str = Field(min_length=20, max_length=200)
    reason: str = Field(default="", max_length=300)


class RecordsInput(DashboardModel):
    """A file of one project's records from the original system, to look at."""

    csrf_token: str = Field(min_length=20, max_length=200)
    records: dict[str, Any]


class RecordsApplyInput(RecordsInput):
    """The same file, to bring over. It changes the backlog and the project, so it needs
    the password again."""

    current_password: str = Field(min_length=1, max_length=128)
    # Bring it over although the original system still had runs under way for the project.
    even_if_running: bool = False


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
    # Exactly one of the four.
    route_to: str | None = Field(default=None, max_length=40)
    clear_route: bool = False
    state_to: str | None = Field(default=None, pattern=r"^(proposed|accepted|skipped|done)$")
    remove: bool = False


class RunsRemoveInput(DashboardModel):
    """Delete the runs that ended at least ``days`` days ago. It needs the password
    again."""

    csrf_token: str = Field(min_length=20, max_length=200)
    current_password: str = Field(min_length=1, max_length=128)
    project: str | None = Field(default=None, max_length=64)
    days: int = Field(default=0, ge=0, le=3650)


class BacklogSyncInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)


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


class ProjectSupervisorMessage(SupervisorMessageInput):
    projects: list[str] = Field(default_factory=list, max_length=64)
    all_reviews: bool = False
    request_id: UUID


class SupervisorStopInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    run_id: UUID


class SupervisorDecisionInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    accept: bool


class SupervisorModelInput(DashboardModel):
    """The model and the reasoning level of the supervisor on one provider. Empty means
    the client's own."""

    csrf_token: str = Field(min_length=20, max_length=200)
    provider: str = Field(min_length=1, max_length=16)
    model: str | None = Field(default=None, max_length=60)
    effort: str | None = Field(default=None, max_length=16)


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
    # How hard the model reasons, by provider. Left out: the client's own default.
    efforts: dict[str, str] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=300)
    # Whether the supervisor looks at failed and stuck work by itself, and how often
    # within an hour. Left out: the platform's default.
    operations: dict[str, Any] | None = None


class AccountCreateInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(pattern=r"^[a-z]{2,16}$")
    # For an account with an API key: which of the provider's addresses it is used at.
    endpoint: str | None = Field(default=None, pattern=r"^[a-z]{2,20}$")


class AccountUpdateInput(DashboardModel):
    """A field left out is left as it is."""

    csrf_token: str = Field(min_length=20, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    max_concurrent: int | None = Field(default=None, ge=1, le=8)
    paused: bool | None = None
    endpoint: str | None = Field(default=None, pattern=r"^[a-z]{2,20}$")
    # What a million tokens cost on each model: read, written, read again from the cache.
    prices: dict[str, dict[str, float | None]] | None = None


class AccountSignInInput(DashboardModel):
    """What the provider's client produced when the administrator signed in."""

    csrf_token: str = Field(min_length=20, max_length=200)
    credential: str = Field(min_length=1, max_length=70000)


class SummariesInput(DashboardModel):
    """Summarise a project's code: which model is asked, and how much the run may do."""

    csrf_token: str = Field(min_length=20, max_length=200)
    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(default="", max_length=100)
    # One of the administrator's agents, in place of a model named here.
    agent: str = Field(default="", max_length=64)
    # How many batches of files at most, and what the questions may cost in US dollars.
    batches: int = Field(default=20, ge=1, le=199)
    dollars: int = Field(default=5, ge=1, le=1000)
    tests: bool = False
    # The model is asked in batches at its provider: answered within hours, at a lower price.
    in_batch: bool = False


class CacheTrialInput(DashboardModel):
    """Try whether a provider reads from its cache: with which model of the account."""

    csrf_token: str = Field(min_length=20, max_length=200)
    model: str = Field(min_length=1, max_length=100)


class PlainChangeInput(DashboardModel):
    """A change that needs nothing but the session's token."""

    csrf_token: str = Field(min_length=20, max_length=200)


class EpicQueueInput(PlainChangeInput):
    epic: UUID


class ExecutionPlanInput(PlainChangeInput):
    version: int = Field(ge=1)
    types: dict[str, Literal["bug", "enhancement"]] = Field(default_factory=dict)
    stories: list[str] | None = None
    order: list[str] | None = None
    requires: dict[str, list[str]] | None = None


class ConnectionInput(PlainChangeInput):
    other: str = Field(min_length=1, max_length=32)
    relation: Literal["depends_on", "used_by", "related"]
    purpose: str = Field(default="", max_length=4000)


class ConnectionAnalysisInput(PlainChangeInput):
    provider: str | None = Field(default=None, max_length=16)
    model: str | None = Field(default=None, max_length=200)


class ConnectionLinkInput(PlainChangeInput):
    source_id: UUID
    target_kind: Literal["epics", "stories", "issues"] | None = None
    target_id: UUID | None = None
    reason: str = Field(default="", max_length=2000)


class ConnectionDecisionInput(PlainChangeInput):
    version: str = Field(min_length=64, max_length=64)
    accept: bool


class OrganizeControlInput(PlainChangeInput):
    action: Literal["stop", "resume", "end"]


class OrganizeAnswerInput(PlainChangeInput):
    # What the administrator says of a case: the Issue goes under that User Story, is a
    # bug, or is left; a link nothing supports is taken off or kept.
    choice: Literal["story", "bug", "leave", "take_off", "keep"]
    story_id: UUID | None = None


class OrganizeGroupAnswerInput(PlainChangeInput):
    # One word for every link of a group that waits: those both models call unrelated
    # are taken off or kept; those in doubt are kept.
    group: Literal["unrelated", "in_doubt"]
    choice: Literal["take_off", "keep"]


class OrganizeEpicAnswerInput(PlainChangeInput):
    # What the administrator says of a case: the User Story belongs to that Epic, or is
    # left as it is; a link to another Epic is taken off or kept.
    choice: Literal["epic", "leave", "take_off", "keep"]
    epic_id: UUID | None = None


class ConnectionCarryOutInput(PlainChangeInput):
    # Carry the plan out as far as the month's limit allows, though it is estimated
    # to cost more.
    within_limit: bool = False


class ConnectionLineInput(PlainChangeInput):
    accept: bool


class ConnectionBulkLinksInput(PlainChangeInput):
    through: AwareDatetime


class EvidencePreviewInput(PlainChangeInput):
    sort: Literal["", "issue", "story", "reason", "decision"] = ""
    direction: Literal["asc", "desc"] = "asc"
    document: dict[str, Any] | None = None
    provider: str = Field(default="", max_length=32)
    model: str = Field(default="", max_length=200)
    page: int = Field(default=1, ge=1, le=10_000_000)
    search: str = Field(default="", max_length=200)


class EvidenceRunInput(PlainChangeInput):
    request_id: UUID
    candidate: str = Field(default="", max_length=260)
    provider: str = Field(default="", max_length=32)
    model: str = Field(default="", max_length=200)
    # None: what the project's settings say a run of Review evidence may use.
    token_cap: int | None = Field(default=None, ge=0, le=2_000_000, strict=True)


class EvidenceControlInput(PlainChangeInput):
    run_id: UUID
    # ``release`` ends the wait for a run whose interrupted call never reported its usage.
    action: Literal["stop", "resume", "end", "release"]


class EvidenceDecisionInput(PlainChangeInput):
    scope: Literal["full", "partial", "unrelated", "unclear"]
    note: str = Field(default="", max_length=2000)
    expected_revision: UUID | None = None


class ReviewBudgetInput(PlainChangeInput):
    expected_limit: int | None = Field(ge=0, le=9_007_199_254_740_991, strict=True)
    token_limit: int = Field(ge=0, le=9_007_199_254_740_991, strict=True)


class ReviewPauseInput(PlainChangeInput):
    paused: bool


class RecordPauseInput(ReviewPauseInput):
    control_id: str = Field(min_length=1, max_length=100)


class WorkspaceControlInput(PlainChangeInput):
    control_id: str = Field(min_length=1, max_length=80)


class WorkspaceCoverageInput(PlainChangeInput):
    version: int = Field(ge=0)
    provider: str | None = Field(default=None, max_length=16)
    model: str | None = Field(default=None, max_length=200)


class ManagedCoverageInput(WorkspaceCoverageInput):
    links_only: bool = False


class WorkspaceImportStep(WorkspaceCoverageInput):
    all_epics: bool = False
    all_stories: bool = False
    all_issues: bool = False
    background: bool = False


class PlanningConfirmation(PlainChangeInput):
    confirmation: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class PlanningStateInput(PlanningConfirmation):
    state: Literal["open", "closed"]


class WorkspaceDescription(PlainChangeInput):
    version: int = Field(ge=0)
    description: str = Field(min_length=1, max_length=4000)


class WorkspaceChoice(DashboardModel):
    id: UUID
    include: bool
    title: str = Field(max_length=300)
    description: str | None = Field(default=None, max_length=40000)
    parents: list[UUID] = Field(default_factory=list)


class WorkspaceReviewInput(PlainChangeInput):
    version: int = Field(ge=0)
    choices: list[WorkspaceChoice]


class ProposalDecisions(PlainChangeInput):
    ids: list[UUID] = Field(min_length=1)
    version: int | None = Field(default=None, ge=0)
    dismiss: bool = False


class ProposalEdit(PlainChangeInput):
    execution_type: Literal["bug", "enhancement"] | None = None
    version: int | None = Field(default=None, ge=0)
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=40000)
    parents: list[UUID] = Field(default_factory=list)


class EpicInput(PlainChangeInput):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=40000)


class NewEpicInput(EpicInput):
    review_id: UUID | None = None


class EpicDescriptionCheck(EpicInput):
    request_id: UUID
    previous: UUID | None = None
    answer: str | None = Field(default=None, max_length=8000)


class StoryInput(EpicInput):
    epic_ids: list[UUID] = Field(default_factory=list)


class OrphanRunInput(PlainChangeInput):
    """A run of a check. With no model named it takes the model that answers, the judge
    and the tokens it stops at from the project's settings."""

    provider: str = Field(default="", max_length=32)
    model: str = Field(default="", max_length=200)
    token_cap: int | None = Field(default=None, ge=1_000, le=5_000_000, strict=True)
    # Ask about every one of them, also those that have an answer already.
    again: bool = False
    # A cheaper model that is asked first: what it settles is kept, and the rest is
    # asked of the model above. Both empty for a run with one model.
    first_provider: str = Field(default="", max_length=32)
    first_model: str = Field(default="", max_length=200)

    # A judge: a model of another maker that answers the same question without seeing
    # the first answer. Both empty for a run without one.
    judge_provider: str = Field(default="", max_length=32)
    judge_model: str = Field(default="", max_length=200)

    def first(self) -> tuple[str, str] | None:
        if not self.first_provider and not self.first_model:
            return None
        return (self.first_provider, self.first_model)

    def judge(self) -> tuple[str, str] | None:
        if not self.judge_provider and not self.judge_model:
            return None
        return (self.judge_provider, self.judge_model)


class GoldLabelInput(PlainChangeInput):
    """The administrator's judgment of one pair of planning records, or none to take it
    back. Which answers a pair allows depends on what it pairs."""

    label: Literal["yes", "no", "unsure", "primary", "related"] | None = None


class DocFoldersInput(PlainChangeInput):
    """The folders that hold a project's documentation, and those among them that do not.
    With no folders given the platform's own list is used again."""

    include: list[str] | None = Field(default=None, max_length=40)
    exclude: list[str] = Field(default_factory=list, max_length=40)


class ModelTrialRunInput(OrphanRunInput):
    """A trial of a model on the questions of a check that have an answer on record."""

    # Ask every one of them, not a sample.
    everything: bool = False


class ReviewSettingsInput(PlainChangeInput):
    """What a project's reviews are set to."""

    # Where there is no judge: the model whose trial on a check agreed is asked first.
    cheaper_first: bool = True
    # How far each kind of change goes without the administrator, by the kind of batch:
    # "off", "propose" or "apply". None leaves it as it is.
    alone: dict[str, Literal["off", "propose", "apply"]] | None = Field(default=None, max_length=8)
    # Which checks run by themselves, by the check: "off", "arrive" or "night". None
    # leaves it as it is.
    runs: dict[str, Literal["off", "arrive", "night"]] | None = Field(default=None, max_length=8)
    # The model asked about what the two did not agree on; empty for none.
    third_provider: str = Field(default="", max_length=32)
    third_model: str = Field(default="", max_length=200)

    def third(self) -> tuple[str, str] | None:
        if not self.third_provider and not self.third_model:
            return None
        return (self.third_provider, self.third_model)

    # The numbers the reviews work to, by their key; none leaves them as they are.
    numbers: dict[str, float] | None = Field(default=None, max_length=40)

    dollar_limit: float = Field(ge=0, le=1000)
    # The models a run takes unless it is told otherwise; empty for none.
    answer_provider: str = Field(default="", max_length=32)
    answer_model: str = Field(default="", max_length=200)
    judge_provider: str = Field(default="", max_length=32)
    judge_model: str = Field(default="", max_length=200)
    # The makers the project's texts may be sent to; none for the defaults.
    makers: list[Annotated[str, Field(min_length=1, max_length=60)]] | None = Field(
        default=None, max_length=200
    )
    # How often an answer goes back when its judge says otherwise.
    rounds: int = Field(default=0, ge=0, le=2)

    def answer(self) -> tuple[str, str] | None:
        return (self.answer_provider, self.answer_model) if self.answer_model else None

    def judge(self) -> tuple[str, str] | None:
        return (self.judge_provider, self.judge_model) if self.judge_model else None


class JudgeTrialRunInput(PlainChangeInput):
    """A measurement of one model as a judge, limited in US dollars."""

    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=200)
    dollars: float = Field(ge=0.05, le=3.0)
    # For an answer from the candidates: a judge that answers unseen, as in a run.
    judge_provider: str = Field(default="", max_length=32)
    judge_model: str = Field(default="", max_length=200)
    # And a third model, asked about what the two do not agree on.
    third_provider: str = Field(default="", max_length=32)
    third_model: str = Field(default="", max_length=200)

    def judge(self) -> tuple[str, str] | None:
        if not self.judge_provider and not self.judge_model:
            return None
        return (self.judge_provider, self.judge_model)

    def third(self) -> tuple[str, str] | None:
        if not self.third_provider and not self.third_model:
            return None
        return (self.third_provider, self.third_model)


class PrimaryEpicRunInput(OrphanRunInput):
    """A run that asks which Epic is the primary one of each User Story under several."""

    # Ask only about the Stories whose texts do not point to one Epic.
    unclear_only: bool = False


class PrimaryEpicInput(PlainChangeInput):
    """The Epic the administrator makes the User Story's primary one."""

    epic_id: UUID


class OrphanDraftInput(PlainChangeInput):
    """The User Story a review said was missing, as the administrator wants it made."""

    title: str = Field(min_length=3, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    epic_id: UUID


class OrphanRunControlInput(PlainChangeInput):
    action: Literal["stop", "resume", "end"]


class OrphanLinkInput(PlainChangeInput):
    """The User Story the administrator puts the issue under."""

    story_id: UUID


class OrphanApproveInput(PlainChangeInput):
    """The answers to approve at once: those of these issues, or every one that can be."""

    item_ids: list[UUID] | None = Field(default=None, max_length=500)


class BugReportInput(PlainChangeInput):
    """A bug as the administrator reports it. Its parts become the text of its issue."""

    title: str = Field(min_length=1, max_length=300)
    happens: str = Field(min_length=1, max_length=10_000)
    expected: str = Field(default="", max_length=10_000)
    steps: str = Field(default="", max_length=10_000)
    seen_in: str = Field(default="", max_length=2_000)
    severity: Literal["critical", "high", "medium", "low"] | None = None


class BugEditInput(PlainChangeInput):
    """A bug's title and text as the administrator changed them."""

    title: str = Field(min_length=1, max_length=300)
    text: str = Field(default="", max_length=40_000)


class BugCloseInput(PlainChangeInput):
    """Why a bug is closed, and what is left on its issue as a comment. A duplicate
    names the bug it duplicates by its number."""

    reason: Literal["fixed", "wont_fix", "duplicate"]
    note: str = Field(default="", max_length=4_000)
    duplicate_of: int | None = Field(default=None, ge=1)


class BugTriageInput(PlainChangeInput):
    """What is decided about a bug. A field that is left out stays as it is; an empty
    severity takes the confirmation back."""

    severity: Literal["", "critical", "high", "medium", "low"] | None = None
    area: str | None = Field(default=None, max_length=60)
    seen_in: str | None = Field(default=None, max_length=200)
    # The pull request or commit that introduced the bug: what makes it a regression.
    caused_by: str | None = Field(default=None, max_length=200)


class BugSeveralInput(PlainChangeInput):
    """One decision for several bugs: a severity or an area, or closing them."""

    item_ids: list[UUID] = Field(min_length=1, max_length=100)
    severity: Literal["", "critical", "high", "medium", "low"] | None = None
    area: str | None = Field(default=None, max_length=60)
    close: Literal["fixed", "wont_fix"] | None = None
    note: str = Field(default="", max_length=4_000)


class BugUnlinkInput(PlainChangeInput):
    """The bugs to take off their User Stories: these, or every bug that is linked."""

    item_ids: list[UUID] | None = Field(default=None, max_length=500)


class MissingStoryInput(EpicInput):
    """A proposed User Story as the administrator words it, and the Epic it goes under."""

    epic_id: UUID


class SplitInput(PlainChangeInput):
    """The titles of the Epics a split makes, one for each group beyond the largest."""

    titles: list[str] = Field(min_length=1, max_length=4)


class BatchInput(PlainChangeInput):
    """Which kind of change to gather into a batch for approval."""

    kind: Literal[
        "reasons",
        "primary",
        "place",
        "unsupported",
        "extra_epics",
        "close",
        "retire_stories",
        "retire_epics",
        "retire_issues",
    ]


class BatchLineInput(PlainChangeInput):
    """The administrator's judgment of one line of a batch, or none to take it back."""

    verdict: Literal["right", "wrong"] | None = None


class PrimaryParentInput(PlainChangeInput):
    """The administrator's choice of a record's primary parent, among its parents."""

    parent_id: UUID


class WorkspaceIssueStories(PlainChangeInput):
    story_ids: list[UUID]
    execution_type: Literal["bug", "enhancement"] | None = None


class WorkspaceIssueInput(EpicInput):
    story_ids: list[UUID] = Field(default_factory=list)
    execution_type: Literal["bug", "enhancement"] | None = None


class FlowDraftInput(DashboardModel):
    """A changed flow, kept as the draft of its key."""

    csrf_token: str = Field(min_length=20, max_length=200)
    document: dict[str, Any]
    note: str | None = Field(default=None, max_length=300)


class TriggerInput(DashboardModel):
    """What starts a section's flow without anyone asking."""

    csrf_token: str = Field(min_length=20, max_length=200)
    section: str = Field(min_length=1, max_length=40)
    kind: str = Field(pattern=r"^(schedule|push|item)$")
    spec: dict[str, Any] = Field(default_factory=dict)


class TriggerUpdateInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    enabled: bool | None = None
    spec: dict[str, Any] | None = None


class FlowCheckInput(DashboardModel):
    """A flow as it is in the editor, to be checked against the rules without saving."""

    csrf_token: str = Field(min_length=20, max_length=200)
    document: dict[str, Any]


class SavedTaskInput(DashboardModel):
    """A task as it is set in a flow, kept under a name to be used in others."""

    csrf_token: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=80)
    task: str = Field(min_length=1, max_length=64)
    settings: dict[str, Any] = Field(default_factory=dict)
    wording: dict[str, str] = Field(default_factory=dict)


class TaskInput(DashboardModel):
    """A task of the administrator's own: a kind of task of the platform's with its
    settings, kept under a name. ``task`` is read when one is made and not after."""

    csrf_token: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=80)
    task: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=600)
    settings: dict[str, Any] = Field(default_factory=dict)
    wording: dict[str, str] = Field(default_factory=dict)


class AgentInput(DashboardModel):
    """One who does work: standing instructions, how it works in a sandbox and over an
    API, and the skills it has."""

    csrf_token: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=600)
    instructions: str = Field(default="", max_length=8000)
    settings: dict[str, Any] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list, max_length=8)


class SkillInput(DashboardModel):
    """Named instructions an agent can be given."""

    csrf_token: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=600)
    text: str = Field(default="", max_length=12000)


class TaskHiddenInput(DashboardModel):
    """Whether a kind of task is left out of the designer's list."""

    csrf_token: str = Field(min_length=20, max_length=200)
    hidden: bool


class FlowCreateInput(DashboardModel):
    """A new flow of the library: brought in as a document, copied from a flow that
    exists, or empty."""

    csrf_token: str = Field(min_length=20, max_length=200)
    key: str = Field(default="", max_length=64)
    name: str = Field(default="", max_length=80)
    summary: str = Field(default="", max_length=400)
    copy_of: str | None = Field(default=None, max_length=64)
    version: int | None = Field(default=None, ge=0)
    document: dict[str, Any] | None = None
    # A part: a flow that other flows run, and that does not run by itself.
    part: bool = False


class FlowPublishInput(DashboardModel):
    """Put a flow's draft in force. Every run that starts afterwards follows it, so it
    needs the password again."""

    csrf_token: str = Field(min_length=20, max_length=200)
    current_password: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=300)


class FlowSimulationInput(DashboardModel):
    """Go through a flow on paper: the document given, or else a version of the flow.
    ``outcomes`` says how tasks end; ``run_id`` takes them from a run that happened."""

    csrf_token: str = Field(min_length=20, max_length=200)
    document: dict[str, Any] | None = None
    version: int | None = Field(default=None, ge=0)
    run_id: UUID | None = None
    outcomes: dict[str, list[str]] = Field(default_factory=dict)
    scope: bool = True
    entries: int = Field(default=2, ge=0, le=6)
    max_rounds: int | None = Field(default=None, ge=1, le=5)


class EvaluationInput(DashboardModel):
    """Try a version of a flow on the work of runs that happened."""

    csrf_token: str = Field(min_length=20, max_length=200)
    version: int = Field(ge=0)
    run_ids: list[UUID] = Field(min_length=1, max_length=8)
    note: str | None = Field(default=None, max_length=300)
    # The flow whose past runs these are, when it is not the flow tried.
    of: str | None = Field(default=None, max_length=64)
    # The models the flow asks are asked in batches, where their providers take them.
    batch: bool = False


class RunDecisionInput(DashboardModel):
    """What to do with the change of a run that waits. It needs the password again."""

    csrf_token: str = Field(min_length=20, max_length=200)
    current_password: str = Field(min_length=1, max_length=128)
    decision: str = Field(pattern=r"^(publish|discard)$")


class AccountRemoveInput(DashboardModel):
    csrf_token: str = Field(min_length=20, max_length=200)
    # Removing an account needs the password again.
    current_password: str = Field(min_length=1, max_length=128)
