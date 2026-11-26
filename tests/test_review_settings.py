"""A project's reviews are set in one place: a limit in dollars a month, the models that
answer and judge, the makers its texts may be sent to, and how often an answer goes back."""

import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.dashboard import parent_choice, review_budget, review_settings
from engineering_control.db.models import (
    BacklogItem,
    ProjectRecord,
    ProjectReviewBudget,
    ReviewSettings,
)
from engineering_control.models.service import ModelError, Question, Reply
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_judging import Readers, priced
from tests.test_orphans import run
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"
AUDIT, FAILURES = "Audit executions", "Respond to failures"


def seed_project(db: Session) -> UUID:
    return db.scalars(select(ProjectRecord.id).where(ProjectRecord.name == "falcone")).one()


def ready(repo: Any, monkeypatch: Any) -> None:
    """Two open issues with no User Story, two models, and nothing remembered."""
    monkeypatch.setattr(review_settings, "FRESH_SECONDS", 0.0)
    for module in (parent_choice, review_settings):
        monkeypatch.setattr(module, "family_of", lambda provider, model: model)
    data = seed(repo)
    priced(repo, ["analysis", "other"])
    with repo.sessions.begin() as db:
        db.add(ProjectReviewBudget(project_id=data["project"], token_limit=500_000, used=0))
        for number in (41, 42):
            item = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == number)).one()
            item.issue_state = "open"
            item.description = "Show failed runs to the operator and keep an audit entry."


def test_settings_give_runs_their_models_makers_and_rounds(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        shown = client.get(BASE + "review-settings").json()
        assert shown["dollars"]["limit"] == 3.0 and shown["dollars"]["spent"] == 0
        assert not shown["dollars"]["over"] and shown["rounds"] == 0
        assert shown["roles"] == {"answer": None, "judge": None, "third": None}
        fixture = next(entry for entry in shown["makers"] if entry["maker"] == "fixture")
        # The maker of the test account is among the defaults in every test.
        assert fixture["allowed"] and fixture["by_default"] and fixture["models"] == 2

        save = {
            "dollar_limit": 5,
            "answer_provider": "fixture",
            "answer_model": "analysis",
            "judge_provider": "fixture",
            "judge_model": "other",
            "makers": ["fixture"],
            "rounds": 2,
        }
        # The judge is of another family than the model that answers.
        same = post(client, "review-settings", **{**save, "judge_model": "analysis"})
        assert same.status_code == 422 and "another maker" in same.text
        saved = post(client, "review-settings", **save)
        assert saved.status_code == 200, saved.text
        roles = {
            "answer": {"provider": "fixture", "model": "analysis"},
            "judge": {"provider": "fixture", "model": "other"},
            "third": None,
        }
        assert saved.json()["roles"] == roles and saved.json()["rounds"] == 2
        assert post(client, "review-settings", **{**save, "rounds": 0}).status_code == 200
        # Every check offers them as what a run takes.
        assert client.get(BASE + "orphans").json()["roles"] == roles
        assert client.get(BASE + "cleanup").json()["check"]["roles"] == roles

        # No round back: what the judge says otherwise is in doubt at once.
        start = {"provider": "fixture", "model": "analysis", "token_cap": 90_000}
        judge = {"judge_provider": "fixture", "judge_model": "other"}
        assert post(client, "orphans/runs", **start, **judge).status_code == 200
        readers = Readers(
            {
                ("analysis", 41): [AUDIT],
                ("other", 41): [FAILURES],
                ("analysis", 42): [AUDIT],
                ("other", 42): [AUDIT],
                # The third open issue of the project: its answer cannot tell.
                ("analysis", 45): ["unclear"],
            }
        )
        run(repo, readers)
        assert [call[2] for call in readers.calls] == [False] * 5
        counts = client.get(BASE + "orphans").json()["counts"]
        assert (counts["agreed"], counts["doubt"]) == (1, 2)

        # A maker the project does not allow: its models are in no list and no run.
        assert post(client, "review-settings", **{**save, "makers": []}).status_code == 422
        bare = {"dollar_limit": 5, "makers": [], "rounds": 1}
        assert post(client, "review-settings", **bare).status_code == 200
        assert client.get(BASE + "orphans").json()["models"] == []
        refused = post(client, "orphans/runs", **start, again=True)
        assert refused.status_code == 422 and "not sent to fixture" in refused.text
    finally:
        client.close()


def test_a_limit_in_dollars_holds_the_reviews_whatever_the_tokens_say(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        limit = {"dollar_limit": 0.0015, "makers": ["fixture"], "rounds": 1}
        assert post(client, "review-settings", **limit).status_code == 200
        start = {"provider": "fixture", "model": "analysis", "token_cap": 90_000}
        assert post(client, "orphans/runs", **start).status_code == 200
        # Each answer costs a tenth of a cent: the second reaches the limit, and the
        # third issue is not asked about.
        run(repo, Readers({("analysis", 41): [AUDIT], ("analysis", 42): [AUDIT]}))
        held = client.get(BASE + "orphans").json()["run"]
        assert (held["state"], held["done"], held["total"]) == ("held", 2, 3)
        assert "may cost $0.00 a month" in held["detail"] or "a month" in held["detail"]
        shown = client.get(BASE + "review-settings").json()["dollars"]
        assert shown["over"] and shown["spent"] >= 0.0015
        # Nothing new starts, and the run is not resumed, until the limit is raised.
        again = post(client, "orphans/runs", **start, again=True)
        assert again.status_code in (409, 422)
        resumed = post(client, f"orphans/runs/{held['id']}/control", action="resume")
        assert resumed.status_code == 409 and "Raise the limit" in resumed.text
        assert post(client, "review-settings", **{**limit, "dollar_limit": 1}).status_code == 200
        resumed = post(client, f"orphans/runs/{held['id']}/control", action="resume")
        assert resumed.status_code == 200
    finally:
        client.close()


def test_a_run_started_without_a_form_takes_what_the_settings_say(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        # No model is chosen to answer: nothing starts, and the page says where to choose.
        assert client.get(BASE + "orphans").json()["plan"]["answer"] is None
        bare = post(client, "orphans/runs")
        assert bare.status_code == 422 and "Reviews, Settings" in bare.text

        save = {
            "dollar_limit": 5,
            "answer_provider": "fixture",
            "answer_model": "analysis",
            "judge_provider": "fixture",
            "judge_model": "other",
            "makers": ["fixture"],
            "rounds": 0,
            "numbers": {"run_tokens": 50_000},
        }
        assert post(client, "review-settings", **save).status_code == 200
        plan = client.get(BASE + "orphans").json()["plan"]
        assert plan["answer"] == {"provider": "fixture", "model": "analysis"}
        assert plan["judge"] == {"provider": "fixture", "model": "other"}
        assert (plan["first"], plan["tokens"], plan["rounds"]) == (None, 50_000, 0)
        # A check that has no judge takes the model that answers alone.
        pages = client.get(BASE + "epic-pages").json()["write"]["plan"]
        assert pages["answer"] == plan["answer"] and pages["judge"] is None

        assert post(client, "orphans/runs").status_code == 200
        started = client.get(BASE + "orphans").json()["run"]
        assert (started["model"], started["token_cap"]) == ("analysis", 50_000)
        assert started["judge"]["model"] == "other"
        run(
            repo,
            Readers(
                {
                    ("analysis", 41): [AUDIT],
                    ("other", 41): [AUDIT],
                    ("analysis", 42): [AUDIT],
                    ("other", 42): [FAILURES],
                    ("analysis", 45): ["unclear"],
                }
            ),
        )
        counts = client.get(BASE + "orphans").json()["counts"]
        assert (counts["agreed"], counts["doubt"]) == (1, 2)

        # Settings say what it cost, for each workflow and model, and what a run of
        # each check would ask and is likely to cost.
        shown = client.get(BASE + "review-settings").json()
        costs = {(row["workflow"], row["model"]): row for row in shown["costs"]}
        mine = costs["orphan_parent", "analysis"]
        assert (mine["calls"], mine["tokens"], mine["dollars"]) == (3, 120, 0.003)
        assert mine["counted"] and mine["label"] == "Issues with no User Story"
        assert costs["orphan_parent", "other"]["calls"] == 2
        assert shown["dollars"]["spent"] == 0.005
        check = next(row for row in shown["checks"] if row["check"] == "issue_story")
        # Every issue has its answer; one question of each of the two cost a tenth of a cent.
        assert (check["waiting"], check["each"], check["cost"]) == (0, 0.002, 0.0)
        assert check["plan"]["judge"]["model"] == "other"
        other = next(row for row in shown["checks"] if row["check"] == "story_epic")
        assert other["each"] is None and other["cost"] is None
    finally:
        client.close()


def test_the_numbers_of_the_reviews_are_the_projects_own(tmp_path: Path, monkeypatch: Any) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        shown = client.get(BASE + "review-settings").json()
        numbers = {entry["key"]: entry for entry in shown["numbers"]}
        assert numbers["candidates"]["value"] == 12 and numbers["undo_days"]["value"] == 30
        assert numbers["add_error"]["value"] == 5 and numbers["run_tokens"]["value"] == 1_000_000
        assert shown["cheaper_first"] is True

        base = {"dollar_limit": 3, "makers": ["fixture"], "rounds": 0}
        # A number outside what it can be set to, a fraction where a whole number is
        # needed, an unknown number, and a sample whose least is above its most.
        for wrong in (
            {"candidates": 2},
            {"candidates": 4.5},
            {"no_such_number": 1},
            {"sample_least": 30, "sample_most": 20},
        ):
            refused = post(client, "review-settings", **base, numbers=wrong)
            assert refused.status_code == 422, wrong

        own = {"candidates": 3, "undo_days": 7, "epic_margin": 2.5, "sample_least": 1}
        saved = post(client, "review-settings", **base, numbers=own, cheaper_first=False)
        assert saved.status_code == 200, saved.text
        numbers = {entry["key"]: entry["value"] for entry in saved.json()["numbers"]}
        assert {key: numbers[key] for key in own} == own and numbers["sample_most"] == 20
        assert saved.json()["cheaper_first"] is False
        # Saving without numbers leaves them as they are.
        assert post(client, "review-settings", **base, cheaper_first=False).status_code == 200
        with repo.sessions() as db:
            mine = review_settings.of(db, seed_project(db))
            assert mine.count("candidates") == 3 and mine.number("epic_margin") == 2.5
            assert mine.share("add_error") == 0.05 and mine.cheaper_first is False
        # Where they are used: the batches can be undone for as long as the project says.
        assert client.get(BASE + "reorganize/batches").json()["undo_days"] == 7
        # A number put back to the platform's is no longer the project's own.
        back = post(client, "review-settings", **base, numbers={**own, "candidates": 12})
        assert back.status_code == 200
        with repo.sessions() as db:
            row = db.scalars(select(ReviewSettings)).one()
            assert "candidates" not in (row.numbers or {}) and row.numbers["undo_days"] == 7
    finally:
        client.close()


def test_no_review_asks_a_model_of_a_maker_the_project_does_not_allow(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        model = {"provider": "fixture", "model": "analysis"}
        # Allowed: the planning reviews, Discovery and Review evidence offer the model.
        assert model in client.get(BASE + "planning-reviews").json()["models"]
        assert model in client.get(BASE + "evidence").json()["models"]
        assert post(client, "planning-reviews/model", **model).status_code == 200
        with repo.sessions() as db:
            project = seed_project(db)
        assert review_budget.not_sent(repo.sessions, project, ("fixture", "analysis")) is None

        bare = {"dollar_limit": 5, "makers": [], "rounds": 0}
        assert post(client, "review-settings", **bare).status_code == 200
        assert client.get(BASE + "planning-reviews").json()["models"] == []
        assert client.get(BASE + "evidence").json()["models"] == []
        refused = post(client, "planning-reviews/model", **model)
        assert refused.status_code == 422 and "Reviews, Settings" in refused.text

        # And whatever a review was set to before: the call itself is not made.
        class Never:
            async def ask(self, question: Question, *, retry: bool) -> Reply:
                raise AssertionError("A model of a maker that is not allowed was asked.")

        asked = Question(
            choices=(("fixture", "analysis"),),
            system="",
            prompt="{}",
            schema={"type": "object"},
            max_tokens=100,
            seconds=10,
        )
        with pytest.raises(ModelError) as held:
            asyncio.run(
                review_budget.ask_review(
                    repo.sessions, project, cast(Any, Never()), asked, "automatic_review"
                )
            )
        assert held.value.label == "configuration" and "not sent to fixture" in str(held.value)
    finally:
        client.close()


def test_a_third_model_is_asked_about_what_stays_in_doubt_within_its_dollars(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        priced(repo, ["analysis", "other", "strong"])
        save = {
            "dollar_limit": 5,
            "answer_provider": "fixture",
            "answer_model": "analysis",
            "judge_provider": "fixture",
            "judge_model": "other",
            "makers": ["fixture"],
            "rounds": 0,
        }
        third = {"third_provider": "fixture", "third_model": "strong"}
        # It needs a judge, and is another model than the two.
        alone = {**save, "judge_provider": "", "judge_model": ""}
        assert post(client, "review-settings", **alone, **third).status_code == 422
        same = post(
            client, "review-settings", **save, third_provider="fixture", third_model="other"
        )
        assert same.status_code == 422
        saved = post(client, "review-settings", **save, **third)
        assert saved.status_code == 200, saved.text
        assert saved.json()["roles"]["third"] == {"provider": "fixture", "model": "strong"}
        plan = client.get(BASE + "orphans").json()["plan"]
        assert plan["third"] == {"provider": "fixture", "model": "strong"}

        assert post(client, "orphans/runs").status_code == 200
        readers = Readers(
            {
                ("analysis", 41): [AUDIT],
                ("other", 41): [AUDIT],
                # The two differ: the third model is asked, and goes with the judge.
                ("analysis", 42): [AUDIT],
                ("other", 42): [FAILURES],
                ("strong", 42): [FAILURES],
                # An answer that cannot tell: no judge is asked, the third model is,
                # and cannot tell either.
                ("analysis", 45): ["unclear"],
                ("strong", 45): ["unclear"],
            }
        )
        run(repo, readers)
        shown = client.get(BASE + "orphans").json()
        assert (shown["run"]["state"], shown["run"]["done"]) == ("complete", 3)
        assert shown["run"]["third"] == {
            "provider": "fixture",
            "model": "strong",
            "asked": 2,
            "cost": 0.002,
        }
        # It decides nothing: both stay in doubt, each saying with whom the third went.
        assert (shown["counts"]["agreed"], shown["counts"]["doubt"]) == (1, 2)
        listed = client.get(BASE + "orphans", params={"show": "doubt"}).json()["items"]
        sides = {item["number"]: item["review"]["judge"]["third"]["sides"] for item in listed}
        assert sides == {42: "judge", 45: "answer"}
        assert all(item["review"]["judge"]["third"]["model"] == "strong" for item in listed)

        # Its dollars are its own: with none left it is not asked, and nothing else changes.
        numbers = {"doubt_dollars": 0}
        assert post(client, "review-settings", **save, **third, numbers=numbers).status_code == 200
        assert post(client, "orphans/runs", again=True).status_code == 200
        again = Readers(
            {
                ("analysis", 41): [AUDIT],
                ("other", 41): [AUDIT],
                ("analysis", 42): [AUDIT],
                ("other", 42): [FAILURES],
                ("analysis", 45): ["unclear"],
            }
        )
        run(repo, again)
        shown = client.get(BASE + "orphans").json()
        assert shown["run"]["third"]["asked"] == 0
        assert (shown["counts"]["agreed"], shown["counts"]["doubt"]) == (1, 2)
        assert not [call for call in again.calls if call[0] == "strong"]
    finally:
        client.close()
