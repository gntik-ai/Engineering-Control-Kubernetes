"""A judge is measured on pairs whose truth is known, within a sum of dollars and without
the project's review tokens. A model that writes code only is kept out of every review."""

import copy
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from engineering_control.dashboard import judging, review_flows
from engineering_control.db.models import ProjectReviewBudget, ProviderAccount
from engineering_control.engine import validate
from engineering_control.engine.builtin import STARTERS
from engineering_control.engine.document import parse
from engineering_control.models import decisions
from engineering_control.models.providers import code_only, family_of, maker
from engineering_control.models.service import Question, Reply
from tests.test_ask_a_model import _judging, _rules
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_orphans import run
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"
INKLING = "thinkingmachines/inkling-2-small"


class Verdicts:
    """A judge: says yes to the pairs whose User Story it is told to accept."""

    def __init__(self, accepts: set[str], cost: float = 0.001) -> None:
        self.accepts, self.cost = accepts, cost
        self.asked: list[dict[str, Any]] = []

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        asked = json.loads(question.prompt)
        self.asked.append(asked)
        said = "yes" if asked["story"]["title"] in self.accepts else "no"
        usage = {"tokens": 30, "cost": self.cost}
        data = {"verdict": said, "reason": "The texts say so."}
        return Reply(data, "", "fixture", "analysis", uuid4(), "Test", usage, 0.1, 1)


def priced(repo: Any, models: list[str]) -> None:
    with repo.sessions.begin() as db:
        account = db.scalars(select(ProviderAccount)).one()
        account.models = models
        account.prices = {model: {"input": 1.0, "output": 2.0} for model in models}


def test_a_judge_is_measured_on_known_pairs_within_dollars(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(judging, "MAKERS", frozenset({"fixture"}))
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        repo.planning_links.reconcile_project("falcone")
        # The project's review tokens are used up: a measurement does not need them.
        with repo.sessions.begin() as db:
            db.add(ProjectReviewBudget(project_id=data["project"], token_limit=10, used=10))
        shown = client.get(BASE + "judge-trials").json()
        issue_story = next(kind for kind in shown["kinds"] if kind["kind"] == "issue_story")
        # Issue 44 was put under a User Story by hand; the Story most like it besides is
        # presumed wrong.
        assert issue_story["right"] == {"you": 1} and issue_story["wrong"] == {"presumed": 1}
        assert issue_story["sample"] == {"right": 1, "wrong": 1}
        assert (shown["spent"], shown["limit"], shown["run_dollars"]) == (0, 3.0, 0.5)
        # Its account does not say what the model costs: not measured within dollars.
        (model,) = shown["models"]
        assert (model["model"], model["decision"], model["priced"]) == ("analysis", False, False)
        start = {"provider": "fixture", "model": "analysis", "dollars": 0.5}
        unpriced = post(client, "judge-trials/issue_story/runs", **start)
        assert unpriced.status_code == 422 and "what this model costs" in unpriced.text
        priced(repo, ["analysis"])
        assert post(client, "judge-trials/nothing/runs", **start).status_code == 422
        too_much = post(client, "judge-trials/issue_story/runs", **{**start, "dollars": 4})
        assert too_much.status_code == 422

        assert post(client, "judge-trials/issue_story/runs", **start).status_code == 200
        lenient = Verdicts({"Audit executions", "Respond to failures"})
        run(repo, lenient)
        assert len(lenient.asked) == 2 and all("claim" in asked for asked in lenient.asked)
        shown = client.get(BASE + "judge-trials").json()
        (trial,) = shown["trials"]
        assert (trial["kind"], trial["maker"], trial["run"]["state"]) == (
            "issue_story",
            "fixture",
            "complete",
        )
        assert (trial["verdicts"], trial["right"], trial["wrong"]) == (2, 1, 1)
        # It let the wrong pair through and refused no right one.
        assert (trial["accepted_wrong"], trial["accepted_wrong_share"]) == (1, 1.0)
        assert (trial["refused_right"], trial["refused_right_share"]) == (0, 0.0)
        assert trial["yes_right_share"] == 0.5
        assert (trial["presumed"], trial["accepted_presumed"]) == (1, 1)
        assert (trial["cost"], trial["cost_each"], trial["tokens_each"]) == (0.002, 0.001, 30)
        assert shown["spent"] == 0.002
        with repo.sessions() as db:
            budget = db.scalars(select(ProjectReviewBudget)).one()
            assert (budget.used, budget.reserved) == (10, 0)
        errors = BASE + "judge-trials/runs/" + trial["run"]["id"] + "/records"
        (line,) = client.get(errors, params={"show": "accepted_wrong"}).json()["items"]
        assert (line["truth"], line["said"], line["known_from"]) == ("no", "yes", "presumed")
        assert client.get(errors, params={"show": "refused_right"}).json()["total"] == 0

        # A stricter judge refuses the wrong pair.
        assert post(client, "judge-trials/issue_story/runs", **start).status_code == 200
        run(repo, Verdicts({"Respond to failures"}))
        strict = client.get(BASE + "judge-trials").json()["trials"][0]
        assert (strict["accepted_wrong"], strict["refused_right"]) == (0, 0)
        assert strict["yes_right_share"] == 1.0

        # A measurement stops at its dollars.
        small = post(client, "judge-trials/issue_story/runs", **{**start, "dollars": 0.05})
        assert small.status_code == 200
        run(repo, Verdicts(set(), cost=0.06))
        stopped = client.get(BASE + "judge-trials").json()["trials"][0]
        assert stopped["verdicts"] == 1 and "limit of $0.05" in stopped["run"]["detail"]
        assert stopped["run"]["dollar_cap"] == 0.05

        # All of a project's measurements together have a limit too.
        monkeypatch.setattr(judging, "PROJECT_DOLLARS", 0.07)
        over = post(client, "judge-trials/issue_story/runs", **start)
        assert over.status_code == 409 and "is left" in over.text
    finally:
        client.close()


def test_a_decision_model_is_asked_through_its_own_interface_and_read_as_a_verdict() -> None:
    assert judging.verdict_of({"link_probability": 0.9, "scope": "partial"})[0] == "yes"
    assert judging.verdict_of({"link_probability": 0.1, "scope": "unrelated"})[0] == "no"
    # A probability and a scope that contradict each other are no verdict.
    assert judging.verdict_of({"link_probability": 0.9, "scope": "unrelated"})[0] == "unclear"
    assert judging.verdict_of({"link_probability": None, "scope": "unclear"})[0] == "unclear"
    body = decisions.body(
        "openrouter",
        "google/gemini-decision-1",
        decisions.INSTRUCTIONS[decisions.EPIC_RUBRIC],
        "{}",
        False,
        {},
        decisions.EPIC_RUBRIC,
    )
    assert "Epic" in body["questions"]["link"]["instructions"]
    assert set(body["questions"]["scope"]["criteria"]) == set(decisions.CHOICES)


def test_a_model_that_writes_code_only_is_never_asked_in_a_review(tmp_path: Path) -> None:
    assert code_only(INKLING) and code_only("ThinkingMachines/Inkling-2-Mini")
    assert not code_only("google/gemini-3.1-flash-lite")
    # Through OpenRouter a model is of its maker's family, not OpenRouter's.
    assert maker("openrouter", "google/gemini-decision-1") == "google"
    assert family_of("openrouter", "anthropic/claude-opus-5.5") == family_of("claude", "")
    assert family_of("openrouter", "openai/gpt-6") == family_of("chatgpt", "")
    assert family_of("openrouter", INKLING) == "thinkingmachines"

    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        priced(repo, ["analysis", INKLING])
        with repo.sessions.begin() as db:
            db.add(ProjectReviewBudget(project_id=data["project"], token_limit=100_000, used=0))
        with repo.sessions() as db:
            offered = {entry["model"] for entry in review_flows.model_choices(db)}
            for_code = {entry["model"] for entry in review_flows.model_choices(db, code=True)}
        assert offered == {"analysis"} and for_code == {"analysis", INKLING}
        # No list a review chooses from offers it, and a run that names it is refused.
        assert [m["model"] for m in client.get(BASE + "orphans").json()["models"]] == ["analysis"]
        refused = post(client, "orphans/runs", provider="fixture", model=INKLING, token_cap=9000)
        assert refused.status_code == 422
        first = post(
            client,
            "orphans/runs",
            provider="fixture",
            model="analysis",
            token_cap=9000,
            first_provider="fixture",
            first_model=INKLING,
        )
        assert first.status_code == 422
        assert all(
            m["model"] != INKLING for m in client.get(BASE + "judge-trials").json()["models"]
        )
    finally:
        client.close()

    # In a flow it may write the change and choose the steps, and nothing else.
    loop = copy.deepcopy(STARTERS["starter-model-loop"].as_dict())
    for task in loop["tasks"]:
        if task["task"] in ("model.choose", "model.edit"):
            task["with"] = {**task["with"], "provider": "openrouter", "model": INKLING}
    assert not [p for p in validate.errors(parse(loop)) if "writes code only" in p["text"]]
    judged = _rules(_judging(provider="openrouter", model=INKLING))
    assert any("writes code only" in text for text in judged)
