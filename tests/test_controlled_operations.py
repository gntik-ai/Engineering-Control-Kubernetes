from __future__ import annotations

import re
import secrets
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from engineering_control.api.schemas import MultiProjectAuditStart, OperationAccepted
from engineering_control.config import Settings
from engineering_control.dashboard.app import create_app
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.dashboard.session_store import SessionAuthorization
from engineering_control.db.base import Base
from engineering_control.db.models import (
    BacklogIssueRecord,
    Campaign,
    ConnectionSnapshot,
    CoverageCell,
    DashboardEvent,
    Enhancement,
    GitHubBacklogCampaign,
    OperationExport,
    OperationRequest,
    QuotaSnapshot,
    ReauthenticationBinding,
    Run,
    WorkerHeartbeat,
)
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.models.enums import Project
from engineering_control.operations.connections import ConnectionHealthProjector
from engineering_control.operations.models import (
    AuditAllProjectsCommand,
    AuditStartCommand,
    EnhancementApproveCommand,
    EnhancementRejectCommand,
    EnhancementRunCommand,
    EvidenceExportCampaignCommand,
    EvidenceExportWorkflowCommand,
    RefreshConnectionsCommand,
    SolveBacklogCommand,
    SolveIssueCommand,
    WorkflowControlCommand,
    parse_command,
)
from engineering_control.operations.registry import COMMAND_REGISTRY
from engineering_control.operations.repository import (
    OperationsRepository,
    _operation_integrity_conflict,
)
from engineering_control.operations.service import TypedCommandBus
from engineering_control.workflows import definitions as workflow_definitions
from engineering_control.workflows.definitions import (
    ExistingGitHubBacklogLoopWorkflow,
    MultiProjectAuditWorkflow,
)
from engineering_control.workflows.inputs import (
    ExistingGitHubBacklogLoopInput,
    MultiProjectAuditInput,
)

CAMPAIGN_ID = UUID("11111111-1111-4111-8111-111111111111")
AUDIT_CAMPAIGN_ID = UUID("22222222-2222-4222-8222-222222222222")
SESSION_A = "a" * 64
SESSION_B = "b" * 64
PASSWORD = "bounded dashboard fixture password"  # noqa: S105
PROJECT_CONFIG_ROOT = Path(__file__).resolve().parents[1] / "config" / "projects"


async def _rotate_candidate(repository, candidate_id, previous_binding):
    """Use the real rotating repository path with explicit synthetic session proof."""
    dashboard = DashboardRepository(str(repository.engine.url))
    try:
        version = await dashboard.authentication_version()
        if version is None:
            version = await dashboard.initialize_password(PASSWORD)
        expiry = datetime.now(UTC) + timedelta(hours=12)
        previous = SessionAuthorization(previous_binding, version, expiry)
        replacement = SessionAuthorization(secrets.token_hex(32), version, expiry)
        token, _expires_at = await repository.rotate_reauthentication(
            candidate_id, previous, replacement.binding_hash
        )
        return token, replacement
    finally:
        dashboard.engine.dispose()


def _repository(tmp_path: Path) -> OperationsRepository:
    config_root = tmp_path / "projects.d"
    config_root.mkdir()
    for source in PROJECT_CONFIG_ROOT.glob("*.yaml"):
        destination = config_root / source.name
        shutil.copyfile(source, destination)
        destination.chmod(0o640)
    repository = OperationsRepository(
        f"sqlite+pysqlite:///{tmp_path / 'operations.db'}",
        project_config_root=config_root,
    )
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


def test_operation_integrity_conflict_classifier_is_constraint_specific() -> None:
    class PostgreSQLDiagnostic:
        constraint_name = "uq_operation_requests_idempotency_key"

    class PostgreSQLUniqueViolation(Exception):
        diag = PostgreSQLDiagnostic()

    active_owner = IntegrityError(
        "INSERT",
        {},
        sqlite3.IntegrityError(
            "UNIQUE constraint failed: operation_requests.action, operation_requests.target_id"
        ),
    )
    idempotency = IntegrityError(
        "INSERT",
        {},
        sqlite3.IntegrityError("UNIQUE constraint failed: operation_requests.idempotency_key"),
    )
    unrelated = IntegrityError(
        "INSERT",
        {},
        sqlite3.IntegrityError("FOREIGN KEY constraint failed"),
    )
    assert _operation_integrity_conflict(active_owner) == "active_owner"
    assert _operation_integrity_conflict(idempotency) == "idempotency"
    assert (
        _operation_integrity_conflict(
            IntegrityError("INSERT", {}, PostgreSQLUniqueViolation("duplicate key"))
        )
        == "idempotency"
    )
    assert _operation_integrity_conflict(unrelated) is None


def _seed_enhancement(repository: OperationsRepository, *, ordinal: int = 1) -> str:
    key = f"enh-{ordinal:032x}"
    with repository.sessions.begin() as session:
        session.add(
            Enhancement(
                enhancement_key=key,
                project="falcone",
                campaign_id=str(CAMPAIGN_ID),
                persona="operator",
                affected_repository="gntik-ai/falcone",
                risk_class="R1",
                status="CANDIDATE_ENHANCEMENT",
                fingerprint=f"sha256:{ordinal:064x}",
                approval_binding_sha256=f"{ordinal:064x}",
                payload_json={"summary": "bounded synthetic enhancement"},
            )
        )
    return key


def _seed_export_targets(repository: OperationsRepository) -> str:
    now = datetime.now(UTC)
    workflow_id = "audit-falcone-export-fixture"
    with repository.sessions.begin() as session:
        session.add(
            Campaign(
                id=AUDIT_CAMPAIGN_ID,
                project="falcone",
                name="bounded export fixture",
                status="COMPLETE",
                source_sha="a" * 40,
                deployment_sha="b" * 40,
                required_cells=2,
                metadata_json={
                    "root_blockers": {
                        "rb-safe": {
                            "code": "DEPENDENCY_UNAVAILABLE",
                            "summary": "Authorization: Bearer must-not-persist",
                            "resolution_state": "open",
                        }
                    }
                },
            )
        )
        session.add_all(
            [
                CoverageCell(
                    campaign_id=AUDIT_CAMPAIGN_ID,
                    repository_surface="source",
                    domain="api",
                    persona="operator",
                    flow="repository-static",
                    lens="correctness",
                    environment="staging",
                    status="PASS",
                    blocker=None,
                    exclusion_approval_id=None,
                    evidence_json=[{"raw": "not exported"}],
                    result_json={"password": "must-not-persist"},
                ),
                CoverageCell(
                    campaign_id=AUDIT_CAMPAIGN_ID,
                    repository_surface="deployment",
                    domain="runtime",
                    persona="viewer",
                    flow="e2e",
                    lens="security",
                    environment="staging",
                    status="BLOCKED",
                    blocker="DEPENDENCY_UNAVAILABLE",
                    exclusion_approval_id=None,
                    evidence_json=[],
                    result_json={},
                ),
            ]
        )
        session.add(
            Run(
                campaign_id=AUDIT_CAMPAIGN_ID,
                change_set_id=None,
                workflow_id=workflow_id,
                run_type="audit",
                status="COMPLETE",
                project="falcone",
                worker="controller",
                started_at=now - timedelta(minutes=2),
                finished_at=now,
                error_code=None,
                summary="cookie=session-secret-value",
            )
        )
    return workflow_id


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
    with pytest.raises(ValidationError):
        EnhancementRejectCommand(
            project="falcone",
            enhancement_id="enh-" + "1" * 32,
            reason_code="arbitrary operator text",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        EnhancementRunCommand(
            project="falcone",
            enhancement_id="enh-" + "1" * 32,
            risk="R1",
            execution_mode="source-pr",
            dry_run=True,
        )
    with pytest.raises(ValidationError):
        parse_command(
            {
                "action": "evidence_export_workflow",
                "workflow_id": "safe-workflow",
                "format": "zip",
            }
        )
    with pytest.raises(ValidationError):
        parse_command(
            {
                "action": "evidence_export_campaign",
                "campaign_id": str(AUDIT_CAMPAIGN_ID),
                "path": "/outside/arbitrary",
            }
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

    token, authorization = await _rotate_candidate(repository, candidate.candidate_id, SESSION_A)
    operation = await repository.queue_candidate(
        candidate.candidate_id,
        token,
        authorization.binding_hash,
        None,
        authorization=authorization,
    )
    assert operation.source_channel == "dashboard"
    assert operation.status == "PENDING"
    repeated = await repository.queue_candidate(
        candidate.candidate_id,
        token,
        authorization.binding_hash,
        None,
        authorization=authorization,
    )
    assert repeated.operation_id == operation.operation_id
    with repository.sessions() as session:
        binding = session.scalar(select(ReauthenticationBinding))
        assert binding is not None and binding.used_at is not None
        assert token not in binding.token_digest


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", ["body", "risk", "source_head"])
async def test_issue_confirmation_rejects_changed_approved_snapshot(
    tmp_path: Path, changed_field: str
) -> None:
    repository = _repository(tmp_path)
    _seed_ready_issue(repository)
    candidate = await repository.candidate(_solve_issue(), SESSION_A)
    token, authorization = await _rotate_candidate(repository, candidate.candidate_id, SESSION_A)
    with repository.sessions.begin() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        campaign = session.get(GitHubBacklogCampaign, CAMPAIGN_ID)
        assert issue is not None and campaign is not None
        if changed_field == "body":
            issue.body_sha256 = "9" * 64
        elif changed_field == "risk":
            issue.calculated_risk = "R3"
        else:
            campaign.source_head_at_import = "9" * 40
    with pytest.raises(ValueError, match="approval snapshot changed"):
        await repository.queue_candidate(
            candidate.candidate_id,
            token,
            authorization.binding_hash,
            None,
            authorization=authorization,
        )
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(OperationRequest)) == 0
        binding = session.scalar(select(ReauthenticationBinding))
        assert binding is not None and binding.used_at is None


@pytest.mark.asyncio
async def test_issue_approval_is_revalidated_after_queue_before_dispatch(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _seed_ready_issue(repository)
    command = _solve_issue()
    candidate = await repository.candidate(command, SESSION_A)
    token, authorization = await _rotate_candidate(repository, candidate.candidate_id, SESSION_A)
    operation = await repository.queue_candidate(
        candidate.candidate_id, token, authorization.binding_hash, None, authorization=authorization
    )
    await repository.validate_issue_approval(operation.operation_id, command)
    with repository.sessions.begin() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert issue is not None
        issue.body_sha256 = "9" * 64
    control = Mock()
    control.start_existing_backlog_loop = AsyncMock()
    bus = TypedCommandBus(control, Mock(), repository)
    assert await bus.dispatch_pending()
    current = await repository.operation(operation.operation_id)
    assert current is not None and current.status == "BLOCKED"
    assert current.safe_error is not None and "approval snapshot changed" in current.safe_error
    control.start_existing_backlog_loop.assert_not_awaited()


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
    token, authorization = await _rotate_candidate(repository, candidate.candidate_id, SESSION_A)
    with pytest.raises(ValueError, match="typed confirmation"):
        await repository.queue_candidate(
            candidate.candidate_id,
            token,
            authorization.binding_hash,
            "WRONG",
            authorization=authorization,
        )

    # A failed phrase does not consume the one-use reauthentication binding.
    first = await repository.queue_candidate(
        candidate.candidate_id,
        token,
        authorization.binding_hash,
        candidate.confirmation_phrase,
        authorization=authorization,
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
    first_token, first_auth = await _rotate_candidate(
        repository, first_candidate.candidate_id, SESSION_A
    )
    first = await repository.queue_candidate(
        first_candidate.candidate_id,
        first_token,
        first_auth.binding_hash,
        None,
        authorization=first_auth,
    )
    await repository.finish(first.operation_id, status="COMPLETED")

    second_candidate = await repository.candidate(command, first_auth.binding_hash)
    second_token, second_auth = await _rotate_candidate(
        repository, second_candidate.candidate_id, first_auth.binding_hash
    )
    second = await repository.queue_candidate(
        second_candidate.candidate_id,
        second_token,
        second_auth.binding_hash,
        None,
        authorization=second_auth,
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
    providers = await repository.operator_snapshot("providers")
    assert providers["status"] == "DEGRADED"
    assert {value["status"] for value in providers["providers"].values()} == {"STALE"}
    assert all(value["freshness_seconds"] >= 599 for value in providers["providers"].values())

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
    assert "FULL_DELIVERY_FINAL_CLOSURE_SERVICE_NOT_IMPLEMENTED" not in (
        full_delivery.capability.blockers
    )
    assert "FALCONE_CANDIDATE_BUILD_CONTRACT_MISSING" in full_delivery.capability.blockers
    assert (
        "FALCONE_SCOPED_STAGING_RELEASER_AND_REPOSITORY_VALUES_MISSING"
        in full_delivery.capability.blockers
    )


def test_full_delivery_capability_uses_project_contracts_and_fails_closed(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    assert set(repository._full_delivery_blockers("musematic")) == {
        "MUSEMATIC_CANDIDATE_BUILD_CONTRACT_MISSING",
        "MUSEMATIC_DIGEST_RENDERING_UNSUPPORTED",
        "MUSEMATIC_DISCOVERED_TARGET_IS_PRODUCTION_DEGRADED_AND_OUT_OF_SYNC",
    }
    assert set(repository._full_delivery_blockers("llmwiki")) == {
        "LLMWIKI_CANDIDATE_BUILD_DISABLED",
        "LLMWIKI_STAGING_BOOTSTRAP_APPROVAL_REQUIRED",
        "LLMWIKI_FIRST_STAGE_BOOTSTRAP_NOT_APPROVED",
    }

    unconfigured = OperationsRepository(
        f"sqlite+pysqlite:///{tmp_path / 'unconfigured-operations.db'}"
    )
    assert unconfigured._full_delivery_blockers("falcone") == [
        "FULL_DELIVERY_PROJECT_CONTRACT_NOT_CONFIGURED",
    ]


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
    with TestClient(
        app,
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    ) as client:
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
        assert authorized.json()["session_rotated"] is True
        assert authorized.json()["csrf_token"] != csrf
        csrf = authorized.json()["csrf_token"]
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
        repeated = client.post(
            "/api/dashboard/actions/execute",
            json={
                "csrf_token": csrf,
                "candidate_id": candidate["candidate_id"],
                "reauthentication_token": authorized.json()["reauthentication_token"],
                "confirmation_phrase": None,
            },
        )
        assert repeated.status_code == 202
        assert repeated.json()["operation_id"] == queued.json()["operation_id"]


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


@pytest.mark.asyncio
async def test_terminal_zero_and_blocked_campaigns_have_truthful_coverage_status(
    tmp_path: Path,
) -> None:
    operations = _repository(tmp_path)
    now = datetime.now(UTC)
    empty = Campaign(
        project="falcone",
        name="empty-terminal",
        status="COMPLETED",
        required_cells=0,
        metadata_json={},
        source_sha=None,
        deployment_sha=None,
        created_at=now,
        updated_at=now,
    )
    blocked = Campaign(
        project="falcone",
        name="blocked-terminal",
        status="COMPLETED",
        required_cells=1,
        metadata_json={},
        source_sha=None,
        deployment_sha=None,
        created_at=now - timedelta(seconds=1),
        updated_at=now,
    )
    inconsistent = Campaign(
        project="llmwiki",
        name="terminal-with-active-cell",
        status="COMPLETED",
        required_cells=1,
        metadata_json={},
        source_sha=None,
        deployment_sha=None,
        created_at=now - timedelta(seconds=2),
        updated_at=now,
    )
    with operations.sessions.begin() as session:
        session.add_all([empty, blocked, inconsistent])
        session.flush()
        session.add(
            CoverageCell(
                campaign_id=blocked.id,
                repository_surface="source",
                domain="api",
                persona="operator",
                flow="bounded-fixture",
                lens="functional",
                environment="staging",
                status="BLOCKED",
                blocker="FIXTURE_DEPENDENCY",
                exclusion_approval_id=None,
                evidence_json=[],
                result_json={},
            )
        )
        session.add(
            CoverageCell(
                campaign_id=inconsistent.id,
                repository_surface="docs",
                domain="publication",
                persona="operator",
                flow="bounded-fixture",
                lens="functional",
                environment="staging",
                status="IN_PROGRESS",
                blocker=None,
                exclusion_approval_id=None,
                evidence_json=[],
                result_json={},
            )
        )

    metadata = SQLMetadataRepository(str(operations.engine.url))
    values = {value["name"]: value for value in await metadata.list_campaigns(10)}
    assert values["empty-terminal"]["execution_status"] == "COMPLETED"
    assert values["empty-terminal"]["coverage_outcome"] == "NOT_RUN"
    assert values["empty-terminal"]["status"] == "NOT_RUN"
    assert values["blocked-terminal"]["coverage_outcome"] == "BLOCKED"
    assert values["blocked-terminal"]["status"] == "BLOCKED"
    assert values["terminal-with-active-cell"]["execution_status"] == "COMPLETED"
    assert values["terminal-with-active-cell"]["coverage_outcome"] == "IN_PROGRESS"
    assert values["terminal-with-active-cell"]["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_connection_reads_expire_green_snapshots_and_cli_is_not_a_connection(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    old = datetime.now(UTC) - timedelta(minutes=10)
    for component in ("engineering-api", "temporal"):
        await repository.upsert_connection(
            {
                "component": component,
                "logical_target": component,
                "status": "CONNECTED",
                "checked_at": old,
                "last_success_at": old,
                "last_failure_at": None,
                "latency_ms": 1,
                "safe_error": None,
                "source": "stale-green-fixture",
                "next_action": None,
                "details": {},
            }
        )
    await repository.upsert_connection(
        {
            "component": "engineering-cli",
            "logical_target": "/opt/engineering-control/venv/bin/engineering-ctl",
            "status": "PRESENT",
            "checked_at": old,
            "last_success_at": None,
            "last_failure_at": None,
            "latency_ms": None,
            "safe_error": None,
            "source": "fixed-path-metadata",
            "next_action": None,
            "details": {"api_compatibility": "v1", "kind": "client"},
        }
    )

    values = {value.component: value for value in await repository.connections()}
    assert values["engineering-api"].status == "STALE"
    assert values["temporal"].status == "STALE"
    assert values["engineering-api"].safe_error == (
        "persisted connection snapshot exceeded its freshness limit"
    )
    assert values["engineering-api"].next_action == (
        "Refresh the owning fixed probe or persisted publisher."
    )
    assert values["engineering-cli"].status == "PRESENT"
    readiness = await repository.operator_snapshot("readiness-all")
    assert readiness["connection_count"] == 2
    assert readiness["connected_count"] == 0
    assert readiness["status"] == "DEGRADED"


def test_gateway_delivery_failure_is_never_projected_as_success(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    now = datetime.now(UTC)
    with repository.sessions.begin() as session:
        session.add_all(
            [
                DashboardEvent(
                    occurred_at=now - timedelta(seconds=2),
                    level="INFO",
                    event_type="telegram.response_delivered",
                    source="hermes",
                    host=None,
                    worker_kind=None,
                    project=None,
                    workflow_id=None,
                    temporal_run_id=None,
                    issue_number=None,
                    campaign_id=None,
                    entity_type=None,
                    entity_id=None,
                    current_phase=None,
                    message="Telegram response delivered",
                    metadata_json={},
                ),
                DashboardEvent(
                    occurred_at=now - timedelta(seconds=1),
                    level="ERROR",
                    event_type="telegram.response_delivery_failed",
                    source="hermes",
                    host=None,
                    worker_kind=None,
                    project=None,
                    workflow_id=None,
                    temporal_run_id=None,
                    issue_number=None,
                    campaign_id=None,
                    entity_type=None,
                    entity_id=None,
                    current_phase=None,
                    message="Telegram response delivery failed",
                    metadata_json={},
                ),
            ]
        )

    event_types = [event.event_type for event in repository.health_sources()["events"]]
    assert event_types[:2] == [
        "telegram.response_delivery_failed",
        "telegram.response_delivered",
    ]
    ConnectionHealthProjector(repository)._persisted(now)
    values = {value.component: value for value in repository._connections()}
    for component in ("hermes-gateway", "telegram"):
        assert values[component].status == "DEGRADED"
        assert values[component].last_success_at is not None
        assert values[component].last_failure_at is not None
        assert values[component].last_success_at.replace(tzinfo=UTC) == now - timedelta(seconds=2)
        assert values[component].last_failure_at.replace(tzinfo=UTC) == now - timedelta(seconds=1)
        assert values[component].details["last_event_type"] == ("telegram.response_delivery_failed")
    assert values["engineering-cli"].status == "PRESENT"

    channel = repository._operator_snapshot("channel")
    assert channel["status"] == "DEGRADED"
    assert channel["last_safe_error"] == "Telegram response delivery failed"


@pytest.mark.asyncio
async def test_readiness_scopes_exclude_only_non_runtime_sentinels_from_core(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    now = datetime.now(UTC)
    for component, status in (
        ("engineering-api", "CONNECTED"),
        ("temporal", "CONNECTED"),
        ("worker-controller", "NOT_INSTALLED"),
        ("project-falcone", "STALE"),
        ("project-musematic", "NOT_CONFIGURED"),
    ):
        await repository.upsert_connection(
            {
                "component": component,
                "logical_target": component,
                "status": status,
                "checked_at": now,
                "last_success_at": now if status == "CONNECTED" else None,
                "last_failure_at": None,
                "latency_ms": None,
                "safe_error": None,
                "source": "readiness-scope-regression",
                "next_action": None,
                "details": {},
            }
        )

    core = await repository.operator_snapshot("readiness-core")
    all_connections = await repository.operator_snapshot("readiness-all")

    assert core["status"] == "READY"
    assert core["connection_count"] == 2
    assert core["connected_count"] == 2
    assert all_connections["status"] == "DEGRADED"
    assert all_connections["connection_count"] == 4
    assert all_connections["connected_count"] == 2


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
async def test_bounded_evidence_exports_are_typed_redacted_expiring_and_cross_channel(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    workflow_id = _seed_export_targets(repository)
    bus = TypedCommandBus(Mock(), Mock(), repository)  # type: ignore[arg-type]

    campaign_command = EvidenceExportCampaignCommand(campaign_id=AUDIT_CAMPAIGN_ID)
    campaign_candidate = await repository.candidate(campaign_command, SESSION_A)
    assert campaign_candidate.capability.status == "AVAILABLE"
    assert campaign_candidate.project == "falcone"
    assert campaign_candidate.operation_binding["project"] == "falcone"
    assert campaign_candidate.operation_binding["resolved"]["coverage_cells"] == 2
    campaign_operation = await bus.submit(
        campaign_command,
        source_channel="cli",
        operator_class="engineering-ctl:hermes",
        idempotency_key=_idempotency_fixture("campaign-export", 1),
    )
    assert campaign_operation.status == "COMPLETED"
    assert campaign_operation.result_available is True
    campaign_export = await repository.export_content(campaign_operation.operation_id)
    assert campaign_export is not None
    assert campaign_export["campaign"]["campaign_id"] == str(AUDIT_CAMPAIGN_ID)
    assert campaign_export["coverage"]["status_counts"] == {"BLOCKED": 1, "PASS": 1}
    assert "raw coverage results" in campaign_export["exclusions"]
    serialized = str(campaign_export)
    assert "must-not-persist" not in serialized
    assert "not exported" not in serialized

    workflow_operation = await bus.submit(
        EvidenceExportWorkflowCommand(workflow_id=workflow_id),
        source_channel="telegram",
        operator_class="telegram:allowlisted",
        idempotency_key=_idempotency_fixture("workflow-export", 1),
    )
    assert workflow_operation.status == "COMPLETED"
    workflow_export = await repository.export_content(workflow_operation.operation_id)
    assert workflow_export is not None
    assert workflow_export["workflow"]["workflow_id"] == workflow_id
    assert "agent transcript content" in workflow_export["exclusions"]
    assert "session-secret-value" not in str(workflow_export)

    with repository.sessions.begin() as session:
        exports = list(session.scalars(select(OperationExport)).all())
        assert len(exports) == 2
        assert all(row.size_bytes <= repository.export_max_bytes for row in exports)
        assert "must-not-persist" not in str(exports[0].content_json)
        operations = list(
            session.scalars(
                select(OperationRequest).where(
                    OperationRequest.id.in_(
                        [campaign_operation.operation_id, workflow_operation.operation_id]
                    )
                )
            ).all()
        )
        assert {row.project for row in operations} == {"falcone"}
        exports[0].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await repository.export_content(campaign_operation.operation_id) is None
    expired_operation = await repository.operation(campaign_operation.operation_id)
    assert expired_operation is not None
    assert expired_operation.result_available is False

    with repository.sessions.begin() as session:
        workflow_row = session.scalar(
            select(OperationExport).where(
                OperationExport.operation_id == workflow_operation.operation_id
            )
        )
        assert workflow_row is not None
        workflow_row.content_json = {"tampered": True}
    assert await repository.export_content(workflow_operation.operation_id) is None
    tampered_operation = await repository.operation(workflow_operation.operation_id)
    assert tampered_operation is not None
    assert tampered_operation.result_available is False

    registry = {item["action"]: item for item in COMMAND_REGISTRY}
    for action in ("evidence_export_campaign", "evidence_export_workflow"):
        assert registry[action]["status"] == "AVAILABLE"
        assert not any(
            field["name"] in {"command", "args", "argv", "shell", "path"}
            for field in registry[action]["fields"]
        )


@pytest.mark.asyncio
async def test_evidence_export_truncates_collection_prefixes_to_configured_size(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'bounded-export.sqlite'}"
    repository = OperationsRepository(database_url, export_max_bytes=65_536)
    Base.metadata.create_all(repository.engine)
    _seed_export_targets(repository)
    with repository.sessions.begin() as session:
        campaign = session.get(Campaign, AUDIT_CAMPAIGN_ID)
        assert campaign is not None
        campaign.metadata_json = {
            "root_blockers": {
                f"root-{index:03d}": {
                    "code": "BOUNDED_EXPORT_TEST",
                    "summary": "x" * 500,
                    "resolution_state": "open",
                }
                for index in range(200)
            }
        }
    bus = TypedCommandBus(Mock(), Mock(), repository)  # type: ignore[arg-type]
    operation = await bus.submit(
        EvidenceExportCampaignCommand(campaign_id=AUDIT_CAMPAIGN_ID),
        source_channel="cli",
        operator_class="engineering-ctl:hermes",
        idempotency_key=_idempotency_fixture("oversized-campaign-export", 1),
    )
    assert operation.status == "COMPLETED"
    export = await repository.export_content(operation.operation_id)
    assert export is not None
    assert export["truncated"]["root_blockers"] is True
    assert export["collection_counts"]["root_blockers"]["available"] == 200
    assert export["collection_counts"]["root_blockers"]["included"] < 200
    assert len(repository._encode_export(export)) <= repository.export_max_bytes
    with repository.sessions() as session:
        assert session.scalar(select(func.count(OperationExport.id))) == 1


@pytest.mark.asyncio
async def test_campaign_reconciliation_candidate_page_is_bounded(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with repository.sessions.begin() as session:
        for ordinal in range(3):
            session.add(
                Campaign(
                    id=UUID(f"00000000-0000-4000-8000-{ordinal + 1:012d}"),
                    project="falcone",
                    name=f"reconciliation-{ordinal}",
                    status="PLANNED",
                    source_sha="a" * 40,
                    deployment_sha="b" * 40,
                    required_cells=1,
                    metadata_json={},
                )
            )

    first = await repository.campaign_reconciliation_candidates(limit=2, offset=0)
    second = await repository.campaign_reconciliation_candidates(limit=2, offset=2)

    assert len(first) == 2
    assert len(second) == 1
    with pytest.raises(ValueError, match="outside its safety bound"):
        await repository.campaign_reconciliation_candidates(limit=202, offset=0)


@pytest.mark.asyncio
async def test_enhancement_actions_use_immutable_binding_and_typed_services(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _seed_ready_issue(repository)
    enhancement_id = _seed_enhancement(repository)
    rejected_id = _seed_enhancement(repository, ordinal=2)
    metadata = SQLMetadataRepository(str(repository.engine.url))
    control = Mock()
    control.start_enhancement_solution = AsyncMock(
        return_value=OperationAccepted(
            workflow_id="enhance-solution-synthetic",
            run_id="enhance-run-1",
        )
    )
    bus = TypedCommandBus(control, metadata, repository)  # type: ignore[arg-type]

    approve = EnhancementApproveCommand(
        project="falcone",
        enhancement_id=enhancement_id,
    )
    candidate = await repository.candidate(approve, SESSION_A)
    assert candidate.capability.status == "AVAILABLE"
    assert candidate.confirmation_phrase is not None
    assert candidate.operation_binding["payload"]["approval_binding"] == "1".zfill(64)
    assert candidate.operation_binding["resolved"] == {
        "enhancement_id": enhancement_id,
        "project": "falcone",
        "campaign_id": str(CAMPAIGN_ID),
        "persona": "operator",
        "affected_repository": "gntik-ai/falcone",
        "risk": "R1",
        "status": "CANDIDATE_ENHANCEMENT",
        "fingerprint": "sha256:" + "1".zfill(64),
        "approval_binding_sha256": "1".zfill(64),
    }
    changed = await repository.capability(
        EnhancementApproveCommand(
            project="falcone",
            enhancement_id=enhancement_id,
            approval_binding="f" * 64,
        )
    )
    assert changed.status == "BLOCKED"
    assert "ENHANCEMENT_APPROVAL_BINDING_CHANGED" in changed.blockers
    approved = await bus.submit(
        approve,
        source_channel="cli",
        operator_class="engineering-ctl:hermes",
        idempotency_key=_idempotency_fixture("enhancement-approve", 1),
    )
    assert approved.status == "COMPLETED"
    approved_record = await metadata.get_enhancement(enhancement_id)
    assert approved_record is not None
    assert approved_record["status"] == "APPROVED"

    run = EnhancementRunCommand(
        project="falcone",
        enhancement_id=enhancement_id,
        risk="R2",
        execution_mode="source-pr",
        dry_run=False,
    )
    run_candidate = await repository.candidate(run, SESSION_A)
    assert run_candidate.capability.status == "AVAILABLE"
    assert run_candidate.confirmation_phrase is not None
    started = await bus.submit(
        run,
        source_channel="telegram",
        operator_class="telegram:allowlisted",
        idempotency_key=_idempotency_fixture("enhancement-run", 1),
    )
    assert started.workflow_id == "enhance-solution-synthetic"
    request = control.start_enhancement_solution.await_args.args[1]
    assert request.approval_binding == "1".zfill(64)
    assert request.execution_mode == "source-pr"
    assert request.dry_run is False

    rejected = await bus.submit(
        EnhancementRejectCommand(
            project="falcone",
            enhancement_id=rejected_id,
            reason_code="OUT_OF_SCOPE",
        ),
        source_channel="cli",
        operator_class="engineering-ctl:hermes",
        idempotency_key=_idempotency_fixture("enhancement-reject", 1),
    )
    assert rejected.status == "COMPLETED"
    rejected_record = await metadata.get_enhancement(rejected_id)
    assert rejected_record is not None
    assert rejected_record["status"] == "REJECTED"
    assert rejected_record["payload_json"]["rejection"]["reason"] == "OUT_OF_SCOPE"

    registry = {item["action"]: item for item in COMMAND_REGISTRY}
    assert registry["enhancement_approve"]["status"] == "AVAILABLE"
    assert registry["enhancement_reject"]["status"] == "AVAILABLE"
    assert registry["enhancement_run"]["status"] == "AVAILABLE"
    assert not any(
        field["name"] in {"command", "args", "argv", "shell"}
        for action in ("enhancement_approve", "enhancement_reject", "enhancement_run")
        for field in registry[action]["fields"]
    )
    metadata.engine.dispose()


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
        operator_class="engineering-ctl:hermes",
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
            operator_class="engineering-ctl:hermes",
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
        operator_class="engineering-ctl:hermes",
        idempotency_key=_idempotency_fixture("audit-plan", 1),
    )
    assert planned.status == "COMPLETED"
    accepted = await bus.submit(
        start,
        source_channel="cli",
        operator_class="engineering-ctl:hermes",
        idempotency_key=_idempotency_fixture("audit-start", 1),
    )
    assert accepted.workflow_id == "audit-synthetic"
    sent = control.start_campaign.await_args.args[0]
    assert sent.publish_findings is False


@pytest.mark.asyncio
async def test_all_project_audit_requires_plan_then_starts_exactly_one_parent(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _seed_ready_issue(repository)
    control = Mock()
    control.plan_campaign = AsyncMock(return_value={"status": "PLANNED", "blockers": []})
    control.start_all_project_audits = AsyncMock(
        return_value=OperationAccepted(workflow_id="audit-all-synthetic", run_id="audit-all-run")
    )
    bus = TypedCommandBus(control, Mock(), repository)  # type: ignore[arg-type]
    start = AuditAllProjectsCommand(mode="start", preset="snapshot")

    before_plan = await repository.capability(start)
    assert before_plan.status == "BLOCKED"
    assert "ALL_PROJECT_AUDIT_PLAN_REQUIRED" in before_plan.blockers

    planned = await bus.submit(
        AuditAllProjectsCommand(mode="plan", preset="snapshot"),
        source_channel="cli",
        operator_class="engineering-ctl:hermes",
        idempotency_key=_idempotency_fixture("audit-all-plan", 1),
    )
    assert planned.status == "COMPLETED"
    assert [call.args[0].project.value for call in control.plan_campaign.await_args_list] == [
        "falcone",
        "musematic",
        "llmwiki",
    ]

    candidate = await repository.candidate(start, SESSION_A)
    assert candidate.capability.status == "AVAILABLE"
    assert candidate.confirmation_phrase == f"START ALL AUDITS {candidate.binding_hash[:8].upper()}"
    assert candidate.operation_binding["resolved"]["execution"] == "sequential"
    registry = {item["action"]: item for item in COMMAND_REGISTRY}
    assert registry["audit_all_projects"]["status"] == "AVAILABLE"
    assert not any(
        field["name"] in {"command", "args", "argv", "shell", "endpoint"}
        for field in registry["audit_all_projects"]["fields"]
    )

    accepted = await bus.submit(
        start,
        source_channel="cli",
        operator_class="engineering-ctl:hermes",
        idempotency_key=_idempotency_fixture("audit-all-start", 1),
    )
    assert accepted.workflow_id == "audit-all-synthetic"
    assert control.start_all_project_audits.await_count == 1

    duplicate = await repository.capability(start)
    assert duplicate.status == "BLOCKED"
    assert "ALL_PROJECT_AUDIT_ALREADY_ACTIVE" in duplicate.blockers


@pytest.mark.asyncio
async def test_all_project_audit_plan_stops_before_start_when_one_project_is_blocked(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    control = Mock()
    control.plan_campaign = AsyncMock(
        side_effect=[
            {"status": "PLANNED", "blockers": []},
            {"status": "BLOCKED", "blockers": ["SYNTHETIC_OBSERVER_UNAVAILABLE"]},
        ]
    )
    bus = TypedCommandBus(control, Mock(), repository)  # type: ignore[arg-type]

    result = await bus.submit(
        AuditAllProjectsCommand(mode="plan", preset="snapshot"),
        source_channel="cli",
        operator_class="engineering-ctl:hermes",
        idempotency_key=_idempotency_fixture("audit-all-blocked", 1),
    )

    assert result.status == "BLOCKED"
    assert result.safe_error == "ALL_PROJECT_AUDIT_PLAN_BLOCKED_MUSEMATIC"
    assert control.plan_campaign.await_count == 2


def test_all_project_audit_command_fails_closed_outside_staging() -> None:
    with pytest.raises(ValidationError):
        parse_command(
            {
                "action": "audit_all_projects",
                "mode": "start",
                "preset": "snapshot",
                "environment": "production",
                "production": False,
            }
        )
    with pytest.raises(ValidationError):
        AuditAllProjectsCommand(mode="plan", preset="security", publish_findings=True)


@pytest.mark.asyncio
async def test_multi_project_audit_metadata_is_created_atomically(tmp_path: Path) -> None:
    metadata = SQLMetadataRepository(f"sqlite+pysqlite:///{tmp_path / 'audit-all.db'}")
    Base.metadata.create_all(metadata.engine)
    parent = "audit-all-11111111-1111-4111-8111-111111111111"
    campaigns = {
        Project.FALCONE: (
            UUID("11111111-1111-4111-8111-111111111111"),
            f"{parent}-falcone",
        ),
        Project.MUSEMATIC: (
            UUID("22222222-2222-4222-8222-222222222222"),
            f"{parent}-musematic",
        ),
        Project.LLMWIKI: (
            UUID("33333333-3333-4333-8333-333333333333"),
            f"{parent}-llmwiki",
        ),
    }

    await metadata.create_multi_project_audit(
        parent,
        campaigns,
        MultiProjectAuditStart(preset="snapshot"),
    )

    with metadata.sessions() as session:
        parent_run = session.scalar(select(Run).where(Run.workflow_id == parent))
        children = session.scalars(
            select(Run).where(Run.run_type == "audit-campaign").order_by(Run.project)
        ).all()
        stored_campaigns = session.scalars(select(Campaign).order_by(Campaign.project)).all()
    assert parent_run is not None and parent_run.status == "QUEUED"
    assert [child.project for child in children] == ["falcone", "llmwiki", "musematic"]
    assert [campaign.project for campaign in stored_campaigns] == [
        "falcone",
        "llmwiki",
        "musematic",
    ]
    assert all(
        campaign.metadata_json["parent_workflow_id"] == parent for campaign in stored_campaigns
    )


@pytest.mark.asyncio
async def test_all_project_audit_workflow_is_sequential_and_continues_after_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = MultiProjectAuditWorkflow()
    instance._checkpoint = AsyncMock(return_value=True)  # type: ignore[method-assign]
    state_calls: list[dict[str, object]] = []

    async def step(name: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        assert name == "campaign.group_status"
        state_calls.append(payload)
        return payload

    instance._step = step  # type: ignore[method-assign]
    children: list[str] = []

    async def child(_name: str, _input: object, *, id: str, **_kwargs: object) -> dict[str, object]:
        children.append(id)
        if id.endswith("-musematic"):
            return {"status": "BLOCKED", "blocker": "SYNTHETIC_DEPENDENCY_BLOCKED"}
        return {"status": "COMPLETE"}

    monkeypatch.setattr(workflow_definitions.workflow, "patched", lambda _name: True)
    monkeypatch.setattr(
        workflow_definitions.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="audit-all-synthetic", task_queue="engineering"),
    )
    monkeypatch.setattr(workflow_definitions.workflow, "execute_child_workflow", child)
    request = MultiProjectAuditInput(
        preset="snapshot",
        campaign_ids={
            "falcone": "11111111-1111-4111-8111-111111111111",
            "musematic": "22222222-2222-4222-8222-222222222222",
            "llmwiki": "33333333-3333-4333-8333-333333333333",
        },
        child_workflow_ids={
            project: f"audit-all-synthetic-{project}"
            for project in ("falcone", "musematic", "llmwiki")
        },
    )

    result = await instance.run(request)

    assert children == [
        "audit-all-synthetic-falcone",
        "audit-all-synthetic-musematic",
        "audit-all-synthetic-llmwiki",
    ]
    assert result["status"] == "BLOCKED"
    assert state_calls[-1]["status"] == "BLOCKED"
    assert set(result["project_results"]) == {"falcone", "musematic", "llmwiki"}


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
async def test_global_child_returns_blocker_without_waiting_or_completing_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = ExistingGitHubBacklogLoopWorkflow()
    instance._checkpoint = AsyncMock(return_value=True)  # type: ignore[method-assign]
    state_calls: list[dict[str, object]] = []

    async def step(name: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        if name == "backlog.load_execution":
            return {
                "status": "READY",
                "items": [{"repository": "gntik-ai/falcone", "issue_number": 957}],
            }
        if name == "backlog.revalidate_gate":
            return {"status": "BLOCKED", "blocker": "STAGING_OBSERVER_NOT_READY"}
        if name == "backlog.record_loop_state":
            state_calls.append(payload)
        return {"status": "REGISTERED"}

    instance._step = step  # type: ignore[method-assign]
    monkeypatch.setattr(workflow_definitions.workflow, "patched", lambda _name: True)

    result = await instance.run(
        ExistingGitHubBacklogLoopInput(
            project="falcone",
            backlog_campaign_id=str(CAMPAIGN_ID),
            maximum_risk="R2",
            delivery_mode="full-delivery",
            maximum_items=1,
            continue_after_blocked_issue=False,
            correction_limit=5,
            global_campaign_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )
    )

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "STAGING_OBSERVER_NOT_READY"
    assert state_calls[-1]["loop_status"] == "PAUSED"


@pytest.mark.asyncio
async def test_global_child_persists_correction_exhaustion_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = ExistingGitHubBacklogLoopWorkflow()
    instance._checkpoint = AsyncMock(return_value=True)  # type: ignore[method-assign]
    state_calls: list[dict[str, object]] = []

    async def step(name: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        if name == "backlog.load_execution":
            return {
                "status": "READY",
                "items": [{"repository": "gntik-ai/falcone", "issue_number": 957}],
            }
        if name == "backlog.revalidate_gate":
            return {
                "status": "READY",
                "finding_id": "00000000-0000-4000-8000-000000000957",
                "risk_class": "R2",
            }
        if name == "backlog.record_loop_state":
            state_calls.append(payload)
        return {"status": "REGISTERED"}

    async def child(_name: str, _input: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "REQUEST_CHANGES"}

    instance._step = step  # type: ignore[method-assign]
    monkeypatch.setattr(workflow_definitions.workflow, "patched", lambda _name: True)
    monkeypatch.setattr(
        workflow_definitions.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="synthetic-global-child", task_queue="engineering"),
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
            correction_limit=0,
            global_campaign_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )
    )

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "CORRECTION_LIMIT_EXHAUSTED"
    assert state_calls[-1]["loop_status"] == "PAUSED"
    assert state_calls[-1]["blocker"] == "CORRECTION_LIMIT_EXHAUSTED"


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
        if name == "RuntimeVerificationWorkflow":
            return {"status": "RUNTIME_VERIFIED", "context": {}}
        assert name == "FinalClosureWorkflow"
        return {"status": "FIXED", "context": {}}

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
        "FinalClosureWorkflow",
    ]
    assert result["status"] == "COMPLETE"
    assert state_calls[-1]["loop_status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_final_closure_patch_false_preserves_the_prior_terminal_sequence(
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

    async def child(name: str, _input: object, **_kwargs: object) -> dict[str, object]:
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

    instance._step = step  # type: ignore[method-assign]
    monkeypatch.setattr(
        workflow_definitions.workflow,
        "patched",
        lambda name: (
            name
            not in {
                "existing-backlog-final-closure-v1",
                "global-backlog-child-blocker-return-v1",
            }
        ),
    )
    monkeypatch.setattr(
        workflow_definitions.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="synthetic-replay", task_queue="engineering"),
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

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "FINAL_MERGED_REVISION_DELIVERY_SERVICE_REQUIRED"
    assert state_calls[-1]["loop_status"] == "PAUSED"
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
