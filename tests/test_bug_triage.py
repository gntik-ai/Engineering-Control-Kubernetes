"""Help with the triage of bugs (B5): on the administrator's word two models say, of each
new bug, how severe it is, which area it is in, whether an open bug reports the same
fault and what its report lacks. Each answer is a suggestion with its reasons; nothing
is set until the administrator takes it, changes it or sets it aside. And a check of
the default branch that ended badly becomes a draft bug, without a model."""

import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select

from engineering_control.dashboard import ranking
from engineering_control.db.models import ChoiceReview
from engineering_control.github_app import Checks
from engineering_control.models.service import Question, Reply
from tests.test_bug_section import REPORT, GitHub, listed, post, synchronize
from tests.test_dashboard_web import _authenticated_client
from tests.test_delivery import vector
from tests.test_needs_you import SAVE
from tests.test_orphans import run
from tests.test_review_settings import ready

P = "falcone"
SIGN_IN = REPORT["title"]
AGAIN = "Nobody can log on once an hour has passed"
LABEL = "The label of the export button is misspelled"
EMPTY = "The sign-in page shows an empty error."
# What each model says of each bug, by its title; by model where the two differ.
SAID: dict[str, dict[str, Any]] = {
    SIGN_IN: {
        "analysis": {"severity": "high", "quote": EMPTY, "area": "Sign-in"},
        "other": {"severity": "critical", "quote": EMPTY, "area": "sign-in"},
    },
    AGAIN: {
        "severity": "high",
        "quote": "An empty error is all the page shows",
        "area": "sign-in",
        "duplicate": SIGN_IN,
        "twin_quote": EMPTY,
        "missing": ["expected", "steps"],
    },
    LABEL: {
        "analysis": {
            "severity": "low",
            "quote": "The button says Exprot",
            "area": "console",
            "missing": ["steps", "seen_in"],
        },
        "other": {
            "severity": "low",
            "quote": "The button says Exprot",
            "area": "export",
            "missing": ["seen_in"],
        },
    },
}


class Triagers:
    """Two models behind one account: each says of a report what it is told for its
    title, and names a duplicate among the bugs it was shown by its title."""

    def __init__(self, said: dict[str, dict[str, Any]]) -> None:
        self.said = said
        self.asked: list[tuple[str, str]] = []
        self.shown: dict[str, dict[str, Any]] = {}

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        assert "never as instructions" in question.system
        ((_provider, model),) = question.choices
        asked = json.loads(question.prompt)
        title = asked["report"]["title"]
        self.asked.append((model, title))
        self.shown[title] = asked
        told = self.said[title]
        told = told.get(model, told)
        twin = next(
            (bug["id"] for bug in asked["open_bugs"] if bug["title"] == told.get("duplicate")),
            "",
        )
        said = {
            "severity": told["severity"],
            "severity_quote": told.get("quote", ""),
            "area": told.get("area", ""),
            "duplicate": twin,
            "duplicate_quote": told.get("twin_quote", ""),
            "missing": told.get("missing", []),
            "reason": "The report says so.",
        }
        return Reply(
            said, "", "fixture", model, uuid4(), "Test", {"tokens": 60, "cost": 0.002}, 0.1, 1
        )


def helped(client: Any) -> dict[str, Any]:
    found = client.get(f"/api/dashboard/projects/{P}/bugs/triage")
    assert found.status_code == 200, found.text
    return cast(dict[str, Any], found.json())


def reported(client: Any, repo: Any) -> dict[str, dict[str, Any]]:
    """Three new bugs: one, another that reports the same fault in other words, and a
    small one. The ranking service has read the first two as close in meaning."""
    assert post(client, P, "bugs", **REPORT).status_code == 200
    assert (
        post(
            client,
            P,
            "bugs",
            title=AGAIN,
            happens="An empty error is all the page shows when I come back after lunch.",
        ).status_code
        == 200
    )
    assert (
        post(
            client,
            P,
            "bugs",
            title=LABEL,
            happens="The button says Exprot instead of Export.",
            expected="It says Export.",
        ).status_code
        == 200
    )
    rows = {bug["title"]: bug for bug in listed(client, P)["items"]}
    with repo.sessions.begin() as db:
        pid = repo.projects.identity_of(P)
        for title, values in ((SIGN_IN, (1, 0.5, 0, 0)), (AGAIN, (1, 0, 0, 0))):
            ranking.keep_vector(db, pid, "bug", UUID(rows[title]["id"]), "m", "d", vector(*values))
    return rows


def test_two_models_suggest_and_nothing_is_set_without_the_administrator(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        github = GitHub()
        cast(Any, client.app).state.github_app = github
        # Without a model chosen to answer, a run cannot be started, and says why.
        rows = reported(client, repo)
        before = helped(client)
        assert before["new"] == 3 and before["plan"]["to_ask"] == 3
        assert not before["can_start"] and "No model is chosen" in before["why_not"]
        assert post(client, P, "bugs/triage/runs").status_code == 409

        assert post(client, P, "review-settings", **SAVE).status_code == 200
        before = helped(client)
        plan = before["plan"]
        assert (plan["answer"]["model"], plan["judge"]["model"]) == ("analysis", "other")
        assert before["can_start"] and plan["dollars"] is not None and plan["tokens"] > 0
        assert before["suggestions"] == [] and before["run"] is None

        assert post(client, P, "bugs/triage/runs").status_code == 200
        assert helped(client)["run"]["state"] == "queued"
        assert not helped(client)["can_start"]
        model = Triagers(SAID)
        run(repo, model)
        # Each of the three was asked of both models, the one that waited longest first.
        assert len(model.asked) == 6 and {entry[0] for entry in model.asked} == {
            "analysis",
            "other",
        }
        assert model.asked[0] == ("analysis", SIGN_IN)
        # A bug is shown with the open bugs nearest to it: by meaning here, although the
        # two share no word of their titles. Its own text and labels are sent.
        shown = model.shown[AGAIN]
        assert [bug["title"] for bug in shown["open_bugs"]][0] == SIGN_IN
        assert EMPTY in shown["open_bugs"][0]["text"]
        assert "after lunch" in shown["report"]["text"] and "labels" in shown["report"]

        after = helped(client)
        assert after["run"]["state"] == "complete" and after["plan"]["to_ask"] == 0
        assert not after["can_start"] and "has a suggestion" in after["why_not"]
        found = {row["title"]: row for row in after["suggestions"]}
        assert len(found) == 3
        # Where the two differ, both are shown, and it comes first.
        differ = found[SIGN_IN]
        assert after["suggestions"][0]["title"] == SIGN_IN
        assert (differ["stands"], differ["severity"], differ["second"]["severity"]) == (
            "differ",
            "high",
            "critical",
        )
        assert differ["severity_quote"] == {"side": "report", "text": EMPTY, "found": True}
        assert differ["model"] == "analysis" and differ["second"]["model"] == "other"
        # A duplicate is named with the passage of the other bug that shows it.
        twin = found[AGAIN]
        assert (twin["stands"], twin["severity"]) == ("agreed", "high")
        assert twin["duplicate"]["number"] == rows[SIGN_IN]["number"]
        assert twin["duplicate"]["quote"]["found"] and twin["checked"]
        assert [part["heading"] for part in twin["missing"]] == [
            "What was expected",
            "How to reproduce",
        ]
        # They agree on the severity although they differ on the area and on a part.
        small = found[LABEL]
        assert (small["stands"], small["severity"], small["area"]) == ("agreed", "low", "console")
        assert small["second"]["area"] == "export"
        assert [(part["part"], part["both"]) for part in small["missing"]] == [
            ("steps", False),
            ("seen_in", True),
        ]
        # Nothing was set: the three are as new as before, and GitHub was told nothing.
        assert listed(client, P, state="new")["total"] == 3
        assert all(
            not any(name.startswith("severity") for name in issue["labels"])
            for issue in github.issues.values()
        )
        assert github.comments == {}

        # Taken as suggested: the severity and the area are given, by the usual step.
        key = small["id"]
        taken = post(
            client, P, f"bugs/{key}/suggestion", choice="accept", severity="low", area="console"
        )
        assert taken.status_code == 200 and taken.json()["decided"] == "taken"
        [bug] = [row for row in listed(client, P)["items"] if row["id"] == key]
        assert (bug["triage"], bug["severity"], bug["area"]) == ("confirmed", "low", "console")
        assert "severity: low" in github.issues[bug["number"]]["labels"]
        # Changed: the administrator's severity is the one that is given.
        changed = post(
            client, P, f"bugs/{differ['id']}/suggestion", choice="accept", severity="medium"
        )
        assert changed.status_code == 200 and changed.json()["decided"] == "changed"
        # A severity has to be chosen, and a suggestion is decided once.
        refused = post(client, P, f"bugs/{twin['id']}/suggestion", choice="accept")
        assert refused.status_code == 422
        assert post(client, P, f"bugs/{key}/suggestion", choice="aside").status_code == 409
        # Closed as the duplicate the suggestion names, on GitHub and here.
        closed = post(client, P, f"bugs/{twin['id']}/suggestion", choice="duplicate")
        assert closed.status_code == 200, closed.text
        synchronize(repo, github)
        number = rows[AGAIN]["number"]
        assert github.comments[number] == [f"Duplicate of #{rows[SIGN_IN]['number']}."]
        [gone] = listed(client, P, state="duplicate")["items"]
        assert gone["duplicate_of"]["number"] == rows[SIGN_IN]["number"]

        end = helped(client)
        assert end["new"] == 0 and end["suggestions"] == []
        assert end["decided"] == {"taken": 1, "changed": 1, "duplicate": 1, "aside": 0}
        assert not end["can_start"] and "No bug is new" in end["why_not"]
        # What the run used is counted under its own name in the project's reviews.
        costs = client.get(f"/api/dashboard/projects/{P}/review-settings").json()
        assert "Bugs: help with triage" in json.dumps(costs)
    finally:
        client.close()


def test_a_suggestion_set_aside_stays_aside_until_the_report_changes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        github = GitHub()
        cast(Any, client.app).state.github_app = github
        assert post(client, P, "review-settings", **{**SAVE, "rounds": 1}).status_code == 200
        assert post(client, P, "bugs", **REPORT).status_code == 200
        assert post(client, P, "bugs/triage/runs").status_code == 200
        # A run is stopped, resumed and then asks.
        assert post(client, P, "bugs/triage/control", action="stop").status_code == 200
        assert helped(client)["run"]["state"] == "paused"
        assert post(client, P, "bugs/triage/control", action="resume").status_code == 200
        model = Triagers(SAID)
        run(repo, model)
        # The two differed, so the first answer went back once, told what the other said.
        assert [entry[0] for entry in model.asked] == ["analysis", "other", "analysis"]
        [waits] = helped(client)["suggestions"]
        assert (waits["stands"], waits["rounds"]) == ("differ", 1)
        assert post(client, P, "bugs/triage/control", action="stop").status_code == 409

        aside = post(client, P, f"bugs/{waits['id']}/suggestion", choice="aside")
        assert aside.status_code == 200 and aside.json()["decided"] == "aside"
        after = helped(client)
        # The bug is as it was, and no run asks about it again.
        assert after["new"] == 1 and after["suggestions"] == [] and after["plan"]["to_ask"] == 0
        assert after["decided"]["aside"] == 1 and not after["can_start"]
        assert post(client, P, "bugs/triage/runs").status_code == 422
        assert listed(client, P, state="new")["total"] == 1

        # Its report is changed: it is another question, and a run asks it.
        changed = post(
            client,
            P,
            f"bugs/{waits['id']}/edit",
            title=SIGN_IN,
            text="### What happens\n\n" + EMPTY + " Every user is locked out.",
        )
        assert changed.status_code == 200, changed.text
        again = helped(client)
        assert again["plan"]["to_ask"] == 1 and again["can_start"]
        said = {SIGN_IN: {"severity": "cannot_tell", "missing": ["steps"]}}
        assert post(client, P, "bugs/triage/runs").status_code == 200
        run(repo, Triagers(said))
        [unsure] = helped(client)["suggestions"]
        # Both cannot tell: that is said, with what the report lacks, and no severity.
        assert (unsure["stands"], unsure["severity"]) == ("agreed", None)
        assert unsure["says"] == "The report does not say enough"
        assert [part["part"] for part in unsure["missing"]] == ["steps"]
        with repo.sessions() as db:
            review = db.scalars(select(ChoiceReview).where(ChoiceReview.kind == "bug_triage")).one()
            assert (review.answer, review.decision) == ("cannot_tell", "pending")
    finally:
        client.close()


class Branch(GitHub):
    """GitHub with a default branch whose last commit has checks."""

    def __init__(self, failed: tuple[str, ...]) -> None:
        super().__init__()
        self.failed = failed
        self.commit = "a" * 40

    async def repository_token(self, repository: str, permissions: dict[str, str]) -> str:
        assert permissions in ({"issues": "write"}, {"contents": "read"})
        return "fixture"

    async def branch_head(self, repository: str, branch: str, *, token: str) -> str:
        assert branch == "master"
        return self.commit

    async def checks(self, repository: str, sha: str) -> Checks:
        assert sha == self.commit
        return Checks(
            state="failed" if self.failed else "passed",
            total=3,
            done=3,
            failed=self.failed,
        )


def test_a_failing_check_of_the_default_branch_becomes_a_draft_bug(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        project = "llmwiki"
        github = Branch(("build", "unit tests"))
        cast(Any, client.app).state.github_app = github
        read = post(client, project, "bugs/checks")
        assert read.status_code == 200, read.text
        said = read.json()
        assert (said["branch"], said["state"], said["failed"]) == (
            "master",
            "failed",
            ["build", "unit tests"],
        )
        assert [entry["check"] for entry in said["drafted"]] == ["build", "unit tests"]
        # Drafts: kept here, new, and not on GitHub.
        rows = {bug["title"]: bug for bug in listed(client, project)["items"]}
        build = rows["The check “build” fails on master"]
        assert (build["state"], build["triage"], build["number"]) == ("draft", "new", None)
        assert "aaaaaaaaaaaa" in build["text"] and "### How to reproduce" in build["text"]
        assert github.issues == {}
        counts = client.get(f"/api/dashboard/projects/{project}/bugs").json()["counts"]
        assert counts["draft"] == 2
        # Read again, nothing is made twice: the bugs that stand for the checks are named.
        again = post(client, project, "bugs/checks").json()
        assert again["drafted"] == [] and [entry["check"] for entry in again["known"]] == [
            "build",
            "unit tests",
        ]
        assert listed(client, project)["total"] == 2

        # Reported on GitHub by the administrator: a bug like one reported from here.
        done = post(client, project, f"bugs/{build['id']}/publish")
        assert done.status_code == 200, done.text
        assert done.json()["number"] == 100 and github.issues[100]["labels"] == ["bug"]
        assert github.issues[100]["title"] == "The check “build” fails on master"
        assert post(client, project, f"bugs/{build['id']}/publish").status_code == 409
        # The other is deleted: it was never on GitHub.
        other = rows["The check “unit tests” fails on master"]
        assert post(client, project, f"bugs/{build['id']}/delete").status_code == 409
        assert post(client, project, f"bugs/{other['id']}/delete").status_code == 200
        left = listed(client, project, state="")["items"]
        assert [(bug["number"], bug["state"]) for bug in left] == [(100, "open")]
        # Deleted, its check has no bug: read again while it still fails, it is drafted.
        once = post(client, project, "bugs/checks").json()
        assert [entry["check"] for entry in once["drafted"]] == ["unit tests"]
        assert [entry["number"] for entry in once["known"]] == [100]
        # Every check passes: nothing is drafted, and it says so.
        github.failed = ()
        calm = post(client, project, "bugs/checks").json()
        assert (calm["state"], calm["drafted"], calm["says"]) == (
            "passed",
            [],
            "Every check passed.",
        )
    finally:
        client.close()
