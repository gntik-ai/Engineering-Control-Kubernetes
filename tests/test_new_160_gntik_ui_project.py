"""NEW-160: gntik-ui is the fourth Hermes project.

It is self-contained (source == deployment repository), has no runtime
(``deployment.strategy: none``) and its full delivery ends when Hermes merges the PR with
its changeset after the maker, the independent checker and green CI. There is no staging
GitOps, release tag, image build or runtime verification; releases stay the operator's.
"""

from __future__ import annotations

import asyncio
import json
import os
import runpy
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import select
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_staging_gitops import (
    MERGE_SHA,
    FakeGh,
    FakeKubectl,
    _config_root,
    _controller,
    _settings,
)
from test_workflow_replay import (
    _TEMPORAL_CLI,
    _registered_child,
    _run_to_history,
    _scheduled_steps,
    _scripted_execute_control_step,
)

from engineering_control import delivery_notifications as notices
from engineering_control.activities.delivery import PublicationPlan
from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.activities.github import GitHubActivityHandler
from engineering_control.activities.gitops import (
    GitOpsGitHubHandler,
    StagingFlowBlocked,
    StagingGitOpsPlan,
    enabled_delivery_gate,
    enabled_staging_flow,
)
from engineering_control.config import Settings
from engineering_control.db import models as db
from engineering_control.db.base import Base
from engineering_control.db.models import BacklogIssueRecord, GitHubBacklogCampaign
from engineering_control.engineering.backlog import (
    BACKLOG_PROJECT_PAIRS,
    GitHubBacklogImportSnapshot,
    GitHubRepositoryBacklogSnapshot,
    approval_binding,
)
from engineering_control.engineering.models import ProjectManifest
from engineering_control.models.reconciliation import (
    ChangesetGatePolicy,
    MergeDeliveryPolicy,
)
from engineering_control.operations.repository import _r3_delivery_briefing
from engineering_control.workflows.definitions import (
    ExistingGitHubBacklogLoopWorkflow,
    StagingGitOpsDeliveryWorkflow,
)
from engineering_control.workflows.inputs import (
    ExistingGitHubBacklogLoopInput,
    IssueDeliveryInput,
    StagingGitOpsDeliveryInput,
)
from engineering_control.workflows.worker_registry import CONTROL_TASK_QUEUE, GITHUB_TASK_QUEUE

ROOT = Path(__file__).resolve().parents[1]
HEAD_SHA = "a" * 40
CHANGE_SET = str(UUID(int=160))
PR_URL = "https://github.com/gntik-ai/gntik-ui/pull/41"
REPOSITORY = "gntik-ai/gntik-ui"
SAFE = (REPOSITORY,)


def _manifest_value() -> dict[str, Any]:
    value = yaml.safe_load((ROOT / "config/projects/gntik-ui.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _manifest(value: dict[str, Any] | None = None) -> ProjectManifest:
    return ProjectManifest.model_validate(value or _manifest_value())


# -- the manifest: self-contained, no runtime, merge-only delivery ---------------------------


def test_gntik_ui_is_a_self_contained_project_without_a_runtime() -> None:
    manifest = _manifest()
    assert manifest.project == "gntik-ui"
    assert str(manifest.repositories.source.full_name) == REPOSITORY
    assert str(manifest.repositories.deployment.full_name) == REPOSITORY
    assert manifest.repositories.source.default_branch == "main"
    assert manifest.repositories.source.mirror == "/srv/engineering/mirrors/gntik-ui.git"
    assert manifest.has_runtime() is False
    assert manifest.deployment.strategy == "none" and manifest.deployment.adapter == "none"
    assert manifest.staging_flow() is None
    assert manifest.staging_flow_blockers() == ["STAGING_FLOW_DISABLED"]
    merge = manifest.merge_delivery()
    assert merge is not None and merge.enabled and merge.source_ci_workflow == "ci"
    assert merge.non_blocking_checks == ("visual",)
    assert merge.changeset.enabled and merge.changeset.package_paths == ("packages/", "blocks/")
    assert ".github/" in merge.protected_paths
    assert manifest.merge_delivery_blockers() == []
    with pytest.raises(ValueError, match="PROJECT_HAS_NO_RUNTIME"):
        manifest.runtime_deployment()
    # NEW-159: Node 24 and the pinned pnpm, prepared offline-ready for the makers.
    assert manifest.toolchain is not None and manifest.toolchain.node == "24"
    assert manifest.toolchain.pnpm == "11.28.3"
    assert [command.argv for command in manifest.toolchain.prepare] == [
        ["pnpm", "install", "--frozen-lockfile"]
    ]
    assert [command.argv for command in manifest.toolchain.test_commands] == [
        ["pnpm", "build"],
        ["pnpm", "lint"],
        ["pnpm", "typecheck"],
        ["pnpm", "test"],
        ["pnpm", "registry:check"],
        ["node", "apps/musematic/scripts/check-kit-only.mjs"],
        ["node", "apps/falcone/scripts/check-kit-only.mjs"],
    ]
    assert manifest.maker_rules is not None and any(
        ".changeset/" in rule for rule in manifest.maker_rules.source
    )


def _staging_flow() -> dict[str, Any]:
    falcone = yaml.safe_load((ROOT / "config/projects/musematic.yaml").read_text("utf-8"))
    flow = falcone["delivery_control"]["staging_flow"]
    assert isinstance(flow, dict)
    return flow


@pytest.mark.parametrize(
    "mutate",
    [
        # A no-runtime project never carries a staging flow, pin or runtime fields.
        lambda value: value["delivery_control"].update(staging_flow=_staging_flow()),
        lambda value: value["deployment"].update(namespaces=["gntik-ui"]),
        lambda value: value["deployment"].update(adapter="falcone"),
        lambda value: value["deployment"]["release"].update(publisher="hermes"),
        lambda value: value.update(
            deployment_pin={"files": [".github/workflows/ci.yml"], "variable": "PIN"}
        ),
        # Without the merge-only delivery it would have no delivery at all.
        lambda value: value["delivery_control"].pop("merge_delivery"),
        lambda value: value["delivery_control"]["merge_delivery"].update(kind="staging"),
        lambda value: value["delivery_control"]["merge_delivery"].update(
            non_blocking_checks=["visual", "visual"]
        ),
        lambda value: value["delivery_control"]["merge_delivery"].update(
            protected_paths=["../escape/"]
        ),
        lambda value: value["delivery_control"]["merge_delivery"]["changeset"].update(
            package_paths=[]
        ),
    ],
)
def test_the_no_runtime_contract_is_strict(mutate: Any) -> None:
    value = _manifest_value()
    mutate(value)
    with pytest.raises(ValidationError):
        _manifest(value)


def test_merge_only_delivery_is_reserved_for_projects_without_a_runtime() -> None:
    falcone = yaml.safe_load((ROOT / "config/projects/falcone.yaml").read_text("utf-8"))
    falcone["delivery_control"]["merge_delivery"] = _manifest_value()["delivery_control"][
        "merge_delivery"
    ]
    with pytest.raises(ValidationError, match="reserved for projects without a runtime"):
        ProjectManifest.model_validate(falcone)
    # Every runtime project keeps its runtime and no merge-only delivery.
    for project in ("falcone", "musematic", "llmwiki"):
        manifest = ProjectManifest.model_validate(
            yaml.safe_load((ROOT / f"config/projects/{project}.yaml").read_text("utf-8"))
        )
        assert manifest.has_runtime() and manifest.merge_delivery() is None
        assert manifest.runtime_deployment() is manifest.deployment


def test_merge_delivery_cross_checks_its_contract() -> None:
    value = _manifest_value()
    value["delivery_control"]["merge_delivery"]["source_ci_workflow"] = "other"
    assert _manifest(value).merge_delivery_blockers() == [
        "MERGE_DELIVERY_SOURCE_CI_WORKFLOW_MISMATCH"
    ]
    value = _manifest_value()
    value["delivery_control"]["merge_delivery"]["enabled"] = False
    assert _manifest(value).merge_delivery_blockers() == ["MERGE_DELIVERY_DISABLED"]


def test_runtime_readers_fail_closed_and_the_gate_is_the_merge_delivery(tmp_path: Path) -> None:
    projects = _config_root(tmp_path)
    with pytest.raises(StagingFlowBlocked) as blocked:
        enabled_staging_flow(projects, "gntik-ui")
    assert blocked.value.blocker == "STAGING_FLOW_DISABLED"
    _, gate = enabled_delivery_gate(projects, "gntik-ui")
    assert isinstance(gate, MergeDeliveryPolicy)
    # falcone and musematic keep their staging flows.
    for project in ("falcone", "musematic"):
        _, flow = enabled_delivery_gate(projects, project)
        assert not isinstance(flow, MergeDeliveryPolicy)
    briefing = _r3_delivery_briefing(_manifest(), "source")
    assert briefing["delivery_adapter"] == "protected-merge"
    assert briefing["build_contract_state"] == "NOT_APPLICABLE_NO_RUNTIME"


# -- the changeset gate -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changed", "missing"),
    [
        (["packages/ui/src/button.tsx"], True),
        (["packages/ui/src/button.tsx", ".changeset/brave-ducks.md"], False),
        (["blocks/login/index.tsx", ".changeset/README.md"], True),
        (["packages/ui/src/button.tsx", ".changeset/nested/x.md"], True),
        (["packages/ui/src/button.tsx", ".changeset/config.json"], True),
        (["apps/docs/pages/index.mdx"], False),  # no published package touched
        ([], False),
    ],
)
def test_a_package_change_requires_a_changeset(changed: list[str], missing: bool) -> None:
    gate = ChangesetGatePolicy(enabled=True, package_paths=("packages/", "blocks/"))
    assert gate.missing(changed) is missing
    assert ChangesetGatePolicy().missing(changed) is False


# -- the registry: a self-contained pair outside the global campaign ------------------------


def test_the_self_contained_publication_plan_and_backlog_pair() -> None:
    assert BACKLOG_PROJECT_PAIRS["gntik-ui"] == (REPOSITORY, REPOSITORY)
    publication = {
        "repository": REPOSITORY,
        "repository_slug": "gntik-ui",
        "working_directory": "/srv/engineering/worktrees/gntik-ui/x",
        "branch": f"agent/gntik-ui/fix-button/{CHANGE_SET}",
        "head_sha": HEAD_SHA,
        "title": "Fix the button focus ring",
        "body": "Fixes the focus ring.",
    }
    plan = PublicationPlan.model_validate(
        {"change_set_id": CHANGE_SET, "publications": [publication]}
    )
    assert [item.repository_slug for item in plan.publications] == ["gntik-ui"]
    with pytest.raises(ValidationError, match="deferred"):
        PublicationPlan.model_validate(
            {
                "change_set_id": CHANGE_SET,
                "publications": [publication],
                "deferred_deployment": {
                    "repository_slug": "falcone-charts",
                    "branch": f"agent/falcone/x/{CHANGE_SET}",
                    "head_sha": HEAD_SHA,
                    "base_sha": "b" * 40,
                    "reason": "AWAITING_IMMUTABLE_ARTIFACT",
                },
            }
        )


@pytest.mark.parametrize("mode", ["full-delivery", "source-pr"])
def test_gntik_ui_approval_bindings_merge_without_staging(mode: str) -> None:
    binding = approval_binding(
        campaign_id="00000000-0000-4000-8000-000000000160",
        project="gntik-ui",
        import_filter="bugs",
        source_head=HEAD_SHA,
        deployment_head=HEAD_SHA,
        dependency_plan_hash="c" * 64,
        maximum_risk="R2",
        maximum_items=1,
        delivery_mode=mode,  # type: ignore[arg-type]
        include_enhancements=False,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )
    assert binding.paired_repositories == (REPOSITORY, REPOSITORY)
    assert binding.source_branch == binding.deployment_branch == "main"
    assert binding.staging_targets == () and binding.allowed_build_workflows == ()
    assert ("protected-pr-merge" in binding.allowed_github_operations) is (mode == "full-delivery")
    assert binding.merge_policy == ("protected-explicit" if mode == "full-delivery" else "no-merge")


def _repository_snapshot(issues: tuple[Any, ...] = ()) -> GitHubRepositoryBacklogSnapshot:
    return GitHubRepositoryBacklogSnapshot(
        repository=REPOSITORY,
        default_branch="main",
        head_sha=HEAD_SHA,
        issues=issues,
        pull_requests=(),
    )


def test_the_import_snapshot_is_the_single_self_contained_repository() -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    snapshot = GitHubBacklogImportSnapshot(
        project="gntik-ui", imported_at=now, repositories=(_repository_snapshot(),)
    )
    assert [item.repository for item in snapshot.repositories] == [REPOSITORY]
    with pytest.raises(ValidationError, match="authoritative repository pair"):
        GitHubBacklogImportSnapshot(
            project="gntik-ui",
            imported_at=now,
            repositories=(_repository_snapshot(), _repository_snapshot()),
        )


class _FakeBacklogBroker:
    """The read-only broker calls of a backlog import, for gntik-ui only."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def preflight(self, repositories: tuple[str, ...]) -> None:
        assert repositories == (REPOSITORY,)
        self.calls.append(("preflight", ",".join(repositories)))

    def repository_metadata(self, repository: str) -> dict[str, Any]:
        self.calls.append(("metadata", repository))
        return {"defaultBranchRef": {"name": "main"}}

    def branch_commit(self, repository: str, branch: str) -> dict[str, str]:
        assert branch == "main"
        return {"sha": HEAD_SHA, "tree": "f" * 40}

    def list_open_issues(self, repository: str) -> list[dict[str, Any]]:
        return [
            {
                "number": 12,
                "url": f"https://github.com/{repository}/issues/12",
                "title": "Button loses its focus ring in high contrast",
                "body": "The focus ring disappears in high-contrast mode.",
                "labels": [{"name": "bug"}],
                "assignees": [],
                "comments": [],
                "updatedAt": "2026-10-08T10:00:00Z",
            }
        ]

    def list_repository_pull_requests(self, repository: str) -> list[dict[str, Any]]:
        return []


async def test_backlog_import_and_plan_for_gntik_ui(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'backlog.sqlite'}",
        project_config_root=ROOT / "config/projects",
        persona_config_root=ROOT / "config/personas",
        flow_config_root=ROOT / "config/flows",
        credential_root=tmp_path,
        run_root=tmp_path / "runs",
    )
    github = GitHubActivityHandler(settings)
    broker = _FakeBacklogBroker()
    github.broker = broker  # type: ignore[assignment]
    campaign_id = "33333333-3333-4333-8333-333333333160"
    imported = await github.handle(
        "backlog.github_import",
        {"backlog_campaign_id": campaign_id, "project": "gntik-ui", "maximum_items": 10},
    )
    assert imported["status"] == "IMPORTED_READ_ONLY" and imported["issue_count"] == 1
    assert [repository for _, repository in broker.calls] == [REPOSITORY, REPOSITORY]
    handler = EngineeringActivityHandler(settings)
    Base.metadata.create_all(handler.engine)
    persisted = await handler.handle(
        "backlog.persist_import",
        {
            "backlog_campaign_id": campaign_id,
            "project": "gntik-ui",
            "import_filter": "bugs",
            "maximum_items": 1,
            "snapshot": imported["snapshot"],
        },
    )
    assert persisted["status"] == "IMPORTED"
    planned = await handler.handle("backlog.plan", persisted)
    assert planned["status"] not in {"BLOCKED", "FAILED"}
    with handler.sessions() as session:
        campaign = session.get(GitHubBacklogCampaign, UUID(campaign_id))
        assert campaign is not None and campaign.project == "gntik-ui"
        assert campaign.source_repository == campaign.deployment_repository == REPOSITORY
        records = session.scalars(select(BacklogIssueRecord)).all()
        assert [(record.repository, record.issue_number) for record in records] == [
            (REPOSITORY, 12)
        ]


# -- GitOps activities: the merge-only gate --------------------------------------------------


def _plan(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "change_set_id": CHANGE_SET,
        "project": "gntik-ui",
        "risk_class": "R1",
        "source_repository": REPOSITORY,
        "source_pr_number": 41,
        "source_pr_url": PR_URL,
        "source_head_sha": HEAD_SHA,
        "source_base_branch": "main",
        "deployment_repository": REPOSITORY,
        "deployment_pr_number": None,
        "deployment_pr_url": None,
        "deployment_head_sha": None,
        "deployment_base_branch": "main",
        "issue_repository": REPOSITORY,
        "issue_number": 12,
    }
    value.update(changes)
    return StagingGitOpsPlan.model_validate(value).model_dump(mode="json")


def _check(name: str, conclusion: str | None) -> dict[str, Any]:
    return {
        "workflowName": "ci",
        "name": name,
        "status": "COMPLETED" if conclusion else "IN_PROGRESS",
        "conclusion": conclusion or "",
        "startedAt": "2026-10-08T10:00:00Z",
    }


GREEN = [_check("checks", "SUCCESS"), _check("a11y", "SUCCESS"), _check("visual", "SUCCESS")]


class _MergeGh(FakeGh):
    def __init__(self, changed: list[str]) -> None:
        super().__init__()
        self.changed = changed

    def _answer(self, args: list[str]) -> str:
        if args[0] == "api" and args[1].endswith("/pulls/41/files"):
            return "\n".join(self.changed)
        if args[:2] == ["pr", "ready"]:
            pull = self.pull_requests[(args[4], int(args[2]))]
            failing = any(
                check["conclusion"] not in {"SUCCESS", "NEUTRAL", "SKIPPED"}
                for check in pull["statusCheckRollup"]
            )
            pull.update(isDraft=False, mergeStateStatus="UNSTABLE" if failing else "CLEAN")
            return ""
        return super()._answer(args)


def _pull_request(checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "number": 41,
        "url": PR_URL,
        "state": "OPEN",
        "isDraft": True,
        "headRefName": f"agent/gntik-ui/fix/{CHANGE_SET}",
        "headRefOid": HEAD_SHA,
        "baseRefName": "main",
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": checks,
        "mergeCommit": None,
    }


def _github(
    tmp_path: Path, checks: list[dict[str, Any]], changed: list[str]
) -> tuple[GitOpsGitHubHandler, _MergeGh]:
    fake = _MergeGh(changed)
    settings = _settings(
        tmp_path,
        project_config_root=_config_root(tmp_path, safe_repositories=SAFE),
        github_writes_enabled=True,
    )
    fake.pull_requests[(REPOSITORY, 41)] = _pull_request(checks)
    return GitOpsGitHubHandler(settings, runner=fake), fake  # type: ignore[arg-type]


CHANGED = ["packages/ui/src/button.tsx", ".changeset/brave-ducks.md"]


async def test_the_continue_on_error_visual_job_never_blocks(tmp_path: Path) -> None:
    for visual in ("FAILURE", None):  # failed or still running
        checks = [*GREEN[:2], _check("visual", visual)]
        handler, fake = _github(tmp_path / str(visual), checks, CHANGED)
        assert (await handler.handle("gitops.pr_checks", {"plan": _plan()}))["status"] == (
            "CHECKS_PASSED"
        )
        merged = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
        assert merged["status"] == "MERGED" and merged["merge_sha"] == MERGE_SHA
        assert [call[:2] for call in fake.mutations] == [["pr", "ready"], ["pr", "merge"]]
    # Every other check still blocks.
    handler, fake = _github(tmp_path / "a11y", [GREEN[0], _check("a11y", "FAILURE")], CHANGED)
    failed = await handler.handle("gitops.pr_checks", {"plan": _plan()})
    assert failed["blocker"] == "SOURCE_PR_CI_FAILED" and failed["failed_checks"] == ["a11y"]
    pending = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert pending["status"] == "MERGE_PENDING"
    assert ["pr", "merge"] not in [call[:2] for call in fake.mutations]


@pytest.mark.parametrize(
    ("changed", "blocker"),
    [
        (["packages/ui/src/button.tsx"], "CHANGESET_MISSING"),
        ([*CHANGED, ".github/workflows/ci.yml"], "MERGE_DELIVERY_PROTECTED_PATH_CHANGED"),
        ([*CHANGED, "CLAUDE.md"], "MERGE_DELIVERY_PROTECTED_PATH_CHANGED"),
        ([*CHANGED, ".claude/skills/x/SKILL.md"], "MERGE_DELIVERY_PROTECTED_PATH_CHANGED"),
    ],
)
async def test_the_merge_requires_a_changeset_and_no_protected_path(
    tmp_path: Path, changed: list[str], blocker: str
) -> None:
    handler, fake = _github(tmp_path, GREEN, changed)
    refused = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert refused == {"status": "BLOCKED", "blocker": blocker}
    assert fake.mutations == []


async def test_a_merge_only_flow_has_no_deployment_target_or_release_step(
    tmp_path: Path,
) -> None:
    handler, fake = _github(tmp_path, GREEN, CHANGED)
    deployment = await handler.handle("gitops.merge", {"plan": _plan(), "target": "deployment"})
    assert deployment["blocker"] == "MERGE_TARGET_INVALID"
    for step in ("gitops.release_tag", "gitops.image_pin", "gitops.workflow_run"):
        result = await handler.handle(step, {"plan": _plan(), "merge_sha": MERGE_SHA})
        assert result["status"] == "BLOCKED", step
    assert fake.mutations == []


async def test_close_issue_records_the_merge_evidence(tmp_path: Path) -> None:
    handler, fake = _github(tmp_path, GREEN, CHANGED)
    fake.issues[(REPOSITORY, 12)] = {
        "number": 12,
        "title": "Button loses its focus ring",
        "state": "OPEN",
        "body": "",
        "comments": [],
        "url": "https://github.com/gntik-ai/gntik-ui/issues/12",
        "closedAt": None,
    }
    closed = await handler.handle("gitops.close_issue", {"plan": _plan(), "merge_sha": MERGE_SHA})
    assert closed["status"] == "ISSUE_CLOSED"
    issue = fake.issues[(REPOSITORY, 12)]
    assert issue["state"] == "CLOSED"
    body = issue["comments"][-1]["body"]
    assert PR_URL in body and MERGE_SHA in body and "no runtime to deploy" in body
    assert "Argo" not in body and "Image tag" not in body


def _seed(handler: Any, *, state: str = "SOURCE_PR") -> UUID:
    change_set_id = uuid4()
    with handler.sessions.begin() as session:
        session.add(
            db.ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                project="gntik-ui",
                risk_class="R1",
                state=state,
                version=1,
                source_repository="gntik-ui",
                source_base_sha="b" * 40,
                source_head_sha=HEAD_SHA,
                source_pr_url=PR_URL,
                deployment_repository="gntik-ui",
                deployment_base_sha="b" * 40,
                image_digests={},
                recovery_json={},
            )
        )
    return change_set_id


async def test_the_controller_selects_and_completes_a_merge_only_delivery(
    tmp_path: Path,
) -> None:
    handler = _controller(tmp_path, FakeKubectl())
    flow = await handler.handle("gitops.flow_for_project", {"project": "gntik-ui"})
    assert flow == {"status": "ENABLED", "enabled": True, "merge_only": True, "blockers": []}
    change_set_id = _seed(handler)
    request = {
        "change_set_id": str(change_set_id),
        "project": "gntik-ui",
        "issue_repository": REPOSITORY,
        "issue_number": 12,
    }
    prepared = await handler.handle("gitops.prepare", request)
    assert prepared["status"] == "READY"
    assert prepared["staging_flow"] == {
        "merge_only": True,
        "argo_application": None,
        "main_ci_workflow": None,
        "image_workflow": None,
        "release_tag_enabled": False,
        "pin_chart_to_merge": False,
    }
    plan = prepared["plan"]
    assert plan["source_repository"] == plan["deployment_repository"] == REPOSITORY
    assert plan["deployment_pr_number"] is None
    workflow_id = "loop-issue-1-staging-gitops"
    with handler.sessions.begin() as session:
        session.add(
            db.Run(
                workflow_id=workflow_id,
                run_type="staging-gitops",
                status="QUEUED",
                project="gntik-ui",
                change_set_id=change_set_id,
            )
        )
    persisted = await handler.handle(
        "gitops.persist_result",
        {
            "plan": plan,
            "outcome": "MERGE_DELIVERED",
            "merge_sha": MERGE_SHA,
            "orchestration": {"workflow_id": workflow_id},
        },
    )
    assert persisted == {
        "status": "PERSISTED",
        "outcome": "MERGE_DELIVERED",
        "change_set_state": "ISSUE_CLOSED",
    }
    with handler.sessions() as session:
        run = session.scalar(select(db.Run).where(db.Run.workflow_id == workflow_id))
        assert run is not None and run.status == "SUCCEEDED" and run.error_code is None
        events = [
            row.payload_json["event"]
            for row in session.scalars(
                select(db.NotificationOutbox).where(
                    db.NotificationOutbox.topic == notices.MILESTONE_TOPIC
                )
            )
        ]
    assert events == ["PRS_MERGED", "MERGE_DELIVERED"]
    # A replayed child finds the delivery done.
    again = await handler.handle("gitops.prepare", request)
    assert again["status"] == "ALREADY_VERIFIED"


# -- the root reconciler: gntik-ui joins without stranding an in-flight campaign -------------

SCRIPT = runpy.run_path(str(ROOT / "scripts/reconcile-backlog-github-policy.py"))
PREVIOUS = [
    "gntik-ai/falcone",
    "gntik-ai/falcone-charts",
    "gntik-ai/musematic",
    "gntik-ai/musematic-deploy",
    "gntik-ai/llmwiki",
]


def _request(tmp_path: Path, repositories: list[str], project: str = "falcone") -> Path:
    path = tmp_path / f"activate-{len(repositories)}-{project}-{uuid4().hex[:8]}.json"
    path.write_text(
        json.dumps(
            {
                "action": "enable-existing-backlog",
                "backlog_campaign_id": "11111111-1111-4111-8111-111111111111",
                "project": project,
                "approval_binding": "a" * 64,
                "safe_repositories": repositories,
                "requested_by": "telegram:42",
                "requested_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_the_reconciler_accepts_new_and_in_flight_requests(tmp_path: Path) -> None:
    assert SCRIPT["SAFE_REPOSITORIES"] == (*PREVIOUS, REPOSITORY)
    read = SCRIPT["read_request"]
    assert read(_request(tmp_path, [*PREVIOUS, REPOSITORY], "gntik-ui"), os.getuid())
    # NEW-160: a request queued by the previous release is still honoured.
    assert read(_request(tmp_path, PREVIOUS), os.getuid())["project"] == "falcone"
    for other in ([*PREVIOUS[:-1]], [REPOSITORY], [*PREVIOUS, REPOSITORY, "gntik-ai/x"]):
        with pytest.raises(ValueError, match="repository set changed"):
            read(_request(tmp_path, other), os.getuid())


def _root_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repositories: list[str] | None
) -> Path:
    directory = tmp_path / "policy"
    directory.mkdir()
    path = directory / "github-issue-write-validation.yaml"
    listed = "".join(f"  - {repository}\n" for repository in repositories or [])
    path.write_text(
        f"enabled: true\nsafe_repositories:\n{listed}"
        if repositories
        else "enabled: false\nsafe_repositories: []\n",
        encoding="utf-8",
    )
    real_lstat = Path.lstat

    def root_owned(self: Path) -> os.stat_result:
        value = real_lstat(self)
        if self not in (path, directory):
            return value
        mode = (stat.S_IFREG | 0o640) if self == path else (stat.S_IFDIR | 0o750)
        fields = list(value)
        fields[0], fields[4], fields[5] = mode, 0, 4242
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", root_owned)
    return path


@pytest.mark.parametrize(
    ("repositories", "valid"),
    [
        ([*PREVIOUS, REPOSITORY], True),
        # The policy the running campaign enabled before this release.
        (PREVIOUS, True),
        (None, True),
        (PREVIOUS[:-1], False),
        ([REPOSITORY], False),
    ],
)
def test_the_reconciler_keeps_an_in_flight_enabled_policy_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repositories: list[str] | None,
    valid: bool,
) -> None:
    path = _root_policy(tmp_path, monkeypatch, repositories)
    if valid:
        SCRIPT["validate_current_policy"](path, 4242)
    else:
        with pytest.raises(ValueError, match="outside this mission"):
            SCRIPT["validate_current_policy"](path, 4242)


def test_installing_an_enabled_policy_writes_the_current_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: None)
    policy = tmp_path / "policy.yaml"
    policy.write_text("enabled: false\nsafe_repositories: []\n", encoding="utf-8")
    SCRIPT["install_policy"](policy, tmp_path / "backups", os.getgid(), enabled=True)
    value = yaml.safe_load(policy.read_text(encoding="utf-8"))
    assert value == {"enabled": True, "safe_repositories": [*PREVIOUS, REPOSITORY]}


def test_the_worker_write_policy_accepts_the_in_flight_allowlist() -> None:
    from engineering_control.policy.github_write import (
        PREVIOUS_WRITE_REPOSITORIES,
        WRITE_REPOSITORIES,
        GitHubWritePolicyDocument,
    )

    assert set(PREVIOUS) == PREVIOUS_WRITE_REPOSITORIES
    assert WRITE_REPOSITORIES == {*PREVIOUS, REPOSITORY}
    document = GitHubWritePolicyDocument(enabled=True, safe_repositories=tuple(PREVIOUS))
    assert REPOSITORY not in document.safe_repositories


# -- workflows: the merge ends the delivery ---------------------------------------------------

_MERGE_ONLY_PLAN = _plan()


def _merge_only_script(**overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    script: dict[str, dict[str, Any]] = {
        "gitops.prepare": {
            "status": "READY",
            "plan": _MERGE_ONLY_PLAN,
            "staging_flow": {
                "merge_only": True,
                "argo_application": None,
                "main_ci_workflow": None,
                "image_workflow": None,
                "release_tag_enabled": False,
                "pin_chart_to_merge": False,
            },
        },
        "gitops.pr_checks": {"status": "CHECKS_PASSED"},
        "gitops.merge": {"status": "MERGED", "target": "source", "merge_sha": MERGE_SHA},
        "gitops.notify_merged": {"status": "NOTIFIED"},
        "gitops.close_issue": {"status": "ISSUE_CLOSED"},
        "gitops.persist_result": {"status": "PERSISTED"},
    }
    script.update(overrides)
    return script


def _merge_only_input() -> StagingGitOpsDeliveryInput:
    return StagingGitOpsDeliveryInput(
        change_set_id=CHANGE_SET,
        project="gntik-ui",
        issue_repository=REPOSITORY,
        issue_number=12,
    )


def _patch_markers(history: Any) -> list[str]:
    return [
        str(event.marker_recorded_event_attributes)
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
    ]


@pytest.mark.skipif(_TEMPORAL_CLI is None, reason="requires the `temporal` CLI")
async def test_a_merge_only_delivery_ends_after_the_merge() -> None:
    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _merge_only_input(),
        _scripted_execute_control_step(_merge_only_script()),
    )
    assert result is not None and result["status"] == "MERGE_DELIVERED"
    assert result["evidence"]["merge_sha"] == MERGE_SHA
    steps = [request["step"] for request in _scheduled_steps(history)]
    assert steps == [
        "gitops.prepare",
        "gitops.pr_checks",
        "gitops.merge",
        "gitops.notify_merged",
        "gitops.close_issue",
        "gitops.persist_result",
    ]
    persisted = _scheduled_steps(history)[-1]["payload"]
    assert persisted["outcome"] == "MERGE_DELIVERED" and persisted["merge_sha"] == MERGE_SHA
    assert any("gitops-merge-only-delivery-v1" in marker for marker in _patch_markers(history))
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


@pytest.mark.skipif(_TEMPORAL_CLI is None, reason="requires the `temporal` CLI")
async def test_a_staging_flow_never_records_the_merge_only_patch() -> None:
    from test_workflow_replay import _gitops_input, _gitops_script

    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(_gitops_script()),
    )
    assert result is not None and result["status"] == "STAGING_VERIFIED"
    assert not any("gitops-merge-only-delivery-v1" in m for m in _patch_markers(history))
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


@pytest.mark.skipif(_TEMPORAL_CLI is None, reason="requires the `temporal` CLI")
async def test_a_merge_only_close_failure_blocks_after_the_merge() -> None:
    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _merge_only_input(),
        _scripted_execute_control_step(
            _merge_only_script(**{"gitops.close_issue": {"status": "BLOCKED"}})
        ),
    )
    assert result is not None and result["status"] == "BLOCKED"
    assert result["blocker"] == "DELIVERED_ISSUE_CLOSE_FAILED"
    persisted = _scheduled_steps(history)[-1]["payload"]
    assert persisted["outcome"] == "BLOCKED" and persisted["merge_sha"] == MERGE_SHA
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


@workflow.defn(name="IssueDeliveryWorkflow", sandboxed=False)
class _PublishedGntikUiDeliveryStub:
    """Stands in for IssueDeliveryWorkflow: the gntik-ui PR has been published."""

    @workflow.run
    async def run(self, request: IssueDeliveryInput) -> dict[str, Any]:
        return {"status": "SOURCE_PR", "context": {"change_set_id": CHANGE_SET}}


@pytest.mark.skipif(_TEMPORAL_CLI is None, reason="requires the `temporal` CLI")
async def test_the_backlog_loop_counts_a_merge_only_delivery_as_delivered() -> None:
    script = {
        "backlog.load_execution": {
            "status": "READY",
            "items": [{"repository": REPOSITORY, "issue_number": 12}],
        },
        "backlog.register_run": {"status": "REGISTERED"},
        "backlog.revalidate_gate": {
            "status": "READY",
            "finding_id": "finding-1",
            "risk_class": "R1",
        },
        "backlog.register_child_run": {"status": "REGISTERED"},
        "backlog.record_loop_state": {"status": "RECORDED"},
        "gitops.flow_for_project": {
            "status": "ENABLED",
            "enabled": True,
            "merge_only": True,
            "blockers": [],
        },
        **_merge_only_script(),
    }

    @activity.defn(name="execute_control_step")
    async def scripted(request: dict[str, Any]) -> dict[str, Any]:
        step = request["step"]
        if step == "github.refresh_mirrors":
            return {"status": "PASS"}
        if step not in script:
            raise AssertionError(f"unscripted step {step!r}")
        return _registered_child(step, request, script[step])

    workflows = [
        ExistingGitHubBacklogLoopWorkflow,
        _PublishedGntikUiDeliveryStub,
        StagingGitOpsDeliveryWorkflow,
    ]
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI) as env:
        async with (
            Worker(
                env.client,
                task_queue=CONTROL_TASK_QUEUE,
                workflows=workflows,
                activities=[scripted],
            ),
            Worker(
                env.client,
                task_queue=GITHUB_TASK_QUEUE,
                workflows=workflows,
                activities=[scripted],
            ),
        ):
            handle = await env.client.start_workflow(
                ExistingGitHubBacklogLoopWorkflow.run,
                ExistingGitHubBacklogLoopInput(
                    project="gntik-ui",
                    backlog_campaign_id="camp-160",
                    maximum_risk="R2",
                    delivery_mode="full-delivery",
                    continue_after_blocked_issue=False,
                ),
                id=f"new-160-merge-only-loop-{uuid.uuid4()}",
                task_queue=CONTROL_TASK_QUEUE,
                execution_timeout=timedelta(minutes=2),
            )
            result = await asyncio.wait_for(handle.result(), timeout=90)
            history = await handle.fetch_history()
    assert result["status"] == "COMPLETE"
    assert "completion" not in result  # delivered, not COMPLETE_NOTHING_DELIVERED
    staged = [item for item in result["results"] if item.get("stage") == "staging-gitops"]
    assert [item["status"] for item in staged] == ["MERGE_DELIVERED"]
    steps = [request["step"] for request in _scheduled_steps(history)]
    for absent in (
        "gitops.release_tag",
        "gitops.workflow_run",
        "gitops.environment_check",
        "gitops.image_pin",
        "gitops.trigger_sync",
        "gitops.verify_workloads",
    ):
        assert absent not in steps
    await Replayer(workflows=workflows).replay_workflow(history)
