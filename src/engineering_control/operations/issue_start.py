from __future__ import annotations

import re

from engineering_control.api.schemas import ExistingBacklogLoopStart
from engineering_control.db.models import OperationRequest
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
