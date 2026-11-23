"""Administrative impact previews and durable, narrowly scoped technical closures."""

import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.planning_lifecycle import PlanningLifecycle
from engineering_control.dashboard.project_records import ProjectConflict, ProjectInvalid
from engineering_control.dashboard.workspace_publishing import (
    WorkspacePublishing,
    approve_issue,
    enqueue,
)
from engineering_control.db.models import (
    BacklogItem,
    ConnectionFinding,
    PlanningLifecycleReceipt,
    ProjectConnection,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    WorkspaceAction,
)
from engineering_control.github_app import GitHubApp, GitHubAppError
from tests.test_dashboard_web import _authenticated_client


def graph(sessions: sessionmaker[Session]) -> dict[str, UUID]:
    with sessions.begin() as db:
        left = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
        peer = db.scalars(
            select(ProjectRecord).where(ProjectRecord.name == "falcone-serverless")
        ).one()
        first, second = sorted([left.id, peer.id])
        connection = ProjectConnection(first_id=first, second_id=second, owner_id=left.id)
        db.add(connection)

        def entity(model: Any, title: str, owner: ProjectRecord, **extra: Any) -> Any:
            row = model(project_id=owner.id, title=title, **extra)
            db.add(row)
            db.flush()
            return row

        def issue(title: str, owner: ProjectRecord, number: int | None = None) -> BacklogItem:
            return entity(
                BacklogItem,
                title,
                owner,
                source="github" if number else "manual",
                state="proposed",
                workspace_review="approved" if number else "pending",
                issue_number=number,
                repository=owner.source_repository if number else None,
                issue_state="open" if number else None,
            )

        def external(src: Any, sk: str, dst: Any, dk: str, status: str = "approved") -> None:
            db.add(
                ConnectionFinding(
                    connection_id=connection.id,
                    source_project=src.project_id,
                    source_kind=sk,
                    source_id=src.id,
                    target_kind=dk,
                    target_id=dst.id,
                    action="link",
                    origin="manual",
                    status=status,
                    fingerprint=uuid4().hex,
                    identity=uuid4().hex,
                )
            )

        root, other = (entity(ProjectEpic, n, left) for n in ("Selected", "Other"))
        peer_epic = entity(ProjectEpic, "Peer epic", peer)
        one, two, shared, protected = (
            entity(ProjectUserStory, n, left)
            for n in ("First story", "Second story", "Shared story", "Shared across projects")
        )
        foreign = entity(ProjectUserStory, "Exclusive peer story", peer)
        for epic, story in (
            (root, one),
            (root, two),
            (root, shared),
            (other, shared),
            (root, protected),
        ):
            db.add(ProjectStoryEpic(project_id=left.id, epic_id=epic.id, story_id=story.id))
        external(protected, "stories", peer_epic, "epics")
        external(root, "epics", foreign, "stories")
        only = issue("Only one parent", left, 211)
        both = issue("Two closing story parents", left, 212)
        common = issue("Shared with peer story", left, 213)
        standalone = issue("Unlinked bug", left, 214)
        draft = issue("Unpublished task", left)
        remote = issue("Cross-project task", peer, 411)
        unrelated = issue("Unapproved suggestion", peer, 412)
        for child, parent in ((only, one), (both, one), (both, two), (common, one), (draft, one)):
            db.add(ProjectIssueStory(project_id=left.id, item_id=child.id, story_id=parent.id))
        external(common, "issues", foreign, "stories")
        # Older references can identify a typed Issue by its repository number.
        external(one, "stories", remote, "issues")
        ref = db.scalar(select(ConnectionFinding).where(ConnectionFinding.target_id == remote.id))
        ref.target_id, ref.target_number = None, remote.issue_number
        external(one, "stories", unrelated, "issues", "pending")
        return {
            r.title: r.id
            for r in (
                root,
                other,
                one,
                two,
                shared,
                protected,
                foreign,
                only,
                both,
                common,
                standalone,
                draft,
                remote,
                unrelated,
            )
        }


class Remote:
    def __init__(self) -> None:
        self.closed: set[tuple[str, int]] = set()
        self.writes: list[tuple[str, int]] = []
        self.lost = False
        self.planning = False

    async def repository_token(self, repository: str, permissions: dict[str, str]) -> str:
        assert permissions == {"issues": "write"}
        return "fixture"

    async def revoke(self, token: str) -> None:
        pass

    async def workspace_issue(self, repository: str, number: int) -> dict[str, Any]:
        return {
            "number": number,
            "state": "closed" if (repository, number) in self.closed else "open",
            "title": "Task",
            "labels": ["epic"] if self.planning else [],
            "closed_at": None,
            "updated_at": "2026-11-01T00:00:00Z",
        }

    async def close_workspace_issue(self, repository: str, number: int, *, token: str) -> None:
        self.closed.add((repository, number))
        self.writes.append((repository, number))
        if self.lost:
            self.lost = False
            raise GitHubAppError("Response lost after the write")


def exercise(sessions: sessionmaker[Session]) -> None:
    ids = graph(sessions)
    life, outbox = PlanningLifecycle(sessions), WorkspacePublishing(sessions)
    root = ids["Selected"]
    preview = life.preview("falcone", "epics", root, "close")
    by_name = {r["title"]: r for r in preview["items"]}
    assert {n for n, r in by_name.items() if r["action"] == "close"} == {
        "Selected",
        "First story",
        "Second story",
        "Exclusive peer story",
        "Only one parent",
        "Unpublished task",
        "Cross-project task",
    }
    assert {n for n, r in by_name.items() if r["action"] == "keep"} == {
        "Shared story",
        "Shared across projects",
        "Two closing story parents",
        "Shared with peer story",
    }
    assert by_name["Cross-project task"]["repository"] == "gntik-ai/falcone-serverless"
    assert {r["number"] for r in by_name.values() if r.get("github") == "close"} == {211, 411}
    with pytest.raises(ProjectConflict):
        life.confirm("falcone", "epics", root, "close", None)
    # A child gets a second parent after the administrator read the list.
    with sessions.begin() as db:
        src = db.get(ProjectEpic, root)
        assert src and src.closed_at is None
        db.add(
            ProjectIssueStory(
                project_id=src.project_id,
                item_id=ids["Only one parent"],
                story_id=ids["Shared story"],
            )
        )
    with pytest.raises(ProjectConflict):
        life.confirm("falcone", "epics", root, "close", preview["token"])
    assert not outbox.due()
    changed = life.preview("falcone", "epics", root, "close")
    assert changed["token"] != preview["token"]
    assert next(r for r in changed["items"] if r["title"] == "Only one parent")["action"] == "keep"
    for _ in range(2):
        assert (
            life.confirm("falcone", "epics", root, "close", changed["token"])["state"] == "closed"
        )
    with sessions() as db:
        assert db.get(ProjectUserStory, ids["Exclusive peer story"]).closed_at is not None
        assert db.get(ProjectUserStory, ids["Shared across projects"]).closed_at is None
        assert db.get(BacklogItem, ids["Unpublished task"]).workspace_review == "closed_draft"
        assert db.get(BacklogItem, ids["Unlinked bug"]).issue_state == "open"
        assert db.get(BacklogItem, ids["Cross-project task"]).issue_state == "open"
        assert db.scalar(select(func.count()).select_from(PlanningLifecycleReceipt)) == 1
        assert db.scalar(select(func.count()).select_from(WorkspaceAction)) == 1
    remote = Remote()
    remote.lost = True
    ((name, key),) = outbox.due()
    fake = cast(GitHubApp, remote)
    assert name == "falcone-serverless"
    assert asyncio.run(outbox.sync(name, key, fake))["status"] == "needs_attention"
    assert asyncio.run(outbox.sync(name, key, fake))["status"] == "done"
    assert remote.writes == [("gntik-ai/falcone-serverless", 411)]
    with sessions() as db:
        assert db.get(BacklogItem, ids["Cross-project task"]).issue_state == "closed"
        assert db.get(BacklogItem, ids["Shared with peer story"]).issue_state == "open"
    with pytest.raises(ProjectInvalid):
        life.preview("falcone-serverless", "issues", ids["Cross-project task"], "delete")
    # A direct Story deletion archives the Story and removes only its own links.
    plan = life.preview("falcone", "stories", ids["First story"], "delete")
    life.confirm("falcone", "stories", ids["First story"], "delete", plan["token"])
    with sessions() as db:
        assert db.get(ProjectUserStory, ids["First story"]).archived_at
        assert db.get(BacklogItem, ids["Two closing story parents"]).issue_state == "open"
        assert db.scalars(
            select(ProjectIssueStory.story_id).where(
                ProjectIssueStory.item_id == ids["Two closing story parents"]
            )
        ).all() == [ids["Second story"]]
    draft = ids["Unpublished task"]
    plan = life.preview("falcone", "issues", draft, "delete")
    life.confirm("falcone", "issues", draft, "delete", plan["token"])
    with sessions() as db:
        assert db.get(BacklogItem, draft).workspace_review == "deleted_draft"
        assert db.scalar(select(func.count()).select_from(WorkspaceAction)) == 1


def test_cross_project_impact_staleness_replay_and_github(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        exercise(repo.sessions)
        closed = repo.planning.page("falcone-serverless", "issues", 1, 25, issue_state="closed")
        assert [r["number"] for r in closed["items"]] == [411]
        assert not repo.planning.page("falcone", "issues", 1, 25, issue_state="draft")["items"]
    finally:
        client.__exit__(None, None, None)


def test_delete_epic_and_direct_issue_closure_are_confirmed(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ids = graph(repo.sessions)
        base = "/api/dashboard/projects/falcone/planning"
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]
        issue = base + "/issues/" + str(ids["Unlinked bug"])
        ip = client.get(issue + "/impact?operation=close").json()
        assert len(ip["items"]) == 1 and ip["items"][0]["number"] == 214
        root = base + "/epics/" + str(ids["Selected"])
        ep = client.get(root + "/impact?operation=delete").json()
        assert (
            client.post(
                root + "/remove", json={"csrf_token": csrf, "confirmation": ip["token"]}
            ).status_code
            == 409
        )
        assert (
            client.post(
                root + "/remove", json={"csrf_token": csrf, "confirmation": ep["token"]}
            ).status_code
            == 200
        )
        with repo.sessions() as db:
            assert db.get(ProjectEpic, ids["Selected"]).archived_at
            assert db.get(ProjectUserStory, ids["First story"]).closed_at
            assert not db.get(ProjectUserStory, ids["First story"]).archived_at
            assert not db.get(ProjectUserStory, ids["Shared story"]).closed_at
            assert db.get(BacklogItem, ids["Unlinked bug"]).issue_state == "open"
        assert (
            client.post(
                f"{base}/stories/{ids['First story']}/state",
                json={"csrf_token": csrf, "state": "open"},
            ).status_code
            == 409
        )
        # The legacy backlog preserves published Issues that predate workspace review.
        with repo.sessions.begin() as db:
            db.get(BacklogItem, ids["Unlinked bug"]).workspace_review = None
        assert (
            client.post(
                f"/api/dashboard/items/{ids['Unlinked bug']}/remove",
                json={"csrf_token": csrf},
            ).status_code
            == 409
        )
        ip = client.get(issue + "/impact?operation=close").json()
        assert (
            client.post(
                issue + "/state",
                json={"csrf_token": csrf, "state": "closed", "confirmation": ip["token"]},
            ).status_code
            == 200
        )
        remote = Remote()
        remote.planning = True
        with repo.sessions() as db:
            act = db.scalars(select(WorkspaceAction).where(WorkspaceAction.number == 214)).one()
        result = asyncio.run(repo.publishing.sync("falcone", act.id, cast(GitHubApp, remote)))
        assert result["status"] == "needs_attention" and not remote.writes
        with repo.sessions() as db:
            assert db.get(WorkspaceAction, act.id).status == "blocked"
    finally:
        client.__exit__(None, None, None)


def test_closing_an_already_publishing_draft_waits_for_its_github_identity(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ids = graph(repo.sessions)
        key = ids["Unpublished task"]
        with repo.sessions.begin() as db:
            project = db.scalar(select(ProjectRecord).where(ProjectRecord.name == "falcone"))
            publishing = approve_issue(db, project, key)
        life = repo.planning.lifecycle
        plan = life.preview("falcone", "issues", key, "close")
        assert plan["items"][0]["github"] == "after_publication"
        life.confirm("falcone", "issues", key, "close", plan["token"])
        remote = Remote()

        class Publisher(Remote):
            async def create_workspace_issue(
                self, repo: str, title: str, body: str, *, token: str
            ) -> int:
                return 311

        assert (
            asyncio.run(repo.publishing.sync("falcone", publishing, cast(GitHubApp, Publisher())))[
                "status"
            ]
            == "done"
        )
        with repo.sessions() as db:
            action = db.scalars(
                select(WorkspaceAction).where(WorkspaceAction.operation == "close_issue")
            ).one()
            assert action.number == 311 and action.status == "pending"
        assert (
            asyncio.run(repo.publishing.sync("falcone", action.id, cast(GitHubApp, remote)))[
                "status"
            ]
            == "done"
        )
        assert remote.writes == [("gntik-ai/falcone", 311)]
        with repo.sessions.begin() as db:
            foreign = ids["Unlinked bug"]
            forged = enqueue(db, project.id, "close_issue", foreign, "gntik-ai/falcone", 214).id
        assert (
            asyncio.run(repo.publishing.sync("falcone", forged, cast(GitHubApp, remote)))["status"]
            == "needs_attention"
        )
        with repo.sessions.begin() as db:
            db.get(WorkspaceAction, forged).confirmation_token = plan["token"]
        assert (
            asyncio.run(repo.publishing.sync("falcone", forged, cast(GitHubApp, remote)))["status"]
            == "needs_attention"
        )
        assert len(remote.writes) == 1
    finally:
        client.__exit__(None, None, None)
