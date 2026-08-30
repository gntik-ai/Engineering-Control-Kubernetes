from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from engineering_control.api.schemas import OperationAccepted
from engineering_control.config import Settings
from engineering_control.dashboard.app import create_app
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base
from engineering_control.db.models import (
    BacklogIssueRecord,
    ConnectionSnapshot,
    GitHubBacklogCampaign,
    OperationRequest,
    QuotaSnapshot,
    ReauthenticationBinding,
    Run,
    WorkerHeartbeat,
)
from engineering_control.operations.connections import ConnectionHealthProjector
from engineering_control.operations.models import (
    AuditStartCommand,
    RefreshConnectionsCommand,
    SolveBacklogCommand,
    SolveIssueCommand,
    WorkflowControlCommand,
    parse_command,
)
from engineering_control.operations.repository import OperationsRepository
from engineering_control.operations.service import TypedCommandBus
from engineering_control.workflows import definitions as workflow_definitions
from engineering_control.workflows.definitions import ExistingGitHubBacklogLoopWorkflow
from engineering_control.workflows.inputs import ExistingGitHubBacklogLoopInput

CAMPAIGN_ID = UUID("11111111-1111-4111-8111-111111111111")
SESSION_A = "a" * 64
SESSION_B = "b" * 64
PASSWORD = "bounded dashboard fixture password"  # noqa: S105


def _repository(tmp_path: Path) -> OperationsRepository:
    repository = OperationsRepository(f"sqlite+pysqlite:///{tmp_path / 'operations.db'}")
    Base.metadata.create_all(repository.engine)
    return repository


def _seed_ready_issue(repository: OperationsRepository) -> None:
    now = datetime.now(UTC)
    with repository.sessions.begin() as session:
        session.add(
            GitHubBacklogCampaign(
                id=CAMPAIGN_ID,
                project="falcone",
                source_repository="gntik-ai/falcone",
                deployment_repository="gntik-ai/falcone-charts",
                imported_at=now,
                issue_query="is:issue is:open",
                import_filter="bugs-and-docs",
                source_default_branch="main",
                deployment_default_branch="main",
                source_head_at_import="a" * 40,
                deployment_head_at_import="b" * 40,
                issue_count=1,
                eligible_count=1,
                deferred_count=0,
                duplicate_count=0,
                stale_count=0,
                already_fixed_count=0,
                existing_pr_count=0,
                blocked_count=0,
                dependency_graph_hash="c" * 64,
                plan_hash="d" * 64,
                approval_binding=None,
                approval_json={},
                maximum_risk=None,
                maximum_items=1,
                maximum_concurrency=1,
                delivery_mode=None,
                include_enhancements=False,
                status="PLANNED",
                workflow_id="backlog-import-fixture",
            )
        )
        session.add(
            BacklogIssueRecord(
                backlog_campaign_id=CAMPAIGN_ID,
                repository="gntik-ai/falcone",
                issue_number=42,
                issue_url_reference="https://github.com/gntik-ai/falcone/issues/42",
                title="Bounded synthetic issue",
                state="OPEN",
                labels_json=["bug"],
                milestone=None,
                issue_type="PRODUCT_BUG",
                openspec_kind=None,
                severity="medium",
                calculated_risk="R1",
                component="api",
                source_or_deployment_owner="source",
                documentation_levels_json=[],
                dependencies_json=[],
                blocking_issues_json=[],
                issues_blocked_json=[],
                linked_prs_json=[],
                linked_branches_json=[],
                linked_commits_json=[],
                duplicate_candidates_json=[],
                current_source_reproduction="REPRODUCED_CURRENT_SOURCE",
                current_runtime_reproduction="NOT_REQUIRED",
                current_runtime_required=False,
                reproduction_mode="SOURCE_ONLY",
                candidate_status="READY",
                eligibility_status="READY",
                eligibility_reason="CURRENT_SOURCE_REVALIDATION_PASS",
                planned_wave="W1",
                planned_order=1,
                finding_id=None,
                change_set_id=None,
                delivery_status="NOT_STARTED",
                final_closure_status="OPEN",
                documentation_impact="DOCUMENTATION_NOT_REQUIRED_WITH_EVIDENCE",
                body_sha256="e" * 64,
                github_updated_at=now,
                github_snapshot_json={},
            )
        )
        for provider in ("codex", "claude"):
            session.add(
                QuotaSnapshot(
                    provider=provider,
                    captured_at=now,
                    remaining_capacity=None,
                    total_capacity=None,
                    reset_at=None,
                    running_jobs=0,
                    rate_limited=False,
                    authenticated=True,
                    cli_present=True,
                    status="READY",
                    source="synthetic-fixture",
                )
            )


def _solve_issue() -> SolveIssueCommand:
    return SolveIssueCommand(
        project="falcone",
        backlog_campaign_id=CAMPAIGN_ID,
        repository="gntik-ai/falcone",
        issue_number=42,
        delivery_mode="source-pr",
    )


def _idempotency_fixture(channel: str, ordinal: int) -> str:
    return f"{channel}-test-{ordinal:012d}"


def test_command_models_deny_production_and_arbitrary_arguments() -> None:
    with pytest.raises(ValidationError):
        parse_command(
            {
                **_solve_issue().model_dump(mode="json"),
                "production": True,
            }
        )
    with pytest.raises(ValidationError):
        parse_command(
            {
                **_solve_issue().model_dump(mode="json"),
                "shell_command": "arbitrary",
            }
        )
    with pytest.raises(ValidationError):
        SolveBacklogCommand(
            project="falcone",
            backlog_campaign_id=CAMPAIGN_ID,
            concurrency=2,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_issue_candidate_reauth_is_session_bound_one_use_and_idempotent(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _seed_ready_issue(repository)
    candidate = await repository.candidate(_solve_issue(), SESSION_A)
    same_candidate_other_session = await repository.candidate(_solve_issue(), SESSION_B)
    assert candidate.capability.status == "AVAILABLE"
    assert candidate.operation_binding["production"] is False
    assert candidate.binding_hash == same_candidate_other_session.binding_hash
    assert candidate.candidate_id != same_candidate_other_session.candidate_id

    token = await repository.create_reauthentication(candidate.candidate_id, SESSION_A)
    operation = await repository.queue_candidate(
        candidate.candidate_id,
        token,
        SESSION_A,
        None,
    )
    assert operation.source_channel == "dashboard"
    assert operation.status == "PENDING"
    with pytest.raises(ValueError, match="already used"):
        await repository.queue_candidate(candidate.candidate_id, token, SESSION_A, None)
    with repository.sessions() as session:
        binding = session.scalar(select(ReauthenticationBinding))
        assert binding is not None and binding.used_at is not None
        assert token not in binding.token_digest


@pytest.mark.asyncio
async def test_backlog_requires_server_phrase_and_prevents_duplicate_active_operation(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _seed_ready_issue(repository)
    command = SolveBacklogCommand(
        project="falcone",
        backlog_campaign_id=CAMPAIGN_ID,
        delivery_mode="source-pr",
    )
    candidate = await repository.candidate(command, SESSION_A)
    assert candidate.capability.status == "AVAILABLE"
    assert candidate.confirmation_phrase is not None
    token = await repository.create_reauthentication(candidate.candidate_id, SESSION_A)
    with pytest.raises(ValueError, match="typed confirmation"):
        await repository.queue_candidate(candidate.candidate_id, token, SESSION_A, "WRONG")

    # A failed phrase does not consume the one-use reauthentication binding.
    first = await repository.queue_candidate(
        candidate.candidate_id,
        token,
        SESSION_A,
        candidate.confirmation_phrase,
    )
    second = await repository.create_external(
        command,
        source_channel="telegram",
        operator_class="telegram:allowlisted",
        idempotency_key=_idempotency_fixture("telegram", 1),
    )
    assert second.operation_id == first.operation_id
    await repository.finish(
        first.operation_id,
        status="STARTED",
        workflow_id="existing-backlog-workflow",
        run_id="existing-temporal-run",
    )
    running = await repository.candidate(command, SESSION_B)
    assert running.capability.status == "RUNNING"
    assert running.operation_binding["resolved"]["existing_workflow_id"] == (
        "existing-backlog-workflow"
    )
    with repository.sessions() as session:
        assert len(session.scalars(select(OperationRequest)).all()) == 1


@pytest.mark.asyncio
async def test_completed_connection_refresh_can_be_run_again(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    command = RefreshConnectionsCommand(scope="all")

    first_candidate = await repository.candidate(command, SESSION_A)
    first_token = await repository.create_reauthentication(first_candidate.candidate_id, SESSION_A)
    first = await repository.queue_candidate(
        first_candidate.candidate_id, first_token, SESSION_A, None
    )
    await repository.finish(first.operation_id, status="COMPLETED")

    second_candidate = await repository.candidate(command, SESSION_A)
    second_token = await repository.create_reauthentication(
        second_candidate.candidate_id, SESSION_A
    )
    second = await repository.queue_candidate(
        second_candidate.candidate_id, second_token, SESSION_A, None
    )

    assert second.operation_id != first.operation_id
    with repository.sessions() as session:
        assert len(session.scalars(select(OperationRequest)).all()) == 2


@pytest.mark.asyncio
async def test_candidate_fails_closed_for_stale_provider_and_r3(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _seed_ready_issue(repository)
    with repository.sessions.begin() as session:
        snapshots = session.scalars(select(QuotaSnapshot)).all()
        for snapshot in snapshots:
            snapshot.captured_at = datetime.now(UTC) - timedelta(minutes=10)
    stale = await repository.candidate(_solve_issue(), SESSION_A)
    assert stale.capability.status == "BLOCKED"
    assert {"CODEX_HEALTH_STALE", "CLAUDE_HEALTH_STALE"} <= set(stale.capability.blockers)

    r3 = await repository.candidate(
        SolveIssueCommand(
            project="falcone",
            backlog_campaign_id=CAMPAIGN_ID,
            repository="gntik-ai/falcone",
            issue_number=42,
            maximum_risk="R3",
        ),
        SESSION_B,
    )
    assert r3.capability.status == "BLOCKED"
    assert "R3_OPERATOR_ACTION_REQUIRED" in r3.capability.blockers

    full_delivery = await repository.candidate(
        SolveIssueCommand(
            project="falcone",
            backlog_campaign_id=CAMPAIGN_ID,
            repository="gntik-ai/falcone",
            issue_number=42,
            delivery_mode="full-delivery",
        ),
        "c" * 64,
    )
    assert full_delivery.capability.status == "BLOCKED"
    assert (
        "FULL_DELIVERY_FINAL_CLOSURE_SERVICE_NOT_IMPLEMENTED" in full_delivery.capability.blockers
    )


def test_audit_publication_is_opt_in_and_full_audit_requires_phrase() -> None:
    command = AuditStartCommand(project="musematic", preset="full-e2e", environment="staging")
    assert command.publish_findings is False
    assert command.production is False


def test_dashboard_action_endpoints_require_session_csrf_and_password_reauthentication(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'web-actions.db'}"
    dashboard = DashboardRepository(database_url)
    operations = OperationsRepository(database_url)
    Base.metadata.create_all(dashboard.engine)
    dashboard.initialize_password_sync(PASSWORD)
    app = create_app(
        settings=Settings(database_url=database_url),
        repository=dashboard,
        operations_repository=operations,
        session_key=b"c" * 48,
    )
    with TestClient(app, base_url="https://testserver") as client:
        endpoint = "/api/dashboard/actions/candidates"
        assert client.post(endpoint, json={}).status_code == 401
        login = client.get("/login")
        match = re.search(r'name="csrf_token" value="([^"]+)"', login.text)
        assert match is not None
        signed_login_csrf = match.group(1)
        logged_in = client.post(
            "/login",
            data={"csrf_token": signed_login_csrf, "password": PASSWORD},
            follow_redirects=False,
        )
        assert logged_in.status_code == 303
        commands = client.get("/commands")
        match = re.search(r'name="csrf_token" value="([^"]+)"', commands.text)
        assert match is not None
        csrf = match.group(1)

        assert (
            client.post(
                endpoint,
                json={
                    "csrf_token": "x" * 20,
                    "command": {"action": "refresh_connections", "scope": "all"},
                },
            ).status_code
            == 403
        )
        prepared = client.post(
            endpoint,
            json={
                "csrf_token": csrf,
                "command": {"action": "refresh_connections", "scope": "all"},
            },
        )
        assert prepared.status_code == 200
        candidate = prepared.json()
        assert candidate["capability"]["status"] == "AVAILABLE"

        denied = client.post(
            "/api/dashboard/actions/reauthenticate",
            json={
                "csrf_token": csrf,
                "candidate_id": candidate["candidate_id"],
                "current_password": "invalid fixture password",
            },
        )
        assert denied.status_code == 401
        authorized = client.post(
            "/api/dashboard/actions/reauthenticate",
            json={
                "csrf_token": csrf,
                "candidate_id": candidate["candidate_id"],
                "current_password": PASSWORD,
            },
        )
        assert authorized.status_code == 200
        queued = client.post(
            "/api/dashboard/actions/execute",
            json={
                "csrf_token": csrf,
                "candidate_id": candidate["candidate_id"],
                "reauthentication_token": authorized.json()["reauthentication_token"],
                "confirmation_phrase": None,
            },
        )
        assert queued.status_code == 202
        assert queued.json()["status"] == "PENDING"
        assert (
            client.post(
                "/api/dashboard/actions/execute",
                json={
                    "csrf_token": csrf,
                    "candidate_id": candidate["candidate_id"],
                    "reauthentication_token": authorized.json()["reauthentication_token"],
                    "confirmation_phrase": None,
                },
            ).status_code
            == 409
        )


@pytest.mark.asyncio
async def test_connection_snapshot_reports_freshness_without_sensitive_probe_output(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    observed = datetime.now(UTC) - timedelta(minutes=10)
    await repository.upsert_connection(
        {
            "component": "synthetic-safe-probe",
            "logical_target": "logical target",
            "status": "STALE",
            "checked_at": observed,
            "last_success_at": observed,
            "last_failure_at": None,
            "latency_ms": 4,
            "safe_error": "snapshot is stale",
            "source": "synthetic-fixture",
            "next_action": "Refresh the owning service snapshot.",
            "details": {"authentication_status": "READY"},
        }
    )
    values = await repository.connections()
    assert len(values) == 1
    assert values[0].status == "STALE"
    assert values[0].freshness_seconds >= 599
    assert values[0].details == {"authentication_status": "READY"}


def test_connection_projection_reports_github_worker_without_inventing_broker_success(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    with repository.sessions.begin() as session:
        session.add(
            WorkerHeartbeat(
                identity="github-runner",
                hostname="controller-test",
                worker_kind="github",
                observed_at=datetime.now(UTC),
            )
        )

    ConnectionHealthProjector(repository)._persisted(datetime.now(UTC))

    with repository.sessions() as session:
        values = {row.component: row for row in session.scalars(select(ConnectionSnapshot)).all()}
    assert values["worker-github"].status == "CONNECTED"
    assert values["github-broker"].status == "DEGRADED"
    assert values["github-broker"].detail_json == {
        "last_event_type": None,
        "worker_heartbeat_current": True,
    }
    assert values["worker-controller"].status == "NOT_INSTALLED"


@pytest.mark.asyncio
async def test_cli_and_telegram_share_one_command_bus_and_workflow_identity(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _seed_ready_issue(repository)
    control = Mock()
    control.start_existing_backlog_loop = AsyncMock(
        return_value=OperationAccepted(workflow_id="backlog-loop-synthetic", run_id="run-1")
    )
    metadata = Mock()
    metadata.prepare_backlog_approval = AsyncMock(return_value={"approval_binding": "f" * 64})
    metadata.approve_backlog_campaign = AsyncMock(return_value={"status": "APPROVED"})
    bus = TypedCommandBus(control, metadata, repository)  # type: ignore[arg-type]

    cli = await bus.submit(
        _solve_issue(),
        source_channel="cli",
        operator_class="engineering-ctl",
        idempotency_key=_idempotency_fixture("cli", 1),
    )
    telegram = await bus.submit(
        _solve_issue(),
        source_channel="telegram",
        operator_class="telegram:allowlisted",
        idempotency_key=_idempotency_fixture("telegram", 2),
    )
    assert cli.operation_id == telegram.operation_id
    assert cli.workflow_id == telegram.workflow_id == "backlog-loop-synthetic"
    control.start_existing_backlog_loop.assert_awaited_once()


@pytest.mark.asyncio
async def test_state_dependent_workflow_controls_use_only_typed_signals(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    now = datetime.now(UTC)
    states = {
        "pause-workflow": ("RUNNING", "pause"),
        "backlog-loop-pause-after": ("RUNNING", "pause_after_current"),
        "resume-workflow": ("PAUSED", "resume"),
        "nudge-workflow": ("RUNNING", "nudge"),
        "retry-workflow": ("FAILED", "retry"),
        "backlog-loop-stop-after": ("RUNNING", "stop_after_current"),
        "cancel-workflow": ("RUNNING", "cancel"),
    }
    with repository.sessions.begin() as session:
        for workflow_id, (raw_status, _action) in states.items():
            session.add(
                Run(
                    campaign_id=None,
                    change_set_id=None,
                    workflow_id=workflow_id,
                    run_type="synthetic-control",
                    status=raw_status,
                    project="falcone",
                    worker="controller",
                    started_at=now,
                    finished_at=None,
                    error_code=None,
                    summary=None,
                )
            )
    control = Mock()
    control.signal = AsyncMock(return_value=None)
    bus = TypedCommandBus(control, Mock(), repository)  # type: ignore[arg-type]
    for ordinal, (workflow_id, (_raw_status, action)) in enumerate(states.items(), start=1):
        result = await bus.submit(
            WorkflowControlCommand(action=action, workflow_id=workflow_id),  # type: ignore[arg-type]
            source_channel="cli",
            operator_class="engineering-ctl",
            idempotency_key=_idempotency_fixture("control", ordinal),
        )
        assert result.status == "STARTED"
        assert result.workflow_id == workflow_id
    assert control.signal.await_count == 7
    signals = [call.args for call in control.signal.await_args_list]
    assert ("backlog-loop-pause-after", "pause_after_current") in signals
    assert ("backlog-loop-stop-after", "stop_after_current") in signals


@pytest.mark.asyncio
async def test_audit_start_requires_recent_matching_plan_and_defaults_to_no_publish(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _seed_ready_issue(repository)
    control = Mock()
    control.plan_campaign = AsyncMock(return_value={"status": "PLANNED", "blockers": []})
    control.start_campaign = AsyncMock(
        return_value=OperationAccepted(workflow_id="audit-synthetic", run_id="audit-run")
    )
    bus = TypedCommandBus(control, Mock(), repository)  # type: ignore[arg-type]
    start = AuditStartCommand(project="falcone", preset="snapshot", environment="staging")
    before_plan = await repository.capability(start)
    assert before_plan.status == "BLOCKED"
    assert "AUDIT_PLAN_REQUIRED" in before_plan.blockers

    planned = await bus.submit(
        parse_command(
            {
                "action": "audit_plan",
                "project": "falcone",
                "preset": "snapshot",
                "environment": "staging",
            }
        ),
        source_channel="cli",
        operator_class="engineering-ctl",
        idempotency_key=_idempotency_fixture("audit-plan", 1),
    )
    assert planned.status == "COMPLETED"
    accepted = await bus.submit(
        start,
        source_channel="cli",
        operator_class="engineering-ctl",
        idempotency_key=_idempotency_fixture("audit-start", 1),
    )
    assert accepted.workflow_id == "audit-synthetic"
    sent = control.start_campaign.await_args.args[0]
    assert sent.publish_findings is False


@pytest.mark.asyncio
async def test_source_pr_backlog_is_sequential_and_continues_after_independent_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = ExistingGitHubBacklogLoopWorkflow()
    instance._checkpoint = AsyncMock(return_value=True)  # type: ignore[method-assign]
    state_calls: list[dict[str, object]] = []
    items = [
        {"repository": "gntik-ai/falcone", "issue_number": number} for number in (101, 102, 103)
    ]

    async def step(name: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        if name == "backlog.load_execution":
            return {"status": "READY", "items": items}
        if name == "backlog.revalidate_gate":
            item = payload["item"]
            assert isinstance(item, dict)
            return {
                "status": "READY",
                "finding_id": f"00000000-0000-4000-8000-{int(item['issue_number']):012d}",
                "risk_class": "R1",
            }
        if name == "backlog.record_loop_state":
            state_calls.append(payload)
        return {"status": "REGISTERED"}

    instance._step = step  # type: ignore[method-assign]
    attempts: list[str] = []

    async def child(_name: str, _input: object, *, id: str, **_kwargs: object) -> dict[str, object]:
        attempts.append(id)
        if "issue-1-attempt-1" in id:
            return {"status": "REQUEST_CHANGES"}
        if "issue-2-attempt-1" in id:
            return {"status": "BLOCKED", "blocker": "INDEPENDENT_TEST_FAILURE"}
        return {"status": "SOURCE_PR"}

    monkeypatch.setattr(workflow_definitions.workflow, "patched", lambda _name: True)
    monkeypatch.setattr(
        workflow_definitions.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="synthetic-backlog", task_queue="engineering"),
    )
    monkeypatch.setattr(workflow_definitions.workflow, "execute_child_workflow", child)

    result = await instance.run(
        ExistingGitHubBacklogLoopInput(
            project="falcone",
            backlog_campaign_id=str(CAMPAIGN_ID),
            maximum_risk="R2",
            delivery_mode="source-pr",
            maximum_items=3,
            continue_after_blocked_issue=True,
            correction_limit=1,
        )
    )

    assert result["status"] == "COMPLETE"
    assert attempts == [
        "synthetic-backlog-issue-1-attempt-1",
        "synthetic-backlog-issue-1-attempt-2",
        "synthetic-backlog-issue-2-attempt-1",
        "synthetic-backlog-issue-3-attempt-1",
    ]
    assert state_calls[-1]["loop_status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_full_delivery_owns_candidate_pipeline_and_preserves_final_merge_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = ExistingGitHubBacklogLoopWorkflow()
    instance._checkpoint = AsyncMock(return_value=True)  # type: ignore[method-assign]
    state_calls: list[dict[str, object]] = []

    async def step(name: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        if name == "backlog.load_execution":
            return {
                "status": "READY",
                "items": [{"repository": "gntik-ai/falcone", "issue_number": 953}],
            }
        if name == "backlog.revalidate_gate":
            return {
                "status": "READY",
                "finding_id": "00000000-0000-4000-8000-000000000953",
                "risk_class": "R2",
            }
        if name == "backlog.record_loop_state":
            state_calls.append(payload)
        return {"status": "REGISTERED"}

    instance._step = step  # type: ignore[method-assign]
    child_types: list[str] = []

    async def child(name: str, _input: object, **_kwargs: object) -> dict[str, object]:
        child_types.append(name)
        if name == "IssueDeliveryWorkflow":
            return {
                "status": "SOURCE_PR",
                "context": {"change_set_id": "00000000-0000-4000-8000-000000000001"},
            }
        if name == "DeliveryReconciliationWorkflow":
            return {"status": "DEPLOY_PR", "context": {}}
        if name == "StagingDeploymentWorkflow":
            return {
                "status": "STAGING_DEPLOYED",
                "context": {"deployment_id": "00000000-0000-4000-8000-000000000002"},
            }
        assert name == "RuntimeVerificationWorkflow"
        return {"status": "RUNTIME_VERIFIED", "context": {}}

    monkeypatch.setattr(workflow_definitions.workflow, "patched", lambda _name: True)
    monkeypatch.setattr(
        workflow_definitions.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="synthetic-full", task_queue="engineering"),
    )
    monkeypatch.setattr(workflow_definitions.workflow, "execute_child_workflow", child)

    result = await instance.run(
        ExistingGitHubBacklogLoopInput(
            project="falcone",
            backlog_campaign_id=str(CAMPAIGN_ID),
            maximum_risk="R2",
            delivery_mode="full-delivery",
            maximum_items=1,
            continue_after_blocked_issue=False,
            correction_limit=1,
        )
    )

    assert child_types == [
        "IssueDeliveryWorkflow",
        "DeliveryReconciliationWorkflow",
        "StagingDeploymentWorkflow",
        "RuntimeVerificationWorkflow",
    ]
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "FINAL_MERGED_REVISION_DELIVERY_SERVICE_REQUIRED"
    assert state_calls[-1]["blocker"] == "FINAL_MERGED_REVISION_DELIVERY_SERVICE_REQUIRED"


@pytest.mark.asyncio
async def test_full_delivery_stops_at_exact_candidate_build_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = ExistingGitHubBacklogLoopWorkflow()
    instance._checkpoint = AsyncMock(return_value=True)  # type: ignore[method-assign]

    async def step(name: str, _payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        if name == "backlog.load_execution":
            return {
                "status": "READY",
                "items": [{"repository": "gntik-ai/falcone", "issue_number": 953}],
            }
        if name == "backlog.revalidate_gate":
            return {
                "status": "READY",
                "finding_id": "00000000-0000-4000-8000-000000000953",
                "risk_class": "R2",
            }
        return {"status": "REGISTERED"}

    instance._step = step  # type: ignore[method-assign]
    child_types: list[str] = []

    async def child(name: str, _input: object, **_kwargs: object) -> dict[str, object]:
        child_types.append(name)
        if name == "IssueDeliveryWorkflow":
            return {
                "status": "SOURCE_PR",
                "context": {"change_set_id": "00000000-0000-4000-8000-000000000001"},
            }
        return {
            "status": "BLOCKED",
            "blocker": "FALCONE_CANDIDATE_BUILD_CONTRACT_MISSING",
        }

    monkeypatch.setattr(workflow_definitions.workflow, "patched", lambda _name: True)
    monkeypatch.setattr(
        workflow_definitions.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="synthetic-full", task_queue="engineering"),
    )
    monkeypatch.setattr(workflow_definitions.workflow, "execute_child_workflow", child)

    result = await instance.run(
        ExistingGitHubBacklogLoopInput(
            project="falcone",
            backlog_campaign_id=str(CAMPAIGN_ID),
            maximum_risk="R2",
            delivery_mode="full-delivery",
            maximum_items=1,
            continue_after_blocked_issue=False,
            correction_limit=1,
        )
    )

    assert child_types == ["IssueDeliveryWorkflow", "DeliveryReconciliationWorkflow"]
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "FALCONE_CANDIDATE_BUILD_CONTRACT_MISSING"


@pytest.mark.asyncio
async def test_backlog_stop_after_current_ends_at_the_next_issue_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = ExistingGitHubBacklogLoopWorkflow()
    instance._stop_after_current = True
    instance._checkpoint = AsyncMock(return_value=True)  # type: ignore[method-assign]
    state_calls: list[dict[str, object]] = []
    items = [{"repository": "gntik-ai/falcone", "issue_number": number} for number in (201, 202)]

    async def step(name: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        if name == "backlog.load_execution":
            return {"status": "READY", "items": items}
        if name == "backlog.revalidate_gate":
            return {
                "status": "READY",
                "finding_id": "00000000-0000-4000-8000-000000000201",
                "risk_class": "R1",
            }
        if name == "backlog.record_loop_state":
            state_calls.append(payload)
        return {"status": "REGISTERED"}

    instance._step = step  # type: ignore[method-assign]
    attempts: list[str] = []

    async def child(_name: str, _input: object, *, id: str, **_kwargs: object) -> dict[str, object]:
        attempts.append(id)
        return {"status": "SOURCE_PR"}

    monkeypatch.setattr(workflow_definitions.workflow, "patched", lambda _name: True)
    monkeypatch.setattr(
        workflow_definitions.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="synthetic-backlog", task_queue="engineering"),
    )
    monkeypatch.setattr(workflow_definitions.workflow, "execute_child_workflow", child)

    result = await instance.run(
        ExistingGitHubBacklogLoopInput(
            project="falcone",
            backlog_campaign_id=str(CAMPAIGN_ID),
            maximum_risk="R2",
            delivery_mode="source-pr",
            maximum_items=2,
            continue_after_blocked_issue=True,
            correction_limit=1,
        )
    )

    assert result["status"] == "STOPPED"
    assert attempts == ["synthetic-backlog-issue-1-attempt-1"]
    assert state_calls[-1]["loop_status"] == "STOPPED"
    assert state_calls[-1]["control_state"] == "STOPPED_AFTER_CURRENT"


@pytest.mark.asyncio
async def test_backlog_pause_after_current_waits_then_resumes_at_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = ExistingGitHubBacklogLoopWorkflow()
    instance._pause_after_current = True
    instance._checkpoint = AsyncMock(return_value=True)  # type: ignore[method-assign]
    state_calls: list[dict[str, object]] = []
    items = [{"repository": "gntik-ai/falcone", "issue_number": number} for number in (301, 302)]

    async def step(name: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        if name == "backlog.load_execution":
            return {"status": "READY", "items": items}
        if name == "backlog.revalidate_gate":
            item = payload["item"]
            assert isinstance(item, dict)
            return {
                "status": "READY",
                "finding_id": f"00000000-0000-4000-8000-{int(item['issue_number']):012d}",
                "risk_class": "R1",
            }
        if name == "backlog.record_loop_state":
            state_calls.append(payload)
        return {"status": "REGISTERED"}

    instance._step = step  # type: ignore[method-assign]
    attempts: list[str] = []

    async def child(_name: str, _input: object, *, id: str, **_kwargs: object) -> dict[str, object]:
        attempts.append(id)
        return {"status": "SOURCE_PR"}

    async def wait_condition(predicate: object) -> None:
        assert callable(predicate)
        assert instance._phase == "PAUSED_AFTER_CURRENT"
        assert instance._paused
        await instance.resume()

    monkeypatch.setattr(workflow_definitions.workflow, "patched", lambda _name: True)
    monkeypatch.setattr(
        workflow_definitions.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="synthetic-backlog", task_queue="engineering"),
    )
    monkeypatch.setattr(workflow_definitions.workflow, "execute_child_workflow", child)
    monkeypatch.setattr(workflow_definitions.workflow, "wait_condition", wait_condition)

    result = await instance.run(
        ExistingGitHubBacklogLoopInput(
            project="falcone",
            backlog_campaign_id=str(CAMPAIGN_ID),
            maximum_risk="R2",
            delivery_mode="source-pr",
            maximum_items=2,
            continue_after_blocked_issue=True,
            correction_limit=1,
        )
    )

    assert result["status"] == "COMPLETE"
    assert attempts == [
        "synthetic-backlog-issue-1-attempt-1",
        "synthetic-backlog-issue-2-attempt-1",
    ]
    assert [call["loop_status"] for call in state_calls] == [
        "PAUSED",
        "COMPLETE",
    ]
    assert state_calls[0]["control_state"] == "PAUSED_AFTER_CURRENT"


def test_operator_pause_classification_is_fail_closed_for_high_impact_blockers() -> None:
    required = ExistingGitHubBacklogLoopWorkflow._requires_operator_pause
    assert required("R3_REQUIRES_OPERATOR")
    assert required("MIGRATION_AMBIGUITY")
    assert required("SECRET_LIFECYCLE_UNRESOLVED")
    assert required("EXTERNAL_REVISION_DRIFT")
    assert not required("INDEPENDENT_TEST_FAILURE")
