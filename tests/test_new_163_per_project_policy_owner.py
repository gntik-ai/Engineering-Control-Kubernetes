"""NEW-163: at most one GitHub write policy owner per project, capped in parallel.

Campaigns of different projects own the shared policy at the same time (default limit 2);
a second campaign of the same project is an inventory; a limit of 1 or an open
autonomous global backlog keeps the single global owner rule; the global backlog itself
stays exclusive; issue-start waits for another campaign's pending activation; and a
delivery write never leaves its project's repository pair.
"""

from __future__ import annotations

import json
import runpy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from engineering_control.activities.delivery import DeliveryActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import GitHubBacklogCampaign, Run
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.engineering.backlog import BACKLOG_POLICY_ACTIVATION_PENDING
from engineering_control.models.enums import Project
from engineering_control.operations.models import AutonomousBacklogCommand, SolveIssueCommand
from engineering_control.operations.repository import OperationsRepository
from engineering_control.operations.service import TypedCommandBus
from engineering_control.policy.github_write import WRITE_REPOSITORIES
from engineering_control.projects import PROJECT_REPOSITORIES

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = runpy.run_path(str(ROOT / "scripts/reconcile-backlog-github-policy.py"))
VALIDATE_CAMPAIGN = SCRIPT["validate_campaign"]
OTHER_OWNER = SCRIPT["other_unexpired_owner"]
EXPIRE = SCRIPT["expire_campaign_approvals"]
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
ACTOR = "cli:engineering-ctl"
HEADS = {
    "falcone": ("a", "b"),
    "musematic": ("c", "d"),
    "llmwiki": ("e", "e"),
    "gntik-ui": ("f", "f"),
}


def _repository(tmp_path: Path, limit: int = 2) -> SQLMetadataRepository:
    request_root = tmp_path / "policy-requests"
    request_root.mkdir(mode=0o700)
    repository = SQLMetadataRepository(
        f"sqlite:///{tmp_path / 'metadata.sqlite'}",
        github_policy_request_root=request_root,
        backlog_parallel_project_limit=limit,
    )
    Base.metadata.create_all(repository.engine)
    return repository


def _campaign(campaign_id: UUID, project: str) -> GitHubBacklogCampaign:
    source, deployment = PROJECT_REPOSITORIES[project]
    source_head, deployment_head = HEADS[project]
    return GitHubBacklogCampaign(
        id=campaign_id,
        project=project,
        source_repository=source,
        deployment_repository=deployment,
        imported_at=NOW,
        issue_query="state:open",
        import_filter="bugs-and-docs",
        source_default_branch="master" if project == "llmwiki" else "main",
        deployment_default_branch="master" if project == "llmwiki" else "main",
        source_head_at_import=source_head * 40,
        deployment_head_at_import=deployment_head * 40,
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
        workflow_id=f"backlog-import-{project}-{campaign_id}",
    )


async def _approve(
    repository: SQLMetadataRepository, campaign_id: UUID, project: str
) -> dict[str, Any]:
    from engineering_control.api.schemas import BacklogApprovalCandidateRequest

    with repository.sessions.begin() as session:
        if session.get(GitHubBacklogCampaign, campaign_id) is None:
            session.add(_campaign(campaign_id, project))
    candidate = await repository.prepare_backlog_approval(
        campaign_id,
        BacklogApprovalCandidateRequest(
            project=project,
            delivery_mode="source-pr",
            maximum_risk="R2",
            maximum_items=1,
        ),
    )
    result: dict[str, Any] = await repository.approve_backlog_campaign(
        campaign_id, project, candidate["approval_binding"], ACTOR
    )
    return result


def _take_request(repository: SQLMetadataRepository) -> dict[str, Any]:
    path = repository.github_policy_request_root / "activate.json"
    request: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()
    return request


def _reconcile(
    repository: SQLMetadataRepository, request: dict[str, Any], *, limit: int = 2
) -> None:
    """The root reconciler's enable transaction (main() minus the file and root checks)."""
    with repository.sessions.begin() as session:
        campaign = VALIDATE_CAMPAIGN(session, request, parallel_project_limit=limit)
        campaign.status = "APPROVED"


def _status(repository: SQLMetadataRepository, campaign_id: UUID) -> tuple[str, Any]:
    with repository.sessions() as session:
        campaign = session.get(GitHubBacklogCampaign, campaign_id)
        assert campaign is not None
        return campaign.status, campaign.approval_json.get("policy_role")


def _id(number: int) -> UUID:
    return UUID(f"{number:08d}-0000-4000-8000-000000000163")


async def _owner(repository: SQLMetadataRepository, number: int, project: str) -> UUID:
    """An approved owner, promoted by the reconciler with the default limit."""
    campaign_id = _id(number)
    approved = await _approve(repository, campaign_id, project)
    assert approved["status"] == "APPROVED_POLICY_PENDING"
    _reconcile(repository, _take_request(repository))
    return campaign_id


async def _pending_request(
    repository: SQLMetadataRepository, number: int, project: str
) -> dict[str, Any]:
    """A pending owner's activation request taken while no other owner is visible."""
    approved = await _approve(repository, _id(number), project)
    assert approved["status"] == "APPROVED_POLICY_PENDING"
    return _take_request(repository)


def _open_global_backlog(repository: SQLMetadataRepository, status: str = "RUNNING") -> None:
    with repository.sessions.begin() as session:
        session.add(
            Run(
                workflow_id="autonomous-backlog-staging-new163",
                run_type="autonomous-global-backlog",
                status=status,
                project="platform",
            )
        )


# --- approval (A, B) -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_projects_own_the_policy_in_parallel_through_the_reconciler(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    falcone = await _owner(repository, 1, "falcone")

    approved = await _approve(repository, _id(2), "musematic")

    assert approved["status"] == "APPROVED_POLICY_PENDING"
    assert approved["policy_activation"] == "QUEUED_ROOT_RECONCILER"
    request = _take_request(repository)
    assert request["action"] == "enable-existing-backlog"
    assert request["project"] == "musematic"
    _reconcile(repository, request)
    assert _status(repository, falcone) == ("APPROVED", "owner")
    assert _status(repository, _id(2)) == ("APPROVED", "owner")


@pytest.mark.asyncio
async def test_a_second_campaign_of_the_same_project_is_an_inventory(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    await _owner(repository, 1, "falcone")
    await _owner(repository, 2, "musematic")

    approved = await _approve(repository, _id(3), "falcone")

    assert approved["status"] == "APPROVED_INVENTORY"
    assert approved["policy_activation"] == "EXISTING_ACTIVE_POLICY"
    assert not (repository.github_policy_request_root / "activate.json").exists()
    assert _status(repository, _id(3)) == ("APPROVED_INVENTORY", "inventory")


@pytest.mark.asyncio
async def test_the_parallel_cap_refuses_another_project(tmp_path: Path) -> None:
    repository = _repository(tmp_path, limit=2)
    await _owner(repository, 1, "falcone")
    await _owner(repository, 2, "musematic")

    with pytest.raises(
        ValueError, match=r"parallel project limit 2 reached: .* owned by falcone, musematic"
    ):
        await _approve(repository, _id(3), "llmwiki")

    assert _status(repository, _id(3))[0] == "AWAITING_APPROVAL"
    assert not (repository.github_policy_request_root / "activate.json").exists()


@pytest.mark.asyncio
async def test_limit_one_is_the_single_global_owner_rule(tmp_path: Path) -> None:
    repository = _repository(tmp_path, limit=1)
    approved = await _approve(repository, _id(1), "falcone")
    assert approved["status"] == "APPROVED_POLICY_PENDING"
    _reconcile(repository, _take_request(repository), limit=1)

    other_project = await _approve(repository, _id(2), "musematic")

    assert other_project["status"] == "APPROVED_INVENTORY"
    assert not (repository.github_policy_request_root / "activate.json").exists()


def test_the_limit_is_at_least_one(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        SQLMetadataRepository(
            f"sqlite:///{tmp_path / 'metadata.sqlite'}", backlog_parallel_project_limit=0
        )
    assert Settings().backlog_parallel_project_limit == 2
    with pytest.raises(ValueError):
        Settings(backlog_parallel_project_limit=0)


@pytest.mark.asyncio
async def test_the_activation_slot_stays_global(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    await _approve(repository, _id(1), "falcone")

    with pytest.raises(ValueError, match=BACKLOG_POLICY_ACTIVATION_PENDING):
        await _approve(repository, _id(2), "musematic")


@pytest.mark.asyncio
@pytest.mark.parametrize("run_status", ["RUNNING", "PAUSED", "BLOCKED"])
async def test_an_open_global_backlog_keeps_the_single_global_owner_rule(
    tmp_path: Path, run_status: str
) -> None:
    repository = _repository(tmp_path)
    await _owner(repository, 1, "falcone")
    _open_global_backlog(repository, run_status)

    approved = await _approve(repository, _id(2), "musematic")

    assert approved["status"] == "APPROVED_INVENTORY"
    assert not (repository.github_policy_request_root / "activate.json").exists()


@pytest.mark.asyncio
async def test_a_finished_global_backlog_no_longer_forces_the_global_rule(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    await _owner(repository, 1, "falcone")
    _open_global_backlog(repository, "COMPLETED")

    approved = await _approve(repository, _id(2), "musematic")

    assert approved["status"] == "APPROVED_POLICY_PENDING"


# --- root reconciler (C) -------------------------------------------------------------


async def _reconciler_case(
    repository: SQLMetadataRepository, first: str, second: str
) -> tuple[UUID, dict[str, Any]]:
    """``first`` APPROVED owner and ``second``'s pending enable request.

    The API never queues a same-project second owner, so the first campaign is hidden
    (AWAITING_APPROVAL) while the second is approved and restored afterwards, as a
    request written by a racing or earlier release would find it."""
    first_id = _id(1)
    await _approve(repository, first_id, first)
    _take_request(repository)
    with repository.sessions.begin() as session:
        row = session.get(GitHubBacklogCampaign, first_id)
        assert row is not None
        row.status = "AWAITING_APPROVAL"
    request = await _pending_request(repository, 2, second)
    with repository.sessions.begin() as session:
        row = session.get(GitHubBacklogCampaign, first_id)
        assert row is not None
        row.status = "APPROVED"
    return first_id, request


@pytest.mark.asyncio
async def test_reconciler_refuses_enable_beside_a_same_project_owner(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _, request = await _reconciler_case(repository, "falcone", "falcone")

    with pytest.raises(ValueError, match="already owns GitHub write policy"):
        _reconcile(repository, request)
    assert _status(repository, _id(2))[0] == "APPROVED_POLICY_PENDING"


@pytest.mark.asyncio
async def test_reconciler_allows_enable_beside_another_project_owner(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _, request = await _reconciler_case(repository, "falcone", "musematic")

    _reconcile(repository, request)

    assert _status(repository, _id(2)) == ("APPROVED", "owner")


@pytest.mark.asyncio
async def test_reconciler_limit_one_and_global_backlog_keep_the_old_rule(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _, request = await _reconciler_case(repository, "falcone", "musematic")

    with pytest.raises(ValueError, match="already owns GitHub write policy"):
        _reconcile(repository, request, limit=1)
    _open_global_backlog(repository)
    with pytest.raises(ValueError, match="already owns GitHub write policy"):
        _reconcile(repository, request, limit=2)


@pytest.mark.asyncio
async def test_reconciler_enforces_the_parallel_cap(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _, request = await _reconciler_case(repository, "falcone", "llmwiki")
    with repository.sessions.begin() as session:
        session.add(_campaign(_id(3), "musematic"))
    with repository.sessions.begin() as session:
        third = session.get(GitHubBacklogCampaign, _id(3))
        assert third is not None
        third.status = "RUNNING"

    with pytest.raises(ValueError, match="parallel project limit 2 reached"):
        _reconcile(repository, request, limit=2)
    _reconcile(repository, request, limit=3)
    assert _status(repository, _id(2))[0] == "APPROVED"


@pytest.mark.asyncio
async def test_reconciler_reads_the_limit_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    _, request = await _reconciler_case(repository, "falcone", "musematic")
    monkeypatch.setenv("ENGINEERING_BACKLOG_PARALLEL_PROJECT_LIMIT", "1")

    with repository.sessions.begin() as session:
        with pytest.raises(ValueError, match="already owns GitHub write policy"):
            VALIDATE_CAMPAIGN(session, request)


@pytest.mark.asyncio
async def test_disable_keeps_the_policy_while_another_project_owner_is_active(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    falcone = await _owner(repository, 1, "falcone")
    musematic = await _owner(repository, 2, "musematic")
    now = datetime.now(UTC)

    with repository.sessions.begin() as session:
        row = session.get(GitHubBacklogCampaign, falcone)
        assert row is not None
        row.status = "COMPLETE"
    with repository.sessions.begin() as session:
        assert OTHER_OWNER(session, falcone, now) == musematic
        assert EXPIRE(session, now) is False

    with repository.sessions.begin() as session:
        row = session.get(GitHubBacklogCampaign, musematic)
        assert row is not None
        row.status = "COMPLETE"
    with repository.sessions.begin() as session:
        assert OTHER_OWNER(session, musematic, now) is None
        assert EXPIRE(session, now) is True


# --- global backlog stays exclusive (D) ----------------------------------------------


def test_global_backlog_is_refused_while_an_outside_owner_is_active(tmp_path: Path) -> None:
    from test_global_backlog_convergence import (
        FALCONE_ID,
        GLOBAL_ID,
        LLMWIKI_ID,
        MUSEMATIC_ID,
    )
    from test_global_backlog_convergence import _campaign as global_campaign

    from engineering_control.activities.engineering import EngineeringActivityHandler

    database_url = f"sqlite+pysqlite:///{tmp_path / 'global.db'}"
    operations = OperationsRepository(database_url)
    Base.metadata.create_all(operations.engine)
    with operations.sessions.begin() as session:
        session.add_all(
            [
                global_campaign(FALCONE_ID, "falcone", status="APPROVED"),
                global_campaign(MUSEMATIC_ID, "musematic", status="APPROVED"),
                global_campaign(LLMWIKI_ID, "llmwiki", status="APPROVED"),
            ]
        )
    ids = {"falcone": FALCONE_ID, "musematic": MUSEMATIC_ID, "llmwiki": LLMWIKI_ID}
    command = AutonomousBacklogCommand(
        campaign_id=GLOBAL_ID,
        backlog_campaign_ids=ids,
        policy_campaign_id=FALCONE_ID,
        typed_confirmation="START_GLOBAL_STAGING_R2_BACKLOG",
    )

    def policy_blocked() -> bool:
        with operations.sessions() as session:
            capability, _ = operations._capability(session, command, datetime.now(UTC))
        return "GITHUB_WRITE_POLICY_NOT_ACTIVE" in capability.blockers

    assert not policy_blocked()

    # A standalone musematic owner (another campaign, outside the global inventories).
    outside_id = UUID("44444444-4444-4444-8444-444444444444")
    with operations.sessions.begin() as session:
        outside = global_campaign(outside_id, "musematic", status="APPROVED")
        outside.status = "APPROVED"
        outside.workflow_id = "backlog-import-musematic-outside"
        outside.approval_json = {**outside.approval_json, "policy_role": "owner"}
        session.add(outside)

    assert policy_blocked()
    handler = EngineeringActivityHandler(Settings(database_url=database_url))
    with pytest.raises(ValueError, match="one active exact GitHub write policy"):
        handler._register_global_backlog(
            {
                "campaign_id": str(GLOBAL_ID),
                "backlog_campaign_ids": {key: str(value) for key, value in ids.items()},
                "policy_campaign_id": str(FALCONE_ID),
                "orchestration": {"workflow_id": "global-exclusive"},
            }
        )


# --- issue-start waits for another campaign's activation (E) -------------------------


def _bus(approve: AsyncMock) -> tuple[TypedCommandBus, Any, Any]:
    control = Mock()
    control.start_existing_backlog_loop = AsyncMock(return_value="started")
    control.workflow_id_closed = AsyncMock(return_value=False)
    metadata = Mock()
    metadata.prepare_backlog_approval = AsyncMock(return_value={"approval_binding": "a" * 64})
    metadata.approve_backlog_campaign = approve
    metadata.ensure_policy_activation = AsyncMock()
    metadata.get_backlog_campaign = AsyncMock(return_value={"status": "APPROVED"})
    operations = Mock()
    operations.operation = AsyncMock(return_value=SimpleNamespace(binding_hash="b" * 64))
    operations.open_campaign_loops = AsyncMock(return_value=[])
    bus = TypedCommandBus(control, metadata, operations)
    bus.policy_activation_poll_seconds = 0
    bus.policy_activation_timeout_seconds = 5
    return bus, control, metadata


def _solve_issue() -> SolveIssueCommand:
    return SolveIssueCommand(
        project=Project.MUSEMATIC,
        backlog_campaign_id=_id(2),
        repository="gntik-ai/musematic",
        issue_number=7,
        delivery_mode="source-pr",
    )


@pytest.mark.asyncio
async def test_issue_start_waits_for_another_pending_activation_then_succeeds() -> None:
    approve = AsyncMock(
        side_effect=[
            ValueError(BACKLOG_POLICY_ACTIVATION_PENDING),
            ValueError(BACKLOG_POLICY_ACTIVATION_PENDING),
            {"status": "APPROVED_POLICY_PENDING", "approval_binding": "a" * 64},
        ]
    )
    bus, control, _ = _bus(approve)

    result = await bus._execute(_solve_issue(), operator_class="telegram:42", operation_id=_id(9))

    assert result == "started"
    assert approve.await_count == 3
    assert {call.args for call in approve.await_args_list} == {
        (_id(2), "musematic", "a" * 64, "telegram:42")
    }
    control.start_existing_backlog_loop.assert_awaited_once()


@pytest.mark.asyncio
async def test_issue_start_propagates_other_refusals_and_the_timeout() -> None:
    approve = AsyncMock(side_effect=ValueError("existing-backlog approval binding is not current"))
    bus, control, _ = _bus(approve)
    with pytest.raises(ValueError, match="binding is not current"):
        await bus._execute(_solve_issue(), operator_class="telegram:42", operation_id=_id(9))
    assert approve.await_count == 1

    approve = AsyncMock(side_effect=ValueError(BACKLOG_POLICY_ACTIVATION_PENDING))
    bus, control, _ = _bus(approve)
    bus.policy_activation_timeout_seconds = 0
    with pytest.raises(ValueError, match=BACKLOG_POLICY_ACTIVATION_PENDING):
        await bus._execute(_solve_issue(), operator_class="telegram:42", operation_id=_id(9))
    control.start_existing_backlog_loop.assert_not_awaited()


# --- write-time project pair (F) -----------------------------------------------------


def _delivery_handler(tmp_path: Path) -> DeliveryActivityHandler:
    from test_github_write_denial import NoCommandRunner, policy_file

    project_root, policy = policy_file(
        tmp_path, enabled=True, repositories=tuple(sorted(WRITE_REPOSITORIES))
    )
    return DeliveryActivityHandler(
        Settings(
            project_config_root=project_root,
            github_issue_write_validation_file=policy,
            github_writes_enabled=True,
            run_root=tmp_path / "runs",
            worktree_root=tmp_path / "worktrees",
        ),
        NoCommandRunner(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "step",
    [
        "delivery.publish",
        "final.merge_source",
        "final.comment_issue",
        "reconcile.publish_promotion",
    ],
)
@pytest.mark.parametrize("project", ["musematic", "", "rogue"])
async def test_a_delivery_write_outside_its_project_pair_is_denied(
    tmp_path: Path, step: str, project: str
) -> None:
    from test_github_write_denial import write_payloads

    handler = _delivery_handler(tmp_path)
    payload = json.loads(json.dumps(write_payloads(tmp_path / "worktrees")[step]))
    payload["project"] = project
    if step == "final.merge_source":
        payload["final_closure_plan"]["project"] = project
    if step == "final.comment_issue":
        payload["final_issue_plan"]["closure"]["project"] = project

    result = await handler.handle(step, payload)

    assert result["status"] == "BLOCKED_BY_POLICY"
    assert result["reason_code"] == "REPOSITORY_OUTSIDE_PROJECT"
    assert result["external_command_count"] == 0


@pytest.mark.parametrize("step", ["delivery.publish", "final.merge_source"])
def test_a_delivery_write_without_its_project_is_denied(tmp_path: Path, step: str) -> None:
    from test_github_write_denial import write_payloads

    handler = _delivery_handler(tmp_path)
    payload = write_payloads(tmp_path / "worktrees")[step]
    project = handler._write_project(step, payload)

    denial = handler.write_policy.evaluate(
        lambda: handler._write_repositories(step, payload), project=project
    )

    assert project == ""
    assert denial is not None and denial.reason_code == "REPOSITORY_OUTSIDE_PROJECT"


def test_a_delivery_write_inside_its_project_pair_is_allowed(tmp_path: Path) -> None:
    from test_github_write_denial import write_payloads

    handler = _delivery_handler(tmp_path)
    payloads = write_payloads(tmp_path / "worktrees")
    payloads["delivery.publish"]["project"] = "falcone"
    payloads["final.merge_source"]["final_closure_plan"]["project"] = "falcone"
    for step in ("delivery.publish", "final.merge_source"):
        payload = payloads[step]
        assert (
            handler.write_policy.evaluate(
                lambda payload=payload, step=step: handler._write_repositories(step, payload),
                project=handler._write_project(step, payload),
            )
            is None
        )
    # Finding and enhancement publication stay bound by the shared allowlist only.
    assert handler._write_project("finding.publish", {"project": "falcone"}) is None
    assert handler._write_project("enhancement.publish", {"project": "falcone"}) is None


def test_the_pre_publication_recheck_keeps_the_project_pair(tmp_path: Path) -> None:
    from test_github_write_denial import write_payloads

    from engineering_control.activities.delivery import RepositoryPublication
    from engineering_control.adapters.publication_authority import PublicationAuthorityError

    handler = _delivery_handler(tmp_path)
    raw = write_payloads(tmp_path / "worktrees")["delivery.publish"]
    plan = RepositoryPublication.model_validate(raw["publication_plan"]["publications"][0])
    authority = Mock()
    authority.execute = lambda **kwargs: kwargs["operation"]()

    def effect(project: str) -> Any:
        return handler._publication_effect(
            plan, (authority, {"project": project}, {}), "pr_create", lambda: "done"
        )

    assert effect("falcone") == "done"
    with pytest.raises(PublicationAuthorityError, match="write policy changed"):
        effect("musematic")
