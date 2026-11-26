"""The other reviews that ask a model have a judge too: the project's judge is asked the
same question without seeing the answer, and each review says what the two agree on."""

import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select

from engineering_control.dashboard import (
    connection_analysis,
    execution_plans,
    issue_coverage,
    review_budget,
    review_settings,
    workspace_review,
)
from engineering_control.db.models import ProjectRecord, ProviderAccount, WorkspaceModelUsage
from engineering_control.models import decisions
from engineering_control.models.service import ModelError, Question, Reply
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"


class Two:
    """Two models behind one account: each says what it is told to."""

    def __init__(self, says: dict[str, Any]) -> None:
        self.says = says
        self.asked: list[str] = []

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        ((_provider, model),) = question.choices
        self.asked.append(model)
        said = self.says[model]
        if isinstance(said, Exception):
            raise said
        usage = {"tokens": 30, "cost": 0.001}
        return Reply(said, "", "fixture", model, uuid4(), "Test", usage, 0.1, 1)


def test_the_projects_judge_is_asked_the_same_question_unseen(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(review_settings, "family_of", lambda provider, model: model)
    client, repo = _authenticated_client(tmp_path)
    try:
        seed(repo)
        with repo.sessions.begin() as db:
            db.scalars(select(ProviderAccount)).one().models = ["analysis", "other"]
            project = db.scalars(
                select(ProjectRecord.id).where(ProjectRecord.name == "falcone")
            ).one()
        asked = Question(
            choices=(("fixture", "analysis"),),
            system="Say.",
            prompt="{}",
            schema={"type": "object"},
            max_tokens=100,
            seconds=10,
        )

        def judged(models: Two) -> Reply | None:
            async def both() -> Reply | None:
                first = await review_budget.ask_review(
                    repo.sessions, project, cast(Any, models), asked, "connection_review"
                )
                return await review_budget.ask_judge(
                    repo.sessions, project, cast(Any, models), asked, first, "connection_review"
                )

            return asyncio.run(both())

        # No judge is named: the answer stands as it would without one.
        models = Two({"analysis": {"n": 1}, "other": {"n": 2}})
        assert judged(models) is None and models.asked == ["analysis"]

        save = {
            "dollar_limit": 5,
            "answer_provider": "fixture",
            "answer_model": "analysis",
            "judge_provider": "fixture",
            "judge_model": "other",
            "makers": ["fixture"],
            "rounds": 0,
        }
        assert post(client, "review-settings", **save).status_code == 200
        models = Two({"analysis": {"n": 1}, "other": {"n": 2}})
        second = judged(models)
        assert second is not None and second.data == {"n": 2} and second.model == "other"
        assert models.asked == ["analysis", "other"]
        # What the judge used is counted under the review's name, as its judge.
        with repo.sessions() as db:
            workflows = [
                usage["workflow"] for usage in db.scalars(select(WorkspaceModelUsage.usage))
            ]
        assert workflows.count("connection_review_judge") == 1
        shown = client.get(BASE + "review-settings").json()["costs"]
        labels = {row["workflow"]: row["label"] for row in shown}
        assert labels["connection_review_judge"] == "Connections between projects: judge"

        # A judge that gives no answer leaves the first answer as it is.
        failing = Two({"analysis": {"n": 1}, "other": ModelError("busy", "No answer.")})
        assert judged(failing) is None
        # The model that answered is not its own judge.
        own = {**save, "answer_model": "other", "judge_model": "analysis"}
        assert post(client, "review-settings", **own).status_code == 200
        models = Two({"analysis": {"n": 1}})
        assert judged(models) is None and models.asked == ["analysis"]
    finally:
        client.close()


def test_each_review_keeps_only_what_the_judge_says_too() -> None:
    # The coverage of Stories by issues: a match becomes a link without approval, so
    # only one the judge makes too is kept, and it is full only where both say so.
    mine = [
        {"issue": "i1", "story": "s1", "full": True, "evidence": "a"},
        {"issue": "i2", "story": "s1", "full": True, "evidence": "b"},
        {"issue": "i3", "story": "s2", "full": False, "evidence": "c"},
    ]
    theirs = {
        "matches": [{"issue": "i1", "story": "s1", "full": False}, {"issue": "i3", "story": "s2"}]
    }
    kept = issue_coverage.both_matched(mine, theirs)
    assert [(m["issue"], m["full"]) for m in kept] == [("i1", False), ("i3", False)]
    assert issue_coverage.both_matched(mine, None) == mine

    # The execution order: an Issue the judge reads as the other type is ambiguous.
    reply = {
        "issues": [
            {"id": "a", "type": "bug", "reason": "It fails.", "ambiguous": False},
            {"id": "b", "type": "enhancement", "reason": "It adds.", "ambiguous": False},
        ]
    }
    second = {"issues": [{"id": "a", "type": "enhancement"}, {"id": "b", "type": "enhancement"}]}
    marked = execution_plans.doubted(reply, second, "other")["issues"]
    assert marked[0]["ambiguous"] and "reads it as an enhancement" in marked[0]["reason"]
    assert marked[1] == reply["issues"][1]

    # Connections: each suggestion says whether the judge made it too.
    suggestions = [
        {"source": "x", "action": "link", "target": "t1", "kind": "issues", "title": ""},
        {"source": "x", "action": "link", "target": "t2", "kind": "issues", "title": ""},
        {
            "source": "y",
            "action": "create",
            "target": "",
            "kind": "stories",
            "title": "Export audit log",
        },
    ]
    judge = {
        "suggestions": [
            {"source": "x", "action": "link", "target": "t1"},
            {
                "source": "y",
                "action": "create",
                "kind": "stories",
                "title": "Audit log export to file",
            },
        ]
    }
    said = connection_analysis.judged(suggestions, judge, "other")
    assert [entry["judge"]["agreed"] for entry in said] == [True, False, True]
    assert said[0]["judge"]["model"] == "other"

    # Discovery: an issue that is there keeps its judge's word on its parents; a record
    # proposed anew, on whether the judge proposed one like it.
    there = [{"number": 7, "parents": ["s1"]}, {"number": 8, "parents": []}]
    records = {
        "records": [
            {"number": 7, "include": True, "parents": ["s1"]},
            {"number": 8, "include": True, "parents": ["s2"]},
        ]
    }
    marked = workspace_review.judged_proposals(
        there, records, "other", "issues", True, {"s1", "s2"}
    )
    assert [entry["judged"]["agreed"] for entry in marked] == [True, False]
    new = [
        {"title": "Rotate bucket keys", "parents": ["e1"]},
        {"title": "Dark mode", "parents": ["e1"]},
    ]
    proposed = {"stories": [{"title": "Rotate and revoke bucket keys"}]}
    marked = workspace_review.judged_proposals(new, proposed, "other", "stories", False, set())
    assert [entry["judged"]["agreed"] for entry in marked] == [True, False]

    # Review evidence: where the project has a judge the second opinion is asked about a
    # contribution the first model found too, and the two have to say the same.
    found = {"scope": "partial", "link_probability": 0.9, "review_reason": "It adds the retry."}
    assert not decisions.needs_second_opinion(found)
    assert decisions.needs_second_opinion(found, None, True)
    skipped = decisions.advisory(found, None)
    assert skipped["status"] == "advisory" and "skipped" in skipped["reason"]
    assert decisions.advisory(found, None, None, True)["status"] == "needs_review"
    other = {"scope": "unrelated", "link_probability": 0.1, "review_reason": "Another feature."}
    assert decisions.advisory(found, other, None, True)["status"] == "needs_review"
    same = decisions.advisory(found, dict(found), None, True)
    assert (same["status"], same["reason"]) == (
        "advisory",
        "Both models found the same contribution.",
    )
