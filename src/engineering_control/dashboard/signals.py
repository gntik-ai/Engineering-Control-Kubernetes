"""How well a parent is found, measured against what is known to be true.

Before any way of choosing a parent is trusted, it is measured: for the records whose
right parent is known, how often does it rank that parent first, and how often among the
first few? What is known to be true comes from three places: a link the record's own text
states, a choice the administrator made, and a pair the administrator judged here.

The pairs to judge are drawn once per project, on purpose from different situations:
links that look right, links that look doubtful, and likely parents of records that have
none. Whole Epics are held out: their pairs are only ever tested on, never learned from.

Nothing here changes a link.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import numpy
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import ranking
from engineering_control.dashboard.issue_coverage import excerpt
from engineering_control.dashboard.issue_types import bugs_with_optional_stories, is_bug
from engineering_control.dashboard.planning import _visible_issues
from engineering_control.dashboard.planning_links import PlanningLinks, likeness
from engineering_control.dashboard.project_records import (
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.ranking import Ranker, RankerError, digest_of, text_of
from engineering_control.dashboard.review_graph import terms
from engineering_control.db.models import (
    BacklogItem,
    DocFile,
    GoldPair,
    PairScore,
    PlanningLink,
    ProjectEpic,
    ProjectRecord,
    ProjectUserStory,
    RecordVector,
)

log = logging.getLogger(__name__)
ISSUE_STORY: Final = "issue_story"
STORY_EPIC: Final = "story_epic"
LABELS: Final = {
    ISSUE_STORY: ("yes", "no", "unsure"),
    STORY_EPIC: ("primary", "related", "no", "unsure"),
}
# How many pairs of each situation are drawn. Together about 150.
BUCKETS: Final = {
    # A link whose parent the words also rank first: probably right.
    "linked_agree": (ISSUE_STORY, 30),
    # A link whose parent the words rank low: possibly wrong.
    "linked_doubt": (ISSUE_STORY, 30),
    # The words' best Story for an issue that has none: possibly a missing link.
    "unlinked_best": (ISSUE_STORY, 30),
    # The suggested primary Epic, where the texts point to it and where they do not.
    "primary_clear": (STORY_EPIC, 20),
    "primary_open": (STORY_EPIC, 20),
    # The Epic a Story is linked to that its text is least like: possibly no relation.
    "related_weak": (STORY_EPIC, 20),
}
WHY: Final = {
    "linked_agree": "They are linked, and the words agree.",
    "linked_doubt": "They are linked, but the words point elsewhere.",
    "unlinked_best": "The issue has no User Story; this is the one most like it.",
    "primary_clear": "The suggested primary Epic; the texts point to it.",
    "primary_open": "The suggested primary Epic; the texts leave it open.",
    "related_weak": "The Epic of the Story that its text is least like.",
}
QUESTION: Final = {
    ISSUE_STORY: "Is this issue part of delivering this User Story?",
    STORY_EPIC: "What is this Epic to this User Story?",
}
CLEAR: Final = 1.5
# How many candidates of each way of ranking the reranker is asked about, for one record.
# Thirty of each: with fifteen, one right parent in twenty was no candidate at all, and
# what is no candidate can never be chosen.
CANDIDATES: Final = 30
HELD_OUT_ONE_IN: Final = 5


def _spread(*parts: Any) -> str:
    """An order that mixes records of every Epic and is the same each time."""
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()


def _held_out(family: UUID | None) -> bool:
    """Whether the pairs of that Epic are kept for testing only."""
    return family is not None and int(_spread("held", family)[:8], 16) % HELD_OUT_ONE_IN == 0


@dataclass
class _Plan:
    """A project's Epics, User Stories and issues with their links, read once."""

    epics: dict[UUID, tuple[str, str]]
    stories: dict[UUID, tuple[str, str]]
    issues: dict[UUID, tuple[str, str, int | None]]
    # Links by child: parent, who made it, and for a Story's Epic whether it is primary.
    issue_links: dict[UUID, list[tuple[UUID, str, bool]]]
    story_links: dict[UUID, list[tuple[UUID, str, bool, str | None]]]
    orphans: set[UUID]
    postings: dict[str, list[tuple[UUID, int]]] = field(default_factory=dict)
    lengths: dict[UUID, int] = field(default_factory=dict)
    average: float = 1.0

    def primary_epic(self, story: UUID) -> UUID | None:
        return next((p for p, _o, primary, _by in self.story_links.get(story, []) if primary), None)

    def by_words(self, issue: UUID) -> list[tuple[UUID, float]]:
        """The Stories most like the issue by the words they share, best first: what the
        worklist of issues without a Story uses."""
        title, body, _number = self.issues[issue]
        scores: dict[UUID, float] = defaultdict(float)
        total = len(self.stories)
        for word in terms({"title": title, "body": body}):
            hits = self.postings.get(word, [])
            weight = math.log1p((total - len(hits) + 0.5) / (len(hits) + 0.5))
            for key, count in hits:
                scores[key] += (
                    weight
                    * count
                    * 2.2
                    / (count + 1.2 * (0.25 + 0.75 * self.lengths[key] / self.average))
                )
        return sorted(scores.items(), key=lambda entry: (-entry[1], str(entry[0])))

    def texts(self) -> dict[tuple[str, UUID], str]:
        """Every record's text as the ranking service is given it."""
        found = {("epic", key): text_of(title, body) for key, (title, body) in self.epics.items()}
        found.update(
            {("story", key): text_of(title, body) for key, (title, body) in self.stories.items()}
        )
        found.update(
            {("issue", key): text_of(title, body) for key, (title, body, _n) in self.issues.items()}
        )
        return found

    def own_epics(self, story: UUID) -> list[UUID]:
        return [p for p, *_rest in self.story_links.get(story, []) if p in self.epics]

    def epics_by_words(self, story: UUID) -> list[tuple[UUID, float]]:
        """The Story's own Epics, the one its text is most like first."""
        title, body = self.stories[story]
        own = [p for p, *_rest in self.story_links.get(story, []) if p in self.epics]
        scores = likeness(title, body, {epic: " ".join(self.epics[epic]) for epic in own})
        return sorted(scores.items(), key=lambda entry: (-entry[1], str(entry[0])))


def _plan(db: Session, project: ProjectRecord) -> _Plan:
    epics = {
        key: (title, body or "")
        for key, title, body in db.execute(
            select(ProjectEpic.id, ProjectEpic.title, ProjectEpic.description).where(
                ProjectEpic.project_id == project.id, ProjectEpic.archived_at.is_(None)
            )
        )
    }
    stories = {
        key: (title, body or "")
        for key, title, body in db.execute(
            select(ProjectUserStory.id, ProjectUserStory.title, ProjectUserStory.description).where(
                ProjectUserStory.project_id == project.id, ProjectUserStory.archived_at.is_(None)
            )
        )
    }
    bugs = set(db.scalars(bugs_with_optional_stories(db, project.id)))
    issues: dict[UUID, tuple[str, str, int | None]] = {}
    for key, title, body, number in db.execute(
        select(
            BacklogItem.id, BacklogItem.title, BacklogItem.description, BacklogItem.issue_number
        ).where(
            BacklogItem.project_id == project.id,
            _visible_issues(project.id),
            BacklogItem.workspace_review.is_distinct_from("planning"),
            BacklogItem.issue_number.is_not(None),
        )
    ):
        # A bug stands apart from the plan: no parent is looked for, measured or judged.
        if key not in bugs:
            issues[key] = (title, body or "", number)
    issue_links: dict[UUID, list[tuple[UUID, str, bool]]] = defaultdict(list)
    story_links: dict[UUID, list[tuple[UUID, str, bool, str | None]]] = defaultdict(list)
    for kind, child, parent, origin, primary, by in db.execute(
        select(
            PlanningLink.kind,
            PlanningLink.child_id,
            PlanningLink.parent_id,
            PlanningLink.origin,
            PlanningLink.is_primary,
            PlanningLink.primary_by,
        ).where(PlanningLink.project_id == project.id, PlanningLink.removed_at.is_(None))
    ):
        if kind == ISSUE_STORY and child in issues and parent in stories:
            issue_links[child].append((parent, origin, primary))
        elif kind == STORY_EPIC and child in stories and parent in epics:
            story_links[child].append((parent, origin, primary, by))
    plan = _Plan(
        epics,
        stories,
        issues,
        issue_links,
        story_links,
        {key for key in issues if key not in issue_links},
    )
    postings: dict[str, list[tuple[UUID, int]]] = defaultdict(list)
    for key in sorted(stories, key=str):
        title, body = stories[key]
        words = terms({"title": title, "body": body})
        plan.lengths[key] = sum(words.values())
        for word, count in words.items():
            postings[word].append((key, count))
    plan.postings = dict(postings)
    plan.average = sum(plan.lengths.values()) / max(1, len(stories)) or 1.0
    return plan


def _truth(
    db: Session, project: UUID, plan: _Plan
) -> tuple[dict[str, dict[UUID, set[UUID]]], dict[str, Counter[str]]]:
    """The right parents that are known, by kind and child, and where each is known from.
    For a Story it is its primary Epic."""
    known: dict[str, dict[UUID, set[UUID]]] = {
        ISSUE_STORY: defaultdict(set),
        STORY_EPIC: defaultdict(set),
    }
    sources: dict[str, Counter[str]] = {ISSUE_STORY: Counter(), STORY_EPIC: Counter()}
    for issue, links in plan.issue_links.items():
        for parent, origin, _primary in links:
            if origin in ("declared", "administrator"):
                known[ISSUE_STORY][issue].add(parent)
                sources[ISSUE_STORY][origin] += 1
    for story, story_links in plan.story_links.items():
        for parent, origin, primary, by in story_links:
            if len(story_links) < 2:
                continue
            if origin == "declared" or (primary and by == "administrator"):
                known[STORY_EPIC][story].add(parent)
                sources[STORY_EPIC]["declared" if origin == "declared" else "administrator"] += 1
    for pair in db.scalars(
        select(GoldPair).where(GoldPair.project_id == project, GoldPair.label.is_not(None))
    ):
        right = pair.label == ("yes" if pair.kind == ISSUE_STORY else "primary")
        children = plan.issues if pair.kind == ISSUE_STORY else plan.stories
        parents = plan.stories if pair.kind == ISSUE_STORY else plan.epics
        if right and pair.child_id in children and pair.parent_id in parents:
            if pair.parent_id not in known[pair.kind][pair.child_id]:
                known[pair.kind][pair.child_id].add(pair.parent_id)
                sources[pair.kind]["judged"] += 1
    return known, sources


class _Meaning:
    """The project's records ranked by how close their vectors lie."""

    def __init__(self, plan: _Plan, kept: dict[tuple[str, UUID], Any]) -> None:
        self.plan, self.kept = plan, kept
        self.stories = [key for key in sorted(plan.stories, key=str) if ("story", key) in kept]
        self.matrix = (
            numpy.stack([kept["story", key] for key in self.stories]) if self.stories else None
        )

    def of_issue(self, issue: UUID) -> list[UUID] | None:
        """The Stories whose meaning is closest to the issue's, closest first."""
        mine = self.kept.get(("issue", issue))
        if mine is None or self.matrix is None or mine.shape[0] != self.matrix.shape[1]:
            return None
        return [self.stories[place] for place in numpy.argsort(-(self.matrix @ mine))]

    def of_story(self, story: UUID) -> list[UUID] | None:
        """The Story's own Epics, the one closest in meaning first."""
        mine = self.kept.get(("story", story))
        own = [epic for epic in self.plan.own_epics(story) if ("epic", epic) in self.kept]
        if mine is None or len(own) != len(self.plan.own_epics(story)):
            return None
        return sorted(own, key=lambda epic: (-float(self.kept["epic", epic] @ mine), str(epic)))


def joined(*rankings: list[UUID]) -> list[UUID]:
    """Several rankings as one, by reciprocal rank: a candidate that stands high in any
    of them stands high in the result."""
    points: dict[UUID, float] = defaultdict(float)
    for ranks in rankings:
        for place, key in enumerate(ranks):
            points[key] += 1.0 / (60 + place)
    return sorted(points, key=lambda key: (-points[key], str(key)))


def _candidates(words: list[UUID], meaning: list[UUID] | None) -> list[UUID]:
    """The parents the reranker is asked about: the first of each way of ranking."""
    return list(dict.fromkeys([*words[:CANDIDATES], *(meaning or [])[:CANDIDATES]]))


def measure(ranks: dict[UUID, list[UUID]], right: dict[UUID, set[UUID]]) -> dict[str, Any]:
    """How well a ranking finds the known parents: how often the first is right, how high
    the first right one stands on average, and how many are among the first 5 and 20."""
    judged = [child for child in right if child in ranks]
    if not judged:
        return {"children": 0}
    wanted = sum(len(right[child]) for child in judged)

    def among(count: int) -> float:
        return sum(len(set(ranks[c][:count]) & right[c]) for c in judged) / wanted

    return {
        "children": len(judged),
        "top1": round(
            sum(1 for c in judged if ranks[c][:1] and ranks[c][0] in right[c]) / len(judged), 3
        ),
        "mrr": round(
            sum(
                next((1 / (place + 1) for place, p in enumerate(ranks[c]) if p in right[c]), 0.0)
                for c in judged
            )
            / len(judged),
            3,
        ),
        "recall5": round(among(5), 3),
        "recall20": round(among(20), 3),
    }


class Signals:
    def __init__(self, sessions: sessionmaker[Session], links: PlanningLinks) -> None:
        self.sessions = sessions
        self.links = links
        # What the ranking service last said of itself; None where there is no service.
        self.ranker: dict[str, Any] | None = None
        # Whether the reranker's score is wanted for every record of a project and kind,
        # not only for the measured ones: the scorer says so where the score was measured
        # to help. Scoring everything takes hours on a large project, so it is not done
        # on a guess.
        self.wanted_for_all: Callable[[UUID, str], bool] | None = None
        # Records all of whose candidates are scored, so that a pass does not rank them
        # again: by project, kind, record and the digest of its text.
        self._settled: set[tuple[UUID, str, UUID, str]] = set()

    def _ranked(
        self, db: Session, project: UUID, plan: _Plan, known: dict[str, dict[UUID, set[UUID]]]
    ) -> tuple[dict[str, dict[str, dict[UUID, list[UUID]]]], dict[str, Any]]:
        """The known children ranked by each way that has what it needs, and how much of
        the project the ranking service has been asked about."""
        ways: dict[str, dict[str, dict[UUID, list[UUID]]]] = {
            kind: {"words": {}, "meaning": {}, "joined": {}, "reranked": {}}
            for kind in (ISSUE_STORY, STORY_EPIC)
        }
        embedding = ranking.stored_model(db, RecordVector, project)
        reranker = ranking.stored_model(db, PairScore, project)
        kept = ranking.vectors(db, project, embedding) if embedding else {}
        said = ranking.scores(db, project, reranker) if reranker else {}
        texts = plan.texts()
        meaning = _Meaning(plan, kept)
        for kind, child_kind, parent_kind in (
            (ISSUE_STORY, "issue", "story"),
            (STORY_EPIC, "story", "epic"),
        ):
            for child in known[kind]:
                if kind == ISSUE_STORY:
                    words = [parent for parent, _score in plan.by_words(child)]
                    close = meaning.of_issue(child)
                else:
                    words = [parent for parent, _score in plan.epics_by_words(child)]
                    close = meaning.of_story(child)
                ways[kind]["words"][child] = words
                if close is None:
                    continue
                ways[kind]["meaning"][child] = close
                ways[kind]["joined"][child] = joined(words, close)
                candidates = _candidates(words, close)
                fits = []
                for parent in candidates:
                    kept_score = said.get((kind, child, parent))
                    wanted = digest_of(texts[child_kind, child], texts[parent_kind, parent])
                    if kept_score is None or kept_score[0] != wanted:
                        break
                    fits.append((kept_score[1], parent))
                else:
                    ways[kind]["reranked"][child] = [
                        parent
                        for _score, parent in sorted(fits, key=lambda fit: (-fit[0], str(fit[1])))
                    ]
        coverage = {
            "embedding": embedding,
            "rerank": reranker,
            "records": len(texts),
            "vectors": sum(1 for key in texts if key in kept),
            "pairs": len(said),
        }
        return ways, coverage

    def report(self, name: str) -> dict[str, Any]:
        """What is known to be true in the project, how well each way of finding a parent
        does against it, and how far the judging of pairs is."""
        self.links.reconcile_project(name)
        with self.sessions() as db:
            project = active_project(db, name)
            plan = _plan(db, project)
            known, sources = _truth(db, project.id, plan)
            ways, coverage = self._ranked(db, project.id, plan, known)
            progress = self._progress(db, project.id)
        labels = {
            "words": "Shared words, as the worklists use now",
            "meaning": "Meaning: how close the vectors of the two texts lie",
            "joined": "Words and meaning joined by rank",
            "reranked": "The reranker, on the first candidates of words and of meaning",
        }
        return {
            "kinds": [
                {
                    "kind": kind,
                    "known": sum(len(parents) for parents in known[kind].values()),
                    "sources": {
                        key: sources[kind][key] for key in ("declared", "administrator", "judged")
                    },
                    "methods": [
                        {"key": key, "label": labels[key], **measure(ranks, known[kind])}
                        for key, ranks in ways[kind].items()
                        if key == "words" or ranks
                    ],
                }
                for kind in (ISSUE_STORY, STORY_EPIC)
            ],
            "labels": progress,
            # The ranking service: whether it answers, which models it runs, and how much
            # of the project it has been asked about.
            "ranker": {**(self.ranker or {"ready": False, "absent": True}), **coverage},
        }

    # ------------------------------------------------------- the ranking service

    def _project_names(self) -> list[str]:
        with self.sessions() as db:
            return list(
                db.scalars(
                    select(ProjectRecord.name)
                    .where(ProjectRecord.archived_at.is_(None))
                    .order_by(ProjectRecord.name)
                )
            )

    def _vectors_due(self, name: str, model: str) -> list[tuple[UUID, str, UUID, str, str]]:
        """The records whose text has no vector of that model yet, or has changed since."""
        with self.sessions() as db:
            project = active_project(db, name)
            have = ranking.vector_digests(db, project.id, model)
            due = []
            texts = _plan(db, project).texts()
            # Each document of the inventory too: what a User Story is like among them
            # is evidence of where the Story stands.
            for key, path, title, body in db.execute(
                select(DocFile.id, DocFile.path, DocFile.title, DocFile.text).where(
                    DocFile.project_id == project.id,
                    DocFile.removed_scan_id.is_(None),
                    DocFile.text.is_not(None),
                )
            ):
                texts["doc", key] = text_of(title or path, body)
            for (kind, record), text in texts.items():
                wanted = digest_of(text)
                if have.get((kind, record)) != wanted:
                    due.append((project.id, kind, record, text, wanted))
            return due

    def _keep_vectors(
        self, rows: list[tuple[UUID, str, UUID, str, str]], model: str, found: list[bytes]
    ) -> None:
        with self.sessions.begin() as db:
            for (project, kind, record, _text, wanted), vector in zip(rows, found, strict=True):
                ranking.keep_vector(db, project, kind, record, model, wanted, vector)

    def _pairs_due(
        self, name: str, embedding: str, reranker: str, limit: int = 400
    ) -> list[tuple[UUID, str, UUID, str, list[tuple[UUID, str, str]]]]:
        """For each record that is measured or judged, the candidate parents the reranker
        has not scored yet: the record's text, and each parent with its text. Where the
        scorer uses the reranker's score, every record a parent is looked for follows,
        the measured ones first. About ``limit`` pairs, so that a pass stays short."""
        self.links.reconcile_project(name)
        with self.sessions() as db:
            project = active_project(db, name)
            plan = _plan(db, project)
            known, _sources = _truth(db, project.id, plan)
            texts = plan.texts()
            meaning = _Meaning(plan, ranking.vectors(db, project.id, embedding))
            said = ranking.scores(db, project.id, reranker)
            drawn: dict[str, dict[UUID, set[UUID]]] = {
                ISSUE_STORY: defaultdict(set),
                STORY_EPIC: defaultdict(set),
            }
            for pair in db.scalars(select(GoldPair).where(GoldPair.project_id == project.id)):
                drawn[pair.kind][pair.child_id].add(pair.parent_id)
            due = []
            found = 0
            for kind, child_kind, parent_kind in (
                (ISSUE_STORY, "issue", "story"),
                (STORY_EPIC, "story", "epic"),
            ):
                children = plan.issues if kind == ISSUE_STORY else plan.stories
                first = sorted({*known[kind], *drawn[kind]}, key=str)
                rest: list[UUID] = []
                if self.wanted_for_all is not None and self.wanted_for_all(project.id, kind):
                    asked = set(first)
                    rest = [
                        child
                        for child in sorted(children, key=str)
                        if child not in asked
                        and (kind == ISSUE_STORY or len(plan.own_epics(child)) > 1)
                    ]
                for child in [*first, *rest]:
                    if found >= limit:
                        break
                    if child not in children:
                        continue
                    settled = (project.id, kind, child, digest_of(texts[child_kind, child]))
                    if settled in self._settled:
                        continue
                    if kind == ISSUE_STORY:
                        words = [parent for parent, _score in plan.by_words(child)]
                        close = meaning.of_issue(child)
                    else:
                        words = [parent for parent, _score in plan.epics_by_words(child)]
                        close = meaning.of_story(child)
                    if close is None:
                        # Its vector is not there yet: it is scored on a later pass.
                        continue
                    # A link whose truth is not known is scored too, wherever it stands.
                    linked = (
                        [parent for parent, _o, _p in plan.issue_links.get(child, [])]
                        if kind == ISSUE_STORY and child not in known[kind]
                        else []
                    )
                    wanted = [
                        parent
                        for parent in dict.fromkeys(
                            [*_candidates(words, close), *drawn[kind][child], *linked]
                        )
                        if (parent_kind, parent) in texts
                    ]
                    missing = []
                    for parent in wanted:
                        both = digest_of(texts[child_kind, child], texts[parent_kind, parent])
                        kept_score = said.get((kind, child, parent))
                        if kept_score is None or kept_score[0] != both:
                            missing.append((parent, texts[parent_kind, parent], both))
                    if missing:
                        due.append((project.id, kind, child, texts[child_kind, child], missing))
                        found += len(missing)
                    elif len(self._settled) < 200_000:
                        self._settled.add(settled)
            return due

    def _keep_scores(
        self,
        project: UUID,
        kind: str,
        child: UUID,
        parents: list[tuple[UUID, str, str]],
        model: str,
        found: list[float],
    ) -> None:
        with self.sessions.begin() as db:
            for (parent, _text, both), score in zip(parents, found, strict=True):
                ranking.keep_score(db, project, kind, child, parent, model, both, score)

    async def ask_ranker(self, ranker: Ranker, *, limit: int = 400) -> int:
        """Bring what is kept of the ranking service's answers up to date: a vector for
        every record whose text is new or changed, and a score for every candidate parent
        of the records that are measured or judged. How many answers were asked for; at
        most ``limit`` in one pass, so that a large project does not hold the others up."""
        health = await ranker.health()
        self.ranker = health
        if not health.get("ready"):
            return 0
        embedding, reranker = str(health.get("embedding")), str(health.get("rerank"))
        asked = 0
        for name in await asyncio.to_thread(self._project_names):
            try:
                due = await asyncio.to_thread(self._vectors_due, name, embedding)
                for start in range(0, len(due), ranking.BATCH):
                    if asked >= limit:
                        return asked
                    rows = due[start : start + ranking.BATCH]
                    model, found = await ranker.embed([row[3] for row in rows])
                    await asyncio.to_thread(self._keep_vectors, rows, model, found)
                    asked += len(rows)
                pairs = await asyncio.to_thread(self._pairs_due, name, embedding, reranker)
                for project, kind, child, text, parents in pairs:
                    if asked >= limit:
                        return asked
                    model, scores = await ranker.rerank(text, [parent[1] for parent in parents])
                    await asyncio.to_thread(
                        self._keep_scores, project, kind, child, parents, model, scores
                    )
                    asked += len(parents)
            except RankerError as error:
                self.ranker = {**health, "ready": False, "detail": str(error)}
                return asked
            except ProjectNotFound:
                continue
        return asked

    # ----------------------------------------------------------- pairs to judge

    def _pairs(self, db: Session, project: UUID) -> tuple[list[GoldPair], int]:
        """The pairs that stand, and how many were set aside because their issue is a
        bug: a bug has no User Story, so there is nothing to judge."""
        bugs = set(db.scalars(bugs_with_optional_stories(db, project)))
        pairs = db.scalars(select(GoldPair).where(GoldPair.project_id == project)).all()
        stand = [p for p in pairs if not (p.kind == ISSUE_STORY and p.child_id in bugs)]
        return stand, len(pairs) - len(stand)

    def _progress(self, db: Session, project: UUID) -> dict[str, Any]:
        pairs, set_aside = self._pairs(db, project)
        rows = [
            (bucket, label, count)
            for (bucket, label), count in Counter((p.bucket, p.label) for p in pairs).items()
        ]
        drawn = sum(count for _b, _l, count in rows)
        judged = sum(count for _b, label, count in rows if label is not None)
        by_label: Counter[str] = Counter()
        for _bucket, label, count in rows:
            if label is not None:
                by_label[label] += count
        return {
            "drawn": drawn,
            "judged": judged,
            "wanted": sum(count for _kind, count in BUCKETS.values()),
            "set_aside": set_aside,
            "by_label": dict(by_label),
            "buckets": [
                {
                    "bucket": bucket,
                    "why": WHY[bucket],
                    "drawn": sum(c for b, _l, c in rows if b == bucket),
                    "judged": sum(c for b, label, c in rows if b == bucket and label is not None),
                }
                for bucket in BUCKETS
            ],
        }

    def draw(self, name: str) -> dict[str, Any]:
        """Draw the pairs to judge, or the ones still missing from the set. A pair whose
        answer is already known, from the text or from the administrator, is not drawn."""
        self.links.reconcile_project(name)
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            plan = _plan(db, project)
            known, _sources = _truth(db, project.id, plan)
            # A pair whose issue turned out to be a bug is set aside, and its place in
            # the set is drawn again.
            standing, _set_aside = self._pairs(db, project.id)
            have = {(pair.kind, pair.child_id, pair.parent_id): pair.bucket for pair in standing}
            taken: Counter[str] = Counter(have.values())
            drawn_children = {(kind, child) for kind, child, _parent in have}
            found: dict[str, list[tuple[UUID, UUID]]] = defaultdict(list)

            def enough(kind: str) -> bool:
                """Whether every situation of that kind has the pairs it asks for. Records
                are looked at in an order that mixes the Epics, and no further than needed:
                ranking the candidates of every record of a large project takes long."""
                return all(
                    taken[bucket] + len(found[bucket]) >= wanted
                    for bucket, (of, wanted) in BUCKETS.items()
                    if of == kind
                )

            for issue in sorted(plan.issues, key=lambda key: _spread("issue", key)):
                if enough(ISSUE_STORY):
                    break
                if issue in known[ISSUE_STORY] or (ISSUE_STORY, issue) in drawn_children:
                    continue
                linked = [parent for parent, _origin, _primary in plan.issue_links.get(issue, [])]
                if not linked and issue not in plan.orphans:
                    continue
                ranked = plan.by_words(issue)
                order = [parent for parent, _score in ranked]
                for parent in linked[:1]:
                    place = order.index(parent) if parent in order else len(order)
                    if place == 0:
                        found["linked_agree"].append((issue, parent))
                    elif place >= 5:
                        found["linked_doubt"].append((issue, parent))
                if issue in plan.orphans and ranked and ranked[0][1] >= 2.0:
                    found["unlinked_best"].append((issue, ranked[0][0]))
            for story in sorted(plan.story_links, key=lambda key: _spread("story", key)):
                story_links = plan.story_links[story]
                if enough(STORY_EPIC):
                    break
                if story in known[STORY_EPIC] or len(story_links) < 2:
                    continue
                if (STORY_EPIC, story) in drawn_children:
                    continue
                ranked = plan.epics_by_words(story)
                suggested = plan.primary_epic(story)
                weakest = len(ranked) >= 3 and ranked[-1][0] != suggested
                # One pair a Story: the weakest of its Epics for every other Story that has
                # three or more, the suggested primary Epic for the rest.
                if weakest and int(_spread("which", story)[:2], 16) % 2 == 0:
                    found["related_weak"].append((story, ranked[-1][0]))
                elif suggested is not None and len(ranked) >= 2:
                    clear = ranked[0][1] > 0 and ranked[0][1] >= CLEAR * ranked[1][1]
                    found["primary_clear" if clear else "primary_open"].append((story, suggested))
            added = 0
            for bucket, (kind, wanted) in BUCKETS.items():
                pairs = sorted(found[bucket], key=lambda pair: _spread(bucket, *pair))
                for child, parent in pairs:
                    if taken[bucket] >= wanted:
                        break
                    if (kind, child, parent) in have or (kind, child) in drawn_children:
                        continue
                    family = plan.primary_epic(parent) if kind == ISSUE_STORY else parent
                    db.add(
                        GoldPair(
                            project_id=project.id,
                            kind=kind,
                            child_id=child,
                            parent_id=parent,
                            bucket=bucket,
                            held_out=_held_out(family),
                        )
                    )
                    drawn_children.add((kind, child))
                    taken[bucket] += 1
                    added += 1
            db.flush()
            return {"added": added, **self._progress(db, project.id)}

    def _shown(self, pair: GoldPair, project: ProjectRecord, plan: _Plan) -> dict[str, Any] | None:
        if pair.kind == ISSUE_STORY:
            if pair.child_id not in plan.issues or pair.parent_id not in plan.stories:
                return None
            title, body, number = plan.issues[pair.child_id]
            child = {
                "kind": "issue",
                "id": str(pair.child_id),
                "title": f"#{number} {title}" if number else title,
                "text": excerpt(body),
                "url": (
                    f"https://github.com/{project.source_repository}/issues/{number}"
                    if number
                    else None
                ),
            }
            parent_title, parent_body = plan.stories[pair.parent_id]
            epic = plan.primary_epic(pair.parent_id)
            parent = {
                "kind": "story",
                "id": str(pair.parent_id),
                "title": parent_title,
                "text": excerpt(parent_body),
                "context": f"Epic: {plan.epics[epic][0]}" if epic in plan.epics else None,
            }
        else:
            if pair.child_id not in plan.stories or pair.parent_id not in plan.epics:
                return None
            title, body = plan.stories[pair.child_id]
            child = {
                "kind": "story",
                "id": str(pair.child_id),
                "title": title,
                "text": excerpt(body),
            }
            others = [
                plan.epics[p][0]
                for p, *_rest in plan.story_links.get(pair.child_id, [])
                if p != pair.parent_id and p in plan.epics
            ]
            parent_title, parent_body = plan.epics[pair.parent_id]
            parent = {
                "kind": "epic",
                "id": str(pair.parent_id),
                "title": parent_title,
                "text": excerpt(parent_body),
                "context": ("The Story's other Epics: " + " · ".join(sorted(others)))
                if others
                else None,
            }
        return {
            "id": str(pair.id),
            "kind": pair.kind,
            "question": QUESTION[pair.kind],
            "labels": list(LABELS[pair.kind]),
            "why": WHY.get(pair.bucket, ""),
            "label": pair.label,
            "child": child,
            "parent": parent,
        }

    def next(self, name: str, after: UUID | None = None) -> dict[str, Any]:
        """The next pair to judge, with both texts; and the last ones judged, to change."""
        with self.sessions() as db:
            project = active_project(db, name)
            plan = _plan(db, project)
            pairs, _set_aside = self._pairs(db, project.id)
            waiting = sorted(
                (pair for pair in pairs if pair.label is None and pair.id != after),
                key=lambda pair: _spread("order", pair.id),
            )
            shown = next(
                (found for pair in waiting if (found := self._shown(pair, project, plan))), None
            )
            judged = sorted(
                (pair for pair in pairs if pair.judged_at is not None),
                key=lambda pair: pair.judged_at or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )
            recent = [found for pair in judged[:5] if (found := self._shown(pair, project, plan))]
            return {"pair": shown, "recent": recent, **self._progress(db, project.id)}

    def judge(self, name: str, pair_id: UUID, label: str | None) -> dict[str, Any]:
        """Record the administrator's judgment of a pair, change it, or take it back."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            pair = db.get(GoldPair, pair_id)
            if pair is None or pair.project_id != project.id:
                raise ProjectNotFound(str(pair_id))
            if pair.kind == ISSUE_STORY and is_bug(db, project.id, pair.child_id):
                raise ProjectInvalid("This issue is a bug: it has no User Story to judge.")
            if label is not None and label not in LABELS[pair.kind]:
                raise ProjectInvalid("That is no answer to this question.")
            pair.label = label
            pair.judged_at = datetime.now(UTC) if label is not None else None
            db.flush()
            return {"id": str(pair.id), "label": pair.label, **self._progress(db, project.id)}


async def worker(signals: Signals, ranker: Ranker) -> None:
    """Keep the ranking service's answers up to date: at once while there is something to
    ask, every two minutes when there is not."""
    await asyncio.sleep(60)
    while True:
        try:
            asked = await signals.ask_ranker(ranker)
        except Exception:
            log.exception("Asking the ranking service failed")
            asked = 0
        # A record that just arrived is read within two minutes.
        await asyncio.sleep(5 if asked else 120)
