"""NEW-234 PR5: the decomposition plan's routing, worker allowlists, workflow registration,
scan-worker inventory and staging, the read-only GitHub issue snapshot, the API (project
gating, approval binding refusals) and the CLI."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from new_234_decomposition_fixtures import SOURCE_SHA, backlog, files, issues
from new_234_plan_fixtures import PlanHarness, drive
from temporalio.exceptions import ApplicationError

from engineering_control.activities.core import ControlActivities
from engineering_control.activities.decomposition_scan import DecompositionScanSteps
from engineering_control.activities.github import GitHubActivityHandler
from engineering_control.activities.provider import ProviderActivityHandler
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.adapters.github.client import GhCliBroker, GhCliPolicyError
from engineering_control.api.main import create_app
from engineering_control.api.schemas import OperationAccepted
from engineering_control.config import Settings
from engineering_control.delivery_notifications import MILESTONE_EVENTS
from engineering_control.engineering.decomposition import (
    APPROVAL_PHRASE,
    MAX_PART_DRAFTS,
    MAX_PART_REVIEWS,
    decomposition_pack_problems,
    pack_revision,
    part_content_sha256,
    plan_json,
)
from engineering_control.workflows.definitions import (
    ACTIVITY_RETRY,
    ALL_WORKFLOWS,
    SINGLE_ATTEMPT,
    BacklogDecompositionPlanWorkflow,
    ControlledWorkflow,
    UiMigrationPlanWorkflow,
)
from engineering_control.workflows.names import WorkflowName
from engineering_control.workflows.worker_registry import GITHUB_TASK_QUEUE, ROLE_TASK_QUEUES

TOKEN = "x" * 32
CLI = {"Authorization": f"Bearer {TOKEN}", "X-Engineering-Actor": "engineering-ctl:hermes"}
DECOMPOSITION_ID = "22222222-2222-4222-8222-222222222222"
CONTROLLER_STEPS = (
    "decomposition.register",
    "decomposition.record_inputs",
    "decomposition.prepare_part_input",
    "decomposition.prepare_part",
    "decomposition.record_part_draft",
    "decomposition.prepare_part_review",
    "decomposition.record_part_review",
    "decomposition.assemble",
    "decomposition.persist",
    "decomposition.record_terminal",
    "decomposition.finalize_run",
)
SCAN_STEPS = (
    "decomposition.inventory",
    "decomposition.stage_inputs",
    "decomposition.stage_document",
)


# Routing, allowlists and registration -----------------------------------------------------


def test_decomposition_steps_route_to_fixed_providers_and_workers() -> None:
    queue = ControlledWorkflow._activity_queue
    assert queue("decomposition.draft", {"provider": "codex"}) == ROLE_TASK_QUEUES["claude"]
    assert queue("decomposition.review", {"provider": "claude"}) == ROLE_TASK_QUEUES["codex"]
    for step in SCAN_STEPS:
        assert queue(step, {}) == ROLE_TASK_QUEUES["scan"]
    assert queue("decomposition.read_issues", {}) == GITHUB_TASK_QUEUE
    for step in CONTROLLER_STEPS:
        assert queue(step, {}) == "engineering-control"


def test_decomposition_steps_are_allowed_only_on_their_workers() -> None:
    steps = {
        kind: ControlActivities.steps_for_worker(kind) for kind in ControlActivities._worker_steps
    }
    assert set(CONTROLLER_STEPS) <= steps["controller"]
    assert not {"decomposition.draft", "decomposition.review", "decomposition.read_issues",
                *SCAN_STEPS} & steps["controller"]  # fmt: skip
    assert {step for step in steps["claude"] if step.startswith("decomposition.")} == {
        "decomposition.draft"
    }
    assert {step for step in steps["codex"] if step.startswith("decomposition.")} == {
        "decomposition.review"
    }
    assert {step for step in steps["scan"] if step.startswith("decomposition.")} == set(SCAN_STEPS)
    assert set(SCAN_STEPS) <= ScanActivityHandler.supported_steps
    assert {step for step in steps["github"] if step.startswith("decomposition.")} == {
        "decomposition.read_issues"
    }
    assert "decomposition.read_issues" in GitHubActivityHandler.supported_steps
    for kind in ("browser", "delivery", "release"):
        assert not any(step.startswith("decomposition.") for step in steps[kind])


def test_the_plan_workflow_is_a_new_registered_type_beside_the_ui_workflows() -> None:
    assert WorkflowName.BACKLOG_DECOMPOSITION_PLAN.value == "BacklogDecompositionPlanWorkflow"
    assert BacklogDecompositionPlanWorkflow in ALL_WORKFLOWS
    assert UiMigrationPlanWorkflow in ALL_WORKFLOWS
    assert len({cls.__name__ for cls in ALL_WORKFLOWS}) == len(ALL_WORKFLOWS)
    assert "DECOMPOSITION_READY" in MILESTONE_EVENTS
    # The deterministic workflow keeps its own copy of the design section 3 budget.
    assert BacklogDecompositionPlanWorkflow._MAX_PART_DRAFTS == MAX_PART_DRAFTS == MAX_PART_REVIEWS


# Scan worker -----------------------------------------------------------------------------


class FakeManager:
    def __init__(self) -> None:
        self.granted: list[tuple[Path, tuple[str, ...]]] = []
        self.inspected: list[tuple[Path, str]] = []

    def grant_model_read(self, target: Path, identities: tuple[str, ...]) -> None:
        self.granted.append((target, identities))

    def inspect_snapshot(self, path: Path, *, expected_sha: str) -> dict[str, Any]:
        self.inspected.append((path, expected_sha))
        return {}


class Scan(DecompositionScanSteps):
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


def _scan(tmp_path: Path, *, provisioned: bool = True) -> Scan:
    settings = Settings(evidence_root=tmp_path / "evidence", worktree_root=tmp_path / "worktrees")
    if provisioned:
        (settings.worktree_root / "ui-plan-inputs").mkdir(parents=True)
    return Scan(settings)


def _snapshot(tmp_path: Path, **overrides: bytes) -> Path:
    root = tmp_path / "worktrees" / "falcone-serverless" / "snapshot"
    for relative, data in files(**overrides).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def _issues(document: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "number": item.number,
            "title": item.title,
            "body": item.body,
            "state": item.state,
            "labels": list(item.labels),
            "parent": item.parent,
        }
        for item in issues(document)
    ]


def test_the_scan_worker_builds_the_inventory_from_the_pinned_snapshot(tmp_path: Path) -> None:
    scan = _scan(tmp_path)
    root = _snapshot(tmp_path)
    payload = {
        "project": "falcone-serverless",
        "snapshot": {"source_worktree": str(root), "source_sha": SOURCE_SHA},
        "issues": _issues(),
    }
    result = scan._decomposition_inventory(payload)
    assert result["status"] == "INVENTORY_PASS"
    assert result["inventory"]["source_sha"] == SOURCE_SHA
    assert result["summary"] == {**result["summary"], "epics": 2, "stories": 3, "references": 1}
    assert scan.manager.inspected == [(root, SOURCE_SHA)]  # type: ignore[attr-defined]
    edited = backlog()
    edited["issues"][1]["body"] += "\nEdited on GitHub only."
    drift = scan._decomposition_inventory({**payload, "issues": _issues(edited)})
    assert (drift["status"], drift["blocker"]) == ("BLOCKED", "DECOMPOSITION_SOURCE_DRIFT")
    bad = scan._decomposition_inventory({**payload, "issues": [{"number": "3"}]})
    assert (bad["status"], bad["blocker"]) == ("BLOCKED", "DECOMPOSITION_SOURCE_INVALID")
    leaky = _snapshot(tmp_path / "leaky", **{"AGENTS.md": b"# AGENTS\n\ntoken: abcdef123456\n"})
    secret = scan._decomposition_inventory(
        {**payload, "snapshot": {"source_worktree": str(leaky), "source_sha": SOURCE_SHA}}
    )
    assert (secret["status"], secret["blocker"]) == ("BLOCKED", "DECOMPOSITION_SOURCE_INVALID")
    with pytest.raises(ValueError, match="only for falcone-serverless"):
        scan._decomposition_inventory({**payload, "project": "falcone"})


def test_the_scan_worker_stages_inputs_and_documents_and_publishes_the_plan(
    tmp_path: Path,
) -> None:
    scan = _scan(tmp_path)
    base = {"decomposition_id": DECOMPOSITION_ID, "project": "falcone-serverless"}
    part = "ep02-" + "a" * 16
    staged = scan._decomposition_stage_inputs(
        {**base, "files": {f"part-{part}-input.json": "{}\n"}}
    )
    directory = tmp_path / "worktrees" / "ui-plan-inputs" / DECOMPOSITION_ID
    assert staged["status"] == "STAGED" and staged["input_directory"] == str(directory)
    assert scan.manager.granted == [(directory, ("codex-runner", "claude-runner"))]  # type: ignore[attr-defined]
    oversized = scan._decomposition_stage_inputs(
        {**base, "files": {f"part-{part}-input.json": "x" * (64 * 1024 + 1)}}
    )
    assert (oversized["status"], oversized["blocker"]) == (
        "BLOCKED",
        "DECOMPOSITION_INPUT_TOO_LARGE",
    )
    draft = {"status": "COMPLETE", "epic": {"stories": []}}
    document = scan._decomposition_stage_document(
        {**base, "name": f"part-{part}-draft-1.json", "document": draft}
    )["document"]
    written = json.loads(Path(document["path"]).read_text())
    assert document["part_sha256"] == part_content_sha256(written["epic"])
    for name in ("../x.json", f"part-{part}-draft-4.json", "draft-1.json"):
        with pytest.raises(ValueError, match="name is invalid"):
            scan._decomposition_stage_document({**base, "name": name, "document": draft})
    plan = {"schema": "backlog-decomposition-v1", "stories": []}
    published = scan._decomposition_stage_document(
        {**base, "publish": True, "document": plan, "markdown": "# Backlog decomposition\n"}
    )
    assert [item["filename"] for item in published["evidence"]] == [
        "backlog-decomposition.json",
        "backlog-decomposition.md",
    ]
    stored = (
        tmp_path / "evidence" / "scan" / "backlog-decomposition" / DECOMPOSITION_ID
        / "backlog-decomposition.json"
    )  # fmt: skip
    assert stored.read_text() == plan_json(plan)


def test_staging_fails_closed_without_the_provisioned_root(tmp_path: Path) -> None:
    scan = _scan(tmp_path, provisioned=False)
    result = scan._decomposition_stage_inputs(
        {
            "decomposition_id": DECOMPOSITION_ID,
            "files": {"part-frame-" + "a" * 16 + "-input.json": "{}"},
        }
    )
    assert (result["status"], result["blocker"]) == ("BLOCKED", "UI_PLAN_INPUT_DIRECTORY_MISSING")
    assert not (tmp_path / "worktrees" / "ui-plan-inputs").exists()


# GitHub issue snapshot -------------------------------------------------------------------


def _broker(tmp_path: Path, pages: list[Any]) -> tuple[GhCliBroker, list[list[str]]]:
    broker = GhCliBroker(Mock(), tmp_path, authenticated_home=tmp_path)
    calls: list[list[str]] = []
    outputs = [json.dumps(page) for page in pages]

    def run(arguments: list[str], **_kwargs: Any) -> Any:
        calls.append(arguments)
        return SimpleNamespace(stdout=outputs.pop(0))

    broker._batch = lambda repository: repository  # type: ignore[method-assign]
    broker._run = run  # type: ignore[method-assign]
    return broker, calls


def _page(nodes: list[dict[str, Any]], cursor: str | None) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "issues": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                }
            }
        }
    }


def test_the_issue_snapshot_is_read_only_paginated_and_keeps_parents(tmp_path: Path) -> None:
    node = {"number": 3, "title": "t", "body": "b", "state": "OPEN",
            "labels": {"nodes": [{"name": "type:story"}]}, "parent": {"number": 1}}  # fmt: skip
    broker, calls = _broker(tmp_path, [_page([node], "Y3Vyc29yOjE="), _page([], None)])
    listed = broker.list_issue_snapshot("gntik-ai/falcone-serverless")
    assert listed == [
        {"number": 3, "title": "t", "body": "b", "state": "OPEN", "labels": ["type:story"],
         "parent": 1}
    ]  # fmt: skip
    assert [call[:2] for call in calls] == [["api", "graphql"], ["api", "graphql"]]
    assert "-f" in calls[1] and "cursor=Y3Vyc29yOjE=" in calls[1]
    assert all("mutation" not in " ".join(call) for call in calls)
    broker, _calls = _broker(tmp_path, [{"data": {}}])
    with pytest.raises(GhCliPolicyError, match="malformed"):
        broker.list_issue_snapshot("gntik-ai/falcone-serverless")


def test_the_github_step_keeps_only_marked_issues_of_a_decomposition_project(
    tmp_path: Path,
) -> None:
    handler = GitHubActivityHandler.__new__(GitHubActivityHandler)
    marked = {
        "number": 1,
        "title": "e",
        "body": "<!-- falcone-serverless:EP-API -->\n",
        "state": "OPEN",
        "labels": ["type:epic"],
        "parent": None,
    }
    other = {"number": 9, "body": "<!-- hermes-decomposition plan=x task=y -->", "title": "t"}
    handler.broker = Mock(list_issue_snapshot=Mock(return_value=[marked, other]))
    result = handler._read_decomposition_issues({"project": "falcone-serverless"})
    assert result == {
        "status": "ISSUES_READ",
        "repository": "gntik-ai/falcone-serverless",
        "issues": [marked],
        "listed": 2,
    }
    leaky = {**marked, "number": 4, "body": marked["body"] + "token: abcdef123456\n"}
    handler.broker = Mock(list_issue_snapshot=Mock(return_value=[marked, leaky]))
    refused = handler._read_decomposition_issues({"project": "falcone-serverless"})
    # Only the blocker and the issue numbers leave the activity, never the text.
    assert refused == {
        "status": "BLOCKED",
        "blocker": "DECOMPOSITION_ISSUES_CREDENTIAL_TEXT",
        "reason": "credential-like text in issues 4",
        "issues": [4],
    }
    for field, value in (
        ("labels", ["type:story", "token=ghp_abcdefghijklmnopqrstuvwxyz0123"]),
        ("title", "Bearer abcdefghijklmnopqrstuvwxyz"),
        ("state", "password: hunter2hunter2"),
    ):
        tainted = {**marked, "number": 7, "state": "OPEN", "labels": [], field: value}
        handler.broker = Mock(list_issue_snapshot=Mock(return_value=[tainted]))
        result = handler._read_decomposition_issues({"project": "falcone-serverless"})
        assert result["blocker"] == "DECOMPOSITION_ISSUES_CREDENTIAL_TEXT", field
        assert result["issues"] == [7] and "abcdef" not in json.dumps(result)
    # Only allowlisted fields are returned (an extra field never reaches history).
    extra = {**marked, "state": "OPEN", "labels": [], "parent": None, "milestone": "M1 token"}
    handler.broker = Mock(list_issue_snapshot=Mock(return_value=[extra]))
    kept = handler._read_decomposition_issues({"project": "falcone-serverless"})
    assert kept["status"] == "ISSUES_READ"
    assert set(kept["issues"][0]) == {"number", "title", "body", "state", "labels", "parent"}
    malformed = {**marked, "number": "1"}
    handler.broker = Mock(list_issue_snapshot=Mock(return_value=[malformed]))
    assert (
        handler._read_decomposition_issues({"project": "falcone-serverless"})["blocker"]
        == "DECOMPOSITION_ISSUES_UNAVAILABLE"
    )
    handler.broker = Mock(list_issue_snapshot=Mock(side_effect=GhCliPolicyError("x")))
    blocked = handler._read_decomposition_issues({"project": "falcone-serverless"})
    assert blocked["blocker"] == "DECOMPOSITION_ISSUES_UNAVAILABLE"
    with pytest.raises(ApplicationError, match="decomposition project"):
        handler._read_decomposition_issues({"project": "falcone"})


# API -------------------------------------------------------------------------------------


@pytest.fixture
def api(tmp_path: Path) -> tuple[TestClient, Mock, PlanHarness]:
    harness = PlanHarness(tmp_path)
    fake = Mock()
    fake.start_backlog_decomposition = AsyncMock(
        side_effect=lambda identity, project, actor, source_sha: OperationAccepted(
            workflow_id=f"backlog-decomposition-{project}-{identity}", run_id="run"
        )
    )
    # NEW-234 PR6: an approval starts (a replay joins) the fan-out.
    fake.start_decomposition_fanout = AsyncMock(
        side_effect=lambda identity, max_items, replace, workflow_id: OperationAccepted(
            workflow_id=workflow_id, run_id="fanout-run"
        )
    )
    fake.ui_fanout_state = AsyncMock(
        side_effect=lambda workflow_id: (
            "RUNNING",
            OperationAccepted(workflow_id=workflow_id, run_id="fanout-run"),
        )
    )
    app = create_app(
        settings=harness.settings,
        service=fake,
        token=TOKEN,
        operations_repository=Mock(),
        command_bus=Mock(dispatch_pending=AsyncMock(return_value=False)),
        decomposition_repository=harness.repository,
    )
    return TestClient(app), fake, harness


def test_the_api_starts_a_plan_for_a_decomposition_project_only(api: Any) -> None:
    client, fake, harness = api
    with client:
        response = client.post(
            "/v1/backlog-decompositions",
            json={"project": "falcone-serverless", "epics": ["EP-RUN", "1"]},
            headers=CLI,
        )
        assert response.status_code == 202, response.text
        identity = UUID(response.json()["decomposition_id"])
        fake.start_backlog_decomposition.assert_awaited_once_with(
            identity, "falcone-serverless", "cli:engineering-ctl", None
        )
        status = client.get(f"/v1/backlog-decompositions/{identity}", headers=CLI).json()
        assert (status["status"], status["requested_epics"]) == ("DRAFTING", ["1", "EP-RUN"])
        assert (
            client.get(f"/v1/backlog-decompositions/{identity}/markdown", headers=CLI).status_code
            == 409
        )
        for body in (
            {"project": "falcone"},
            {"project": "falcone-serverless", "source_sha": "main"},
            {"project": "falcone-serverless", "epics": ["api"]},
        ):
            assert (
                client.post("/v1/backlog-decompositions", json=body, headers=CLI).status_code == 422
            )
        listed = client.get("/v1/backlog-decompositions?status=DRAFTING", headers=CLI).json()
        assert [item["id"] for item in listed["items"]] == [str(identity)]
        assert (
            client.get(f"/v1/backlog-decompositions/{DECOMPOSITION_ID}", headers=CLI).status_code
            == 404
        )
    # The repository refuses another project too (gating below the API schema).
    with pytest.raises(ValueError, match="configured only for falcone-serverless"):
        harness.repository.create_sync("falcone", "cli:engineering-ctl", None)


def test_a_failed_start_leaves_a_failed_record(api: Any) -> None:
    client, fake, harness = api
    fake.start_backlog_decomposition.side_effect = RuntimeError("temporal down")
    with client:
        response = client.post(
            "/v1/backlog-decompositions", json={"project": "falcone-serverless"}, headers=CLI
        )
    assert response.status_code == 503
    [row] = harness.repository.list_sync(None, None, 10)
    assert (row["status"], row["error_code"]) == ("FAILED", "WORKFLOW_START_FAILED")


def test_the_api_approves_only_the_bound_candidate_and_records_the_decision(api: Any) -> None:
    client, _fake, harness = api
    created = harness.create()
    result = drive(harness, created["id"])
    binding = result["approval_binding"]
    route = f"/v1/backlog-decompositions/{created['id']}"
    with client:
        shown = client.get(f"{route}/markdown", headers=CLI).json()
        assert shown["markdown"].startswith("# Backlog decomposition: falcone-serverless")
        assert shown["usage"]["total"]["calls"] == 6
        mismatch = client.post(
            f"{route}/approve",
            json={"approval_binding": "0" * 64, "typed_confirmation": APPROVAL_PHRASE},
            headers=CLI,
        )
        assert mismatch.status_code == 409 and "binding does not match" in mismatch.text
        wrong = client.post(
            f"{route}/approve",
            json={"approval_binding": binding, "typed_confirmation": "APPROVE_UI_BLUEPRINT"},
            headers=CLI,
        )
        assert wrong.status_code == 422
        approved = client.post(
            f"{route}/approve",
            json={"approval_binding": binding, "typed_confirmation": APPROVAL_PHRASE},
            headers=CLI,
        )
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert (body["status"], body["replayed"], body["decided_by"]) == (
            "APPROVED",
            False,
            "cli:engineering-ctl",
        )
        # NEW-234 PR6: the approval starts the fan-out (default cap 250), a replay joins it.
        assert body["fanout"] == {
            "workflow_id": f"backlog-decomposition-fanout-{created['id']}-1",
            "run_id": "fanout-run",
            "max_items": 250,
            "replace": False,
            "joined": False,
        }
        again = client.post(
            f"{route}/approve",
            json={"approval_binding": binding, "typed_confirmation": APPROVAL_PHRASE},
            headers=CLI,
        )
        assert again.json()["replayed"] is True
        assert again.json()["fanout"]["joined"] is True


def test_the_api_refuses_approval_after_the_agent_pack_changed(api: Any) -> None:
    client, _fake, harness = api
    created = harness.create()
    binding = drive(harness, created["id"])["approval_binding"]
    role = harness.pack / "roles" / "backlog-decomposer.yaml"
    role.write_text(role.read_text() + "purpose: changed\n")
    with client:
        response = client.post(
            f"/v1/backlog-decompositions/{created['id']}/approve",
            json={"approval_binding": binding, "typed_confirmation": APPROVAL_PHRASE},
            headers=CLI,
        )
    assert response.status_code == 409 and "DECOMPOSITION_PACK_DRIFT" in response.text
    assert harness.repository.get_sync(UUID(created["id"]))["status"] == "CANDIDATE"  # type: ignore[index]


def test_the_api_refuses_approval_when_only_a_released_schema_changed(api: Any) -> None:
    client, _fake, harness = api
    created = harness.create()
    binding = drive(harness, created["id"])["approval_binding"]
    before = pack_revision(harness.pack)
    schema = harness.pack / "skills" / "review-decomposition" / "references" / "output.schema.json"
    released = json.loads(schema.read_text())
    released["properties"]["blockers"]["maxItems"] = 6
    schema.write_text(json.dumps(released))
    # The released bytes are bound into the revision, and the pack check fails too.
    assert pack_revision(harness.pack) != before
    assert decomposition_pack_problems(harness.pack)
    with client:
        response = client.post(
            f"/v1/backlog-decompositions/{created['id']}/approve",
            json={"approval_binding": binding, "typed_confirmation": APPROVAL_PHRASE},
            headers=CLI,
        )
    assert response.status_code == 409 and "DECOMPOSITION_PACK_INVALID" in response.text
    assert harness.repository.get_sync(UUID(created["id"]))["status"] == "CANDIDATE"  # type: ignore[index]


def test_decomposition_provider_calls_execute_once_per_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_new_114_bundle import _CapacityAdapter, _provider

    monkeypatch.setattr(
        "engineering_control.activities.provider.activity.heartbeat", lambda _: None
    )
    adapter = _CapacityAdapter(failures=10)
    handler, payload = _provider(tmp_path, adapter)
    result = asyncio.run(
        handler.handle(
            "decomposition.review",
            {"review_request": {**payload["maker_request"], "writable": False}},
        )
    )
    # No capacity retry: the reserved call runs once (the UI and delivery steps keep theirs).
    assert result["status"] == "FAILED" and len(adapter.requests) == 1
    assert ProviderActivityHandler._single_execution_steps == {
        "decomposition.draft",
        "decomposition.review",
    }
    # Existing callers keep the default activity retry; the plan passes a single attempt.
    default = inspect.signature(ControlledWorkflow._agent_step).parameters["retry_policy"]
    assert default.default is ACTIVITY_RETRY and ACTIVITY_RETRY.maximum_attempts == 5
    assert SINGLE_ATTEMPT.maximum_attempts == 1


# CLI -------------------------------------------------------------------------------------


class RecordingClient:
    def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        return {"method": method, "path": path, "payload": payload}


def test_cli_decomposition_commands_use_only_the_decomposition_routes() -> None:
    from engineering_control.cli import dispatch, parser

    identifier = "11111111-1111-4111-8111-111111111111"
    route = "/v1/backlog-decompositions"
    cases = {
        ("backlog-decompose", "falcone-serverless"): (
            "POST",
            route,
            {"project": "falcone-serverless"},
        ),
        (
            "backlog-decompose",
            "falcone-serverless",
            "--source-sha",
            "a" * 40,
            "--epic",
            "EP-API",
            "--epic",
            "2",
        ): (
            "POST",
            route,
            {"project": "falcone-serverless", "source_sha": "a" * 40, "epics": ["EP-API", "2"]},
        ),
        ("backlog-decompositions", "--status", "CANDIDATE"): (
            "GET",
            f"{route}?status=CANDIDATE",
            None,
        ),
        ("backlog-decomposition-status", identifier): ("GET", f"{route}/{identifier}", None),
        ("backlog-decomposition-show", identifier): (
            "GET",
            f"{route}/{identifier}/markdown",
            None,
        ),
        ("backlog-decomposition-approve", identifier, "b" * 64, APPROVAL_PHRASE): (
            "POST",
            f"{route}/{identifier}/approve",
            # NEW-234 PR6: the fan-out's --replace (default off) travels with the approval.
            {"approval_binding": "b" * 64, "typed_confirmation": APPROVAL_PHRASE, "replace": False},
        ),
    }
    for argv, (method, path, payload) in cases.items():
        result = dispatch(RecordingClient(), parser().parse_args(list(argv)))  # type: ignore[arg-type]
        assert result == {"method": method, "path": path, "payload": payload}, argv
    with pytest.raises(SystemExit):
        parser().parse_args(["backlog-decompose", "falcone"])
