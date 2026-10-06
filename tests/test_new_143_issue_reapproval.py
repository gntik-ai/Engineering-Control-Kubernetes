"""NEW-143: re-create a voided per-issue campaign approval for an issue loop's operation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_controlled_operations import CAMPAIGN_ID
from test_issue_operation_approval import _approved_direct_request
from test_metadata_activity import add_campaign

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.api.main import create_app
from engineering_control.api.schemas import BacklogApprovalCandidateRequest
from engineering_control.cli import dispatch, parser
from engineering_control.config import Settings
from engineering_control.db.models import (
    BacklogIssueRecord,
    CoverageCell,
    DashboardEvent,
    Finding,
    GitHubBacklogCampaign,
    Issue,
    OperationRequest,
    Run,
    Step,
)
from engineering_control.operations.issue_reapproval import (
    IssueReapprovalBlocked,
    reapproval_phrase,
)
from engineering_control.operations.issue_start import issue_workflow_id
from engineering_control.operations.service import TypedCommandBus

ISSUE = "gntik-ai/falcone#42"
RUN = "loop-run-1"
TOKEN = "fixture-token-" + "x" * 40


class _Live:
    """falcone #972's sequence on the real repositories (no Temporal, no GitHub)."""

    def __init__(self, operations: Any, metadata: Any, engineering: Any, request: Any) -> None:
        self.operations = operations
        self.metadata = metadata
        self.engineering = engineering
        self.request = request
        self.loop = issue_workflow_id(request)
        self.operation_id: UUID = request.issue_operation_id
        self.activity = 0
        self.finding_id = ""
        self.control = Mock()
        self.control.current_open_run_id = AsyncMock(return_value=RUN)
        self.control.awaited_operator_issue = AsyncMock(return_value=None)
        self.bus = TypedCommandBus(self.control, metadata, operations)
        self.bus.policy_activation_poll_seconds = 0
        self.bus.policy_activation_timeout_seconds = 5
        original = metadata.get_backlog_campaign

        async def promoted(campaign_id: UUID) -> Any:
            # The root policy reconciler promotes a pending campaign.
            self.set_campaign(status="APPROVED", only_from="APPROVED_POLICY_PENDING")
            return await original(campaign_id)

        metadata.get_backlog_campaign = promoted

    def set_campaign(self, *, status: str, only_from: str | None = None) -> None:
        with self.operations.sessions.begin() as session:
            campaign = session.get(GitHubBacklogCampaign, CAMPAIGN_ID)
            if only_from is None or campaign.status == only_from:
                campaign.status = status
                # The reconciler consumes the policy request it acted on.
                for request in Path(self.metadata.github_policy_request_root).glob("*.json"):
                    request.unlink()

    def loop_state(self, status: str, issue: str | None = ISSUE, blocker: str = "X") -> None:
        self.activity += 1
        payload: dict[str, Any] = {
            "project": "falcone",
            "backlog_campaign_id": str(CAMPAIGN_ID),
            "loop_status": status,
            "workflow_id": self.loop,
            "orchestration": {"workflow_run_id": RUN, "activity_id": str(self.activity)},
        }
        if issue is not None:
            payload["issue"] = issue
        if status == "PAUSED":
            payload["blocker"] = blocker
        self.engineering._record_backlog_loop_state(payload)

    def register(self, attempt: int) -> dict[str, Any]:
        payload = {
            **self.request.model_dump(mode="json"),
            "child_kind": "issue-delivery",
            "child_workflow_id": f"{self.loop}-issue-1-attempt-{attempt}",
            "finding_id": self.finding_id,
            "orchestration": {"workflow_id": self.loop},
        }
        result: dict[str, Any] = self.engineering._register_backlog_child_run(payload)
        return result

    async def reapprove(self, **overrides: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "loop_workflow_id": self.loop,
            "project": "falcone",
            "repository": "falcone",
            "issue_number": 42,
            "phrase": reapproval_phrase(self.operation_id),
            "actor": "cli:engineering-ctl",
            **overrides,
        }
        result: dict[str, Any] = await self.bus.reapprove_issue(**arguments)
        return result

    def approval(self) -> tuple[str, dict[str, Any]]:
        with self.operations.sessions() as session:
            campaign = session.get(GitHubBacklogCampaign, CAMPAIGN_ID)
            return campaign.status, dict(campaign.approval_json)

    def dispose(self) -> None:
        for item in (self.operations, self.metadata, self.engineering):
            item.engine.dispose()


async def _voided(tmp_path: Path) -> _Live:
    """issue-start approval, a running loop, backlog-pause voiding it, the general
    re-approval, and the loop paused because the next child registration is refused."""
    operations, metadata, engineering, request = await _approved_direct_request(tmp_path)
    live = _Live(operations, metadata, engineering, request)
    handler = MetadataActivityHandler(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url=str(operations.engine.url),  # type: ignore[arg-type]
            state_root=tmp_path / "state",
            project_config_root=Path(__file__).parents[1] / "config/projects",
        )
    )
    campaign = add_campaign(handler)
    handler.engine.dispose()
    coverage = CoverageCell(
        id=uuid4(),
        campaign_id=campaign.id,
        domain="api",
        persona="operator",
        lens="correctness",
        environment="staging",
        status="CONFIRMED_FINDING",
        evidence_json=[],
        result_json={},
    )
    finding = Finding(
        id=uuid4(),
        campaign_id=campaign.id,
        coverage_cell_id=coverage.id,
        fingerprint=uuid4().hex * 2,
        project="falcone",
        domain="api",
        persona="operator",
        severity="medium",
        confidence=0.9,
        risk_class="R1",
        disposition="CONFIRMED",
        expected_behavior="fixture contract",
        actual_behavior="fixture mismatch",
        evidence_json=[],
        minimal_reproduction=["fixture only"],
        affected_source_sha="a" * 40,
        affected_deployment_sha="b" * 40,
        affected_component="api",
        impact_json={},
        recommended_action="bounded fixture repair",
    )
    with operations.sessions.begin() as session:
        session.add_all([coverage, finding])
        session.flush()
        issue = session.scalar(select(BacklogIssueRecord))
        issue.finding_id = finding.id
        session.add(
            Issue(
                finding_id=finding.id,
                repository=issue.repository,
                number=issue.issue_number,
                url=issue.issue_url_reference,
                state="OPEN",
            )
        )
        operation = session.get(OperationRequest, live.operation_id)
        operation.status = "STARTED"
        operation.workflow_id = live.loop
        session.add(
            Run(
                workflow_id=live.loop,
                run_type="existing-backlog-loop",
                status="RUNNING",
                project="falcone",
                temporal_first_execution_run_id=RUN,
            )
        )
    live.finding_id = str(finding.id)
    live.set_campaign(status="APPROVED")  # the reconciler activated the issue-start policy
    live.loop_state("RUNNING")
    assert live.register(1)["status"] == "REGISTERED"
    # backlog-pause during the GitHub outage voids the campaign approval.
    live.loop_state("PAUSED", blocker="OPERATOR_PAUSE")
    # The operator re-approves through the general backlog-approve (R2, max 5).
    candidate = await metadata.prepare_backlog_approval(
        CAMPAIGN_ID,
        BacklogApprovalCandidateRequest(
            project="falcone",
            delivery_mode=request.delivery_mode,
            maximum_risk=request.maximum_risk,
            maximum_items=5,
        ),
    )
    await metadata.approve_backlog_campaign(
        CAMPAIGN_ID, "falcone", candidate["approval_binding"], "cli:engineering-ctl"
    )
    live.set_campaign(status="APPROVED")
    live.loop_state("RUNNING")
    with pytest.raises(ValueError, match="differs from its operation"):
        live.register(2)
    live.loop_state("PAUSED", blocker="LOOP_STEP_FAILED_BACKLOG_REGISTER_CHILD_RUN")
    return live


async def test_the_live_sequence_resumes_after_issue_reapprove(tmp_path: Path) -> None:
    live = await _voided(tmp_path)
    assert live.approval()[1]["binding"].get("issue_approval") is None

    result = await live.reapprove()

    assert result["status"] == "REAPPROVED"
    assert result["next"] == f"engineering-ctl backlog-retry {live.loop} falcone 42"
    status, approval = live.approval()
    # Same shape as issue-start's: one item, bound to the existing operation, activated.
    assert status == "APPROVED"
    assert approval["policy_role"] == "owner"
    assert approval["approval"]["approved_by"] == "cli:engineering-ctl"
    assert approval["binding"]["maximum_items"] == 1
    assert approval["binding"]["issue_approval"] == {
        "operation_id": str(live.operation_id),
        "binding_sha256": live.request.issue_approval_binding,
    }
    with live.operations.sessions() as session:
        events = session.scalars(
            select(DashboardEvent).where(DashboardEvent.event_type == "issue_approval_reissued")
        ).all()
        assert [event.entity_id for event in events] == [str(live.operation_id)]
        assert events[0].metadata_json["approved_by"] == "cli:engineering-ctl"
        assert events[0].workflow_id == live.loop
    # backlog-retry resumes the loop; its next delivery child now registers.
    live.loop_state("RUNNING")
    assert live.register(2)["status"] == "REGISTERED"
    live.dispose()


async def test_a_rerun_is_idempotent(tmp_path: Path) -> None:
    live = await _voided(tmp_path)
    first = await live.reapprove()
    binding = live.approval()[1]["binding"]
    second = await live.reapprove()
    assert (first["status"], second["status"]) == ("REAPPROVED", "ALREADY_APPROVED")
    assert live.approval()[1]["binding"] == binding
    with live.operations.sessions() as session:
        events = session.scalars(
            select(DashboardEvent).where(DashboardEvent.event_type == "issue_approval_reissued")
        ).all()
        assert len(events) == 1
    live.dispose()


async def test_a_loop_paused_before_its_pause_record_is_proven_by_its_status_query(
    tmp_path: Path,
) -> None:
    live = await _voided(tmp_path)
    with live.operations.sessions.begin() as session:
        run = session.scalar(select(Run).where(Run.workflow_id == live.loop))
        for step in session.scalars(select(Step).where(Step.run_id == run.id)):
            session.delete(step)
    with pytest.raises(IssueReapprovalBlocked, match="LOOP_NOT_PAUSED"):
        await live.reapprove()
    live.control.awaited_operator_issue = AsyncMock(return_value=ISSUE)
    assert (await live.reapprove())["status"] == "REAPPROVED"
    live.dispose()


def _edit_issue(live: _Live) -> None:
    with live.operations.sessions.begin() as session:
        session.scalar(select(BacklogIssueRecord)).body_sha256 = "9" * 64


def _operation(**fields: Any) -> Callable[[_Live], None]:
    def apply(live: _Live) -> None:
        with live.operations.sessions.begin() as session:
            operation = session.get(OperationRequest, live.operation_id)
            for key, value in fields.items():
                setattr(operation, key, value)

    return apply


def _loop_run(**fields: Any) -> Callable[[_Live], None]:
    def apply(live: _Live) -> None:
        with live.operations.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.workflow_id == live.loop))
            for key, value in fields.items():
                setattr(run, key, value)

    return apply


def _paused_on_another_issue(live: _Live) -> None:
    live.loop_state("PAUSED", issue="gntik-ai/falcone#43", blocker="APPROVAL_EXPIRED")


def _no_open_run(live: _Live) -> None:
    live.control.current_open_run_id = AsyncMock(return_value=None)


def _another_open_loop(live: _Live) -> None:
    with live.operations.sessions.begin() as session:
        session.add(
            Run(
                workflow_id=f"backlog-loop-falcone-{CAMPAIGN_ID}",
                run_type="existing-backlog-loop",
                status="RUNNING",
                project="falcone",
            )
        )


def _general_policy(**fields: Any) -> Callable[[_Live], None]:
    def apply(live: _Live) -> None:
        with live.operations.sessions.begin() as session:
            campaign = session.get(GitHubBacklogCampaign, CAMPAIGN_ID)
            binding = {**campaign.approval_json["binding"], **fields}
            campaign.approval_json = {**campaign.approval_json, "binding": binding}

    return apply


def _bound_to_another_open_operation(live: _Live) -> None:
    with live.operations.sessions.begin() as session:
        original = session.get(OperationRequest, live.operation_id)
        other = OperationRequest(
            **{
                column.name: getattr(original, column.name)
                for column in OperationRequest.__table__.columns
                if column.name not in {"id", "idempotency_key", "workflow_id", "target_id"}
            },
            id=uuid4(),
            idempotency_key="other-issue-operation-0001",
            target_id="gntik-ai/falcone#43",
        )
        session.add(other)
        session.flush()
        campaign = session.get(GitHubBacklogCampaign, CAMPAIGN_ID)
        binding = {
            **campaign.approval_json["binding"],
            "maximum_items": 1,
            "issue_approval": {"operation_id": str(other.id), "binding_sha256": "e" * 64},
        }
        campaign.approval_json = {**campaign.approval_json, "binding": binding}


def _loop_ran_another_request(live: _Live) -> None:
    with live.operations.sessions.begin() as session:
        for step in session.scalars(select(Step).where(Step.name == "delivery.issue_approval")):
            value = dict(step.result_json)
            value["request"] = {**value["request"], "correction_limit": 5}
            step.result_json = value


Mutation = Callable[[_Live], None] | None


@pytest.mark.parametrize(
    ("mutate", "overrides", "blocker"),
    [
        (None, {"phrase": "REAPPROVE_ISSUE_00000000"}, "CONFIRMATION_MISMATCH"),
        (None, {"project": "musematic"}, "LOOP_INVALID"),
        (None, {"loop_workflow_id": f"backlog-loop-falcone-{CAMPAIGN_ID}"}, "LOOP_INVALID"),
        (None, {"issue_number": 43}, "OPERATION_MISMATCH"),
        (None, {"repository": "falcone-charts"}, "OPERATION_MISMATCH"),
        (_operation(status="COMPLETED"), {}, "OPERATION_FINISHED"),
        (_operation(status="BLOCKED"), {}, "OPERATION_FINISHED"),
        (_operation(workflow_id="backlog-loop-other"), {}, "OPERATION_MISMATCH"),
        (_edit_issue, {}, "STALE_BINDING"),
        (_loop_ran_another_request, {}, "LOOP_REQUEST_MISMATCH"),
        (_loop_run(status="RUNNING"), {}, "LOOP_NOT_PAUSED"),
        (_loop_run(status="COMPLETE"), {}, "LOOP_NOT_PAUSED"),
        (_paused_on_another_issue, {}, "LOOP_NOT_PAUSED"),
        (_no_open_run, {}, "LOOP_NOT_PAUSED"),
        (_general_policy(maximum_risk="R1"), {}, "POLICY_EXCEEDED"),
        (_general_policy(delivery_mode="full-delivery"), {}, "POLICY_EXCEEDED"),
        (_another_open_loop, {}, "CAMPAIGN_IN_USE"),
        (_bound_to_another_open_operation, {}, "CAMPAIGN_BOUND_TO_OTHER_ISSUE"),
    ],
)
async def test_every_unmet_precondition_refuses_the_reapproval(
    tmp_path: Path, mutate: Mutation, overrides: dict[str, Any], blocker: str
) -> None:
    live = await _voided(tmp_path)
    if mutate is not None:
        mutate(live)
    before = live.approval()
    with pytest.raises(IssueReapprovalBlocked) as refused:
        await live.reapprove(**overrides)
    assert refused.value.blocker == f"ISSUE_REAPPROVE_{blocker}"
    assert live.approval() == before
    live.dispose()


async def test_only_an_allowlisted_operator_may_reapprove(tmp_path: Path) -> None:
    live = await _voided(tmp_path)
    with pytest.raises(ValueError, match="allowlisted operator channel"):
        await live.reapprove(actor="engineering-ctl")
    live.dispose()


def test_cli_issue_reapprove_parses_into_the_api_request() -> None:
    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, Any]] = []

        def request(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
            self.calls.append((method, path, body))
            return {}

    loop = f"backlog-loop-falcone-{CAMPAIGN_ID}-operation-e2535599-198d-46e3-b9f6-e8e2ad8da088"
    client = _Client()
    dispatch(
        client,  # type: ignore[arg-type]
        parser().parse_args(
            ["issue-reapprove", loop, "falcone", "falcone", "972", "REAPPROVE_ISSUE_E2535599"]
        ),
    )
    assert client.calls == [
        (
            "POST",
            "/v1/issues/reapprove",
            {
                "loop_workflow_id": loop,
                "project": "falcone",
                "repository": "falcone",
                "issue_number": 972,
                "confirmation_phrase": "REAPPROVE_ISSUE_E2535599",
            },
        )
    ]
    assert reapproval_phrase(UUID("e2535599-198d-46e3-b9f6-e8e2ad8da088")) == (
        "REAPPROVE_ISSUE_E2535599"
    )


def test_api_reapproves_through_the_operator_channel_only() -> None:
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    reapprove: Callable[..., Awaitable[dict[str, Any]]] = AsyncMock(
        side_effect=[
            {"status": "REAPPROVED"},
            IssueReapprovalBlocked("ISSUE_REAPPROVE_STALE_BINDING", "amend first"),
        ]
    )
    bus.reapprove_issue = reapprove
    body = {
        "loop_workflow_id": f"backlog-loop-falcone-{CAMPAIGN_ID}-operation-{uuid4()}",
        "project": "falcone",
        "repository": "falcone",
        "issue_number": 972,
        "confirmation_phrase": "REAPPROVE_ISSUE_E2535599",
    }
    headers = {"Authorization": f"Bearer {TOKEN}"}
    app = create_app(
        settings=Settings(),
        service=Mock(),
        token=TOKEN,
        operations_repository=Mock(),
        command_bus=bus,
    )
    with TestClient(app) as client:
        forged = [
            client.post(
                "/v1/issues/reapprove",
                headers={**headers, "X-Engineering-Actor": actor},
                json=body,
            )
            for actor in ("dashboard", "engineering-ctl", "telegram:x")
        ]
        operator = {**headers, "X-Engineering-Actor": "engineering-ctl:hermes"}
        malformed = client.post(
            "/v1/issues/reapprove", headers=operator, json={**body, "confirmation_phrase": "y"}
        )
        granted = client.post("/v1/issues/reapprove", headers=operator, json=body)
        refused = client.post("/v1/issues/reapprove", headers=operator, json=body)
    assert [response.status_code for response in forged] == [403] * 3
    assert malformed.status_code == 422
    assert granted.status_code == 200 and granted.json()["status"] == "REAPPROVED"
    assert refused.status_code == 409 and "STALE_BINDING" in refused.json()["detail"]
    bus.reapprove_issue.assert_awaited_with(
        body["loop_workflow_id"],
        "falcone",
        "falcone",
        972,
        "REAPPROVE_ISSUE_E2535599",
        "cli:engineering-ctl",
    )
