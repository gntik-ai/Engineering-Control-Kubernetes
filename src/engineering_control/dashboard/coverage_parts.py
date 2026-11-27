"""Coverage: the parts of a project's documentation as they are kept, and the records a
part may be covered by (O4 of docs/design/organize-and-coverage.md).

A part is what stands under one heading of a document (``doc_parts``). Here it is kept:
its text, what a rule says it is about, what the administrator says it is about where
they said so, and what was found of it. Its key is made from the document's path and
the part's own text, so a part that did not change keeps what was found, wherever its
lines moved to, and a part that changed is a new one.

The records a part may be covered by are the Epics, User Stories and Issues of the
project and of the projects connected to it, closed ones too: what is delivered is
covered. They are offered to a part by the words they share with it and, where the
ranking service read both, by meaning.

No model is asked here.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import NAMESPACE_OID, UUID, uuid5

import numpy
from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer, sessionmaker

from engineering_control.dashboard import doc_parts, organize, ranking
from engineering_control.dashboard.issue_types import bugs_with_optional_stories
from engineering_control.dashboard.project_records import active_project
from engineering_control.dashboard.review_graph import terms
from engineering_control.db.models import (
    BacklogItem,
    DocFile,
    DocPart,
    ProjectEpic,
    ProjectRecord,
    ProjectUserStory,
    RecordVector,
)

log = logging.getLogger(__name__)

# How much of a part's text is kept, and how many parts one request to the ranking
# service reads.
KEPT: Final = 6000
BATCH: Final = 16
# How many records a part is offered.
OFFERED: Final = 8
RECORD_TEXT: Final = 500
FRESH: Final = 300.0
STATES: Final = ("unchecked", "covered", "none", "doubt", "other")
_RECORDS: dict[str, tuple[float, Any, Records]] = {}

Reader = Callable[[list[str]], tuple[str, list[bytes]]]


def part_key(project: UUID, path: str, digest: str, nth: int) -> UUID:
    """The same for the same text in the same document, wherever its lines are."""
    return uuid5(NAMESPACE_OID, f"part:{project}:{path}:{digest}:{nth}")


def digest_of(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def effective(about: str, said: str | None, state: str) -> str:
    """What a part is about, all told: the administrator's word first, then the rule,
    then what the models found. ``open`` where nobody said yet."""
    if said in ("product", "other"):
        return said
    if about in ("product", "other"):
        return about
    if state == "other":
        return "other"
    return "product" if state in ("covered", "none", "doubt") else "open"


def sync(db: Session, project: ProjectRecord) -> dict[str, int]:
    """Bring the kept parts in step with the documents as they were last read. A part
    that is no longer there is marked as gone and kept: records may have been made for
    it. A part of an OpenSpec change that is named after an Issue of the plan is covered
    by that Issue: the change says so itself."""
    known = {
        row.id: row
        for row in db.scalars(
            select(DocPart)
            .where(DocPart.project_id == project.id)
            .options(defer(DocPart.vector), defer(DocPart.text))
        )
    }
    bugs = set(db.scalars(bugs_with_optional_stories(db, project.id)))
    numbers = {
        number: (key, title)
        for key, number, title in db.execute(
            select(BacklogItem.id, BacklogItem.issue_number, BacklogItem.title).where(
                BacklogItem.project_id == project.id,
                BacklogItem.issue_number.is_not(None),
                BacklogItem.workspace_review.in_(("pending", "publishing", "approved")),
            )
        )
        if key not in bugs
    }
    seen: set[UUID] = set()
    added = 0
    now = datetime.now(UTC)
    for path, text, sections, facts in db.execute(
        select(DocFile.path, DocFile.text, DocFile.sections, DocFile.facts)
        .where(
            DocFile.project_id == project.id,
            DocFile.removed_scan_id.is_(None),
            DocFile.text.is_not(None),
        )
        .order_by(DocFile.path)
        .execution_options(yield_per=50)
    ):
        lines = text.splitlines()
        change = (facts or {}).get("openspec") or {}
        stated = numbers.get(change.get("issue")) if not change.get("archived") else None
        met: Counter[str] = Counter()
        for part in doc_parts.cut(path, text, sections or []):
            body = "\n".join(lines[part.first_line - 1 : part.last_line])
            digest = digest_of(body)
            key = part_key(project.id, path, digest, met[digest])
            met[digest] += 1
            seen.add(key)
            row = known.get(key)
            if row is None:
                row = DocPart(
                    id=key,
                    project_id=project.id,
                    path=path,
                    digest=digest,
                    text=body[:KEPT],
                    about=part.about,
                    state="unchecked",
                    covered_by=[],
                )
                db.add(row)
                known[key] = row
                added += 1
            if row.heading != part.heading[:300]:
                row.heading = part.heading[:300]
            if list(row.under or []) != list(part.under):
                row.under = list(part.under)
            if (row.first_line, row.last_line, row.words) != (
                part.first_line,
                part.last_line,
                part.words,
            ):
                row.first_line, row.last_line, row.words = (
                    part.first_line,
                    part.last_line,
                    part.words,
                )
            if row.about != part.about:
                row.about = part.about
            if row.gone_at is not None:
                row.gone_at = None
            by_statement = bool(row.covered_by) and all(
                entry.get("how") == "stated" for entry in row.covered_by
            )
            if stated is not None and part.about == "product":
                if row.state == "unchecked" or by_statement:
                    wanted = [
                        {
                            "kind": "issues",
                            "id": str(stated[0]),
                            "title": stated[1],
                            "ref": f"#{change.get('issue')}",
                            "how": "stated",
                        }
                    ]
                    if row.state != "covered" or row.covered_by != wanted:
                        row.state, row.covered_by = "covered", wanted
            elif by_statement:
                # The change no longer names an Issue of the plan.
                row.state, row.covered_by = "unchecked", []
    gone = 0
    for key, row in known.items():
        if key not in seen and row.gone_at is None:
            row.gone_at = now
            gone += 1
    db.flush()
    return {"parts": len(seen), "added": added, "gone": gone}


def embed(
    sessions: sessionmaker[Session], name: str, reader: Reader | None, most: int = 10 * BATCH
) -> int:
    """Have the ranking service read parts it has not read, some at a time. Returns how
    many are left to read; none where there is no ranking service or the project's
    records were not read by it, since then there is nothing to compare a part with."""
    if reader is None:
        return 0
    with sessions() as db:
        project = active_project(db, name)
        model = ranking.stored_model(db, RecordVector, project.id)
        if not model:
            return 0
        unread = (
            DocPart.project_id == project.id,
            DocPart.gone_at.is_(None),
            DocPart.vector.is_(None) | (DocPart.model != model),
        )
        left = int(db.scalar(select(func.count()).select_from(DocPart).where(*unread)) or 0)
        rows = db.execute(
            select(DocPart.id, DocPart.path, DocPart.heading, DocPart.text)
            .where(*unread)
            .order_by(DocPart.path, DocPart.first_line)
            .limit(most)
        ).all()
    read = 0
    for start in range(0, len(rows), BATCH):
        chunk = rows[start : start + BATCH]
        try:
            said, blobs = reader(
                [ranking.text_of(f"{path}: {heading}", text) for _key, path, heading, text in chunk]
            )
        except Exception:  # noqa: BLE001 - the ranking service is not there: words alone
            log.warning("Coverage could not have parts of %s read", name, exc_info=True)
            return 0
        if said != model:
            # The service reads with another model than the records were read with.
            return 0
        with sessions.begin() as db:
            for (key, _path, _heading, _text), blob in zip(chunk, blobs, strict=True):
                row = db.get(DocPart, key)
                if row is not None:
                    row.model, row.vector = model, blob
        read += len(chunk)
    return max(0, left - read)


@dataclass(frozen=True)
class Rec:
    """One record a part may be covered by."""

    id: str
    kind: str
    title: str
    text: str
    # The project it belongs to, where that is a connected one.
    project: str | None
    ref: str | None
    closed: bool
    digest: str


class Records:
    """The Epics, User Stories and Issues of a project and of the projects connected to
    it, as a part of the documentation is compared with them."""

    def __init__(self, db: Session, project: ProjectRecord) -> None:
        self.rows: dict[str, Rec] = {}
        vectors: dict[str, Any] = {}
        model = ranking.stored_model(db, RecordVector, project.id)
        self.model = model
        for key in organize.scope(db, project.id):
            owner = db.get(ProjectRecord, key)
            if owner is None:
                continue
            label = None if key == project.id else owner.name
            for kind, table in (("epics", ProjectEpic), ("stories", ProjectUserStory)):
                for one, title, body, closed in db.execute(
                    select(table.id, table.title, table.description, table.closed_at).where(
                        table.project_id == key, table.archived_at.is_(None)
                    )
                ):
                    self._add(one, kind, title, body, label, None, closed is not None)
            bugs = set(db.scalars(bugs_with_optional_stories(db, key)))
            for item in db.execute(
                select(
                    BacklogItem.id,
                    BacklogItem.title,
                    BacklogItem.description,
                    BacklogItem.issue_number,
                    BacklogItem.issue_state,
                    BacklogItem.facts,
                ).where(
                    BacklogItem.project_id == key,
                    BacklogItem.workspace_review.in_(("pending", "publishing", "approved")),
                )
            ):
                one, title, body, number, state, facts = item
                # A bug is no part of the plan, and what was not planned covers nothing.
                if one in bugs or (facts or {}).get("github_state_reason") == "not_planned":
                    continue
                ref = f"#{number}" if number else None
                self._add(one, "issues", title, body, label, ref, state == "closed")
            if model and ranking.stored_model(db, RecordVector, key) == model:
                for (what, one), vector in ranking.vectors(db, key, model).items():
                    if str(one) in self.rows and what in ("epic", "story", "issue"):
                        vectors[str(one)] = vector
        self.terms: dict[str, list[str]] = defaultdict(list)
        for key_, row in self.rows.items():
            for term in terms({"title": row.title, "body": row.text}):
                self.terms[term].append(key_)
        self.keys = sorted(vectors)
        self.table = numpy.stack([vectors[key_] for key_ in self.keys]) if self.keys else None

    def _add(
        self,
        key: UUID,
        kind: str,
        title: str,
        body: str | None,
        project: str | None,
        ref: str | None,
        closed: bool,
    ) -> None:
        text = " ".join((body or "").split())[: 4 * RECORD_TEXT]
        self.rows[str(key)] = Rec(
            str(key), kind, title, text, project, ref, closed, digest_of(f"{title}\n{text}")
        )

    def nearest(
        self, title: str, text: str, vector: Any | None, most: int = OFFERED
    ) -> list[tuple[Rec, float | None, int]]:
        """The records nearest to a text, by shared words and by meaning joined: each
        with how close it lies and what it shares."""
        shared: Counter[str] = Counter()
        rare = max(15, len(self.rows) // 4)
        for term in sorted(terms({"title": title, "body": text})):
            matches = self.terms.get(term, [])
            if matches and len(matches) <= rare:
                for key in matches:
                    shared[key] += max(1, 100 // len(matches))
        by_words = sorted(shared, key=lambda key: (-shared[key], key))
        close: dict[str, float] = {}
        if vector is not None and self.table is not None and self.table.shape[1] == vector.shape[0]:
            close = dict(
                zip(self.keys, (float(value) for value in self.table @ vector), strict=True)
            )
        by_meaning = sorted(close, key=lambda key: (-close[key], key))
        joined: dict[str, float] = defaultdict(float)
        for ranking_ in (by_words, by_meaning):
            for place, key in enumerate(ranking_[:30]):
                joined[key] += 1 / (60 + place)
        best = sorted(joined, key=lambda key: (-joined[key], key))[:most]
        return [(self.rows[key], close.get(key), int(shared.get(key, 0))) for key in best]


def _mark(db: Session, projects: list[UUID]) -> tuple[Any, ...]:
    found: list[Any] = []
    for model in (ProjectEpic, ProjectUserStory, BacklogItem, RecordVector):
        count, last = db.execute(
            select(func.count(), func.max(model.updated_at)).where(model.project_id.in_(projects))
        ).one()
        found.append((int(count), str(last)))
    return tuple(found)


def records(db: Session, project: ProjectRecord) -> Records:
    """The records of the scope, read once and kept until one of them changes."""
    scope = organize.scope(db, project.id)
    at = f"{id(db.get_bind())}:{project.id}"
    mark, now = _mark(db, scope), time.monotonic()
    kept = _RECORDS.get(at)
    if kept and kept[1] == mark and now - kept[0] < FRESH:
        return kept[2]
    found = Records(db, project)
    _RECORDS[at] = (now, mark, found)
    return found


def vector_of(blob: bytes | None) -> Any | None:
    return numpy.frombuffer(blob, dtype=numpy.float32) if blob else None
