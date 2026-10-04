"""NEW-123: routing, worker allowlists, scan input staging, the API and the CLI."""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
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
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.ui_blueprint import APPROVAL_PHRASE
from engineering_control.engineering.ui_inventory import inventory_hash
from engineering_control.engineering.ui_kit import snapshot_hash
from engineering_control.workflows.definitions import ControlledWorkflow
from engineering_control.workflows.worker_registry import ROLE_TASK_QUEUES

ROOT = Path(__file__).parents[1]
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
        self.digests: dict[str, str] = {}

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


def staging(tmp_path: Path, *, provisioned: bool = True) -> Staging:
    """Evidence written exactly as the producers write it (digest recomputed from content)."""
    settings = Settings(evidence_root=tmp_path / "evidence", worktree_root=tmp_path / "worktrees")
    inventory: dict[str, Any] = {"schema_version": 1, "pages": [{"file": "a.tsx"}]}
    inventory["inventory_sha256"] = inventory_hash(inventory)
    kit: dict[str, Any] = {"schema_version": 1, "calls": [{"tool": "list_kit", "text": "x"}]}
    kit["snapshot_sha256"] = snapshot_hash(kit)
    for kind, name, document, digest in (
        ("ui-inventory", "ui-inventory.json", inventory, inventory["inventory_sha256"]),
        ("ui-kit", "ui-kit-snapshot.json", kit, kit["snapshot_sha256"]),
    ):
        directory = settings.evidence_root / "scan" / kind / digest[:32]
        directory.mkdir(parents=True)
        (directory / name).write_text(json.dumps(document))
    if provisioned:
        (settings.worktree_root / "ui-plan-inputs").mkdir(parents=True)
    handler = Staging(settings)
    handler.digests = {
        "inventory_sha256": inventory["inventory_sha256"],
        "kit_snapshot_sha256": kit["snapshot_sha256"],
    }
    return handler


def test_bootstrap_provisions_the_input_root_for_scan_with_runner_traverse() -> None:
    script = (ROOT / "scripts" / "bootstrap-worker.sh").read_text()
    assert (
        "install -d -o scan-runner -g scan-runner -m 0700 "
        '"${PLATFORM_ROOT}/worktrees/ui-plan-inputs"' in script
    )
    assert "setfacl --modify u:codex-runner:--x,u:claude-runner:--x" in script
    unit = (ROOT / "systemd" / "engineering-worker@.service").read_text()
    # Inside the scan unit's existing writable root; read-only (ProtectSystem) for the others.
    assert " /srv/engineering/worktrees " in unit


def test_stage_inputs_copies_hash_verified_files_into_a_model_readable_directory(
    tmp_path: Path,
) -> None:
    handler = staging(tmp_path)
    result = handler._ui_plan_stage_inputs({"blueprint_id": BLUEPRINT_ID, **handler.digests})
    directory = tmp_path / "worktrees" / "ui-plan-inputs" / BLUEPRINT_ID
    assert result["input_directory"] == str(directory)
    inventory = Path(result["files"]["inventory"]["path"])
    assert inventory.parent == directory
    assert (
        result["files"]["inventory"]["sha256"] == hashlib.sha256(inventory.read_bytes()).hexdigest()
    )
    kit = json.loads(Path(result["files"]["kit"]["path"]).read_text())
    assert kit["snapshot_sha256"] == handler.digests["kit_snapshot_sha256"]
    assert handler.manager.granted == [(directory, ("codex-runner", "claude-runner"))]  # type: ignore[attr-defined]


def test_stage_inputs_fails_closed_without_the_provisioned_root(tmp_path: Path) -> None:
    handler = staging(tmp_path, provisioned=False)
    result = handler._ui_plan_stage_inputs({"blueprint_id": BLUEPRINT_ID, **handler.digests})
    assert (result["status"], result["blocker"]) == ("BLOCKED", "UI_PLAN_INPUT_DIRECTORY_MISSING")
    assert not (tmp_path / "worktrees" / "ui-plan-inputs").exists()
    staged = handler._ui_plan_stage_document(
        {"blueprint_id": BLUEPRINT_ID, "name": "draft-1.json", "document": {"blueprint": {}}}
    )
    assert staged["blocker"] == "UI_PLAN_INPUT_DIRECTORY_MISSING"


@pytest.mark.parametrize("label", ["inventory", "kit"])
def test_stage_inputs_recomputes_the_digest_from_the_content(tmp_path: Path, label: str) -> None:
    handler = staging(tmp_path)
    kind, name = (
        ("ui-inventory", "ui-inventory.json")
        if label == "inventory"
        else ("ui-kit", "ui-kit-snapshot.json")
    )
    digest = handler.digests["inventory_sha256" if label == "inventory" else "kit_snapshot_sha256"]
    path = tmp_path / "evidence" / "scan" / kind / digest[:32] / name
    altered = json.loads(path.read_text())
    altered["schema_version"] = 2  # content changed, embedded digest field kept
    path.write_text(json.dumps(altered))
    with pytest.raises(ValueError, match="does not match"):
        handler._ui_plan_stage_inputs({"blueprint_id": BLUEPRINT_ID, **handler.digests})


def test_stage_inputs_refuses_a_symlinked_evidence_directory(tmp_path: Path) -> None:
    handler = staging(tmp_path)
    kit = handler.digests["kit_snapshot_sha256"]
    link = tmp_path / "evidence" / "scan" / "ui-kit" / ("f" * 32)
    link.symlink_to(tmp_path / "evidence" / "scan" / "ui-kit" / kit[:32])
    with pytest.raises(OSError):
        handler._ui_plan_stage_inputs(
            {
                "blueprint_id": BLUEPRINT_ID,
                "inventory_sha256": handler.digests["inventory_sha256"],
                "kit_snapshot_sha256": "f" * 64,
            }
        )


def test_stage_document_writes_named_inputs_and_publishes_the_blueprint(tmp_path: Path) -> None:
    handler = staging(tmp_path)
    draft = {"mode": "draft", "blueprint": blueprint()}
    staged = handler._ui_plan_stage_document(
        {"blueprint_id": BLUEPRINT_ID, "name": "draft-1.json", "document": draft}
    )
    assert Path(staged["document"]["path"]).name == "draft-1.json"
    # Hermes hashes exactly the staged file the reviewer reads.
    written = json.loads(Path(staged["document"]["path"]).read_text())
    assert staged["document"]["blueprint_sha256"] == canonical_sha256(written["blueprint"])
    assert staged["document"]["blueprint_sha256"] == canonical_sha256(blueprint())
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
    repository.mark_reviewed_sync(blueprint_id, 1, digest)
    persisted = repository.persist_candidate_sync(blueprint_id, 1, digest, [], {})
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
        side_effect=lambda blueprint_id, max_items, workflow_id: OperationAccepted(
            workflow_id=workflow_id, run_id="fan"
        )
    )
    fake.running_ui_fanout = AsyncMock(return_value=None)
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
    first = f"ui-blueprint-fanout-{blueprint_id}-1"
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
        assert approved.json()["fanout"] == {
            "workflow_id": first,
            "run_id": "fan",
            "max_items": 5,
            "joined": False,
        }
        fake.start_ui_fanout.assert_awaited_once_with(blueprint_id, 5, first)

        def replay() -> dict[str, Any]:
            response = client.post(
                f"/v1/ui-blueprints/{blueprint_id}/approve",
                json={"approval_binding": binding, "typed_confirmation": APPROVAL_PHRASE},
                headers=CLI,
            )
            assert response.status_code == 200
            assert response.json()["replayed"] is True
            return dict(response.json()["fanout"])

        # While fan-out 1 runs, every replay joins it and consumes no sequence.
        fake.running_ui_fanout.return_value = OperationAccepted(workflow_id=first, run_id="r1")
        for _ in range(3):
            assert replay()["workflow_id"] == first
        assert fake.start_ui_fanout.await_count == 1
        assert repository.get_sync(blueprint_id)["fanout_sequence"] == 1  # type: ignore[index]
        # Once it ended (without clearing its marker), the next replay starts fan-out 2.
        fake.running_ui_fanout.return_value = None
        resumed = replay()
        assert (resumed["workflow_id"], resumed["joined"], resumed["max_items"]) == (
            f"ui-blueprint-fanout-{blueprint_id}-2",
            False,
            80,
        )
        assert fake.start_ui_fanout.await_count == 2
        view = repository.get_sync(blueprint_id)
        assert view is not None and view["fanout_workflow_id"] == resumed["workflow_id"]


def test_a_single_fanout_claim_wins_under_concurrent_approvals(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'claim.sqlite'}", connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    repository = UiBlueprintRepository(engine=engine)
    blueprint_id, binding = candidate(repository)
    repository.approve_sync(blueprint_id, binding, APPROVAL_PHRASE, "telegram:42")
    barrier = threading.Barrier(8)

    def claim() -> dict[str, Any]:
        barrier.wait()
        return repository.claim_fanout_sync(blueprint_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: claim(), range(8)))
    started = [item for item in claims if item["action"] == "START"]
    assert len(started) == 1
    assert {item["workflow_id"] for item in claims} == {started[0]["workflow_id"]}
    view = repository.get_sync(blueprint_id)
    assert view is not None and view["fanout_sequence"] == 1
    # Only the holder clears the marker; then exactly the next sequence is claimed.
    assert repository.release_fanout_sync(blueprint_id, "ui-blueprint-fanout-other-9") is False
    assert repository.release_fanout_sync(blueprint_id, started[0]["workflow_id"]) is True
    following = repository.claim_fanout_sync(blueprint_id)
    assert following == {
        "action": "START",
        "workflow_id": f"ui-blueprint-fanout-{blueprint_id}-2",
        "sequence": 2,
    }


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
