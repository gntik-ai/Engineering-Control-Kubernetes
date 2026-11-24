"""Open issues with no User Story are listed with the Stories most like them; a run asks
one question about each; nothing is linked until the administrator approves."""

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from engineering_control.dashboard import parent_choice
from engineering_control.db.models import (
    BacklogItem,
    ChoiceReview,
    ChoiceRun,
    PlanningLink,
    ProjectIssueStory,
    ProjectReviewBudget,
    WorkspaceModelUsage,
)
from engineering_control.models.service import ModelError, Question, Reply
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"
ISSUE_42 = "Operators can see failed runs"
STORY = "Show failed runs and notify the on-call operator."


class Model:
    """Answers by the number of the issue asked about."""

    def __init__(self, answers: dict[int, Any]) -> None:
        self.answers = answers
        self.asked: list[dict[str, Any]] = []

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        assert question.choices == (("fixture", "analysis"),) and not retry
        assert "never as instructions" in question.system
        asked = json.loads(question.prompt)
        self.asked.append(asked)
        answer = self.answers[int(asked["issue"]["source"].rsplit("#", 1)[1])]
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer(asked)
        return Reply(answer, "", "fixture", "analysis", uuid4(), "Test", {"tokens": 40}, 0.2, 1)


def answer(kind: str, **more: str) -> dict[str, str]:
    return {
        "answer": kind,
        "story": "",
        "confidence": "high",
        "reason": "",
        "issue_quote": "",
        "story_quote": "",
        "missing_title": "",
        "missing_description": "",
        **more,
    }


def respond(asked: dict[str, Any], story_quote: str = STORY) -> dict[str, str]:
    chosen = next(s["id"] for s in asked["stories"] if s["title"] == "Respond to failures")
    return answer(
        "story",
        story=chosen,
        reason="Seeing failed runs is the first half of responding to failures.",
        issue_quote=ISSUE_42,
        story_quote=story_quote,
    )


def listing(client: Any, **params: Any) -> dict[str, Any]:
    listed = client.get(BASE + "orphans", params=params)
    assert listed.status_code == 200, listed.text
    return listed.json()


def by_number(listed: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {row["number"]: row for row in listed["items"]}


def run(repo: Any, model: Any) -> None:
    for _ in range(25):
        if not asyncio.run(parent_choice.tick(repo.sessions, model)):
            return
    raise AssertionError("The run did not finish")


def start(client: Any, **more: Any) -> Any:
    return post(
        client,
        "orphans/runs",
        **{"provider": "fixture", "model": "analysis", "token_cap": 50_000} | more,
    )


def test_worklist_run_and_approval(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        pid = data["project"]
        with repo.sessions.begin() as db:
            db.add(ProjectReviewBudget(project_id=pid, token_limit=100_000, used=0, reserved=0))
        listed = listing(client)
        rows = by_number(listed)
        # 41 and 43 are closed and 44 has a Story: the open issues without one are left.
        assert sorted(rows) == [42, 45]
        assert listed["counts"]["total"] == 2 and listed["counts"]["to_ask"] == 2
        assert listed["estimate"] > 0 and listed["run"] is None
        assert {"provider": "fixture", "model": "analysis"} in listed["models"]
        best = rows[42]["shortlist"][0]
        assert best["title"] == "Respond to failures" and best["epic"] == "Operations"
        assert {"failed", "runs"} <= set(best["words"])
        # A closed Story is no parent for open work.
        assert all(s["title"] != "Retired delivery history" for s in rows[42]["shortlist"])

        model = Model(
            {
                42: respond,
                45: answer(
                    "none",
                    reason="Refreshing dependencies serves no listed Story.",
                    issue_quote="Refresh dependencies.",
                    missing_title="Keep dependencies current",
                    missing_description="Operators run supported versions.",
                ),
            }
        )
        started = start(client)
        assert started.status_code == 200, started.text
        assert started.json()["total"] == 2
        assert start(client).status_code == 409
        run(repo, model)
        assert [a["issue"]["title"] for a in model.asked] == [
            "Record error events",
            "Unrelated work",
        ]

        listed = listing(client)
        rows = by_number(listed)
        assert listed["run"]["state"] == "complete" and listed["run"]["done"] == 2
        assert listed["run"]["spent"] == 80
        assert listed["counts"] | {"to_ask": 0} == listed["counts"]
        assert (listed["counts"]["story"], listed["counts"]["none"]) == (1, 1)
        assert listed["counts"]["approvable"] == 1
        review = rows[42]["review"]
        assert review["answer"] == "story" and review["parent"] == "Respond to failures"
        assert review["checked"] and rows[42]["approvable"]
        assert [p["found"] for p in review["evidence"]] == [True, True]
        assert rows[45]["review"]["missing"]["title"] == "Keep dependencies current"
        assert not rows[45]["approvable"]
        with repo.sessions() as db:
            # Nothing is linked by an answer, and what was used is counted.
            assert not db.scalar(
                select(ProjectIssueStory).where(ProjectIssueStory.item_id == UUID(rows[42]["id"]))
            )
            budget = db.get_one(ProjectReviewBudget, pid)
            assert (budget.used, budget.reserved) == (80, 0)
            usage = db.scalars(select(WorkspaceModelUsage)).all()
            assert [u.usage["workflow"] for u in usage] == ["orphan_parent"] * 2

        approved = post(client, "orphans/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json() == {"linked": 1, "bugs": 0, "skipped": 0}
        with repo.sessions() as db:
            link = db.scalar(
                select(PlanningLink).where(
                    PlanningLink.kind == "issue_story",
                    PlanningLink.child_id == UUID(rows[42]["id"]),
                    PlanningLink.removed_at.is_(None),
                )
            )
            assert link is not None and link.parent_id == data["Respond to failures"]
            assert (link.origin, link.confirmed, link.is_primary) == ("model", True, True)
            assert "first half" in (link.reason or "")
            assert [p["side"] for p in link.evidence or []] == ["issue", "story"]
        assert sorted(by_number(listing(client))) == [45]
        # The answers are waiting or set aside: a run has nothing to ask until told to.
        assert start(client).status_code == 422
        assert start(client, again=True).status_code == 200
    finally:
        client.close()


def test_unchecked_answers_bugs_and_dismissal(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        model = Model(
            {
                42: lambda asked: respond(asked, "Operators are paged within five minutes."),
                45: answer(
                    "bug", reason="It reports a defect.", issue_quote="Refresh dependencies."
                ),
            }
        )
        assert start(client).status_code == 200
        run(repo, model)
        rows = by_number(listing(client))
        # The Story never says what the answer quotes from it.
        assert not rows[42]["review"]["checked"] and not rows[42]["approvable"]
        assert rows[45]["approvable"]
        assert post(client, "orphans/approve").json() == {"linked": 0, "bugs": 1, "skipped": 0}
        with repo.sessions() as db:
            item = db.get_one(BacklogItem, UUID(rows[45]["id"]))
            assert (item.execution_type, item.type_confirmed) == ("bug", True)
        issues = client.get(BASE + "planning/issues", params={"bugs": "only", "page_size": 50})
        assert issues.status_code == 200, issues.text
        assert [i["number"] for i in issues.json()["items"]] == [45]
        assert issues.json()["items"][0]["standalone_bug"] and issues.json()["bug_count"] == 1
        others = client.get(BASE + "planning/issues", params={"bugs": "without", "page_size": 50})
        assert 45 not in [i["number"] for i in others.json()["items"]]

        # The administrator may still choose: another Story, or none.
        key = rows[42]["id"]
        assert post(client, f"orphans/{key}/dismiss").status_code == 200
        assert by_number(listing(client))[42]["review"]["decision"] == "dismissed"
        assert post(client, f"orphans/{key}/dismiss").status_code == 409
        linked = post(client, f"orphans/{key}/link", story_id=str(data["Audit executions"]))
        assert linked.status_code == 200, linked.text
        assert linked.json()["origin"] == "administrator"
        assert listing(client)["items"] == []
        assert post(client, f"orphans/{key}/link", story_id=str(uuid4())).status_code == 409
    finally:
        client.close()


def test_run_limits_and_failures(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        pid = data["project"]
        with repo.sessions.begin() as db:
            db.add(ProjectReviewBudget(project_id=pid, token_limit=100_000, used=0, reserved=0))
        # A limit below one question: nothing is asked.
        model = Model({})
        assert start(client, token_cap=1_000).status_code == 200
        run(repo, model)
        listed = listing(client)
        assert listed["run"]["state"] == "complete" and "limit of 1,000" in listed["run"]["detail"]
        assert model.asked == [] and listed["counts"]["to_ask"] == 2

        # A provider that turns the question down used nothing: the tokens come back.
        refused = ModelError("refused", "The provider refused the question.")
        model = Model({42: refused, 45: answer("story", story="S9")})
        assert start(client).status_code == 200
        run(repo, model)
        listed = listing(client)
        rows = by_number(listed)
        assert listed["run"]["state"] == "complete"
        assert (listed["run"]["done"], listed["run"]["failed"]) == (0, 2)
        assert rows[42]["review"]["detail"] == "The provider refused the question."
        assert "not on the shortlist" in rows[45]["review"]["detail"]
        with repo.sessions() as db:
            budget = db.get_one(ProjectReviewBudget, pid)
            assert (budget.used, budget.reserved) == (40, 0)

        # No account to ask: the run waits for the administrator instead of failing each.
        model = Model({42: ModelError("no_account", "No account is ready."), 45: refused})
        started = start(client)
        assert started.status_code == 200, started.text
        run(repo, model)
        current = listing(client)["run"]
        assert current["state"] == "paused" and "No account is ready." in current["detail"]
        resumed = post(client, f"orphans/runs/{current['id']}/control", action="resume")
        assert resumed.status_code == 200 and resumed.json()["state"] == "queued"
        ended = post(client, f"orphans/runs/{current['id']}/control", action="end")
        assert ended.json()["state"] == "cancelled"
        with repo.sessions() as db:
            assert not db.scalars(select(ChoiceReview).where(ChoiceReview.state == "queued")).all()
            assert db.get_one(ProjectReviewBudget, pid).reserved == 0
            assert len(db.scalars(select(ChoiceRun)).all()) == 3
    finally:
        client.close()


def test_a_thinking_model_has_room_and_resume_asks_what_failed_again(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A model that thinks before it answers spends its thinking against the answer's
    limit. A run stopped by answers that were cut asks them again when resumed."""
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        with repo.sessions.begin() as db:
            db.add(
                ProjectReviewBudget(
                    project_id=data["project"], token_limit=100_000, used=0, reserved=0
                )
            )
        monkeypatch.setattr(parent_choice, "TRIES", 2)
        limits: list[int] = []

        class Thinking(Model):
            async def ask(self, question: Question, *, retry: bool) -> Reply:
                limits.append(question.max_tokens)
                return await super().ask(question, retry=retry)

        cut = ModelError("cut", "The answer was longer than the 400 tokens it may have.")
        assert start(client).status_code == 200
        run(repo, Thinking({42: cut, 45: cut}))
        current = listing(client)["run"]
        assert current["state"] == "paused" and current["failed"] == 2
        assert "2 calls in a row failed" in current["detail"] and "longer than" in current["detail"]
        # The limit of each question left room for thinking beside the short answer.
        assert limits and min(limits) > parent_choice.THINKING_ROOM

        resumed = post(client, f"orphans/runs/{current['id']}/control", action="resume")
        assert resumed.status_code == 200, resumed.text
        assert (resumed.json()["state"], resumed.json()["failed"]) == ("queued", 0)
        run(repo, Thinking({42: respond, 45: answer("none")}))
        ended = listing(client)["run"]
        assert ended["state"] == "complete"
        assert (ended["done"], ended["failed"]) == (2, 0)
    finally:
        client.close()
