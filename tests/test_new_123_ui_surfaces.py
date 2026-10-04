"""NEW-123: routing, worker allowlists, scan input staging, the API and the CLI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from new_123_fixtures import INVENTORY_SHA, KIT_SHA, KIT_VERSION, SOURCE_SHA, blueprint
from sqlalchemy import create_engine

from engineering_control.activities.core import ControlActivities
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.activities.ui_plan_scan import UiPlanScanSteps
from engineering_control.api.main import create_app
from engineering_control.api.schemas import OperationAccepted
from engineering_control.config import Settings
from engineering_control.db import models  # noqa: F401
from engineering_control.db.base import Base
from engineering_control.db.change_requests import ChangeRequestRepository
from engineering_control.db.ui_blueprints import UiBlueprintRepository
from engineering_control.engineering.ui_blueprint import APPROVAL_PHRASE
from engineering_control.workflows.definitions import ControlledWorkflow
from engineering_control.workflows.worker_registry import ROLE_TASK_QUEUES

TOKEN = "x" * 32
CLI = {"Authorization": f"Bearer {TOKEN}", "X-Engineering-Actor": "engineering-ctl:hermes"}


# Routing and allowlists -----------------------------------------------------------------


def test_ui_steps_route_to_fixed_opposite_providers_and_the_scan_worker() -> None:
    queue = ControlledWorkflow._activity_queue
    assert queue("ui_plan.draft", {"provider": "codex"}) == ROLE_TASK_QUEUES["claude"]
    assert queue("ui_fanout.gap_draft", {}) == ROLE_TASK_QUEUES["claude"]
    assert queue("ui_plan.review", {"provider": "claude"}) == ROLE_TASK_QUEUES["codex"]
    assert queue("change_request.review", {}) == ROLE_TASK_QUEUES["codex"]
    for step in ("ui_plan.stage_inputs", "ui_plan.stage_document", "ui.kit_snapshot"):
        assert queue(step, {}) == ROLE_TASK_QUEUES["scan"]
    assert queue("ui_plan.persist", {}) == "engineering-control"


def test_ui_steps_are_allowed_only_on_their_workers() -> None:
    controller = ControlActivities.steps_for_worker("controller")
    for step in ("ui_plan.register", "ui_plan.persist", "ui_fanout.plan", "ui_fanout.finalize"):
        assert step in controller
    for step in ("ui_plan.draft", "ui_plan.review", "ui_fanout.gap_draft", "ui_plan.stage_inputs"):
        assert step not in controller
    assert {"ui_plan.draft", "ui_fanout.gap_draft"} <= ControlActivities.steps_for_worker("claude")
    assert "ui_plan.review" in ControlActivities.steps_for_worker("codex")
    assert "ui_plan.review" not in ControlActivities.steps_for_worker("claude")
    assert "ui_plan.draft" not in ControlActivities.steps_for_worker("codex")
    scan = ControlActivities.steps_for_worker("scan")
    assert {"ui_plan.stage_inputs", "ui_plan.stage_document"} <= scan
    assert {"ui_plan.stage_inputs", "ui_plan.stage_document"} <= ScanActivityHandler.supported_steps
    for kind in ("github", "browser", "delivery", "release"):
        assert not any(
            step.startswith(("ui_plan.", "ui_fanout."))
            for step in ControlActivities.steps_for_worker(kind)
        )


# Scan staging ------------------------------------------------------------------------------


class FakeManager:
    def __init__(self) -> None:
        self.granted: list[tuple[Path, tuple[str, ...]]] = []

    def grant_model_read(self, target: Path, identities: tuple[str, ...]) -> None:
        self.granted.append((target, identities))


class Staging(UiPlanScanSteps):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.manager = FakeManager()  # type: ignore[assignment]
        self.published: list[tuple[str, list[str]]] = []

    def _publish_reports(
        self,
        payload: dict[str, Any],
        reports: list[Path],
        *,
        filename_prefix: str,
        category: str = "changes",
    ) -> list[dict[str, Any]]:
        self.published.append((filename_prefix, [report.name for report in reports]))
        return [{"filename": report.name, "category": category} for report in reports]


BLUEPRINT_ID = "11111111-1111-4111-8111-111111111111"


def staging(tmp_path: Path) -> Staging:
    settings = Settings(evidence_root=tmp_path / "evidence", worktree_root=tmp_path / "worktrees")
    for kind, name, field, digest in (
        ("ui-inventory", "ui-inventory.json", "inventory_sha256", INVENTORY_SHA),
        ("ui-kit", "ui-kit-snapshot.json", "snapshot_sha256", KIT_SHA),
    ):
        directory = settings.evidence_root / "scan" / kind / digest[:32]
        directory.mkdir(parents=True)
        (directory / name).write_text(json.dumps({field: digest, "data": [1, 2]}))
    return Staging(settings)


def test_stage_inputs_copies_hash_verified_files_into_a_model_readable_directory(
    tmp_path: Path,
) -> None:
    handler = staging(tmp_path)
    result = handler._ui_plan_stage_inputs(
        {
            "blueprint_id": BLUEPRINT_ID,
            "inventory_sha256": INVENTORY_SHA,
            "kit_snapshot_sha256": KIT_SHA,
        }
    )
    directory = tmp_path / "worktrees" / "ui-plan-inputs" / BLUEPRINT_ID
    assert result["input_directory"] == str(directory)
    inventory = Path(result["files"]["inventory"]["path"])
    assert inventory.parent == directory
    assert (
        result["files"]["inventory"]["sha256"] == hashlib.sha256(inventory.read_bytes()).hexdigest()
    )
    assert (
        json.loads(Path(result["files"]["kit"]["path"]).read_text())["snapshot_sha256"] == KIT_SHA
    )
    assert handler.manager.granted == [(directory, ("codex-runner", "claude-runner"))]  # type: ignore[attr-defined]


def test_stage_inputs_refuses_evidence_that_does_not_match_its_digest(tmp_path: Path) -> None:
    handler = staging(tmp_path)
    forged = "e" * 64
    directory = tmp_path / "evidence" / "scan" / "ui-inventory" / forged[:32]
    directory.mkdir(parents=True)
    (directory / "ui-inventory.json").write_text(json.dumps({"inventory_sha256": INVENTORY_SHA}))
    with pytest.raises(ValueError, match="does not match"):
        handler._ui_plan_stage_inputs(
            {
                "blueprint_id": BLUEPRINT_ID,
                "inventory_sha256": forged,
                "kit_snapshot_sha256": KIT_SHA,
            }
        )
    link = tmp_path / "evidence" / "scan" / "ui-kit" / ("f" * 32)
    link.symlink_to(tmp_path / "evidence" / "scan" / "ui-kit" / KIT_SHA[:32])
    with pytest.raises(OSError):
        handler._ui_plan_stage_inputs(
            {
                "blueprint_id": BLUEPRINT_ID,
                "inventory_sha256": INVENTORY_SHA,
                "kit_snapshot_sha256": "f" * 64,
            }
        )


def test_stage_document_writes_named_inputs_and_publishes_the_blueprint(tmp_path: Path) -> None:
    handler = staging(tmp_path)
    staged = handler._ui_plan_stage_document(
        {"blueprint_id": BLUEPRINT_ID, "name": "draft-1.json", "document": {"a": 1}}
    )
    assert Path(staged["document"]["path"]).name == "draft-1.json"
    for name in ("../x.json", "draft-3.json", "notes.md"):
        with pytest.raises(ValueError, match="name is invalid"):
            handler._ui_plan_stage_document(
                {"blueprint_id": BLUEPRINT_ID, "name": name, "document": {}}
            )
    published = handler._ui_plan_stage_document(
        {
            "blueprint_id": BLUEPRINT_ID,
            "project": "falcone",
            "publish": True,
            "document": blueprint(),
            "markdown": "# UI migration blueprint: falcone\n",
        }
    )
    assert [item["filename"] for item in published["evidence"]] == [
        "ui-blueprint.json",
        "ui-blueprint.md",
    ]
    assert handler.published == [
        ("ui-blueprint-11111111", ["ui-blueprint.json", "ui-blueprint.md"])
    ]
    stored = tmp_path / "evidence" / "scan" / "ui-blueprint" / BLUEPRINT_ID / "ui-blueprint.json"
    assert json.loads(stored.read_text()) == blueprint()


# API -------------------------------------------------------------------------------------


def candidate(repository: UiBlueprintRepository) -> tuple[UUID, str]:
    created = repository.create_sync("falcone", "cli:engineering-ctl", None)
    blueprint_id = UUID(created["id"])
    repository.register_sync(blueprint_id, created["workflow_id"])
    repository.record_inputs_sync(
        blueprint_id,
        {
            "source_sha": SOURCE_SHA,
            "inventory_sha256": INVENTORY_SHA,
            "kit_snapshot_sha256": KIT_SHA,
            "gntik_ui_version": KIT_VERSION,
        },
    )
    digest = repository.record_draft_sync(blueprint_id, 1, blueprint(), [])
    persisted = repository.persist_candidate_sync(blueprint_id, digest, [], {})
    return blueprint_id, persisted["approval_binding"]


@pytest.fixture
def api(tmp_path: Path) -> tuple[TestClient, Mock, UiBlueprintRepository]:
    engine = create_engine(f"sqlite:///{tmp_path / 'api.sqlite'}")
    Base.metadata.create_all(engine)
    repository = UiBlueprintRepository(engine=engine)
    fake = Mock()
    fake.start_ui_plan = AsyncMock(
        side_effect=lambda blueprint_id, project, actor, source_sha: OperationAccepted(
            workflow_id=f"ui-plan-{project}-{blueprint_id}", run_id="run"
        )
    )
    fake.start_ui_fanout = AsyncMock(
        side_effect=lambda blueprint_id, max_items, sequence: OperationAccepted(
            workflow_id=f"ui-blueprint-fanout-{blueprint_id}-{sequence}", run_id="fan"
        )
    )
    app = create_app(
        settings=Settings(),
        service=fake,
        token=TOKEN,
        operations_repository=Mock(),
        command_bus=Mock(dispatch_pending=AsyncMock(return_value=False)),
        change_request_repository=ChangeRequestRepository(engine=engine),
        ui_blueprint_repository=repository,
    )
    return TestClient(app), fake, repository


def test_api_starts_a_plan_for_falcone_only(api: Any) -> None:
    client, fake, repository = api
    with client:
        response = client.post("/v1/ui-blueprints", json={"project": "falcone"}, headers=CLI)
        assert response.status_code == 202
        blueprint_id = UUID(response.json()["blueprint_id"])
        fake.start_ui_plan.assert_awaited_once_with(
            blueprint_id, "falcone", "cli:engineering-ctl", None
        )
        assert (
            client.get(f"/v1/ui-blueprints/{blueprint_id}", headers=CLI).json()["status"]
            == "DRAFTING"
        )
        assert (
            client.get(f"/v1/ui-blueprints/{blueprint_id}/markdown", headers=CLI).status_code == 409
        )
        assert (
            client.post("/v1/ui-blueprints", json={"project": "musematic"}, headers=CLI).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/ui-blueprints", json={"project": "falcone", "source_sha": "main"}, headers=CLI
            ).status_code
            == 422
        )
        listed = client.get("/v1/ui-blueprints?status=DRAFTING", headers=CLI).json()["items"]
        assert [item["id"] for item in listed] == [str(blueprint_id)]


def test_api_approval_is_bound_and_starts_the_capped_fanout(api: Any) -> None:
    client, fake, repository = api
    blueprint_id, binding = candidate(repository)
    with client:
        markdown = client.get(f"/v1/ui-blueprints/{blueprint_id}/markdown", headers=CLI).json()
        assert markdown["markdown"].startswith("# UI migration blueprint: falcone")
        wrong = client.post(
            f"/v1/ui-blueprints/{blueprint_id}/approve",
            json={"approval_binding": "0" * 64, "typed_confirmation": APPROVAL_PHRASE},
            headers=CLI,
        )
        assert wrong.status_code == 409
        fake.start_ui_fanout.assert_not_awaited()
        assert (
            client.post(
                f"/v1/ui-blueprints/{blueprint_id}/approve",
                json={"approval_binding": binding, "typed_confirmation": "APPROVE_CHANGE"},
                headers=CLI,
            ).status_code
            == 422
        )
        approved = client.post(
            f"/v1/ui-blueprints/{blueprint_id}/approve",
            json={
                "approval_binding": binding,
                "typed_confirmation": APPROVAL_PHRASE,
                "max_items": 5,
            },
            headers=CLI,
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "APPROVED"
        assert approved.json()["fanout"]["max_items"] == 5
        fake.start_ui_fanout.assert_awaited_once_with(blueprint_id, 5, 1)
        replay = client.post(
            f"/v1/ui-blueprints/{blueprint_id}/approve",
            json={"approval_binding": binding, "typed_confirmation": APPROVAL_PHRASE},
            headers=CLI,
        )
        assert replay.json()["replayed"] is True
        assert replay.json()["fanout"]["max_items"] == 80
        # A replay resumes as the next fan-out execution (its own workflow id and Run).
        fake.start_ui_fanout.assert_awaited_with(blueprint_id, 80, 2)
        assert replay.json()["fanout"]["workflow_id"].endswith("-2")


# CLI -------------------------------------------------------------------------------------


class RecordingClient:
    def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        return {"method": method, "path": path, "payload": payload}


def test_cli_ui_commands_use_only_the_ui_blueprint_routes() -> None:
    from engineering_control.cli import dispatch, parser

    identifier = "11111111-1111-4111-8111-111111111111"
    cases = {
        ("ui-plan", "falcone"): ("POST", "/v1/ui-blueprints", {"project": "falcone"}),
        ("ui-plan", "falcone", "--source-sha", "a" * 40): (
            "POST",
            "/v1/ui-blueprints",
            {"project": "falcone", "source_sha": "a" * 40},
        ),
        ("ui-plan-status", identifier): ("GET", f"/v1/ui-blueprints/{identifier}", None),
        ("ui-blueprint-show", identifier): (
            "GET",
            f"/v1/ui-blueprints/{identifier}/markdown",
            None,
        ),
        ("ui-blueprint-approve", identifier, "b" * 64, APPROVAL_PHRASE, "--max-items", "10"): (
            "POST",
            f"/v1/ui-blueprints/{identifier}/approve",
            {"approval_binding": "b" * 64, "typed_confirmation": APPROVAL_PHRASE, "max_items": 10},
        ),
        ("ui-blueprints", "--status", "CANDIDATE"): (
            "GET",
            "/v1/ui-blueprints?status=CANDIDATE",
            None,
        ),
    }
    for argv, (method, path, payload) in cases.items():
        result = dispatch(RecordingClient(), parser().parse_args(list(argv)))  # type: ignore[arg-type]
        assert result == {"method": method, "path": path, "payload": payload}, argv
    with pytest.raises(SystemExit):
        parser().parse_args(["ui-plan", "musematic"])
