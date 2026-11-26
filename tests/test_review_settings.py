"""A project's reviews are set in one place: a limit in dollars a month, the models that
answer and judge, the makers its texts may be sent to, and how often an answer goes back."""

from pathlib import Path
from typing import Any

from sqlalchemy import select

from engineering_control.dashboard import parent_choice, review_settings
from engineering_control.db.models import BacklogItem, ProjectReviewBudget
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_judging import Readers, priced
from tests.test_orphans import run
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"
AUDIT, FAILURES = "Audit executions", "Respond to failures"


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
        assert shown["roles"] == {"answer": None, "judge": None}
        fixture = next(entry for entry in shown["makers"] if entry["maker"] == "fixture")
        # The project allows the maker of its test account, which is no default.
        assert fixture["allowed"] and not fixture["by_default"] and fixture["models"] == 2

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
