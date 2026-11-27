"""Organize: the stage that creates (O3 of docs/design/organize-and-coverage.md).

When a run of Organize has linked what it could, this stage goes on with what has no
counterpart. Behind "User Stories and Issues":

- a User Story whose Issues are all closed as done, with the proof the records hold, is
  put before the administrator to be closed. No Issue is made for finished work;
- Issues that two models found to fit no User Story are grouped, and the User Story
  they are the work of is written and made, under the Epic it belongs to;
- a User Story with no Issue gets its Issues written. They are kept here and reach
  GitHub together, at one word of the administrator. Where its work is done already, or
  nothing says what would have to be done, it is the administrator's to close.

Behind "Epics and User Stories":

- an Epic with no open User Story gets its Stories written, or is the administrator's
  to close or explain;
- an Epic that holds too many Stories is proposed to be split. Nothing is split without
  the administrator.

A record is made without the administrator only after the careful review: one model
wrote it and named, for every sentence, the records it rests on; a second model of
another maker found every sentence supported by them; and nothing like it exists in
the project or the projects connected to it. Otherwise what was written is a case under
"Needs you". Every record made is a line under "Created", names what it was made from,
and can be taken away again for 30 days. A record taken away counts as wrong: where
more than the share the settings accept was, nothing more is made without the
administrator.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import numpy
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import (
    organize,
    parent_choice,
    ranking,
    review_settings,
    structure,
)
from engineering_control.dashboard.delivery import Delivery
from engineering_control.dashboard.organize_writing import (
    NEW_ISSUES,
    NEW_STORIES,
    NEW_STORY,
    Subject,
)
from engineering_control.dashboard.planning import PlanningRecords, save_record
from engineering_control.dashboard.planning_links import Note, PlanningLinks
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.reorg import Reorg
from engineering_control.dashboard.structure import Structure
from engineering_control.dashboard.workspace_costs import AccountPrices
from engineering_control.dashboard.workspace_publishing import approve_issue
from engineering_control.db.models import (
    BacklogItem,
    ChoiceReview,
    ChoiceRun,
    OrganizeLine,
    OrganizeRun,
    PlanningReview,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectUserStory,
    RecordVector,
    StoryDelivery,
    StructureDecision,
)

log = logging.getLogger(__name__)

LIVE: Final = ("running", "held")
# What the stage does, in order, behind each button. A name that is a check asks a
# model; the others read the records.
STEPS: Final = {
    "stories_issues": ("delivered", NEW_STORY, NEW_ISSUES),
    "epics_stories": (NEW_STORIES, "splits"),
}
CHECKS: Final = (NEW_STORY, NEW_ISSUES, NEW_STORIES)
# The lines of "Needs you" this stage writes, by what the administrator can say to them.
OPS: Final = ("make", "retire", "close_story", "split", "created")
# What each check writes, and under what.
WRITES: Final = {NEW_STORY: "stories", NEW_ISSUES: "issues", NEW_STORIES: "stories"}
ABOUT: Final = {NEW_STORY: "issues", NEW_ISSUES: "stories", NEW_STORIES: "epics"}
# From how close by meaning a record is a twin of one that exists.
TWIN: Final = 0.9
# How doubtful a case is, the most doubtful first.
DOUBT: Final = {
    "draft_unsupported": 0.8,
    "draft_twin": 0.75,
    "draft_no_epic": 0.7,
    "draft_unclear": 0.7,
    "draft_unverified": 0.65,
    "unclear": 0.65,
    "draft_single": 0.6,
    "draft_unchecked": 0.6,
    "draft_stopped": 0.5,
    "nothing_doubt": 0.6,
    "done_doubt": 0.6,
    "nothing": 0.5,
    "done": 0.45,
    "split": 0.4,
    "delivered": 0.3,
}
SAID: Final = {
    "draft_unsupported": "The second model found a sentence that the records do not support.",
    "draft_twin": "A record like it exists already.",
    "draft_no_epic": "No Epic was found to fit it.",
    "draft_unclear": "The second model could not tell whether the records support it.",
    "draft_unverified": "The search for a record like it could not be made.",
    "draft_single": "No second model of another maker is set: nothing is made without you.",
    "draft_unchecked": "A sentence named a record the model was not shown, and was left out.",
    "draft_stopped": "Records that were made were taken away again: nothing more is made "
    "without you until that share is within what the settings accept.",
}
GROUPS: Final = {
    "delivered": (("delivered",), ("close", "keep")),
    "drafts": (("draft_stopped", "draft_single"), ("make",)),
    # Records nothing says anything about are kept open together, never closed so.
    "nothing": (("nothing", "nothing_doubt", "unclear"), ("keep",)),
}
CHUNK: Final = 20
FRESH: Final = 300.0
_PLANS: dict[str, tuple[float, Any, dict[str, Any]]] = {}

Embed = Callable[[list[str]], tuple[str, list[bytes]]]


def _norm(title: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in title.casefold()).split())


def stands(review: ChoiceReview | None, current: str) -> str:
    """Where what was written about a record stands: not asked yet, decided by the
    administrator, or how far the careful review went."""
    if review is None or review.state != "answered" or review.revision != current:
        return "unasked"
    if review.decision != "pending":
        return "decided"
    if review.answer == "unclear":
        return "unclear"
    if review.agreed is None:
        return "single"
    if review.agreed is False:
        return "unsupported" if (review.judge or {}).get("answer") == "unsupported" else "unsure"
    return "agreed" if review.checked else "unchecked"


class _Twins:
    """The search for a record like one that is to be made, over the project and the
    projects connected to it: by its title, and by meaning where the ranking service
    read them. What one pass made is searched too."""

    def __init__(self, db: Session, project: ProjectRecord, embed: Embed | None) -> None:
        self.db, self.project, self.embed = db, project, embed
        self.model = ranking.stored_model(db, RecordVector, project.id)
        self.scope = [
            row
            for row in (db.get(ProjectRecord, key) for key in organize.scope(db, project.id))
            if row is not None
        ]
        self._titles: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._vectors: dict[str, tuple[list[dict[str, Any]], Any]] = {}
        self.made: dict[str, list[tuple[dict[str, Any], Any]]] = {"stories": [], "issues": []}

    def _records(self, kind: str) -> list[dict[str, Any]]:
        found = []
        for row in self.scope:
            if kind == "stories":
                rows = self.db.execute(
                    select(ProjectUserStory.id, ProjectUserStory.title).where(
                        ProjectUserStory.project_id == row.id,
                        ProjectUserStory.archived_at.is_(None),
                        ProjectUserStory.closed_at.is_(None),
                    )
                )
            else:
                rows = self.db.execute(
                    select(BacklogItem.id, BacklogItem.title).where(
                        BacklogItem.project_id == row.id,
                        BacklogItem.workspace_review.in_(("pending", "publishing", "approved")),
                    )
                )
            found += [
                {
                    "id": str(key),
                    "kind": kind,
                    "title": title,
                    **({} if row.id == self.project.id else {"project": row.name}),
                }
                for key, title in rows
            ]
        return found

    def _by_title(self, kind: str) -> dict[str, list[dict[str, Any]]]:
        if kind not in self._titles:
            found: dict[str, list[dict[str, Any]]] = {}
            for entry in self._records(kind):
                found.setdefault(_norm(entry["title"]), []).append(entry)
            self._titles[kind] = found
        return self._titles[kind]

    def _by_meaning(self, kind: str) -> tuple[list[dict[str, Any]], Any]:
        if kind not in self._vectors:
            known = {entry["id"]: entry for entry in self._records(kind)}
            side = "story" if kind == "stories" else "issue"
            entries, rows = [], []
            for row in self.scope:
                if ranking.stored_model(self.db, RecordVector, row.id) != self.model:
                    continue
                for (what, key), vector in ranking.vectors(
                    self.db, row.id, str(self.model)
                ).items():
                    if what == side and str(key) in known:
                        entries.append(known[str(key)])
                        rows.append(vector)
            self._vectors[kind] = (entries, numpy.stack(rows) if rows else None)
        return self._vectors[kind]

    def search(self, kind: str, title: str, text: str) -> tuple[list[dict[str, Any]], str, Any]:
        """The records like this one, how they were searched ("meaning", "words" or
        "failed"), and the record's own vector where it was read."""
        found = [dict(entry) for entry in self._by_title(kind).get(_norm(title), [])]
        found += [
            dict(entry)
            for entry, _vector in self.made[kind]
            if _norm(entry["title"]) == _norm(title)
        ]
        if self.embed is None or not self.model:
            return found[:3], "words", None
        try:
            model, blobs = self.embed([ranking.text_of(title, text)])
            mine = numpy.frombuffer(blobs[0], dtype=numpy.float32)
        except Exception:  # noqa: BLE001 - the ranking service is not there: it is said
            log.warning("The search for twins could not read a new record", exc_info=True)
            return found[:3], "failed", None
        if model != self.model:
            return found[:3], "words", None
        entries, table = self._by_meaning(kind)
        seen = {entry["id"] for entry in found}
        if table is not None and table.shape[1] == mine.shape[0]:
            close = table @ mine
            for place in sorted(range(len(entries)), key=lambda at: -float(close[at])):
                if float(close[place]) < TWIN:
                    break
                if entries[place]["id"] not in seen:
                    found.append({**entries[place], "closeness": round(float(close[place]), 3)})
        for entry, vector in self.made[kind]:
            if vector is not None and vector.shape == mine.shape and entry["id"] not in seen:
                close_to = float(vector @ mine)
                if close_to >= TWIN:
                    found.append({**entry, "closeness": round(close_to, 3)})
        return found[:3], "meaning", mine


class Creator:
    """The Create stage of a run of Organize."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        planning: PlanningRecords,
        links: PlanningLinks,
        changes: Reorg,
        shape: Structure,
        delivery: Delivery,
    ) -> None:
        self.sessions = sessions
        self.planning, self.links, self.changes = planning, links, changes
        self.shape, self.delivery = shape, delivery
        # How a text is read by the ranking service, where there is one.
        self.embed: Embed | None = None

    # -- who writes, and what it would cost ---------------------------------------------

    @staticmethod
    def roles(db: Session, project: UUID) -> dict[str, tuple[str, str] | None]:
        """The model that writes (the one set to write, else the one that answers) and
        the one that checks what it wrote: the judge, else the model that answers,
        whichever is of another maker than the writer."""
        mine, offered = review_settings.of(db, project), review_settings.models(db, project)

        def usable(role: tuple[str, str] | None) -> tuple[str, str] | None:
            return role if role and {"provider": role[0], "model": role[1]} in offered else None

        writer = usable(mine.writer) or usable(mine.answer)
        checker = None
        if writer is not None:
            for named in (usable(mine.judge), usable(mine.answer)):
                if named and _family(named) != _family(writer):
                    checker = named
                    break
        return {"writer": writer, "checker": checker}

    @staticmethod
    def creating(db: Session, project: UUID) -> dict[str, Any]:
        """Whether records are made without the administrator: not where more of those
        made were taken away again than the settings accept."""
        counted = {"applied": 0, "undone": 0}
        for state in db.scalars(
            select(OrganizeLine.state).where(
                OrganizeLine.project_id == project, OrganizeLine.listed == "created"
            )
        ):
            if state in counted:
                counted[state] += 1
        made = counted["applied"] + counted["undone"]
        accepted = review_settings.of(db, project).share("add_error")
        over = bool(counted["undone"]) and counted["undone"] / made > accepted
        return {
            "in_force": not over,
            "made": made,
            "undone": counted["undone"],
            "accepted": round(accepted * 100, 1),
            "why": (
                f"{counted['undone']} of the {made} records that were made were taken away "
                f"again, more than the {accepted:.0%} the settings accept."
                if over
                else None
            ),
        }

    @staticmethod
    def _waiting(
        db: Session, project: ProjectRecord, check: str
    ) -> list[tuple[Subject, list[dict[str, Any]], ChoiceReview | None, str]]:
        """Every record the check is about, with what was written of it."""
        scope: Any = parent_choice.KINDS[check].scope(db, project)
        known = parent_choice.reviews(db, project.id, check)
        found = []
        for child in scope.children():
            listed = scope.shortlist(child)
            found.append((child, listed, known.get(child.id), str(scope.revision(child, listed))))
        return found

    @staticmethod
    def _mark(db: Session, project: UUID) -> tuple[Any, ...]:
        """What the plan depends on, read cheaply: how many records of each kind there
        are and when one last changed."""

        def stamp(model: Any) -> tuple[int, str]:
            count, last = db.execute(
                select(func.count(), func.max(model.updated_at)).where(model.project_id == project)
            ).one()
            return int(count), str(last)

        links = db.scalar(
            select(func.count())
            .select_from(ProjectIssueStory)
            .where(ProjectIssueStory.project_id == project)
        )
        return (
            *(
                stamp(model)
                for model in (
                    ChoiceReview,
                    StoryDelivery,
                    ProjectUserStory,
                    ProjectEpic,
                    BacklogItem,
                    OrganizeLine,
                    StructureDecision,
                )
            ),
            int(links or 0),
        )

    def _heavy(self, db: Session, project: ProjectRecord, kind: str) -> dict[str, Any]:
        """What takes reading every record: how many each check is about, how many it
        would ask about and how long those questions are. Kept until a record changes."""
        at = f"{id(self.sessions)}:{project.id}:{kind}"
        mark, now = self._mark(db, project.id), time.monotonic()
        kept = _PLANS.get(at)
        if kept and kept[1] == mark and now - kept[0] < FRESH:
            return kept[2]
        stand_in = ChoiceRun(provider="", model="")
        checks: dict[str, dict[str, Any]] = {}
        steps = STEPS.get(kind, ())
        for check in (step for step in steps if step in CHECKS):
            rows = self._waiting(db, project, check)
            ask = [row for row in rows if stands(row[2], row[3]) == "unasked"]
            scope: Any = parent_choice.KINDS[check].scope(db, project)
            read = 0
            for child, listed, _review, _current in ask:
                asked = scope.question(stand_in, child, listed)
                read += (len(asked.system) + len(asked.prompt)) * 2 // 5
            entry: dict[str, Any] = {
                "records": len(rows),
                "to_ask": len(ask),
                "read": read,
                "written": 900 * len(ask),
            }
            if check == NEW_ISSUES:
                entry["by_state"] = {
                    state: sum(1 for row in rows if row[0].extra.get("state") == state)
                    for state in ("planned", "leads", "unsupported")
                }
            if check == NEW_STORY:
                entry["issues"] = sum(len(row[0].members) for row in rows)
            checks[check] = entry
        found: dict[str, Any] = {
            "checks": checks,
            "delivered": len(self._delivered(db, project)) if "delivered" in steps else 0,
            "splits": len(self._splits(db, project)) if "splits" in steps else 0,
        }
        _PLANS[at] = (now, mark, found)
        return found

    def plan(self, db: Session, project: ProjectRecord, kind: str) -> dict[str, Any]:
        """What the stage would do behind that button, said before it starts: how many
        records it would write for, who writes and who checks, and what that is likely
        to cost. No model is asked for it."""
        roles = self.roles(db, project.id)
        writer, checker = roles["writer"], roles["checker"]
        prices = AccountPrices(db)
        heavy = self._heavy(db, project, kind)
        checks: dict[str, dict[str, Any]] = {}
        tokens = 0
        dollars: float | None = 0.0
        for check, entry in heavy["checks"].items():
            read, written, asks = int(entry["read"]), int(entry["written"]), int(entry["to_ask"])
            cost: float | None = 0.0
            if asks and writer is None:
                cost = None
            elif asks and writer is not None:
                first = _price(prices, writer, read, written)
                # The check reads the same records and what was written, and answers
                # with a word for each sentence.
                second = (
                    _price(prices, checker, read + written, 300 * asks)
                    if checker is not None
                    else 0.0
                )
                cost = first + second if first is not None and second is not None else None
            used = read + written + (read + written + 300 * asks if checker else 0)
            checks[check] = {
                key: value for key, value in entry.items() if key not in ("read", "written")
            } | {"tokens": used, "dollars": round(cost, 2) if cost is not None else None}
            tokens += used
            dollars = None if dollars is None or cost is None else dollars + cost
        to_ask = sum(int(entry["to_ask"]) for entry in checks.values())
        mine = review_settings.of(db, project.id)
        spent, _since = review_settings.spent(db, project.id)
        blocked = None
        if to_ask and writer is None:
            blocked = parent_choice.NO_ANSWERING_MODEL
        elif to_ask:
            blocked = review_settings.over(db, project.id)
        found: dict[str, Any] = {
            "checks": checks,
            "to_ask": to_ask,
            "writer": _shown(writer),
            "checker": _shown(checker),
            "tokens": tokens,
            "dollars": round(dollars, 2) if dollars is not None else None,
            "limit": mine.dollars,
            "spent": round(spent, 2),
            # A run that would pass the month's limit stops there with what it has.
            "would_pass": bool(dollars and spent + dollars > mine.dollars),
            "blocked": blocked,
            "creating": self.creating(db, project.id),
        }
        if kind == "stories_issues":
            found["delivered"] = heavy["delivered"]
            found["unpublished"] = len(self._unpublished(db, project.id))
        else:
            found["splits"] = heavy["splits"]
        return found

    # -- the stage ------------------------------------------------------------------

    def begin(self, db: Session, run: OrganizeRun) -> bool:
        """Hand a run over to this stage, when its linking is done. False where the
        stage has nothing to do behind that button."""
        if run.kind not in STEPS:
            return False
        run.stage, run.state = "create", "running"
        run.estimate = {
            **(run.estimate or {}),
            "choice_run": None,
            "create": {"at": 0, "begun": False, "made": 0, "needs": 0, "notes": []},
        }
        return True

    def _start(self, name: str, check: str) -> str | None:
        """Have the check ask, with the model that writes and the one that checks. A run
        of it that is asking already is taken into this one. None where it has nothing
        to ask or cannot ask, with why."""
        if check == NEW_ISSUES:
            # Where each Story stands is read anew: it says which have no Issue.
            self.delivery.refresh(name)
        with self.sessions() as db:
            project = active_project(db, name)
            under_way = db.scalar(
                select(ChoiceRun.id).where(
                    ChoiceRun.project_id == project.id,
                    ChoiceRun.kind == check,
                    ChoiceRun.state.in_(parent_choice.LIVE),
                )
            )
            roles = self.roles(db, project.id)
        if under_way:
            return str(under_way)
        writer = roles["writer"]
        if writer is None:
            raise ProjectInvalid(parent_choice.NO_ANSWERING_MODEL)
        started = parent_choice.start(
            self.sessions, name, check, writer[0], writer[1], judge=roles["checker"]
        )
        return str(started["id"])

    def advance(self, name: str, key: UUID) -> bool:
        """Carry the stage on: follow the check that is asking, do what follows from
        what it wrote when it is over, and go on to the next step. True when the run
        ended."""
        for _ in range(8):
            with self.sessions.begin() as db:
                run = db.get_one(OrganizeRun, key)
                if run.state not in LIVE or run.stage != "create":
                    return run.state not in LIVE
                est = dict(run.estimate or {})
                mine = dict(est.get("create") or {})
                steps = STEPS[run.kind]
                asking = est.get("choice_run")
                check = db.get(ChoiceRun, UUID(str(asking))) if asking else None
                if check is not None:
                    organize.count_costs(run, est, check)
                    run.estimate = est
                    if check.state in parent_choice.ASKING:
                        run.state, run.detail = "running", ""
                        return False
                    if check.state in parent_choice.LIVE:
                        run.state = "held"
                        run.detail = check.detail or "The asking is stopped."
                        return False
                    if check.state == "cancelled":
                        est["ended"] = True
                ended = bool(est.get("ended"))
                at = int(mine.get("at", 0))
                if at >= len(steps) or ended:
                    self._finish(db, run, est, mine, "stopped" if ended else "complete")
                    return True
                step = steps[at]
            if step in CHECKS and not mine.get("begun"):
                started: str | None = None
                try:
                    started = self._start(name, step)
                except (ProjectInvalid, ProjectConflict) as nothing:
                    mine["notes"] = [*mine.get("notes", []), {"step": step, "why": str(nothing)}]
                with self.sessions.begin() as db:
                    run = db.get_one(OrganizeRun, key)
                    mine["begun"] = True
                    run.estimate = {**est, "create": mine, "choice_run": started}
                if started:
                    continue
            made, needs, left = self._gather(name, key, step)
            with self.sessions.begin() as db:
                run = db.get_one(OrganizeRun, key)
                now = dict(run.estimate or {})
                mine = {
                    **dict(now.get("create") or {}),
                    "at": at + 1,
                    "begun": False,
                    "made": int(mine.get("made", 0)) + made,
                    "needs": int(mine.get("needs", 0)) + needs,
                    "left": int(mine.get("left", 0)) + left,
                }
                run.estimate = {**now, "create": mine, "choice_run": None}
                run.counts = {
                    **(run.counts or {}),
                    "create": {"records": mine["made"], "in_doubt": mine["needs"]},
                }
        return False

    @staticmethod
    def _finish(
        db: Session, run: OrganizeRun, est: dict[str, Any], mine: dict[str, Any], final: str
    ) -> None:
        said = [str(run.detail or "")] if run.detail else []
        # A check that could not ask says why; that it had nothing to ask is no news.
        quiet = {kind.nothing_to_ask for kind in parent_choice.KINDS.values()} | {
            kind.nothing_waits for kind in parent_choice.KINDS.values()
        }
        said += [
            f"Nothing was written: {note['why']}"
            for note in mine.get("notes", [])
            if note["why"] not in quiet
        ][:1]
        left = int(mine.get("left", 0))
        if left and not said:
            said.append(
                f"{left} records were not written for in this run: it was ended, or reached "
                "a limit. Start it again to go on from there."
            )
        run.state, run.stage, run.ended_at = final, "done", datetime.now(UTC)
        run.detail = " ".join(dict.fromkeys(part for part in said if part))
        run.counts = {
            **(run.counts or {}),
            "create": {
                "records": int(mine.get("made", 0)),
                "in_doubt": int(mine.get("needs", 0)),
            },
        }
        run.estimate = {**est, "create": mine, "choice_run": None}

    def _gather(self, name: str, key: UUID, step: str) -> tuple[int, int, int]:
        """What one step leaves behind: how many records it made, how many cases it put
        before the administrator, and how many records it was not asked about."""
        if step == "delivered":
            return 0, self._gather_delivered(name, key), 0
        if step == "splits":
            return 0, self._gather_splits(name, key), 0
        return self._gather_written(name, key, step)

    # -- lines ----------------------------------------------------------------------

    @staticmethod
    def _open(db: Session, project: UUID, kind: str, op: str, about: str) -> OrganizeLine | None:
        """The case that still waits about that record, from whichever run."""
        return next(
            (
                line
                for line in db.scalars(
                    select(OrganizeLine).where(
                        organize.lines_where(project, kind),
                        OrganizeLine.listed == "needs_you",
                        OrganizeLine.state == "open",
                    )
                )
                if (line.action or {}).get("op") == op
                and str((line.action or {}).get("about")) == about
            ),
            None,
        )

    def _case(self, db: Session, run: OrganizeRun, op: str, about: str, case: str) -> OrganizeLine:
        line = self._open(db, run.project_id, run.kind, op, about)
        if line is None:
            line = OrganizeLine(project_id=run.project_id, run_id=run.id)
            db.add(line)
        line.run_id, line.listed, line.state, line.case = run.id, "needs_you", "open", case
        line.answer = line.answered_at = line.undone_at = line.before = None
        line.doubt = DOUBT[case]
        return line

    @staticmethod
    def _kept(db: Session, project: UUID, kind: str, op: str) -> set[str]:
        """What the administrator said to keep as it is: not put before them again."""
        return {
            str((line.action or {}).get("about"))
            for line in db.scalars(
                select(OrganizeLine).where(
                    organize.lines_where(project, kind), OrganizeLine.answer == "keep"
                )
            )
            if (line.action or {}).get("op") == op
        }

    # -- delivered Stories ----------------------------------------------------------

    def _delivered(self, db: Session, project: ProjectRecord) -> list[dict[str, Any]]:
        """The open User Stories whose Issues are all closed as done, with the proof."""
        kept = self._kept(db, project.id, "stories_issues", "close_story")
        return [
            entry
            for entry in self.changes.ready_in(db, project, "close")
            if str(entry["child"]) not in kept
        ]

    def _gather_delivered(self, name: str, key: UUID) -> int:
        self.delivery.refresh(name)
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            run = db.get_one(OrganizeRun, key)
            found = self._delivered(db, project)
            titles = _titles(db, ProjectUserStory, [entry["child"] for entry in found])
            issues = {
                item.id: item
                for item in db.scalars(
                    select(BacklogItem).where(
                        BacklogItem.id.in_([entry["parent"] for entry in found])
                    )
                )
            }
            for entry in found:
                story = entry["child"]
                line = self._case(db, run, "close_story", str(story), "delivered")
                line.subject_kind, line.subject_id = "stories", story
                line.title = str(titles.get(story) or "")[:300]
                shown = issues.get(entry["parent"])
                line.candidates = (
                    [
                        {
                            "id": str(shown.id),
                            "kind": "issues",
                            "title": shown.title,
                            "ref": f"#{shown.issue_number}" if shown.issue_number else None,
                        }
                    ]
                    if shown is not None
                    else []
                )
                line.said = [{"stage": "read", "by": "the records", "answer": "delivered"}]
                line.reason = entry["why"]
                line.action = {
                    "op": "close_story",
                    "about": str(story),
                    "issue": str(entry["parent"]),
                }
            return len(found)

    # -- splits ---------------------------------------------------------------------

    @staticmethod
    def _splits(db: Session, project: ProjectRecord) -> list[dict[str, Any]]:
        return list(structure.survey(db, project)["splits"])

    def _gather_splits(self, name: str, key: UUID) -> int:
        self.links.reconcile_project(name)
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            run = db.get_one(OrganizeRun, key)
            found = self._splits(db, project)
            for entry in found:
                line = self._case(db, run, "split", str(entry["id"]), "split")
                line.subject_kind, line.subject_id = "epics", UUID(str(entry["epic"]["id"]))
                line.title = str(entry["epic"]["title"])[:300]
                line.candidates = [
                    {
                        "id": f"{entry['id']}:{place}",
                        "kind": "epics",
                        "title": part["title"],
                        "stays": bool(part["stays"]),
                        "stories": len(part["stories"]),
                        "shown": [story["title"] for story in part["stories"][:5]],
                    }
                    for place, part in enumerate(entry["parts"])
                ]
                line.said = [{"stage": "match", "by": "the texts", "answer": "groups"}]
                total = sum(len(part["stories"]) for part in entry["parts"])
                line.reason = (
                    f"It is the one Epic of {total} User Stories, and they fall into "
                    f"{len(entry['parts'])} groups by meaning. Each group beyond the largest "
                    "would become an Epic of its own. No model was asked."
                )
                line.action = {"op": "split", "about": str(entry["id"])}
            return len(found)

    # -- what was written -------------------------------------------------------------

    @staticmethod
    def _draft(review: ChoiceReview) -> list[dict[str, Any]]:
        """What was written, record by record: its title, its text as it would be
        saved, and every sentence with the records it names and the check's word."""
        missing = review.missing or {}
        sentences = review.evidence or []
        verdicts = (review.judge or {}).get("sentences") or {}

        def said(number: int) -> dict[str, Any]:
            sentence = sentences[number - 1]
            verdict = verdicts.get(str(number)) or {}
            return {
                "text": sentence.get("text"),
                "part": sentence.get("part"),
                "sources": [
                    {"kind": s.get("kind"), "id": s.get("id"), "title": s.get("title")}
                    for s in sentence.get("sources") or []
                ],
                "verdict": verdict.get("verdict"),
                "reason": verdict.get("reason") or None,
            }

        found = []
        for item in missing.get("items") or []:
            what = [said(number) for number in item.get("what") or []]
            done = [said(number) for number in item.get("done_when") or []]
            text = " ".join(str(entry["text"]) for entry in what)
            if done:
                text += "\n\nDone when:\n" + "\n".join(f"- {entry['text']}" for entry in done)
            found.append(
                {"title": item.get("title"), "description": text, "sentences": [*what, *done]}
            )
        return found

    @staticmethod
    def _sources(review: ChoiceReview, item: dict[str, Any]) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for sentence in item["sentences"]:
            for source in sentence["sources"]:
                seen.setdefault(str(source["id"]), source)
        return list(seen.values())

    @staticmethod
    def _spoke(review: ChoiceReview) -> list[dict[str, Any]]:
        said: list[dict[str, Any]] = [
            {
                "stage": "create",
                "by": review.model or "a model",
                "answer": review.answer,
                "reason": review.reason,
            }
        ]
        judge = review.judge or {}
        if judge:
            said.append(
                {
                    "stage": "create",
                    "by": str(judge.get("model") or "a second model"),
                    "answer": judge.get("answer"),
                    "reason": judge.get("reason") or None,
                }
            )
        return said

    def _gather_written(self, name: str, key: UUID, check: str) -> tuple[int, int, int]:
        """What follows from what a check wrote. A record that passed the careful
        review is made; everything else is a case before the administrator."""
        with self.sessions() as db:
            project = active_project(db, name)
            rows = self._waiting(db, project, check)
            creating = self.creating(db, project.id)
            twins = _Twins(db, project, self.embed)
            kind = db.get_one(OrganizeRun, key).kind
            ready: list[dict[str, Any]] = []
            left = 0
            for child, listed, review, current in rows:
                stand = stands(review, current)
                left += stand == "unasked"
                if review is None or stand in ("unasked", "decided"):
                    continue
                entry: dict[str, Any] = {
                    "child": child,
                    "review": review.id,
                    "answer": review.answer,
                    "stand": stand,
                    "said": self._spoke(review),
                    "reason": review.reason or "",
                    "named": (review.missing or {}).get("named") or [],
                    "parent": (review.missing or {}).get("parent"),
                    "epics": [e for e in listed if e["kind"] == "epic"],
                    "items": self._draft(review) if review.answer == "written" else [],
                }
                entry["case"], entry["twins"], entry["vectors"] = self._decide(
                    check, entry, creating, twins
                )
                for item, vector in zip(entry["items"], entry["vectors"], strict=False):
                    # What this pass is about to make is searched for by the next.
                    if entry["case"] == "make":
                        twins.made[WRITES[check]].append(
                            ({"id": "", "kind": WRITES[check], "title": item["title"]}, vector)
                        )
                ready.append(entry)
        made = needs = 0
        for entry in ready:
            child = entry["child"]
            if entry["case"] == "make":
                try:
                    made += len(self._make(name, key, check, entry, by="run"))
                    continue
                except (ProjectConflict, ProjectInvalid, ProjectNotFound) as problem:
                    entry["case"], entry["problem"] = "draft_unverified", str(problem)
            with self.sessions.begin() as db:
                run = db.get_one(OrganizeRun, key)
                op = "make" if entry["items"] else "retire"
                line = self._case(db, run, op, str(child.id), entry["case"])
                line.subject_kind = ABOUT[check]
                line.subject_id = child.members[0] if child.members else child.id
                line.title = str(child.title)[:300]
                line.said = entry["said"]
                line.sources = [
                    {"kind": s.kind, "id": s.id, "title": s.title, "note": s.note}
                    for s in child.sources
                ]
                line.candidates = self._offered(check, entry)
                line.reason = self._why(check, entry)
                line.action = {
                    "op": op,
                    "about": str(child.id),
                    "check": check,
                    "review": str(entry["review"]),
                    "answer": entry["answer"],
                    "items": entry["items"],
                    "members": [str(member) for member in child.members],
                    "parent": (entry["parent"] or {}).get("id"),
                    "count": len(child.members),
                }
                needs += 1
        del kind
        return made, needs, left

    def _decide(
        self, check: str, entry: dict[str, Any], creating: dict[str, Any], twins: _Twins
    ) -> tuple[str, list[dict[str, Any]], list[Any]]:
        """Which case what was written is: "make" where it passed the careful review."""
        stand, answer = entry["stand"], entry["answer"]
        if answer in ("done", "nothing"):
            return (answer if stand == "agreed" else f"{answer}_doubt"), [], []
        if answer != "written":
            return "unclear", [], []
        found: list[dict[str, Any]] = []
        vectors: list[Any] = []
        how = "words"
        for item in entry["items"]:
            like, how, vector = twins.search(WRITES[check], item["title"], item["description"])
            found += [{**twin, "of": item["title"]} for twin in like]
            vectors.append(vector)
        entry["searched"] = how
        if stand == "unsupported":
            return "draft_unsupported", found, vectors
        if found:
            return "draft_twin", found, vectors
        if check == NEW_STORY and not entry["parent"]:
            return "draft_no_epic", found, vectors
        if stand in ("unsure", "unclear"):
            return "draft_unclear", found, vectors
        if how == "failed":
            return "draft_unverified", found, vectors
        if stand == "single":
            return "draft_single", found, vectors
        if stand == "unchecked":
            return "draft_unchecked", found, vectors
        if not creating["in_force"]:
            return "draft_stopped", found, vectors
        return "make", found, vectors

    @staticmethod
    def _offered(check: str, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """What the administrator is shown beside the case: the records like it that
        exist, the Epics a new Story could go under, or the work that was named."""
        found = [{**twin, "twin": True} for twin in entry["twins"]]
        if check == NEW_STORY and entry["items"]:
            chosen = (entry["parent"] or {}).get("id")
            found += [
                {
                    "id": epic["id"],
                    "kind": "epics",
                    "title": epic["title"],
                    "chosen": epic["id"] == chosen,
                    "offered": True,
                }
                for epic in entry["epics"]
            ]
        found += [
            {"id": named["id"], "kind": named["kind"], "title": named["title"], "named": True}
            for named in entry["named"]
        ]
        return found

    @staticmethod
    def _why(check: str, entry: dict[str, Any]) -> str:
        case = entry["case"]
        if entry.get("problem"):
            return f"It could not be made: {entry['problem']}"
        if case in SAID:
            said = SAID[case]
            if case == "draft_twin":
                said += " " + "; ".join(
                    f"“{twin['title']}”"
                    + (f" of {twin['project']}" if twin.get("project") else "")
                    + (f" (as close as {twin['closeness']:.2f})" if twin.get("closeness") else "")
                    for twin in entry["twins"]
                )
            return said
        what = "User Story" if check == NEW_ISSUES else "Epic"
        doubt = "" if case in ("done", "nothing") else " The second model did not confirm it."
        if case.startswith("done"):
            said = f"The work of this {what} seems to be done already."
            return f"{said} {entry['reason']}{doubt}".strip()
        if case.startswith("nothing"):
            return (
                f"Nothing in its own text or the records near it says what this {what} "
                f"would need. {entry['reason']}{doubt}"
            ).strip()
        return f"The model could not tell what this {what} needs. {entry['reason']}".strip()

    # -- making -----------------------------------------------------------------------

    def _save(self, name: str, kind: str, title: str, text: str, parent: UUID, by: str) -> UUID:
        """One record, as a model wrote it. The review a new record otherwise gets is
        not asked for: two models have read it."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            made = save_record(db, project, kind, title, text, [parent])  # type: ignore[arg-type]
            job = db.get(PlanningReview, (project.id, kind, made))
            if job is not None:
                job.status, job.lease = "dismissed", None
                job.result = {"note": "Written and checked by a run of Organize."}
        self.links.after_change(name, kind, made)
        if by == "run":
            self.links.noted(
                name,
                kind,
                made,
                parent,
                Note(
                    "model",
                    confirmed=True,
                    reason="Made by a run of Organize: written by one model from the records "
                    "it names, and every sentence found supported by a second.",
                ),
            )
        return made

    def _make(
        self,
        name: str,
        run_key: UUID,
        check: str,
        entry: dict[str, Any],
        *,
        by: str,
        epic: UUID | None = None,
    ) -> list[UUID]:
        """Make what was written about one record, and write a line under "Created" for
        each record made."""
        child: Subject | None = entry.get("child")
        about = str(child.id) if child is not None else str(entry["about"])
        members = (
            list(child.members)
            if child is not None
            else [UUID(str(member)) for member in entry.get("members") or []]
        )
        kind = WRITES[check]
        if check == NEW_STORY:
            parent = epic or (
                UUID(str(entry["parent"]["id"]))
                if isinstance(entry.get("parent"), dict)
                else UUID(str(entry["parent"]))
                if entry.get("parent")
                else None
            )
            if parent is None:
                raise ProjectInvalid("Choose the Epic it goes under.")
        else:
            parent = UUID(about)
        with self.sessions() as db:
            project = active_project(db, name)
            alive = db.scalar(
                select(ProjectEpic.id if kind == "stories" else ProjectUserStory.id).where(
                    (ProjectEpic.id if kind == "stories" else ProjectUserStory.id) == parent,
                    (ProjectEpic if kind == "stories" else ProjectUserStory).project_id
                    == project.id,
                    (ProjectEpic if kind == "stories" else ProjectUserStory).archived_at.is_(None),
                    (ProjectEpic if kind == "stories" else ProjectUserStory).closed_at.is_(None),
                )
            )
            if alive is None:
                raise ProjectConflict("The record it would go under is closed or gone.")
            titles = _titles(db, ProjectEpic if kind == "stories" else ProjectUserStory, [parent])
        made: list[UUID] = []
        for item in entry["items"]:
            key = self._save(name, kind, str(item["title"]), str(item["description"]), parent, by)
            linked: list[str] = []
            if check == NEW_STORY:
                linked = self._place(name, key, members)
            with self.sessions.begin() as db:
                run = db.get_one(OrganizeRun, run_key)
                review = db.get(ChoiceReview, UUID(str(entry["review"])))
                parent_choice.decide(review, True)
                db.add(
                    OrganizeLine(
                        run_id=run.id,
                        project_id=run.project_id,
                        listed="created",
                        state="applied",
                        case=f"{kind[:-1] if kind != 'stories' else 'story'}_made",
                        subject_kind=kind,
                        subject_id=key,
                        title=str(item["title"])[:300],
                        candidates=[],
                        said=entry.get("said") or [],
                        reason=(
                            "Written from the records it names, and every sentence found "
                            "supported by a second model. No record like it exists in the "
                            "project or the projects connected to it."
                            if by == "run"
                            else "Made at your word, as it was written."
                        ),
                        doubt=0.0,
                        action={
                            "op": "created",
                            "about": about,
                            "check": check,
                            "review": str(entry["review"]),
                            "by": by,
                            "linked": linked,
                            "for": {
                                "kind": "epics" if kind == "stories" else "stories",
                                "id": str(parent),
                                "title": str(titles.get(parent) or ""),
                            },
                            "description": item["description"],
                            "searched": entry.get("searched"),
                        },
                        made_kind=kind,
                        made_id=key,
                        sources=self._sources_of(item),
                        answered_at=datetime.now(UTC),
                    )
                )
            made.append(key)
        return made

    @staticmethod
    def _sources_of(item: dict[str, Any]) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for sentence in item.get("sentences") or []:
            for source in sentence.get("sources") or []:
                seen.setdefault(str(source.get("id")), source)
        return list(seen.values())

    def _place(self, name: str, story: UUID, members: list[UUID]) -> list[str]:
        """Put the Issues a Story was written from under it: those that still have
        none."""
        placed: list[str] = []
        for issue in members:
            with self.sessions() as db:
                taken = db.scalar(
                    select(ProjectIssueStory.story_id)
                    .where(ProjectIssueStory.item_id == issue)
                    .limit(1)
                )
            if taken is not None:
                continue
            try:
                self.links.before_change(name, "issues", issue)
                self.planning.link_stories(name, issue, [story])
                self.links.noted(
                    name,
                    "issues",
                    issue,
                    story,
                    Note(
                        "model",
                        confirmed=True,
                        reason="Put under this User Story when it was written from Issues "
                        "that two models found to fit no Story there was.",
                    ),
                )
                with self.sessions.begin() as db:
                    project = active_project(db, name)
                    parent_choice.decide(
                        parent_choice.review_of(db, project.id, "issue_story", issue), True
                    )
                    # The case that waited about this Issue is answered by the Story.
                    for line in db.scalars(
                        select(OrganizeLine).where(
                            organize.lines_where(project.id, "stories_issues"),
                            OrganizeLine.listed == "needs_you",
                            OrganizeLine.state == "open",
                            OrganizeLine.subject_id == issue,
                        )
                    ):
                        if (line.action or {}).get("op") == "place":
                            line.state, line.answer = "skipped", "made"
                            line.answered_at = datetime.now(UTC)
                placed.append(str(issue))
            except (ProjectInvalid, ProjectNotFound, ProjectConflict):
                continue
        return placed

    # -- the administrator's word -----------------------------------------------------

    def _close(self, name: str, kind: str, key: UUID) -> None:
        """Close a User Story or an Epic, and nothing under it."""
        impact = self.planning.lifecycle.preview(name, kind, key, "close")  # type: ignore[arg-type]
        if any(entry["action"] == "close" and entry["id"] != str(key) for entry in impact["items"]):
            raise ProjectConflict("Closing it would close records under it that are open.")
        self.planning.lifecycle.confirm(name, kind, key, "close", impact["token"])  # type: ignore[arg-type]

    def answer(
        self,
        name: str,
        kind: str,
        key: UUID,
        choice: str,
        *,
        epic: UUID | None = None,
        titles: list[str] | None = None,
    ) -> dict[str, str]:
        """The administrator's answer to a case of this stage: make what was written
        (``make``) or leave it (``leave``); close the record (``close``) or keep it
        (``keep``); split the Epic (``split``) or keep it."""
        with self.sessions() as db:
            project = active_project(db, name)
            line = organize.own_line(db, project.id, kind, key)
            if line.listed != "needs_you" or line.state != "open":
                raise ProjectConflict("This case was decided already.")
            action = dict(line.action or {})
            run_key, subject, reason = line.run_id, line.subject_id, line.reason
            offered = [dict(entry) for entry in line.candidates or []]
            said = list(line.said or [])
        op = str(action.get("op"))
        allowed = {
            "make": ("make", "leave"),
            "retire": ("close", "keep"),
            "close_story": ("close", "keep"),
            "split": ("split", "keep"),
        }.get(op, ())
        if choice not in allowed:
            raise ProjectInvalid("That is no answer to this case.")
        listed, state, before, problem = "needs_you", "answered", None, None
        case = None
        try:
            if op == "make" and choice == "make":
                check = str(action["check"])
                if check == NEW_STORY:
                    chosen = epic or (UUID(str(action["parent"])) if action.get("parent") else None)
                    known = {e["id"] for e in offered if e.get("offered")}
                    if chosen is None or str(chosen) not in known:
                        raise ProjectInvalid("Choose one of the Epics it was offered.")
                    epic = chosen
                self._make(name, run_key, check, {**action, "said": said}, by="you", epic=epic)
            elif op == "make":
                with self.sessions.begin() as db:
                    parent_choice.decide(db.get(ChoiceReview, UUID(str(action["review"]))), False)
            elif op == "retire" and choice == "close":
                side = "stories" if action["check"] == NEW_ISSUES else "epics"
                self._close(name, side, UUID(str(action["about"])))
                with self.sessions.begin() as db:
                    parent_choice.decide(db.get(ChoiceReview, UUID(str(action["review"]))), True)
                before, listed, state, case = {"closed": side}, "done", "applied", "closed"
            elif op == "retire":
                with self.sessions.begin() as db:
                    parent_choice.decide(db.get(ChoiceReview, UUID(str(action["review"]))), False)
            elif op == "close_story" and choice == "close":
                self.changes.apply_one(
                    name, "close", UUID(str(action["about"])), UUID(str(action["issue"])), reason
                )
                before, listed, state, case = {"closed": "stories"}, "done", "applied", "closed"
            elif op == "split" and choice == "split":
                before = self._split(name, str(action["about"]), offered, titles)
                listed, state, case = "done", "applied", "split_made"
            elif op == "split":
                self.shape.dismiss(name, str(action["about"]))
        except (ProjectConflict, ProjectNotFound) as changed:
            problem = str(changed)
        del subject
        with self.sessions.begin() as db:
            project = active_project(db, name)
            line = organize.own_line(db, project.id, kind, key)
            now = datetime.now(UTC)
            if problem:
                line.state, line.answered_at = "skipped", now
                line.reason = f"{line.reason} Set aside: {problem}".strip()
                return {"id": str(key), "state": "skipped", "detail": problem}
            line.listed, line.state, line.answer, line.answered_at = listed, state, choice, now
            line.before = before
            if case:
                line.case = case
        return {"id": str(key), "state": state}

    def _split(
        self, name: str, proposal: str, offered: list[dict[str, Any]], titles: list[str] | None
    ) -> dict[str, Any]:
        """Split the Epic as proposed, with the titles given or those proposed, and
        return what is needed to put it back."""
        new = [entry for entry in offered if not entry.get("stays")]
        names = [title.strip() for title in titles or []] or [str(e["title"]) for e in new]
        with self.sessions() as db:
            found = structure.survey(db, active_project(db, name))
        entry = next((e for e in found["splits"] if e["id"] == proposal), None)
        if entry is None:
            raise ProjectConflict("This split is no longer proposed: the plan has changed.")
        moved = [
            UUID(story["id"])
            for part in entry["parts"]
            if not part["stays"]
            for story in part["stories"]
        ]
        links = {str(story): self.links.snapshot(name, "stories", story) for story in moved}
        made = self.shape.split(name, proposal, names)
        return {"split": made["epics"], "links": links}

    def answer_many(self, name: str, kind: str, group: str, choice: str) -> dict[str, int]:
        """One word for many cases of this stage. A request answers some of them and
        says how many are left."""
        if group not in GROUPS or choice not in GROUPS[group][1]:
            raise ProjectInvalid("That cannot be answered for all of them at once.")
        with self.sessions() as db:
            project = active_project(db, name)
            keys = list(
                db.scalars(
                    select(OrganizeLine.id)
                    .where(
                        organize.lines_where(project.id, kind),
                        OrganizeLine.listed == "needs_you",
                        OrganizeLine.state == "open",
                        OrganizeLine.case.in_(GROUPS[group][0]),
                    )
                    .order_by(OrganizeLine.created_at, OrganizeLine.id)
                )
            )
        answered = skipped = 0
        for key in keys[:CHUNK]:
            try:
                state = self.answer(name, kind, key, choice)["state"]
                skipped += state == "skipped"
                answered += state != "skipped"
            except (ProjectConflict, ProjectInvalid, ProjectNotFound):
                skipped += 1
        return {"answered": answered, "skipped": skipped, "left": max(0, len(keys) - CHUNK)}

    def waiting(self, db: Session, project: UUID, kind: str) -> dict[str, int]:
        found = dict.fromkeys(GROUPS, 0)
        for case in db.scalars(
            select(OrganizeLine.case).where(
                organize.lines_where(project, kind),
                OrganizeLine.listed == "needs_you",
                OrganizeLine.state == "open",
            )
        ):
            for group, (cases, _choices) in GROUPS.items():
                found[group] += case in cases
        return found

    # -- undo -------------------------------------------------------------------------

    def _remove(self, name: str, kind: str, key: UUID) -> None:
        """Take a record away that a run made, and nothing else with it."""
        impact = self.planning.lifecycle.preview(name, kind, key, "delete")  # type: ignore[arg-type]
        if any(entry["id"] != str(key) for entry in impact["items"]):
            raise ProjectConflict(
                "Records were put under it since it was made. Take those off first."
            )
        self.planning.lifecycle.confirm(name, kind, key, "delete", impact["token"])  # type: ignore[arg-type]

    def undo(self, name: str, kind: str, key: UUID) -> None:
        """Take back what a line of this stage did: a record that was made is taken
        away, a record that was closed is open again, a split is put back."""
        with self.sessions() as db:
            project = active_project(db, name)
            line = organize.own_line(db, project.id, kind, key)
            if not organize.can_undo(line, datetime.now(UTC)):
                raise ProjectConflict(
                    f"This can be undone for {organize.UNDO.days} days after it was applied."
                )
            action, before = dict(line.action or {}), dict(line.before or {})
            made_kind, made = line.made_kind, line.made_id
            if made_kind == "issues" and made is not None:
                item = db.get(BacklogItem, made)
                if item is not None and item.workspace_review != "pending":
                    raise ProjectConflict(
                        "This Issue was published to GitHub. It is closed there, not taken "
                        "away here."
                    )
        op = str(action.get("op"))
        if op == "created" and made is not None and made_kind:
            for issue in action.get("linked") or []:
                self.links.before_change(name, "issues", UUID(str(issue)))
                self.planning.remove_story_link(name, UUID(str(issue)), made, remember=False)
            self._remove(name, made_kind, made)
            with self.sessions.begin() as db:
                project = active_project(db, name)
                review = db.get(ChoiceReview, UUID(str(action["review"])))
                # It is not written and made again: the administrator took it away.
                if review is not None and review.state == "answered":
                    review.decision, review.decided_at = "dismissed", datetime.now(UTC)
                for issue in action.get("linked") or []:
                    earlier = parent_choice.review_of(
                        db, project.id, "issue_story", UUID(str(issue))
                    )
                    if earlier is not None and earlier.state == "answered":
                        earlier.decision, earlier.decided_at = "dismissed", datetime.now(UTC)
        elif op in ("retire", "close_story"):
            side = str(before.get("closed") or "stories")
            self.planning.set_state(name, side, UUID(str(action["about"])), "open")  # type: ignore[arg-type]
        elif op == "split":
            self._unsplit(name, before)
        else:
            raise ProjectConflict("This cannot be undone from here.")
        with self.sessions.begin() as db:
            project = active_project(db, name)
            line = organize.own_line(db, project.id, kind, key)
            line.state, line.undone_at = "undone", datetime.now(UTC)
            if op in ("retire", "close_story", "split"):
                # The case waits again: it was the administrator's own answer.
                line.listed, line.state, line.answer = "needs_you", "open", None
                line.case = "split" if op == "split" else line.case
                if op != "split":
                    line.case = "undone"
                    line.reason = "You took this back: the record is open again. " + line.reason

    def _unsplit(self, name: str, before: dict[str, Any]) -> None:
        links = before.get("links") or {}
        for made in before.get("split") or []:
            epic = UUID(str(made["epic"]))
            for story in made.get("stories") or []:
                key = UUID(str(story))
                self.links.before_change(name, "stories", key)
                self.planning.remove_epic_link(name, key, epic)
                self.links.after_change(name, "stories", key)
                self.links.restore(name, "stories", key, links.get(str(story)) or [])
            self._remove(name, "epics", epic)

    # -- publishing -------------------------------------------------------------------

    @staticmethod
    def _unpublished(db: Session, project: UUID) -> list[UUID]:
        """The Issues a run made that are kept here: not written to GitHub yet."""
        made = [
            key
            for key in db.scalars(
                select(OrganizeLine.made_id).where(
                    OrganizeLine.project_id == project,
                    OrganizeLine.listed == "created",
                    OrganizeLine.state == "applied",
                    OrganizeLine.made_kind == "issues",
                )
            )
            if key is not None
        ]
        if not made:
            return []
        return list(
            db.scalars(
                select(BacklogItem.id).where(
                    BacklogItem.id.in_(made), BacklogItem.workspace_review == "pending"
                )
            )
        )

    def publish(self, name: str) -> dict[str, int]:
        """The administrator's one word: every Issue a run made and that is kept here
        is given to GitHub. They are written there by the worker that writes every
        approved Issue."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            waiting = self._unpublished(db, project.id)
            sent = 0
            for key in waiting:
                try:
                    sent += approve_issue(db, project, key) is not None
                except ProjectConflict:
                    continue
        return {"published": sent, "left": len(waiting) - sent}


def _family(named: tuple[str, str]) -> str:
    # The makers are told apart as the check engine tells them apart.
    return str(getattr(parent_choice, "family_of")(*named))  # noqa: B009


def _shown(role: tuple[str, str] | None) -> dict[str, str] | None:
    return {"provider": role[0], "model": role[1]} if role else None


def _price(prices: AccountPrices, named: tuple[str, str], read: int, written: int) -> float | None:
    entry = prices.tariff(None, named[0], named[1])
    if not entry:
        return None
    try:
        return (read * float(entry["input"]) + written * float(entry["output"])) / 1e6
    except (KeyError, TypeError, ValueError):
        return None


def _titles(db: Session, model: Any, keys: list[UUID]) -> dict[UUID, str]:
    if not keys:
        return {}
    return dict(db.execute(select(model.id, model.title).where(model.id.in_(keys))).tuples().all())


__all__ = ["CHECKS", "GROUPS", "OPS", "STEPS", "Creator", "stands"]
