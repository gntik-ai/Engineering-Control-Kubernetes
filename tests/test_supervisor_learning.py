"""Saved process evidence, scoped guidance, historical receipts and deployable issues."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from engineering_control.config import get_settings
from engineering_control.dashboard.project_records import ProjectConflict, ProjectRecords
from engineering_control.dashboard.workspace_publishing import WorkspacePublishing
from engineering_control.db.models import (
    BacklogItem,
    ProjectRecord,
    ReviewFlowRun,
    SandboxRun,
    SandboxStep,
    SupervisorIncident,
    WorkspaceAction,
)
from engineering_control.github_app import GitHubApp
from engineering_control.supervisor.learning import BRIEF, ProcessLearning
from engineering_control.supervisor.operations import Operations
from tests.test_workspace_review import PublishingFixture

pytest_plugins = ("tests.test_supervisor",)
PROJECT = "falcone-workflow"


def test_benchmark_receipts_stay_in_history_and_release_their_alerts(world: Any) -> None:
    now = datetime.now(UTC)
    suite = str(uuid4())
    with world.sessions.begin() as db:
        project = db.scalar(select(ProjectRecord))
        runs = [
            ReviewFlowRun(
                project_id=project.id,
                flow_key="review-supervisor",
                flow_name=f"Supervisor benchmark · {method}",
                action="supervisor",
                settings={},
                version=0,
                started_at=now - timedelta(seconds=4),
                finished_at=now,
                outcome=state,
                findings={"benchmark": suite} if marked else {"observation": "real"},
            )
            for method, state, marked in (
                ("standard", "failed", True),
                ("rerank", "failed", True),
                ("standard", "succeeded", True),
                ("rerank", "succeeded", True),
                ("standard", "failed", False),
            )
        ]
        db.add_all(runs)
        db.flush()
        for row in runs[:2]:
            db.add(
                SupervisorIncident(
                    project_id=project.id,
                    kind="review_attention",
                    state="waiting",
                    ref=f"review:{row.id.hex}:failed",
                    summary=row.flow_name,
                    facts={"needs": "administrator"},
                )
            )
        real = str(runs[-1].id)
        failed = {str(r.id) for r in runs[:2]} | {real}
    watch = Operations(world.sessions, world.conversation)
    assert watch.watch(now) == 1
    assert watch.watch(now + timedelta(minutes=2)) == 0
    assert [i["facts"]["review"] for i in watch.listing(project=PROJECT)["items"]] == [real]
    with world.sessions() as db:
        cleared = db.scalars(
            select(SupervisorIncident).where(SupervisorIncident.state == "handled")
        ).all()
        assert len(cleared) == 2 and all(i.closed_at for i in cleared)
        assert db.scalar(select(func.count()).select_from(SandboxRun)) == 0
    reviews = world.toolbox.reviews
    attention = reviews.listing([PROJECT], state="attention")
    assert [i["id"] for i in attention["items"]] == [real]
    assert attention["counts"] == {"succeeded": 2, "failed": 3}
    assert {i["id"] for i in reviews.listing([PROJECT], state="failed")["items"]} == failed
    assert all(
        reviews.detail(PROJECT, "review", UUID(k))["findings"]["benchmark"] == suite
        for k in failed - {real}
    )
    learned = world.toolbox.learning.summary(PROJECT)
    assert learned["sampled"] == 1 and learned["patterns"][0]["id"] == real


def seed(world: Any) -> str:
    now = datetime.now(UTC)
    with world.sessions.begin() as db:
        pid = db.scalar(select(ProjectRecord.id).where(ProjectRecord.name == PROJECT))
        keys = []
        for offset, state, minutes in (
            (6, "failed", 5),
            (4, "failed", 5),
            (2, "succeeded", 35),
            (0, "succeeded", 2),
            (32 * 24, "failed", 3),
        ):
            end = now - timedelta(hours=offset)
            start = end - timedelta(minutes=minutes)
            run = SandboxRun(
                project_id=pid,
                kind="deliver_issue",
                state=state,
                title="Deliver issue",
                data={"flow": {"key": "release", "version": 7}},
                started_at=start,
                finished_at=end,
            )
            db.add(run)
            db.flush()
            db.add(
                SandboxStep(
                    run_id=run.id,
                    position=1,
                    name="verify",
                    state=state,
                    outcome="tests_failed" if state == "failed" else "verified",
                    started_at=start,
                    finished_at=end,
                )
            )
            keys.append(str(run.id))
        return keys[1]


def post(world: Any, *targets: str) -> UUID:
    world.conversation.post(
        "Investigate release failures", channel="console", thread=PROJECT, projects=list(targets)
    )
    return UUID(world.conversation.listing(thread=PROJECT)["turn"]["run_id"])


def call(world: Any, turn: UUID, name: str, args: dict[str, Any]) -> Any:
    return asyncio.run(world.toolbox.call(turn, world.definitions.current(), name, args))


def decision(world: Any, name: str, accept: bool = True) -> Any:
    pending = next(a for a in world.toolbox.pending(thread=PROJECT) if a["tool"] == name)
    return world.toolbox.decide(
        UUID(pending["action_id"]), accept=accept, via="console", thread=PROJECT
    )


def test_observed_failures_slow_steps_and_approved_guidance_are_scoped(world: Any) -> None:
    key = seed(world)
    other = "other-project"
    ProjectRecords(world.sessions).create(
        name=other, display_name="Other", repository="example/other"
    )
    turn = post(world)
    learning = ProcessLearning(world.sessions)
    summary = learning.summary(PROJECT)
    assert summary["sampled"] == 4
    fail, slow = summary["patterns"]
    assert (fail["id"], fail["step"], fail["occurrences"], fail["samples"]) == (key, "verify", 2, 4)
    assert fail["latest_success"]["at"] > fail["at"]
    assert slow["signal"] == "bottleneck" and slow["seconds"] == 35 * 60
    assert learning.summary(other)["sampled"] == 0
    args = dict(
        project=PROJECT,
        source="run",
        evidence_id=key,
        reason="Two releases have failed.",
        approach="Check the pinned digest against the running image before retrying.",
    )
    assert call(world, turn, "remember_approach", {**args, "project": other}).error
    assert call(world, turn, "remember_approach", {**args, "evidence_id": str(uuid4())}).error
    assert not call(world, turn, "remember_approach", args).error
    assert not learning.summary(PROJECT)["approaches"]
    decision(world, "remember_approach", False)
    assert not learning.summary(PROJECT)["approaches"]
    for message in (
        args["approach"],
        "Read the deployment revision and verify the image digest first.",
    ):
        assert not call(world, turn, "remember_approach", {**args, "approach": message}).error
        decision(world, "remember_approach")
    (saved,) = learning.turn_guidance({"thread": PROJECT})
    assert saved["approach"] == message and saved["id"] == key
    assert learning.turn_guidance({"thread": other}) == []
    with world.sessions.begin() as db:
        incident = SupervisorIncident(
            project_id=db.scalar(select(ProjectRecord.id).where(ProjectRecord.name == PROJECT)),
            kind="run_failed",
            ref=key,
            state="open",
            summary="Delivery failed",
            facts={},
        )
        db.add(incident)
        db.flush()
        iid = str(incident.id)
    assert learning.turn_guidance({"incidents": [iid]}) == [saved]


def test_improvement_issue_can_be_published_and_enter_delivery(world: Any) -> None:
    key = seed(world)
    turn = post(world)
    args = dict(
        project=PROJECT,
        source="run",
        evidence_id=key,
        reason="Two matching execution receipts.",
        target="engineering_control",
        title="Verify the pinned image before reporting deployment",
        category="bug",
        component="Release verification in the controller",
        observed="A successful sync can leave a stale image running.",
        change="Compare the pinned and running image digests before closing the delivery.",
        acceptance="A stale digest keeps delivery open and identifies the affected workload.",
        tests="Exercise matching and stale digest fixtures and assert delivery status.",
        rollout="Deploy the new controller through the existing Argo CD application.",
        verification="Run the stale digest fixture in staging and observe a blocked delivery.",
        rollback="Restore the previous controller image pin and verify readiness.",
    )
    assert call(world, turn, "propose_process_issue", {**args, "tests": ""}).error
    assert not call(world, turn, "propose_process_issue", args).error
    with world.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProjectRecord)) == 1
    result = decision(world, "propose_process_issue")["result"]
    target = result["project"]
    iid = UUID(result["id"])
    assert result["repository"] == get_settings().controller_repository
    assert world.toolbox.learning.summary(PROJECT)["controller_project"] == target
    with world.sessions() as db:
        item = db.get(BacklogItem, iid)
        assert item.workspace_review == "pending" and item.issue_number is None
        assert item.proposed_by == "supervisor" and item.facts["process_evidence"]["id"] == key
        assert all(v in item.description for v in BRIEF.values())
        assert not db.scalar(select(func.count()).select_from(WorkspaceAction))
    assert world.backlog.get(iid)["section"]["flow"] == "deliver-issue"
    with pytest.raises(ProjectConflict, match="awaiting approval"):
        world.runs.start_item(iid, toolchain={})
    assert call(world, turn, "propose_process_issue", args).error  # destination now needs selection
    world.conversation.answered(turn, "The draft is ready.")
    with world.sessions.begin() as db:
        db.get(SandboxRun, turn).state = "succeeded"
    turn = post(world, target)
    assert not call(world, turn, "propose_process_issue", args).error
    again = decision(world, "propose_process_issue")["result"]
    assert again["id"] == result["id"] and again["existing"]

    class Repository(PublishingFixture):
        async def repository_token(self, repository: str, permissions: dict[str, str]) -> str:
            assert repository == result["repository"] and permissions == {"issues": "write"}
            return "fixture"

    remote = Repository()
    remote.lost = False
    publish = WorkspacePublishing(world.sessions)
    pub = publish.approve(target, iid)
    assert pub is not None
    assert asyncio.run(publish.sync(target, pub, cast(GitHubApp, remote)))["status"] == "done"
    assert remote.creates == 1 and args["verification"] in remote.issues[100]["body"]
    assert world.runs.start_item(iid, toolchain={})["state"] == "queued"
    assert world.backlog.get(iid)["issue_number"] == 100
    (issue,) = world.toolbox.learning.summary(PROJECT)["improvements"]
    assert issue["github_url"].endswith("/issues/100") and issue["run_state"] == "queued"


def test_explicit_technical_backlog_proposals_require_the_delivery_brief(world: Any) -> None:
    turn = post(world)
    item = {
        "title": "Correct release reporting",
        "section": "issues",
        "reason": "Release status is stale.",
    }
    assert call(world, turn, "propose_items", {"project": PROJECT, "items": [item]}).error
    item["delivery"] = {key: f"Specific {key} for stale release reporting" for key in BRIEF}  # type: ignore[assignment]
    assert not call(world, turn, "propose_items", {"project": PROJECT, "items": [item]}).error
    with world.sessions() as db:
        row = db.scalar(select(BacklogItem).where(BacklogItem.title == item["title"]))
        assert (
            row.workspace_review == "pending"
            and "## Post-deployment verification" in row.description
        )


def test_ranked_observations_are_paged_without_losing_counts(world: Any) -> None:
    now = datetime.now(UTC)
    with world.sessions.begin() as db:
        pid = db.scalar(select(ProjectRecord.id))
        for n in range(12):
            db.add(
                ReviewFlowRun(
                    project_id=pid,
                    flow_key=f"review-{n}",
                    flow_name=f"Review {n}",
                    action="issues",
                    version=3,
                    settings={},
                    started_at=now - timedelta(minutes=2),
                    finished_at=now - timedelta(seconds=n),
                    outcome="failed",
                )
            )
    learning = world.toolbox.learning
    first = learning.summary(PROJECT)
    second = learning.summary(PROJECT, page=2)
    assert first["pattern_count"] == second["pattern_count"] == 12
    assert first["pages"] == 2 and len(first["patterns"]) == 8
    assert [r["flow"] for r in second["patterns"]] == [f"review-{n}" for n in range(8, 12)]
    assert all(r["version"] == 3 for r in second["patterns"])
