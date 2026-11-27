"""Organize, Epics and User Stories: linking behind one button (O2 of
docs/design/organize-and-coverage.md).

After a run every open User Story has the one Epic it belongs to, or waits for the
administrator under "Needs you". The run puts the engines that exist in one order:

- **Read**: the open User Stories and the Epics each is under. A Story whose own text
  names one of its Epics has that Epic made its one Epic by rule.
- **Verify, the one Epic**: for a Story under several Epics, a model says which it
  belongs to and a second model of another maker says the same unseen (the check
  ``story_epic``). What is settled is applied where the project's record allows.
- **Verify, the other Epics**: for each Epic a Story is under beside its one Epic, the
  two models say whether the Story has to do with it (the check ``epic_links``). A link
  both call related stays, as "also related". A link both call unrelated, and one they
  differ on, is put under "Needs you": a link is never taken off without the
  administrator.
- **The only Epic**: a Story under one Epic, or under none, is compared with every Epic
  of the project by shared words and by meaning. Where both point to another Epic and
  neither places its own among the first five, it is put under "Needs you". No model is
  asked about it.

A Story's one Epic is an Epic of its own project. What a Story has to do with the Epics
of a connected project is a link of the connection, not a parent.

Nothing is created here. An Epic with no Story, and Stories that fit no Epic, are O3.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import (
    epic_links,
    organize,
    parent_choice,
    reorg,
    review_settings,
)
from engineering_control.dashboard.connection_plan import Across
from engineering_control.dashboard.connection_records import catalog
from engineering_control.dashboard.connections import link_story_across, unlink_issue_across
from engineering_control.dashboard.epic_links import EpicLinks
from engineering_control.dashboard.organize_create import GROUPS as CREATE_GROUPS
from engineering_control.dashboard.organize_create import OPS, Creator
from engineering_control.dashboard.planning import PlanningRecords
from engineering_control.dashboard.planning_links import Note, PlanningLinks
from engineering_control.dashboard.primary_epics import PrimaryEpics
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.reorg import Reorg
from engineering_control.db.models import (
    ChoiceReview,
    ChoiceRun,
    ConnectionFinding,
    OrganizeLine,
    OrganizeRun,
    PlanningLink,
    ProjectConnection,
    ProjectEpic,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    ReorgBatch,
    ReorgLine,
)

log = logging.getLogger(__name__)

KIND: Final = "epics_stories"
ONE: Final = "story_epic"
OTHERS: Final = "epic_links"
LIVE: Final = ("running", "held")
# How doubtful a case is. The one Epic: as for an Issue's Story. Another Epic: what the
# models differ on first, what both call unrelated last, since both say the same.
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
LINK: Final = {"differ": 0.8, "unclear": 0.7, "unchecked": 0.65, "alone": 0.6, "unrelated": 0.3}
# The link cases that can be answered together, with one word: those both models call
# unrelated, taken off or kept; and those in doubt, kept.
GROUPS: Final = {
    "unrelated": (("link_unrelated",), ("take_off", "keep")),
    "in_doubt": (("link_differ", "link_unclear", "link_unchecked", "link_alone"), ("keep",)),
}
# How many lines one request answers: the page asks again until none is left.
CHUNK: Final = 25
ONLY: Final = 0.45
# A Story's own Epic has to be this far down both rankings to be called in question:
# not among the first five, or the first third where the project has few Epics.
FAR: Final = 5
# What the texts say of the Stories under one Epic is kept this long: reading every
# vector of a project for each look at the page would be slow.
FRESH: Final = 300.0
_FLAGS: dict[str, tuple[float, Any, list[dict[str, Any]]]] = {}
SHOWN: Final = 6
PAGE: Final = 25
STATED: Final = "The User Story's own text names this Epic."


class EpicsRun:
    """The run behind the button "Epics and User Stories"."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        primary: PrimaryEpics,
        others: EpicLinks,
        changes: Reorg,
        planning: PlanningRecords,
        links: PlanningLinks,
        creator: Creator,
    ) -> None:
        self.sessions = sessions
        self.primary, self.others, self.changes = primary, others, changes
        self.planning, self.links = planning, links
        # What goes on when the linking is done: the stage that creates.
        self.creator = creator

    # -- how things stand ---------------------------------------------------------------

    @staticmethod
    def _read(db: Session, project: ProjectRecord) -> dict[str, Any]:
        """The open User Stories, the open Epics each is under, the Stories whose one
        Epic the administrator chose, and those that have no Epic of their own project
        and stand under an Epic of a connected one by a link of the connection."""
        epics = dict(
            db.execute(
                select(ProjectEpic.id, ProjectEpic.title).where(
                    ProjectEpic.project_id == project.id,
                    ProjectEpic.archived_at.is_(None),
                    ProjectEpic.closed_at.is_(None),
                )
            )
            .tuples()
            .all()
        )
        stories = dict(
            db.execute(
                select(ProjectUserStory.id, ProjectUserStory.title).where(
                    ProjectUserStory.project_id == project.id,
                    ProjectUserStory.archived_at.is_(None),
                    ProjectUserStory.closed_at.is_(None),
                )
            )
            .tuples()
            .all()
        )
        under: dict[UUID, list[UUID]] = {key: [] for key in stories}
        for story, epic in db.execute(
            select(ProjectStoryEpic.story_id, ProjectStoryEpic.epic_id).where(
                ProjectStoryEpic.project_id == project.id
            )
        ):
            if story in under and epic in epics:
                under[story].append(epic)
        chosen = set(
            db.scalars(
                select(PlanningLink.child_id).where(
                    PlanningLink.project_id == project.id,
                    PlanningLink.kind == "story_epic",
                    PlanningLink.removed_at.is_(None),
                    PlanningLink.is_primary,
                    PlanningLink.primary_by == "administrator",
                )
            )
        )
        none = [key for key, mine in under.items() if not mine]
        across: dict[UUID, UUID] = {}
        if none:
            for story, epic in db.execute(
                select(ConnectionFinding.source_id, ConnectionFinding.target_id)
                .join(ProjectConnection, ProjectConnection.id == ConnectionFinding.connection_id)
                .join(ProjectEpic, ProjectEpic.id == ConnectionFinding.target_id)
                .where(
                    ConnectionFinding.source_project == project.id,
                    ConnectionFinding.source_id.in_(none),
                    ConnectionFinding.action == "link",
                    ConnectionFinding.status == "approved",
                    ProjectConnection.archived_at.is_(None),
                    ProjectEpic.archived_at.is_(None),
                    ProjectEpic.closed_at.is_(None),
                )
            ):
                across[story] = epic
        return {
            "epics": epics,
            "stories": stories,
            "under": under,
            "chosen": chosen,
            "across": across,
        }

    @staticmethod
    def _cases(db: Session, project: ProjectRecord) -> list[dict[str, Any]]:
        """Every Story under several Epics whose one Epic is not chosen: what it is
        offered, what was answered, and how that answer stands."""
        spec = parent_choice.KINDS[ONE]
        scope = spec.scope(db, project)
        known = parent_choice.reviews(db, project.id, ONE)
        cases = []
        for child in scope.children():
            listed = scope.shortlist(child)
            review = known.get(child.id)
            current = parent_choice.standing(scope, child, listed, review)
            ask = parent_choice.to_ask(review, current, False)
            stands = parent_choice.where(spec, child, review, current)
            if not stands:
                stands = "unasked" if ask else "left"
            cases.append(
                {"story": child, "listed": listed, "review": review, "stands": stands, "ask": ask}
            )
        return cases

    @staticmethod
    def _links(
        db: Session, project: ProjectRecord, chosen: set[UUID], soon: set[UUID]
    ) -> tuple[int, list[dict[str, Any]]]:
        """The Epics a Story is under beside its one Epic, as two models judged them:
        how many Stories still wait to be asked about, and every judged link that is
        not settled, with what each model said. A link both call related is settled.

        Only for a Story whose one Epic is chosen (or, for the count, will be by rule
        when the run starts): what its other Epics are to it is asked once that is
        known, since the answer is about the Epics beside that one."""
        scope = epic_links._Scope(db, project)
        known = parent_choice.reviews(db, project.id, OTHERS)
        to_ask = 0
        for child in scope.children():
            if child.id not in chosen and child.id not in soon:
                continue
            listed = scope.shortlist(child)
            review = known.get(child.id)
            current = parent_choice.standing(scope, child, listed, review)
            to_ask += parent_choice.to_ask(review, current, False)
        found = []
        for story, review, entry, stands in epic_links._findings(db, project, scope):
            if not stands or story.id not in chosen:
                continue
            first = str(entry.get("answer"))
            second = epic_links.second_opinion(review, entry)
            if first == "part" and second in (None, "part"):
                continue
            if first == "unclear" and second in (None, "unclear"):
                case = "unclear"
            elif first == "unrelated" and not entry.get("found"):
                # It would be taken off on a passage that is not in the Epic's text.
                case = "unchecked"
            elif first == "unrelated" and second == "unrelated":
                case = "unrelated"
            elif first == "unrelated" and second is None:
                case = "alone"
            else:
                case = "differ"
            found.append(
                {
                    "story": story,
                    "epic": UUID(str(entry["epic"])),
                    "title": str(entry.get("title") or ""),
                    "review": review,
                    "entry": entry,
                    "second": second,
                    "case": case,
                }
            )
        return to_ask, found

    def _only(
        self, db: Session, project: ProjectRecord, read: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """The Stories under one Epic that both rankings place elsewhere, and those
        under none: each with the Epics nearest to it. A Story under one Epic is not
        called in question without the ranking service: shared words alone say too
        little. A Story under none is offered the Epics of the whole scope, each of a
        connected project with its project."""
        few = sorted(
            (
                key
                for key, epics in read["under"].items()
                if len(epics) < 2 and key not in read["across"]
            ),
            key=str,
        )
        if not few:
            return []
        mark = (tuple((key, tuple(read["under"][key])) for key in few), len(read["epics"]))
        at = f"{id(self.sessions)}:{project.id}"
        kept, now = _FLAGS.get(at), time.monotonic()
        if kept and kept[1] == mark and now - kept[0] < FRESH:
            return kept[2]
        far = min(FAR, max(2, len(read["epics"]) // 3))
        mine = catalog(db, project, planning_only=True)
        across = Across.read(db, [mine, mine])
        found = []
        for key in few:
            item = mine.local.get(key)
            if item is None:
                continue
            own = read["under"][key][0] if read["under"][key] else None
            if own is None:
                found.append(
                    {
                        "story": key,
                        "title": read["stories"][key],
                        "own": None,
                        "far": far,
                        "near": self._nearest(db, project, key),
                    }
                )
                continue
            ranked = [
                entry for entry in across.ranked(0, item, "epics") if entry[0].id in read["epics"]
            ]
            by_meaning = [
                e[0].id for e in sorted(ranked, key=lambda e: -(e[1] or 0.0)) if e[1] is not None
            ]
            by_words = [e[0].id for e in sorted(ranked, key=lambda e: -e[2]) if e[2]]
            if (
                not by_meaning
                or not by_words
                or by_meaning[0] != by_words[0]
                or own in by_meaning[:far]
                or own in by_words[:far]
            ):
                continue
            near = [
                {
                    "id": str(epic.id),
                    "kind": "epics",
                    "title": epic.title,
                    "closeness": round(close, 3) if close is not None else None,
                    "shared": shared,
                    "current": False,
                }
                for epic, close, shared in [e for e in ranked if e[0].id != own][:3]
            ]
            near.append(
                {
                    "id": str(own),
                    "kind": "epics",
                    "title": read["epics"][own],
                    "closeness": None,
                    "shared": 0,
                    "current": True,
                }
            )
            found.append(
                {"story": key, "title": read["stories"][key], "own": own, "far": far, "near": near}
            )
        _FLAGS[at] = (now, mark, found)
        return found

    @staticmethod
    def _nearest(db: Session, project: ProjectRecord, story: UUID) -> list[dict[str, Any]]:
        """The open Epics nearest to a Story in the whole scope: its own project's, and
        those of the projects connected to it, each naming its project."""
        try:
            offered = organize.candidates_in(db, project, story, 3 * SHOWN)["candidates"]
        except ProjectNotFound:
            return []
        keys = [UUID(entry["id"]) for entry in offered if entry["id"]]
        still = set(
            db.scalars(
                select(ProjectEpic.id).where(
                    ProjectEpic.id.in_(keys),
                    ProjectEpic.archived_at.is_(None),
                    ProjectEpic.closed_at.is_(None),
                )
            )
        )
        return [
            {
                "id": entry["id"],
                "kind": "epics",
                "title": entry["title"],
                "closeness": entry["closeness"],
                "shared": entry["shared"],
                "current": False,
                **({} if entry["own"] else {"project": entry["project"], "label": entry["label"]}),
            }
            for entry in offered
            if entry["id"] and UUID(entry["id"]) in still
        ][:SHOWN]

    def _kept(self, db: Session, project: UUID) -> set[tuple[str, str, str]]:
        """What the administrator said to keep: it is not put before them again. A link
        whose taking off they judged wrong in a batch is kept too."""
        judged = {
            ("epic_link", str(child), str(parent))
            for child, parent in db.execute(
                select(ReorgLine.child_id, ReorgLine.parent_id)
                .join(ReorgBatch, ReorgBatch.id == ReorgLine.batch_id)
                .where(
                    ReorgLine.project_id == project,
                    ReorgBatch.kind == "extra_epics",
                    ReorgLine.verdict == "wrong",
                )
            )
        }
        return judged | {
            (
                str((line.action or {}).get("op")),
                str((line.action or {}).get("story")),
                str((line.action or {}).get("epic") or "")
                if (line.action or {}).get("op") == "epic_link"
                else "",
            )
            for line in db.scalars(
                select(OrganizeLine).where(
                    organize.lines_where(project, KIND), OrganizeLine.answer == "keep"
                )
            )
        }

    def shown(self, name: str) -> dict[str, Any]:
        """Where the User Stories stand to the Epics, what a run started now would do
        and cost, the run under way or the last one, and how many lines the lists hold."""
        with self.sessions() as db:
            project = active_project(db, name)
            read = self._read(db, project)
            cases = self._cases(db, project)
            links_to_ask, links = self._links(
                db,
                project,
                read["chosen"],
                {case["story"].id for case in cases if case["story"].stated},
            )
            kept = self._kept(db, project.id)
            links = [
                entry
                for entry in links
                if ("epic_link", str(entry["story"].id), str(entry["epic"])) not in kept
            ]
            only = [
                entry
                for entry in self._only(db, project, read)
                if ("epic_of", str(entry["story"]), "") not in kept
            ]
            under = read["under"]
            one = sum(1 for epics in under.values() if len(epics) == 1)
            across = len(read["across"])
            none = sum(1 for epics in under.values() if not epics) - across
            several = len(under) - one - none - across
            chosen = sum(
                1 for key, epics in under.items() if len(epics) > 1 and key in read["chosen"]
            )
            stated = sum(1 for case in cases if case["story"].stated)
            to_ask = sum(1 for case in cases if case["ask"] and not case["story"].stated)
            waits = sum(1 for case in cases if case["stands"] in DOUBT)
            left = sum(1 for case in cases if case["stands"] == "left")
            flagged = sum(1 for entry in only if entry["own"] is not None)
            plan = parent_choice.planned(db, project.id, ONE)

            def each(check: str) -> float | None:
                if not plan["answer"]:
                    return None
                cost = parent_choice.question_cost(
                    db, check, (plan["answer"]["provider"], plan["answer"]["model"])
                )
                if cost is not None and plan["judge"]:
                    second = parent_choice.question_cost(
                        db, check, (plan["judge"]["provider"], plan["judge"]["model"])
                    )
                    cost = cost + second if second is not None else None
                return cost

            costs = {ONE: each(ONE), OTHERS: each(OTHERS)}
            asked = {ONE: to_ask, OTHERS: links_to_ask}
            priced = all(costs[check] is not None for check in asked if asked[check])
            dollars = (
                round(sum((costs[check] or 0.0) * asked[check] for check in asked), 2)
                if priced
                else None
            )
            blocked = None
            if (to_ask or links_to_ask) and not plan["answer"]:
                blocked = parent_choice.NO_ANSWERING_MODEL
            elif to_ask or links_to_ask:
                blocked = review_settings.over(db, project.id)
            alone = reorg.alone_standing(db, project.id, "primary")
            runs = organize.runs_of(db, project.id, KIND)
            run = runs[0] if runs else None
            live = run is not None and run.state in LIVE
            create = self.creator.plan(db, project, KIND)
            # What the stage that creates would do without asking, or may ask.
            creates = int(create["splits"]) + (
                int(create["to_ask"]) if not create["blocked"] else 0
            )
            other = waits + stated + len(links) + len(only)
            why = None
            if live:
                why = "A run is under way."
            elif not read["stories"] and not create["to_ask"]:
                why = "The project has no open User Story."
            elif not (to_ask or links_to_ask or other or create["to_ask"] or create["splits"]):
                why = "Every open User Story has its one Epic: there is nothing to check."
            elif (blocked or not (to_ask or links_to_ask)) and not other and not creates:
                why = blocked or create["blocked"]
            without = sum(1 for epic in read["epics"] if not any(epic in e for e in under.values()))
            return {
                "project": project.name,
                "kind": KIND,
                # The sentence on top: every open User Story is one of these.
                "stands": {
                    "stories": len(under),
                    "settled": one - flagged + chosen + across,
                    "in_doubt": waits + flagged,
                    "unchecked": several - chosen - waits - left,
                    # Under several Epics, and you said to leave it so.
                    "left": left,
                    "none": none,
                    # Of the settled: under an Epic of a connected project.
                    "across": across,
                    "epics": len(read["epics"]),
                    # Epics that hold no open User Story: making their Stories is O3.
                    "epics_with_no_story": without,
                },
                "plan": {
                    "to_ask": to_ask,
                    "ask_blocked": blocked,
                    "stated": stated,
                    "settled": sum(1 for case in cases if case["stands"] == "settled"),
                    "links_to_ask": links_to_ask,
                    "links_unrelated": sum(1 for e in links if e["case"] == "unrelated"),
                    "links_in_doubt": sum(1 for e in links if e["case"] != "unrelated"),
                    "only": len(only),
                    "answer": plan["answer"],
                    "judge": plan["judge"],
                    "tokens": plan["tokens"],
                    "dollars": dollars,
                    "applies": {
                        "in_force": bool(alone["in_force"]),
                        "setting": alone["setting"],
                        "why": alone["why"],
                    },
                },
                "can_start": why is None,
                "why_not": why,
                "run": self._shown_run(db, run) if run else None,
                "runs": len(runs),
                # What the stage that creates would do after the linking.
                "create": create,
                "lists": organize.list_counts(db, project.id, KIND),
                # Of what needs you: the cases that can be answered together.
                "waiting": {
                    **self._waiting(db, project.id),
                    **self.creator.waiting(db, project.id, KIND),
                },
                "undo_days": organize.UNDO.days,
            }

    @staticmethod
    def _waiting(db: Session, project: UUID) -> dict[str, int]:
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
            # Which of the two checks is asking, and how far it is.
            "asking": {
                "about": organize.ASKS.get(asking.kind, ""),
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
        """The administrator's word: the User Stories are checked against the Epics."""
        shown = self.shown(name)
        if not shown["can_start"]:
            raise ProjectConflict(str(shown["why_not"]))
        stands, plan = shown["stands"], shown["plan"]
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            if any(run.state in LIVE for run in organize.runs_of(db, project.id, KIND)):
                raise ProjectConflict("A run is under way.")
            run = OrganizeRun(
                project_id=project.id,
                kind=KIND,
                scope=[str(project.id)],
                state="running",
                stage="verify",
                started_at=datetime.now(UTC),
                estimate={
                    **plan,
                    "step": "one",
                    "choice_run": None,
                    "begun": False,
                    "ended": False,
                    "costs": {},
                    "asked": 0,
                    "blocked": bool(plan["ask_blocked"]),
                },
                counts={
                    "read": {
                        "records": stands["stories"],
                        "settled": stands["settled"],
                        "none": stands["none"],
                    },
                    "match": {"records": stands["stories"] - stands["settled"]},
                    "verify": {"records": 0},
                },
            )
            db.add(run)
            db.flush()
            key = run.id
        self._by_rule(name, key)
        self._advance(name, key)
        return {"id": str(key)}

    def _by_rule(self, name: str, key: UUID) -> None:
        """A Story whose own text names one of its Epics belongs to that Epic: it is
        made its one Epic without asking a model, and can be undone."""
        with self.sessions() as db:
            project = active_project(db, name)
            stated = [
                (case["story"].id, case["story"].title, case["story"].stated)
                for case in self._cases(db, project)
                if case["story"].stated
            ]
            titles = self._read(db, project)["epics"]
        for story, title, epic in stated:
            before = self.links.snapshot(name, "stories", story)
            try:
                self.links.set_primary(
                    name, "stories", story, epic, Note("declared", confirmed=True, reason=STATED)
                )
            except (ProjectConflict, ProjectInvalid, ProjectNotFound):
                continue
            with self.sessions.begin() as db:
                run = db.get_one(OrganizeRun, key)
                db.add(
                    OrganizeLine(
                        run_id=run.id,
                        project_id=run.project_id,
                        listed="done",
                        state="applied",
                        case="one_epic",
                        subject_kind="stories",
                        subject_id=story,
                        title=title[:300],
                        candidates=[
                            {
                                "id": str(epic),
                                "kind": "epics",
                                "title": titles.get(epic, ""),
                                "chosen": True,
                            }
                        ],
                        said=[{"stage": "read", "by": "the text", "answer": "names it"}],
                        reason=STATED,
                        doubt=0.0,
                        action={"op": "primary", "story": str(story), "epic": str(epic)},
                        before={"links": before},
                        answered_at=datetime.now(UTC),
                    )
                )

    def _begin(self, name: str, check: str) -> str | None:
        """Have the check ask, with the project's models: a run of it that is asking
        already is taken into this one. None where it has nothing to ask."""
        with self.sessions() as db:
            project = active_project(db, name)
            under_way = db.scalar(
                select(ChoiceRun.id).where(
                    ChoiceRun.project_id == project.id,
                    ChoiceRun.kind == check,
                    ChoiceRun.state.in_(parent_choice.LIVE),
                )
            )
        if under_way:
            return str(under_way)
        try:
            if check == ONE:
                return str(self.primary.start(name, "", "", None)["id"])
            # What a Story's other Epics are to it is asked once its one Epic is chosen.
            self.links.reconcile_project(name)
            with self.sessions() as db:
                chosen = self._read(db, active_project(db, name))["chosen"]
            started = parent_choice.start(
                self.sessions, name, OTHERS, only=lambda child, _listed: child.id in chosen
            )
            return str(started["id"])
        except (ProjectInvalid, ProjectConflict) as nothing:
            log.info("Organize did not start %s for %s: %s", check, name, nothing)
            return None

    def _advance(self, name: str, key: UUID) -> bool:
        """Carry the run on: follow the check that is asking, gather what it answered
        when it is over, and go on to the next. True when the run ended."""
        for _ in range(6):
            with self.sessions.begin() as db:
                run = db.get_one(OrganizeRun, key)
                if run.state not in LIVE:
                    return True
                created = run.stage == "create"
            if created:
                # The linking is done: the stage that creates carries the run on.
                return self.creator.advance(name, key)
            with self.sessions.begin() as db:
                run = db.get_one(OrganizeRun, key)
                est = dict(run.estimate or {})
                check = (
                    db.get(ChoiceRun, UUID(str(est["choice_run"])))
                    if est.get("choice_run")
                    else None
                )
                if check is not None:
                    est["costs"] = {
                        **(est.get("costs") or {}),
                        str(check.id): [
                            int(check.spent or 0) + int(check.first_spent or 0),
                            float(check.cost or 0)
                            + float(check.first_cost or 0)
                            + float(check.judge_cost or 0)
                            + float(check.third_cost or 0),
                            int(check.done or 0),
                        ],
                    }
                    used = list(est["costs"].values())
                    run.spent = sum(int(entry[0]) for entry in used)
                    run.cost = round(sum(float(entry[1]) for entry in used), 6)
                    run.counts = {
                        **(run.counts or {}),
                        "verify": {"records": sum(int(entry[2]) for entry in used)},
                    }
                    run.estimate = est
                    if check.state in parent_choice.ASKING:
                        run.state, run.detail = "running", ""
                        return False
                    if check.state in parent_choice.LIVE:
                        run.state, run.detail = "held", check.detail or "The asking is stopped."
                        return False
                    est["ended"] = bool(est.get("ended")) or check.state == "cancelled"
                step, begun = str(est["step"]), bool(est.get("begun"))
                may_ask = not est.get("ended") and not est.get("blocked")
            if not begun and may_ask:
                started = self._begin(name, ONE if step == "one" else OTHERS)
                with self.sessions.begin() as db:
                    run = db.get_one(OrganizeRun, key)
                    run.estimate = {**est, "begun": True, "choice_run": started}
                if started:
                    continue
            if step == "one":
                self._gather_one(name, key)
                with self.sessions.begin() as db:
                    run = db.get_one(OrganizeRun, key)
                    run.estimate = {**est, "step": "others", "begun": False, "choice_run": None}
                continue
            self._gather_rest(name, key, "stopped" if est.get("ended") else "complete")
            # The linking is done. Where the run was not ended, the stage that creates
            # goes on from here.
            return self.creator.advance(name, key)
        return False

    def tick(self) -> bool:
        """Carry every run that is under way one step on. True when one ended."""
        ended = False
        with self.sessions() as db:
            waiting = [
                (run.id, run.project_id)
                for run in db.scalars(
                    select(OrganizeRun).where(OrganizeRun.kind == KIND, OrganizeRun.state.in_(LIVE))
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
                log.exception("Organizing the Epics of %s could not be carried on", name)
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

    # -- what a run found -----------------------------------------------------------

    def _line(
        self, db: Session, project: UUID, story: UUID, op: str, epic: str = ""
    ) -> OrganizeLine | None:
        """The line that still waits for this Story and question, from whichever run."""
        return next(
            (
                line
                for line in db.scalars(
                    select(OrganizeLine).where(
                        organize.lines_where(project, KIND),
                        OrganizeLine.subject_id == story,
                        OrganizeLine.state == "open",
                    )
                )
                if (line.action or {}).get("op") == op
                and (op != "epic_link" or str((line.action or {}).get("epic") or "") == epic)
            ),
            None,
        )

    def _write(
        self, db: Session, run: OrganizeRun, story: UUID, op: str, epic: str = ""
    ) -> OrganizeLine:
        line = self._line(db, run.project_id, story, op, epic)
        if line is None:
            line = OrganizeLine(project_id=run.project_id, run_id=run.id)
            db.add(line)
        line.run_id, line.listed, line.state = run.id, "needs_you", "open"
        line.subject_kind, line.subject_id = "stories", story
        line.answer = line.answered_at = line.undone_at = line.before = None
        return line

    def _gather_one(self, name: str, key: UUID) -> None:
        """The one Epic: what is settled and may be applied is applied and listed under
        Done; every other answer is a case under Needs you."""
        applied: list[dict[str, Any]] = []
        made = self.changes.apply_alone(name, "primary")
        if made is not None:
            applied = self.changes.applied(name, UUID(str(made["id"])))
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            run = db.get_one(OrganizeRun, key)
            read = self._read(db, project)
            for entry in applied:
                earlier = self._line(db, project.id, entry["child"], "primary")
                if earlier is not None:
                    earlier.state = "skipped"
                db.add(
                    OrganizeLine(
                        run_id=run.id,
                        project_id=project.id,
                        listed="done",
                        state="applied",
                        case="one_epic",
                        subject_kind="stories",
                        subject_id=entry["child"],
                        title=str(read["stories"].get(entry["child"]) or "")[:300],
                        candidates=[
                            {
                                "id": str(entry["parent"]),
                                "kind": "epics",
                                "title": str(read["epics"].get(entry["parent"]) or ""),
                                "chosen": True,
                            }
                        ],
                        said=[{"stage": "verify", "by": "two models", "answer": "the same"}],
                        reason=str(entry["reason"] or ""),
                        doubt=0.0,
                        action={
                            "op": "primary",
                            "story": str(entry["child"]),
                            "epic": str(entry["parent"]),
                            "reorg_line": str(entry["id"]),
                        },
                        answered_at=datetime.now(UTC),
                    )
                )
            cases = self._cases(db, project)
            listed_now = {case["story"].id for case in cases}
            waiting: set[UUID] = set()
            alone = reorg.alone_standing(db, project.id, "primary")
            for case in cases:
                stands, story, review = case["stands"], case["story"], case["review"]
                if stands not in DOUBT:
                    continue
                waiting.add(story.id)
                line = self._write(db, run, story.id, "primary")
                line.case, line.title = stands, story.title[:300]
                line.candidates = self._offered(case["listed"], review)
                line.said = self._said(review, case["listed"])
                line.reason = parent_choice.SAID.get(stands, "")
                if stands == "settled":
                    line.reason = (
                        "Two models chose the same Epic without seeing each other's answer, "
                        "and the texts alone rank it first. "
                        + (
                            "You took this back before: it is not applied again by itself."
                            if alone["in_force"]
                            else "It is not applied by itself: "
                            + (alone["why"] or "the project is set to propose only.")
                        )
                    )
                line.doubt = DOUBT[stands]
                line.action = {
                    "op": "primary",
                    "story": str(story.id),
                    "epic": str(review.parent_id) if review and review.parent_id else None,
                    "stands": stands,
                }
            for line in db.scalars(
                select(OrganizeLine).where(
                    organize.lines_where(project.id, KIND), OrganizeLine.state == "open"
                )
            ):
                if (line.action or {}).get("op") != "primary" or line.subject_id in waiting:
                    continue
                # What the administrator took back waits for as long as the Story's one
                # Epic is not chosen.
                if line.case == "undone" and line.subject_id in listed_now:
                    continue
                line.state = "skipped"

    def _gather_rest(self, name: str, key: UUID, final: str) -> None:
        """The other Epics of each Story, and the Stories whose only Epic is in
        question; then the run is over."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            run = db.get_one(OrganizeRun, key)
            read = self._read(db, project)
            kept = self._kept(db, project.id)
            _, links = self._links(db, project, read["chosen"], set())
            seen: set[tuple[UUID, str, str]] = set()
            for entry in links:
                story, epic = entry["story"], str(entry["epic"])
                if ("epic_link", str(story.id), epic) in kept:
                    continue
                seen.add((story.id, "epic_link", epic))
                review, said = entry["review"], entry["entry"]
                earlier = self._line(db, project.id, story.id, "epic_link", epic)
                if earlier is not None and earlier.case == "undone":
                    continue
                line = self._write(db, run, story.id, "epic_link", epic)
                line.case = "link_" + entry["case"]
                line.title = story.title[:300]
                line.candidates = [
                    {"id": epic, "kind": "epics", "title": entry["title"], "linked": True}
                ]
                line.said = [
                    {
                        "stage": "verify",
                        "by": review.model or "a model",
                        "answer": said.get("answer"),
                        "reason": said.get("reason"),
                        "passages": [
                            {"side": "epic", "text": said.get("text"), "found": said.get("found")}
                        ]
                        if said.get("text")
                        else [],
                    },
                    *(
                        [
                            {
                                "stage": "verify",
                                "by": str((review.judge or {}).get("model") or "a second model"),
                                "answer": entry["second"],
                            }
                        ]
                        if entry["second"] is not None
                        else []
                    ),
                ]
                line.reason = {
                    "unrelated": "Both models found that the User Story has nothing to do "
                    "with this Epic.",
                    "alone": "One model found that the User Story has nothing to do with "
                    "this Epic. No second model was asked.",
                    "differ": "The two models did not say the same of this link.",
                    "unclear": "No model could tell whether the User Story has to do with "
                    "this Epic.",
                    "unchecked": "A model found the User Story unrelated to this Epic, on a "
                    "passage that is not in the Epic's text.",
                }[entry["case"]]
                line.doubt = LINK[entry["case"]]
                line.action = {"op": "epic_link", "story": str(story.id), "epic": epic}
            for entry in self._only(db, project, read):
                story = entry["story"]
                if ("epic_of", str(story), "") in kept:
                    continue
                seen.add((story, "epic_of", ""))
                earlier = self._line(db, project.id, story, "epic_of")
                if earlier is not None and earlier.case == "undone":
                    continue
                line = self._write(db, run, story, "epic_of")
                line.case = "only_epic" if entry["own"] else "no_epic"
                line.title = entry["title"][:300]
                line.candidates = entry["near"]
                line.said = [{"stage": "match", "by": "the texts", "answer": "another Epic"}]
                line.reason = (
                    "Shared words and meaning both place another Epic first, and neither "
                    f"places its own Epic among the first {entry['far']}. No model was asked."
                    if entry["own"]
                    else "The User Story is under no open Epic. These are the nearest, of "
                    "the project and of the projects connected to it."
                    if entry["near"]
                    else "The User Story is under no open Epic, and no Epic is near it."
                )
                line.doubt = ONLY if entry["own"] else DOUBT["nothing"]
                line.action = {
                    "op": "epic_of",
                    "story": str(story),
                    "current": str(entry["own"]) if entry["own"] else None,
                }
            for line in db.scalars(
                select(OrganizeLine).where(
                    organize.lines_where(project.id, KIND), OrganizeLine.state == "open"
                )
            ):
                op = str((line.action or {}).get("op"))
                if op == "primary" or line.subject_id is None:
                    continue
                at = (
                    line.subject_id,
                    op,
                    str((line.action or {}).get("epic") or "") if op == "epic_link" else "",
                )
                if at not in seen and line.case != "undone":
                    line.state = "skipped"
            counts = organize.list_counts(db, project.id, KIND)
            est = run.estimate or {}
            left = sum(
                1 for case in self._cases(db, project) if case["stands"] in ("unasked", "stale")
            )
            asked = int(((run.counts or {}).get("verify") or {}).get("records", 0))
            run.counts = {
                **(run.counts or {}),
                "verify": {
                    "records": asked,
                    "settled": counts["done"],
                    "in_doubt": counts["needs_you"],
                    "none": left,
                },
                "create": {"records": 0},
            }
            run.detail = (
                f"{left} User Stories under several Epics have no answer yet: they were not "
                "asked about in this run."
                if left
                else ("No model could be asked." if est.get("blocked") else "")
            )
            # Ended by the administrator, the run stops here; otherwise the stage that
            # creates goes on with what has no counterpart.
            if final != "complete" or not self.creator.begin(db, run):
                run.state, run.stage, run.ended_at = final, "done", datetime.now(UTC)

    @staticmethod
    def _offered(listed: list[dict[str, Any]], review: ChoiceReview | None) -> list[dict[str, Any]]:
        """The Story's own Epics, as it was offered them, with whichever a model chose."""
        offered = (review.shortlist if review and review.shortlist else listed) or []
        chosen = str(review.parent_id) if review and review.parent_id else None
        judged = str(((review.judge if review else None) or {}).get("parent_id") or "") or None
        return [
            {
                "id": str(entry.get("id")),
                "kind": "epics",
                "title": str(entry.get("title") or ""),
                "words": list(entry.get("words") or [])[:5],
                "place": place + 1,
                "suggested": bool(entry.get("suggested")),
                "chosen": str(entry.get("id")) == chosen,
                "judge": str(entry.get("id")) == judged,
            }
            for place, entry in enumerate(offered)
            if place < SHOWN or str(entry.get("id")) in (chosen, judged)
        ]

    @staticmethod
    def _said(review: ChoiceReview | None, listed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if review is None:
            return []
        titles = {
            str(entry.get("id")): str(entry.get("title") or "")
            for entry in [*listed, *(review.shortlist or [])]
        }
        said = [
            {
                "stage": "verify",
                "by": review.model or "a model",
                "answer": review.answer,
                "epic": titles.get(str(review.parent_id)) if review.parent_id else None,
                "reason": review.reason,
                "passages": [
                    {"side": e.get("side"), "text": e.get("text"), "found": e.get("found")}
                    for e in review.evidence or []
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
                    "epic": titles.get(str(judge.get("parent_id")))
                    if judge.get("parent_id")
                    else None,
                    "reason": judge.get("reason"),
                }
            )
        return said

    # -- the lists, and what the administrator does with a line -----------------------

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
                "items": [organize.shown_line(line, now) for line in rows],
            }

    def _move(self, name: str, story: UUID, epic: UUID, current: UUID | None) -> dict[str, Any]:
        """Put the Story under that Epic as its one Epic, and take it off the Epic it
        was under alone. Returns how its links stood."""
        before = self.links.snapshot(name, "stories", story)
        self.links.before_change(name, "stories", story)
        if not self.planning.restore_epic_link(name, story, epic):
            raise ProjectConflict("The User Story or the Epic is gone.")
        self.links.after_change(name, "stories", story)
        self.links.set_primary(name, "stories", story, epic)
        if current is not None and current != epic:
            self.links.before_change(name, "stories", story)
            self.planning.remove_epic_link(name, story, current)
            self.links.after_change(name, "stories", story)
        return {"links": before, "current": str(current) if current else None}

    def answer(
        self,
        name: str,
        key: UUID,
        choice: str,
        epic: UUID | None = None,
        titles: list[str] | None = None,
    ) -> dict[str, str]:
        """The administrator's answer to a case: the Story belongs to that Epic
        (``epic``) or is left as it is (``leave``); a link to another Epic is taken off
        (``take_off``) or kept (``keep``). Where the records changed since, the case is
        set aside and says so."""
        with self.sessions() as db:
            project = active_project(db, name)
            line = organize.own_line(db, project.id, KIND, key)
            if line.listed != "needs_you" or line.state != "open":
                raise ProjectConflict("This case was decided already.")
            action = dict(line.action or {})
            offered = {str(entry.get("id")): entry for entry in line.candidates or []}
            reason = line.reason
        if action.get("op") in OPS:
            return self.creator.answer(name, KIND, key, choice, epic=epic, titles=titles)
        op, story = action["op"], UUID(str(action["story"]))
        listed, state, before, problem = "needs_you", "answered", None, None
        try:
            if op == "epic_link":
                if choice == "take_off":
                    taken = self.changes.apply_one(
                        name, "extra_epics", story, UUID(str(action["epic"])), reason
                    )
                    before, listed, state = {"links": taken}, "done", "applied"
                elif choice != "keep":
                    raise ProjectInvalid("Take the link off, or keep it.")
            elif choice == "epic":
                if epic is None or str(epic) not in offered:
                    raise ProjectInvalid("Choose one of the Epics it was offered.")
                if op == "primary":
                    before = {"links": self.links.snapshot(name, "stories", story)}
                    self.primary.choose(name, story, epic)
                else:
                    current = UUID(str(action["current"])) if action.get("current") else None
                    if current == epic:
                        raise ProjectInvalid(
                            "That is the Epic it is under. Keep it, or choose another."
                        )
                    if offered[str(epic)].get("project"):
                        # An Epic of a connected project: a link of the connection.
                        with self.sessions.begin() as db:
                            link_story_across(
                                db, active_project(db, name, lock=True), story, epic, ""
                            )
                        before, action["across"] = {"links": [], "current": None}, True
                    else:
                        before = self._move(name, story, epic, current)
                action.update(epic=str(epic), by="you")
                listed, state = "done", "applied"
            elif choice in ("leave", "keep"):
                if op == "primary":
                    try:
                        self.primary.dismiss(name, story)
                    except ProjectConflict:
                        pass
                choice = "keep" if op == "epic_of" else choice
            else:
                raise ProjectInvalid("Choose an Epic, or leave it as it is.")
        except ProjectConflict as changed:
            problem = str(changed)
        with self.sessions.begin() as db:
            project = active_project(db, name)
            line = organize.own_line(db, project.id, KIND, key)
            now = datetime.now(UTC)
            if problem:
                line.state, line.answered_at = "skipped", now
                line.reason = f"{line.reason} Set aside: {problem}".strip()
                return {"id": str(key), "state": "skipped", "detail": problem}
            line.listed, line.state, line.answer, line.answered_at = listed, state, choice, now
            line.action, line.before = action, before
            if listed == "done":
                line.case = "link_off" if op == "epic_link" else "one_epic"
                line.candidates = [
                    {**entry, "chosen": str(entry.get("id")) == action.get("epic")}
                    for entry in line.candidates or []
                ]
        return {"id": str(key), "state": state}

    def answer_many(self, name: str, group: str, choice: str) -> dict[str, int]:
        """One word for many: every link of that group that waits is taken off, or
        kept. A request answers some of them and says how many are left."""
        if group in CREATE_GROUPS:
            return self.creator.answer_many(name, KIND, group, choice)
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
                skipped += self.answer(name, key, choice)["state"] == "skipped"
                answered += 1
            except (ProjectConflict, ProjectNotFound):
                skipped += 1
        return {
            "answered": answered - skipped if answered else 0,
            "skipped": skipped,
            "left": max(0, len(keys) - CHUNK),
        }

    def _undo(self, name: str, key: UUID) -> None:
        with self.sessions() as db:
            project = active_project(db, name)
            line = organize.own_line(db, project.id, KIND, key)
            if not organize.can_undo(line, datetime.now(UTC)):
                raise ProjectConflict(
                    f"This can be undone for {organize.UNDO.days} days after it was applied."
                )
            action, before = dict(line.action or {}), dict(line.before or {})
        if action.get("op") in OPS:
            self.creator.undo(name, KIND, key)
            return
        op, story, epic = action["op"], UUID(str(action["story"])), UUID(str(action["epic"]))
        links = before.get("links") or []
        if op == "epic_link":
            self.changes.revert_one(name, "extra_epics", story, epic, links)
        elif action.get("reorg_line"):
            self.changes.undo_line(name, UUID(str(action["reorg_line"])))
        elif op == "primary":
            if not self.links.restore(name, "stories", story, links):
                raise ProjectConflict("The User Story's links are gone.")
        elif action.get("across"):
            with self.sessions.begin() as db:
                if not unlink_issue_across(db, active_project(db, name, lock=True), story, epic):
                    raise ProjectConflict("The link to the connected project's Epic is gone.")
        else:
            current = UUID(str(before["current"])) if before.get("current") else None
            self.links.before_change(name, "stories", story)
            try:
                if current is not None and not self.planning.restore_epic_link(
                    name, story, current
                ):
                    raise ProjectConflict("The Epic it was under is gone.")
                if not self.planning.remove_epic_link(name, story, epic):
                    raise ProjectConflict(
                        "It is the only Epic the User Story is under: it cannot be taken off."
                    )
            finally:
                self.links.after_change(name, "stories", story)
            self.links.restore(name, "stories", story, links)
        with self.sessions.begin() as db:
            project = active_project(db, name)
            line = organize.own_line(db, project.id, KIND, key)
            line.listed, line.state, line.answer = "needs_you", "open", None
            line.undone_at = datetime.now(UTC)
            line.doubt = max(line.doubt, DOUBT["texts"])
            line.case = "undone"
            line.reason = "You took this back. " + (
                "The link to the Epic stands again."
                if op == "epic_link"
                else "The User Story's Epics stand as they did before."
            )

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
