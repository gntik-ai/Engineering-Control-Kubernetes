"""What the scorer would do reaches the administrator as batches: a sample is judged
first, approval applies the rest, and an approved batch can be undone as a whole."""

from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from engineering_control.dashboard.planning_health import measure
from engineering_control.dashboard.reorg import sample_size, why
from engineering_control.dashboard.workspace_issues import add_story_links
from engineering_control.db.models import (
    BacklogItem,
    PlanningLink,
    ProjectIssueStory,
    ReorgLine,
    ScorerDecision,
)
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_primary_epics import more_epics
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"
LOW = {"words_place": 0.05, "meaning_place": 0.1, "words": 0.1}
HIGH = {"words": 1.0, "words_place": 1.0, "meaning_gap": 0.0, "meaning_place": 1.0}


def issue(repo: Any, number: int) -> UUID:
    with repo.sessions() as db:
        return db.scalars(
            select(BacklogItem.id).where(
                BacklogItem.issue_number == number, BacklogItem.repository != "other/elsewhere"
            )
        ).one()


def decide(repo: Any, pid: UUID, kind: str, outcome: str, child: UUID, parent: UUID) -> None:
    """What the scorer is taken to have said of one record."""
    with repo.sessions.begin() as db:
        db.add(
            ScorerDecision(
                project_id=pid,
                kind=kind,
                child_id=child,
                parent_id=parent,
                outcome=outcome,
                confidence=0.2 if outcome == "unsupported" else 0.9,
                model="light",
                detail={"signals": LOW if outcome == "unsupported" else HIGH, "of": 4},
            )
        )


def ready(client: Any) -> dict[str, int]:
    found = client.get(BASE + "reorganize/batches")
    assert found.status_code == 200, found.text
    return {kind["kind"]: kind["ready"] for kind in found.json()["kinds"]}


def prepared(client: Any, kind: str) -> dict[str, Any]:
    made = post(client, "reorganize/batches", kind=kind)
    assert made.status_code == 200, made.text
    lines = client.get(BASE + "reorganize/batches/" + made.json()["id"]).json()
    return {"id": made.json()["id"], **lines}


def links_of(repo: Any, child: UUID) -> list[PlanningLink]:
    with repo.sessions() as db:
        return list(
            db.scalars(
                select(PlanningLink).where(
                    PlanningLink.child_id == child, PlanningLink.removed_at.is_(None)
                )
            )
        )


def test_a_sample_grows_with_the_batch_and_a_line_says_why() -> None:
    assert [sample_size(n) for n in (1, 4, 5, 30, 100, 900)] == [1, 4, 5, 6, 10, 20]
    said = why("place", 0.87, {**HIGH, "reranker_first": 1.0}, 12)
    assert "alone among 12 candidates" in said and "87%" in said
    assert "shares more words" in said and "closest in meaning" in said and "reranker" in said
    doubt = why("unsupported", 0.02, LOW, 30)
    assert "place 20 by shared words" in doubt and "place 10 by meaning" in doubt


def test_an_issue_is_placed_by_an_approved_batch_and_the_batch_can_be_undone(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        pid = data["project"]
        orphan, other = issue(repo, 42), issue(repo, 45)
        story = data["Respond to failures"]
        decide(repo, pid, "issue_story", "place", orphan, story)
        decide(repo, pid, "issue_story", "place", other, data["Audit executions"])
        assert ready(client)["place"] == 2
        # A bug is never in a batch: it has no User Story.
        with repo.sessions.begin() as db:
            db.get_one(BacklogItem, other).title = "bug: dependencies are stale"
        assert ready(client) == {
            "reasons": 0,
            "primary": 0,
            "place": 1,
            "unsupported": 0,
            "close": 0,
        }

        batch = prepared(client, "place")
        assert batch["batch"]["state"] == "sample" and batch["total"] == 1
        [line] = batch["items"]
        assert line["sampled"] and line["child"]["title"].startswith("#42")
        assert line["parent"]["title"] == "Respond to failures" and "candidates" in line["reason"]
        # Preparing changed nothing, a second batch of the kind cannot be opened, and
        # the batch is not approved before its sample is judged.
        assert not links_of(repo, orphan)
        assert post(client, "reorganize/batches", kind="place").status_code == 409
        path = "reorganize/batches/" + batch["id"]
        assert post(client, path + "/approve").status_code == 409
        assert post(client, f"{path}/lines/{line['id']}", verdict="right").status_code == 200
        done = post(client, path + "/approve")
        assert done.status_code == 200, done.text
        assert (done.json()["state"], done.json()["applied"]) == ("approved", 1)

        [link] = links_of(repo, orphan)
        assert (link.parent_id, link.origin, link.confirmed) == (story, "scorer", True)
        assert link.reason and "candidates" in link.reason
        overview = client.get(BASE + "reorganize/batches").json()
        assert overview["past"][0]["can_undo"] and ready(client)["place"] == 0

        undone = post(client, path + "/undo")
        assert undone.status_code == 200, undone.text
        assert undone.json()["state"] == "undone"
        assert not links_of(repo, orphan)
        with repo.sessions() as db:
            assert not db.scalars(
                select(ProjectIssueStory).where(ProjectIssueStory.item_id == orphan)
            ).all()
            [kept] = db.scalars(select(ReorgLine)).all()
            assert kept.state == "undone" and kept.verdict == "right"
        assert post(client, path + "/undo").status_code == 409
    finally:
        client.close()


def test_a_wrong_line_in_the_sample_withdraws_the_batch(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        epics = more_epics(repo, data)
        pid = data["project"]
        audit, respond = data["Audit executions"], data["Respond to failures"]
        decide(repo, pid, "story_epic", "primary", audit, epics["Billing"])
        decide(repo, pid, "story_epic", "primary", respond, epics["Audit trail"])
        assert ready(client)["primary"] == 2
        batch = prepared(client, "primary")
        assert batch["batch"]["sample"] == 2 and batch["batch"]["allowed_wrong"] == 0
        wrong = next(line for line in batch["items"] if line["parent"]["title"] == "Billing")
        path = "reorganize/batches/" + batch["id"]
        judged = post(client, f"{path}/lines/{wrong['id']}", verdict="wrong")
        assert judged.status_code == 200 and judged.json()["state"] == "withdrawn"
        assert post(client, path + "/approve").status_code == 409
        # Nothing was chosen, and the line judged wrong is not offered again.
        assert not any(link.primary_by == "administrator" for link in links_of(repo, audit))
        assert ready(client)["primary"] == 1

        again = prepared(client, "primary")
        [line] = again["items"]
        path = "reorganize/batches/" + again["id"]
        before = {link.parent_id: link.primary_by for link in links_of(repo, respond)}
        assert post(client, f"{path}/lines/{line['id']}", verdict="right").status_code == 200
        assert post(client, path + "/approve").json()["applied"] == 1
        chosen = {link.parent_id: link for link in links_of(repo, respond)}
        assert chosen[epics["Audit trail"]].is_primary
        assert chosen[epics["Audit trail"]].primary_by == "administrator"
        assert "share of belief" in (chosen[epics["Audit trail"]].reason or "")
        assert post(client, path + "/undo").status_code == 200
        assert {link.parent_id: link.primary_by for link in links_of(repo, respond)} == before
    finally:
        client.close()


def test_reasons_are_recorded_and_unsupported_links_taken_off(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        pid = data["project"]
        good, doubtful = issue(repo, 43), issue(repo, 41)
        retired, respond = data["Retired delivery history"], data["Respond to failures"]
        with repo.sessions.begin() as db:
            db.add(ProjectIssueStory(project_id=pid, item_id=good, story_id=retired))
            db.add(ProjectIssueStory(project_id=pid, item_id=doubtful, story_id=respond))
        repo.planning_links.reconcile_project("falcone")
        decide(repo, pid, "issue_story", "confirmed", good, retired)
        decide(repo, pid, "issue_story", "unsupported", doubtful, respond)
        # The administrator's own link is never called unsupported, whatever is said of it.
        decide(repo, pid, "issue_story", "unsupported", issue(repo, 44), respond)
        assert ready(client) == {
            "reasons": 1,
            "primary": 0,
            "place": 0,
            "unsupported": 1,
            "close": 0,
        }
        without = measure(repo.sessions, "falcone")["links_without_reason"]

        reasons = prepared(client, "reasons")
        path = "reorganize/batches/" + reasons["id"]
        line = reasons["items"][0]["id"]
        assert post(client, f"{path}/lines/{line}", verdict="right").status_code == 200
        assert post(client, path + "/approve").json()["applied"] == 1
        [link] = links_of(repo, good)
        origin = link.origin
        assert link.reason and link.confirmed and origin != "scorer"
        assert ready(client)["reasons"] == 0
        assert measure(repo.sessions, "falcone")["links_without_reason"] == without - 1
        assert post(client, path + "/undo").status_code == 200
        [link] = links_of(repo, good)
        assert link.reason is None and link.origin == origin

        gone = prepared(client, "unsupported")
        path = "reorganize/batches/" + gone["id"]
        assert gone["batch"]["measured"]["error"] == 0.02
        line = gone["items"][0]["id"]
        assert post(client, f"{path}/lines/{line}", verdict="right").status_code == 200
        assert post(client, path + "/approve").json()["applied"] == 1
        assert not links_of(repo, doubtful)
        with repo.sessions.begin() as db:
            # The link's record is closed, not lost, and the platform does not make it again.
            closed = db.scalars(select(PlanningLink).where(PlanningLink.child_id == doubtful)).one()
            assert closed.removed_at is not None
            assert add_story_links(db, pid, doubtful, {respond}) == 0
        assert post(client, path + "/undo").status_code == 200
        [back] = links_of(repo, doubtful)
        assert back.parent_id == respond and back.origin == closed.origin
        with repo.sessions.begin() as db:
            assert add_story_links(db, pid, doubtful, {retired}) == 1
    finally:
        client.close()
