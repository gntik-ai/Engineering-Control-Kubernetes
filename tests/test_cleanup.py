"""The clean-up proposes closing a record only on two reasons of different kinds or an
exact one; a model's check can give the second reason or speak for the record; and what
is proposed is closed only by a batch the administrator approves."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from engineering_control.dashboard import ranking
from engineering_control.dashboard.planning_health import measure
from engineering_control.db.models import (
    BacklogItem,
    ProjectEpic,
    ProjectReviewBudget,
    ProjectUserStory,
    WorkspaceAction,
)
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_delivery import plan, vector
from tests.test_primary_epics import Model, run
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"
STORY = "Audit executions"


def prepared(repo: Any, client: Any) -> tuple[dict[str, Any], dict[str, UUID]]:
    data = seed(repo)
    made = plan(repo, data)
    with repo.sessions.begin() as db:
        db.add(ProjectReviewBudget(project_id=data["project"], token_limit=600000))
    repo.delivery.refresh("falcone")
    return data, made


def records(client: Any, **params: Any) -> dict[str, dict[str, Any]]:
    found = client.get(BASE + "cleanup/records", params={"page_size": 100, **params})
    assert found.status_code == 200, found.text
    return {row["title"]: row for row in found.json()["items"]}


def ready(client: Any) -> dict[str, int]:
    kinds = client.get(BASE + "reorganize/batches").json()["kinds"]
    return {kind["kind"]: kind["ready"] for kind in kinds}


def answer(kind: str, quote: str, piece: str = "", piece_quote: str = "") -> Any:
    def said(asked: dict[str, Any]) -> dict[str, str]:
        return {
            "answer": kind,
            "piece": next((p["id"] for p in asked["pieces"] if p["title"] == piece), ""),
            "confidence": "high",
            "reason": "Nothing shown keeps an audit entry of executions.",
            "story_quote": quote,
            "piece_quote": piece_quote,
        }

    return said


def test_one_reason_proposes_nothing_and_a_models_check_gives_the_second(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data, _made = prepared(repo, client)
        found = records(client, kind="stories")
        # Nothing supports the Story: one reason, so it is listed and not proposed.
        assert [r["key"] for r in found[STORY]["reasons"]] == ["story_unsupported"]
        assert not found[STORY]["proposed"] and ready(client)["retire_stories"] == 0
        overview = client.get(BASE + "cleanup").json()
        check = overview["check"]
        assert (check["stories"], check["to_ask"]) == (1, 1) and check["likely"] > 0
        assert {r["key"]: (r["records"], r["proposed"]) for r in overview["reasons"]}[
            "story_unsupported"
        ] == (1, 0)

        started = post(
            client, "cleanup/runs", provider="fixture", model="analysis", token_cap=50_000
        )
        assert started.status_code == 200, started.text
        model = Model({STORY: answer("unsupported", "durable audit entry")})
        run(repo, model)
        # The model was shown the Story and the pieces of the project closest to it.
        [asked] = model.asked
        assert asked["story"]["title"] == STORY
        assert [piece["what"] for piece in asked["pieces"]] == ["document"]

        found = records(client, kind="stories")
        assert [r["family"] for r in found[STORY]["reasons"]] == ["absence", "model"]
        assert found[STORY]["proposed"] and not found[STORY]["withdrawn"]
        assert ready(client)["retire_stories"] == 1
        assert measure(repo.sessions, "falcone")["cleanup_proposed"] == 1

        batch = post(client, "reorganize/batches", kind="retire_stories").json()
        path = "reorganize/batches/" + batch["id"]
        [line] = client.get(BASE + path).json()["items"]
        assert line["child"]["title"] == STORY
        assert "No issue and no document" in line["reason"] and "audit entry" in line["reason"]
        assert post(client, f"{path}/lines/{line['id']}", verdict="right").status_code == 200
        assert post(client, path + "/approve").json()["applied"] == 1
        with repo.sessions() as db:
            assert db.get_one(ProjectUserStory, data[STORY]).closed_at is not None
            assert db.scalar(select(WorkspaceAction)) is None
        assert post(client, path + "/undo").status_code == 200
        with repo.sessions() as db:
            assert db.get_one(ProjectUserStory, data[STORY]).closed_at is None
    finally:
        client.close()


def test_a_model_that_names_the_storys_work_withdraws_the_proposal(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        prepared(repo, client)
        post(client, "cleanup/runs", provider="fixture", model="analysis", token_cap=50_000)
        said = answer(
            "supported",
            "durable audit entry",
            piece="Themes of the console",
            piece_quote="How the console is themed.",
        )
        run(repo, Model({STORY: said}))
        found = records(client, kind="stories")
        assert found[STORY]["withdrawn"] and not found[STORY]["proposed"]
        assert found[STORY]["model"]["answer"] == "supported"
        assert found[STORY]["model"]["parent"] == "Themes of the console"
        assert ready(client)["retire_stories"] == 0
        assert STORY in records(client, show="withdrawn")
        assert client.get(BASE + "cleanup").json()["check"]["answers"]["supported"] == 1
    finally:
        client.close()


def test_epics_and_issues_are_judged_by_the_same_rule(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data, _made = prepared(repo, client)
        pid = data["project"]
        with repo.sessions.begin() as db:
            empty = ProjectEpic(project_id=pid, title="Left over", description="Nothing here.")
            db.add(empty)
            items = {
                item.issue_number: item
                for item in db.scalars(select(BacklogItem).where(BacklogItem.project_id == pid))
            }
            # Issue 45 says the same as issue 62, which was closed as done; issue 42
            # says the same as the open and older issue 41... which is closed: no twin.
            ranking.keep_vector(db, pid, "issue", items[45].id, "m", "d", vector(0, 0, 0, 1))
            ranking.keep_vector(db, pid, "issue", items[62].id, "m", "d", vector(0, 0, 0, 1))
            items[42].description = "Duplicate of #44. Operators can see failed runs."
            items[42].issue_updated_at = datetime.now(UTC) - timedelta(days=400)
        found = records(client)
        assert found["Left over"]["proposed"]
        assert [r["key"] for r in found["Left over"]["reasons"]] == ["epic_empty"]
        twin = found["#45 Unrelated work"]
        assert [r["family"] for r in twin["reasons"]] == ["alike", "state"] and twin["proposed"]
        assert twin["reasons"][0]["twin"]["title"] == "#62 Refund a charge"
        stated = found["#42 Record error events"]
        assert {r["key"] for r in stated["reasons"]} == {"issue_stale", "issue_stated"}
        assert stated["proposed"]
        # The closed issue and the bug-free rule: a closed issue is never a candidate.
        assert not any(title.startswith("#62") for title in found)

        # Issues are listed, not closed by a batch; the Epic is.
        assert ready(client)["retire_epics"] == 1
        batch = post(client, "reorganize/batches", kind="retire_epics").json()
        path = "reorganize/batches/" + batch["id"]
        [line] = client.get(BASE + path).json()["items"]
        assert post(client, f"{path}/lines/{line['id']}", verdict="right").status_code == 200
        assert post(client, path + "/approve").json()["applied"] == 1
        with repo.sessions() as db:
            assert db.get_one(ProjectEpic, empty.id).closed_at is not None
        assert "Left over" not in records(client)
        assert post(client, path + "/undo").status_code == 200
        with repo.sessions() as db:
            assert db.get_one(ProjectEpic, empty.id).closed_at is None
    finally:
        client.close()


class GitHub:
    """Stands in for GitHub: remembers what was written on which issue."""

    def __init__(self) -> None:
        self.closed: dict[int, str | None] = {}
        self.comments: dict[int, list[str]] = {}

    async def repository_token(self, repository: str, permissions: dict[str, str]) -> str:
        assert permissions == {"issues": "write"}
        return "fixture"

    async def revoke(self, token: str) -> None:
        pass

    async def workspace_issue(self, repository: str, number: int) -> dict[str, Any]:
        return {
            "number": number,
            "state": "closed" if number in self.closed else "open",
            "state_reason": self.closed.get(number),
            "title": "Task",
            "labels": [],
            "closed_at": None,
            "updated_at": "2026-11-24T00:00:00Z",
        }

    async def comment_workspace_issue(
        self, repository: str, number: int, body: str, marker: str, *, token: str
    ) -> bool:
        said = self.comments.setdefault(number, [])
        if any(marker in comment for comment in said):
            return False
        said.append(body + "\n\n" + marker)
        return True

    async def close_workspace_issue(
        self, repository: str, number: int, *, token: str, reason: str | None = None
    ) -> None:
        self.closed[number] = reason


def test_proposed_issues_are_closed_on_github_by_an_approved_batch(tmp_path: Path) -> None:
    import asyncio
    from typing import cast

    from engineering_control.github_app import GitHubApp

    client, repo = _authenticated_client(tmp_path)
    try:
        data, _made = prepared(repo, client)
        pid = data["project"]
        with repo.sessions.begin() as db:
            items = {
                item.issue_number: item
                for item in db.scalars(select(BacklogItem).where(BacklogItem.project_id == pid))
            }
            # Issue 45 says the same as issue 62, which was closed as done.
            ranking.keep_vector(db, pid, "issue", items[45].id, "m", "d", vector(0, 0, 0, 1))
            ranking.keep_vector(db, pid, "issue", items[62].id, "m", "d", vector(0, 0, 0, 1))
            twin = items[45].id
        assert ready(client)["retire_issues"] == 1
        batch = post(client, "reorganize/batches", kind="retire_issues").json()
        path = "reorganize/batches/" + batch["id"]
        lines = client.get(BASE + path).json()
        assert lines["batch"]["final"] and not lines["batch"]["can_undo"]
        [line] = lines["items"]
        assert line["child"]["title"] == "#45 Unrelated work"
        assert line["parent"]["title"] == "#62 Refund a charge"
        # Nothing is asked of GitHub before the batch is approved.
        with repo.sessions() as db:
            assert db.scalar(select(WorkspaceAction)) is None
        assert post(client, f"{path}/lines/{line['id']}", verdict="right").status_code == 200
        done = post(client, path + "/approve")
        assert done.status_code == 200 and done.json()["applied"] == 1
        with repo.sessions() as db:
            [action] = db.scalars(select(WorkspaceAction)).all()
            assert (action.operation, action.number, action.status) == (
                "close_issue",
                45,
                "pending",
            )
            assert action.state_reason == "not_planned"
            assert "#62 Refund a charge" in (action.note or "")
            assert "reopen it" in (action.note or "")
            key = action.id

        github = GitHub()
        told = asyncio.run(repo.publishing.sync("falcone", key, cast(GitHubApp, github)))
        assert told["status"] == "done", told
        # Closed as not planned, with one comment that says why.
        assert github.closed == {45: "not_planned"}
        [comment] = github.comments[45]
        assert "Says nearly the same as another issue (#62 Refund a charge)" in comment
        assert f"engineering-control:closure:{key}" in comment
        with repo.sessions() as db:
            assert db.get_one(BacklogItem, twin).issue_state == "closed"
        # What was written on GitHub is not taken back from here.
        undo = post(client, path + "/undo")
        assert undo.status_code == 409 and "GitHub" in undo.text
        assert "#45 Unrelated work" not in records(client)
    finally:
        client.close()
