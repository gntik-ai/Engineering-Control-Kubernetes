"""Coverage: the documentation of a project against its Epics, User Stories and Issues
(O4 of docs/design/organize-and-coverage.md).

One number says how much of the documentation the plan covers: of the parts that are
about the product, the share that an Epic, a User Story or an Issue does what it says.
One button checks it. A run goes through the same stages as a run of Organize:

- **Read**: the documents as they were last read are cut into parts, and the ranking
  service reads the parts it has not read. A part of an OpenSpec change that is named
  after an Issue of the plan is covered by that Issue without a model being asked.
- **Match and Verify**: every other part that is about the product, or of which nobody
  said yet, is shown the records that lie closest to it, and two models of different
  makers each say whether one of them covers it, with a passage of the part and of the
  record. What both say, with a record in common and passages that are in the texts,
  is settled. What they differ on is put before the administrator.
- **Create**: for the parts that nothing covers, records are written at the right
  level and made after the careful review of ``organize_create``. An Epic that is
  written is never made without the administrator.

A part of the documentation of a connected project that names this project is checked
against this project's plan too, and counted beside the number.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import numpy
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import (
    coverage_check,
    coverage_parts,
    doc_parts,
    organize,
    parent_choice,
    review_settings,
)
from engineering_control.dashboard.coverage_check import COVER, RECORD, SETTLED, named
from engineering_control.dashboard.coverage_parts import effective
from engineering_control.dashboard.organize_create import DOUBT as DRAFT_DOUBT
from engineering_control.dashboard.organize_create import SAID as DRAFT_SAID
from engineering_control.dashboard.organize_create import Creator, _price, _Twins, stands
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.workspace_costs import AccountPrices
from engineering_control.db.models import (
    BacklogItem,
    ChoiceReview,
    ChoiceRun,
    DocFile,
    DocPart,
    DocScan,
    OrganizeLine,
    OrganizeRun,
    ProjectEpic,
    ProjectRecord,
    ProjectUserStory,
)

log = logging.getLogger(__name__)

KIND: Final = "coverage"
LIVE: Final = ("running", "held")
STEPS: Final = ("read", COVER, RECORD)
STAGE: Final = {"read": "read", COVER: "verify", RECORD: "create"}
PAGE: Final = 25
# How doubtful a case is, the most doubtful first.
DOUBT: Final = {
    **DRAFT_DOUBT,
    "differ": 0.85,
    "unclear": 0.8,
    "unchecked": 0.6,
    "single": 0.55,
    "draft_epic": 0.5,
    "source_gone": 0.4,
}
SAID: Final = {
    "differ": "The two models did not say the same of this part.",
    "unclear": "The model could not tell whether a record covers this part.",
    "unchecked": "The two models agree that it is covered, and a passage one quoted is "
    "not in the texts.",
    "single": "One model answered, and no second model of another maker is set.",
}
GROUPS: Final = {"drafts": (("draft_stopped", "draft_single", "draft_epic"), ("make",))}
CHUNK: Final = 20
FRESH: Final = 300.0
# How many questions are read to say how long a question is.
SAMPLE: Final = 40
# From how close by meaning a part is taken to speak of a record.
SPEAKS: Final = 0.7
_PLANS: dict[str, tuple[float, Any, dict[str, Any]]] = {}
_SURVEYS: dict[str, tuple[float, Any, dict[str, Any]]] = {}
_SILENT: dict[str, tuple[float, Any, list[dict[str, Any]]]] = {}


def _stamp(db: Session, model: Any, project: UUID) -> tuple[int, str]:
    count, last = db.execute(
        select(func.count(), func.max(model.updated_at)).where(model.project_id == project)
    ).one()
    return int(count), str(last)


class CoverageRun:
    """The run behind the button of Coverage."""

    def __init__(self, sessions: sessionmaker[Session], creator: Creator) -> None:
        self.sessions = sessions
        self.creator = creator

    # -- the number -----------------------------------------------------------------

    def _survey(self, db: Session, project: ProjectRecord) -> dict[str, Any]:
        """How the documents cut into parts, before a run kept them: by rule alone."""
        at = f"{id(self.sessions)}:{project.id}"
        mark, now = _stamp(db, DocFile, project.id), time.monotonic()
        kept = _SURVEYS.get(at)
        if kept and kept[1] == mark and now - kept[0] < FRESH:
            return kept[2]
        found = doc_parts.survey(db, project)
        _SURVEYS[at] = (now, mark, found)
        return found

    def numbers(self, db: Session, project: ProjectRecord) -> dict[str, Any]:
        """Where the documentation stands: of its parts that are about the product, how
        many a record covers, how many are in doubt, how many nothing covers, and how
        many were not checked yet. And the parts of which nobody said yet what they
        are about, and those that are not about the product."""
        found = dict.fromkeys(
            ("parts", "product", "covered", "doubt", "none", "unchecked", "open", "other"), 0
        )
        rows = db.execute(
            select(DocPart.about, DocPart.said, DocPart.state, func.count())
            .where(DocPart.project_id == project.id, DocPart.gone_at.is_(None))
            .group_by(DocPart.about, DocPart.said, DocPart.state)
        ).all()
        if not rows:
            # No run kept the parts yet: what the rules alone say of them.
            survey = self._survey(db, project)
            return {
                **found,
                "parts": int(survey["parts"]),
                "product": int(survey["product"]),
                "unchecked": int(survey["product"]),
                "open": int(survey["open"]),
                "other": int(survey["other"]),
                "documents": int(survey["files_read"]),
                "read": False,
                "percent": None,
            }
        for about, said, state, count in rows:
            what = effective(about, said, state)
            found["parts"] += count
            if what == "product":
                found["product"] += count
                found[state if state in ("covered", "doubt", "none") else "unchecked"] += count
            else:
                found[what] += count
        documents = int(
            db.scalar(
                select(func.count())
                .select_from(DocFile)
                .where(
                    DocFile.project_id == project.id,
                    DocFile.removed_scan_id.is_(None),
                    DocFile.text.is_not(None),
                )
            )
            or 0
        )
        return {
            **found,
            "documents": documents,
            "read": True,
            "percent": round(100 * found["covered"] / found["product"], 1)
            if found["product"]
            else None,
        }

    @staticmethod
    def _asks(db: Session, project: ProjectRecord) -> dict[str, int]:
        """The parts of connected documentation that name this project, and how many of
        them its plan covers."""
        wanted = coverage_check.asking(db, project)
        found = {"parts": len(wanted), "covered": 0, "none": 0, "doubt": 0}
        if not wanted:
            return found
        for review in db.scalars(
            select(ChoiceReview).where(
                ChoiceReview.project_id == project.id,
                ChoiceReview.kind == COVER,
                ChoiceReview.state == "answered",
                ChoiceReview.child_id.in_(wanted),
            )
        ):
            if review.agreed and review.answer == "covered" and review.checked:
                found["covered"] += 1
            elif review.agreed and review.answer in ("none", "other"):
                found["none"] += review.answer == "none"
            else:
                found["doubt"] += 1
        return found

    # -- what a run would do ----------------------------------------------------------

    def _heavy(self, db: Session, project: ProjectRecord) -> dict[str, Any]:
        """What takes reading every part: how many would be asked about, and how long
        a question is. Kept until a part, an answer or a record changes."""
        at = f"{id(self.sessions)}:{project.id}"
        mark = (
            _stamp(db, DocPart, project.id),
            _stamp(db, ChoiceReview, project.id),
            _stamp(db, ProjectUserStory, project.id),
            _stamp(db, ProjectEpic, project.id),
            _stamp(db, BacklogItem, project.id),
        )
        now = time.monotonic()
        kept = _PLANS.get(at)
        if kept and kept[1] == mark and now - kept[0] < FRESH:
            return kept[2]
        scope = coverage_check.KIND.scope(db, project)
        known = parent_choice.reviews(db, project.id, COVER)
        ask = []
        for child in scope.children():
            listed = scope.shortlist(child)
            review = known.get(child.id)
            current = parent_choice.standing(scope, child, listed, review)
            if parent_choice.to_ask(review, current, False):
                ask.append((child, listed))
        stand_in = ChoiceRun(provider="", model="")
        step = max(1, len(ask) // SAMPLE)
        sizes = [
            (len(asked.system) + len(asked.prompt)) * 2 // 5
            for asked in (
                scope.question(stand_in, child, listed) for child, listed in ask[::step][:SAMPLE]
            )
        ]
        found = {
            "children": len(scope.children()),
            "to_ask": len(ask),
            "asks": sum(1 for child, _listed in ask if child.asks),
            "read": int(sum(sizes) / len(sizes)) if sizes else 0,
        }
        _PLANS[at] = (now, mark, found)
        return found

    def plan(self, db: Session, project: ProjectRecord, numbers: dict[str, Any]) -> dict[str, Any]:
        """What a run started now would ask, of whom, and what that is likely to cost.
        No model is asked for it."""
        roles = parent_choice.planned(db, project.id, COVER)
        mine = review_settings.of(db, project.id)
        spent, _since = review_settings.spent(db, project.id)
        if not numbers["read"]:
            # Nothing is kept yet: every part that is not set aside by rule is asked about.
            to_ask, read = int(numbers["product"]) + int(numbers["open"]), 1400
            asks = 0
        else:
            heavy = self._heavy(db, project)
            to_ask, read, asks = int(heavy["to_ask"]), int(heavy["read"]), int(heavy["asks"])
        answer, judge = roles["answer"], roles["judge"]
        models = 2 if judge else 1
        tokens = to_ask * (read + 300) * models
        dollars: float | None = 0.0
        if to_ask and answer is None:
            dollars = None
        elif to_ask and answer is not None:
            prices = AccountPrices(db)
            first = _price(
                prices, (answer["provider"], answer["model"]), to_ask * read, to_ask * 300
            )
            second = (
                _price(prices, (judge["provider"], judge["model"]), to_ask * read, to_ask * 300)
                if judge
                else 0.0
            )
            dollars = first + second if first is not None and second is not None else None
        blocked = None
        if to_ask and answer is None:
            blocked = parent_choice.NO_ANSWERING_MODEL
        elif to_ask:
            blocked = review_settings.over(db, project.id)
        unread = int(
            db.scalar(
                select(func.count())
                .select_from(DocPart)
                .where(
                    DocPart.project_id == project.id,
                    DocPart.gone_at.is_(None),
                    DocPart.vector.is_(None),
                )
            )
            or 0
        )
        return {
            "to_ask": to_ask,
            # Of those: parts of connected documentation that name this project.
            "asks": asks,
            "ask_blocked": blocked,
            "answer": answer,
            "judge": judge,
            "tokens": tokens,
            "dollars": round(dollars, 2) if dollars is not None else None,
            "limit": mine.dollars,
            "spent": round(spent, 2),
            "would_pass": bool(dollars and spent + dollars > mine.dollars),
            # The documentation was read again, and changed, since the last run looked.
            "reread": self._reread(db, project.id),
            "run_tokens": mine.count("run_tokens"),
            # Parts the ranking service has not read: it reads them when a run starts.
            "unread": unread if numbers["read"] else int(numbers["parts"]),
            "meaning": self.creator.embed is not None,
        }

    @staticmethod
    def _reread(db: Session, project: UUID) -> bool:
        runs = organize.runs_of(db, project, KIND)
        if not runs or runs[0].started_at is None:
            return False
        return bool(
            db.scalar(
                select(func.count())
                .select_from(DocScan)
                .where(
                    DocScan.project_id == project,
                    DocScan.state == "complete",
                    DocScan.ended_at > runs[0].started_at,
                    DocScan.added + DocScan.modified + DocScan.removed > 0,
                )
            )
        )

    def shown(self, name: str) -> dict[str, Any]:
        """The number, what a run started now would do and cost, the run under way or
        the last one, and how many lines the lists hold."""
        with self.sessions() as db:
            project = active_project(db, name)
            numbers = self.numbers(db, project)
            plan = self.plan(db, project, numbers)
            create = self.creator.plan(db, project, KIND)
            runs = organize.runs_of(db, project.id, KIND)
            run = runs[0] if runs else None
            live = run is not None and run.state in LIVE
            writes = int(create["to_ask"]) if not create["blocked"] else 0
            asks = int(plan["to_ask"]) if not plan["ask_blocked"] else 0
            why = None
            if live:
                why = "A run is under way."
            elif not numbers["parts"]:
                why = (
                    "No documentation of this project was read yet. It is read under Documentation."
                )
            elif plan["reread"]:
                why = None
            elif numbers["read"] and not (plan["to_ask"] or create["to_ask"]):
                why = "Every part of the documentation has its answer: there is nothing to check."
            elif numbers["read"] and not (asks or writes):
                why = plan["ask_blocked"] or create["blocked"]
            return {
                "project": project.name,
                "kind": KIND,
                "stands": numbers,
                "asks": self._asks(db, project),
                "plan": plan,
                "create": create,
                "can_start": why is None,
                "why_not": why,
                "run": self._shown_run(db, run) if run else None,
                "runs": len(runs),
                "lists": organize.list_counts(db, project.id, KIND),
                "waiting": self.creator_waiting(db, project.id),
                # The Issues runs made that are kept here and not on GitHub yet.
                "unpublished": len(self.creator._unpublished(db, project.id)),
                "undo_days": organize.UNDO.days,
            }

    @staticmethod
    def creator_waiting(db: Session, project: UUID) -> dict[str, int]:
        found = dict.fromkeys(GROUPS, 0)
        for case in db.scalars(
            select(OrganizeLine.case).where(
                organize.lines_where(project, KIND),
                OrganizeLine.listed == "needs_you",
                OrganizeLine.state == "open",
            )
        ):
            for group, (cases, _choices) in GROUPS.items():
                found[group] += case in cases
        return found

    @staticmethod
    def _shown_run(db: Session, run: OrganizeRun) -> dict[str, Any]:
        est = run.estimate or {}
        asking = db.get(ChoiceRun, UUID(str(est["choice_run"]))) if est.get("choice_run") else None
        return {
            "id": str(run.id),
            "state": run.state,
            "stage": run.stage,
            "detail": run.detail,
            "spent": run.spent,
            "cost": run.cost,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "ended_at": run.ended_at.isoformat() if run.ended_at else None,
            "asking": {
                "about": "whether a record covers each part"
                if asking.kind == COVER
                else "writing records for the parts nothing covers",
                "done": asking.done,
                "total": asking.total,
                "state": asking.state,
            }
            if asking is not None and run.state in LIVE
            else None,
            "stages": organize.shown_stages(run),
        }

    # -- the run ----------------------------------------------------------------------

    def start(self, name: str) -> dict[str, str]:
        """The administrator's word: the documentation is checked against the plan."""
        shown = self.shown(name)
        if not shown["can_start"]:
            raise ProjectConflict(str(shown["why_not"]))
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            if any(run.state in LIVE for run in organize.runs_of(db, project.id, KIND)):
                raise ProjectConflict("A run is under way.")
            run = OrganizeRun(
                project_id=project.id,
                kind=KIND,
                scope=[str(key) for key in organize.scope(db, project.id)],
                state="running",
                stage="read",
                started_at=datetime.now(UTC),
                estimate={
                    "plan": shown["plan"],
                    "at": 0,
                    "begun": False,
                    "choice_run": None,
                    "notes": [],
                    "made": 0,
                    "needs": 0,
                },
                counts={},
            )
            db.add(run)
            db.flush()
            key = run.id
        # The worker carries it on from here: reading the parts takes its time.
        return {"id": str(key)}

    def _begin(self, name: str, check: str) -> str | None:
        with self.sessions() as db:
            project = active_project(db, name)
            under_way = db.scalar(
                select(ChoiceRun.id).where(
                    ChoiceRun.project_id == project.id,
                    ChoiceRun.kind == check,
                    ChoiceRun.state.in_(parent_choice.LIVE),
                )
            )
            roles = self.creator.roles(db, project.id)
        if under_way:
            return str(under_way)
        if check == COVER:
            return str(parent_choice.start(self.sessions, name, COVER)["id"])
        writer = roles["writer"]
        if writer is None:
            raise ProjectInvalid(parent_choice.NO_ANSWERING_MODEL)
        started = parent_choice.start(
            self.sessions, name, RECORD, writer[0], writer[1], judge=roles["checker"]
        )
        return str(started["id"])

    def _read(self, name: str, key: UUID) -> bool:
        """The stage that reads: the parts are kept as the documents stand, and the
        ranking service reads those it has not read. False while it is still reading."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            run = db.get_one(OrganizeRun, key)
            est = dict(run.estimate or {})
            if not est.get("kept"):
                found = coverage_parts.sync(db, project)
                # What a connected project's documentation asks of this one is read too.
                for other in organize.scope(db, project.id):
                    peer = db.get(ProjectRecord, other)
                    if peer is not None and other != project.id:
                        coverage_parts.sync(db, peer)
                self._orphaned(db, run)
                est["kept"], est["parts"] = True, found
                run.estimate = est
        left = coverage_parts.embed(self.sessions, name, self.creator.embed)
        with self.sessions.begin() as db:
            project = active_project(db, name)
            run = db.get_one(OrganizeRun, key)
            est = dict(run.estimate or {})
            # It goes on while the service gets through the parts; where it does not
            # (it is not there, or stands still), the parts are compared by their words.
            if left and left < int(est.get("unread", left + 1)):
                est["unread"] = left
                run.estimate = est
                run.detail = f"The ranking service is reading the parts: {left:,} are left."
                return False
            numbers = self.numbers(db, project)
            run.detail = ""
            run.counts = {
                **(run.counts or {}),
                "read": {
                    "records": numbers["parts"],
                    "settled": numbers["covered"],
                    "none": numbers["other"],
                },
                "match": {"records": numbers["product"] + numbers["open"] - numbers["covered"]},
            }
        return True

    def _advance(self, name: str, key: UUID) -> bool:
        """Carry the run on, step by step. True when it ended."""
        for _ in range(8):
            with self.sessions.begin() as db:
                run = db.get_one(OrganizeRun, key)
                if run.state not in LIVE:
                    return True
                est = dict(run.estimate or {})
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
                at = int(est.get("at", 0))
                gathers = ended and at < len(STEPS) and STEPS[at] != "read" and est.get("begun")
                if at >= len(STEPS) or (ended and not gathers):
                    self._finish(db, run, est, "stopped" if ended else "complete")
                    return True
                step = STEPS[at]
                run.stage = STAGE[step]
            if gathers:
                # Ended: what was answered until then is kept, and nothing more is asked.
                if step == COVER:
                    self._gather_cover(name, key)
                else:
                    self._gather_written(name, key)
                with self.sessions.begin() as db:
                    run = db.get_one(OrganizeRun, key)
                    self._finish(db, run, dict(run.estimate or {}), "stopped")
                return True
            if step == "read":
                if not self._read(name, key):
                    return False
            else:
                if not est.get("begun"):
                    started: str | None = None
                    try:
                        started = self._begin(name, step)
                    except (ProjectInvalid, ProjectConflict) as nothing:
                        est["notes"] = [*est.get("notes", []), {"step": step, "why": str(nothing)}]
                    with self.sessions.begin() as db:
                        run = db.get_one(OrganizeRun, key)
                        est["begun"] = True
                        run.estimate = {**est, "choice_run": started}
                    if started:
                        continue
                if step == COVER:
                    self._gather_cover(name, key)
                else:
                    self._gather_written(name, key)
            with self.sessions.begin() as db:
                run = db.get_one(OrganizeRun, key)
                now = dict(run.estimate or {})
                run.estimate = {**now, "at": at + 1, "begun": False, "choice_run": None}
        return False

    @staticmethod
    def _finish(db: Session, run: OrganizeRun, est: dict[str, Any], final: str) -> None:
        quiet = {kind.nothing_to_ask for kind in parent_choice.KINDS.values()} | {
            kind.nothing_waits for kind in parent_choice.KINDS.values()
        }
        said = [
            f"Not everything was asked: {note['why']}"
            for note in est.get("notes", [])
            if note["why"] not in quiet
        ][:1]
        left = int(est.get("left", 0))
        if left and not said:
            said.append(
                f"{left:,} parts were not asked about in this run: it was ended, or reached "
                "a limit. Start it again to go on from there."
            )
        run.state, run.stage, run.ended_at = final, "done", datetime.now(UTC)
        run.detail = " ".join(said)
        run.counts = {
            **(run.counts or {}),
            "create": {
                "records": int(est.get("made", 0)),
                "in_doubt": int(est.get("needs", 0)),
            },
        }
        run.estimate = {**est, "choice_run": None}

    def tick(self) -> bool:
        """Carry every run that is under way one step on. True when one ended."""
        ended = False
        with self.sessions() as db:
            waiting = [
                (run.id, run.project_id)
                for run in db.scalars(
                    select(OrganizeRun).where(
                        OrganizeRun.kind == KIND,
                        OrganizeRun.connection_id.is_(None),
                        OrganizeRun.state.in_(LIVE),
                    )
                )
            ]
        for key, project_id in waiting:
            with self.sessions.begin() as db:
                project = db.get(ProjectRecord, project_id)
                if project is None or project.archived_at is not None:
                    run = db.get_one(OrganizeRun, key)
                    run.state, run.detail = "failed", "The project was archived."
                    run.ended_at = datetime.now(UTC)
                    continue
                name = project.name
            try:
                ended = self._advance(name, key) or ended
            except Exception:  # noqa: BLE001 - one project's run must not stop the others
                log.exception("Coverage of %s could not be carried on", name)
                with self.sessions.begin() as db:
                    run = db.get_one(OrganizeRun, key)
                    run.state, run.ended_at = "failed", datetime.now(UTC)
                    run.detail = "What was answered could not be gathered. Start it again."
        return ended

    def control(self, name: str, action: str) -> dict[str, str]:
        """Stop, resume or end the run under way: it is the asking that is stopped.
        Ended, the run asks nothing more and keeps what was answered."""
        with self.sessions.begin() as db:
            project = active_project(db, name)
            run = next(
                (run for run in organize.runs_of(db, project.id, KIND) if run.state in LIVE), None
            )
            if run is None:
                raise ProjectConflict("No run is under way.")
            key, asking = run.id, (run.estimate or {}).get("choice_run")
            check = db.get(ChoiceRun, UUID(str(asking))) if asking else None
            kind = check.kind if check is not None else None
            if action == "end":
                run.estimate = {**(run.estimate or {}), "ended": True}
        if asking and kind:
            try:
                parent_choice.control(self.sessions, name, kind, UUID(str(asking)), action)
            except ProjectConflict:
                if action != "end":
                    raise
        self._advance(name, key)
        return {"id": str(key)}

    # -- what a run found -------------------------------------------------------------

    @staticmethod
    def _lines(db: Session, project: UUID) -> dict[tuple[str, str], OrganizeLine]:
        """Every line of Coverage about a part or a group of parts, by what it is about:
        the one that still waits where there is one, else the last."""
        found: dict[tuple[str, str], OrganizeLine] = {}
        for line in db.scalars(
            select(OrganizeLine)
            .where(organize.lines_where(project, KIND))
            .order_by(OrganizeLine.created_at)
        ):
            at = (str((line.action or {}).get("op")), str((line.action or {}).get("about")))
            if at not in found or line.state == "open" or found[at].state != "open":
                found[at] = line
        return found

    def _gather_cover(self, name: str, key: UUID) -> None:
        """What follows from what the models said of each part: it is covered, nothing
        covers it, it is not about the product, or it is in doubt and waits."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            run = db.get_one(OrganizeRun, key)
            scope = coverage_check.KIND.scope(db, project)
            known = parent_choice.reviews(db, project.id, COVER)
            lines = self._lines(db, project.id)
            rows = {
                row.id: row
                for row in db.scalars(
                    select(DocPart).where(
                        DocPart.project_id == project.id, DocPart.gone_at.is_(None)
                    )
                )
            }
            counted = {"asked": 0, "covered": 0, "doubt": 0, "none": 0, "left": 0}
            waiting: set[str] = set()
            for child in scope.children():
                review = known.get(child.id)
                listed = scope.shortlist(child)
                current = parent_choice.standing(scope, child, listed, review)
                if review is None or review.state != "answered" or review.revision != current:
                    counted["left"] += 1
                    continue
                counted["asked"] += 1
                if review.decision != "pending":
                    continue
                case, covering = self._case(review)
                part = rows.get(child.id)
                if part is not None:
                    state = case if case in ("covered", "none", "other") else "doubt"
                    if part.state != state or (state == "covered" and part.covered_by != covering):
                        part.state = state
                        part.covered_by = covering if state == "covered" else []
                counted["covered"] += case == "covered"
                counted["none"] += case == "none"
                if case in ("none", "other"):
                    continue
                about = str(child.id)
                title = f"{child.path}: {child.heading}"[:300]
                if case == "covered":
                    if ("covered", about) in lines:
                        continue
                    db.add(
                        OrganizeLine(
                            run_id=run.id,
                            project_id=project.id,
                            listed="done",
                            state="applied",
                            case="covered",
                            subject_kind="parts",
                            subject_id=child.id,
                            subject_ref=child.asks,
                            title=title,
                            candidates=covering,
                            said=self._spoke(review),
                            reason="Two models, each without seeing the other's answer, found "
                            "that this record does what the part says, and the passages "
                            "they quoted are in the texts.",
                            doubt=0.0,
                            action={"op": "covered", "about": about},
                            answered_at=datetime.now(UTC),
                        )
                    )
                    continue
                counted["doubt"] += 1
                waiting.add(about)
                line = lines.get(("cover", about))
                if line is None or line.state != "open":
                    line = OrganizeLine(project_id=project.id, run_id=run.id)
                    db.add(line)
                line.run_id, line.listed, line.state, line.case = run.id, "needs_you", "open", case
                line.subject_kind, line.subject_id, line.subject_ref = "parts", child.id, child.asks
                line.title = title
                line.candidates = self._offered(listed, review)
                line.said = self._spoke(review)
                line.reason = SAID[case]
                line.doubt = DOUBT[case]
                line.action = {
                    "op": "cover",
                    "about": about,
                    "review": str(review.id),
                    "text": (part.text[:1200] if part is not None else ""),
                    "own": part is not None,
                }
            for (op, about), line in lines.items():
                if op == "cover" and line.state == "open" and about not in waiting:
                    line.state = "skipped"
            est = dict(run.estimate or {})
            est["left"] = counted["left"]
            est["needs"] = int(est.get("needs", 0)) + counted["doubt"]
            run.estimate = est
            run.counts = {
                **(run.counts or {}),
                "verify": {
                    "records": counted["asked"],
                    "settled": counted["covered"],
                    "in_doubt": counted["doubt"],
                    "none": counted["none"],
                },
            }

    @staticmethod
    def _case(review: ChoiceReview) -> tuple[str, list[dict[str, Any]]]:
        """Where an answer leaves a part, and the records that cover it where both
        models named them."""
        if review.answer == "unclear":
            return "unclear", []
        if review.agreed is None:
            return "single", []
        if review.agreed is False:
            return "differ", []
        if review.answer != "covered":
            return str(review.answer), []
        second = {str(key) for key in (review.judge or {}).get("records") or []}
        both = [
            {
                "kind": entry.get("kind"),
                "id": entry.get("record"),
                "title": entry.get("text"),
                "project": entry.get("project"),
                "ref": entry.get("ref"),
                "how": "models",
            }
            for entry in review.evidence or []
            if entry.get("side") == "named" and str(entry.get("record")) in second
        ]
        judged = bool((review.judge or {}).get("checked"))
        return ("covered" if review.checked and judged and both else "unchecked"), both

    @staticmethod
    def _offered(listed: list[dict[str, Any]], review: ChoiceReview) -> list[dict[str, Any]]:
        first = set(named(review.evidence))
        second = {str(key) for key in (review.judge or {}).get("records") or []}
        return [
            {
                "id": entry["id"],
                "kind": entry["kind"],
                "title": entry["title"],
                "project": entry.get("project"),
                "ref": entry.get("ref"),
                "closed": bool(entry.get("closed")),
                "closeness": entry.get("closeness"),
                "chosen": entry["id"] in first,
                "judge": entry["id"] in second,
            }
            for entry in (review.shortlist or listed)
        ]

    @staticmethod
    def _spoke(review: ChoiceReview) -> list[dict[str, Any]]:
        said: list[dict[str, Any]] = [
            {
                "stage": "verify",
                "by": review.model or "a model",
                "answer": review.answer,
                "reason": review.reason,
                "passages": [
                    {"side": e.get("side"), "text": e.get("text"), "found": e.get("found")}
                    for e in review.evidence or []
                    if e.get("side") in ("part", "record")
                ],
            }
        ]
        judge = review.judge or {}
        if judge:
            said.append(
                {
                    "stage": "verify",
                    "by": str(judge.get("model") or "a second model"),
                    "answer": judge.get("answer"),
                    "reason": judge.get("reason"),
                }
            )
        return said

    def _orphaned(self, db: Session, run: OrganizeRun) -> None:
        """A record that was made for parts that are all gone from the documentation is
        put before the administrator. Nothing is taken away."""
        lines = self._lines(db, run.project_id)
        for line in db.scalars(
            select(OrganizeLine).where(
                organize.lines_where(run.project_id, KIND),
                OrganizeLine.listed == "created",
                OrganizeLine.state == "applied",
            )
        ):
            members = [UUID(str(key)) for key in (line.action or {}).get("members") or []]
            if not members or ("orphaned", str(line.id)) in lines:
                continue
            still = db.scalar(
                select(func.count())
                .select_from(DocPart)
                .where(DocPart.id.in_(members), DocPart.gone_at.is_(None))
            )
            if still:
                continue
            db.add(
                OrganizeLine(
                    run_id=run.id,
                    project_id=run.project_id,
                    listed="needs_you",
                    state="open",
                    case="source_gone",
                    subject_kind=line.made_kind or "",
                    subject_id=line.made_id,
                    title=line.title,
                    candidates=[],
                    said=[],
                    reason="This record was made for a part of the documentation that is "
                    "no longer there, or no longer says what it said.",
                    doubt=DOUBT["source_gone"],
                    action={"op": "orphaned", "about": str(line.id), "line": str(line.id)},
                )
            )

    # -- what was written ---------------------------------------------------------------

    def _gather_written(self, name: str, key: UUID) -> None:
        """What follows from what was written for the parts nothing covers: a User Story
        or an Issue that passed the careful review is made; an Epic, and everything else,
        is a case before the administrator."""
        with self.sessions() as db:
            project = active_project(db, name)
            rows = self.creator._waiting(db, project, RECORD)
            waits = {str(child.id) for child, _listed, _review, _current in rows}
            creating = self.creator.creating(db, project.id)
            twins = _Twins(db, project, self.creator.embed)
            ready: list[dict[str, Any]] = []
            for child, _listed, review, current in rows:
                stand = stands(review, current)
                if review is None or stand in ("unasked", "decided") or review.answer != "written":
                    continue
                entry: dict[str, Any] = {
                    "about": str(child.id),
                    "title": child.title,
                    "members": [str(member) for member in child.members],
                    "review": str(review.id),
                    "said": self.creator._spoke(review),
                    "sources": [
                        {"kind": s.kind, "id": s.id, "title": s.title} for s in child.sources
                    ],
                    "items": self.creator._draft(review),
                }
                found: list[dict[str, Any]] = []
                vectors: list[Any] = []
                how = "words"
                for item in entry["items"]:
                    like, how, vector = twins.search(
                        str(item["kind"]), str(item["title"]), str(item["description"])
                    )
                    found += [{**twin, "of": item["title"], "twin": True} for twin in like]
                    vectors.append(vector)
                entry["twins"], entry["searched"] = found, how
                if stand == "unsupported":
                    case = "draft_unsupported"
                elif found:
                    case = "draft_twin"
                elif stand in ("unsure", "unclear"):
                    case = "draft_unclear"
                elif how == "failed":
                    case = "draft_unverified"
                elif stand == "single":
                    case = "draft_single"
                elif stand == "unchecked":
                    case = "draft_unchecked"
                elif any(item["kind"] == "epics" for item in entry["items"]):
                    # An Epic shapes the plan: it is made at the administrator's word.
                    case = "draft_epic"
                elif not creating["in_force"]:
                    case = "draft_stopped"
                else:
                    case = "make"
                    for item, vector in zip(entry["items"], vectors, strict=False):
                        twins.made[str(item["kind"])].append(
                            ({"id": "", "kind": item["kind"], "title": item["title"]}, vector)
                        )
                entry["case"] = case
                ready.append(entry)
        with self.sessions.begin() as db:
            run = db.get_one(OrganizeRun, key)
            # What was written for parts that no longer wait (a record covers them now,
            # or they are gone) is set aside.
            for (op, about), stale in self._lines(db, run.project_id).items():
                if op == "make" and stale.state == "open" and about not in waits:
                    stale.state = "skipped"
        made = needs = 0
        for entry in ready:
            if entry["case"] == "make":
                try:
                    made += len(self._make(name, key, entry, by="run"))
                    continue
                except (ProjectConflict, ProjectInvalid, ProjectNotFound) as problem:
                    entry["case"], entry["problem"] = "draft_unverified", str(problem)
            with self.sessions.begin() as db:
                run = db.get_one(OrganizeRun, key)
                lines = self._lines(db, run.project_id)
                line = lines.get(("make", entry["about"]))
                if line is None or line.state != "open":
                    line = OrganizeLine(project_id=run.project_id, run_id=run.id)
                    db.add(line)
                case = str(entry["case"])
                line.run_id, line.listed, line.state, line.case = run.id, "needs_you", "open", case
                line.subject_kind, line.subject_id = "parts", UUID(entry["members"][0])
                line.title = str(entry["title"])[:300]
                line.said, line.sources = entry["said"], entry["sources"]
                line.candidates = entry["twins"]
                line.reason = (
                    f"It could not be made: {entry['problem']}"
                    if entry.get("problem")
                    else "An Epic was written. An Epic shapes the plan: it is made at your word."
                    if case == "draft_epic"
                    else DRAFT_SAID.get(case, "")
                )
                line.doubt = DOUBT[case]
                line.action = {
                    "op": "make",
                    "about": entry["about"],
                    "check": RECORD,
                    "review": entry["review"],
                    "items": entry["items"],
                    "members": entry["members"],
                    "count": len(entry["members"]),
                }
                needs += 1
        with self.sessions.begin() as db:
            run = db.get_one(OrganizeRun, key)
            est = dict(run.estimate or {})
            est["made"] = int(est.get("made", 0)) + made
            est["needs"] = int(est.get("needs", 0)) + needs
            run.estimate = est

    def _make(self, name: str, run_key: UUID, entry: dict[str, Any], *, by: str) -> list[UUID]:
        """Make the records that were written for a group of parts, list each under
        "Created", and count the parts as covered by them."""
        made: list[UUID] = []
        covering: list[dict[str, Any]] = []
        for item in entry["items"]:
            kind = str(item["kind"])
            parent = item.get("parent") or None
            under = UUID(str(parent["id"])) if parent else None
            if under is not None:
                table: Any = ProjectEpic if kind == "stories" else ProjectUserStory
                with self.sessions() as db:
                    alive = db.scalar(
                        select(table.id).where(
                            table.id == under,
                            table.archived_at.is_(None),
                            table.closed_at.is_(None),
                        )
                    )
                if alive is None:
                    raise ProjectConflict("The record it would go under is closed or gone.")
            elif kind != "epics":
                raise ProjectInvalid("It names no record to go under.")
            key = self.creator._save(
                name, kind, str(item["title"]), str(item["description"]), under, by
            )
            made.append(key)
            covering.append({"kind": kind, "id": str(key), "title": item["title"], "how": "made"})
            with self.sessions.begin() as db:
                run = db.get_one(OrganizeRun, run_key)
                db.add(
                    OrganizeLine(
                        run_id=run.id,
                        project_id=run.project_id,
                        listed="created",
                        state="applied",
                        case={"epics": "epic_made", "stories": "story_made"}.get(
                            kind, "issue_made"
                        ),
                        subject_kind=kind,
                        subject_id=key,
                        title=str(item["title"])[:300],
                        candidates=[],
                        said=entry.get("said") or [],
                        reason=(
                            "Written from the parts of the documentation it names, and every "
                            "sentence found supported by a second model. No record like it "
                            "exists in the project or the projects connected to it."
                            if by == "run"
                            else "Made at your word, as it was written."
                        ),
                        doubt=0.0,
                        action={
                            "op": "created",
                            "about": entry["about"],
                            "check": RECORD,
                            "review": entry["review"],
                            "by": by,
                            "members": entry["members"],
                            "for": {
                                "kind": "epics" if kind == "stories" else "stories",
                                "id": str(under),
                                "title": str(parent.get("title") or ""),
                            }
                            if parent
                            else None,
                            "description": item["description"],
                        },
                        made_kind=kind,
                        made_id=key,
                        sources=self.creator._sources_of(item),
                        answered_at=datetime.now(UTC),
                    )
                )
        with self.sessions.begin() as db:
            parent_choice.decide(db.get(ChoiceReview, UUID(str(entry["review"]))), True)
            for member in entry["members"]:
                part = db.get(DocPart, UUID(str(member)))
                if part is not None:
                    part.state, part.covered_by = "covered", covering
        return made

    # -- the lists, and what the administrator does with a line ---------------------

    def lines(self, name: str, listed: str, page: int = 1, page_size: int = PAGE) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self.sessions() as db:
            project = active_project(db, name)
            total, rows, page, pages = organize.list_page(
                db, project.id, KIND, listed, page, page_size
            )
            return {
                "list": listed,
                "name": organize.LISTS[listed],
                "total": total,
                "page": page,
                "pages": pages,
                "items": [
                    {
                        **organize.shown_line(line, now),
                        # A finding changes nothing in the plan: there is nothing to undo.
                        "can_undo": organize.can_undo(line, now)
                        and (line.action or {}).get("op") != "covered",
                    }
                    for line in rows
                ],
            }

    def answer(
        self, name: str, key: UUID, choice: str, record: UUID | None = None
    ) -> dict[str, str]:
        """The administrator's answer to a case. Of a part in doubt: a record covers it
        (``covered``), nothing does (``none``), or it is not about the product
        (``other``). Of what was written: make it (``make``) or leave it (``leave``).
        Of a record whose part is gone: keep it (``keep``) or take it away
        (``take_away``)."""
        with self.sessions() as db:
            project = active_project(db, name)
            line = organize.own_line(db, project.id, KIND, key)
            if line.listed != "needs_you" or line.state != "open":
                raise ProjectConflict("This case was decided already.")
            action = dict(line.action or {})
            offered = {str(entry.get("id")): dict(entry) for entry in line.candidates or []}
            run_key, said = line.run_id, list(line.said or [])
        op = str(action.get("op"))
        allowed = {
            "cover": ("covered", "none", "other"),
            "make": ("make", "leave"),
            "orphaned": ("keep", "take_away"),
        }.get(op, ())
        if choice not in allowed:
            raise ProjectInvalid("That is no answer to this case.")
        problem = None
        try:
            if op == "cover":
                if choice == "covered" and (record is None or str(record) not in offered):
                    raise ProjectInvalid("Choose one of the records it was offered.")
                with self.sessions.begin() as db:
                    review = db.get(ChoiceReview, UUID(str(action["review"])))
                    parent_choice.decide(review, choice == "covered")
                    part = (
                        db.get(DocPart, UUID(str(action["about"]))) if action.get("own") else None
                    )
                    if part is not None:
                        chosen = offered.get(str(record)) if record else None
                        if choice == "other":
                            part.said, part.state, part.covered_by = "other", "other", []
                        elif choice == "none":
                            part.state, part.covered_by = "none", []
                        elif chosen is not None:
                            part.state = "covered"
                            part.covered_by = [
                                {
                                    "kind": chosen.get("kind"),
                                    "id": chosen.get("id"),
                                    "title": chosen.get("title"),
                                    "project": chosen.get("project"),
                                    "ref": chosen.get("ref"),
                                    "how": "you",
                                }
                            ]
            elif op == "make" and choice == "make":
                self._make(name, run_key, {**action, "said": said}, by="you")
            elif op == "make":
                with self.sessions.begin() as db:
                    parent_choice.decide(db.get(ChoiceReview, UUID(str(action["review"]))), False)
            elif op == "orphaned" and choice == "take_away":
                self._undo(name, UUID(str(action["line"])))
        except (ProjectConflict, ProjectNotFound) as changed:
            problem = str(changed)
        with self.sessions.begin() as db:
            project = active_project(db, name)
            line = organize.own_line(db, project.id, KIND, key)
            now = datetime.now(UTC)
            if problem:
                line.state, line.answered_at = "skipped", now
                line.reason = f"{line.reason} Set aside: {problem}".strip()
                return {"id": str(key), "state": "skipped", "detail": problem}
            line.state, line.answer, line.answered_at = "answered", choice, now
            if op == "cover" and record is not None:
                line.candidates = [
                    {**entry, "yours": str(entry.get("id")) == str(record)}
                    for entry in line.candidates or []
                ]
        return {"id": str(key), "state": "answered"}

    def answer_many(self, name: str, group: str, choice: str) -> dict[str, int]:
        """One word for many: what was written and only waits for a word is made."""
        if group not in GROUPS or choice not in GROUPS[group][1]:
            raise ProjectInvalid("That cannot be answered for all of them at once.")
        with self.sessions() as db:
            project = active_project(db, name)
            keys = list(
                db.scalars(
                    select(OrganizeLine.id)
                    .where(
                        organize.lines_where(project.id, KIND),
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
                state = self.answer(name, key, choice)["state"]
                skipped += state == "skipped"
                answered += state != "skipped"
            except (ProjectConflict, ProjectInvalid, ProjectNotFound):
                skipped += 1
        return {"answered": answered, "skipped": skipped, "left": max(0, len(keys) - CHUNK)}

    def _undo(self, name: str, key: UUID) -> None:
        """Take a record away that was made for parts of the documentation. The parts
        stand again as covered by nothing, and it is not written again."""
        with self.sessions() as db:
            project = active_project(db, name)
            line = organize.own_line(db, project.id, KIND, key)
            if not organize.can_undo(line, datetime.now(UTC)) or line.made_id is None:
                raise ProjectConflict(
                    f"This can be undone for {organize.UNDO.days} days after it was applied."
                )
            action = dict(line.action or {})
            kind, made = str(line.made_kind), line.made_id
            if kind == "issues":
                item = db.get(BacklogItem, made)
                if item is not None and item.workspace_review != "pending":
                    raise ProjectConflict(
                        "This Issue was published to GitHub. It is closed there, not taken "
                        "away here."
                    )
        self.creator._remove(name, kind, made)
        with self.sessions.begin() as db:
            project = active_project(db, name)
            line = organize.own_line(db, project.id, KIND, key)
            line.state, line.undone_at = "undone", datetime.now(UTC)
            review = db.get(ChoiceReview, UUID(str(action["review"])))
            if review is not None and review.state == "answered":
                review.decision, review.decided_at = "dismissed", datetime.now(UTC)
            for member in action.get("members") or []:
                part = db.get(DocPart, UUID(str(member)))
                if part is None:
                    continue
                left = [e for e in part.covered_by or [] if str(e.get("id")) != str(made)]
                part.covered_by = left
                if not left:
                    part.state = "none"

    def undo(self, name: str, key: UUID) -> dict[str, str]:
        self._undo(name, key)
        return {"id": str(key)}

    def publish(self, name: str) -> dict[str, int]:
        """The Issues runs made and that are kept here are given to GitHub, together."""
        return self.creator.publish(name)

    def undo_run(self, name: str, key: UUID) -> dict[str, int]:
        """Every record a run made is taken away again, as far as it still can be."""
        with self.sessions() as db:
            project = active_project(db, name)
            run = db.get(OrganizeRun, key)
            if run is None or run.project_id != project.id or run.kind != KIND:
                raise ProjectNotFound(str(key))
            todo = list(
                db.scalars(
                    select(OrganizeLine.id).where(
                        OrganizeLine.run_id == run.id,
                        OrganizeLine.listed == "created",
                        OrganizeLine.state == "applied",
                    )
                )
            )
        undone = kept = 0
        for line in todo:
            try:
                self._undo(name, line)
                undone += 1
            except (ProjectConflict, ProjectInvalid, ProjectNotFound):
                kept += 1
        with self.sessions.begin() as db:
            db.get_one(OrganizeRun, key).undone_at = datetime.now(UTC)
        return {"undone": undone, "kept": kept}

    # -- the parts, and what the documentation leaves out ---------------------------

    def parts(self, name: str, show: str, page: int = 1, page_size: int = PAGE) -> dict[str, Any]:
        """The parts of the documentation, with what each is about and who said so, and
        what covers it: all, or those of one kind."""
        if show not in ("all", "product", "other", "open", "covered", "none", "doubt", "unchecked"):
            raise ProjectInvalid("That is no way to list the parts.")
        page, page_size = max(1, page), min(100, max(1, page_size))
        with self.sessions() as db:
            project = active_project(db, name)
            rows = [
                row
                for row in db.execute(
                    select(
                        DocPart.id,
                        DocPart.path,
                        DocPart.heading,
                        DocPart.words,
                        DocPart.about,
                        DocPart.said,
                        DocPart.state,
                        DocPart.covered_by,
                    )
                    .where(DocPart.project_id == project.id, DocPart.gone_at.is_(None))
                    .order_by(DocPart.path, DocPart.first_line)
                )
                if show == "all"
                or (what := effective(row[4], row[5], row[6])) == show
                or (
                    what == "product"
                    and show == (row[6] if row[6] in ("covered", "none", "doubt") else "unchecked")
                )
            ]
            total = len(rows)
            pages = max(1, -(-total // page_size))
            page = min(page, pages)
            return {
                "show": show,
                "total": total,
                "page": page,
                "pages": pages,
                "items": [
                    {
                        "id": str(key),
                        "path": path,
                        "heading": heading,
                        "words": words,
                        "about": effective(about, said, state),
                        "by": "you"
                        if said
                        else "rule"
                        if about != "open"
                        else "models"
                        if state != "unchecked"
                        else None,
                        "state": state,
                        "covered_by": by or [],
                    }
                    for key, path, heading, words, about, said, state, by in rows[
                        (page - 1) * page_size : page * page_size
                    ]
                ],
            }

    def say(self, name: str, key: UUID, about: str) -> dict[str, str]:
        """The administrator's word on what a part is about: the product, or not; or
        the word is taken back and the rule and the models say it again."""
        if about not in ("product", "other", "rule"):
            raise ProjectInvalid("A part is about the product, or it is not.")
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            part = db.get(DocPart, key)
            if part is None or part.project_id != project.id:
                raise ProjectNotFound(str(key))
            part.said = None if about == "rule" else about
            if about == "other":
                part.state, part.covered_by = "other", []
            elif part.state == "other":
                # Said to be about the product after all. The models saw the records
                # nearest to it and named none: nothing covers it, until a run says more.
                part.state = "none" if about == "product" else "unchecked"
            return {"id": str(key), "about": effective(part.about, part.said, part.state)}

    def silent(self, name: str, page: int = 1, page_size: int = PAGE) -> dict[str, Any]:
        """The open records of the project that the documentation does not speak of: no
        part is covered by them, and none lies close to them by meaning. Nothing is
        created for them: it is a list of what the documents leave out."""
        page, page_size = max(1, page), min(100, max(1, page_size))
        with self.sessions() as db:
            project = active_project(db, name)
            at = f"{id(self.sessions)}:{project.id}"
            mark = (
                _stamp(db, DocPart, project.id),
                _stamp(db, ProjectUserStory, project.id),
                _stamp(db, ProjectEpic, project.id),
                _stamp(db, BacklogItem, project.id),
            )
            now = time.monotonic()
            kept = _SILENT.get(at)
            if kept and kept[1] == mark and now - kept[0] < FRESH:
                found = kept[2]
            else:
                found = self._silent(db, project)
                _SILENT[at] = (now, mark, found)
            total = len(found)
            pages = max(1, -(-total // page_size))
            page = min(page, pages)
            parts = int(
                db.scalar(
                    select(func.count())
                    .select_from(DocPart)
                    .where(DocPart.project_id == project.id, DocPart.gone_at.is_(None))
                )
                or 0
            )
            return {
                "read": bool(parts),
                "total": total,
                "page": page,
                "pages": pages,
                "items": found[(page - 1) * page_size : page * page_size],
            }

    @staticmethod
    def _silent(db: Session, project: ProjectRecord) -> list[dict[str, Any]]:
        recs = coverage_parts.records(db, project)
        spoken: set[str] = set()
        vectors: list[Any] = []
        for by, blob in db.execute(
            select(DocPart.covered_by, DocPart.vector).where(
                DocPart.project_id == project.id, DocPart.gone_at.is_(None)
            )
        ):
            spoken.update(str(entry.get("id")) for entry in by or [])
            if blob:
                vectors.append(coverage_parts.vector_of(blob))
        if vectors and recs.table is not None and recs.table.shape[1] == vectors[0].shape[0]:
            table = numpy.stack(vectors)
            for start in range(0, len(recs.keys), 256):
                keys = recs.keys[start : start + 256]
                close = (recs.table[start : start + 256] @ table.T).max(axis=1)
                spoken.update(
                    key for key, value in zip(keys, close, strict=True) if value >= SPEAKS
                )
        order = {"epics": 0, "stories": 1, "issues": 2}
        return [
            {"id": rec.id, "kind": rec.kind, "title": rec.title, "ref": rec.ref}
            for rec in sorted(
                recs.rows.values(), key=lambda rec: (order[rec.kind], rec.title.casefold())
            )
            if rec.project is None and not rec.closed and rec.id not in spoken
        ]


__all__ = ["KIND", "SETTLED", "CoverageRun"]
