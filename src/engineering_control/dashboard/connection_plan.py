"""The plan of a connection: what it asks of two projects, and how that will be checked
(OC of docs/design/organize-and-coverage.md).

When two projects are connected, or a connected project changes, a plan is prepared
before any model is asked. It has two halves.

- **The requirements to update.** What of each project the connection touches: the
  records of one that name the other or one of its issues, the records that lie close by
  meaning to a record of the other, the same thing recorded in both, and the parts of
  each project's documentation that name the other. A record that nothing ties to the
  other project is not put before a model.
- **How it will be verified.** Which stages check what, by which models of which makers,
  what is applied and what is only shown, and the most the run can cost.

Nothing here asks a model: the plan costs nothing, and the same records always give the
same plan. The records of the two projects are read together by ``Across``, which the
run uses as well, to offer each record its candidates from the other project.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import numpy
from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.dashboard import doc_parts, ranking, review_settings
from engineering_control.dashboard.connection_records import KINDS, Catalog, Item
from engineering_control.dashboard.workspace_costs import AccountPrices
from engineering_control.db.models import (
    ConnectionFinding,
    DocFile,
    ProjectConnection,
    ProjectRecord,
    RecordVector,
)
from engineering_control.models.providers import family_of, maker

# How many records of each kind of the other project a record is offered.
OPTIONS: Final = 3
# From how close by meaning a record counts as touched by the connection, and from how
# close two records of the same kind count as the same thing recorded twice.
CLOSE: Final = 0.6
# The closeness a record has to reach is learned from the links of the connection that
# were approved, once there are this many: it is set so that this share of them would
# be missed, and never below the value above.
LEARN_FROM: Final = 30
MISSED: Final = 0.05
TWIN: Final = 0.9
# How many examples of a group the plan names.
SHOWN: Final = 8
# For the estimate: the records one question holds, what a model writes for a record at
# most, what a question carries beside its records, and the characters of a token.
BATCH: Final = 6
WRITTEN: Final = 350
OVERHEAD: Final = 450
CHARACTERS: Final = 4
WORD: Final = re.compile(r"[\w-]{3,}", re.UNICODE)
# The kinds of the ranking service's vectors that hold a record of each kind.
VECTORS: Final = {"epics": ("epic",), "stories": ("story",), "issues": ("issue", "bug")}
SAID: Final = {"epics": "Epic", "stories": "User Story", "issues": "Issue"}


def words(item: Item) -> set[str]:
    return set(WORD.findall((item.title + " " + item.body[:900]).casefold()))


def _names(project: ProjectRecord) -> set[str]:
    """What a text calls the project: its name, its repository and what it is shown as."""
    found = {
        project.name,
        project.source_repository,
        project.source_repository.split("/")[-1],
        project.display_name or "",
    }
    return {name.casefold().strip() for name in found if len(name.strip()) >= 3}


def _naming(names: set[str]) -> re.Pattern[str] | None:
    """A text names the project when one of its names stands by itself: "falcone" is not
    named by "falcone-serverless"."""
    if not names:
        return None
    listed = "|".join(re.escape(name) for name in sorted(names, key=lambda n: (-len(n), n)))
    return re.compile(rf"(?<![\w-])(?:{listed})(?![\w-])", re.I)


class Across:
    """The approved records of two connected projects, and for each record of one what
    is nearest to it in the other: by the words they share, and by meaning where the
    ranking service has read both with the same model."""

    def __init__(
        self,
        sides: list[Catalog],
        kept: list[dict[tuple[str, UUID], Any]] | None = None,
    ) -> None:
        self.sides = sides
        self.items = [
            {item.key: item for item in side.local.values() if item.approved} for side in sides
        ]
        self.terms: list[dict[str, list[str]]] = []
        self.by_kind: list[dict[str, list[str]]] = []
        for items in self.items:
            terms: dict[str, list[str]] = defaultdict(list)
            kinds: dict[str, list[str]] = defaultdict(list)
            for key, item in sorted(items.items()):
                kinds[item.kind].append(key)
                for term in words(item):
                    terms[term].append(key)
            self.terms.append(terms)
            self.by_kind.append(kinds)
        self.naming = [_naming(_names(side.project)) for side in sides]
        # The vectors of each side's records, and of each kind stacked for a product.
        self.vector: list[dict[str, Any]] = [{}, {}]
        self.stacked: list[dict[str, tuple[list[str], Any]]] = [{}, {}]
        for place, vectors in enumerate(kept or []):
            for key, item in self.items[place].items():
                if item.id is None:
                    continue
                found = next(
                    (
                        vectors[kind, item.id]
                        for kind in VECTORS[item.kind]
                        if (kind, item.id) in vectors
                    ),
                    None,
                )
                if found is not None:
                    self.vector[place][key] = found
            for kind in KINDS:
                keys = [key for key in self.by_kind[place][kind] if key in self.vector[place]]
                if keys:
                    self.stacked[place][kind] = (
                        keys,
                        numpy.stack([self.vector[place][key] for key in keys]),
                    )
        self.meaning = all(self.vector)
        # From how close by meaning a record counts as touched, and from how many
        # approved links that was learned.
        self.close = CLOSE
        self.learned = 0
        # What was worked out once for a record is kept while these records are read.
        self._close: dict[tuple[int, str, str], dict[str, float]] = {}

    @classmethod
    def read(cls, db: Session, sides: list[Catalog]) -> Across:
        """With the vectors the ranking service kept, where both projects were read with
        the same model; else by words alone."""
        models = [ranking.stored_model(db, RecordVector, side.project.id) for side in sides]
        if models[0] and models[0] == models[1]:
            return cls(sides, [ranking.vectors(db, side.project.id, models[0]) for side in sides])
        return cls(sides)

    def _closeness(self, place: int, item: Item, kind: str) -> dict[str, float]:
        """How close each record of that kind on the other side is to this one."""
        kept = self._close.get((place, item.key, kind))
        if kept is not None:
            return kept
        mine = self.vector[place].get(item.key)
        stacked = self.stacked[1 - place].get(kind)
        found: dict[str, float] = {}
        if mine is not None and stacked is not None and stacked[1].shape[1] == mine.shape[0]:
            keys, matrix = stacked
            found = dict(zip(keys, (float(value) for value in matrix @ mine), strict=True))
        if len(self._close) < 20000:
            self._close[place, item.key, kind] = found
        return found

    def _shared(self, place: int, item: Item) -> Counter[str]:
        """The records of the other side by the rare words they share with this one."""
        there, scores = self.terms[1 - place], Counter[str]()
        most = max(15, len(self.items[1 - place]) // 4)
        for term in sorted(words(item)):
            matches = there.get(term, [])
            if matches and len(matches) <= most:
                for key in matches:
                    scores[key] += max(1, 100 // len(matches))
        return scores

    def ranked(self, place: int, item: Item, kind: str) -> list[tuple[Item, float | None, int]]:
        """The records of that kind of the other project, the nearest first, by shared
        words and by meaning joined: each with how close it lies and what it shares."""
        other = self.items[1 - place]
        shared = self._shared(place, item)
        by_words = sorted(
            (key for key in shared if other[key].kind == kind), key=lambda k: (-shared[k], k)
        )
        close = self._closeness(place, item, kind)
        by_meaning = sorted(close, key=lambda k: (-close[k], k))
        joined: dict[str, float] = defaultdict(float)
        for ranking_ in (by_words, by_meaning):
            for position, key in enumerate(ranking_[:30]):
                joined[key] += 1 / (60 + position)
        return [
            (other[key], close.get(key), int(shared.get(key, 0)))
            for key in sorted(joined, key=lambda k: (-joined[k], k))
        ]

    def near(self, place: int, item: Item) -> list[Item]:
        """What the record is offered of the other project: of each kind the first few,
        by shared words and by meaning joined. Where neither says anything, the first of
        the kind, so that a record that is missing there can still be given parents."""
        other = self.items[1 - place]
        found: list[Item] = []
        for kind in KINDS:
            ranked = [entry[0] for entry in self.ranked(place, item, kind)]
            group = [other[key] for key in self.by_kind[1 - place][kind]]
            found.extend((ranked or group)[:OPTIONS])
        return found

    def closest(self, place: int, item: Item) -> tuple[float, Item] | None:
        """The record of the other project nearest to this one by meaning."""
        best: tuple[float, Item] | None = None
        for kind in KINDS:
            for key, value in self._closeness(place, item, kind).items():
                if best is None or value > best[0]:
                    best = (value, self.items[1 - place][key])
        return best

    def names(self, place: int, item: Item) -> bool:
        """Whether the record's own text names the other project."""
        pattern = self.naming[1 - place]
        return bool(pattern and pattern.search(f"{item.title}\n{item.body}\n{item.original}"))

    def touched(self, place: int, item: Item) -> str | None:
        """Why the connection touches the record, or None where nothing ties it to the
        other project: it ``names`` it, lies ``close`` to one of its records, or was not
        read by the ranking service yet (``unread``), which is no reason to leave it out.
        Without the ranking service every record is put before a model, as before."""
        if self.names(place, item):
            return "names"
        if not self.meaning:
            return "words"
        if item.key not in self.vector[place]:
            return "unread"
        best = self.closest(place, item)
        return "close" if best is not None and best[0] >= self.close else None

    def learn(self, pairs: list[tuple[int, str, str]]) -> list[float]:
        """Set how close a record has to lie from the pairs that are known to be linked:
        each a source, by its side, and what it is linked to on the other. How close a
        pair lies depends on the model that read the texts, so a fixed value says
        little; the approved links of the connection say what "close" is here. Returns
        how close each known pair lies."""
        found = []
        for place, source, target in pairs:
            one, two = self.vector[place].get(source), self.vector[1 - place].get(target)
            if one is not None and two is not None and one.shape == two.shape:
                found.append(float(one @ two))
        if len(found) >= LEARN_FROM:
            self.close = min(0.95, max(CLOSE, float(numpy.quantile(found, MISSED))))
            self.learned = len(found)
        return found

    def twins(self) -> list[tuple[float, Item, Item]]:
        """The pairs of the same kind that lie so close that they may be the same thing
        recorded in both projects, the closest first."""
        found: list[tuple[float, Item, Item]] = []
        for kind in KINDS:
            one, two = self.stacked[0].get(kind), self.stacked[1].get(kind)
            if one is None or two is None or one[1].shape[1] != two[1].shape[1]:
                continue
            product = one[1] @ two[1].T
            for row, column in zip(*numpy.nonzero(product >= TWIN), strict=True):
                found.append(
                    (
                        float(product[row, column]),
                        self.items[0][one[0][int(row)]],
                        self.items[1][two[0][int(column)]],
                    )
                )
        return sorted(found, key=lambda pair: (-pair[0], pair[1].key, pair[2].key))


def calibrate(db: Session, c: ProjectConnection, across: Across) -> list[float]:
    """Teach the pair of projects what "close" is from the links of the connection that
    were approved."""
    places = {side.project.id: place for place, side in enumerate(across.sides)}
    pairs = [
        (places[project], str(source), str(target))
        for project, source, target in db.execute(
            select(
                ConnectionFinding.source_project,
                ConnectionFinding.source_id,
                ConnectionFinding.target_id,
            ).where(
                ConnectionFinding.connection_id == c.id,
                ConnectionFinding.action == "link",
                ConnectionFinding.status == "approved",
                ConnectionFinding.source_id.is_not(None),
                ConnectionFinding.target_id.is_not(None),
            )
        )
        if project in places
    ]
    return across.learn(pairs)


def _spread(values: list[float]) -> dict[str, float] | None:
    """Where the values lie: the value 5, 25, 50, 75 and 95 of a hundred lie below."""
    if not values:
        return None
    return {
        str(share): round(float(numpy.quantile(values, share / 100)), 3)
        for share in (5, 25, 50, 75, 95)
    }


def _example(project: ProjectRecord, item: Item) -> dict[str, Any]:
    return {
        "project": project.name,
        "kind": item.kind,
        "id": str(item.id) if item.id else None,
        "number": item.number,
        "title": item.title[:200],
    }


def _documents(db: Session, sides: list[Catalog], across: Across) -> dict[str, Any]:
    """The parts of each project's documentation that name the other project."""
    by_project: dict[str, int] = {}
    examples: list[dict[str, Any]] = []
    for place, side in enumerate(sides):
        pattern = across.naming[1 - place]
        count = 0
        for path, text, sections in db.execute(
            select(DocFile.path, DocFile.text, DocFile.sections)
            .where(
                DocFile.project_id == side.project.id,
                DocFile.removed_scan_id.is_(None),
                DocFile.text.is_not(None),
            )
            .order_by(DocFile.path)
            .execution_options(yield_per=50)
        ):
            if pattern is None or not pattern.search(text):
                continue
            lines = text.splitlines()
            for part in doc_parts.cut(path, text, sections or []):
                if part.about == "other":
                    continue
                if pattern.search("\n".join(lines[part.first_line - 1 : part.last_line])):
                    count += 1
                    if len(examples) < SHOWN:
                        examples.append(
                            {
                                "project": side.project.name,
                                "kind": "documents",
                                "ref": part.ref,
                                "title": f"{path}: {part.heading}"[:200],
                            }
                        )
        by_project[side.project.name] = count
    return {"count": sum(by_project.values()), "by_project": by_project, "examples": examples}


def answering(db: Session, c: ProjectConnection) -> tuple[str, str] | None:
    """The model that answers for the connection: the one chosen on its page, else the
    one the owning project's settings name for answers."""
    if c.provider and c.model:
        return (c.provider, c.model)
    return review_settings.of(db, c.owner_id).answer


def refused(db: Session, projects: list[ProjectRecord], named: tuple[str, str]) -> str | None:
    """Why the model may not be asked about these two projects: a question that holds
    text of both goes only to a maker both allow."""
    for project in projects:
        if not review_settings.permits(db, project.id, *named):
            return (
                f"{named[1]} is of a maker ({maker(*named)}) that the texts of "
                f"{project.display_name} may not be sent to (Reviews, Settings)."
            )
    return None


def judging(
    db: Session, c: ProjectConnection, projects: list[ProjectRecord], answer: tuple[str, str]
) -> tuple[tuple[str, str] | None, str | None]:
    """The judge of the connection's run, and where there is none, why: the owning
    project's judge, of another maker than the answer, and allowed by both projects."""
    judge = review_settings.judge_of(db, c.owner_id, answer)
    if judge is None:
        named = review_settings.of(db, c.owner_id).judge
        if named and family_of(*named) == family_of(*answer):
            return None, "The judge set in Settings is of the same maker as the model that answers."
        return None, "No judge is set under Reviews, Settings."
    why = refused(db, projects, judge)
    return (None, why) if why else (judge, None)


def _price(prices: AccountPrices, named: tuple[str, str], read: int, written: int) -> float | None:
    entry = prices.tariff(None, named[0], named[1])
    if not entry:
        return None
    try:
        return round((read * float(entry["input"]) + written * float(entry["output"])) / 1e6, 2)
    except (KeyError, TypeError, ValueError):
        return None


def prepare(
    db: Session,
    c: ProjectConnection,
    projects: list[ProjectRecord],
    sides: list[Catalog],
    across: Across,
    asked: dict[str, str],
    stamp: Callable[[Item, list[Item]], str],
    mark: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The plan of the connection as its records stand. ``asked`` are the records a model
    was asked about already, each with the digest of what it was asked, which ``stamp``
    gives for a record and what it is offered: a record is asked about again only when
    it, or one of the records it is offered, changed."""
    by_name = {project.id: project for project in projects}
    linked = calibrate(db, c, across)
    nearest = [
        best[0]
        for place in range(2)
        for item in across.items[place].values()
        if (best := across.closest(place, item)) is not None
    ]
    findings = list(
        db.scalars(select(ConnectionFinding).where(ConnectionFinding.connection_id == c.id))
    )
    waiting = [f for f in findings if f.status == "pending"]
    stated = [f for f in waiting if f.origin == "reference"]
    earlier = [f for f in waiting if f.origin == "model"]
    # A record an earlier suggestion still waits for is asked about again, so that a
    # second model answers too.
    held = {f.source_id for f in earlier if f.source_id}
    groups: dict[str, dict[str, Any]] = {
        key: {"count": 0, "by_project": {p.name: 0 for p in projects}, "examples": []}
        for key in ("names", "close", "unread", "words", "untouched")
    }
    ask: list[tuple[int, Item, list[Item]]] = []
    already = 0
    for place, side in enumerate(sides):
        for _, item in sorted(across.items[place].items()):
            why = across.touched(place, item) or ("close" if item.id in held else None)
            group = groups[why or "untouched"]
            group["count"] += 1
            group["by_project"][side.project.name] += 1
            if len(group["examples"]) < SHOWN and why in ("names", "close"):
                group["examples"].append(_example(side.project, item))
            if why:
                near = across.near(place, item)
                if asked.get(item.key) == stamp(item, near):
                    already += 1
                else:
                    ask.append((place, item, near))
    twins = across.twins()
    # How well the rule finds what is known: of the links approved so far, how many
    # have a source the rule would put before a model, and how many of those are
    # offered the record they are linked to.
    known = selected = offered = 0
    places = {side.project.id: place for place, side in enumerate(sides)}
    for f in findings:
        if f.status != "approved" or f.action != "link" or not f.source_id or not f.target_id:
            continue
        where = places.get(f.source_project)
        if where is None:
            continue
        source = across.items[where].get(str(f.source_id))
        if source is None:
            continue
        place = where
        known += 1
        if across.touched(place, source):
            selected += 1
            offered += any(option.id == f.target_id for option in across.near(place, source))
    read = sum(
        len(json.dumps(item.summary())) + sum(len(json.dumps(n.summary())) for n in near)
        for _, item, near in ask
    )
    questions = -(-len(ask) // BATCH)
    tokens_in = read // CHARACTERS + questions * OVERHEAD
    tokens_out = len(ask) * WRITTEN
    answer = answering(db, c)
    blocked = None
    if not all(across.items):
        empty = next(
            side.project for side, items in zip(sides, across.items, strict=True) if not items
        )
        blocked = f"{empty.display_name} has no approved Epics, User Stories or Issues yet."
    elif answer is None:
        blocked = "Choose the model that answers, here or under Reviews, Settings."
    else:
        blocked = refused(db, projects, answer)
    judge, no_judge = judging(db, c, projects, answer) if answer else (None, None)
    prices = AccountPrices(db)
    answer_cost = _price(prices, answer, tokens_in, tokens_out) if answer else None
    judge_cost = _price(prices, judge, tokens_in, tokens_out) if judge else None
    dollars = (
        None
        if answer is None or answer_cost is None or (judge and judge_cost is None)
        else round(answer_cost + (judge_cost or 0.0), 2)
    )
    limit = review_settings.of(db, c.owner_id).dollars
    spent = review_settings.spent(db, c.owner_id)[0]
    owner = by_name[c.owner_id]
    dependent = by_name.get(c.dependent_id) if c.dependent_id else None
    update = [
        {
            "key": "stated",
            "title": "Issues one project names in the other's repository",
            "expected": "A link across the two projects. It is stated in the text.",
            "by": "rule",
            "count": len(stated),
            "by_project": {
                p.name: sum(1 for f in stated if f.source_project == p.id) for p in projects
            },
            "examples": [
                {
                    "project": by_name[f.source_project].name,
                    "kind": f.source_kind,
                    "id": str(f.source_id) if f.source_id else None,
                    "number": f.source_number,
                    "title": str((f.evidence or {}).get("source_title") or "")[:200],
                }
                for f in stated[:SHOWN]
            ],
        },
        {
            "key": "names",
            "title": "Records whose text names the other project",
            "expected": "A link, a text to compare, or something that is missing there.",
            "by": "models",
            **groups["names"],
        },
        {
            "key": "close",
            "title": "Records that lie close by meaning to a record of the other project",
            "expected": "A link, a text to compare, or something that is missing there.",
            "by": "models",
            **groups["close"],
        },
        {
            "key": "twins",
            "title": "The same thing recorded in both projects",
            "expected": "One of the two is linked to the other, or one text is corrected.",
            "by": "models",
            "count": len(twins),
            "by_project": {},
            "examples": [
                {
                    "project": sides[0].project.name,
                    "kind": one.kind,
                    "id": str(one.id) if one.id else None,
                    "number": one.number,
                    "title": f"{one.title[:80]} ({sides[0].project.name}) = "
                    f"{two.title[:80]} ({sides[1].project.name})",
                }
                for _, one, two in twins[:SHOWN]
            ],
        },
        {
            "key": "earlier",
            "title": "Suggestions of an earlier analysis that still wait",
            "expected": "Asked about again, so that a second model answers too.",
            "by": "models",
            "count": len(earlier),
            "by_project": {
                p.name: sum(1 for f in earlier if f.source_project == p.id) for p in projects
            },
            "examples": [],
        },
        {
            "key": "documents",
            "title": "Parts of the documentation that name the other project",
            "expected": "Listed here. They are checked against the records by Coverage, "
            "not by this run.",
            "by": "coverage",
            **_documents(db, sides, across),
        },
        {
            "key": "untouched",
            "title": "Records that nothing ties to the other project",
            "expected": "Left as they are. No model is asked about them.",
            "by": "nobody",
            **groups["untouched"],
        },
    ]
    for key, title in (
        ("unread", "Records the ranking service has not read yet"),
        ("words", "Records compared by shared words alone"),
    ):
        if groups[key]["count"]:
            update.insert(
                3,
                {
                    "key": key,
                    "title": title,
                    "expected": "Put before the models, since nothing rules them out.",
                    "by": "models",
                    **groups[key],
                },
            )

    def said(named: tuple[str, str]) -> str:
        return f"{named[1]} ({maker(*named)})"

    verify = [
        {
            "step": "Stated links",
            "says": (
                "1 reference of one project to an issue of the other is a link by what the "
                "text says. It is recorded without asking a model, and can be undone."
                if len(stated) == 1
                else f"{len(stated)} references of one project to an issue of the other "
                "are links by what the text says. They are recorded without asking a "
                "model, and each can be undone."
            ),
        },
        {
            "step": "Candidates",
            "says": f"Each record is offered {OPTIONS} records of each kind of the other "
            "project: "
            + (
                "by the words they share and by meaning, from the ranking service. "
                + (
                    f"A record counts as close to the other project from a closeness of "
                    f"{across.close:.2f}, learned from the {across.learned} links approved "
                    "so far: 95 of 100 of them lie at least that close."
                    if across.learned
                    else f"A record counts as close to the other project from a closeness "
                    f"of {across.close:.2f}, the value used until {LEARN_FROM} links of "
                    "this connection are approved to learn from."
                )
                if across.meaning
                else "by the words they share. The ranking service has not read both "
                "projects with the same model, so closeness by meaning is not used and "
                "every record is put before the models."
            ),
        },
        {
            "step": "Answer",
            "says": (
                f"{said(answer)} is asked about {len(ask)} records"
                + (
                    f" ({already} more were asked about before and did not change)"
                    if already
                    else ""
                )
                + f", {BATCH} to a question: "
                "which of the offered records each is linked to, whether a text has to be "
                "corrected, and what is missing in the other project. Every suggestion "
                "has to give its reason."
                if answer
                else "No model is chosen to answer."
            ),
        },
        {
            "step": "Review",
            "says": (
                f"{said(judge)}, of another maker, is asked the same without seeing the "
                "first answer. A suggestion both make stands; one only the first makes is "
                "in doubt."
                if judge
                else f"{no_judge} Without a second answer every suggestion is in doubt "
                "and waits for you."
            ),
        },
        {
            "step": "What is applied",
            "says": "A link both models make is recorded and listed under Done. A changed "
            "text and a record that is missing are never applied: they are listed under "
            "Needs you, with the text before and after. Nothing is closed and nothing is "
            "deleted. What you accept can be undone for 30 days.",
        },
        {
            "step": "Limits",
            "says": f"The run is charged to {owner.display_name} and stops at its limit of "
            f"{limit:.2f} US dollars a month, of which {spent:.2f} are spent. It can be "
            "stopped at any time and keeps what it has.",
        },
    ]
    return {
        "prepared_at": (now or datetime.now(UTC)).isoformat(),
        "mark": mark,
        "relation": (
            f"{dependent.display_name} depends on "
            f"{next(p.display_name for p in projects if p.id != dependent.id)}"
            if dependent
            else " and ".join(p.display_name for p in projects) + " are related"
        ),
        "purpose": c.purpose,
        "projects": [
            {
                "name": side.project.name,
                "label": side.project.display_name,
                "records": {kind: len(across.by_kind[place][kind]) for kind in KINDS},
                "read": len(across.vector[place]),
            }
            for place, side in enumerate(sides)
        ],
        "meaning": across.meaning,
        "update": update,
        "verify": verify,
        "measured": {
            "links": known,
            "selected": selected,
            "offered": offered,
            # From how close a record counts as touched, how many approved links that
            # was learned from, how close the linked pairs lie, and how close every
            # record lies to its nearest in the other project.
            "threshold": round(across.close, 3),
            "learned_from": across.learned,
            "linked": _spread(linked),
            "nearest": _spread(nearest),
        },
        "ask": {
            "records": len(ask),
            # Touched records a model was asked about already, as they stand.
            "already": already,
            "questions": questions,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "answer": {"provider": answer[0], "model": answer[1], "dollars": answer_cost}
            if answer
            else None,
            "judge": {"provider": judge[0], "model": judge[1], "dollars": judge_cost}
            if judge
            else None,
            "no_judge": no_judge,
            "dollars": dollars,
            "limit": limit,
            "spent": round(spent, 2),
            "over": dollars is not None and spent + dollars > limit,
        },
        "stated": len(stated),
        "blocked": blocked,
    }
