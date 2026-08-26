from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from engineering_control.activities.core import ControlActivities
from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.api.schemas import AuditPlanRequest
from engineering_control.api.service import TemporalControlService
from engineering_control.config import Settings
from engineering_control.db.repository import EmptyMetadataRepository
from engineering_control.engineering.configuration import EngineeringCatalog
from engineering_control.identity.attestation import (
    ObserverReadinessAttestation,
    observer_contract_hash,
)
from engineering_control.models.enums import Project

ROOT = Path(__file__).parents[1]


class AttestationObserverRunner:
    def run(self, executable: str, arguments: list[str], **_kwargs: Any) -> CommandResult:
        assert executable == "kubectl"
        if arguments[-5:] == ["config", "view", "--minify", "--output", "json"]:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "clusters": [{"cluster": {"server": "https://cluster.example.test"}}],
                        "contexts": [{"name": "falcone-observer"}],
                    }
                ),
                "",
            )
        if "whoami" in arguments:
            return CommandResult(
                0,
                json.dumps({"status": {"userInfo": {"username": "system:serviceaccount:test"}}}),
                "",
            )
        if "can-i" in arguments:
            index = arguments.index("can-i")
            verb, resource = arguments[index + 1 : index + 3]
            allowed = verb in {"get", "list", "watch"} and resource not in {
                "secrets",
                "pods/exec",
                "pods/attach",
                "pods/portforward",
            }
            return CommandResult(0, "yes\n" if allowed else "no\n", "")
        raise AssertionError(arguments)


def observer_attestation(*, project: str = "falcone") -> dict[str, Any]:
    return ObserverReadinessAttestation(
        project=project,
        contract_id=f"{project}-observer",
        file_present=True,
        parser_valid=True,
        target_valid=True,
        permissions_valid=True,
        forbidden_resource_read_denied=True,
        mutation_access_denied=True,
        worker_identity=f"scan-runner@{os.uname().nodename}",
        checked_timestamp=datetime.now(UTC),
        attestation_ttl_seconds=300,
        contract_hash=observer_contract_hash(project),
        blocker_code="NONE",
    ).model_dump(mode="json")


def test_observer_file_contract_accepts_only_private_owner_or_root_group_read() -> None:
    regular = stat.S_IFREG
    owner_private = os.stat_result((regular | 0o600, 0, 0, 1, 1000, 1000, 0, 0, 0, 0))
    root_group = os.stat_result((regular | 0o640, 0, 0, 1, 0, 2000, 0, 0, 0, 0))
    world_readable = os.stat_result((regular | 0o644, 0, 0, 1, 0, 2000, 0, 0, 0, 0))
    wrong_group = os.stat_result((regular | 0o640, 0, 0, 1, 0, 3000, 0, 0, 0, 0))

    assert ScanActivityHandler._observer_mode_valid(
        owner_private, effective_uid=1000, effective_groups={1000}
    )
    assert ScanActivityHandler._observer_mode_valid(
        root_group, effective_uid=1000, effective_groups={1000, 2000}
    )
    assert not ScanActivityHandler._observer_mode_valid(
        world_readable, effective_uid=1000, effective_groups={1000, 2000}
    )
    assert not ScanActivityHandler._observer_mode_valid(
        wrong_group, effective_uid=1000, effective_groups={1000, 2000}
    )


@pytest.mark.asyncio
async def test_scan_worker_returns_only_bounded_observer_attestation(tmp_path: Path) -> None:
    credential_root = tmp_path / "credentials"
    credential_root.mkdir()
    kubeconfig = credential_root / "observer.kubeconfig"
    kubeconfig.write_text("bounded-test-fixture", encoding="utf-8")
    kubeconfig.chmod(0o600)
    run_root = tmp_path / "runs"
    (run_root / "scan").mkdir(parents=True)
    handler = ScanActivityHandler(
        Settings(observer_credential_root=credential_root, run_root=run_root),
        observer_runner=AttestationObserverRunner(),  # type: ignore[arg-type]
    )
    handler.catalog.project = lambda _project: SimpleNamespace(  # type: ignore[method-assign]
        deployment=SimpleNamespace(
            observer_credential_ref=f"file:{kubeconfig}",
            kubernetes_context_ref="falcone-observer",
            api_server_identity="https://cluster.example.test",
            namespaces=["falcone-staging"],
            argocd_application=None,
        )
    )

    result = await handler.handle("identity.observer_attest", {"project": "falcone"})
    attestation = ObserverReadinessAttestation.model_validate(result)

    assert attestation.ready()
    serialized = json.dumps(result).lower()
    assert "kubeconfig" not in serialized
    assert "serviceaccount:test" not in serialized
    assert "bearer" not in serialized
    assert "token" not in serialized


@pytest.mark.asyncio
async def test_controller_plan_consumes_worker_observer_attestation(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    attestations = tmp_path / "attestations"
    attestations.mkdir()
    settings = Settings(
        database_url="sqlite://",
        project_config_root=ROOT / "config/projects",
        persona_config_root=ROOT / "config/personas",
        flow_config_root=ROOT / "config/flows",
        credential_root=credentials,
        credential_attestation_root=attestations,
        scan_worker_identity=f"scan-runner@{os.uname().nodename}",
    )
    handler = EngineeringActivityHandler(settings)

    result = await handler.handle(
        "campaign.plan",
        {
            "project": "falcone",
            "preset": "snapshot",
            "environment": "staging",
            "observer_attestation": observer_attestation(),
        },
    )

    assert result["blockers"] == []
    assert result["kubernetes_readiness"] == "OPERATIONAL"
    observer = next(
        item
        for item in result["credential_readiness"]
        if item["reference"] == "worker-attestation:falcone-observer"
    )
    assert observer["worker_attested"] is True


@pytest.mark.asyncio
async def test_controller_activity_redaction_preserves_observer_denial_booleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    attestations = tmp_path / "attestations"
    attestations.mkdir()
    monkeypatch.setattr("engineering_control.activities.core.activity.heartbeat", lambda _: None)
    monkeypatch.setattr(
        "engineering_control.activities.core.MetadataActivityHandler", lambda _settings: None
    )
    activities = ControlActivities(
        Settings(
            database_url="sqlite://",
            project_config_root=ROOT / "config/projects",
            persona_config_root=ROOT / "config/personas",
            flow_config_root=ROOT / "config/flows",
            credential_root=credentials,
            credential_attestation_root=attestations,
            scan_worker_identity=f"scan-runner@{os.uname().nodename}",
        ),
        "controller",
    )

    result = await activities.execute_control_step(
        {
            "step": "campaign.plan",
            "payload": {
                "project": "falcone",
                "preset": "snapshot",
                "environment": "staging",
                "observer_attestation": observer_attestation(),
            },
        }
    )

    assert result["blockers"] == []
    assert result["kubernetes_readiness"] == "OPERATIONAL"
    assert result["observer_attestation"]["forbidden_resource_read_denied"] is True
    assert result["observer_attestation"]["mutation_access_denied"] is True


class ExecuteWorkflowClient:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.workflow_input: Any = None
        self.task_queue = ""

    async def execute_workflow(
        self, _workflow: str, workflow_input: Any, *, task_queue: str, **_kwargs: Any
    ) -> dict[str, Any]:
        self.workflow_input = workflow_input
        self.task_queue = task_queue
        return self.result


@pytest.mark.asyncio
async def test_api_plan_uses_existing_audit_workflow_in_plan_only_mode(tmp_path: Path) -> None:
    catalog = EngineeringCatalog(
        project_root=ROOT / "config/projects",
        persona_root=ROOT / "config/personas",
        flow_root=ROOT / "config/flows",
        credential_root=tmp_path,
        attestation_root=tmp_path,
    )
    expected = catalog.plan("falcone", "snapshot", "staging").model_dump(mode="json")
    client = ExecuteWorkflowClient(expected)
    service = TemporalControlService(
        client,  # type: ignore[arg-type]
        "engineering-control",
        EmptyMetadataRepository(),
    )

    result = await service.plan_campaign(
        AuditPlanRequest(project=Project.FALCONE, preset="snapshot", environment="staging")
    )

    assert result == expected
    assert client.workflow_input.plan_only is True
    assert client.workflow_input.dry_run is True
    assert client.task_queue == "engineering-control"


def test_observer_attestation_is_scan_worker_only() -> None:
    assert "identity.observer_attest" in ControlActivities.steps_for_worker("scan")
    for worker in ("controller", "browser", "codex", "claude", "delivery", "release", "github"):
        assert "identity.observer_attest" not in ControlActivities.steps_for_worker(worker)
