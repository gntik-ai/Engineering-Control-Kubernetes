from __future__ import annotations

import hmac
import os
import re
import stat
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from engineering_control.adapters.github.client import ALLOWED_REPOSITORIES, READ_ONLY_REPOSITORIES
from engineering_control.engineering.backlog import CHANGE_REQUEST_LABEL
from engineering_control.engineering.change_requests import (
    ChangeReviewRecord,
    change_request_publish_workflow_id,
    render_change_request,
    target_repository_allowed,
    validate_proposal,
)
from engineering_control.policy.change_request_attestation import ChangeRequestAttestation

GITHUB_WRITE_POLICY_PATH = Path(
    "/etc/engineering-control/policy/github-issue-write-validation.yaml"
)
READ_ONLY_GITHUB_STEPS = frozenset(
    {
        "enhancement.github_dedupe",
        "finding.dedupe",
        "github.refresh_mirrors",
        "backlog.github_import",
        "delivery.refresh_issue_body",
        "llmwiki.bootstrap.plan",
        "reconcile.inspect_ci",
        "final.inspect_artifact",
        "final.sync_worktrees",
    }
)
WRITE_CAPABLE_GITHUB_STEPS = frozenset(
    {
        "delivery.publish",
        "enhancement.publish",
        "finding.publish",
        "reconcile.publish_promotion",
        "final.merge_source",
        "final.merge_deployment",
        "final.comment_issue",
        "final.close_issue",
    }
)
# Post-merge staging GitOps flow (activities/gitops.py). Kept separate from the
# delivery handler's sets above: these are dispatched to GitOpsGitHubHandler, which
# applies the same GitHubWritePolicyEvaluator gate before any write.
GITOPS_READ_ONLY_GITHUB_STEPS = frozenset(
    {
        "gitops.pr_checks",
        "gitops.pr_heads",
        "gitops.deployment_pr_checks",
        "gitops.workflow_run",
        "gitops.image_pin",
    }
)
GITOPS_WRITE_CAPABLE_GITHUB_STEPS = frozenset(
    {
        "gitops.merge",
        "gitops.release_tag",
        "gitops.environment_issue",
        "gitops.close_issue",
    }
)
# NEW-108 operator change requests. The publish step is a separate, narrow issue-create
# capability: it is deliberately NOT write-capable in the sense above, is never gated on
# backlog-campaign write-policy ownership, and is authorized only by
# ChangeRequestIssueCreatePolicy below.
CHANGE_REQUEST_READ_ONLY_GITHUB_STEPS = frozenset({"change_request.github_dedupe"})
CHANGE_REQUEST_ISSUE_CREATE_STEPS = frozenset({"change_request.publish"})
CHANGE_REQUEST_ISSUE_CREATE_OPERATION = "create_issue"
GITHUB_ACTIVITY_STEPS = (
    READ_ONLY_GITHUB_STEPS
    | WRITE_CAPABLE_GITHUB_STEPS
    | GITOPS_READ_ONLY_GITHUB_STEPS
    | GITOPS_WRITE_CAPABLE_GITHUB_STEPS
    | CHANGE_REQUEST_READ_ONLY_GITHUB_STEPS
    | CHANGE_REQUEST_ISSUE_CREATE_STEPS
)
WRITE_REPOSITORIES = ALLOWED_REPOSITORIES - READ_ONLY_REPOSITORIES


class GitHubWriteDenialReason(StrEnum):
    POLICY_DISABLED = "POLICY_DISABLED"
    EMPTY_SAFE_REPOSITORY_LIST = "EMPTY_SAFE_REPOSITORY_LIST"
    REPOSITORY_NOT_ALLOWLISTED = "REPOSITORY_NOT_ALLOWLISTED"
    POLICY_MISSING = "POLICY_MISSING"
    POLICY_UNREADABLE = "POLICY_UNREADABLE"
    POLICY_INVALID = "POLICY_INVALID"


class GitHubWritePolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    safe_repositories: tuple[str, ...]

    @field_validator("safe_repositories")
    @classmethod
    def bounded_allowlist(cls, repositories: tuple[str, ...]) -> tuple[str, ...]:
        if len(repositories) > len(WRITE_REPOSITORIES):
            raise ValueError("GitHub write repository allowlist exceeds its bound")
        if len(repositories) != len(set(repositories)):
            raise ValueError("GitHub write repository allowlist contains duplicates")
        if any(repository not in WRITE_REPOSITORIES for repository in repositories):
            raise ValueError("GitHub write repository allowlist contains an unsafe target")
        return repositories


class GitHubWritePolicyState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    repository_allowed: bool


class GitHubWritePolicyDenial(BaseModel):
    """Secret-free terminal result returned before any GitHub write-side effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["BLOCKED_BY_POLICY"] = "BLOCKED_BY_POLICY"
    blocker: Literal["BLOCKED_BY_POLICY"] = "BLOCKED_BY_POLICY"
    workflow_state: Literal["AWAITING_WRITE_POLICY"] = "AWAITING_WRITE_POLICY"
    terminal: Literal[True] = True
    retryable: Literal[False] = False
    external_command_started: Literal[False] = False
    external_mutation_attempted: Literal[False] = False
    mutating_command_constructed: Literal[False] = False
    external_command_count: Literal[0] = 0
    external_mutation_attempt_count: Literal[0] = 0
    mutating_command_construction_count: Literal[0] = 0
    policy: GitHubWritePolicyState
    reason_code: GitHubWriteDenialReason


class GitHubWritePolicyEvaluator:
    """Load one policy and authorize typed repositories before mutation construction."""

    def __init__(
        self,
        *,
        policy_path: Path,
        project_config_root: Path,
        production: bool,
        writes_enabled: bool,
    ) -> None:
        self.policy_path = policy_path
        self.config_root = project_config_root.resolve().parent
        self.production = production
        self.writes_enabled = writes_enabled

    def evaluate(
        self, repository_resolver: Callable[[], tuple[str, ...]]
    ) -> GitHubWritePolicyDenial | None:
        loaded = self._load()
        if isinstance(loaded, GitHubWritePolicyDenial):
            return loaded
        effective_enabled = self.writes_enabled and loaded.enabled
        if not effective_enabled:
            return self._denial(GitHubWriteDenialReason.POLICY_DISABLED, enabled=False)
        if not loaded.safe_repositories:
            return self._denial(
                GitHubWriteDenialReason.EMPTY_SAFE_REPOSITORY_LIST,
                enabled=True,
            )
        repositories = repository_resolver()
        if not repositories:
            return self._denial(GitHubWriteDenialReason.POLICY_INVALID, enabled=True)
        repository_allowed = all(
            repository in WRITE_REPOSITORIES and repository in loaded.safe_repositories
            for repository in repositories
        )
        if not repository_allowed:
            return self._denial(
                GitHubWriteDenialReason.REPOSITORY_NOT_ALLOWLISTED,
                enabled=True,
            )
        return None

    def _load(self) -> GitHubWritePolicyDocument | GitHubWritePolicyDenial:
        path = self.policy_path
        expected = self.config_root / "policy" / GITHUB_WRITE_POLICY_PATH.name
        if path != expected or path.is_symlink():
            return self._denial(GitHubWriteDenialReason.POLICY_INVALID)
        if not path.exists():
            return self._denial(GitHubWriteDenialReason.POLICY_MISSING)
        try:
            metadata = path.stat()
            parent_metadata = path.parent.stat()
        except OSError:
            return self._denial(GitHubWriteDenialReason.POLICY_UNREADABLE)
        permissions = stat.S_IMODE(metadata.st_mode)
        parent_permissions = stat.S_IMODE(parent_metadata.st_mode)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            return self._denial(GitHubWriteDenialReason.POLICY_INVALID)
        if self.production:
            readable_groups = {*os.getgroups(), os.getegid()}
            if (
                metadata.st_uid != 0
                or metadata.st_gid not in readable_groups
                or permissions != 0o640
                or parent_metadata.st_uid != 0
                or parent_metadata.st_gid != metadata.st_gid
                or parent_permissions != 0o750
            ):
                return self._denial(GitHubWriteDenialReason.POLICY_UNREADABLE)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            return GitHubWritePolicyDocument.model_validate(raw)
        except PermissionError:
            return self._denial(GitHubWriteDenialReason.POLICY_UNREADABLE)
        except (OSError, UnicodeError):
            return self._denial(GitHubWriteDenialReason.POLICY_UNREADABLE)
        except (yaml.YAMLError, ValidationError):
            return self._denial(GitHubWriteDenialReason.POLICY_INVALID)

    @staticmethod
    def _denial(
        reason: GitHubWriteDenialReason,
        *,
        enabled: bool = False,
    ) -> GitHubWritePolicyDenial:
        return GitHubWritePolicyDenial(
            policy=GitHubWritePolicyState(enabled=enabled, repository_allowed=False),
            reason_code=reason,
        )


class ChangeRequestIssueCreateDenialReason(StrEnum):
    STEP_NOT_ALLOWED = "STEP_NOT_ALLOWED"
    OPERATION_NOT_ALLOWED = "OPERATION_NOT_ALLOWED"
    WORKFLOW_TYPE_NOT_ALLOWED = "WORKFLOW_TYPE_NOT_ALLOWED"
    WORKFLOW_NOT_BOUND = "WORKFLOW_NOT_BOUND"
    WRITES_DISABLED = "WRITES_DISABLED"
    NOT_APPROVED = "NOT_APPROVED"
    REVISION_MISMATCH = "REVISION_MISMATCH"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    REPOSITORY_NOT_ALLOWLISTED = "REPOSITORY_NOT_ALLOWLISTED"
    CONTENT_TAMPERED = "CONTENT_TAMPERED"


class ChangeRequestIssueCreateDenial(BaseModel):
    """Secret-free denial returned before any GitHub command is constructed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["BLOCKED_BY_POLICY"] = "BLOCKED_BY_POLICY"
    reason_code: ChangeRequestIssueCreateDenialReason
    external_mutation_attempted: Literal[False] = False


class ChangeRequestIssuePlan(BaseModel):
    """The only GitHub write a change request may perform: create this exact issue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["create_issue"] = "create_issue"
    change_request_id: str
    revision: int
    repository: str
    title: str
    body: str
    marker: str
    approval_binding: str
    wanted_labels: tuple[str, ...]


class ChangeRequestIssueCreatePolicy:
    """Authorize exactly one issue creation for one durably approved change request.

    Authorization comes only from the root-owned attestation the reconciler derived from
    the PostgreSQL row (the GitHub worker has no database credential) and from the
    activity's own Temporal identity -- never from activity payload content. It is
    independent of GitHubWritePolicyEvaluator and backlog write-policy ownership; only
    the global GitHub write switch still applies.
    """

    workflow_type = "ChangeRequestPublishWorkflow"

    def __init__(self, *, writes_enabled: bool) -> None:
        self.writes_enabled = writes_enabled

    def evaluate(
        self,
        *,
        step: str,
        operation: str,
        workflow_type: str,
        workflow_id: str,
        change_request_id: str,
        revision: object,
        approval_binding: str,
        attestation: ChangeRequestAttestation | None,
    ) -> ChangeRequestIssuePlan | ChangeRequestIssueCreateDenial:
        reason = ChangeRequestIssueCreateDenialReason
        if step not in CHANGE_REQUEST_ISSUE_CREATE_STEPS:
            return ChangeRequestIssueCreateDenial(reason_code=reason.STEP_NOT_ALLOWED)
        if operation != CHANGE_REQUEST_ISSUE_CREATE_OPERATION:
            return ChangeRequestIssueCreateDenial(reason_code=reason.OPERATION_NOT_ALLOWED)
        if workflow_type != self.workflow_type:
            return ChangeRequestIssueCreateDenial(reason_code=reason.WORKFLOW_TYPE_NOT_ALLOWED)
        if attestation is None or attestation.change_request_id != change_request_id:
            return ChangeRequestIssueCreateDenial(reason_code=reason.NOT_APPROVED)
        if workflow_id != change_request_publish_workflow_id(
            attestation.change_request_id, attestation.revision
        ):
            return ChangeRequestIssueCreateDenial(reason_code=reason.WORKFLOW_NOT_BOUND)
        if revision != attestation.revision:
            return ChangeRequestIssueCreateDenial(reason_code=reason.REVISION_MISMATCH)
        if not self.writes_enabled:
            return ChangeRequestIssueCreateDenial(reason_code=reason.WRITES_DISABLED)
        if not re.fullmatch(r"[a-f0-9]{64}", approval_binding) or not hmac.compare_digest(
            attestation.approval_binding, approval_binding
        ):
            return ChangeRequestIssueCreateDenial(reason_code=reason.BINDING_MISMATCH)
        repository = attestation.target_repository
        if repository not in WRITE_REPOSITORIES or not target_repository_allowed(
            attestation.project, repository
        ):
            return ChangeRequestIssueCreateDenial(reason_code=reason.REPOSITORY_NOT_ALLOWLISTED)
        try:
            proposal = validate_proposal(attestation.proposal, attestation.project)
            rendered = render_change_request(
                change_request_id=attestation.change_request_id,
                revision=attestation.revision,
                project=attestation.project,
                target_repository=repository,
                proposal=proposal,
                review=ChangeReviewRecord.model_validate(attestation.review),
                description_redacted=attestation.description_redacted,
            )
        except ValueError:
            return ChangeRequestIssueCreateDenial(reason_code=reason.CONTENT_TAMPERED)
        if (
            not hmac.compare_digest(rendered.approval_binding, attestation.approval_binding)
            or rendered.fingerprint != attestation.fingerprint
            or rendered.title != attestation.issue_title
            or rendered.body != attestation.issue_body
        ):
            return ChangeRequestIssueCreateDenial(reason_code=reason.CONTENT_TAMPERED)
        return ChangeRequestIssuePlan(
            change_request_id=attestation.change_request_id,
            revision=attestation.revision,
            repository=repository,
            title=attestation.issue_title,
            body=attestation.issue_body,
            marker=rendered.marker,
            approval_binding=attestation.approval_binding,
            wanted_labels=(CHANGE_REQUEST_LABEL, proposal.kind),
        )
