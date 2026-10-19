"""NEW-229: a loop never starts a child its campaign approval cannot outlive, an expired
approval is named as such, and a paused campaign-wide loop can be re-issued its approval
with its exact scope (gntik-ui #175: loop e686ec02 lost a passing CI-repair maker run)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from temporalio.exceptions import ApplicationError
from test_controlled_operations import CAMPAIGN_ID, _repository, _seed_ready_issue
from test_issue_operation_approval import _approved_direct_request
from test_metadata_activity import add_campaign
from test_new_143_issue_reapproval import ISSUE, RUN, _Live
from test_new_144_reissue_custody import _Delivery, _recovery, _register_ci_repair

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.api.schemas import (
    BacklogApprovalCandidateRequest,
    ExistingBacklogLoopStart,
)
from engineering_control.config import Settings
from engineering_control.db import runtime_binding
from engineering_control.db.models import (
    BacklogIssueRecord,
    ChangeSet,
    ComponentLease,
    CoverageCell,
    DashboardEvent,
    Finding,
    GitHubBacklogCampaign,
    Issue,
    NotificationOutbox,
    OperationRequest,
    Run,
    Step,
)
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.delivery_notifications import CTL, loop_commands
from engineering_control.engineering.backlog import BacklogApprovalBinding
from engineering_control.models.enums import RiskClass
from engineering_control.operations import issue_delivery_approval
from engineering_control.operations.correction_extension import loop_pause_target_id
from engineering_control.operations.issue_delivery_approval import (
    APPROVAL_LIFETIME_BLOCKERS,
    CHILD_AUTHORITY_MINIMUM_LIFETIME,
    REISSUE_CUSTODY_KEY,
    CampaignApprovalLapse,
    _custody_chain,
    _prove_reissue,
    approval_lifetime_blocker,
    validate_change_set_issue_approval,
)
from engineering_control.operations.issue_reapproval import (
    IssueReapprovalBlocked,
    loop_reapproval_phrase,
)
from engineering_control.workflows import definitions

LOOP_ID = UUID("33333333-3333-4333-8333-333333333333")
LOOP = f"backlog-loop-falcone-{CAMPAIGN_ID}-{LOOP_ID}"
ACTOR = "cli:engineering-ctl"


class _CampaignLive(_Live):
    """A campaign-wide (backlog-start) loop on the real repositories."""

    def __init__(self, operations: Any, metadata: Any, engineering: Any, request: Any) -> None:
        with patch("test_new_143_issue_reapproval.issue_workflow_id", return_value=LOOP):
            super().__init__(operations, metadata, engineering, request)

    async def reapprove(self, **overrides: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "loop_workflow_id": LOOP,
            "project": "falcone",
            "repository": "falcone",
            "issue_number": 42,
            "phrase": loop_reapproval_phrase(LOOP, "falcone"),
            "actor": ACTOR,
            **overrides,
        }
        result: dict[str, Any] = await self.bus.reapprove_issue(**arguments)
        return result


def _clock(monkeypatch: pytest.MonkeyPatch, ahead: timedelta) -> None:
    """The custody module's clock, ``ahead`` of now (the approval itself is unchanged)."""

    class _Later(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return datetime.now(tz) + ahead

    monkeypatch.setattr(issue_delivery_approval, "datetime", _Later)


def _seed_finding(live: _Live, tmp_path: Path, *, register_loop: bool = False) -> None:
    handler = MetadataActivityHandler(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url=str(live.operations.engine.url),  # type: ignore[arg-type]
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
    with live.operations.sessions.begin() as session:
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
        if live.operation_id is not None:
            operation = session.get(OperationRequest, live.operation_id)
            operation.status = "STARTED"
            operation.workflow_id = live.loop
        if not register_loop:
            session.add(
                Run(
                    workflow_id=live.loop,
                    run_type="existing-backlog-loop",
                    status="RUNNING",
                    project="falcone",
                    temporal_first_execution_run_id=RUN,
                )
            )
    if register_loop:
        # The loop registers its own Run (and, NEW-229, the request it starts with).
        token = runtime_binding._current_identity.set(
            runtime_binding._Identity(live.loop, RUN, RUN)
        )
        try:
            registered = live.engineering._register_backlog_run(
                {**live.request.model_dump(mode="json"), "workflow_id": live.loop}
            )
        finally:
            runtime_binding._current_identity.reset(token)
        assert registered["status"] == "REGISTERED"
    live.finding_id = str(finding.id)
    live.set_campaign(status="APPROVED")  # the reconciler activated the policy
    live.loop_state("RUNNING")


async def _campaign_start(tmp_path: Path) -> _CampaignLive:
    """backlog-approve (R1, source-pr, 5 items) and backlog-start's loop registered."""
    operations = _repository(tmp_path)
    _seed_ready_issue(operations)
    policy_root = tmp_path / "fixture-policy-requests"
    policy_root.mkdir(mode=0o700)
    metadata = SQLMetadataRepository(
        str(operations.engine.url), github_policy_request_root=policy_root
    )
    scope = {"delivery_mode": "source-pr", "maximum_risk": RiskClass.R1, "maximum_items": 5}
    candidate = await metadata.prepare_backlog_approval(
        CAMPAIGN_ID, BacklogApprovalCandidateRequest(project="falcone", **scope)
    )
    await metadata.approve_backlog_campaign(
        CAMPAIGN_ID, "falcone", candidate["approval_binding"], ACTOR
    )
    engineering = EngineeringActivityHandler(
        Settings(_env_file=None, database_url=str(operations.engine.url))  # type: ignore[call-arg]
    )
    request = ExistingBacklogLoopStart(project="falcone", backlog_campaign_id=CAMPAIGN_ID, **scope)
    live = _CampaignLive(operations, metadata, engineering, request)
    _seed_finding(live, tmp_path, register_loop=True)
    return live


async def _campaign_loop(tmp_path: Path) -> tuple[_CampaignLive, _Delivery]:
    """_campaign_start, and its first delivery child published its ChangeSet (SOURCE_PR)
    and finished."""
    live = await _campaign_start(tmp_path)
    delivery = _Delivery(tmp_path)
    delivery(live, live.register(1))
    _finish_children(live)
    return live, delivery


async def _issue_loop(tmp_path: Path) -> _Live:
    operations, metadata, engineering, request = await _approved_direct_request(tmp_path)
    live = _Live(operations, metadata, engineering, request)
    _seed_finding(live, tmp_path)
    assert live.register(1)["status"] == "REGISTERED"
    return live


def _finish_children(live: _Live) -> None:
    with live.operations.sessions.begin() as session:
        for run in session.scalars(select(Run).where(Run.run_type != "existing-backlog-loop")):
            run.status = "COMPLETE"
            run.finished_at = datetime.now(UTC)


def _refused(call: Any, blocker: str) -> ApplicationError:
    with pytest.raises(ApplicationError) as raised:
        call()
    assert raised.value.type == blocker
    assert raised.value.non_retryable
    return raised.value


def _child_runs(live: _Live) -> list[str]:
    with live.operations.sessions() as session:
        return sorted(
            session.scalars(
                select(Run.workflow_id).where(Run.run_type != "existing-backlog-loop")
            ).all()
        )


def _expires_at(live: _Live) -> datetime:
    with live.operations.sessions() as session:
        campaign = session.get(GitHubBacklogCampaign, CAMPAIGN_ID)
        return BacklogApprovalBinding.model_validate(campaign.approval_json["binding"]).expires_at


def test_the_child_bound_covers_every_agent_timeout() -> None:
    # spec 1 h + source and deployment makers 2 h each + checker 1.5 h, plus the margin.
    assert CHILD_AUTHORITY_MINIMUM_LIFETIME == timedelta(hours=8)
    assert CHILD_AUTHORITY_MINIMUM_LIFETIME > timedelta(seconds=3600 + 2 * 7200 + 5400)
    assert definitions._APPROVAL_LIFETIME_BLOCKERS == APPROVAL_LIFETIME_BLOCKERS


@pytest.mark.parametrize(
    ("ahead", "blocker"),
    [
        (timedelta(hours=17), "CAMPAIGN_APPROVAL_EXPIRING"),  # 7 h left: below the bound
        (timedelta(hours=25), "CAMPAIGN_APPROVAL_EXPIRED"),
    ],
)
async def test_a_campaign_loop_child_is_not_started_on_a_lapsing_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ahead: timedelta, blocker: str
) -> None:
    live, delivery = await _campaign_loop(tmp_path)
    before = _child_runs(live)
    _clock(monkeypatch, ahead)

    refused = _refused(lambda: live.register(2), blocker)

    # Nothing was registered (the transaction rolled back): no Run, no approval Step.
    assert _child_runs(live) == before
    assert refused.details == ({"approval_expires_at": _expires_at(live).isoformat()},)
    # With 9 h left the child starts as before.
    _clock(monkeypatch, timedelta(hours=15))
    assert live.register(2)["status"] == "REGISTERED"
    delivery.handler.engine.dispose()  # type: ignore[union-attr]
    live.dispose()


async def test_an_issue_start_child_pauses_and_issue_reapprove_renews_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = await _issue_loop(tmp_path)
    _clock(monkeypatch, timedelta(hours=17))
    _refused(lambda: live.register(2), "CAMPAIGN_APPROVAL_EXPIRING")
    live.loop_state("PAUSED", blocker="CAMPAIGN_APPROVAL_EXPIRING")
    monkeypatch.undo()
    # The issue-start remedy is the existing NEW-143 re-approval: a fresh 24 h approval.
    assert (await live.reapprove())["status"] == "REAPPROVED"
    live.loop_state("RUNNING")
    assert live.register(2)["status"] == "REGISTERED"
    live.dispose()


async def test_a_ci_repair_round_and_a_revalidation_are_not_started_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, delivery = await _campaign_loop(tmp_path)
    before = _child_runs(live)
    _clock(monkeypatch, timedelta(hours=20))
    _refused(lambda: _register_ci_repair(live, delivery, 4), "CAMPAIGN_APPROVAL_EXPIRING")
    revalidation = {
        **live.request.model_dump(mode="json"),
        "child_kind": "finding-verification",
        "child_workflow_id": f"{LOOP}-issue-2-revalidation",
        "finding_id": live.finding_id,
    }
    _refused(
        lambda: live.engineering._register_backlog_child_run(revalidation),
        "CAMPAIGN_APPROVAL_EXPIRING",
    )
    assert _child_runs(live) == before
    with live.operations.sessions() as session:
        change_set = session.get(ChangeSet, delivery.change_set_id)
        assert change_set.state == "SOURCE_PR"  # the published ChangeSet is untouched
    delivery.handler.engine.dispose()  # type: ignore[union-attr]
    live.dispose()


async def test_an_expired_approval_inside_a_child_is_named_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, delivery = await _campaign_loop(tmp_path)
    handler = delivery.handler
    assert handler is not None
    payload = {"change_set_id": str(delivery.change_set_id)}
    _clock(monkeypatch, timedelta(hours=25))
    with pytest.raises(CampaignApprovalLapse) as lapse:
        handler._issue_approval_preparation(payload)
    assert lapse.value.blocker == "CAMPAIGN_APPROVAL_EXPIRED"
    assert isinstance(lapse.value, ValueError)  # every existing refusal path still applies
    blocked = await handler.handle("delivery.prepare_deployment_maker", payload)
    assert (blocked["status"], blocked["blocker"]) == ("BLOCKED", "CAMPAIGN_APPROVAL_EXPIRED")
    # An approval that changed as well stays the generic refusal.
    with live.operations.sessions.begin() as session:
        campaign = session.get(GitHubBacklogCampaign, CAMPAIGN_ID)
        binding = BacklogApprovalBinding.model_validate(campaign.approval_json["binding"])
        changed = binding.model_copy(update={"maximum_items": 4})
        campaign.approval_json = {
            **campaign.approval_json,
            "binding": changed.model_dump(mode="json"),
        }
        campaign.approval_binding = changed.sha256()
    blocked = await handler.handle("delivery.prepare_deployment_maker", payload)
    assert blocked["blocker"] == "DELIVERY_ISSUE_APPROVAL_REQUIRED_OR_CHANGED"
    handler.engine.dispose()
    live.dispose()


async def test_the_pause_names_the_expiry_and_the_reissue_command(tmp_path: Path) -> None:
    live, delivery = await _campaign_loop(tmp_path)
    live.loop_state("PAUSED", blocker="CAMPAIGN_APPROVAL_EXPIRING")
    expires_at = _expires_at(live).isoformat()
    with live.operations.sessions() as session:
        loop = session.scalar(select(Run).where(Run.workflow_id == LOOP))
        pause = session.get(Step, loop_pause_target_id(loop.id))
        assert pause.result_json["blocker"] == "CAMPAIGN_APPROVAL_EXPIRING"
        assert pause.result_json["approval_expires_at"] == expires_at
        (notice,) = [
            row
            for row in session.scalars(select(NotificationOutbox)).all()
            if row.payload_json.get("blocker") == "CAMPAIGN_APPROVAL_EXPIRING"
        ]
    phrase = loop_reapproval_phrase(LOOP, "falcone")
    assert phrase == f"REAPPROVE_ISSUE_{LOOP_ID.hex[:8].upper()}"
    body = notice.payload_json
    assert body["blocker"] == "CAMPAIGN_APPROVAL_EXPIRING"
    assert body["approval_expires_at"] == datetime.fromisoformat(expires_at).isoformat(
        timespec="seconds"
    )
    assert "campaign approval expiry" in body["message"]
    assert any(
        command.endswith(f"issue-reapprove {LOOP} falcone falcone 42 {phrase}")
        for command in body["commands"]
    )
    delivery.handler.engine.dispose()  # type: ignore[union-attr]
    live.dispose()


async def _reissued_campaign(tmp_path: Path) -> tuple[_CampaignLive, _Delivery]:
    live, delivery = await _campaign_loop(tmp_path)
    live.loop_state("PAUSED", blocker="CAMPAIGN_APPROVAL_EXPIRING")
    result = await live.reapprove()
    assert result["status"] == "REAPPROVED", result
    live.loop_state("RUNNING")  # backlog-retry resumes the paused issue
    return live, delivery


def _event(live: _Live) -> DashboardEvent:
    with live.operations.sessions() as session:
        return session.scalars(
            select(DashboardEvent)
            .where(DashboardEvent.event_type == "issue_approval_reissued")
            .order_by(DashboardEvent.id)
        ).all()[-1]


async def test_a_paused_campaign_loop_is_reissued_its_exact_scope(tmp_path: Path) -> None:
    live, delivery = await _campaign_loop(tmp_path)
    live.loop_state("PAUSED", blocker="CAMPAIGN_APPROVAL_EXPIRING")
    with live.operations.sessions() as session:
        old = session.get(GitHubBacklogCampaign, CAMPAIGN_ID).approval_json["binding"]

    result = await live.reapprove()

    assert result["status"] == "REAPPROVED"
    assert result["issue_operation_id"] is None
    assert result["next"] == f"engineering-ctl backlog-retry {LOOP} falcone 42"
    status, approval = live.approval()
    assert status == "APPROVED"
    binding = approval["binding"]
    assert binding.get("issue_approval") is None
    for key in ("project", "delivery_mode", "maximum_risk", "maximum_items", "source_head"):
        assert binding[key] == old[key]
    assert binding["expires_at"] > old["expires_at"]
    event = _event(live)
    assert (event.entity_type, event.entity_id, event.workflow_id) == (
        "backlog_campaign",
        str(CAMPAIGN_ID),
        LOOP,
    )
    assert event.metadata_json["approval_binding"] == result["approval_binding"]
    assert event.metadata_json["loop_scope"]["maximum_items"] == 5
    # A rerun is idempotent.
    assert (await live.reapprove())["status"] == "ALREADY_APPROVED"
    # backlog-retry: the paused issue's CI-repair round continues the published ChangeSet.
    live.loop_state("RUNNING")
    registered, child = _register_ci_repair(live, delivery, 4)
    loaded = delivery.load(live, registered, child)
    assert loaded["status"] == "CONFIRMED_LOADED"
    assert loaded["issue_execution_approval"] == delivery.first["issue_execution_approval"]
    recovery = _recovery(live, delivery)
    (move,) = recovery[REISSUE_CUSTODY_KEY]
    assert (move["audit_event_id"], move["to"]) == (event.id, result["approval_binding"])
    assert move["issue_operation_id"] is None and move["loop_workflow_id"] == LOOP
    with live.operations.sessions() as session:
        change_set = session.get(ChangeSet, delivery.change_set_id)
        assert change_set.source_pr_url.endswith("/pull/972")  # the published PR is kept
        assert (
            validate_change_set_issue_approval(session, change_set.id)
            == (recovery["issue_execution_approval"])
        )
    delivery.handler.engine.dispose()  # type: ignore[union-attr]
    live.dispose()


async def test_a_wrong_phrase_returns_the_expected_one(tmp_path: Path) -> None:
    live, delivery = await _campaign_loop(tmp_path)
    live.loop_state("PAUSED", blocker="CAMPAIGN_APPROVAL_EXPIRED")
    expected = loop_reapproval_phrase(LOOP, "falcone")
    with pytest.raises(IssueReapprovalBlocked, match=f"CONFIRMATION_MISMATCH.*{expected}"):
        await live.reapprove(phrase="REAPPROVE_ISSUE_00000000")
    assert _event_or_none(live) is None
    delivery.handler.engine.dispose()  # type: ignore[union-attr]
    live.dispose()


def _event_or_none(live: _Live) -> DashboardEvent | None:
    with live.operations.sessions() as session:
        return session.scalar(
            select(DashboardEvent).where(DashboardEvent.event_type == "issue_approval_reissued")
        )


def _running_child(live: _Live) -> None:
    with live.operations.sessions.begin() as session:
        session.add(
            Run(
                workflow_id=f"{LOOP}-issue-1-ci-repair-3-attempt-1",
                run_type="issue-delivery",
                status="RUNNING",
                project="falcone",
            )
        )


def _leased_child(live: _Live) -> None:
    with live.operations.sessions.begin() as session:
        child = session.scalar(select(Run).where(Run.run_type == "issue-delivery"))
        session.add(
            ComponentLease(
                component_key="falcone:api",
                owner_run_id=child.id,
                mode="write",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )


def _ran_another_request(live: _Live) -> None:
    with live.operations.sessions.begin() as session:
        step = session.scalar(select(Step).where(Step.name == "delivery.issue_approval"))
        session.add(
            Step(
                id=uuid4(),
                run_id=step.run_id,
                name="delivery.issue_approval",
                ordinal=1,
                status="COMPLETE",
                idempotency_key=f"fixture-other-request-{uuid4()}",
                attempt=1,
                result_json={
                    **step.result_json,
                    "request": {**step.result_json["request"], "maximum_items": 50},
                },
            )
        )


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (_running_child, "CHILD_ACTIVE"),
        (_leased_child, "CHILD_ACTIVE"),
        (_ran_another_request, "LOOP_REQUEST_MISMATCH"),
    ],
)
async def test_a_reissue_is_refused_while_a_child_runs_or_the_scope_differs(
    tmp_path: Path, change: Any, code: str
) -> None:
    live, delivery = await _campaign_loop(tmp_path)
    live.loop_state("PAUSED", blocker="CAMPAIGN_APPROVAL_EXPIRING")
    change(live)
    with pytest.raises(IssueReapprovalBlocked, match=f"ISSUE_REAPPROVE_{code}"):
        await live.reapprove()
    assert _event_or_none(live) is None
    delivery.handler.engine.dispose()  # type: ignore[union-attr]
    live.dispose()


async def test_custody_refuses_a_different_scope_or_a_plain_reapproval(tmp_path: Path) -> None:
    live, delivery = await _reissued_campaign(tmp_path)
    with live.operations.sessions.begin() as session:
        event = session.get(DashboardEvent, _event(live).id)
        scope = {**event.metadata_json["loop_scope"], "maximum_items": 50}
        event.metadata_json = {**event.metadata_json, "loop_scope": scope}
    with pytest.raises(ValueError, match="already belongs to another issue approval"):
        delivery.load(live, *_register_ci_repair(live, delivery, 4))
    assert REISSUE_CUSTODY_KEY not in _recovery(live, delivery)
    # A plain backlog-approve (no re-issue for the loop) never continues the custody.
    with live.operations.sessions.begin() as session:
        session.delete(session.get(DashboardEvent, _event(live).id))
    with pytest.raises(ValueError, match="already belongs to another issue approval"):
        delivery.load(live, *_register_ci_repair(live, delivery, 5))
    delivery.handler.engine.dispose()  # type: ignore[union-attr]
    live.dispose()


async def test_an_older_campaign_reissue_is_never_replayed(tmp_path: Path) -> None:
    live, delivery = await _reissued_campaign(tmp_path)  # re-issue B
    with live.operations.sessions() as session:
        older = session.get(GitHubBacklogCampaign, CAMPAIGN_ID).approval_binding
    live.loop_state("PAUSED", blocker="CAMPAIGN_APPROVAL_EXPIRING")
    assert (await live.reapprove())["status"] == "REAPPROVED"  # re-issue C
    live.loop_state("RUNNING")
    assert delivery.load(live, *_register_ci_repair(live, delivery, 4))["status"] == (
        "CONFIRMED_LOADED"
    )
    (move,) = _recovery(live, delivery)[REISSUE_CUSTODY_KEY]  # A -> C
    with live.operations.sessions() as session:
        change_set = session.get(ChangeSet, delivery.change_set_id)
        origin = session.get(
            Step, UUID(change_set.recovery_json["issue_execution_approval"]["approval_id"])
        )
        stale = {**origin.result_json, "campaign_approval_binding": older}
        record = SimpleNamespace(id=uuid4(), result_json=stale)
        bindings, latest = _custody_chain(session, change_set, origin)
        assert latest[0] == move["audit_event_id"]
        with pytest.raises(ValueError, match="already belongs to another issue approval"):
            _prove_reissue(session, origin, record, bindings[-1], latest)  # type: ignore[arg-type]
        # Without a newer move the same older re-issue is still a valid first move.
        assert _prove_reissue(session, origin, record, "f" * 64)["to"] == older  # type: ignore[arg-type]
    delivery.handler.engine.dispose()  # type: ignore[union-attr]
    live.dispose()


async def test_an_issue_loop_reissue_event_never_moves_a_campaign_loop(tmp_path: Path) -> None:
    live, delivery = await _reissued_campaign(tmp_path)
    with live.operations.sessions.begin() as session:
        event = session.get(DashboardEvent, _event(live).id)
        event.entity_type = "operation_request"
        event.entity_id = str(uuid4())
    with pytest.raises(ValueError, match="already belongs to another issue approval"):
        delivery.load(live, *_register_ci_repair(live, delivery, 4))
    assert ISSUE.endswith("#42")
    delivery.handler.engine.dispose()  # type: ignore[union-attr]
    live.dispose()


def test_both_loop_kinds_get_their_own_reissue_command() -> None:
    operation = uuid4()
    issue_loop = f"backlog-loop-falcone-{CAMPAIGN_ID}-operation-{operation}"
    for loop, identity in ((issue_loop, operation), (LOOP, LOOP_ID)):
        commands = loop_commands(
            workflow_id=loop,
            project="falcone",
            issue="gntik-ai/falcone#42",
            state="PAUSED",
            blocker="CAMPAIGN_APPROVAL_EXPIRED",
        )
        phrase = f"REAPPROVE_ISSUE_{identity.hex[:8].upper()}"
        assert f"{CTL} issue-reapprove {loop} falcone falcone 42 {phrase}" in commands
        assert f"{CTL} backlog-retry {loop} falcone 42" in commands
        assert not any("backlog-approve " in command for command in commands)
    assert loop_reapproval_phrase(LOOP, "musematic") is None


async def test_a_loop_paused_before_its_first_child_is_reissued_from_its_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex review: the first registration rolls back, so no delivery child records the
    # request; the loop's own start record (backlog.register_run) supplies the scope.
    live = await _campaign_start(tmp_path)
    _clock(monkeypatch, timedelta(hours=17))
    _refused(lambda: live.register(1), "CAMPAIGN_APPROVAL_EXPIRING")
    monkeypatch.undo()
    live.loop_state("PAUSED", blocker="CAMPAIGN_APPROVAL_EXPIRING")
    assert (await live.reapprove())["status"] == "REAPPROVED"
    assert _event(live).metadata_json["loop_scope"]["maximum_items"] == 5
    live.loop_state("RUNNING")
    assert live.register(1)["status"] == "REGISTERED"
    # A loop registered before NEW-229, with nothing recorded, gets the plain remedy.
    with live.operations.sessions.begin() as session:
        for step in session.scalars(
            select(Step).where(Step.name.in_(["backlog.loop_request", "delivery.issue_approval"]))
        ):
            session.delete(step)
    live.loop_state("PAUSED", blocker="CAMPAIGN_APPROVAL_EXPIRING")
    with pytest.raises(IssueReapprovalBlocked, match="NO_CUSTODY.*backlog-approve"):
        await live.reapprove()
    live.dispose()


async def test_a_recorded_active_child_proven_closed_in_temporal_does_not_refuse(
    tmp_path: Path,
) -> None:
    live, delivery = await _campaign_loop(tmp_path)
    live.loop_state("PAUSED", blocker="CAMPAIGN_APPROVAL_EXPIRING")
    _running_child(live)
    live.control.workflow_id_closed = AsyncMock(side_effect=RuntimeError("unavailable"))
    with pytest.raises(ValueError, match="CHILD_ACTIVE: cannot verify"):
        await live.reapprove()
    live.control.workflow_id_closed = AsyncMock(return_value=True)
    assert (await live.reapprove())["status"] == "REAPPROVED"
    delivery.handler.engine.dispose()  # type: ignore[union-attr]
    live.dispose()


async def test_an_attempt_stopped_by_expiry_is_ended_and_its_retry_binds(
    tmp_path: Path,
) -> None:
    # Codex review: the stopped attempt's Run stayed RUNNING and its ChangeSet in
    # IMPLEMENTING, so neither the re-issue nor the retry child could proceed.
    live, delivery = await _campaign_loop(tmp_path)
    handler = delivery.handler
    assert handler is not None
    with live.operations.sessions.begin() as session:
        change_set = session.get(ChangeSet, delivery.change_set_id)
        change_set.state = "IMPLEMENTING"
        change_set.source_pr_url = None
        run = session.scalar(select(Run).where(Run.workflow_id == f"{LOOP}-issue-1-attempt-1"))
        run.status = "RUNNING"
        run.finished_at = None
        run_id = run.id
    handler._release_delivery_leases(
        {
            "run_id": str(run_id),
            "change_set_id": str(delivery.change_set_id),
            "status": "BLOCKED",
            "blocker": "CAMPAIGN_APPROVAL_EXPIRED",
        }
    )
    with live.operations.sessions() as session:
        run = session.get(Run, run_id)
        assert (run.status, run.error_code) == ("BLOCKED", "CAMPAIGN_APPROVAL_EXPIRED")
        change_set = session.get(ChangeSet, delivery.change_set_id)
        assert change_set.state == "REQUEST_CHANGES"
    live.loop_state("PAUSED", blocker="CAMPAIGN_APPROVAL_EXPIRED")
    assert (await live.reapprove())["status"] == "REAPPROVED"
    live.loop_state("RUNNING")
    child = f"{LOOP}-issue-1-retry-1-attempt-1"
    registered = live.engineering._register_backlog_child_run(
        {
            **live.request.model_dump(mode="json"),
            "child_kind": "issue-delivery",
            "child_workflow_id": child,
            "finding_id": live.finding_id,
            "orchestration": {"workflow_id": LOOP},
        }
    )
    loaded = delivery.load(live, registered, child)
    assert loaded["status"] == "CONFIRMED_LOADED"
    assert len(_recovery(live, delivery)[REISSUE_CUSTODY_KEY]) == 1
    handler.engine.dispose()
    live.dispose()


def test_a_blocked_child_names_an_expired_approval_to_its_loop() -> None:
    blocked = {"status": "BLOCKED", "context": {"blocker": "CAMPAIGN_APPROVAL_EXPIRED"}}
    assert definitions._approval_lifetime_blocker(blocked) == "CAMPAIGN_APPROVAL_EXPIRED"
    other = {"status": "BLOCKED", "context": {"blocker": "MAKER_TEST_FAILURE"}}
    assert definitions._approval_lifetime_blocker(other) is None
    assert approval_lifetime_blocker("CI_REPAIR_CAMPAIGN_APPROVAL_EXPIRED") == (
        "CAMPAIGN_APPROVAL_EXPIRED"
    )
    commands = loop_commands(
        workflow_id=LOOP,
        project="falcone",
        issue="gntik-ai/falcone#42",
        state="PAUSED",
        blocker="CI_REPAIR_CAMPAIGN_APPROVAL_EXPIRED",
    )
    assert any(" issue-reapprove " in command for command in commands)
