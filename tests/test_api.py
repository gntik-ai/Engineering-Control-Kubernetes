from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from engineering_control.api.main import create_app
from engineering_control.api.schemas import BacklogImportAccepted, OperationAccepted
from engineering_control.config import Settings
from engineering_control.db.repository import EmptyMetadataRepository
from engineering_control.operations.models import OperationView


class FakeService:
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
        response = client.post("/v1/campaigns/reconcile?limit=2&offset=0", headers=headers)
        assert response.status_code == 200
        assert len(response.json()["items"]) == 2
        assert response.json()["has_more"] is True
        assert response.json()["next_offset"] == 2
        assert client.post("/v1/campaigns/reconcile?limit=201", headers=headers).status_code == 422

    operations.campaign_reconciliation_candidates.assert_awaited_once_with(3, 0)


def test_campaign_reconciliation_repairs_terminal_before_scoping_duplicate_canaries() -> None:
    terminal_campaign_id = uuid4()
    active_campaign_ids = [uuid4(), uuid4()]
    unrelated_campaign_id = uuid4()
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
            "campaign_id": str(unrelated_campaign_id),
            "project": "musematic",
            "name": "different-provider-canary",
            "status": "PLANNED",
            "workflow_id": "unrelated-canary",
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
        response = client.post("/v1/campaigns/reconcile", headers=headers)

    assert response.status_code == 200
    items = {item["workflow_id"]: item for item in response.json()["items"]}
    assert items["terminal-canary"]["classification"] == "TERMINAL_NOT_RECONCILED"
    assert items["terminal-canary"]["projection_repaired"] is True
    assert items["active-canary-0"]["classification"] == "DUPLICATE_CANARY"
    assert items["active-canary-1"]["classification"] == "DUPLICATE_CANARY"
    assert items["unrelated-canary"]["classification"] == "RUNNING_HEALTHY"
    operations.reconcile_terminal_campaign.assert_awaited_once_with(
        terminal_campaign_id, "terminal-canary", "COMPLETED"
    )


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
            headers={**base, "X-Engineering-Actor": "engineering-ctl"},
            json={"action": "refresh_connections"},
        )
        telegram_actor = client.post(
            "/v1/commands",
            headers={**base, "X-Engineering-Actor": "telegram:allowlisted"},
            json={"action": "refresh_connections"},
        )
        arbitrary = client.post(
            "/v1/commands",
            headers={**base, "X-Engineering-Actor": "engineering-ctl"},
            json={"action": "shell", "command": "id"},
        )
    assert default_actor.status_code == 403
    assert dashboard_actor.status_code == 403
    assert cli_actor.status_code == 202
    assert telegram_actor.status_code == 202
    assert arbitrary.status_code == 409
    assert bus.submit.await_args_list[0].kwargs["source_channel"] == "cli"
    assert bus.submit.await_args_list[1].kwargs["source_channel"] == "telegram"


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
        assert usage.json()["codex"]["status"] == "AVAILABLE"
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
