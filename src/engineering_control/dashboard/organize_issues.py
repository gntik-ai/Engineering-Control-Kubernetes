"""Organize, User Stories and Issues: linking behind one button (O1 of
docs/design/organize-and-coverage.md).

After a run every open Issue that is no bug is under a User Story or waits for the
administrator under "Needs you". The run puts the engines that exist in one order:

- **Read**: the open Issues, and which of them have no User Story. A bug is left out.
- **Match**: each Issue with none is offered the User Stories nearest to it, of the
  project and of the projects connected to it, by shared words and by meaning.
- **Verify**: a model is asked which Story the Issue belongs under, and a second model
  of another maker is asked the same without seeing the first answer (the check
  ``issue_story``). An answer is settled where both chose the same and the texts alone
  rank it first.
- **Done**: what is settled is applied where the project's record allows applying
  without the administrator, as a batch whose sample is judged afterwards. Everything
  else is a case under "Needs you", the most doubtful first: the administrator chooses
  a Story, says it is a bug, or leaves it.

Links that neither the scorer nor the plain rankings support are put under "Needs you"
as well: taking a link off is never done without the administrator.

Nothing is created here. An Issue that fits no Story waits; creating its Story is O3.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import organize, parent_choice, reorg, review_settings
from engineering_control.dashboard.connection_records import peers
from engineering_control.dashboard.organize_create import OPS, Creator
from engineering_control.dashboard.orphans import Orphans, open_issues
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.reorg import Reorg
from engineering_control.db.models import (
    BacklogItem,
    ChoiceReview,
    ChoiceRun,
    OrganizeLine,
    OrganizeRun,
    ProjectRecord,
    ProjectUserStory,
)

log = logging.getLogger(__name__)

KIND: Final = "stories_issues"
CHECK: Final = "issue_story"
LIVE: Final = ("running", "held")
# How doubtful a case is, by how its answer stands: what could not tell first, what
# both models and the texts agree on last.
DOUBT: Final = {
    "unclear": 0.9,
    "differ": 0.85,
    "nothing": 0.75,
    "single": 0.7,
    "unchecked": 0.65,
    "texts": 0.6,
    "back": 0.5,
    "settled": 0.2,
}
UNSUPPORTED: Final = 0.55
# How many of the Stories an Issue was offered a line names.
SHOWN: Final = 6
PAGE: Final = 25
STAGES: Final = organize.STAGES


def _runs(db: Session, project: UUID) -> list[OrganizeRun]:
    return list(
        db.scalars(
            select(OrganizeRun)
            .where(OrganizeRun.project_id == project, OrganizeRun.kind == KIND)
            .order_by(OrganizeRun.created_at.desc(), OrganizeRun.id)
        )
    )


def _lines(project: UUID) -> Any:
    """The lines of every run of this kind in the project."""
    return OrganizeLine.run_id.in_(
        select(OrganizeRun.id).where(OrganizeRun.project_id == project, OrganizeRun.kind == KIND)
    )


def _counts(db: Session, project: UUID) -> dict[str, int]:
    found = {"needs_you": 0, "done": 0, "created": 0}
    for listed, state, number in db.execute(
        select(OrganizeLine.listed, OrganizeLine.state, func.count())
        .where(_lines(project))
        .group_by(OrganizeLine.listed, OrganizeLine.state)
    ):
        if (listed == "needs_you" and state == "open") or (
            listed != "needs_you" and state == "applied"
        ):
            found[listed] = found.get(listed, 0) + int(number)
    return found


class IssuesRun:
    """The run behind the button "User Stories and Issues"."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        orphans: Orphans,
        changes: Reorg,
        creator: Creator,
    ) -> None:
        self.sessions = sessions
        self.orphans = orphans
        self.changes = changes
        # What goes on when the linking is done: the stage that creates.
        self.creator = creator

    # -- how things stand, and what a run would do ------------------------------------

    @staticmethod
    def _cases(db: Session, project: ProjectRecord) -> tuple[str | None, list[dict[str, Any]]]:
        """Every open Issue with no User Story: what it is offered, what was answered
        about it, and how that answer stands. ``ask`` says a run would ask about it."""
        spec = parent_choice.KINDS[CHECK]
        scope = spec.scope(db, project)
        refusal = scope.refusal()
        if refusal:
            return refusal, [
                {"issue": child, "listed": [], "review": None, "stands": "unasked", "ask": False}
                for child in scope.children()
            ]
        known = parent_choice.reviews(db, project.id, CHECK)
        cases = []
        for child in scope.children():
            listed = scope.shortlist(child)
            review = known.get(child.id)
            current = parent_choice.standing(scope, child, listed, review)
            ask = parent_choice.to_ask(review, current, False)
            stands = parent_choice.where(spec, child, review, current)
            if not stands:
                # No answer waits: nobody was asked, or the administrator set the
                # answer aside and left the Issue as it is.
                stands = "unasked" if ask else "left"
            cases.append(
                {"issue": child, "listed": listed, "review": review, "stands": stands, "ask": ask}
            )
        return None, cases

    def shown(self, name: str) -> dict[str, Any]:
        """Where the Issues of the project stand, what a run started now would do and
        cost, the run that is under way or the last one, and how many lines the lists
        hold."""
        unsupported = len(self.changes.ready(name, "unsupported"))
        with self.sessions() as db:
            project = active_project(db, name)
            refusal, cases = self._cases(db, project)
            counted, bugs = open_issues(db, project)
            stands: dict[str, int] = {}
            for case in cases:
                stands[case["stands"]] = stands.get(case["stands"], 0) + 1
            to_ask = sum(1 for case in cases if case["ask"])
            waits = sum(stands.get(key, 0) for key in (*parent_choice.NEEDS, "settled"))
            plan = parent_choice.planned(db, project.id, CHECK)
            each: float | None = None
            if plan["answer"]:
                each = parent_choice.question_cost(
                    db, CHECK, (plan["answer"]["provider"], plan["answer"]["model"])
                )
                if each is not None and plan["judge"]:
                    second = parent_choice.question_cost(
                        db, CHECK, (plan["judge"]["provider"], plan["judge"]["model"])
                    )
                    each = each + second if second is not None else None
            alone = reorg.alone_standing(db, project.id, "place")
            runs = _runs(db, project.id)
            run = runs[0] if runs else None
            live = run is not None and run.state in LIVE
            lists = _counts(db, project.id)
            # What keeps the models from being asked. Whatever needs no model is still
            # gathered: answers that wait, and links nothing supports.
            blocked = None
            if to_ask and not plan["answer"]:
                blocked = parent_choice.NO_ANSWERING_MODEL
            elif to_ask:
                blocked = review_settings.over(db, project.id)
            create = self.creator.plan(db, project, KIND)
            # What the stage that creates would do without asking, or may ask.
            creates = int(create["delivered"]) + (
                int(create["to_ask"]) if not create["blocked"] else 0
            )
            other = waits + unsupported
            why = None
            if live:
                why = "A run is under way."
            elif refusal and cases and not creates:
                why = refusal
            elif not (to_ask or other or create["to_ask"] or create["delivered"]):
                why = (
                    "Every open Issue is under a User Story and every User Story has "
                    "Issues: there is nothing to check."
                )
            elif (blocked or not to_ask) and not other and not creates:
                why = blocked or create["blocked"]
            issues = max(counted, len(cases))
            return {
                "project": project.name,
                "kind": KIND,
                # The sentence on top: every open Issue that is no bug is one of these.
                "stands": {
                    "issues": issues,
                    "settled": issues - len(cases),
                    "in_doubt": waits,
                    "none": len(cases) - waits,
                    "left": stands.get("left", 0),
                    "bugs_left_out": bugs,
                },
                "plan": {
                    "to_ask": to_ask,
                    # Why the models are not asked, where they are not.
                    "ask_blocked": blocked,
                    "answered": len(cases) - to_ask,
                    "settled": stands.get("settled", 0),
                    "unsupported": unsupported,
                    "answer": plan["answer"],
                    "judge": plan["judge"],
                    "first": plan["first"],
                    "tokens": plan["tokens"],
                    # What one question has cost with these models, and what asking
                    # about all of them is likely to cost: from the runs before.
                    "each": round(each, 6) if each is not None else None,
                    "dollars": round(each * to_ask, 2) if each is not None else None,
                    "scope": [peer.display_name for peer in peers(db, project.id)],
                    # Whether what is settled is applied without the administrator.
                    "applies": {
                        "in_force": bool(alone["in_force"]),
                        "setting": alone["setting"],
                        "why": alone["why"],
                    },
                },
                # What the stage that creates would do after the linking.
                "create": create,
                "can_start": why is None,
                "why_not": why,
                "run": self._shown_run(db, run) if run else None,
                "runs": len(runs),
                "lists": lists,
                # Of what needs you: the cases that one word answers together.
                "waiting": self.creator.waiting(db, project.id, KIND),
                "undo_days": organize.UNDO.days,
            }

    @staticmethod
    def _shown_run(db: Session, run: OrganizeRun) -> dict[str, Any]:
        counts = run.counts or {}
        reached = list(STAGES).index(run.stage) if run.stage in STAGES else 0
        asking = (
            db.get(ChoiceRun, UUID(str(run.estimate["choice_run"])))
            if (run.estimate or {}).get("choice_run")
            else None
        )
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
                "about": organize.ASKS.get(asking.kind, ""),
                "done": asking.done,
                "total": asking.total,
                "state": asking.state,
            }
            if asking is not None
            else None,
            "stages": [
                {
                    "stage": stage,
                    "name": title,
                    "reached": index <= reached,
                    **{
                        key: int((counts.get(stage) or {}).get(key, 0))
                        for key in ("records", "settled", "in_doubt", "none")
                    },
                }
                for index, (stage, title) in enumerate(STAGES.items())
            ],
        }

    # -- the run ----------------------------------------------------------------------

    def start(self, name: str) -> dict[str, str]:
        """The administrator's word: the Issues are checked and organized."""
        shown = self.shown(name)
        if not shown["can_start"]:
            raise ProjectConflict(str(shown["why_not"]))
        asking: str | None = None
        if shown["plan"]["to_ask"] and not shown["plan"]["ask_blocked"]:
            with self.sessions() as db:
                project = active_project(db, name)
                under_way = db.scalar(
                    select(ChoiceRun.id).where(
                        ChoiceRun.project_id == project.id,
                        ChoiceRun.kind == CHECK,
                        ChoiceRun.state.in_(parent_choice.LIVE),
                    )
                )
            # A check that is asking already, started elsewhere or by itself, is taken
            # into the run; otherwise the check is started with the project's models.
            asking = (
                str(under_way)
                if under_way
                else str(parent_choice.start(self.sessions, name, CHECK)["id"])
            )
        stands = shown["stands"]
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            if any(run.state in LIVE for run in _runs(db, project.id)):
                raise ProjectConflict("A run is under way.")
            run = OrganizeRun(
                project_id=project.id,
                kind=KIND,
                scope=[str(key) for key in organize.scope(db, project.id)],
                state="running",
                stage="verify",
                started_at=datetime.now(UTC),
                estimate={**shown["plan"], "choice_run": asking},
                counts={
                    "read": {
                        "records": stands["issues"],
                        "settled": stands["settled"],
                        "none": stands["issues"] - stands["settled"],
                    },
                    "match": {"records": stands["issues"] - stands["settled"]},
                    "verify": {"records": 0},
                },
            )
            db.add(run)
            db.flush()
            key = run.id
        if asking is None:
            # Nothing to ask: what was answered before is gathered at once, and the
            # stage that creates goes on from there.
            self._collect(name, key, "complete")
            self.creator.advance(name, key)
        return {"id": str(key)}

    def control(self, name: str, action: str) -> dict[str, str]:
        """Stop, resume or end the run that is under way: it is the check's asking
        that is stopped. Ended, the run keeps what was answered."""
        with self.sessions() as db:
            project = active_project(db, name)
            run = next((run for run in _runs(db, project.id) if run.state in LIVE), None)
            if run is None:
                raise ProjectConflict("No run is under way.")
            asking = (run.estimate or {}).get("choice_run")
            check = db.get(ChoiceRun, UUID(str(asking))) if asking else None
            kind = check.kind if check is not None else CHECK
            key = run.id
        if action == "end":
            with self.sessions.begin() as db:
                ending = db.get_one(OrganizeRun, key)
                ending.estimate = {**(ending.estimate or {}), "ended": True}
        if asking:
            try:
                parent_choice.control(self.sessions, name, kind, UUID(str(asking)), action)
            except ProjectConflict:
                if action != "end":
                    raise
        self.tick()
        return {"id": str(key)}

    def tick(self) -> bool:
        """Carry the runs that are under way one step on: follow the check's asking,
        and gather what it answered when it is over. True when a run ended."""
        ended = False
        with self.sessions() as db:
            waiting = [
                (run.id, run.project_id, (run.estimate or {}).get("choice_run"), run.stage)
                for run in db.scalars(
                    select(OrganizeRun).where(OrganizeRun.kind == KIND, OrganizeRun.state.in_(LIVE))
                )
            ]
        for key, project_id, asking, stage in waiting:
            if stage == "create":
                # The linking is done: the stage that creates carries the run on.
                with self.sessions() as db:
                    project = db.get(ProjectRecord, project_id)
                    name = project.name if project is not None else ""
                try:
                    ended = self.creator.advance(name, key) or ended
                except Exception:  # noqa: BLE001 - one project's run must not stop the others
                    log.exception("What Organize creates for %s could not be carried on", name)
                    with self.sessions.begin() as db:
                        failed = db.get_one(OrganizeRun, key)
                        failed.state, failed.ended_at = "failed", datetime.now(UTC)
                        failed.detail = "What was written could not be gathered. Start it again."
                continue
            with self.sessions.begin() as db:
                project = db.get(ProjectRecord, project_id)
                run = db.get(OrganizeRun, key)
                if project is None or run is None:
                    continue
                check = db.get(ChoiceRun, UUID(str(asking))) if asking else None
                name = project.name
                final = None
                if project.archived_at is not None:
                    run.state, run.detail = "failed", "The project was archived."
                    run.ended_at = datetime.now(UTC)
                    continue
                if check is None or check.state not in parent_choice.LIVE:
                    final = (
                        "stopped"
                        if check is not None and check.state == "cancelled"
                        else "complete"
                    )
                elif check.state in parent_choice.ASKING:
                    run.state, run.detail = "running", ""
                else:
                    run.state, run.detail = "held", check.detail or "The asking is stopped."
                if check is not None:
                    self._spent(run, check)
            if final:
                try:
                    self._collect(name, key, final)
                    ended = self.creator.advance(name, key) or ended
                except Exception:  # noqa: BLE001 - one project's run must not stop the others
                    log.exception("Organizing the Issues of %s could not be gathered", name)
                    with self.sessions.begin() as db:
                        run = db.get_one(OrganizeRun, key)
                        run.state, run.ended_at = "failed", datetime.now(UTC)
                        run.detail = "What was answered could not be gathered. Start it again."
        return ended

    @staticmethod
    def _spent(run: OrganizeRun, check: ChoiceRun) -> None:
        est = dict(run.estimate or {})
        organize.count_costs(run, est, check)
        run.estimate = est
        run.counts = {**(run.counts or {}), "verify": {"records": int(check.done or 0)}}

    def _line(self, db: Session, project: UUID, issue: UUID, op: str) -> OrganizeLine | None:
        """The line that still waits for this Issue, from whichever run wrote it."""
        return next(
            (
                line
                for line in db.scalars(
                    select(OrganizeLine).where(
                        _lines(project),
                        OrganizeLine.subject_id == issue,
                        OrganizeLine.state == "open",
                    )
                )
                if (line.action or {}).get("op") == op
            ),
            None,
        )

    def _collect(self, name: str, key: UUID, final: str) -> None:
        """What the run found, as lines: what was settled and may be applied is applied
        and listed under Done; every other answer is a case under Needs you."""
        applied: list[dict[str, Any]] = []
        made = self.changes.apply_alone(name, "place")
        if made is not None:
            applied = self.changes.applied(name, UUID(str(made["id"])))
        unsupported = self.changes.ready(name, "unsupported")
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            run = db.get_one(OrganizeRun, key)
            titles = dict(
                db.execute(
                    select(BacklogItem.id, BacklogItem.title).where(
                        BacklogItem.project_id == project.id
                    )
                )
                .tuples()
                .all()
            )
            numbers = dict(
                db.execute(
                    select(BacklogItem.id, BacklogItem.issue_number).where(
                        BacklogItem.project_id == project.id
                    )
                )
                .tuples()
                .all()
            )
            stories = {
                story.id: story
                for story in db.scalars(
                    select(ProjectUserStory).where(
                        ProjectUserStory.id.in_(
                            {entry["parent"] for entry in [*applied, *unsupported]}
                        )
                    )
                )
            }

            def story_of(parent: UUID) -> dict[str, Any]:
                story = stories.get(parent)
                home = db.get(ProjectRecord, story.project_id) if story else None
                return {
                    "id": str(parent),
                    "kind": "stories",
                    "title": story.title if story else "",
                    "project": home.display_name
                    if home is not None and home.id != project.id
                    else None,
                }

            for entry in applied:
                # A case of an earlier run that this one settled no longer waits.
                earlier = self._line(db, project.id, entry["child"], "place")
                if earlier is not None:
                    earlier.state = "skipped"
                target = story_of(entry["parent"])
                db.add(
                    OrganizeLine(
                        run_id=run.id,
                        project_id=project.id,
                        listed="done",
                        state="applied",
                        case="placed",
                        subject_kind="issues",
                        subject_id=entry["child"],
                        subject_ref=f"#{numbers.get(entry['child'])}"
                        if numbers.get(entry["child"])
                        else None,
                        title=str(titles.get(entry["child"]) or "")[:300],
                        candidates=[{**target, "chosen": True}],
                        said=[{"stage": "verify", "by": "two models", "answer": "the same"}],
                        reason=str(entry["reason"] or ""),
                        doubt=0.0,
                        action={
                            "op": "place",
                            "item": str(entry["child"]),
                            "story": str(entry["parent"]),
                            "reorg_line": str(entry["id"]),
                            "across": bool(target["project"]),
                        },
                        answered_at=datetime.now(UTC),
                    )
                )
            _, cases = self._cases(db, project)
            orphaned = {case["issue"].id for case in cases}
            waiting: set[UUID] = set()
            alone = reorg.alone_standing(db, project.id, "place")
            in_doubt = 0
            for case in cases:
                stands = case["stands"]
                if stands not in DOUBT:
                    continue
                issue, review, listed = case["issue"], case["review"], case["listed"]
                waiting.add(issue.id)
                in_doubt += 1
                line = self._line(db, project.id, issue.id, "place")
                if line is None:
                    line = OrganizeLine(project_id=project.id, run_id=run.id)
                    db.add(line)
                line.run_id = run.id
                line.listed, line.state, line.case = "needs_you", "open", stands
                line.subject_kind, line.subject_id = "issues", issue.id
                line.subject_ref = f"#{issue.issue_number}" if issue.issue_number else None
                line.title = issue.title[:300]
                line.candidates = self._candidates(listed, review)
                line.said = self._said(review, listed)
                line.reason = parent_choice.SAID.get(stands, "")
                if stands == "settled":
                    line.reason = (
                        "Two models chose the same User Story without seeing each other's "
                        "answer, and the texts alone rank it first. "
                        + (
                            "You took this link back before: it is not applied again by itself."
                            if alone["in_force"]
                            else "It is not applied by itself: "
                            + (alone["why"] or "the project is set to propose only.")
                        )
                    )
                line.doubt = DOUBT[stands]
                line.action = {
                    "op": "place",
                    "item": str(issue.id),
                    "story": str(review.parent_id) if review and review.parent_id else None,
                    "stands": stands,
                }
                line.answer = line.answered_at = line.undone_at = None
            # A case whose Issue has a Story by now, or is no orphan any more, is over.
            for line in db.scalars(
                select(OrganizeLine).where(_lines(project.id), OrganizeLine.state == "open")
            ):
                if (line.action or {}).get("op") != "place" or line.subject_id in waiting:
                    continue
                # What the administrator took back waits for them for as long as the
                # Issue has no User Story.
                if line.case == "undone" and line.subject_id in orphaned:
                    in_doubt += 1
                    continue
                line.state = "skipped"
            # Links nothing supports: taking one off is the administrator's.
            kept = {
                (str((line.action or {}).get("item")), str((line.action or {}).get("story")))
                for line in db.scalars(
                    select(OrganizeLine).where(_lines(project.id), OrganizeLine.answer == "keep")
                )
            }
            for entry in unsupported:
                pair = (str(entry["child"]), str(entry["parent"]))
                if pair in kept:
                    continue
                line = self._line(db, project.id, entry["child"], "unsupported")
                if line is None:
                    line = OrganizeLine(project_id=project.id, run_id=run.id)
                    db.add(line)
                line.run_id = run.id
                line.listed, line.state, line.case = "needs_you", "open", "unsupported"
                line.subject_kind, line.subject_id = "issues", entry["child"]
                number = numbers.get(entry["child"])
                line.subject_ref = f"#{number}" if number else None
                line.title = str(titles.get(entry["child"]) or "")[:300]
                line.candidates = [{**story_of(entry["parent"]), "linked": True}]
                line.said = [{"stage": "match", "by": "the scorer", "answer": "rules it out"}]
                line.reason = entry["why"] or (
                    "The scorer rules this link out, and neither shared words nor meaning "
                    "place the User Story among the Issue's first five."
                )
                line.doubt = UNSUPPORTED
                line.action = {"op": "unsupported", "item": pair[0], "story": pair[1]}
                in_doubt += 1
            left = sum(1 for case in cases if case["stands"] in ("unasked", "stale"))
            run.counts = {
                **(run.counts or {}),
                "verify": {
                    "records": int(((run.counts or {}).get("verify") or {}).get("records", 0)),
                    "settled": len(applied),
                    "in_doubt": in_doubt,
                    "none": left,
                },
                "create": {"records": 0},
            }
            run.detail = (
                f"{left} have no answer yet: they were not asked about in this run." if left else ""
            )
            # Ended by the administrator, the run stops here; otherwise the stage that
            # creates goes on with what has no counterpart.
            if final != "complete" or not self.creator.begin(db, run):
                run.state, run.stage, run.ended_at = final, "done", datetime.now(UTC)
        organize.forget(name)

    @staticmethod
    def _candidates(
        listed: list[dict[str, Any]], review: ChoiceReview | None
    ) -> list[dict[str, Any]]:
        """The Stories the Issue was offered, the first few, and whichever a model
        chose: each says which project it belongs to where that is not this one."""
        offered = (review.shortlist if review and review.shortlist else listed) or []
        chosen = str(review.parent_id) if review and review.parent_id else None
        judged = str(((review.judge if review else None) or {}).get("parent_id") or "") or None
        keep = [
            entry
            for place, entry in enumerate(offered)
            if place < SHOWN or str(entry.get("id")) in (chosen, judged)
        ]
        return [
            {
                "id": str(entry.get("id")),
                "kind": "stories",
                "title": str(entry.get("title") or ""),
                "epic": entry.get("epic"),
                "project": entry.get("project"),
                "words": list(entry.get("words") or [])[:5],
                "place": offered.index(entry) + 1,
                "chosen": str(entry.get("id")) == chosen,
                "judge": str(entry.get("id")) == judged,
            }
            for entry in keep
        ]

    @staticmethod
    def _said(review: ChoiceReview | None, listed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if review is None:
            return []
        titles = {str(entry.get("id")): str(entry.get("title") or "") for entry in listed}
        titles.update(
            {
                str(entry.get("id")): str(entry.get("title") or "")
                for entry in review.shortlist or []
            }
        )
        said = [
            {
                "stage": "verify",
                "by": review.model or "a model",
                "answer": review.answer,
                "story": titles.get(str(review.parent_id)) if review.parent_id else None,
                "reason": review.reason,
                "passages": [
                    {
                        "side": entry.get("side"),
                        "text": entry.get("text"),
                        "found": entry.get("found"),
                    }
                    for entry in review.evidence or []
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
                    "story": titles.get(str(judge.get("parent_id")))
                    if judge.get("parent_id")
                    else None,
                    "reason": judge.get("reason"),
                }
            )
        return said

    # -- the lists, and what the administrator does with a line -----------------------

    def lines(self, name: str, listed: str, page: int = 1, page_size: int = PAGE) -> dict[str, Any]:
        """One of the three lists, over every run of the project: what still waits, the
        most doubtful first; and what was done and still stands, the newest first."""
        if listed not in organize.LISTS:
            raise ProjectInvalid("That is no list of a run")
        now = datetime.now(UTC)
        page, page_size = max(1, page), min(100, max(1, page_size))
        with self.sessions() as db:
            project = active_project(db, name)
            where = [
                _lines(project.id),
                OrganizeLine.listed == listed,
                OrganizeLine.state == ("open" if listed == "needs_you" else "applied"),
            ]
            total = int(
                db.scalar(select(func.count()).select_from(OrganizeLine).where(*where)) or 0
            )
            order = (
                (OrganizeLine.doubt.desc(), OrganizeLine.created_at, OrganizeLine.id)
                if listed == "needs_you"
                else (OrganizeLine.answered_at.desc(), OrganizeLine.id)
            )
            rows = db.scalars(
                select(OrganizeLine)
                .where(*where)
                .order_by(*order)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            url = f"https://github.com/{project.source_repository}/issues/"
            items = []
            for line in rows:
                shown = organize.shown_line(line, now)
                number = (line.subject_ref or "").lstrip("#")
                shown["subject"]["url"] = url + number if number.isdigit() else None
                items.append(shown)
            return {
                "list": listed,
                "name": organize.LISTS[listed],
                "total": total,
                "page": page,
                "pages": max(1, -(-total // page_size)),
                "items": items,
            }

    def _own(self, db: Session, name: str, key: UUID) -> OrganizeLine:
        project = active_project(db, name)
        line = db.get(OrganizeLine, key)
        run = db.get(OrganizeRun, line.run_id) if line else None
        if line is None or run is None or run.project_id != project.id or run.kind != KIND:
            raise ProjectNotFound(str(key))
        return line

    def answer(
        self, name: str, key: UUID, choice: str, story: UUID | None = None, epic: UUID | None = None
    ) -> dict[str, str]:
        """The administrator's answer to a case: the Issue goes under that User Story
        (``story``), is a bug (``bug``) or is left as it is (``leave``); a link nothing
        supports is taken off (``take_off``) or kept (``keep``). Where the records
        changed since, the case is set aside and says so."""
        with self.sessions() as db:
            line = self._own(db, name, key)
            if line.listed != "needs_you" or line.state != "open":
                raise ProjectConflict("This case was decided already.")
            action = dict(line.action or {})
            offered = {str(entry.get("id")): entry for entry in line.candidates or []}
            reason = line.reason
        if action.get("op") in OPS:
            return self.creator.answer(name, KIND, key, choice, epic=epic)
        item = UUID(str(action["item"]))
        listed, state, before, problem = "needs_you", "answered", None, None
        try:
            if action["op"] == "unsupported":
                if choice == "take_off":
                    before = self.changes.apply_one(
                        name, "unsupported", item, UUID(str(action["story"])), reason
                    )
                    listed, state = "done", "applied"
                elif choice != "keep":
                    raise ProjectInvalid("Take the link off, or keep it.")
            elif choice == "story":
                if story is None or str(story) not in offered:
                    raise ProjectInvalid("Choose one of the User Stories it was offered.")
                self.orphans.link(name, item, story)
                action.update(
                    story=str(story), across=bool(offered[str(story)].get("project")), by="you"
                )
                listed, state = "done", "applied"
            elif choice == "bug":
                self.orphans.bug(name, item)
            elif choice == "leave":
                try:
                    self.orphans.dismiss(name, item)
                except ProjectConflict:
                    pass
            else:
                raise ProjectInvalid("Choose a User Story, say it is a bug, or leave it.")
        except ProjectConflict as changed:
            problem = str(changed)
        with self.sessions.begin() as db:
            line = self._own(db, name, key)
            now = datetime.now(UTC)
            if problem:
                line.state, line.answered_at = "skipped", now
                line.reason = f"{line.reason} Set aside: {problem}".strip()
                return {"id": str(key), "state": "skipped", "detail": problem}
            line.listed, line.state, line.answer, line.answered_at = listed, state, choice, now
            line.action, line.before = action, {"links": before} if before is not None else None
            if listed == "done":
                line.case = "placed" if action["op"] == "place" else "taken_off"
                line.candidates = [
                    {**entry, "chosen": str(entry.get("id")) == action.get("story")}
                    for entry in line.candidates or []
                ]
        organize.forget(name)
        return {"id": str(key), "state": state}

    def _undo(self, name: str, key: UUID) -> None:
        with self.sessions() as db:
            line = self._own(db, name, key)
            if not organize.can_undo(line, datetime.now(UTC)):
                raise ProjectConflict(
                    f"This can be undone for {organize.UNDO.days} days after it was applied."
                )
            action, before = dict(line.action or {}), (line.before or {}).get("links")
        if action.get("op") in OPS:
            self.creator.undo(name, KIND, key)
            organize.forget(name)
            return
        item, story = UUID(str(action["item"])), UUID(str(action["story"]))
        if action["op"] == "unsupported":
            self.changes.revert_one(name, "unsupported", item, story, before)
        elif action.get("reorg_line"):
            self.changes.undo_line(name, UUID(str(action["reorg_line"])))
        else:
            self.changes.revert_one(
                name, "place", item, story, [{"across": True}] if action.get("across") else []
            )
        with self.sessions.begin() as db:
            line = self._own(db, name, key)
            # What was applied is taken back, and the case waits for the administrator.
            line.listed, line.state, line.answer = "needs_you", "open", None
            line.undone_at = datetime.now(UTC)
            line.doubt = max(line.doubt, DOUBT["texts"])
            line.case = "undone" if action["op"] == "place" else "unsupported"
            line.reason = "You took this back. " + (
                "The Issue has no User Story again."
                if action["op"] == "place"
                else "The link stands again."
            )
        organize.forget(name)

    def answer_many(self, name: str, group: str, choice: str) -> dict[str, int]:
        """One word for many cases of the stage that creates."""
        found = self.creator.answer_many(name, KIND, group, choice)
        organize.forget(name)
        return found

    def publish(self, name: str) -> dict[str, int]:
        """The Issues a run made and that are kept here are given to GitHub, together."""
        return self.creator.publish(name)

    def undo(self, name: str, key: UUID) -> dict[str, str]:
        self._undo(name, key)
        return {"id": str(key)}

    def undo_run(self, name: str, key: UUID) -> dict[str, int]:
        """Everything a run applied is taken back, as far as it still can be."""
        with self.sessions() as db:
            project = active_project(db, name)
            run = db.get(OrganizeRun, key)
            if run is None or run.project_id != project.id or run.kind != KIND:
                raise ProjectNotFound(str(key))
            todo = list(
                db.scalars(
                    select(OrganizeLine.id).where(
                        OrganizeLine.run_id == run.id, OrganizeLine.state == "applied"
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
