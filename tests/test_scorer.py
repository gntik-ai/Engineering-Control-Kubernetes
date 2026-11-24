"""The scorer learns one judgment from every signal, says when it does not know, and
changes nothing: what it would do is a report."""

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy
from sqlalchemy import select

from engineering_control.dashboard.scorer import (
    ISSUE_STORY,
    _Case,
    fit,
    kept,
    limit,
    measure,
    surely,
)
from engineering_control.dashboard.workspace_issues import import_issues
from engineering_control.db.models import (
    BacklogItem,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    ScorerDecision,
)
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed
from tests.test_signals import Ranking

BASE = "/api/dashboard/projects/falcone/"
TOPICS = {
    "Tenant quotas": "quota tenant limit memory usage metering ceiling",
    "Signing keys": "signing key rotation certificate expiry vault secret",
    "Webhook delivery": "webhook delivery retry backoff receiver endpoint",
    "Console themes": "console theme dark contrast palette appearance",
    "Function logs": "function log stream tail retention search",
    "Workflow schedules": "workflow schedule cron trigger calendar timezone",
}


def test_the_bound_keeps_what_cannot_be_ruled_out() -> None:
    # Of twenty right parents, the one that fell furthest short may be ruled out at 5%.
    misses = [0.02 * place for place in range(1, 21)]
    assert limit(misses, 0.05) == 0.4
    assert limit(misses, 0.5) == 0.22
    # Too few known cases to promise anything: nothing is ruled out.
    assert limit([0.1, 0.2], 0.05) == 1.0 and limit([], 0.05) == 1.0
    shares = numpy.array([0.05, 0.7, 0.2])
    assert kept(shares, 0.4) == [1]
    assert kept(shares, 0.85) == [1, 2]
    assert kept(shares, 0.1) == []
    assert kept(shares, 1.0) == [1, 2, 0]


def test_one_candidate_left_is_acted_on_only_where_that_was_shown_to_be_right() -> None:
    right = [(0.9 - 0.01 * place, True) for place in range(12)]
    wrong = [(0.5, False), (0.4, False)]
    # Down to the twelfth it was always right; the next two would make it wrong too often.
    assert surely([*right, *wrong, (0.3, True)], 0.05) == right[-1][0]
    # Too few cases show nothing, however right they were.
    assert surely(right[:9], 0.05) is None
    # One slip among many right ones stays within the error.
    assert surely([(0.95, False), *[(0.8, True)] * 30], 0.05) == 0.8
    assert surely([], 0.05) is None


def _cases(count: int, noise: float, seed_: int) -> list[_Case]:
    """Records with six candidates each: the right one stands out on two signals, more
    or less clearly."""
    chance = numpy.random.default_rng(seed_)
    project = uuid4()
    found = []
    for number in range(count):
        parents = [uuid4() for _ in range(6)]
        rows = chance.normal(0.0, 1.0, (6, 9))
        rows[2, 0] += 3.0 - noise * (number % 3)
        rows[2, 2] += 2.5 - noise * (number % 3)
        found.append(
            _Case(project, ISSUE_STORY, uuid4(), parents, rows, None, {parents[2]}, fold=number % 5)
        )
    return found


def test_the_scorer_learns_the_signals_and_abstains_when_unsure() -> None:
    cases = _cases(150, 0.8, 7)
    rows = numpy.vstack([case.light for case in cases])
    labels = numpy.array([1.0 if place == 2 else 0.0 for _ in cases for place in range(6)])
    model = fit(rows, labels)
    assert model is not None
    # The two signals that carry the answer get the weight; the others stay near nothing.
    assert model.weights[0] > 1.0 and model.weights[2] > 1.0
    others = [abs(w) for place, w in enumerate(model.weights) if place not in (0, 2)]
    assert max(others) < 0.5
    assert fit(rows, numpy.zeros(len(rows))) is None

    found = measure(cases, False)
    assert found is not None and found["children"] == 150
    assert found["top1"] > 0.75
    # The promise: a right parent is ruled out at most about one time in twenty.
    assert found["in_set"] >= 0.9
    # Where it would act it is more often right than its first guess is overall, and it
    # does abstain on some.
    assert found["acted_right"] >= found["top1"] and found["acted_right"] >= 0.9
    assert 0.0 < found["acted"] < 1.0 and found["several"] + found["none"] > 0.0
    # The same input gives the same answer.
    assert measure(cases, False) == found
    assert measure(cases[:5], False) is None


def _plan(repo: Any, data: dict[str, Any]) -> dict[str, UUID]:
    """Six Stories with five issues each that the administrator linked, and issues that
    have no Story yet."""
    pid = data["project"]
    stories: dict[str, UUID] = {}
    with repo.sessions.begin() as db:
        project = db.get_one(ProjectRecord, pid)
        second = ProjectEpic(project_id=pid, title="Platform", description="Platform services.")
        db.add(second)
        db.flush()
        for place, (title, words) in enumerate(TOPICS.items()):
            story = ProjectUserStory(project_id=pid, title=title, description=f"About {words}.")
            db.add(story)
            db.flush()
            stories[title] = story.id
            epic = data["epic"] if place % 2 else second.id
            db.add(ProjectStoryEpic(project_id=pid, story_id=story.id, epic_id=epic))
        issues = []
        number = 100
        for words in TOPICS.values():
            terms = words.split()
            for turn in range(5):
                number += 1
                picked = " ".join(terms[turn % len(terms) :] + terms[: turn % len(terms)][:2])
                issues.append(
                    {
                        "number": number,
                        "state": "open",
                        "title": f"Work on {terms[turn % len(terms)]} {terms[-turn - 1]}",
                        "body": f"Implement {picked} handling.",
                        "labels": ["task"],
                    }
                )
        issues += [
            {
                "number": 201,
                "state": "open",
                "title": "Rotate the signing key before expiry",
                "body": "The signing key rotation must respect certificate expiry in the vault.",
                "labels": ["task"],
            },
            {
                "number": 202,
                "state": "open",
                "title": "bug: webhook delivery retry never stops",
                "body": "The webhook delivery retry ignores backoff for a receiver endpoint.",
                "labels": ["task"],
            },
        ]
        import_issues(db, project, issues)
        db.flush()
        items = {
            item.issue_number: item
            for item in db.scalars(select(BacklogItem).where(BacklogItem.project_id == pid))
        }
        number = 100
        for title in TOPICS:
            for _turn in range(5):
                number += 1
                db.add(
                    ProjectIssueStory(
                        project_id=pid, item_id=items[number].id, story_id=stories[title]
                    )
                )
    # The administrator made these links: they are known to be right.
    repo.planning_links._in_step("falcone", "issues", None, made_by="administrator")
    return stories


def test_the_scorer_reports_what_it_would_do_and_changes_nothing(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        stories = _plan(repo, data)
        # Without vectors there is nothing to combine.
        repo.scorer.refresh()
        early = client.get(BASE + "scorer").json()
        assert not early["ready"] and "vectors" in early["why"]

        service = Ranking()
        for _ in range(20):
            if not asyncio.run(repo.signals.ask_ranker(service)):  # type: ignore[arg-type]
                break
        with repo.sessions() as db:
            links_before = len(db.scalars(select(ProjectIssueStory)).all())
        written = repo.scorer.refresh()
        assert written > 0
        # Nothing it reads has changed: it is not computed again.
        assert repo.scorer.refresh() == 0

        report = client.get(BASE + "scorer")
        assert report.status_code == 200, report.text
        found = report.json()
        assert found["ready"] and found["errors"] == {"add": 0.05, "remove": 0.02}
        issue_story = next(kind for kind in found["kinds"] if kind["kind"] == "issue_story")
        assert issue_story["ready"] and issue_story["known_here"] >= 30
        assert issue_story["measured_here"]
        light = next(row for row in issue_story["measured"] if row["key"] == "light")
        assert light["children"] >= 30 and light["top1"] >= 0.8 and light["in_set"] >= 0.8
        assert set(issue_story["weights"]) >= {"words", "meaning", "siblings", "both_lists"}
        assert issue_story["would"]["known"] >= 30
        assert [row["reason"] for row in found["removal"]]

        # The issue without a Story is spoken of; the bug is not: a bug has no Story.
        with repo.sessions() as db:
            orphan = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 201)).one()
            bug = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 202)).one()
            said = {row.child_id: row for row in db.scalars(select(ScorerDecision)).all()}
        assert bug.id not in said
        about = said[orphan.id]
        assert about.outcome in ("place", "several", "none")
        assert about.parent_id == stories["Signing keys"]
        listed = client.get(BASE + "scorer/decisions", params={"kind": "issue_story"}).json()
        assert listed["total"] >= 1
        row = next(item for item in listed["items"] if item["child"]["id"] == str(orphan.id))
        assert row["parent"]["title"] == "Signing keys" and row["signals"]["words"] == 1.0
        assert row["says"]

        # Shadow mode: no link was made, removed or proposed.
        with repo.sessions() as db:
            assert len(db.scalars(select(ProjectIssueStory)).all()) == links_before
            assert not db.scalars(
                select(ProjectIssueStory).where(ProjectIssueStory.item_id == orphan.id)
            ).all()
    finally:
        client.close()
