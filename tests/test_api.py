from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from temporalio.service import RPCError, RPCStatusCode

from engineering_control.api.main import create_app
from engineering_control.api.schemas import BacklogImportAccepted, OperationAccepted
from engineering_control.config import Settings
from engineering_control.db.repository import EmptyMetadataRepository
from engineering_control.operations.models import OperationView


class FakeService:
    async def get_reconciliation_observation(self, workflow_id: str) -> dict[str, Any]:
        return await self.get_workflow(workflow_id)

    async def plan_campaign(self, request: Any) -> dict[str, Any]:
        return {
            "project": request.project.value,
            "preset": request.preset,
            "environment": request.environment,
            "blockers": [],
        }

    async def start_campaign(self, request: Any) -> OperationAccepted:
        return OperationAccepted(workflow_id="audit-test", run_id="run-test")

    async def start_delivery(self, request: Any) -> OperationAccepted:
        return OperationAccepted(workflow_id="delivery-test", run_id="run-test")

    async def start_finding_verification(self, finding_id: UUID) -> OperationAccepted:
        return OperationAccepted(workflow_id=f"finding-verify-{finding_id}", run_id="run-test")

    async def reconcile_delivery(self, request: Any) -> OperationAccepted:
        return OperationAccepted(workflow_id="reconcile-test", run_id="run-test")

    async def signal(self, workflow_id: str, signal: str, argument: Any = None) -> None:
        return None

    async def prepare_deployment(self, request: Any) -> OperationAccepted:
        return OperationAccepted(workflow_id="deploy-test", run_id="run-test")

    async def rollback(self, request: Any) -> OperationAccepted:
        return OperationAccepted(workflow_id="recovery-test", run_id="run-test")

    async def start_runtime_verification(self, request: Any) -> OperationAccepted:
        return OperationAccepted(workflow_id="runtime-test", run_id="run-test")

    async def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return {"workflow_id": workflow_id, "status": "RUNNING"}

    async def record_approval(self, request: Any, approver_subject: str) -> None:
        return None

    async def start_acceptance(self, request: Any, validation_root: str) -> OperationAccepted:
        return OperationAccepted(workflow_id="acceptance-test", run_id="run-test")

    async def start_observability_acceptance(self, request: Any) -> OperationAccepted:
        return OperationAccepted(workflow_id="observability-acceptance-test", run_id="run-test")

    async def start_reverse_observability_acceptance(self, request: Any) -> OperationAccepted:
        return OperationAccepted(
            workflow_id="reverse-observability-acceptance-test",
            run_id="reverse-run-test",
        )

    async def import_github_backlog(self, request: Any) -> BacklogImportAccepted:
        return BacklogImportAccepted(
            workflow_id="backlog-import-test",
            run_id="run-test",
            backlog_campaign_id=UUID("11111111-1111-4111-8111-111111111111"),
        )

    async def start_issue_loop(self, request: Any) -> OperationAccepted:
        return OperationAccepted(workflow_id="loop-test", run_id="run-test")

    async def start_existing_backlog_loop(self, request: Any) -> OperationAccepted:
        return OperationAccepted(workflow_id="backlog-loop-test", run_id="run-test")


class FakeMetadataRepository(EmptyMetadataRepository):
    def __init__(self) -> None:
        self.artifact_id = uuid4()
        self.deployment_id = uuid4()

    async def get_artifact(self, artifact_id: UUID) -> dict[str, Any] | None:
        if artifact_id != self.artifact_id:
            return None
        return {
            "id": str(artifact_id),
            "relative_path": "falcone/campaign/findings/evidence.json",
            "sha256": "a" * 64,
            "redaction_status": "REDACTED",
        }

    async def get_deployment(self, deployment_id: UUID) -> dict[str, Any] | None:
        if deployment_id != self.deployment_id:
            return None
        return {
            "id": str(deployment_id),
            "status": "AWAITING_STAGING_APPROVAL",
            "strategy": "helm",
        }

    async def latest_quota_snapshots(self) -> dict[str, dict[str, Any]]:
        return {
            "codex": {
                "provider": "codex",
                "remaining_capacity": None,
                "authenticated": True,
                "cli_present": True,
                "status": "AVAILABLE",
                "source": "cli-does-not-report",
            }
        }

    async def provider_usage_history(self, days: int = 7) -> dict[str, dict[str, Any]]:
        return {
            "codex": {
                "window_days": days,
                "running_jobs": 1,
                "roles": {
                    "implementer": {
                        "invocations": 2,
                        "successes": 1,
                        "success_rate": 0.5,
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cached_input_tokens": None,
                        "estimated_cost": None,
                    }
                },
            }
        }

    async def list_campaigns(self, limit: int = 50) -> list[dict[str, Any]]:
        assert limit == 50
        return [
            {
                "campaign_id": "11111111-1111-4111-8111-111111111111",
                "name": "terminal-with-active-cell",
                "project": "llmwiki",
                "execution_status": "COMPLETED",
                "coverage_outcome": "IN_PROGRESS",
                "status": "BLOCKED",
            },
            {
                "campaign_id": "22222222-2222-4222-8222-222222222222",
                "name": "terminal-with-no-cells",
                "project": "falcone",
                "execution_status": "COMPLETED",
                "coverage_outcome": "NOT_RUN",
                "status": "NOT_RUN",
            },
            {
                "campaign_id": "33333333-3333-4333-8333-333333333333",
                "name": "terminal-with-partial-blocker",
                "project": "musematic",
                "execution_status": "COMPLETED",
                "coverage_outcome": "PARTIAL_BLOCKED",
                "status": "PARTIAL_BLOCKED",
            },
        ]


def test_api_requires_bearer_auth() -> None:
    app = create_app(settings=Settings(), service=FakeService(), token="x" * 32)
    with TestClient(app) as client:
        response = client.get("/v1/campaigns")
        assert response.status_code == 401
        response = client.get("/v1/campaigns", headers={"Authorization": f"Bearer {'x' * 32}"})
        assert response.status_code == 200


def test_readiness_api_preserves_the_validated_scope() -> None:
    operations = Mock()
    operations.operator_snapshot = AsyncMock(return_value={"status": "READY"})
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    app = create_app(
        settings=Settings(),
        service=FakeService(),
        token="x" * 32,
        operations_repository=operations,
        command_bus=bus,
    )
    headers = {"Authorization": f"Bearer {'x' * 32}"}

    with TestClient(app) as client:
        core = client.get("/v1/readiness/core", headers=headers)
        all_connections = client.get("/v1/readiness/all", headers=headers)

    assert core.json() == {"status": "READY", "scope": "core"}
    assert all_connections.json() == {"status": "READY", "scope": "all"}
    assert [value.args for value in operations.operator_snapshot.await_args_list] == [
        ("readiness-core",),
        ("readiness-all",),
    ]


def test_campaign_reconciliation_api_is_bounded_and_returns_continuation() -> None:
    operations = Mock()
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    operations.campaign_reconciliation_candidates = AsyncMock(
        return_value=[
            {
                "campaign_id": str(uuid4()),
                "project": "falcone",
                "name": f"campaign-{ordinal}",
                "status": "PLANNED",
                "workflow_id": f"workflow-{ordinal}",
                "run_type": "audit",
                "run_status": "RUNNING",
                "blocker": None,
                "age_seconds": 10,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            for ordinal in range(3)
        ]
    )
    app = create_app(
        settings=Settings(),
        service=FakeService(),
        token="x" * 32,
        operations_repository=operations,
        command_bus=bus,
    )
    headers = {"Authorization": f"Bearer {'x' * 32}"}

    with TestClient(app) as client:
        response = client.get("/v1/campaigns/reconcile?limit=2&offset=0", headers=headers)
        assert response.status_code == 200
        assert len(response.json()["items"]) == 2
        assert response.json()["classification_scope"] == {
            "kind": "BOUNDED_PAGE",
            "offset": 0,
            "candidate_count": 2,
            "canary_identity_fields": ["project", "name", "run_type"],
        }
        assert response.json()["has_more"] is True
        assert response.json()["next_offset"] == 2
        assert client.get("/v1/campaigns/reconcile?limit=201", headers=headers).status_code == 422

    operations.campaign_reconciliation_candidates.assert_awaited_once_with(3, 0)


def test_campaign_reconciliation_repairs_terminal_before_scoping_duplicate_canaries() -> None:
    terminal_campaign_id = uuid4()
    active_campaign_ids = [uuid4(), uuid4()]
    unrelated_campaign_ids = [uuid4(), uuid4()]
    candidates = [
        {
            "campaign_id": str(terminal_campaign_id),
            "project": "falcone",
            "name": "provider-canary",
            "status": "PLANNED",
            "workflow_id": "terminal-canary",
            "run_type": "provider_acceptance",
            "run_status": "RUNNING",
            "blocker": None,
            "age_seconds": 10,
            "updated_at": datetime.now(UTC).isoformat(),
        },
        *[
            {
                "campaign_id": str(campaign_id),
                "project": "falcone",
                "name": "provider-canary",
                "status": "PLANNED",
                "workflow_id": f"active-canary-{ordinal}",
                "run_type": "provider_acceptance",
                "run_status": "RUNNING",
                "blocker": None,
                "age_seconds": 10,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            for ordinal, campaign_id in enumerate(active_campaign_ids)
        ],
        {
            "campaign_id": str(unrelated_campaign_ids[0]),
            "project": "musematic",
            "name": "provider-canary",
            "status": "PLANNED",
            "workflow_id": "different-project-canary",
            "run_type": "provider_acceptance",
            "run_status": "RUNNING",
            "blocker": None,
            "age_seconds": 10,
            "updated_at": datetime.now(UTC).isoformat(),
        },
        {
            "campaign_id": str(unrelated_campaign_ids[1]),
            "project": "falcone",
            "name": "different-provider-canary",
            "status": "PLANNED",
            "workflow_id": "different-name-canary",
            "run_type": "provider_acceptance",
            "run_status": "RUNNING",
            "blocker": None,
            "age_seconds": 10,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    ]
    operations = Mock()
    operations.campaign_reconciliation_candidates = AsyncMock(return_value=candidates)
    operations.reconcile_terminal_campaign = AsyncMock(return_value=True)
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    service = FakeService()
    service.get_workflow = AsyncMock(
        side_effect=lambda workflow_id: {
            "workflow_id": workflow_id,
            "status": "COMPLETED" if workflow_id == "terminal-canary" else "RUNNING",
        }
    )
    app = create_app(
        settings=Settings(),
        service=service,
        token="x" * 32,
        operations_repository=operations,
        command_bus=bus,
    )
    headers = {"Authorization": f"Bearer {'x' * 32}"}

    with TestClient(app) as client:
        response = client.get("/v1/campaigns/reconcile", headers=headers)

    assert response.status_code == 200
    items = {item["workflow_id"]: item for item in response.json()["items"]}
    assert items["terminal-canary"]["classification"] == "EXECUTION_BINDING_UNVERIFIED"
    assert items["terminal-canary"]["projection_repaired"] is False
    assert items["active-canary-0"]["classification"] == "DUPLICATE_CANARY"
    assert items["active-canary-1"]["classification"] == "DUPLICATE_CANARY"
    assert items["different-project-canary"]["classification"] == "RUNNING_HEALTHY"
    assert items["different-name-canary"]["classification"] == "RUNNING_HEALTHY"
    operations.reconcile_terminal_campaign.assert_not_awaited()


def test_campaign_reconciliation_distinguishes_missing_from_unavailable_temporal() -> None:
    candidate = {
        "campaign_id": str(uuid4()),
        "project": "falcone",
        "name": "provider-canary",
        "status": "PLANNED",
        "workflow_id": "missing-canary",
        "run_type": "provider_acceptance",
        "run_status": "RUNNING",
        "blocker": None,
        "age_seconds": 10,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    operations = Mock()
    operations.campaign_reconciliation_candidates = AsyncMock(return_value=[candidate])
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    service = FakeService()
    service.get_workflow = AsyncMock(
        side_effect=RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
    )
    app = create_app(
        settings=Settings(),
        service=service,
        token="x" * 32,
        operations_repository=operations,
        command_bus=bus,
    )
    headers = {"Authorization": f"Bearer {'x' * 32}"}

    with TestClient(app) as client:
        missing = client.get("/v1/campaigns/reconcile", headers=headers)
        service.get_workflow.side_effect = RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
        unavailable = client.get("/v1/campaigns/reconcile", headers=headers)

    assert missing.status_code == 200
    assert missing.json()["items"][0]["classification"] == "TEMPORAL_WORKFLOW_MISSING"
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Temporal visibility is temporarily unavailable"}


def test_campaign_reconciliation_never_repairs_a_shared_terminal_workflow_binding() -> None:
    campaign_ids = [uuid4(), uuid4()]
    candidates = [
        {
            "campaign_id": str(campaign_id),
            "project": "falcone",
            "name": "provider-canary",
            "status": "PLANNED",
            "workflow_id": "shared-terminal-canary",
            "run_type": "provider_acceptance",
            "run_status": "RUNNING",
            "blocker": None,
            "age_seconds": 10,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        for campaign_id in campaign_ids
    ]
    operations = Mock()
    operations.campaign_reconciliation_candidates = AsyncMock(return_value=candidates)
    operations.reconcile_terminal_campaign = AsyncMock(return_value=True)
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    service = FakeService()
    service.get_workflow = AsyncMock(
        return_value={"workflow_id": "shared-terminal-canary", "status": "COMPLETED"}
    )
    app = create_app(
        settings=Settings(),
        service=service,
        token="x" * 32,
        operations_repository=operations,
        command_bus=bus,
    )
    headers = {"Authorization": f"Bearer {'x' * 32}"}

    with TestClient(app) as client:
        response = client.get("/v1/campaigns/reconcile", headers=headers)

    assert response.status_code == 200
    assert [item["classification"] for item in response.json()["items"]] == [
        "INCONSISTENT_STATE",
        "INCONSISTENT_STATE",
    ]
    assert [item["projection_repaired"] for item in response.json()["items"]] == [
        False,
        False,
    ]
    service.get_workflow.assert_awaited_once_with("shared-terminal-canary")
    operations.reconcile_terminal_campaign.assert_not_awaited()


def test_shared_projection_rows_do_not_make_a_distinct_canary_look_duplicate() -> None:
    workflow_ids = ["shared-running-canary", "shared-running-canary", "distinct-running-canary"]
    candidates = [
        {
            "campaign_id": str(uuid4()),
            "project": "falcone",
            "name": "provider-canary",
            "status": "PLANNED",
            "workflow_id": workflow_id,
            "run_type": "provider_acceptance",
            "run_status": "RUNNING",
            "blocker": None,
            "age_seconds": 10,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        for workflow_id in workflow_ids
    ]
    operations = Mock()
    operations.campaign_reconciliation_candidates = AsyncMock(return_value=candidates)
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    service = FakeService()
    service.get_workflow = AsyncMock(
        side_effect=lambda workflow_id: {"workflow_id": workflow_id, "status": "RUNNING"}
    )
    app = create_app(
        settings=Settings(),
        service=service,
        token="x" * 32,
        operations_repository=operations,
        command_bus=bus,
    )
    headers = {"Authorization": f"Bearer {'x' * 32}"}

    with TestClient(app) as client:
        response = client.get("/v1/campaigns/reconcile", headers=headers)

    assert response.status_code == 200
    assert [item["classification"] for item in response.json()["items"]] == [
        "INCONSISTENT_STATE",
        "INCONSISTENT_STATE",
        "RUNNING_HEALTHY",
    ]
    assert service.get_workflow.await_count == 2


def test_campaign_reconciliation_unbound_preview_does_not_reach_writer() -> None:
    candidate = {
        "campaign_id": str(uuid4()),
        "project": "falcone",
        "name": "provider-canary",
        "status": "PLANNED",
        "workflow_id": "rebound-terminal-canary",
        "run_type": "provider_acceptance",
        "run_status": "RUNNING",
        "blocker": None,
        "age_seconds": 10,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    operations = Mock()
    operations.campaign_reconciliation_candidates = AsyncMock(return_value=[candidate])
    operations.reconcile_terminal_campaign = AsyncMock(
        side_effect=ValueError("campaign workflow binding changed during reconciliation")
    )
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    service = FakeService()
    service.get_workflow = AsyncMock(
        return_value={"workflow_id": "rebound-terminal-canary", "status": "COMPLETED"}
    )
    app = create_app(
        settings=Settings(),
        service=service,
        token="x" * 32,
        operations_repository=operations,
        command_bus=bus,
    )
    headers = {"Authorization": f"Bearer {'x' * 32}"}

    with TestClient(app) as client:
        response = client.get("/v1/campaigns/reconcile", headers=headers)

    assert response.status_code == 200
    assert response.json()["items"][0]["classification"] == "EXECUTION_BINDING_UNVERIFIED"
    assert response.json()["items"][0]["projection_repaired"] is False
    operations.reconcile_terminal_campaign.assert_not_awaited()


def test_typed_command_route_accepts_only_cli_or_telegram_actors() -> None:
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    bus.submit = AsyncMock(
        return_value=OperationView(
            operation_id=uuid4(),
            action="refresh_connections",
            target_type="connections",
            target_id="all",
            source_channel="cli",
            status="COMPLETED",
            workflow_id=None,
            temporal_run_id=None,
            binding_hash="a" * 64,
            safe_error=None,
            created_at=datetime.now(UTC),
        )
    )
    app = create_app(
        settings=Settings(),
        service=FakeService(),
        token="x" * 32,
        operations_repository=Mock(),
        command_bus=bus,
    )
    base = {
        "Authorization": f"Bearer {'x' * 32}",
        "Idempotency-Key": str(uuid4()),
    }
    with TestClient(app) as client:
        default_actor = client.post(
            "/v1/commands", headers=base, json={"action": "refresh_connections"}
        )
        dashboard_actor = client.post(
            "/v1/commands",
            headers={**base, "X-Engineering-Actor": "dashboard"},
            json={"action": "refresh_connections"},
        )
        cli_actor = client.post(
            "/v1/commands",
            headers={**base, "X-Engineering-Actor": "engineering-ctl:hermes"},
            json={"action": "refresh_connections"},
        )
        telegram_actor = client.post(
            "/v1/commands",
            headers={**base, "X-Engineering-Actor": "telegram:42"},
            json={"action": "refresh_connections"},
        )
        legacy_cli_actor = client.post(
            "/v1/commands",
            headers={**base, "X-Engineering-Actor": "engineering-ctl"},
            json={"action": "refresh_connections"},
        )
        forged_cli_actor = client.post(
            "/v1/commands",
            headers={**base, "X-Engineering-Actor": "engineering-ctl:root"},
            json={"action": "refresh_connections"},
        )
        malformed_telegram_actor = client.post(
            "/v1/commands",
            headers={**base, "X-Engineering-Actor": "telegram:allowlisted"},
            json={"action": "refresh_connections"},
        )
        arbitrary = client.post(
            "/v1/commands",
            headers={**base, "X-Engineering-Actor": "engineering-ctl:hermes"},
            json={"action": "shell", "command": "id"},
        )
    assert default_actor.status_code == 403
    assert dashboard_actor.status_code == 403
    assert cli_actor.status_code == 202
    assert telegram_actor.status_code == 202
    assert legacy_cli_actor.status_code == 403
    assert forged_cli_actor.status_code == 403
    assert malformed_telegram_actor.status_code == 403
    assert arbitrary.status_code == 409
    assert bus.submit.await_args_list[0].kwargs["source_channel"] == "cli"
    assert bus.submit.await_args_list[1].kwargs["source_channel"] == "telegram"
    assert bus.submit.await_args_list[0].kwargs["operator_class"] == "engineering-ctl:hermes"
    assert bus.submit.await_args_list[1].kwargs["operator_class"] == "telegram:42"


def test_every_direct_approval_route_returns_403_before_dispatch_for_forged_actors() -> None:
    metadata = Mock()
    metadata.approve_backlog_campaign = AsyncMock()
    metadata.approve_enhancement = AsyncMock()
    metadata.reject_enhancement = AsyncMock()
    operations = Mock()
    operations.approve_r3_issue = AsyncMock()
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    service = FakeService()
    service.record_approval = AsyncMock()
    app = create_app(
        settings=Settings(),
        service=service,
        repository=metadata,
        token="x" * 32,
        operations_repository=operations,
        command_bus=bus,
    )
    routes = (
        (
            "/v1/r3-issues/approve",
            {
                "project": "musematic",
                "surface": "source",
                "issue_number": 1119,
                "binding": "a" * 64,
                "confirmation_phrase": "APPROVE_R3_MUSEMATIC_1119_AAAAAAAAAAAAAAAA",
            },
        ),
        (
            f"/v1/backlogs/{uuid4()}/approve",
            {
                "project": "falcone",
                "approval_binding": "a" * 64,
                "typed_confirmation": "APPROVE_EXISTING_GITHUB_BACKLOG_LOOP",
            },
        ),
        (f"/v1/enhancements/{uuid4()}/approve", {"approval_binding": "a" * 64}),
        (f"/v1/enhancements/{uuid4()}/reject", {"reason": "bounded rejection"}),
        (
            "/v1/approvals",
            {
                "change_set_id": str(uuid4()),
                "artifact_binding_sha256": "a" * 64,
                "decision": "approve",
            },
        ),
    )
    base = {"Authorization": f"Bearer {'x' * 32}"}

    with TestClient(app) as client:
        responses = [
            client.post(
                path,
                headers={**base, "X-Engineering-Actor": actor},
                json=payload,
            )
            for actor in ("engineering-ctl", "engineering-ctl:root", "telegram:allowlisted")
            for path, payload in routes
        ]

    assert all(response.status_code == 403 for response in responses)
    operations.approve_r3_issue.assert_not_awaited()
    metadata.approve_backlog_campaign.assert_not_awaited()
    metadata.approve_enhancement.assert_not_awaited()
    metadata.reject_enhancement.assert_not_awaited()
    service.record_approval.assert_not_awaited()


def test_every_direct_approval_route_dispatches_for_the_verified_cli_subject() -> None:
    metadata = Mock()
    metadata.approve_backlog_campaign = AsyncMock(return_value={"status": "APPROVED"})
    metadata.approve_enhancement = AsyncMock(return_value={"status": "APPROVED"})
    metadata.reject_enhancement = AsyncMock(return_value={"status": "REJECTED"})
    operations = Mock()
    operations.approve_r3_issue = AsyncMock(return_value={"status": "R3_APPROVED"})
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    service = FakeService()
    service.record_approval = AsyncMock()
    app = create_app(
        settings=Settings(),
        service=service,
        repository=metadata,
        token="x" * 32,
        operations_repository=operations,
        command_bus=bus,
    )
    headers = {
        "Authorization": f"Bearer {'x' * 32}",
        "X-Engineering-Actor": "engineering-ctl:hermes",
    }

    with TestClient(app) as client:
        responses = (
            client.post(
                "/v1/r3-issues/approve",
                headers=headers,
                json={
                    "project": "musematic",
                    "surface": "source",
                    "issue_number": 1119,
                    "binding": "a" * 64,
                    "confirmation_phrase": "APPROVE_R3_MUSEMATIC_1119_AAAAAAAAAAAAAAAA",
                },
            ),
            client.post(
                f"/v1/backlogs/{uuid4()}/approve",
                headers=headers,
                json={
                    "project": "falcone",
                    "approval_binding": "a" * 64,
                    "typed_confirmation": "APPROVE_EXISTING_GITHUB_BACKLOG_LOOP",
                },
            ),
            client.post(
                f"/v1/enhancements/{uuid4()}/approve",
                headers=headers,
                json={"approval_binding": "a" * 64},
            ),
            client.post(
                f"/v1/enhancements/{uuid4()}/reject",
                headers=headers,
                json={"reason": "bounded rejection"},
            ),
            client.post(
                "/v1/approvals",
                headers=headers,
                json={
                    "change_set_id": str(uuid4()),
                    "artifact_binding_sha256": "a" * 64,
                    "decision": "approve",
                },
            ),
        )

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 202]
    operations.approve_r3_issue.assert_awaited_once()
    metadata.approve_backlog_campaign.assert_awaited_once()
    metadata.approve_enhancement.assert_awaited_once()
    metadata.reject_enhancement.assert_awaited_once()
    service.record_approval.assert_awaited_once()
    assert operations.approve_r3_issue.await_args.args[-1] == "cli:engineering-ctl"
    assert metadata.approve_backlog_campaign.await_args.args[-1] == "cli:engineering-ctl"


def test_private_operation_export_requires_api_auth_and_returns_only_stored_projection() -> None:
    operation_id = uuid4()
    operations = Mock()
    operations.export_content = AsyncMock(
        side_effect=lambda value: (
            {"schema_version": 1, "workflow": {"workflow_id": "safe-workflow"}}
            if value == operation_id
            else None
        )
    )
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    app = create_app(
        settings=Settings(),
        service=FakeService(),
        token="x" * 32,
        operations_repository=operations,
        command_bus=bus,
    )
    headers = {"Authorization": f"Bearer {'x' * 32}"}
    with TestClient(app) as client:
        assert client.get(f"/v1/operations/{operation_id}/export").status_code == 401
        response = client.get(f"/v1/operations/{operation_id}/export", headers=headers)
        missing = client.get(f"/v1/operations/{uuid4()}/export", headers=headers)
    assert response.status_code == 200
    assert response.json()["workflow"]["workflow_id"] == "safe-workflow"
    assert missing.status_code == 404


def test_internal_metrics_expose_named_control_plane_families() -> None:
    app = create_app(settings=Settings(), service=FakeService(), token="x" * 32)
    with TestClient(app) as client:
        unauthorized = client.get("/internal/metrics")
        response = client.get(
            "/internal/metrics",
            headers={"Authorization": f"Bearer {'x' * 32}"},
        )
    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert "engineering_active_campaigns" in response.text
    assert "engineering_worker_health" in response.text
    assert "engineering_evidence_storage_bytes" in response.text
    assert "engineering_dashboard_session_revocations" in response.text
    assert "engineering_dashboard_session_revocation_cleanup_truncated" in response.text
    assert "engineering_notification_outbox" in response.text
    assert "engineering_notification_oldest_pending_age_seconds" in response.text


def test_api_rejects_extra_fields() -> None:
    app = create_app(settings=Settings(), service=FakeService(), token="x" * 32)
    body = {
        "project": "falcone",
        "name": "smoke",
        "source_sha": "a" * 40,
        "deployment_sha": "b" * 40,
        "domains": ["gateway"],
        "personas": ["tenant-admin"],
        "lenses": ["correctness"],
        "environments": ["snapshot"],
        "dry_run": True,
        "shell": "forbidden",
    }
    with TestClient(app) as client:
        response = client.post(
            "/v1/campaigns",
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json=body,
        )
        assert response.status_code == 422


def test_delivery_reconciliation_accepts_only_bounded_identifiers() -> None:
    app = create_app(settings=Settings(), service=FakeService(), token="x" * 32)
    with TestClient(app) as client:
        response = client.post(
            "/v1/deliveries/reconcile",
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={
                "change_set_id": str(uuid4()),
                "candidate_build_run_id": 42,
                "dry_run": True,
            },
        )
        rejected = client.post(
            "/v1/deliveries/reconcile",
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={
                "change_set_id": str(uuid4()),
                "candidate_build_run_id": 0,
                "dry_run": True,
                "repository": "attacker/repo",
            },
        )
    assert response.status_code == 202
    assert response.json()["workflow_id"] == "reconcile-test"
    assert rejected.status_code == 422


def test_finding_verification_has_a_bounded_authenticated_route() -> None:
    finding_id = uuid4()
    app = create_app(settings=Settings(), service=FakeService(), token="x" * 32)
    with TestClient(app) as client:
        unauthorized = client.post("/v1/findings/verify", json={"finding_id": str(finding_id)})
        accepted = client.post(
            "/v1/findings/verify",
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={"finding_id": str(finding_id)},
        )
        invalid = client.post(
            "/v1/findings/verify",
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={"finding_id": "not-a-uuid"},
        )
        extra = client.post(
            "/v1/findings/verify",
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={"finding_id": str(finding_id), "execute": True},
        )
    assert unauthorized.status_code == 401
    assert accepted.status_code == 202
    assert accepted.json()["workflow_id"] == f"finding-verify-{finding_id}"
    assert invalid.status_code == 422
    assert extra.status_code == 422


def test_synthetic_validation_endpoint_is_disabled_by_default() -> None:
    app = create_app(settings=Settings(), service=FakeService(), token="x" * 32)
    with TestClient(app) as client:
        response = client.post(
            "/v1/validation/lifecycle",
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={"hold_seconds": 1},
        )
        assert response.status_code == 404


def test_backlog_import_route_is_fixed_read_only_and_strict() -> None:
    app = create_app(settings=Settings(), service=FakeService(), token="x" * 32)
    headers = {"Authorization": f"Bearer {'x' * 32}"}
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/backlogs/import",
            headers=headers,
            json={
                "project": "falcone",
                "source": "github-open",
                "import_filter": "bugs-and-docs",
                "maximum_items": 1,
            },
        )
        llmwiki = client.post(
            "/v1/backlogs/import",
            headers=headers,
            json={
                "project": "llmwiki",
                "source": "github-open",
                "import_filter": "all",
                "maximum_items": 1,
            },
        )
        injected = client.post(
            "/v1/backlogs/import",
            headers=headers,
            json={
                "project": "falcone",
                "source": "github-open",
                "import_filter": "all",
                "maximum_items": 1,
                "gh_args": ["issue", "close", "1"],
            },
        )
    assert accepted.status_code == 202
    assert accepted.json()["workflow_id"] == "backlog-import-test"
    assert llmwiki.status_code == 202
    assert injected.status_code == 422


def test_general_synthetic_mode_does_not_enable_acceptance_endpoint() -> None:
    app = create_app(
        settings=Settings(synthetic_mode=True),
        service=FakeService(),
        token="x" * 32,
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/validation/lifecycle",
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={"hold_seconds": 1},
        )
        assert response.status_code == 404


def test_synthetic_validation_endpoint_requires_explicit_acceptance_enablement(
    tmp_path: Any,
) -> None:
    app = create_app(
        settings=Settings(acceptance_enabled=True, acceptance_root=tmp_path),
        service=FakeService(),
        token="x" * 32,
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/validation/lifecycle",
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={"hold_seconds": 1},
        )
        assert response.status_code == 202


def test_observability_acceptance_is_authenticated_gated_and_read_only() -> None:
    app = create_app(
        settings=Settings(acceptance_enabled=True),
        service=FakeService(),
        token="x" * 32,
    )
    headers = {"Authorization": f"Bearer {'x' * 32}"}
    with TestClient(app) as client:
        unauthorized = client.post("/v1/validation/observability", json={"confirm_read_only": True})
        accepted = client.post(
            "/v1/validation/observability",
            headers=headers,
            json={"confirm_read_only": True},
        )
        rejected = client.post(
            "/v1/validation/observability",
            headers=headers,
            json={"confirm_read_only": False},
        )
        injected = client.post(
            "/v1/validation/observability",
            headers=headers,
            json={"confirm_read_only": True, "command": "shell"},
        )
        reverse = client.post(
            "/v1/validation/observability/reverse",
            headers=headers,
            json={"confirm_read_only": True},
        )
    assert unauthorized.status_code == 401
    assert accepted.status_code == 202
    assert accepted.json()["workflow_id"] == "observability-acceptance-test"
    assert rejected.status_code == 422
    assert injected.status_code == 422
    assert reverse.status_code == 202
    assert reverse.json() == {
        "status": "ACCEPTED",
        "workflow_id": "reverse-observability-acceptance-test",
        "run_id": "reverse-run-test",
    }


def test_metadata_endpoints_return_stored_records_and_unknown_usage() -> None:
    repository = FakeMetadataRepository()
    app = create_app(
        settings=Settings(),
        service=FakeService(),
        repository=repository,
        token="x" * 32,
    )
    headers = {"Authorization": f"Bearer {'x' * 32}"}
    with TestClient(app) as client:
        evidence = client.get(f"/v1/evidence/{repository.artifact_id}", headers=headers)
        assert evidence.status_code == 200
        assert evidence.json()["sha256"] == "a" * 64

        deployment = client.get(f"/v1/deployments/{repository.deployment_id}", headers=headers)
        assert deployment.status_code == 200
        assert deployment.json()["strategy"] == "helm"

        verification = client.post(
            "/v1/deployments/verify-runtime",
            headers=headers,
            json={"deployment_id": str(repository.deployment_id)},
        )
        assert verification.status_code == 202
        assert verification.json()["workflow_id"] == "runtime-test"

        usage = client.get("/v1/usage", headers=headers)
        assert usage.status_code == 200
        assert usage.json()["codex"]["source"] == "cli-does-not-report"
        assert usage.json()["codex"]["authenticated"] is True
        assert usage.json()["codex"]["cli_present"] is True

        campaigns = client.get("/v1/campaigns", headers=headers)
        assert campaigns.status_code == 200
        assert campaigns.json()["items"] == [
            {
                "campaign_id": "11111111-1111-4111-8111-111111111111",
                "name": "terminal-with-active-cell",
                "project": "llmwiki",
                "execution_status": "COMPLETED",
                "coverage_outcome": "IN_PROGRESS",
                "status": "BLOCKED",
            },
            {
                "campaign_id": "22222222-2222-4222-8222-222222222222",
                "name": "terminal-with-no-cells",
                "project": "falcone",
                "execution_status": "COMPLETED",
                "coverage_outcome": "NOT_RUN",
                "status": "NOT_RUN",
            },
            {
                "campaign_id": "33333333-3333-4333-8333-333333333333",
                "name": "terminal-with-partial-blocker",
                "project": "musematic",
                "execution_status": "COMPLETED",
                "coverage_outcome": "PARTIAL_BLOCKED",
                "status": "PARTIAL_BLOCKED",
            },
        ]
        # This historical fixture has no timestamp, so it is not current readiness.
        assert usage.json()["codex"]["status"] == "STALE"
        assert usage.json()["codex"]["raw_status"] == "AVAILABLE"
        assert usage.json()["codex"]["running_jobs"] == 1
        assert usage.json()["codex"]["history"]["roles"]["implementer"]["success_rate"] == 0.5
        assert usage.json()["claude"]["source"] == "not-reported"
        assert usage.json()["claude"]["remaining_capacity"] is None
        assert usage.json()["claude"]["authenticated"] is None
        assert usage.json()["claude"]["cli_present"] is None
        assert usage.json()["claude"]["status"] == "NOT_REPORTED"
        assert usage.json()["claude"]["history"]["roles"] == {}


def test_metadata_endpoints_return_not_found_for_unknown_ids() -> None:
    app = create_app(settings=Settings(), service=FakeService(), token="x" * 32)
    headers = {"Authorization": f"Bearer {'x' * 32}"}
    with TestClient(app) as client:
        assert client.get(f"/v1/evidence/{uuid4()}", headers=headers).status_code == 404
        assert client.get(f"/v1/deployments/{uuid4()}", headers=headers).status_code == 404
