"""New planning enters one durable review, with finite human-approved follow-ups."""

import asyncio
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from starlette.testclient import TestClient

from engineering_control.dashboard.planning_analysis import PlanningAnalysis
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.dashboard.workspace_issues import import_issues
from engineering_control.db.models import (
    BacklogItem,
    PlanningReview,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    ProviderAccount,
    WorkspaceAction,
    WorkspaceCandidate,
    WorkspaceImport,
    WorkspaceModelUsage,
)
from engineering_control.models.service import Question, Reply
from tests.test_dashboard_web import _authenticated_client

BASE = "/api/dashboard/projects/falcone/"


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[tuple[TestClient, DashboardRepository]]:
    client, repo = _authenticated_client(tmp_path)
    try:
        yield client, repo
    finally:
        client.__exit__(None, None, None)


def post(client: TestClient, path: str, **body: Any) -> Any:
    return client.post(
        BASE + path,
        json={**body, "csrf_token": client.get("/api/dashboard/session").json()["csrf_token"]},
    )


def configure(repo: DashboardRepository, client: TestClient) -> None:
    with repo.sessions.begin() as db:
        db.add(
            ProviderAccount(
                name="Test account",
                provider="fixture",
                state="ready",
                sealed_key="test-envelope",
                models=["chosen"],
            )
        )
    reply = post(client, "planning-reviews/model", provider="fixture", model="chosen")
    assert reply.status_code == 200, reply.text


def empty(**updates: Any) -> dict[str, Any]:
    return {"questions": [], "parents": [], "stories": [], "issues": [], **updates}


def suggestion(title: str, *parents: str) -> dict[str, Any]:
    return {
        "title": title,
        "description": "Expose the failing execution and verify the trace ID.",
        "reason": "The new runtime needs failure evidence that existing work does not cover.",
        "parents": list(parents),
    }


class Model:
    def __init__(self, answer: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        assert question.choices == (("fixture", "chosen"),) and not retry
        context = json.loads(question.prompt)
        self.calls.append(context)
        answer = {"matches": []} if "imported_issues" in context else self.answer(context)
        return Reply(answer, "", "fixture", "chosen", uuid4(), "Test", {"tokens": 25}, 0.2, 1)


def drain(worker: PlanningAnalysis, model: Any) -> None:
    for _ in range(25):
        if not asyncio.run(worker.tick(model)):
            return
    raise AssertionError("Review chain did not finish")


def test_creation_coverage_across_epics_follow_up_and_explicit_publication(workspace: Any) -> None:
    client, repo = workspace
    pid = repo.projects.identity_of("falcone")
    with repo.sessions.begin() as db:
        obs = ProjectEpic(project_id=pid, title="Observability")
        db.add(obs)
        db.flush()
        story = ProjectUserStory(project_id=pid, title="Inspect failed jobs")
        db.add(story)
        db.flush()
        db.add(ProjectStoryEpic(project_id=pid, story_id=story.id, epic_id=obs.id))
        db.add(
            BacklogItem(
                project_id=pid,
                title="Existing trace API",
                source="github",
                state="done",
                issue_number=71,
                issue_state="closed",
                workspace_review="approved",
            )
        )
        db.add(
            BacklogItem(
                project_id=pid,
                title="Earlier 👩‍💻 tooling",
                source="github",
                state="done",
                issue_number=72,
                issue_state="closed",
                workspace_review="approved",
            )
        )
        db.add(
            WorkspaceImport(
                project_id=pid, stage="complete", repository="gntik-ai/falcone", branch="main"
            )
        )
    epic = post(
        client,
        "planning/epics",
        title="Migrate runtime",
        description="Move jobs to FaaS with traceable failures.",
    ).json()["id"]
    assert repo.planning_analysis.claim() is None
    assert (
        post(client, "planning-reviews/model", provider="fixture", model="unknown").status_code
        == 422
    )
    configure(repo, client)

    def answer(c: dict[str, Any]) -> dict[str, Any]:
        source = c["source"]
        if source["id"] == epic:
            assert any(
                r["title"] == "Existing trace API" and r["state"] == "closed" for r in c["records"]
            )
            return empty(
                stories=[suggestion("Trace a function", epic, str(obs.id))],
                issues=[
                    suggestion("Add runtime correlation", str(story.id)),
                    suggestion("Existing trace API", str(story.id)),
                ],
            )
        if source["kind"] == "stories":
            assert c["scope"] == "children"
            return empty(
                parents=source["parents"],
                issues=[suggestion("Attach function spans", source["id"])],
                stories=[suggestion("Recursive story", epic)],
            )
        assert c["scope"] == "verify"
        return empty(
            parents=source["parents"], issues=[suggestion("Recursive issue", str(story.id))]
        )

    model = Model(answer)
    drain(repo.planning_analysis, model)
    data = client.get(BASE + "planning/epics", params={"record": epic}).json()
    result = next(x for x in data["items"] if x["id"] == epic)["automatic_review"]
    assert result["status"] == "proposals" and len(result["proposals"]) == 2
    prop = client.get(BASE + "planning/stories").json()["items"][0]
    assert prop["review_reason"] and set(prop["epic_ids"]) == {epic, str(obs.id)}
    draft = client.get(BASE + "planning/issues", params={"issue_state": "draft"}).json()
    assert [r["title"] for r in draft["items"]] == ["Add runtime correlation"]
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(WorkspaceAction)) == 0
        assert db.get(WorkspaceImport, pid).stage == "complete"
    assert post(client, "planning/stories/decisions", ids=[prop["id"]]).status_code == 200
    assert post(client, "planning/stories/decisions", ids=[prop["id"]]).status_code == 200
    drain(repo.planning_analysis, model)
    drafts = client.get(BASE + "planning/issues", params={"issue_state": "draft"}).json()["items"]
    assert {r["title"] for r in drafts} == {"Add runtime correlation", "Attach function spans"}
    assert next(r for r in drafts if r["title"] == "Attach function spans")["story_ids"] == [
        prop["id"]
    ]
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(WorkspaceAction)) == 0
    assert (
        post(client, "planning/issues/decisions", ids=[r["id"] for r in drafts]).status_code == 200
    )
    drain(repo.planning_analysis, model)
    assert len([c for c in model.calls if "source" in c]) == 4
    assert any("imported_issues" in c for c in model.calls)
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(WorkspaceCandidate)) == 3
        assert list(db.scalars(select(WorkspaceAction.operation))) == ["create", "create"]
        assert db.scalar(select(func.count()).select_from(WorkspaceModelUsage)) == len(model.calls)


def test_intake_needs_input_then_resumes_after_correction(workspace: Any) -> None:
    client, repo = workspace
    configure(repo, client)
    pid = repo.projects.identity_of("falcone")
    with repo.sessions.begin() as db:
        parent = ProjectEpic(project_id=pid, title="Runtime")
        db.add(parent)
        db.flush()
    uid = post(client, "planning/stories", title="Runtime alerts", description="Alerts.").json()[
        "id"
    ]
    assert client.get(BASE + "planning/stories/pending").json()["ids"] == []
    assert post(client, "planning/stories/decisions", ids=[uid]).status_code == 422
    model = Model(
        lambda c: (
            empty(questions=["Which operator needs this alert?"])
            if c["source"]["description"] == "Alerts."
            else empty(parents=[str(parent.id)])
        )
    )
    drain(repo.planning_analysis, model)
    row = client.get(BASE + "planning/stories").json()["items"][0]
    assert (
        row["automatic_review"]["questions"] == ["Which operator needs this alert?"]
        and row["epic_ids"] == []
    )
    assert (
        post(
            client,
            "planning/stories/" + uid + "/proposal",
            title="Runtime alerts",
            description="As an on-call operator I can see a failure alert within one minute.",
            parents=[],
        ).status_code
        == 200
    )
    drain(PlanningAnalysis(repo.sessions), model)
    row = client.get(BASE + "planning/stories").json()["items"][0]
    assert (
        row["automatic_review"]["status"] == "matched"
        and row["epic_ids"] == [str(parent.id)]
        and row["proposal"]
    )
    assert post(client, "planning/stories/decisions", ids=[uid]).status_code == 200
    drain(repo.planning_analysis, model)
    with repo.sessions() as db:
        assert db.get(PlanningReview, (pid, "stories", UUID(uid))).status == "covered"
        assert db.scalar(select(func.count()).select_from(PlanningReview)) == 1
        assert db.get(ProjectUserStory, UUID(uid)) is not None
    assert post(client, "planning-reviews/stories/" + uid + "/retry").status_code == 409


def test_incremental_github_inventory_keeps_identity_and_ambiguous_orphans(workspace: Any) -> None:
    client, repo = workspace
    pid = repo.projects.identity_of("falcone")
    with repo.sessions.begin() as db:
        db.add(
            WorkspaceImport(
                project_id=pid,
                stage="complete",
                repository="gntik-ai/falcone",
                branch="main",
                provider="fixture",
                model="chosen",
            )
        )
        parent = ProjectUserStory(project_id=pid, title="Diagnose errors")
        db.add(parent)
        db.flush()
    issues = [
        {"number": n, "state": state, "title": title, "labels": [label], "body": title + " details"}
        for n, state, title, label in (
            (11, "closed", "Record trace IDs", "task"),
            (12, "open", "Investigate errors", "task"),
            (13, "open", "Story: Read failures", "story"),
        )
    ]
    for _ in range(2):
        with repo.sessions.begin() as db:
            import_issues(db, db.get(ProjectRecord, pid), issues + issues)
    model = Model(
        lambda c: (
            empty(parents=[str(parent.id)])
            if c["source"]["title"] == "Record trace IDs"
            else empty(questions=["Which outcome does this support?"])
        )
    )
    drain(repo.planning_analysis, model)
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(PlanningReview)) == 3
        assert db.scalar(select(func.count()).select_from(ProjectIssueStory)) == 1
        assert db.scalar(select(func.count()).select_from(WorkspaceAction)) == 0
        assert (
            db.scalar(select(BacklogItem.issue_state).where(BacklogItem.issue_number == 11))
            == "closed"
        )
    orphan = client.get(BASE + "planning/issues", params={"issue_state": "open"}).json()["items"][0]
    assert orphan["orphan"] and orphan["automatic_review"]["status"] == "needs_input"
    assert client.get(BASE + "planning/stories").json()["items"][0]["number"] == 13
    assert (
        post(
            client, "planning/issues/" + orphan["id"] + "/stories", story_ids=[str(parent.id)]
        ).status_code
        == 200
    )
    drain(repo.planning_analysis, Model(lambda _: empty(parents=[str(parent.id)])))
    assert not client.get(BASE + "planning/issues", params={"issue_state": "open"}).json()["items"][
        0
    ]["orphan"]


def test_checkpoint_recovers_expired_lease_and_rejects_old_results(workspace: Any) -> None:
    client, repo = workspace
    pid = repo.projects.identity_of("falcone")
    configure(repo, client)
    with repo.sessions.begin() as db:
        db.add_all(
            ProjectEpic(project_id=pid, id=UUID(int=n + 1), title=f"Existing epic {n}")
            for n in range(80)
        )
    uid = post(
        client,
        "planning/epics",
        title="Cross-cutting change",
        description="Move the service registry.",
    ).json()["id"]
    worker = repo.planning_analysis
    claim = worker.claim()
    work = worker.prepare(*claim)
    assert work.screen and worker.claim() is None
    worker.apply(work, {"relevant": [str(UUID(int=1))]})
    claim = worker.claim()
    interrupted = worker.prepare(*claim)
    assert interrupted.screen and interrupted.last and str(UUID(int=1)) in interrupted.ids
    with repo.sessions.begin() as db:
        db.get(PlanningReview, (pid, "epics", UUID(uid))).due_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    restarted = PlanningAnalysis(repo.sessions)
    resumed = restarted.prepare(*restarted.claim())
    assert resumed.ids == interrupted.ids and resumed.lease != interrupted.lease
    restarted.apply(interrupted, {"relevant": []})
    restarted.apply(resumed, {"relevant": [str(UUID(int=80))]})
    final = restarted.prepare(*restarted.claim())
    assert not final.screen and final.ids == {str(UUID(int=80))}
    assert (
        post(client, "planning/epics/" + uid + "/update", title="Changed during review").status_code
        == 200
    )
    restarted.apply(final, empty(stories=[suggestion("Stale proposal", str(UUID(int=80)))]))
    with repo.sessions() as db:
        job = db.get(PlanningReview, (pid, "epics", UUID(uid)))
        assert job.status == "queued" and job.progress == {}
        assert db.scalar(select(func.count()).select_from(WorkspaceCandidate)) == 0


def test_story_and_issue_check_other_epics_and_reject_foreign_parents(workspace: Any) -> None:
    client, repo = workspace
    configure(repo, client)
    pid = repo.projects.identity_of("falcone")
    foreign_pid = repo.projects.identity_of("llmwiki")
    with repo.sessions.begin() as db:
        primary = ProjectEpic(project_id=pid, title="Execution")
        other = ProjectEpic(project_id=pid, title="Audit")
        foreign = ProjectEpic(project_id=foreign_pid, title="Another project's Audit")
        db.add_all([primary, other, foreign])
        db.flush()
        archived = WorkspaceCandidate(
            project_id=pid,
            kind="stories",
            identity="old",
            title="Record every job",
            description="Already reviewed",
            parents=[str(other.id)],
            decision="dismissed",
        )
        db.add(archived)
    story = post(
        client,
        "planning/stories",
        title="Execute a job",
        description="As an operator I can run a job and get its tracking ID.",
        epic_ids=[str(primary.id)],
    ).json()["id"]
    issue = post(
        client,
        "planning/issues",
        title="Emit the tracking ID",
        description="Expose the ID in each response.",
        story_ids=[story],
    ).json()["id"]

    def answer(c: dict[str, Any]) -> dict[str, Any]:
        source = c["source"]
        assert c["scope"] == "full"
        assert all(r["id"] != str(foreign.id) for r in c["records"])
        if source["id"] == story:
            return empty(
                parents=[str(primary.id)],
                issues=[suggestion("Test ID propagation", story)],
                stories=[
                    suggestion("Read a job audit entry", str(other.id)),
                    suggestion("Record every job", str(other.id)),
                ],
            )
        return empty(
            parents=[story], stories=[suggestion("Audit the response ID", str(foreign.id))]
        )

    model = Model(answer)
    drain(repo.planning_analysis, model)
    plan = repo.planning.page("falcone", "stories", 1, 10)
    derived = [x for x in plan["items"] if x["proposal"]]
    assert len(derived) == 1 and derived[0]["epic_ids"] == [str(other.id)]
    # A foreign model-supplied ID never becomes a parent or partially writes a proposal.
    assert len(model.calls) == 2
    with repo.sessions() as db:
        job = db.get(PlanningReview, (pid, "issues", UUID(issue)))
        assert job.status == "queued" and job.attempts == 1
        assert not db.scalar(
            select(WorkspaceCandidate.id).where(WorkspaceCandidate.title == "Audit the response ID")
        )
    with repo.sessions.begin() as db:
        db.get(PlanningReview, (pid, "issues", UUID(issue))).due_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    drain(
        repo.planning_analysis,
        Model(
            lambda _: empty(
                parents=[story], stories=[suggestion("Audit the response ID", str(other.id))]
            )
        ),
    )
    with repo.sessions() as db:
        assert db.get(PlanningReview, (pid, "issues", UUID(issue))).status == "proposals"
        assert not db.scalar(select(WorkspaceAction.id))
    # Missing text asks for a correction before spending tokens or generating related work.
    blank = post(client, "planning/epics", title="Audit report").json()["id"]
    drain(repo.planning_analysis, Model(lambda _: pytest.fail("Unexpected model call")))
    with repo.sessions() as db:
        assert db.get(PlanningReview, (pid, "epics", UUID(blank))).status == "needs_input"


def test_intake_screens_only_possible_parents_and_meters_the_review(workspace: Any) -> None:
    client, repo = workspace
    configure(repo, client)
    pid = repo.projects.identity_of("falcone")
    with repo.sessions.begin() as db:
        epic = ProjectEpic(project_id=pid, title="Release", description="Release safely.")
        story = ProjectUserStory(
            project_id=pid,
            title="Verify a release",
            description="As the maintainer I verify each release.",
        )
        db.add_all([epic, story])
        for i in range(200):
            db.add(
                BacklogItem(
                    project_id=pid,
                    title=f"Technical item {i}",
                    description="Work for a different intake.",
                    state="proposed",
                    source="github",
                    issue_number=i + 1,
                    workspace_review="approved",
                )
            )
        db.flush()
        eid = str(epic.id)
        db.add(
            PlanningReview(
                project_id=pid,
                kind="stories",
                record_id=story.id,
                scope="verify",
                title=story.title,
            )
        )
    prompts = []

    def answer(ctx: dict[str, Any]) -> dict[str, Any]:
        prompts.append(ctx)
        return empty(parents=[eid])

    drain(repo.planning_analysis, Model(answer))
    assert len(prompts) == 1
    assert [row["id"] for row in prompts[0]["records"]] == [eid]
    assert repo.planning_analysis.status("falcone", 1)["usage_24h"]["requests"] == 1
    assert repo.planning_analysis.active_counts() == []
