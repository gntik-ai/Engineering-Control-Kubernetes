"""The scorer: one learned judgment from every signal, in shadow mode.

No single way of finding a parent is good enough alone (Reviews → Reorganize measures
them). The scorer combines them: shared words, closeness of meaning, the reranker's score,
how the titles overlap, and whether the parent's other children resemble the record. It is
a logistic regression, so each signal's weight can be read, and the same input always
gives the same answer.

What makes it safe to use is that it says when it does not know. From the links whose
truth is known it learns how low a right parent's score can fall, and for a new record it
keeps every candidate that cannot be ruled out at the chosen error rate (split conformal
prediction). One candidate left: the scorer would act. Several, or none: it abstains, and
the case goes on to a decision model or to the administrator.

Shadow mode: everything here is computed, measured and shown, and nothing is changed. No
link is made, removed or proposed. Bugs are left out throughout: a bug has no User Story.

It learns from every project together, because the signals are relative (a rank, a share
of the best score) and a project may have little known truth of its own; each project is
measured on its own known links once it has enough of them.
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
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import ranking
from engineering_control.dashboard.planning_links import PlanningLinks
from engineering_control.dashboard.project_records import ProjectNotFound, active_project
from engineering_control.dashboard.ranking import digest_of
from engineering_control.dashboard.review_graph import terms
from engineering_control.dashboard.signals import (
    ISSUE_STORY,
    STORY_EPIC,
    _candidates,
    _Meaning,
    _Plan,
    _plan,
    _spread,
    _truth,
)
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
    ReorgBatch,
    ReorgLine,
    ScorerDecision,
    ScorerReport,
)

log = logging.getLogger(__name__)

# Of the right parents, at most this share may fall outside what the scorer keeps: the
# error allowed when it would add or choose something, and the stricter one when it would
# call an existing link unsupported.
ADD_ERROR: Final = 0.05
REMOVE_ERROR: Final = 0.02
FOLDS: Final = 5
# A project is measured on its own known links from this many; below, on all projects'.
OWN_FROM: Final = 20
# Fewer known links than this in all projects together: nothing is learned yet.
LEARN_FROM: Final = 12
SIBLINGS: Final = 20
# One candidate left is acted on only once at least this many such cases were measured.
ACT_FROM: Final = 10
L2: Final = 1.0
# Two texts whose vectors lie this close say nearly the same.
SAME_TEXT: Final = 0.95
# What one question to a language model has cost in the worklists so far, in tokens.
TOKENS_A_QUESTION: Final = 1400
# The scorer is not computed again sooner than this, however often its inputs change.
REST_SECONDS: Final = 300

BASE: Final = (
    "words",
    "words_place",
    "meaning",
    "meaning_gap",
    "meaning_place",
    "title",
    "siblings",
    "has_siblings",
)
EXTRA: Final = {ISSUE_STORY: "both_lists", STORY_EPIC: "epic_share"}
RERANKED: Final = ("reranker", "reranker_gap", "reranker_first")
SIGNALS: Final = {
    "words": "Shared words, as a share of the best candidate's",
    "words_place": "Place by shared words",
    "meaning": "Closeness of meaning",
    "meaning_gap": "Distance in meaning from the closest candidate",
    "meaning_place": "Place by meaning",
    "title": "Words the two titles share",
    "siblings": "Likeness to the parent's other children",
    "has_siblings": "The parent has other children",
    "both_lists": "Among the first by words and by meaning",
    "epic_share": "Share of all User Stories the Epic holds",
    "reranker": "The reranker's score",
    "reranker_gap": "Distance from the reranker's best candidate",
    "reranker_first": "The reranker's first choice",
}
OUTCOMES: Final = {
    "place": "Would place the issue under this User Story",
    "primary": "Would choose this Epic as the primary one",
    "confirmed": "Would record this link as supported",
    "kept": "Would leave this link as it is",
    "unsupported": "Would call this link unsupported",
    "unsure": "Abstains: one candidate is left, but not surely enough",
    "several": "Abstains: several candidates cannot be ruled out",
    "none": "Abstains: no candidate is good enough",
}


def names(kind: str, full: bool) -> tuple[str, ...]:
    return (*BASE, EXTRA[kind], *(RERANKED if full else ()))


@dataclass
class _Case:
    """One record with its candidate parents and what every signal says of each."""

    project: UUID
    kind: str
    child: UUID
    parents: list[UUID]
    light: Any
    # The same with the reranker's signals, where it has scored every candidate.
    full: Any | None
    # Parents known to be right, and ones judged not to be.
    right: set[UUID] = field(default_factory=set)
    wrong: set[UUID] = field(default_factory=set)
    linked: list[UUID] = field(default_factory=list)
    fold: int = 0
    suggested: UUID | None = None
    # The Epic the record belongs to, where that is known: records of one Epic are
    # kept in one fold, so that none is scored by a model that saw its neighbours.
    family: UUID | None = None


@dataclass
class _Model:
    mean: Any
    scale: Any
    weights: Any
    bias: float

    def shares(self, rows: Any) -> Any:
        """For each candidate, the share of belief that it is the parent; what is left
        of one is the belief that none of them is."""
        logits = ((rows - self.mean) / self.scale) @ self.weights + self.bias
        odds = numpy.exp(numpy.clip(logits, -30.0, 30.0))
        return odds / (1.0 + odds.sum())


def fit(rows: Any, labels: Any) -> _Model | None:
    """A logistic regression of whether a candidate is the parent, by Newton's method
    with a penalty on large weights. None where there is nothing to tell apart."""
    if len(rows) == 0 or labels.min() == labels.max():
        return None
    mean = rows.mean(axis=0)
    scale = rows.std(axis=0)
    scale[scale < 1e-9] = 1.0
    inputs = numpy.hstack([(rows - mean) / scale, numpy.ones((len(rows), 1))])
    penalty = numpy.eye(inputs.shape[1]) * L2
    penalty[-1, -1] = 1e-6
    weights = numpy.zeros(inputs.shape[1])
    for _ in range(40):
        said = 1.0 / (1.0 + numpy.exp(-numpy.clip(inputs @ weights, -30.0, 30.0)))
        slope = inputs.T @ (labels - said) - penalty @ weights
        curve = (inputs * (said * (1.0 - said))[:, None]).T @ inputs + penalty
        step = numpy.linalg.solve(curve, slope)
        weights = weights + step
        if float(numpy.abs(step).max()) < 1e-7:
            break
    return _Model(mean, scale, weights[:-1], float(weights[-1]))


def _rows(cases: list[_Case], full: bool) -> tuple[Any, Any]:
    """What is learned from: every candidate of a record whose right parent is known,
    and every candidate that was judged wrong."""
    found, labels = [], []
    for case in cases:
        table = case.full if full else case.light
        if table is None:
            continue
        for place, parent in enumerate(case.parents):
            if case.right or parent in case.wrong:
                found.append(table[place])
                labels.append(1.0 if parent in case.right else 0.0)
    if not found:
        return numpy.zeros((0, 0)), numpy.zeros(0)
    return numpy.stack(found), numpy.array(labels)


def limit(misses: list[float], error: float) -> float:
    """How far below certainty a candidate's share may lie before it is ruled out, so
    that a right parent is ruled out at most ``error`` of the time: the conformal
    quantile of how far the known right parents fell short."""
    if not misses:
        return 1.0
    place = math.ceil((len(misses) + 1) * (1.0 - error))
    return 1.0 if place > len(misses) else sorted(misses)[place - 1]


def kept(shares: Any, bound: float) -> list[int]:
    """The candidates that cannot be ruled out, the likeliest first."""
    order = sorted(range(len(shares)), key=lambda place: -float(shares[place]))
    return [place for place in order if 1.0 - float(shares[place]) <= bound + 1e-12]


def _miss(case: _Case, shares: Any) -> float:
    right = [float(shares[i]) for i, parent in enumerate(case.parents) if parent in case.right]
    return 1.0 - max(right) if right else 1.0


def measure(cases: list[_Case], full: bool, only: UUID | None = None) -> dict[str, Any] | None:
    """How the scorer does on records whose right parent is known, each scored by a
    model that never saw it or any other record of its Epic, and with a bound drawn
    from the other folds. It learns from every project's cases; ``only`` measures on one
    project's. None where there is too little to learn from or to measure on."""
    known = [
        c
        for c in cases
        if c.right
        and (c.full is not None if full else True)
        and (only is None or c.project == only)
    ]
    if len(known) < LEARN_FROM or len({c.fold for c in known}) < 2:
        return None
    scored: dict[int, Any] = {}
    for fold in sorted({c.fold for c in known}):
        rows, labels = _rows([c for c in cases if c.fold != fold], full)
        model = fit(rows, labels)
        if model is None:
            return None
        for place, case in enumerate(known):
            if case.fold == fold:
                scored[place] = model.shares(case.full if full else case.light)
    misses = {place: _miss(case, scored[place]) for place, case in enumerate(known)}
    first = inside = several = none = 0
    # Where one candidate is left: how sure the scorer was, and whether it was right.
    alone: list[tuple[float, bool]] = []
    for place, case in enumerate(known):
        shares = scored[place]
        others = [miss for other, miss in misses.items() if known[other].fold != case.fold]
        left = kept(shares, limit(others, ADD_ERROR))
        best = case.parents[int(numpy.argmax(shares))]
        first += best in case.right
        inside += any(case.parents[i] in case.right for i in left)
        if len(left) == 1:
            alone.append((float(shares[left[0]]), case.parents[left[0]] in case.right))
        elif left:
            several += 1
        else:
            none += 1
    total = len(known)
    sure = surely(alone, ADD_ERROR)
    acting = [right for share, right in alone if sure is not None and share >= sure]
    return {
        "children": total,
        "top1": round(first / total, 3),
        "in_set": round(inside / total, 3),
        "acted": round(len(acting) / total, 3),
        "acted_right": round(sum(acting) / len(acting), 3) if acting else None,
        "acted_count": len(acting),
        "alone": round(len(alone) / total, 3),
        "several": round(several / total, 3),
        "none": round(none / total, 3),
        # From this share of belief on, one candidate left is acted on; None: never yet.
        "sure_from": round(sure, 6) if sure is not None else None,
        "misses": [round(miss, 6) for miss in misses.values()],
        "projects": sorted({str(case.project) for case in known}),
    }


def surely(alone: list[tuple[float, bool]], error: float) -> float | None:
    """The share of belief from which a single candidate left is acted on: the lowest
    one down to which the scorer was wrong at most ``error`` of the time on the known
    links. None where even its surest cases are too few or too often wrong to show it."""
    ranked = sorted(alone, key=lambda entry: -entry[0])
    found = None
    right = 0
    for count, (share, was_right) in enumerate(ranked, start=1):
        right += was_right
        if count >= ACT_FROM and right / count >= 1.0 - error:
            found = share
    return found


def _jaccard(one: set[str], other: set[str]) -> float:
    return len(one & other) / len(one | other) if one and other else 0.0


def _fold(*family: Any) -> int:
    return int(_spread("fold", *family)[:8], 16) % FOLDS


def cases_of(db: Session, project: ProjectRecord) -> tuple[list[_Case], _Plan] | None:
    """Every record of the project a parent can be looked for, with its candidates and
    their signals. None while the ranking service has given no vectors."""
    embedding = ranking.stored_model(db, RecordVector, project.id)
    if not embedding:
        return None
    plan = _plan(db, project)
    known, _sources = _truth(db, project.id, plan)
    vectors = ranking.vectors(db, project.id, embedding)
    reranker = ranking.stored_model(db, PairScore, project.id)
    said = ranking.scores(db, project.id, reranker) if reranker else {}
    texts = plan.texts()
    meaning = _Meaning(plan, vectors)
    wrong: dict[tuple[str, UUID], set[UUID]] = defaultdict(set)
    # What the administrator judged of a batch's lines is known too. A line of a batch
    # that takes links off says the opposite of its verdict about the link.
    for line, batch_kind in db.execute(
        select(ReorgLine, ReorgBatch.kind)
        .join(ReorgBatch, ReorgBatch.id == ReorgLine.batch_id)
        .where(ReorgLine.project_id == project.id, ReorgLine.verdict.is_not(None))
    ):
        if line.link_kind not in (ISSUE_STORY, STORY_EPIC):
            continue
        children = plan.issues if line.link_kind == ISSUE_STORY else plan.stories
        parents_ = plan.stories if line.link_kind == ISSUE_STORY else plan.epics
        if line.child_id not in children or line.parent_id not in parents_:
            continue
        if (line.verdict == "right") != (batch_kind == "unsupported"):
            known[line.link_kind][line.child_id].add(line.parent_id)
        else:
            wrong[line.link_kind, line.child_id].add(line.parent_id)
    for pair in db.scalars(
        select(GoldPair).where(
            GoldPair.project_id == project.id, GoldPair.label.in_(("no", "related"))
        )
    ):
        wrong[pair.kind, pair.child_id].add(pair.parent_id)

    def reranked(kind: str, child: UUID, parents: list[UUID], kinds: tuple[str, str]) -> Any | None:
        found = []
        for parent in parents:
            score = said.get((kind, child, parent))
            both = digest_of(texts[kinds[0], child], texts[kinds[1], parent])
            if score is None or score[0] != both:
                return None
            found.append(score[1] / 10.0)
        best = max(found)
        return numpy.array([[s, s - best, 1.0 if s == best else 0.0] for s in found])

    def resemblance(mine: Any, others: list[UUID], kind: str) -> tuple[float, float]:
        """How close the record lies to the parent's other children."""
        close = [
            float(vectors[kind, other] @ mine)
            for other in sorted(others, key=str)[:SIBLINGS]
            if (kind, other) in vectors
        ]
        return (sum(close) / len(close), 1.0) if close else (0.0, 0.0)

    found: list[_Case] = []
    titles = {
        key: set(terms({"title": title, "body": ""})) for key, (title, _b) in plan.stories.items()
    }
    place_of = {key: place for place, key in enumerate(meaning.stories)}
    children_of: dict[UUID, list[UUID]] = defaultdict(list)
    for issue, links in plan.issue_links.items():
        for parent, _origin, _primary in links:
            children_of[parent].append(issue)
    for issue in sorted(plan.issues, key=str):
        mine = vectors.get(("issue", issue))
        if mine is None or meaning.matrix is None or mine.shape[0] != meaning.matrix.shape[1]:
            continue
        ranked = plan.by_words(issue)
        by_words = dict(ranked)
        word_place = {parent: place for place, (parent, _s) in enumerate(ranked)}
        top = ranked[0][1] if ranked else 0.0
        close = meaning.matrix @ mine
        order = [meaning.stories[place] for place in numpy.argsort(-close)]
        meaning_place = {parent: place for place, parent in enumerate(order)}
        right = known[ISSUE_STORY].get(issue, set())
        linked = [parent for parent, _o, _p in plan.issue_links.get(issue, [])]
        parents = _candidates([parent for parent, _s in ranked], order)
        # A link whose truth is not known is judged too, wherever it stands; so is a
        # candidate that was judged wrong. A known right parent must be found unaided.
        extra = [*([] if right else linked), *sorted(wrong[ISSUE_STORY, issue], key=str)]
        parents = [p for p in dict.fromkeys([*parents, *extra]) if p in place_of]
        if not parents:
            continue
        best = max(float(close[place_of[parent]]) for parent in parents)
        title = set(terms({"title": plan.issues[issue][0], "body": ""}))
        rows = []
        for parent in parents:
            near = float(close[place_of[parent]])
            others = [other for other in children_of.get(parent, []) if other != issue]
            alike, has = resemblance(mine, others, "issue")
            rows.append(
                [
                    by_words.get(parent, 0.0) / top if top else 0.0,
                    1.0 / (1 + word_place[parent]) if parent in word_place else 0.0,
                    near,
                    near - best,
                    1.0 / (1 + meaning_place[parent]),
                    _jaccard(title, titles.get(parent, set())),
                    alike,
                    has,
                    1.0 if word_place.get(parent, 99) < 15 and meaning_place[parent] < 15 else 0.0,
                ]
            )
        light = numpy.array(rows)
        more = reranked(ISSUE_STORY, issue, parents, ("issue", "story"))
        family = next((plan.primary_epic(parent) for parent in sorted(right, key=str)), None)
        found.append(
            _Case(
                project.id,
                ISSUE_STORY,
                issue,
                parents,
                light,
                numpy.hstack([light, more]) if more is not None else None,
                set(right),
                set(wrong[ISSUE_STORY, issue]),
                linked,
                _fold(family or issue),
                family=family,
            )
        )
    epic_titles = {
        key: set(terms({"title": title, "body": ""})) for key, (title, _b) in plan.epics.items()
    }
    stories_of: dict[UUID, list[UUID]] = defaultdict(list)
    for story, story_links in plan.story_links.items():
        for parent, *_rest in story_links:
            stories_of[parent].append(story)
    for story in sorted(plan.story_links, key=str):
        parents = sorted(plan.own_epics(story), key=str)
        mine = vectors.get(("story", story))
        if len(parents) < 2 or mine is None or any(("epic", p) not in vectors for p in parents):
            continue
        ranked = plan.epics_by_words(story)
        by_words = dict(ranked)
        word_place = {parent: place for place, (parent, _s) in enumerate(ranked)}
        top = ranked[0][1] if ranked else 0.0
        close_to = {parent: float(vectors["epic", parent] @ mine) for parent in parents}
        order = sorted(parents, key=lambda parent: (-close_to[parent], str(parent)))
        best = close_to[order[0]]
        title = titles.get(story, set())
        rows = []
        for parent in parents:
            others = [other for other in stories_of.get(parent, []) if other != story]
            alike, has = resemblance(mine, others, "story")
            rows.append(
                [
                    by_words.get(parent, 0.0) / top if top else 0.0,
                    1.0 / (1 + word_place[parent]) if parent in word_place else 0.0,
                    close_to[parent],
                    close_to[parent] - best,
                    1.0 / (1 + order.index(parent)),
                    _jaccard(title, epic_titles.get(parent, set())),
                    alike,
                    has,
                    len(stories_of.get(parent, [])) / max(1, len(plan.stories)),
                ]
            )
        light = numpy.array(rows)
        more = reranked(STORY_EPIC, story, parents, ("story", "epic"))
        right = known[STORY_EPIC].get(story, set())
        found.append(
            _Case(
                project.id,
                STORY_EPIC,
                story,
                parents,
                light,
                numpy.hstack([light, more]) if more is not None else None,
                set(right),
                set(wrong[STORY_EPIC, story]),
                parents,
                _fold(next(iter(sorted(right, key=str)), story)),
                plan.primary_epic(story),
                next(iter(sorted(right, key=str)), None),
            )
        )
    return found, plan


def _removal(db: Session, project: ProjectRecord, plan: _Plan) -> list[dict[str, Any]]:
    """A first count of records that one reason speaks against. One reason is never
    enough to propose a removal: this only says where the clean-up will look."""
    embedding = ranking.stored_model(db, RecordVector, project.id)
    vectors = ranking.vectors(db, project.id, embedding) if embedding else {}

    def same(kind: str, keys: list[UUID]) -> int:
        have = [key for key in sorted(keys, key=str) if (kind, key) in vectors]
        if len(have) < 2:
            return 0
        table = numpy.stack([vectors[kind, key] for key in have])
        close = table @ table.T
        numpy.fill_diagonal(close, 0.0)
        return int((close.max(axis=1) >= SAME_TEXT).sum())

    primary: Counter[UUID] = Counter()
    for story_links in plan.story_links.values():
        for parent, _origin, is_primary, _by in story_links:
            if is_primary:
                primary[parent] += 1
    state = {
        key: (issue_state, (facts or {}).get("github_state_reason"))
        for key, issue_state, facts in db.execute(
            select(BacklogItem.id, BacklogItem.issue_state, BacklogItem.facts).where(
                BacklogItem.project_id == project.id
            )
        )
    }
    under: dict[UUID, list[UUID]] = defaultdict(list)
    for issue, links in plan.issue_links.items():
        for parent, _origin, _primary in links:
            under[parent].append(issue)
    dropped = sum(
        1
        for issues in under.values()
        if all(state.get(issue) == ("closed", "not_planned") for issue in issues)
    )
    return [
        {
            "kind": "issue",
            "reason": "Says nearly the same as another issue",
            "count": same("issue", list(plan.issues)),
        },
        {
            "kind": "story",
            "reason": "Says nearly the same as another User Story",
            "count": same("story", list(plan.stories)),
        },
        {
            "kind": "story",
            "reason": "Every issue under it was closed as not planned",
            "count": dropped,
        },
        {
            "kind": "epic",
            "reason": "No User Story has it as its primary Epic",
            "count": sum(1 for epic in plan.epics if not primary[epic]),
        },
    ]


class Scorer:
    def __init__(self, sessions: sessionmaker[Session], links: PlanningLinks) -> None:
        self.sessions = sessions
        self.links = links
        self.computed_at: datetime | None = None
        # What is worked out from the scorer's answers, done again whenever they are.
        self.after: Callable[[], Any] | None = None

    # ------------------------------------------------------------- computing

    def _projects(self) -> list[str]:
        with self.sessions() as db:
            return list(
                db.scalars(
                    select(ProjectRecord.name)
                    .where(ProjectRecord.archived_at.is_(None))
                    .order_by(ProjectRecord.name)
                )
            )

    def _state(self) -> str:
        """A mark that changes whenever something the scorer reads has changed."""
        with self.sessions() as db:
            tables: tuple[Any, ...] = (
                PlanningLink,
                GoldPair,
                RecordVector,
                PairScore,
                BacklogItem,
                ProjectUserStory,
                ProjectEpic,
                ReorgLine,
                DocFile,
            )
            parts = [
                tuple(db.execute(select(func.count(), func.max(table.updated_at))).one())
                for table in tables
            ]
            return hashlib.sha256(repr(parts).encode()).hexdigest()

    def refresh(self, *, force: bool = False) -> int:
        """Compute the scorer again where what it reads has changed: learn from every
        project's known links, measure, and say of each record what it would do. Returns
        how many records it said something of. Nothing but its own tables is written."""
        state = self._state()
        names_ = self._projects()
        with self.sessions() as db:
            have = {
                project: mark
                for project, mark in db.execute(select(ScorerReport.project_id, ScorerReport.state))
            }
        with self.sessions() as db:
            current = {active_project(db, name).id for name in names_ if self._exists(db, name)}
        if not force and all(have.get(project) == state for project in current):
            return 0
        bundles: dict[str, tuple[UUID, list[_Case], _Plan]] = {}
        for name in names_:
            try:
                self.links.reconcile_project(name)
                with self.sessions() as db:
                    project = active_project(db, name)
                    built = cases_of(db, project)
                    if built is not None:
                        bundles[name] = (project.id, built[0], built[1])
            except ProjectNotFound:
                continue
        every = [case for _id, cases, _plan_ in bundles.values() for case in cases]
        for kind in (ISSUE_STORY, STORY_EPIC):
            # With fewer Epics than folds, folding by Epic would leave folds empty or
            # put everything in one: the records are then folded one by one.
            of_kind = [case for case in every if case.kind == kind]
            if len({case.family for case in of_kind if case.right}) < FOLDS:
                for case in of_kind:
                    case.fold = _fold(case.child)
        written = 0
        learned: dict[str, dict[str, Any]] = {}
        for kind in (ISSUE_STORY, STORY_EPIC):
            of_kind = [case for case in every if case.kind == kind]
            learned[kind] = {}
            for key, full in (("light", False), ("full", True)):
                rows, labels = _rows(of_kind, full)
                model = fit(rows, labels) if len(rows) else None
                learned[kind][key] = (model, measure(of_kind, full) if model else None)
        for name in names_:
            # Computed while only reading, so that nobody waits for it; then written at once.
            with self.sessions() as db:
                if not self._exists(db, name):
                    continue
                project = active_project(db, name)
                pid = project.id
                report, decisions = self._report(db, project, bundles.get(name), every, learned)
            with self.sessions.begin() as db:
                db.execute(delete(ScorerDecision).where(ScorerDecision.project_id == pid))
                db.add_all(decisions)
                row = db.scalar(select(ScorerReport).where(ScorerReport.project_id == pid))
                if row is None:
                    row = ScorerReport(project_id=pid)
                    db.add(row)
                row.state, row.report = state, report
                row.computed_at = datetime.now(UTC)
                written += len(decisions)
        self.computed_at = datetime.now(UTC)
        if self.after is not None:
            self.after()
        return written

    @staticmethod
    def _exists(db: Session, name: str) -> bool:
        try:
            active_project(db, name)
        except ProjectNotFound:
            return False
        return True

    def _report(
        self,
        db: Session,
        project: ProjectRecord,
        bundle: tuple[UUID, list[_Case], _Plan] | None,
        every: list[_Case],
        learned: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], list[ScorerDecision]]:
        if bundle is None:
            return {
                "ready": False,
                "why": "The ranking service has given no vectors for this project yet.",
            }, []
        _id, cases, plan = bundle
        decisions: list[ScorerDecision] = []
        kinds = []
        for kind in (ISSUE_STORY, STORY_EPIC):
            mine = [case for case in cases if case.kind == kind]
            pooled = [case for case in every if case.kind == kind]
            own_known = sum(1 for case in mine if case.right)
            # Measured on this project's own known links where there are enough of them.
            own = own_known >= OWN_FROM
            measured = []
            local: dict[str, dict[str, Any] | None] = {}
            for key, full in (("light", False), ("full", True)):
                # Where the project's own links are too few to measure on, or cannot be
                # split, the measurement on all projects stands in.
                local[key] = (measure(pooled, full, project.id) if own else None) or learned[kind][
                    key
                ][1]
                found = local[key] if learned[kind][key][0] else None
                if found is not None:
                    measured.append(
                        {"key": key, **{k: v for k, v in found.items() if k != "misses"}}
                    )
            entry: dict[str, Any] = {
                "kind": kind,
                "known_here": own_known,
                "known_everywhere": sum(1 for case in pooled if case.right),
                "judged_wrong": sum(len(case.wrong) for case in mine),
                "measured_here": False,
                "measured": measured,
                "would": {},
                "weights": {},
                "ready": False,
            }
            entry["measured_here"] = own and local["light"] is not learned[kind]["light"][1]
            kinds.append(entry)
            light_model, light_measure = learned[kind]["light"]
            full_model, full_measure = learned[kind]["full"]
            if light_model is None or light_measure is None:
                entry["why"] = (
                    "Too few links are known to be right, in all projects together, to learn "
                    "from. Judging the pairs on this page gives it what it needs."
                )
                continue
            entry["ready"] = True
            # The reranker's signals are used where they were measured to help.
            use_full = (
                full_model is not None
                and full_measure is not None
                and (
                    full_measure["top1"] > light_measure["top1"]
                    or (
                        full_measure["top1"] == light_measure["top1"]
                        and full_measure["acted"] > light_measure["acted"]
                    )
                )
            )
            entry["uses"] = "full" if use_full else "light"
            bounds = {}
            for key in ("light", "full"):
                found = local[key]
                if found is not None:
                    misses = found["misses"]
                    bounds[key] = (
                        limit(misses, ADD_ERROR),
                        limit(misses, REMOVE_ERROR),
                        found.get("sure_from"),
                    )
            entry["weights"] = {
                name: round(float(weight), 3)
                for name, weight in zip(
                    names(kind, use_full),
                    (full_model if use_full and full_model else light_model).weights,
                    strict=True,
                )
            }
            would: Counter[str] = Counter()
            for case in mine:
                if case.right:
                    would["known"] += 1
                    continue
                full = use_full and case.full is not None and full_model is not None
                model = full_model if full and full_model else light_model
                table = case.full if full else case.light
                add_bound, remove_bound, sure = bounds["full" if full else "light"]
                shares = model.shares(table)
                left = kept(shares, add_bound)
                plausible = {case.parents[i] for i in kept(shares, remove_bound)}
                first = [
                    [str(case.parents[i]), round(float(shares[i]), 4)]
                    for i in sorted(range(len(shares)), key=lambda i: -float(shares[i]))[:5]
                ]
                signal_names = names(kind, full)

                def said(
                    parent: UUID | None,
                    outcome: str,
                    *,
                    case: _Case = case,
                    shares: Any = shares,
                    table: Any = table,
                    signal_names: tuple[str, ...] = signal_names,
                    first: list[list[Any]] = first,
                    full: bool = full,
                    kind: str = kind,
                ) -> ScorerDecision:
                    place = case.parents.index(parent) if parent in case.parents else None
                    return ScorerDecision(
                        project_id=project.id,
                        kind=kind,
                        child_id=case.child,
                        parent_id=parent,
                        outcome=outcome,
                        confidence=round(float(shares[place]), 4) if place is not None else 0.0,
                        model="full" if full else "light",
                        detail={
                            "first": first,
                            "of": len(case.parents),
                            "signals": {
                                name: round(float(value), 3)
                                for name, value in zip(signal_names, table[place], strict=True)
                            }
                            if place is not None
                            else {},
                            "agrees": parent == case.suggested if kind == STORY_EPIC else None,
                        },
                    )

                best = case.parents[left[0]] if left else case.parents[int(numpy.argmax(shares))]
                # One candidate left is acted on only where the scorer is sure enough.
                acts_on = (
                    case.parents[left[0]]
                    if len(left) == 1 and sure is not None and float(shares[left[0]]) >= sure
                    else None
                )
                if kind == ISSUE_STORY and case.linked:
                    for parent in case.linked:
                        if parent not in case.parents:
                            continue
                        outcome = (
                            "unsupported"
                            if parent not in plausible
                            else "confirmed"
                            if acts_on == parent
                            else "kept"
                        )
                        would[outcome] += 1
                        decisions.append(said(parent, outcome))
                    continue
                acts = "place" if kind == ISSUE_STORY else "primary"
                outcome = (
                    acts
                    if acts_on is not None
                    else "unsure"
                    if len(left) == 1
                    else "several"
                    if left
                    else "none"
                )
                would[outcome] += 1
                decisions.append(said(best, outcome))
            entry["would"] = dict(would)
            abstained = would["unsure"] + would["several"] + would["none"]
            entry["to_decide"] = abstained
            entry["tokens_likely"] = abstained * TOKENS_A_QUESTION
        return {
            "ready": any(entry["ready"] for entry in kinds),
            "kinds": kinds,
            "errors": {"add": ADD_ERROR, "remove": REMOVE_ERROR},
            "removal": _removal(db, project, plan),
        }, decisions

    # --------------------------------------------------------------- reading

    def uses_reranker(self, project: UUID, kind: str) -> bool:
        """Whether the reranker's score was measured to help for that kind of link, so
        that it is worth asking for every record of the project."""
        with self.sessions() as db:
            row = db.scalar(select(ScorerReport).where(ScorerReport.project_id == project))
            if row is None:
                return False
            return any(
                entry.get("kind") == kind and entry.get("uses") == "full"
                for entry in (row.report or {}).get("kinds", [])
            )

    def report(self, name: str) -> dict[str, Any]:
        """What the scorer learned, how it measures, and what it would do. Shadow mode:
        it has done none of it."""
        with self.sessions() as db:
            project = active_project(db, name)
            row = db.scalar(select(ScorerReport).where(ScorerReport.project_id == project.id))
            if row is None:
                return {"ready": False, "why": "The scorer has not been computed yet."}
            return {
                **row.report,
                "computed_at": row.computed_at.isoformat() if row.computed_at else None,
                "signals": SIGNALS,
                "outcomes": OUTCOMES,
            }

    def decisions(
        self, name: str, kind: str, outcome: str, page: int, page_size: int
    ) -> dict[str, Any]:
        """What the scorer would do with each record, the surest first."""
        with self.sessions() as db:
            project = active_project(db, name)
            scope = [ScorerDecision.project_id == project.id]
            if kind:
                scope.append(ScorerDecision.kind == kind)
            if outcome:
                scope.append(ScorerDecision.outcome == outcome)
            total = int(
                db.scalar(select(func.count()).select_from(ScorerDecision).where(*scope)) or 0
            )
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(max(1, page), pages)
            rows = db.scalars(
                select(ScorerDecision)
                .where(*scope)
                .order_by(ScorerDecision.confidence.desc(), ScorerDecision.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            issues = {
                key: (title, number)
                for key, title, number in db.execute(
                    select(BacklogItem.id, BacklogItem.title, BacklogItem.issue_number).where(
                        BacklogItem.id.in_({row.child_id for row in rows})
                    )
                )
            }
            wanted = {row.child_id for row in rows} | {
                row.parent_id for row in rows if row.parent_id
            }
            stories: dict[UUID, str] = dict(
                db.execute(
                    select(ProjectUserStory.id, ProjectUserStory.title).where(
                        ProjectUserStory.id.in_(wanted)
                    )
                )
                .tuples()
                .all()
            )
            epics: dict[UUID, str] = dict(
                db.execute(
                    select(ProjectEpic.id, ProjectEpic.title).where(ProjectEpic.id.in_(wanted))
                )
                .tuples()
                .all()
            )

            def child(row: ScorerDecision) -> dict[str, Any]:
                if row.kind == ISSUE_STORY:
                    title, number = issues.get(row.child_id, ("", None))
                    return {
                        "kind": "issues",
                        "id": str(row.child_id),
                        "title": f"#{number} {title}" if number else title,
                    }
                return {
                    "kind": "stories",
                    "id": str(row.child_id),
                    "title": stories.get(row.child_id, ""),
                }

            def parent(row: ScorerDecision) -> dict[str, Any] | None:
                if row.parent_id is None:
                    return None
                titles = stories if row.kind == ISSUE_STORY else epics
                return {
                    "kind": "stories" if row.kind == ISSUE_STORY else "epics",
                    "id": str(row.parent_id),
                    "title": titles.get(row.parent_id, ""),
                }

            return {
                "items": [
                    {
                        "kind": row.kind,
                        "outcome": row.outcome,
                        "says": OUTCOMES.get(row.outcome, row.outcome),
                        "child": child(row),
                        "parent": parent(row),
                        "confidence": row.confidence,
                        "model": row.model,
                        "signals": (row.detail or {}).get("signals", {}),
                        "candidates": len((row.detail or {}).get("first", [])),
                        "agrees": (row.detail or {}).get("agrees"),
                    }
                    for row in rows
                ],
                "total": total,
                "page": page,
                "pages": pages,
            }


async def worker(scorer: Scorer) -> None:
    """Keep the scorer in step with what it reads: looked at every two minutes, computed
    again at most every five."""
    await asyncio.sleep(120)
    while True:
        try:
            rested = scorer.computed_at is None or (
                (datetime.now(UTC) - scorer.computed_at).total_seconds() >= REST_SECONDS
            )
            if rested:
                await asyncio.to_thread(scorer.refresh)
        except Exception:
            log.exception("Computing the scorer failed")
        await asyncio.sleep(120)
