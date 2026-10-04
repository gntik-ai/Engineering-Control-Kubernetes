from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.api.schemas import ExistingBacklogLoopStart
from engineering_control.db.models import BacklogIssueRecord, OperationRequest
from engineering_control.engineering.backlog import BACKLOG_PROJECT_PAIRS
from engineering_control.operations.models import SolveIssueCommand, parse_command

ISSUE_START_UNCONFIRMED = (
    "ISSUE_START_UNCONFIRMED: preserve active ownership; reconcile the canonical execution"
)


def issue_workflow_id(request: ExistingBacklogLoopStart) -> str:
    if request.issue_operation_id is None:
        raise ValueError("issue observation requires its originating operation")
    return (
        f"backlog-loop-{request.project}-{request.backlog_campaign_id}"
        f"-operation-{request.issue_operation_id}"
    )


class IssueStartUnconfirmedError(RuntimeError):
    def __init__(self, request: ExistingBacklogLoopStart) -> None:
        self.workflow_id = issue_workflow_id(request)
        self.operation_id = request.issue_operation_id
        super().__init__(ISSUE_START_UNCONFIRMED)


def issue_request_from_operation(operation: OperationRequest) -> ExistingBacklogLoopStart:
    """Reconstruct the immutable attempted scope, not a new execution approval.

    Read-only recovery must still observe an earlier attempt after an issue body
    changes. This function never authorizes another start or external effect.
    """
    command = parse_command(operation.payload_json)
    if (
        not isinstance(command, SolveIssueCommand)
        or operation.action != "solve_issue"
        or operation.target_type != "github_issue"
        or operation.project != command.project.value
        or operation.target_id != f"{command.repository}#{command.issue_number}"
        or re.fullmatch(r"[0-9a-f]{64}", operation.binding_hash) is None
    ):
        raise ValueError("issue observation has an invalid stored operation binding")
    return ExistingBacklogLoopStart(
        project=command.project.value,
        backlog_campaign_id=command.backlog_campaign_id,
        maximum_risk=command.maximum_risk,
        delivery_mode=command.delivery_mode,
        maximum_items=1,
        target_repository=command.repository,
        target_issue_number=command.issue_number,
        continue_after_blocked_issue=False,
        correction_limit=command.correction_limit,
        issue_operation_id=operation.id,
        issue_approval_binding=operation.binding_hash,
    )


# NEW-128: shared by the retry preflight and /loop amend --retry loop resolution.
_BACKLOG_LOOP_WORKFLOW = re.compile(
    r"backlog-loop-(falcone|musematic|llmwiki)-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:-|$)"
)


def backlog_issue_signal_blocker(
    session: Session, workflow_id: str, repository: str, issue_number: int
) -> str | None:
    """NEW-114: an issue-bound retry/skip must name an issue of that loop's campaign.

    `backlog-retry <wf> falcone 45` was accepted although #45 belongs to falcone-charts,
    and the loop silently ignored the signal. Workflows that are not backlog loops keep
    their behavior (None)."""
    match = _BACKLOG_LOOP_WORKFLOW.match(workflow_id)
    if match is None:
        return None
    full_repository = f"gntik-ai/{repository}"
    if full_repository not in BACKLOG_PROJECT_PAIRS[match.group(1)]:
        return "BACKLOG_ISSUE_NOT_IN_LOOP_PROJECT"
    found = session.scalar(
        select(BacklogIssueRecord.id)
        .where(
            BacklogIssueRecord.backlog_campaign_id == UUID(match.group(2)),
            BacklogIssueRecord.repository == full_repository,
            BacklogIssueRecord.issue_number == issue_number,
        )
        .limit(1)
    )
    return None if found is not None else "BACKLOG_ISSUE_NOT_IN_LOOP"
