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


class Ranking:
    """Stands in for the ranking service: a vector from a text's words, a score from how
    many words two texts share."""

    def __init__(self) -> None:
        self.ready = True
        self.embedded: list[str] = []
        self.scored: list[tuple[str, list[str]]] = []

    async def health(self) -> dict[str, Any]:
        return {"ready": self.ready, "embedding": "words-64", "rerank": "overlap"}

    async def embed(self, texts: list[str]) -> tuple[str, list[bytes]]:
        import numpy

        self.embedded.extend(texts)
        found = []
        for text in texts:
            vector = numpy.zeros(64, dtype=numpy.float32)
            for word in text.lower().split():
                vector[sum(word.encode()) % 64] += 1
            found.append((vector / (numpy.linalg.norm(vector) or 1)).tobytes())
        return "words-64", found

    async def rerank(self, query: str, documents: list[str]) -> tuple[str, list[float]]:
        self.scored.append((query, documents))
        mine = set(query.lower().split())
        return "overlap", [float(len(mine & set(d.lower().split()))) for d in documents]


def test_the_ranking_service_is_asked_once_for_each_text(tmp_path: Path) -> None:
    import asyncio

    from engineering_control.db.models import BacklogItem, PairScore, RecordVector

    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        more_epics(repo, data)
        service = Ranking()
        # No service, no answers: only words are measured.
        absent = client.get(BASE + "signals").json()
        assert absent["ranker"]["absent"] and absent["ranker"]["vectors"] == 0
        assert [m["key"] for m in kinds(client)["issue_story"]["methods"]] == ["words"]

        asked = asyncio.run(repo.signals.ask_ranker(service))  # type: ignore[arg-type]
        with repo.sessions() as db:
            vectors = db.scalars(
                select(RecordVector).where(RecordVector.project_id == data["project"])
            ).all()
            scores = db.scalars(
                select(PairScore).where(PairScore.project_id == data["project"])
            ).all()
            locked = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 44)).first()
            assert locked is not None
        # Every Epic, Story and issue of the project has a vector; the issue whose Story is
        # known has a score for each candidate.
        assert {vector.kind for vector in vectors} == {"epic", "story", "issue"}
        assert len(vectors) == 3 + 3 + 5 and all(len(v.vector) == 64 * 4 for v in vectors)
        assert scores and {score.child_id for score in scores} == {locked.id}
        assert asked >= len(vectors) + len(scores)
        assert not any("Story:" in text for text in service.embedded)

        report = client.get(BASE + "signals").json()
        assert report["ranker"]["ready"] and report["ranker"]["embedding"] == "words-64"
        assert (report["ranker"]["vectors"], report["ranker"]["records"]) == (11, 11)
        methods = {m["key"]: m for m in report["kinds"][0]["methods"]}
        assert list(methods) == ["words", "meaning", "joined", "reranked"]
        assert all(method["children"] == 1 for method in methods.values())

        # Nothing changed: nothing is asked again.
        before = (len(service.embedded), len(service.scored))
        assert asyncio.run(repo.signals.ask_ranker(service)) == 0  # type: ignore[arg-type]
        assert (len(service.embedded), len(service.scored)) == before
        # A text that changed gets a new vector, and its pairs new scores.
        with repo.sessions.begin() as db:
            db.get_one(BacklogItem, locked.id).description = "Notify the operator of failed runs."
        assert asyncio.run(repo.signals.ask_ranker(service)) > 0  # type: ignore[arg-type]
        assert len(service.embedded) == before[0] + 1 and len(service.scored) == before[1] + 1

        # A service that is not ready is not asked.
        service.ready = False
        assert asyncio.run(repo.signals.ask_ranker(service)) == 0  # type: ignore[arg-type]
        assert not client.get(BASE + "signals").json()["ranker"]["ready"]
    finally:
        client.close()
