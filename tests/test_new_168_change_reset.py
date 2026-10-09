"""NEW-168: operator reset of a stuck, never-published delivery ChangeSet."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_controlled_operations import CAMPAIGN_ID
from test_issue_operation_approval import _approved_direct_request
from test_metadata_activity import add_campaign
from test_new_143_issue_reapproval import ISSUE, RUN, _Live

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.api.main import create_app
from engineering_control.cli import dispatch, parser
from engineering_control.config import Settings
from engineering_control.db.models import (
    BacklogIssueRecord,
    ChangeSet,
    CoverageCell,
    DashboardEvent,
    Finding,
    Issue,
    OperationRequest,
    Run,
    Step,
    WorktreeLease,
)
from engineering_control.operations.change_reset import (
    ChangeResetBlocked,
    reset_phrase,
    revalidated_delivery_heads,
)

TOKEN = "fixture-token-" + "x" * 40
ACTOR = "cli:engineering-ctl"
IMPORT = ("a" * 40, "b" * 40)
REVALIDATED = ("c" * 40, "d" * 40)


class _Stuck:
    """An issue-start loop paused on its issue after a delivery attempt left the
    ChangeSet PLANNED (falcone #975 / gntik-ui #25)."""

    def __init__(self, live: _Live, handler: MetadataActivityHandler) -> None:
        self.live = live
        self.handler = handler
        self.attempt = 1

    @property
    def change_set_id(self) -> UUID:
        return uuid5(NAMESPACE_URL, f"changeset:{self.live.finding_id}")

    def load(self) -> dict[str, Any]:
        registered = self.live.register(self.attempt)
        assert registered["status"] == "REGISTERED"
        loaded: dict[str, Any] = self.handler._load_confirmed_finding(
            {
                "finding_id": self.live.finding_id,
                "risk_class": "R1",
                "dry_run": False,
                "issue_approval_id": registered["issue_approval_id"],
                "orchestration": {
                    "workflow_id": f"{self.live.loop}-issue-1-attempt-{self.attempt}"
                },
            }
        )
        self.attempt += 1
        return loaded

    def change_set(self, **fields: Any) -> ChangeSet:
        with self.live.operations.sessions.begin() as session:
            change_set = session.get(ChangeSet, self.change_set_id)
            for key, value in fields.items():
                setattr(change_set, key, value)
            session.flush()
            session.expunge(change_set)
            return change_set

    async def reset(self, **overrides: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "loop_workflow_id": self.live.loop,
            "project": "falcone",
            "repository": "falcone",
            "issue_number": 42,
            "change_set_id": self.change_set_id,
            "confirmation": reset_phrase(self.change_set_id),
            "mode": "retry",
            "current_run_id": RUN,
            "awaited_issue": None,
            **overrides,
        }
        actor = arguments.pop("actor", ACTOR)
        result: dict[str, Any] = await self.live.operations.reset_change_set(
            actor=actor, **arguments
        )
        return result

    def dispose(self) -> None:
        self.handler.engine.dispose()
        self.live.dispose()


async def _stuck(tmp_path: Path, *, revalidated: tuple[str, str] | None = None) -> _Stuck:
    operations, metadata, engineering, request = await _approved_direct_request(tmp_path)
    live = _Live(operations, metadata, engineering, request)
    handler = MetadataActivityHandler(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url=str(operations.engine.url),  # type: ignore[arg-type]
            state_root=tmp_path / "state",
            worktree_root=tmp_path / "worktrees",
            run_root=tmp_path / "runs",
            project_config_root=Path(__file__).parents[1] / "config/projects",
        )
    )
    campaign = add_campaign(handler)
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
    impact: dict[str, Any] = {}
    if revalidated is not None:
        impact["revalidated_heads"] = _heads(revalidated)
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
        affected_source_sha=IMPORT[0],
        affected_deployment_sha=IMPORT[1],
        affected_component="api",
        impact_json=impact,
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
    live.set_campaign(status="APPROVED")
    live.loop_state("RUNNING")
    stuck = _Stuck(live, handler)
    first = stuck.load()
    assert first["status"] == "CONFIRMED_LOADED"
    # The attempt reached create_openspec (PLANNED) and then stopped without a correction
    # state: the maker's AUTH_STATUS_TIMED_OUT before NEW-168, or a crashed worker.
    stuck.change_set(state="PLANNED", version=3)
    live.loop_state("PAUSED", blocker="DELIVERY_FAILED:POLICY_VIOLATION")
    return stuck


def _heads(heads: tuple[str, str]) -> dict[str, str]:
    return {
        "source_sha": heads[0],
        "deployment_sha": heads[1],
        "import_source_sha": IMPORT[0],
        "import_deployment_sha": IMPORT[1],
        "snapshot_evidence_sha256": "e" * 64,
        "workflow_id": "revalidation-fixture",
    }


def _events(stuck: _Stuck) -> list[DashboardEvent]:
    with stuck.live.operations.sessions() as session:
        return list(
            session.scalars(
                select(DashboardEvent).where(DashboardEvent.event_type == "change_set_reset")
            ).all()
        )


async def test_retry_reset_lets_the_next_backlog_retry_bind(tmp_path: Path) -> None:
    stuck = await _stuck(tmp_path)
    stuck.live.loop_state("RUNNING")
    with pytest.raises(ValueError, match="explicit correction state"):
        stuck.load()  # the production refusal: every backlog-retry fails here
    stuck.live.loop_state("PAUSED", blocker="DELIVERY_FAILED:POLICY_VIOLATION")

    result = await stuck.reset()

    assert result["status"] == "CHANGESET_RESET"
    assert (result["from_state"], result["state"], result["version"]) == (
        "PLANNED",
        "REQUEST_CHANGES",
        4,
    )
    assert result["next"] == f"engineering-ctl backlog-retry {stuck.live.loop} falcone 42"
    change_set = stuck.change_set()
    requested = change_set.recovery_json["correction_requested"]
    assert requested["blocker"] == "OPERATOR_RESET" and requested["actor"] == ACTOR
    assert change_set.recovery_json["operator_reset"]["count"] == 1
    # The custody is untouched: the next retry child binds within the bounded lineage.
    children = change_set.recovery_json["issue_execution_approval_children"]
    events = _events(stuck)
    assert [event.entity_id for event in events] == [str(stuck.change_set_id)]
    assert events[0].metadata_json["actor"] == ACTOR
    stuck.live.loop_state("RUNNING")
    loaded = stuck.load()
    assert loaded["status"] == "CONFIRMED_LOADED"
    after = stuck.change_set().recovery_json["issue_execution_approval_children"]
    assert after[: len(children)] == children and len(after) == len(children) + 1
    stuck.dispose()


async def test_a_retry_reset_counts_toward_the_correction_limit(tmp_path: Path) -> None:
    stuck = await _stuck(tmp_path)
    limit = stuck.live.request.correction_limit + 1
    for _ in range(limit - 1):
        await stuck.reset()
        stuck.live.loop_state("RUNNING")
        assert stuck.load()["status"] == "CONFIRMED_LOADED"
        stuck.change_set(state="PLANNED")
        stuck.live.loop_state("PAUSED", blocker="DELIVERY_FAILED:POLICY_VIOLATION")
    with pytest.raises(ChangeResetBlocked, match="CHANGE_RESET_LINEAGE_EXHAUSTED"):
        await stuck.reset()
    assert stuck.change_set().state == "PLANNED"
    stuck.dispose()


async def test_rebase_restarts_an_import_based_change_set_on_its_revalidated_heads(
    tmp_path: Path,
) -> None:
    stuck = await _stuck(tmp_path)
    # A ChangeSet that got work on the import heads before its finding recorded the newer
    # revalidation heads (NEW-117 never moves a ChangeSet with work).
    with stuck.live.operations.sessions.begin() as session:
        finding = session.get(Finding, UUID(stuck.live.finding_id))
        finding.impact_json = {"revalidated_heads": _heads(REVALIDATED)}
        attempt = session.scalar(
            select(Run).where(Run.workflow_id == f"{stuck.live.loop}-issue-1-attempt-1")
        )
        lease = WorktreeLease(
            id=uuid4(),
            change_set_id=stuck.change_set_id,
            repository="gntik-ai/falcone",
            worktree_path=str(tmp_path / "worktrees/falcone" / str(stuck.change_set_id)),
            branch=f"agent/falcone/42/{stuck.change_set_id}",
            owner_run_id=attempt.id,
            base_sha=IMPORT[0],
            expires_at=datetime.now(UTC) + timedelta(hours=8),
        )
        session.add(lease)
    stuck.change_set(
        state="REQUEST_CHANGES",
        source_branch=lease.branch,
        source_head_sha="f" * 40,
        recovery_json={
            **stuck.change_set().recovery_json,
            "validation_findings": {"findings": ["old diff"], "recorded_at": "x"},
        },
    )

    result = await stuck.reset(mode="rebase")

    assert result["state"] == "CONFIRMED" and result["bases"] == list(REVALIDATED)
    assert result["released_worktree_leases"] == [str(lease.id)]
    change_set = stuck.change_set()
    assert (change_set.source_base_sha, change_set.deployment_base_sha) == REVALIDATED
    assert change_set.source_branch is None and change_set.source_head_sha is None
    assert "validation_findings" not in change_set.recovery_json
    assert change_set.recovery_json["issue_execution_approval"]  # custody kept
    with stuck.live.operations.sessions() as session:
        assert session.get(WorktreeLease, lease.id).released_at is not None
    # The next retry starts the unstarted ChangeSet from its new base.
    stuck.live.loop_state("RUNNING")
    loaded = stuck.load()
    assert (loaded["source_base_sha"], loaded["deployment_base_sha"]) == REVALIDATED
    assert loaded["base_heads"]["moved_from_import"] is True
    stuck.dispose()


async def test_rebase_without_newer_revalidated_heads_requires_a_revalidation(
    tmp_path: Path,
) -> None:
    # gntik-ui #25: the ChangeSet already sits on its finding's revalidated heads; only a
    # fresh revalidation can give a newer base.
    stuck = await _stuck(tmp_path, revalidated=REVALIDATED)
    assert (stuck.change_set().source_base_sha, stuck.change_set().deployment_base_sha) == (
        REVALIDATED
    )
    with pytest.raises(ChangeResetBlocked, match="CHANGE_RESET_REVALIDATION_REQUIRED"):
        await stuck.reset(mode="rebase")
    # No revalidation at all.
    with stuck.live.operations.sessions.begin() as session:
        session.get(Finding, UUID(stuck.live.finding_id)).impact_json = {}
    stuck.change_set(source_base_sha=IMPORT[0], deployment_base_sha=IMPORT[1])
    with pytest.raises(ChangeResetBlocked, match="CHANGE_RESET_REVALIDATION_REQUIRED"):
        await stuck.reset(mode="rebase")
    assert stuck.change_set().state == "PLANNED" and _events(stuck) == []
    stuck.dispose()


def _published_pr(stuck: _Stuck) -> None:
    stuck.change_set(source_pr_url="https://github.com/gntik-ai/falcone/pull/1")


def _published_record(stuck: _Stuck) -> None:
    stuck.change_set(recovery_json={**stuck.change_set().recovery_json, "change_evidence": {}})


def _publication_effect(stuck: _Stuck) -> None:
    with stuck.live.operations.sessions.begin() as session:
        attempt = session.scalar(
            select(Run).where(Run.workflow_id == f"{stuck.live.loop}-issue-1-attempt-1")
        )
        session.add(
            Step(
                run_id=attempt.id,
                name="delivery.publication_effect",
                ordinal=0,
                status="UNKNOWN",
                idempotency_key=f"effect-{uuid4()}",
                attempt=1,
                result_json={},
            )
        )


def _delivered(stuck: _Stuck) -> None:
    stuck.change_set(state="ISSUE_CLOSED")


def _already_requested(stuck: _Stuck) -> None:
    stuck.change_set(state="REQUEST_CHANGES")


def _unstarted(stuck: _Stuck) -> None:
    stuck.change_set(state="CONFIRMED")


def _loop_running(stuck: _Stuck) -> None:
    stuck.live.loop_state("RUNNING")


def _paused_elsewhere(stuck: _Stuck) -> None:
    stuck.live.loop_state("PAUSED", issue="gntik-ai/falcone#43")


def _foreign_issue_record(stuck: _Stuck) -> None:
    with stuck.live.operations.sessions.begin() as session:
        session.scalar(select(BacklogIssueRecord)).finding_id = uuid4()


@pytest.mark.parametrize(
    ("change", "overrides", "code"),
    [
        (None, {"actor": "engineering-ctl"}, "allowlisted operator channel"),
        (None, {"confirmation": "RESET_CHANGESET_00000000"}, "CHANGE_RESET_CONFIRMATION"),
        (None, {"mode": "force"}, "CHANGE_RESET_MODE_INVALID"),
        (
            None,
            {"loop_workflow_id": f"backlog-loop-falcone-{CAMPAIGN_ID}-operation-fixture"},
            "CHANGE_RESET_LOOP_INVALID",
        ),
        (None, {"project": "musematic"}, "CHANGE_RESET_LOOP_INVALID"),
        (None, {"issue_number": 43}, "CHANGE_RESET_LOOP_NOT_PAUSED"),
        (None, {"change_set_id": uuid4()}, "CHANGE_RESET_CONFIRMATION"),
        (None, {"current_run_id": None}, "CHANGE_RESET_LOOP_NOT_PAUSED"),
        (None, {"current_run_id": "superseded-run"}, "CHANGE_RESET_LOOP_NOT_PAUSED"),
        (_loop_running, {}, "CHANGE_RESET_LOOP_NOT_PAUSED"),
        (_paused_elsewhere, {}, "CHANGE_RESET_LOOP_NOT_PAUSED"),
        (_foreign_issue_record, {}, "CHANGE_RESET_TARGET_MISMATCH"),
        (_published_pr, {}, "CHANGE_RESET_PUBLISHED"),
        (_published_record, {}, "CHANGE_RESET_PUBLISHED"),
        (_publication_effect, {}, "CHANGE_RESET_PUBLISHED"),
        (_delivered, {}, "CHANGE_RESET_STATE"),
        (_already_requested, {}, "CHANGE_RESET_ALREADY_RETRYABLE"),
        (_unstarted, {}, "CHANGE_RESET_ALREADY_RETRYABLE"),
        (_unstarted, {"mode": "rebase"}, "CHANGE_RESET_ALREADY_RETRYABLE"),
    ],
)
async def test_every_unmet_precondition_refuses_the_reset(
    tmp_path: Path, change: Any, overrides: dict[str, Any], code: str
) -> None:
    stuck = await _stuck(tmp_path)
    if change is not None:
        change(stuck)
    before = stuck.change_set()
    with pytest.raises(ValueError, match=code):
        await stuck.reset(**overrides)
    after = stuck.change_set()
    assert (after.state, after.version, after.recovery_json) == (
        before.state,
        before.version,
        before.recovery_json,
    )
    assert _events(stuck) == []
    stuck.dispose()


async def test_a_change_set_of_another_finding_is_not_found(tmp_path: Path) -> None:
    stuck = await _stuck(tmp_path)
    other = uuid4()
    with pytest.raises(ChangeResetBlocked, match="CHANGE_RESET_CHANGESET_NOT_FOUND"):
        await stuck.reset(change_set_id=other, confirmation=reset_phrase(other))
    stuck.dispose()


async def test_a_loop_paused_before_its_pause_record_is_proven_by_its_status_query(
    tmp_path: Path,
) -> None:
    stuck = await _stuck(tmp_path)
    with stuck.live.operations.sessions.begin() as session:
        run = session.scalar(select(Run).where(Run.workflow_id == stuck.live.loop))
        for step in session.scalars(
            select(Step).where(Step.run_id == run.id, Step.name == "backlog.loop_pause_target")
        ):
            session.delete(step)
    with pytest.raises(ChangeResetBlocked, match="CHANGE_RESET_LOOP_NOT_PAUSED"):
        await stuck.reset()
    assert (await stuck.reset(awaited_issue=ISSUE))["state"] == "REQUEST_CHANGES"
    stuck.dispose()


def test_revalidated_heads_match_the_delivery_base_rule() -> None:
    finding = Mock(
        affected_source_sha=IMPORT[0],
        affected_deployment_sha=IMPORT[1],
        impact_json={"revalidated_heads": _heads(REVALIDATED)},
    )
    assert revalidated_delivery_heads(finding) == REVALIDATED
    assert MetadataActivityHandler._delivery_base_heads(finding) == REVALIDATED
    finding.impact_json = {"revalidated_heads": {**_heads(REVALIDATED), "import_source_sha": "0"}}
    assert revalidated_delivery_heads(finding) is None
    assert MetadataActivityHandler._delivery_base_heads(finding) == IMPORT


def test_cli_change_reset_parses_into_the_api_request() -> None:
    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, Any]] = []

        def request(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
            self.calls.append((method, path, body))
            return {}

    change_set_id = "9ec2e871-680f-56ce-bbb1-390a72484290"
    loop = f"backlog-loop-falcone-{CAMPAIGN_ID}-operation-{uuid4()}"
    client = _Client()
    for extra, mode in (([], "retry"), (["--mode", "rebase"], "rebase")):
        arguments = parser().parse_args(
            [
                "change-reset",
                loop,
                "falcone",
                "falcone",
                "975",
                change_set_id,
                "RESET_CHANGESET_9EC2E871",
                *extra,
            ]
        )
        dispatch(client, arguments)  # type: ignore[arg-type]
        assert client.calls[-1] == (
            "POST",
            "/v1/change-sets/reset",
            {
                "loop_workflow_id": loop,
                "project": "falcone",
                "repository": "falcone",
                "issue_number": 975,
                "change_set_id": change_set_id,
                "confirmation_phrase": "RESET_CHANGESET_9EC2E871",
                "mode": mode,
            },
        )
    assert reset_phrase(UUID(change_set_id)) == "RESET_CHANGESET_9EC2E871"


def _api(operations: Any) -> tuple[TestClient, Mock]:
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    service = Mock()
    service.current_open_run_id = AsyncMock(return_value=RUN)
    service.awaited_operator_issue = AsyncMock(return_value=ISSUE)
    client = TestClient(
        create_app(
            settings=Settings(),
            service=service,
            token=TOKEN,
            operations_repository=operations,
            command_bus=bus,
        )
    )
    return client, service


def _headers(actor: str = "engineering-ctl:hermes") -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "X-Engineering-Actor": actor}


def test_api_resets_through_the_operator_channel_and_refuses_others() -> None:
    operations = Mock()
    operations.reset_change_set = AsyncMock(
        side_effect=[
            {"status": "CHANGESET_RESET"},
            ChangeResetBlocked("CHANGE_RESET_ALREADY_RETRYABLE", "already"),
        ]
    )
    change_set_id = "9ec2e871-680f-56ce-bbb1-390a72484290"
    loop = f"backlog-loop-falcone-{CAMPAIGN_ID}-operation-{uuid4()}"
    body = {
        "loop_workflow_id": loop,
        "project": "falcone",
        "repository": "falcone",
        "issue_number": 975,
        "change_set_id": change_set_id,
        "confirmation_phrase": "RESET_CHANGESET_9EC2E871",
    }
    client, service = _api(operations)
    with client:
        forged = [
            client.post("/v1/change-sets/reset", headers=_headers(actor), json=body)
            for actor in ("dashboard", "engineering-ctl", "telegram:x")
        ]
        malformed = [
            client.post("/v1/change-sets/reset", headers=_headers(), json={**body, **change})
            for change in ({"confirmation_phrase": "yes"}, {"mode": "force"})
        ]
        reset = client.post("/v1/change-sets/reset", headers=_headers(), json=body)
        again = client.post(
            "/v1/change-sets/reset", headers=_headers(), json={**body, "mode": "rebase"}
        )
    assert [response.status_code for response in forged] == [403] * 3
    assert [response.status_code for response in malformed] == [422, 422]
    assert reset.status_code == 200 and reset.json()["status"] == "CHANGESET_RESET"
    assert again.status_code == 409
    assert "CHANGE_RESET_ALREADY_RETRYABLE" in again.json()["detail"]
    first = operations.reset_change_set.await_args_list[0].kwargs
    assert first == {
        "actor": ACTOR,
        "loop_workflow_id": loop,
        "project": "falcone",
        "repository": "falcone",
        "issue_number": 975,
        "change_set_id": UUID(change_set_id),
        "confirmation": "RESET_CHANGESET_9EC2E871",
        "mode": "retry",
        "current_run_id": RUN,  # the loop's open run and its status query, read by the API
        "awaited_issue": ISSUE,
    }
    assert operations.reset_change_set.await_args_list[1].kwargs["mode"] == "rebase"
    service.awaited_operator_issue.assert_awaited_with(loop, RUN)
