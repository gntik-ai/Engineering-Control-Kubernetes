"""How well a parent is found is measured against what is known to be true: links the
text states, the administrator's choices, and pairs the administrator judges once."""

from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from engineering_control.dashboard.signals import measure
from engineering_control.db.models import GoldPair, PlanningLink
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_primary_epics import more_epics
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"


def kinds(client: Any) -> dict[str, dict[str, Any]]:
    report = client.get(BASE + "signals")
    assert report.status_code == 200, report.text
    return {row["kind"]: row for row in report.json()["kinds"]}


def test_a_ranking_is_measured_against_the_known_parents() -> None:
    a, b, c, d = (UUID(int=n) for n in range(1, 5))
    ranks = {a: [b, c, d], b: [c, d, a], c: [a]}
    right = {a: {b}, b: {a}, c: {b}, d: {a}}
    # d has no ranking: it is not counted. c's right parent is never ranked.
    assert measure(ranks, right) == {
        "children": 3,
        "top1": 0.333,
        "mrr": round((1 + 1 / 3 + 0) / 3, 3),
        "recall5": 0.667,
        "recall20": 0.667,
    }
    assert measure({}, right) == {"children": 0}


def test_pairs_are_drawn_judged_and_become_known_truth(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        more_epics(repo, data)
        found = kinds(client)
        # One link is the administrator's own: issue 44 under "Respond to failures".
        assert found["issue_story"]["sources"] == {"declared": 0, "administrator": 1, "judged": 0}
        words = found["issue_story"]["methods"][0]
        assert words["key"] == "words" and words["children"] == 1
        assert found["story_epic"]["known"] == 0

        drawn = post(client, "labels/draw")
        assert drawn.status_code == 200, drawn.text
        assert drawn.json()["added"] == drawn.json()["drawn"] > 0
        assert post(client, "labels/draw").json()["added"] == 0
        with repo.sessions() as db:
            pairs = db.scalars(select(GoldPair)).all()
            by_bucket = {pair.bucket: pair for pair in pairs}
            # A likely Story for an issue that has none, and Epics of Stories under several.
            assert "unlinked_best" in by_bucket
            assert {"primary_clear", "primary_open", "related_weak"} & set(by_bucket)
            # What is already known is not asked again.
            known = db.scalars(
                select(PlanningLink.child_id).where(PlanningLink.origin == "administrator")
            ).all()
            assert not {pair.child_id for pair in pairs} & set(known)
            # No record is asked about twice.
            assert len({(pair.kind, pair.child_id) for pair in pairs}) == len(pairs)

        # Every pair is shown once, with both texts; "cannot tell" teaches nothing.
        seen: dict[str, dict[str, Any]] = {}
        for count in range(1, len(pairs) + 1):
            pair = client.get(BASE + "labels/next").json()["pair"]
            seen[pair["kind"]] = pair
            assert post(client, f"labels/{pair['id']}", label="unsure").json()["judged"] == count
        assert client.get(BASE + "labels/next").json()["pair"] is None
        issue, story = seen["issue_story"], seen["story_epic"]
        assert issue["labels"] == ["yes", "no", "unsure"]
        assert issue["child"]["title"].startswith("#") and issue["child"]["text"]
        assert issue["parent"]["context"].startswith("Epic: ")
        assert story["labels"] == ["primary", "related", "no", "unsure"]
        assert story["parent"]["context"].startswith("The Story's other Epics: ")
        assert kinds(client)["issue_story"]["sources"]["judged"] == 0

        # A judgment can be changed. An answer of the other question is refused.
        assert post(client, f"labels/{issue['id']}", label="primary").status_code == 422
        assert post(client, f"labels/{issue['id']}", label="yes").status_code == 200
        changed = post(client, f"labels/{story['id']}", label="primary").json()
        assert changed["judged"] == len(pairs)
        assert changed["by_label"] == {"yes": 1, "primary": 1, "unsure": len(pairs) - 2}
        recent = client.get(BASE + "labels/next").json()["recent"]
        assert {issue["id"], story["id"]} <= {row["id"] for row in recent}

        # What was judged right is known truth now, and every way is measured on it.
        found = kinds(client)
        assert found["issue_story"]["sources"]["judged"] == 1
        assert found["issue_story"]["methods"][0]["children"] == 2
        assert found["story_epic"]["sources"]["judged"] == 1
        assert found["story_epic"]["methods"][0]["children"] == 1

        # A judgment can be taken back.
        assert post(client, f"labels/{issue['id']}").json()["judged"] == len(pairs) - 1
        assert kinds(client)["issue_story"]["sources"]["judged"] == 0
    finally:
        client.close()
