from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from engineering_control.api.main import create_app
from engineering_control.api.schemas import OperationAccepted
from engineering_control.config import Settings
from engineering_control.db.repository import EmptyMetadataRepository


class FakeService:
    async def start_campaign(self, request: Any) -> OperationAccepted:
        return OperationAccepted(workflow_id="audit-test", run_id="run-test")

    async def start_delivery(self, request: Any) -> OperationAccepted:
        return OperationAccepted(workflow_id="delivery-test", run_id="run-test")

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
                "source": "cli-does-not-report",
            }
        }


def test_api_requires_bearer_auth() -> None:
    app = create_app(settings=Settings(), service=FakeService(), token="x" * 32)
    with TestClient(app) as client:
        response = client.get("/v1/campaigns")
        assert response.status_code == 401
        response = client.get("/v1/campaigns", headers={"Authorization": f"Bearer {'x' * 32}"})
        assert response.status_code == 200


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


def test_synthetic_validation_endpoint_is_disabled_by_default() -> None:
    app = create_app(settings=Settings(), service=FakeService(), token="x" * 32)
    with TestClient(app) as client:
        response = client.post(
            "/v1/validation/lifecycle",
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={"hold_seconds": 1},
        )
        assert response.status_code == 404


def test_synthetic_validation_endpoint_requires_explicit_mode(tmp_path: Any) -> None:
    app = create_app(
        settings=Settings(synthetic_mode=True, acceptance_root=tmp_path),
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
        assert usage.json()["claude"]["source"] == "not-reported"
        assert usage.json()["claude"]["remaining_capacity"] is None


def test_metadata_endpoints_return_not_found_for_unknown_ids() -> None:
    app = create_app(settings=Settings(), service=FakeService(), token="x" * 32)
    headers = {"Authorization": f"Bearer {'x' * 32}"}
    with TestClient(app) as client:
        assert client.get(f"/v1/evidence/{uuid4()}", headers=headers).status_code == 404
        assert client.get(f"/v1/deployments/{uuid4()}", headers=headers).status_code == 404
