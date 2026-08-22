from typing import Any

from fastapi.testclient import TestClient

from engineering_control.api.main import create_app
from engineering_control.api.schemas import OperationAccepted
from engineering_control.config import Settings


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

    async def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return {"workflow_id": workflow_id, "status": "RUNNING"}

    async def record_approval(self, request: Any, approver_subject: str) -> None:
        return None


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
