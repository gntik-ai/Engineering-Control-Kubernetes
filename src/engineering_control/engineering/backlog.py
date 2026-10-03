from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

BACKLOG_PROJECT_PAIRS: dict[str, tuple[str, str]] = {
    "falcone": ("gntik-ai/falcone", "gntik-ai/falcone-charts"),
    "musematic": ("gntik-ai/musematic", "gntik-ai/musematic-deploy"),
    "llmwiki": ("gntik-ai/llmwiki", "gntik-ai/llmwiki"),
}
BACKLOG_REPOSITORIES = frozenset(
    repository for repositories in BACKLOG_PROJECT_PAIRS.values() for repository in repositories
)
BACKLOG_FILTERS = frozenset({"bugs", "bugs-and-docs", "security", "enhancements", "all"})
BACKLOG_SKIP_REASONS = frozenset(
    {
        "DUPLICATE",
        "ALREADY_FIXED",
        "SUPERSEDED",
        "NEEDS_PRODUCT_DECISION",
        "BLOCKED_BY_DEPENDENCY",
        "BLOCKED_BY_RUNTIME_BASELINE",
        "R3_REQUIRES_OPERATOR",
        "NOT_REPRODUCIBLE",
        "ENHANCEMENT_DEFERRED",
    }
)
BACKLOG_DELIVERY_MODES = frozenset({"source-pr", "full-delivery"})
BACKLOG_APPROVAL_PHRASE = "APPROVE_EXISTING_GITHUB_BACKLOG_LOOP"
BACKLOG_RELEASE_OWNERSHIP_PHRASE = "RELEASE_GITHUB_WRITE_POLICY_OWNERSHIP"
BACKLOG_FIXED_OPERATOR_ACTORS = frozenset({"dashboard:reauthenticated", "cli:engineering-ctl"})
ENGINEERING_CTL_OPERATOR_SUBJECT = "engineering-ctl:hermes"

_SHA = re.compile(r"^[a-f0-9]{40}$")
_TELEGRAM_OPERATOR_ACTOR = re.compile(r"^telegram:[1-9][0-9]{0,18}$")
_REPOSITORY = re.compile(r"^gntik-ai/(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)$")
_ISSUE_URL = re.compile(
    r"^https://github\.com/gntik-ai/(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)/issues/[1-9][0-9]*$"
)
_REFERENCE = re.compile(
    r"(?:(?:gntik-ai/)?(?P<repo>falcone|falcone-charts|musematic|musematic-deploy|llmwiki))?#(?P<number>[1-9][0-9]*)"
)
_DEPENDENCY = re.compile(
    r"(?i)(?:blocked\s+by|depends\s+on|dependency|requires)\s*[:#-]?\s*([^\n]{0,240})"
)


def is_reauthenticated_backlog_operator(actor: str) -> bool:
    """Validate a canonical actor emitted only after channel authentication."""
    return isinstance(actor, str) and bool(
        _TELEGRAM_OPERATOR_ACTOR.fullmatch(actor) or actor in BACKLOG_FIXED_OPERATOR_ACTORS
    )


def canonical_operator_approval_actor(subject: str) -> str:
    """Map an authenticated channel subject to its durable operator identity."""
    if subject == ENGINEERING_CTL_OPERATOR_SUBJECT:
        return "cli:engineering-ctl"
    if isinstance(subject, str) and _TELEGRAM_OPERATOR_ACTOR.fullmatch(subject):
        return subject
    raise ValueError("approval requires an authenticated operator identity")


class BacklogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BacklogIssueType(StrEnum):
    PRODUCT_BUG = "PRODUCT_BUG"
    SECURITY_DEFECT = "SECURITY_DEFECT"
    DEPLOYMENT_DEFECT = "DEPLOYMENT_DEFECT"
    DOCUMENTATION_DEFECT = "DOCUMENTATION_DEFECT"
    OPERATIONS_DEFECT = "OPERATIONS_DEFECT"
    ENHANCEMENT = "ENHANCEMENT"
    SPEC_DECISION = "SPEC_DECISION"
    QUESTION = "QUESTION"
    META_TOOLING = "META_TOOLING"
    DUPLICATE = "DUPLICATE"
    STALE = "STALE"
    ALREADY_FIXED = "ALREADY_FIXED"
    SUPERSEDED = "SUPERSEDED"
    INVALID_OR_UNREPRODUCIBLE = "INVALID_OR_UNREPRODUCIBLE"


class ExistingPullRequestStatus(StrEnum):
    ADOPTABLE_ACTIVE_PR = "ADOPTABLE_ACTIVE_PR"
    STALE_PR = "STALE_PR"
    CONFLICTING_PR = "CONFLICTING_PR"
    UNRELATED_PR = "UNRELATED_PR"
    ALREADY_MERGED = "ALREADY_MERGED"
    CLOSED_WITHOUT_MERGE = "CLOSED_WITHOUT_MERGE"


class ReproductionMode(StrEnum):
    SOURCE_ONLY = "SOURCE_ONLY"
    STATIC_PLUS_TEST = "STATIC_PLUS_TEST"
    RUNTIME_REQUIRED = "RUNTIME_REQUIRED"
    DEPLOYMENT_REQUIRED = "DEPLOYMENT_REQUIRED"
    DOCUMENTATION_REQUIRED = "DOCUMENTATION_REQUIRED"


class DocumentationImpact(StrEnum):
    DOCUMENTATION_UPDATED_IN_CHANGESET = "DOCUMENTATION_UPDATED_IN_CHANGESET"
    DOCUMENTATION_NOT_REQUIRED_WITH_EVIDENCE = "DOCUMENTATION_NOT_REQUIRED_WITH_EVIDENCE"
    SEPARATE_DOCUMENTATION_CHANGESET_CREATED = "SEPARATE_DOCUMENTATION_CHANGESET_CREATED"
    BLOCKED_BY_UNRESOLVED_DOCUMENTATION_IMPACT = "BLOCKED_BY_UNRESOLVED_DOCUMENTATION_IMPACT"


class GitHubIssueSnapshot(BacklogModel):
    repository: str
    number: int = Field(ge=1)
    url: str
    title: str = Field(min_length=1, max_length=300)
    body_excerpt: str = Field(max_length=6000)
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: Literal["OPEN"] = "OPEN"
    labels: tuple[str, ...] = Field(max_length=100)
    milestone: str | None = Field(default=None, max_length=200)
    assignees: tuple[str, ...] = Field(max_length=50)
    comments: tuple[str, ...] = Field(max_length=10)
    updated_at: datetime

    @model_validator(mode="after")
    def bound_identity(self) -> GitHubIssueSnapshot:
        if self.repository not in BACKLOG_REPOSITORIES or not _ISSUE_URL.fullmatch(self.url):
            raise ValueError("GitHub issue snapshot escaped the paired-repository allowlist")
        if f"/{self.repository}/issues/{self.number}" not in self.url:
            raise ValueError("GitHub issue URL does not match its repository and number")
        return self


class GitHubPullRequestSnapshot(BacklogModel):
    repository: str
    number: int = Field(ge=1)
    url: str = Field(pattern=r"^https://github\.com/gntik-ai/[a-z0-9-]+/pull/[1-9][0-9]*$")
    title: str = Field(min_length=1, max_length=300)
    body_excerpt: str = Field(max_length=6000)
    state: Literal["OPEN", "CLOSED", "MERGED"]
    draft: bool
    head_ref: str = Field(min_length=1, max_length=240)
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    base_ref: str = Field(min_length=1, max_length=120)
    merge_state: str = Field(max_length=40)
    updated_at: datetime
    merged_at: datetime | None = None
    closed_at: datetime | None = None
    comments: tuple[str, ...] = Field(max_length=10)
    commits: tuple[str, ...] = Field(max_length=250)

    @field_validator("repository")
    @classmethod
    def paired_repository(cls, value: str) -> str:
        if value not in BACKLOG_REPOSITORIES:
            raise ValueError("pull request repository is outside the backlog allowlist")
        return value


class GitHubRepositoryBacklogSnapshot(BacklogModel):
    repository: str
    default_branch: Literal["main", "master"]
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    issues: tuple[GitHubIssueSnapshot, ...] = Field(max_length=1000)
    pull_requests: tuple[GitHubPullRequestSnapshot, ...] = Field(max_length=1000)

    @field_validator("repository")
    @classmethod
    def paired_repository(cls, value: str) -> str:
        if value not in BACKLOG_REPOSITORIES:
            raise ValueError("repository snapshot is outside the backlog allowlist")
        return value


class GitHubBacklogImportSnapshot(BacklogModel):
    project: Literal["falcone", "musematic", "llmwiki"]
    imported_at: datetime
    repositories: tuple[GitHubRepositoryBacklogSnapshot, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def exact_pair(self) -> GitHubBacklogImportSnapshot:
        expected = tuple(dict.fromkeys(BACKLOG_PROJECT_PAIRS[self.project]))
        if tuple(repository.repository for repository in self.repositories) != expected:
            raise ValueError("GitHub import did not return the authoritative repository pair")
        return self


class LinkedPullRequest(BacklogModel):
    repository: str
    number: int = Field(ge=1)
    url: str
    status: ExistingPullRequestStatus
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")


class BacklogIssueRecord(BacklogModel):
    repository: str
    issue_number: int = Field(ge=1)
    issue_url_reference: str
    title: str = Field(min_length=1, max_length=300)
    state: Literal["OPEN"] = "OPEN"
    labels: tuple[str, ...]
    milestone: str | None = None
    issue_type: BacklogIssueType
    openspec_kind: str | None = Field(default=None, max_length=80)
    severity: Literal["critical", "high", "medium", "low", "unspecified"]
    calculated_risk: Literal["R0", "R1", "R2", "R3"]
    component: str = Field(min_length=1, max_length=200)
    source_or_deployment_owner: Literal["source", "deployment"]
    documentation_levels: tuple[str, ...]
    dependencies: tuple[str, ...]
    blocking_issues: tuple[str, ...]
    issues_blocked: tuple[str, ...]
    linked_prs: tuple[LinkedPullRequest, ...]
    linked_branches: tuple[str, ...]
    linked_commits: tuple[str, ...]
    duplicate_candidates: tuple[str, ...]
    current_source_reproduction: str
    current_runtime_reproduction: str
    current_runtime_required: bool
    reproduction_mode: ReproductionMode
    candidate_status: str
    eligibility_status: str
    eligibility_reason: str
    planned_wave: Literal["W0", "W1", "W2", "W3", "W4", "W5"]
    planned_order: int = Field(ge=0)
    change_set_id: str | None = None
    delivery_status: str
    final_closure_status: str
    documentation_impact: DocumentationImpact
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    updated_at: datetime
    github_snapshot: dict[str, Any]

    @model_validator(mode="after")
    def issue_identity(self) -> BacklogIssueRecord:
        if self.repository not in BACKLOG_REPOSITORIES:
            raise ValueError("backlog issue escaped the repository allowlist")
        if not _ISSUE_URL.fullmatch(self.issue_url_reference):
            raise ValueError("backlog issue URL is not allowlisted")
        return self


class IssueApprovalReference(BacklogModel):
    operation_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class BacklogApprovalBinding(BacklogModel):
    backlog_campaign_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    project: Literal["falcone", "musematic", "llmwiki"]
    paired_repositories: tuple[str, str]
    source_branch: Literal["main", "master"] = "main"
    deployment_branch: Literal["main", "master"] = "main"
    source_head: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_head: str = Field(pattern=r"^[a-f0-9]{40}$")
    issue_filter: Literal["bugs", "bugs-and-docs", "security", "enhancements", "all"]
    dependency_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    maximum_risk: Literal["R0", "R1", "R2", "R3"]
    maximum_items: int = Field(ge=1, le=50)
    maximum_concurrency: Literal[1] = 1
    delivery_mode: Literal["source-pr", "full-delivery"]
    implementation_correction_limit: int = Field(default=5, ge=1, le=5)
    deployment_correction_limit: int = Field(default=3, ge=1, le=3)
    allowed_github_operations: tuple[str, ...]
    allowed_build_workflows: tuple[str, ...]
    staging_targets: tuple[str, ...]
    production: Literal[False] = False
    include_enhancements: bool
    merge_policy: Literal["protected-explicit", "no-merge"]
    issue_close_policy: Literal["verified-final-runtime", "never"]
    expires_at: datetime
    issue_approval: IssueApprovalReference | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_binding(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        value: dict[str, Any] = handler(self)
        if self.issue_approval is None:
            value.pop("issue_approval", None)
        return value

    @model_validator(mode="after")
    def exact_pair(self) -> BacklogApprovalBinding:
        if self.paired_repositories != BACKLOG_PROJECT_PAIRS[self.project]:
            raise ValueError("approval repository pair is not authoritative")
        if self.delivery_mode == "source-pr" and (
            self.merge_policy != "no-merge" or self.issue_close_policy != "never"
        ):
            raise ValueError("source-pr approval cannot permit merge or issue closure")
        if self.issue_approval is not None and self.maximum_items != 1:
            raise ValueError("issue operation approval must remain bound to one item")
        return self

    def sha256(self) -> str:
        value = self.model_dump(mode="json")
        return canonical_sha256(value)


class BacklogFinalClosureEvidence(BacklogModel):
    """Immutable evidence required before an imported GitHub issue may close."""

    final_source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    final_deployment_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    immutable_artifact_digests: tuple[str, ...]
    staging_revision_verified: bool
    issue_acceptance_passed: bool
    regression_suite_passed: bool
    maker_provider: Literal["codex", "claude"]
    checker_provider: Literal["codex", "claude"]
    documentation_impact: DocumentationImpact
    cleanup_passed: bool
    safe_evidence_comment_ready: bool
    deployed_change: bool

    @model_validator(mode="after")
    def independent_and_complete(self) -> BacklogFinalClosureEvidence:
        if self.maker_provider == self.checker_provider:
            raise ValueError("issue closure requires an opposite native provider checker")
        if (
            self.documentation_impact
            is DocumentationImpact.BLOCKED_BY_UNRESOLVED_DOCUMENTATION_IMPACT
        ):
            raise ValueError("issue closure has unresolved documentation impact")
        if self.deployed_change and (
            self.final_deployment_commit is None or not self.immutable_artifact_digests
        ):
            raise ValueError(
                "deployed issue closure requires deployment commit and immutable artifacts"
            )
        return self


def evaluate_final_closure(evidence: BacklogFinalClosureEvidence) -> str:
    """Fail closed unless every final-source, runtime, review, docs and cleanup gate passed."""

    if not all(
        (
            evidence.staging_revision_verified,
            evidence.issue_acceptance_passed,
            evidence.regression_suite_passed,
            evidence.cleanup_passed,
            evidence.safe_evidence_comment_ready,
        )
    ):
        return "BLOCKED_BY_FINAL_CLOSURE_GATE"
    return "FIXED_CLOSURE_ELIGIBLE"


def r3_approval_payload_matches(
    payload: dict[str, Any],
    *,
    repository: str,
    issue_number: int,
    body_sha256: str,
    github_updated_at: str,
    source_head: str,
    deployment_head: str,
) -> bool:
    """NEW-95: an approved R3 candidate applies only to the exact issue text it was approved
    for, in a campaign imported at the same source and deployment heads. Shared by the
    operation preflight and the loop's runtime selection so both apply the same rule."""
    return (
        payload.get("repository") == repository
        and payload.get("issue_number") == issue_number
        and payload.get("issue_body_sha256") == body_sha256
        and payload.get("issue_updated_at") == github_updated_at
        and payload.get("source_head") == source_head
        and payload.get("deployment_head") == deployment_head
        and payload.get("production") is False
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def classify_issue(
    issue: GitHubIssueSnapshot,
    pull_requests: tuple[GitHubPullRequestSnapshot, ...],
    *,
    project: str,
    import_filter: str,
    evaluated_at: datetime | None = None,
) -> BacklogIssueRecord:
    if project not in BACKLOG_PROJECT_PAIRS or import_filter not in BACKLOG_FILTERS:
        raise ValueError("backlog classification input is not allowlisted")
    labels = tuple(sorted({label.strip().lower() for label in issue.labels if label.strip()}))
    text = f"{issue.title}\n{issue.body_excerpt}".lower()
    change_kind = _change_request_kind(text)
    issue_type = (
        _issue_type(issue.repository, labels, text)
        if change_kind is None
        else _change_request_issue_type(issue.repository, labels, text, change_kind)
    )
    severity = _severity(labels, text)
    risk = _risk(issue_type, severity, text)
    if change_kind is not None:
        risk = _declared_risk_floor(risk, text)
    owner: Literal["source", "deployment"] = (
        "deployment" if issue.repository.endswith(("-charts", "-deploy")) else "source"
    )
    reproduction_mode = _reproduction_mode(issue_type, owner, labels, text)
    linked = _linked_pull_requests(issue, pull_requests, evaluated_at or datetime.now(UTC))
    dependencies = _dependencies(issue, text)
    component = _component(labels, issue.repository)
    wave = _wave(issue_type, severity, dependencies, text)
    eligible, reason = _eligibility(issue_type, import_filter, risk, text)
    openspec = _openspec_kind(labels, text)
    documentation_levels = _documentation_levels(issue_type, labels, text)
    branches = tuple(sorted({pr.head_ref for pr in pull_requests if _pr_links_issue(pr, issue)}))
    commits = tuple(
        sorted(
            {commit for pr in pull_requests if _pr_links_issue(pr, issue) for commit in pr.commits}
        )
    )
    snapshot = {
        "body_excerpt": issue.body_excerpt,
        "comments": list(issue.comments),
        "assignees": list(issue.assignees),
        "updated_at": issue.updated_at.isoformat(),
    }
    return BacklogIssueRecord(
        repository=issue.repository,
        issue_number=issue.number,
        issue_url_reference=issue.url,
        title=issue.title,
        labels=labels,
        milestone=issue.milestone,
        issue_type=issue_type,
        openspec_kind=openspec,
        severity=severity,
        calculated_risk=risk,
        component=component,
        source_or_deployment_owner=owner,
        documentation_levels=documentation_levels,
        dependencies=dependencies,
        blocking_issues=dependencies,
        issues_blocked=(),
        linked_prs=linked,
        linked_branches=branches,
        linked_commits=commits,
        duplicate_candidates=(),
        current_source_reproduction="PENDING_CURRENT_STATE_REVALIDATION",
        current_runtime_reproduction="PENDING"
        if reproduction_mode
        in {
            ReproductionMode.RUNTIME_REQUIRED,
            ReproductionMode.DEPLOYMENT_REQUIRED,
        }
        else "NOT_REQUIRED",
        current_runtime_required=reproduction_mode
        in {
            ReproductionMode.RUNTIME_REQUIRED,
            ReproductionMode.DEPLOYMENT_REQUIRED,
        },
        reproduction_mode=reproduction_mode,
        candidate_status="NEEDS_CURRENT_STATE_REVALIDATION",
        eligibility_status="ELIGIBLE_FOR_REVALIDATION" if eligible else "DEFERRED",
        eligibility_reason=reason,
        planned_wave=wave,
        planned_order=0,
        change_set_id=None,
        delivery_status="NOT_STARTED",
        final_closure_status="OPEN",
        documentation_impact=DocumentationImpact.BLOCKED_BY_UNRESOLVED_DOCUMENTATION_IMPACT,
        body_sha256=issue.body_sha256,
        updated_at=issue.updated_at,
        github_snapshot=snapshot,
    )


def plan_records(records: list[BacklogIssueRecord]) -> tuple[list[BacklogIssueRecord], str]:
    wave_rank = {"W0": 0, "W1": 1, "W2": 2, "W3": 3, "W4": 4, "W5": 5}
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unspecified": 4}
    risk_rank = {"R3": 0, "R2": 1, "R1": 2, "R0": 3}

    def priority(item: BacklogIssueRecord) -> tuple[int, int, int, int, str, int]:
        return (
            wave_rank[item.planned_wave],
            severity_rank[item.severity],
            risk_rank[item.calculated_risk],
            -len(item.issues_blocked),
            item.repository,
            item.issue_number,
        )

    by_reference = {f"{item.repository}#{item.issue_number}": item for item in records}
    pending = set(by_reference)
    ordered: list[BacklogIssueRecord] = []
    while pending:
        ready = [
            reference
            for reference in pending
            if not any(dependency in pending for dependency in by_reference[reference].dependencies)
        ]
        if not ready:
            ready = list(pending)
        selected = min(ready, key=lambda reference: priority(by_reference[reference]))
        ordered.append(by_reference[selected])
        pending.remove(selected)
    planned = [
        item.model_copy(update={"planned_order": ordinal})
        for ordinal, item in enumerate(ordered, start=1)
    ]
    graph = [
        {
            "repository": item.repository,
            "issue_number": item.issue_number,
            "dependencies": list(item.dependencies),
            "wave": item.planned_wave,
            "order": item.planned_order,
        }
        for item in planned
    ]
    return planned, canonical_sha256(graph)


def approval_binding(
    *,
    campaign_id: str,
    project: Literal["falcone", "musematic", "llmwiki"],
    import_filter: Literal["bugs", "bugs-and-docs", "security", "enhancements", "all"],
    source_head: str,
    deployment_head: str,
    dependency_plan_hash: str,
    maximum_risk: Literal["R0", "R1", "R2", "R3"],
    maximum_items: int,
    delivery_mode: Literal["source-pr", "full-delivery"],
    include_enhancements: bool,
    now: datetime | None = None,
) -> BacklogApprovalBinding:
    if not _SHA.fullmatch(source_head) or not _SHA.fullmatch(deployment_head):
        raise ValueError("backlog approval requires immutable repository heads")
    if project == "llmwiki" and delivery_mode == "full-delivery":
        raise ValueError("LLMWIKI_FULL_DELIVERY_INPUTS_UNRESOLVED")
    operations: tuple[str, ...] = (
        "issue-comment",
        "bounded-branch-push",
        "draft-pr-create-or-update",
        "ci-status-read",
    )
    if delivery_mode == "full-delivery":
        operations += ("pr-ready", "protected-pr-merge", "issue-close-after-verification")
    targets = (
        ("in-falcone-staging", "helm:falcone")
        if project == "falcone"
        else ("musematic-platform", "platform-data", "argocd:musematic-platform")
        if project == "musematic"
        else ()
    )
    build_workflows = (
        (".github/workflows/release-images.yml",)
        if project == "falcone" and delivery_mode == "full-delivery"
        else ()
    )
    return BacklogApprovalBinding(
        backlog_campaign_id=campaign_id,
        project=project,
        paired_repositories=BACKLOG_PROJECT_PAIRS[project],
        source_branch="master" if project == "llmwiki" else "main",
        deployment_branch="master" if project == "llmwiki" else "main",
        source_head=source_head,
        deployment_head=deployment_head,
        issue_filter=import_filter,
        dependency_plan_hash=dependency_plan_hash,
        maximum_risk=maximum_risk,
        maximum_items=maximum_items,
        delivery_mode=delivery_mode,
        allowed_github_operations=operations,
        allowed_build_workflows=build_workflows,
        staging_targets=targets,
        include_enhancements=include_enhancements,
        merge_policy="protected-explicit" if delivery_mode == "full-delivery" else "no-merge",
        issue_close_policy=(
            "verified-final-runtime" if delivery_mode == "full-delivery" else "never"
        ),
        expires_at=(now or datetime.now(UTC)) + timedelta(hours=24),
    )


def _issue_type(repository: str, labels: tuple[str, ...], text: str) -> BacklogIssueType:
    label_text = " ".join(labels)
    title = text.splitlines()[0].strip()
    if "duplicate" in label_text or re.search(r"(?i)\bduplicate\s+(?:of\s+)?#\d+", text):
        return BacklogIssueType.DUPLICATE
    if any(value in label_text for value in ("already-fixed", "already fixed")):
        return BacklogIssueType.ALREADY_FIXED
    if "superseded" in label_text:
        return BacklogIssueType.SUPERSEDED
    if "stale" in labels:
        return BacklogIssueType.STALE
    if any(value in label_text for value in ("invalid", "not reproducible", "wontfix")):
        return BacklogIssueType.INVALID_OR_UNREPRODUCIBLE
    enhancement_metadata = (
        any(
            value in labels
            for value in ("enhancement", "feature", "type:modification", "type:task")
        )
        or (
            re.search(r"(?im)^\s*kind\s*:\s*(?:enhancement|modification|feature|chore)\s*$", text)
            is not None
        )
        or (
            re.match(r"^(?:\[enhancement\]|enhancement(?:\([^)]*\))?:|feature request:)", title)
            is not None
        )
    )
    independent_defect = (
        any(value in labels for value in ("bug", "defect", "type:bug", "type:defect"))
        or re.match(r"^(?:\[bug\]|bug(?:\([^)]*\))?:|security defect:|deployment defect:)", title)
        is not None
        or "independently confirmed defect" in text
    )
    # Explicit enhancement/OpenSpec task metadata wins unless the issue also
    # carries independent defect evidence.  Security impact or high severity
    # alone does not convert a feature request into a bugs-campaign item.
    if enhancement_metadata and not independent_defect:
        return BacklogIssueType.ENHANCEMENT
    if "security" in label_text or any(
        value in text for value in ("vulnerability", "tenant isolation", "authorization bypass")
    ):
        return BacklogIssueType.SECURITY_DEFECT
    if any(value in label_text for value in ("needs-decision", "product-decision", "rfc")) or any(
        value in text for value in ("needs product decision", "decision required", "proposal only")
    ):
        return BacklogIssueType.SPEC_DECISION
    if enhancement_metadata:
        return BacklogIssueType.ENHANCEMENT
    if repository.endswith(("-charts", "-deploy")) or any(
        value in label_text for value in ("deployment", "helm", "argocd", "gitops")
    ):
        return BacklogIssueType.DEPLOYMENT_DEFECT
    documentation_label = any(value in label_text for value in ("documentation", "docs", "runbook"))
    if documentation_label or text.startswith(("docs:", "documentation:")):
        return BacklogIssueType.DOCUMENTATION_DEFECT
    if any(value in label_text for value in ("operations", "operational", "devops", "sre")):
        return BacklogIssueType.OPERATIONS_DEFECT
    if any(value in label_text for value in ("meta", "tooling", "ci")) and "bug" not in label_text:
        return BacklogIssueType.META_TOOLING
    if "question" in label_text or issue_title_is_question(text):
        return BacklogIssueType.QUESTION
    return BacklogIssueType.PRODUCT_BUG


_CHANGE_REQUEST_KIND = re.compile(r"(?im)^\s*kind\s*:\s*([a-z][a-z0-9_-]{0,79})\s*$")
_DECLARED_RISK = re.compile(r"(?im)^\s*risk\s*:\s*r([0-3])\s*$")
# NEW-108: the label Hermes applies to a published operator change request.
CHANGE_REQUEST_LABEL = "hermes:change-request"
_RISK_LEVELS: tuple[Literal["R0", "R1", "R2", "R3"], ...] = ("R0", "R1", "R2", "R3")


def _change_request_kind(text: str) -> str | None:
    """NEW-108: the kind of a Hermes ``Change request:`` issue, from its first kind line."""
    if not text.startswith("change request:"):
        return None
    match = _CHANGE_REQUEST_KIND.search(text)
    if match is None or match.group(1) not in {"feature", "enhancement", "bug", "chore"}:
        return None
    return match.group(1)


def _change_request_issue_type(
    repository: str, labels: tuple[str, ...], text: str, kind: str
) -> BacklogIssueType:
    """Classify a change request by its kind line.

    An enhancement keeps the existing enhancement rules. A feature, bug or chore is
    classified exactly as the same issue written by hand without Hermes' own kind metadata
    (the kind line and the labels Hermes applies), so it is eligible under the same import
    filters; nothing else about the issue is ignored.
    """
    if kind == "enhancement":
        return _issue_type(repository, labels, text)
    own_labels = {CHANGE_REQUEST_LABEL, kind}
    stripped = "\n".join(
        line for line in text.splitlines() if not _CHANGE_REQUEST_KIND.fullmatch(line)
    )
    return _issue_type(
        repository, tuple(label for label in labels if label not in own_labels), stripped
    )


def _declared_risk_floor(
    detected: Literal["R0", "R1", "R2", "R3"], text: str
) -> Literal["R0", "R1", "R2", "R3"]:
    """A change request's ``Risk: R<n>`` line can only raise the keyword-detected risk."""
    match = _DECLARED_RISK.search(text)
    if match is None:
        return detected
    declared = int(match.group(1))
    return detected if int(detected[1]) >= declared else _RISK_LEVELS[declared]


def issue_title_is_question(text: str) -> bool:
    title = text.splitlines()[0].strip()
    return title.endswith("?") or title.startswith(("question:", "how do ", "how can "))


def _severity(
    labels: tuple[str, ...], text: str
) -> Literal["critical", "high", "medium", "low", "unspecified"]:
    combined = " ".join(labels)
    for severity in ("critical", "high", "medium", "low"):
        if f"severity:{severity}" in combined or f"severity/{severity}" in combined:
            return severity
    if any(value in text for value in ("critical severity", "remote code execution", "data loss")):
        return "critical"
    return "unspecified"


def _risk(
    issue_type: BacklogIssueType, severity: str, text: str
) -> Literal["R0", "R1", "R2", "R3"]:
    if any(
        value in text
        for value in (
            "data migration",
            "destructive",
            "credential rotation",
            "secret lifecycle",
            "production",
            "clusterrole",
        )
    ):
        return "R3"
    if issue_type is BacklogIssueType.SECURITY_DEFECT or severity in {"critical", "high"}:
        return "R2"
    if issue_type in {
        BacklogIssueType.DOCUMENTATION_DEFECT,
        BacklogIssueType.QUESTION,
        BacklogIssueType.META_TOOLING,
    }:
        return "R0"
    return "R1"


def _reproduction_mode(
    issue_type: BacklogIssueType, owner: str, labels: tuple[str, ...], text: str
) -> ReproductionMode:
    if issue_type is BacklogIssueType.DOCUMENTATION_DEFECT:
        return ReproductionMode.DOCUMENTATION_REQUIRED
    if owner == "deployment" or any(value in text for value in ("helm", "argocd", "gitops")):
        return ReproductionMode.DEPLOYMENT_REQUIRED
    if any(value in labels for value in ("runtime", "e2e", "browser")) or any(
        value in text
        for value in ("login", "http 401", "http 404", "runtime", "staging", "browser")
    ):
        return ReproductionMode.RUNTIME_REQUIRED
    if any(value in labels for value in ("unit-test", "regression")):
        return ReproductionMode.STATIC_PLUS_TEST
    return ReproductionMode.SOURCE_ONLY


def _wave(
    issue_type: BacklogIssueType, severity: str, dependencies: tuple[str, ...], text: str
) -> Literal["W0", "W1", "W2", "W3", "W4", "W5"]:
    if any(
        value in text
        for value in (
            "login",
            "identity endpoint",
            "build blocker",
            "outofsync",
            "degraded",
            "deployment blocker",
            "runtime blocker",
        )
    ):
        return "W0"
    if issue_type is BacklogIssueType.SECURITY_DEFECT or severity in {"critical", "high"}:
        return "W1"
    if dependencies:
        return "W2"
    if issue_type is BacklogIssueType.ENHANCEMENT:
        return "W5"
    if issue_type in {BacklogIssueType.DOCUMENTATION_DEFECT, BacklogIssueType.META_TOOLING}:
        return "W4"
    return "W3"


def _eligibility(
    issue_type: BacklogIssueType, import_filter: str, risk: str, text: str
) -> tuple[bool, str]:
    eligible, reason = _filter_eligibility(issue_type, import_filter, text)
    if eligible and risk == "R3":
        # NEW-96 review: only an issue the campaign's import filter admits can wait for an
        # operator's R3 approval (and so become selectable once approved).
        return False, "R3_REQUIRES_OPERATOR"
    return eligible, reason


def _filter_eligibility(
    issue_type: BacklogIssueType, import_filter: str, text: str
) -> tuple[bool, str]:
    terminal = {
        BacklogIssueType.DUPLICATE: "DUPLICATE",
        BacklogIssueType.STALE: "STALE_REQUIRES_CURRENT_EVIDENCE",
        BacklogIssueType.ALREADY_FIXED: "ALREADY_FIXED_REQUIRES_PROOF",
        BacklogIssueType.SUPERSEDED: "SUPERSEDED",
        BacklogIssueType.INVALID_OR_UNREPRODUCIBLE: "NOT_REPRODUCIBLE",
        BacklogIssueType.SPEC_DECISION: "NEEDS_PRODUCT_DECISION",
        BacklogIssueType.QUESTION: "QUESTION_NOT_AUTONOMOUS_WORK",
    }
    if issue_type in terminal:
        return False, terminal[issue_type]
    if issue_type is BacklogIssueType.ENHANCEMENT:
        return (
            (True, "FILTER_MATCH_PENDING_REVALIDATION")
            if import_filter in {"enhancements", "all"}
            else (False, "ENHANCEMENT_DEFERRED")
        )
    bugs = {
        BacklogIssueType.PRODUCT_BUG,
        BacklogIssueType.SECURITY_DEFECT,
        BacklogIssueType.DEPLOYMENT_DEFECT,
        BacklogIssueType.OPERATIONS_DEFECT,
    }
    if import_filter == "security":
        return (
            (True, "FILTER_MATCH_PENDING_REVALIDATION")
            if issue_type is BacklogIssueType.SECURITY_DEFECT
            else (False, "FILTER_EXCLUDED")
        )
    if import_filter == "bugs":
        return (
            (True, "FILTER_MATCH_PENDING_REVALIDATION")
            if issue_type in bugs
            else (False, "FILTER_EXCLUDED")
        )
    if import_filter == "bugs-and-docs":
        return (
            (True, "FILTER_MATCH_PENDING_REVALIDATION")
            if issue_type in {*bugs, BacklogIssueType.DOCUMENTATION_DEFECT}
            else (False, "FILTER_EXCLUDED")
        )
    if import_filter == "enhancements":
        return False, "FILTER_EXCLUDED"
    if import_filter == "all" and "needs product decision" not in text:
        return True, "FILTER_MATCH_PENDING_REVALIDATION"
    return False, "FILTER_EXCLUDED"


def _dependencies(issue: GitHubIssueSnapshot, text: str) -> tuple[str, ...]:
    values: set[str] = set()
    default_slug = issue.repository.rsplit("/", 1)[-1]
    for block in _DEPENDENCY.findall(text):
        for match in _REFERENCE.finditer(block):
            slug = match.group("repo") or default_slug
            reference = f"gntik-ai/{slug}#{match.group('number')}"
            if reference != f"{issue.repository}#{issue.number}" and _REPOSITORY.fullmatch(
                f"gntik-ai/{slug}"
            ):
                values.add(reference)
    return tuple(sorted(values))


def _linked_pull_requests(
    issue: GitHubIssueSnapshot,
    pull_requests: tuple[GitHubPullRequestSnapshot, ...],
    evaluated_at: datetime,
) -> tuple[LinkedPullRequest, ...]:
    linked: list[LinkedPullRequest] = []
    for pull_request in pull_requests:
        if not _pr_links_issue(pull_request, issue):
            continue
        if pull_request.state == "MERGED":
            status = ExistingPullRequestStatus.ALREADY_MERGED
        elif pull_request.state == "CLOSED":
            status = ExistingPullRequestStatus.CLOSED_WITHOUT_MERGE
        elif pull_request.merge_state.upper() in {"DIRTY", "BLOCKED"}:
            status = ExistingPullRequestStatus.CONFLICTING_PR
        elif evaluated_at.astimezone(UTC) - pull_request.updated_at.astimezone(UTC) > timedelta(
            days=45
        ):
            status = ExistingPullRequestStatus.STALE_PR
        else:
            status = ExistingPullRequestStatus.ADOPTABLE_ACTIVE_PR
        linked.append(
            LinkedPullRequest(
                repository=pull_request.repository,
                number=pull_request.number,
                url=pull_request.url,
                status=status,
                head_sha=pull_request.head_sha,
            )
        )
    return tuple(sorted(linked, key=lambda item: (item.repository, item.number)))


def _pr_links_issue(pr: GitHubPullRequestSnapshot, issue: GitHubIssueSnapshot) -> bool:
    combined = f"{pr.title}\n{pr.body_excerpt}\n{pr.head_ref}".lower()
    url = issue.url.lower()
    keyword = re.compile(
        rf"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:(?:{re.escape(issue.repository)})?#)?{issue.number}\b"
    )
    branch_number = re.search(rf"(?:^|[-_/]){issue.number}(?:[-_/]|$)", pr.head_ref) is not None
    return url in combined or keyword.search(combined) is not None or branch_number


def _component(labels: tuple[str, ...], repository: str) -> str:
    for label in labels:
        if label.startswith(("component:", "area:", "cap:")):
            return f"{repository.rsplit('/', 1)[-1]}:{label.split(':', 1)[1]}"[:200]
    return f"{repository.rsplit('/', 1)[-1]}:general"


def _openspec_kind(labels: tuple[str, ...], text: str) -> str | None:
    match = re.search(r"(?im)^\s*kind\s*:\s*([a-z][a-z0-9_-]{0,79})\s*$", text)
    if match:
        return match.group(1)
    if "type:modification" in labels:
        return "modification"
    if "enhancement" in labels:
        return "enhancement"
    return None


def _documentation_levels(
    issue_type: BacklogIssueType, labels: tuple[str, ...], text: str
) -> tuple[str, ...]:
    levels: set[str] = set()
    if issue_type is BacklogIssueType.DOCUMENTATION_DEFECT:
        levels.add("technical-documentation")
    if any(value in text for value in ("api documentation", "openapi")):
        levels.add("api-documentation")
    if any(value in text for value in ("runbook", "devops", "deployment")) or "runbook" in labels:
        levels.add("devops-runbook")
    if any(value in text for value in ("user guide", "end-user", "administrator")):
        levels.add("end-user-or-administrator")
    levels.add("documentation-impact-review-required")
    return tuple(sorted(levels))
