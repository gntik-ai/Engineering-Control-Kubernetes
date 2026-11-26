"""One list of what waits for the administrator across Reviews: each row says how many,
what, and in which view it is decided; a project's next steps are its rows."""

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from engineering_control.dashboard import needs_you
from engineering_control.db.models import BacklogItem, ChoiceRun, PlanningReview, ProjectRecord
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_judging import Readers
from tests.test_needs_you import SAVE, candidates
from tests.test_orphans import run
from tests.test_review_settings import BASE, ready, seed_project


def listed(client: Any, project: str = "falcone") -> dict[str, Any]:
    found = client.get(f"/api/dashboard/projects/{project}/needs-you")
    assert found.status_code == 200, found.text
    body: dict[str, Any] = found.json()
    return body


def rows(client: Any, project: str = "falcone") -> dict[str, dict[str, Any]]:
    return {row["key"]: row for row in listed(client, project)["rows"]}


def test_what_waits_across_reviews_is_one_list(tmp_path: Path, monkeypatch: Any) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        assert post(client, "review-settings", **SAVE).status_code == 200
        # Nothing was asked yet: no decision waits. The issues nobody was asked about
        # are to be started by hand, since the check does not run by itself.
        before = rows(client)
        assert not [row for row in before.values() if row["group"] == "decide"]
        unasked = before["unasked:issue_story"]
        assert (unasked["group"], unasked["count"]) == ("start", 3)
        assert (unasked["view"], unasked["anchor"]) == ("related-work", "issues-without-story")
        assert before["ranking"]["group"] == "held"

        shown = candidates(client)
        first, other = shown[41][0], shown[41][1]
        assert post(client, "orphans/runs").status_code == 200
        run(
            repo,
            Readers(
                {
                    # Settled: both choose the Story the texts put first.
                    ("analysis", 41): [first],
                    ("other", 41): [first],
                    # They agree and the texts do not; and one that cannot tell.
                    ("analysis", 42): [other],
                    ("other", 42): [other],
                    ("analysis", 45): ["unclear"],
                }
            ),
        )
        found = listed(client)
        after = {row["key"]: row for row in found["rows"]}
        # The answers that need the administrator are one row, with why; its number is
        # the one the worklist opens on.
        doubt = after["doubt:issue_story"]
        assert (doubt["group"], doubt["count"]) == ("decide", 2)
        assert doubt["count"] == client.get(BASE + "orphans").json()["counts"]["needs"]
        assert "the answer could not tell" in doubt["detail"]
        assert "the texts alone point to another" in doubt["detail"]
        # What two models and the texts agree on waits as a change to approve.
        changes = after["changes"]
        assert changes["count"] == 1 and (changes["view"], changes["anchor"]) == (
            "reorganize",
            "changes",
        )
        assert "Place issues under a User Story: 1" in changes["detail"]
        # Every issue has an answer now: none is left to start.
        assert "unasked:issue_story" not in after
        # What is to decide comes first, then what is held, then what is to start.
        groups = [row["group"] for row in found["rows"]]
        assert groups == sorted(groups, key=needs_you.GROUPS.index)
        assert found["counts"]["decide"] == 2

        # A project's next steps, wherever they are shown, are these rows.
        steps = client.get(BASE + "rollout").json()["steps"]
        assert [step["says"] for step in steps] == [row["says"] for row in found["rows"]]
        assert steps[0]["anchor"] == found["rows"][0]["anchor"]

        # A batch that is open is counted with its lines, and says how far its sample is.
        opened = post(client, "reorganize/batches", kind="place")
        assert opened.status_code == 200, opened.text
        changes = rows(client)["changes"]
        assert (
            changes["count"] == 1
            and "a batch is open; 0 of 1 sampled lines judged" in (changes["detail"])
        )
    finally:
        client.close()


def test_what_stopped_and_what_asks_is_in_the_list(tmp_path: Path, monkeypatch: Any) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        assert post(client, "review-settings", **SAVE).status_code == 200
        assert post(client, "orphans/runs").status_code == 200
        with repo.sessions.begin() as db:
            pid = seed_project(db)
            held = db.scalars(select(ChoiceRun).where(ChoiceRun.project_id == pid)).one()
            held.state, held.detail = "held", "The project's review tokens are used up."
            held.total, held.done = 3, 1
            # A planning review that asks a question, one that failed, and one that
            # waits while the automatic reviews are stopped.
            for number, status in enumerate(("needs_input", "failed", "queued")):
                db.add(
                    PlanningReview(
                        project_id=pid,
                        kind="stories",
                        record_id=uuid4(),
                        title=f"Story {number}",
                        scope="record",
                        status=status,
                    )
                )
            db.get(ProjectRecord, pid).planning_reviews_paused_at = datetime.now(UTC)
        found = rows(client)
        stopped = found["run:issue_story"]
        assert (stopped["group"], stopped["count"]) == ("held", 2)
        assert "on hold at 1 of 3" in stopped["says"]
        assert stopped["detail"] == "The project's review tokens are used up."
        # A run that waits is not also listed as something to start.
        assert "unasked:issue_story" not in found
        asking = found["planning:asking"]
        assert (asking["group"], asking["count"], asking["anchor"]) == (
            "decide",
            1,
            "planning-reviews",
        )
        assert found["planning:failed"]["group"] == "held"
        queued = found["planning:queued"]
        assert queued["group"] == "held" and "stopped" in queued["detail"]

        # A check set to run by itself is not something to start by hand.
        assert "unasked:epic_overview" in found
        saved = post(client, "review-settings", **SAVE, runs={"epic_overview": "arrive"})
        assert saved.status_code == 200, saved.text
        assert "unasked:epic_overview" not in rows(client)

        # A bug stands apart from the plan: one that still carries a link is listed.
        assert "bugs" not in found
        with repo.sessions.begin() as db:
            linked = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 44)).one()
            linked.title = "bug: the ledger loses entries"
        bugs = rows(client)["bugs"]
        assert (bugs["group"], bugs["count"], bugs["view"]) == ("decide", 1, "bugs")

        # A project that was never through Discovery has one row, and it says so.
        (only,) = listed(client, "llmwiki")["rows"]
        assert (only["key"], only["view"]) == ("discovery", "discovery")
        assert client.get("/api/dashboard/projects/unknown/needs-you").status_code == 404
    finally:
        client.close()


def test_doubt_that_decides_nothing_is_listed_apart_and_is_no_step(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        # Two answers the models differ on, of every check.
        monkeypatch.setattr(
            needs_you.parent_choice, "standings", lambda db, project, kind: Counter(differ=2)
        )
        found = rows(client)
        # An issue with no User Story and a Story with no primary Epic wait for a word.
        for kind in ("issue_story", "story_epic"):
            assert found["doubt:" + kind]["group"] == "decide"
        # Elsewhere the record is left as it is: nothing waits, and the row says so.
        links = found["doubt:epic_links"]
        assert (links["group"], links["count"]) == ("doubt", 2)
        assert "did not say the same of every link" in links["says"]
        assert "stays" in links["detail"]
        needed = found["doubt:story_needed"]
        assert needed["group"] == "doubt" and needed["says"] == (
            "2 User Stories that nothing supports: the two models did not agree whether "
            "they are still needed."
        )
        assert found["doubt:epic_overview"]["group"] == "doubt"
        assert list(found)[-1].startswith("doubt:")
        said = [step["says"] for step in client.get(BASE + "rollout").json()["steps"]]
        assert links["says"] not in said and found["doubt:story_epic"]["says"] in said
    finally:
        client.close()
