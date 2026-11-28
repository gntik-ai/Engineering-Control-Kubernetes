"""Batches to approve: what the scorer would do, put before the administrator.

The scorer says of each record what it would do and how sure it is (``scorer.py``); it
changes nothing. Here its sure cases of one kind are gathered into a batch:

- ``reasons``: an existing link the scorer singles out gets its reason recorded;
- ``primary``: a User Story under several Epics gets its primary Epic chosen;
- ``place``: an issue without a User Story is put under one;
- ``unsupported``: an existing link that both the scorer and the plain rankings speak
  against is taken off;
- ``extra_epics``: a link of a User Story to an Epic that is not its primary one, and
  that a model found to have nothing to do with it, is taken off (``epic_links.py``).

Nothing is applied without approval. A batch opens with a sample of its lines to judge
right or wrong. More wrong ones than its error allows, and the batch is withdrawn. Any line
can be struck. Approval applies the rest through the same paths the worklists use, each
line keeping how the link stood before, so that the whole batch can be undone for thirty
days. Every line judged, right or wrong, is something the scorer learns from.

A bug is never in a batch: it has no User Story.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import cleanup, epic_links, parent_choice, review_settings
from engineering_control.dashboard.connections import link_issue_across, unlink_issue_across
from engineering_control.dashboard.issue_types import bugs_with_optional_stories
from engineering_control.dashboard.planning import PlanningRecords
from engineering_control.dashboard.planning_links import Note, PlanningLinks, explained
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.scorer import ADD_ERROR, REMOVE_ERROR, SIGNALS
from engineering_control.dashboard.signals import ISSUE_STORY, STORY_EPIC, _spread
from engineering_control.db.models import (
    BacklogItem,
    PlanningLink,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectUserStory,
    ReorgBatch,
    ReorgLine,
    ReviewSettings,
    ScorerDecision,
    ScorerReport,
    StoryDelivery,
)

UNDO_DAYS: Final = 30
SAMPLE_MIN: Final = 5
SAMPLE_MAX: Final = 20
# A link is called unsupported only when the plain rankings speak against it too: it
# stands below the fifth place both by shared words and by meaning.
LOW_PLACE: Final = 1 / 6
# kind: the kind of link, the scorer's outcome it gathers, the error it may make, and
# what it does, as a title and in one sentence.
KINDS: Final[dict[str, tuple[str, str, float, str, str]]] = {
    "reasons": (
        ISSUE_STORY,
        "confirmed",
        ADD_ERROR,
        "Record why links hold",
        "Each of these links has no reason recorded. The scorer singles its User Story out "
        "among all candidates: approving records that as the reason. No link is added or "
        "removed.",
    ),
    "primary": (
        STORY_EPIC,
        "primary",
        ADD_ERROR,
        "Choose primary Epics",
        "Each of these User Stories stands under several Epics. Approving makes the Epic "
        "shown its primary one. Its other Epics stay linked.",
    ),
    "place": (
        ISSUE_STORY,
        "place",
        ADD_ERROR,
        "Place issues under a User Story",
        "Each of these issues has no User Story. Approving links it to the one shown.",
    ),
    "unsupported": (
        ISSUE_STORY,
        "unsupported",
        REMOVE_ERROR,
        "Take off links nothing supports",
        "The scorer rules each of these links out, and neither shared words nor meaning "
        "place the User Story among the issue's first five. Approving takes the link off; "
        "an issue left with no User Story then waits for one like any other.",
    ),
    "extra_epics": (
        STORY_EPIC,
        "unrelated",
        REMOVE_ERROR,
        "Take off Epic links a model found unrelated",
        "Each of these User Stories is linked to an Epic that is not its primary one, and "
        "a model that read both found the Story to have nothing to do with that Epic's "
        "goal; each line gives its reason and the passages it read. Approving takes the "
        "link off. The Story keeps its primary Epic and its other Epics.",
    ),
    "close": (
        "story_close",
        "delivered",
        REMOVE_ERROR,
        "Close delivered User Stories",
        "Each of these User Stories has issues, all of them closed as done, and something "
        "shows the whole Story was covered: a review, or an OpenSpec change with every task "
        "done. Approving closes the Story. No issue is touched.",
    ),
    "retire_stories": (
        "story_retire",
        "proposed",
        REMOVE_ERROR,
        "Close User Stories nothing stands behind",
        "Two reasons of different kinds, or an exact one, speak against each of these User "
        "Stories; each line says which. Approving closes the Story: it is not deleted and "
        "can be reopened. No issue is touched.",
    ),
    "retire_epics": (
        "epic_retire",
        "proposed",
        REMOVE_ERROR,
        "Close Epics nothing stands behind",
        "Two reasons of different kinds, or an exact one, speak against each of these "
        "Epics. Approving closes the Epic: it is not deleted and can be reopened. No User "
        "Story and no issue is touched.",
    ),
    "retire_issues": (
        "issue_retire",
        "proposed",
        REMOVE_ERROR,
        "Close issues nothing stands behind",
        "Two reasons of different kinds, or an exact one, speak against each of these open "
        "issues. Approving closes each on GitHub as not planned, with a comment that gives "
        "the reasons and names the issue that replaced it. This writes on GitHub and is "
        "not undone from here: an issue closed by mistake is reopened on GitHub.",
    ),
}
RETIRED: Final = {"retire_stories": "stories", "retire_epics": "epics", "retire_issues": "issues"}
# The kinds whose lines come from a rule or a model's answer, not from the scorer: each
# line brings its own reason, nothing was measured, and the sample is the check.
RULED: Final = frozenset({"close", "extra_epics", *RETIRED})
# What a batch does that cannot be put back from here.
FINAL: Final = frozenset({"retire_issues"})


def why(kind: str, confidence: float, signals: dict[str, Any], candidates: int) -> str:
    """The reason a line gives, in words, from the signals the scorer read."""
    said = []
    if signals.get("words", 0) >= 0.999:
        said.append("it shares more words with the record than any other candidate")
    elif signals.get("words_place", 0) >= 0.33:
        said.append("it is among the first three by shared words")
    if signals.get("meaning_gap", -1) >= -1e-9:
        said.append("it is the closest in meaning")
    elif signals.get("meaning_place", 0) >= 0.33:
        said.append("it is among the first three by meaning")
    if signals.get("reranker_first"):
        said.append("the reranker reads it as the best fit")
    if signals.get("siblings", 0) >= 0.7:
        said.append("the parent's other children resemble the record")
    share = f"{round(confidence * 100)}%"
    if kind == "unsupported":
        places = []
        for name, label in (("words_place", "shared words"), ("meaning_place", "meaning")):
            value = float(signals.get(name) or 0)
            places.append(
                f"place {round(1 / value)} by {label}" if value else f"unranked by {label}"
            )
        return (
            f"The scorer gives this link a share of belief of {share}, below what a right "
            f"link has among {candidates} candidates, and it stands at {' and '.join(places)}."
        )
    return (
        f"The scorer leaves this parent alone among {candidates} candidates, with a share "
        f"of belief of {share}" + (": " + "; ".join(said) + "." if said else ".")
    )


def closing_comment(reason: str) -> str:
    """The comment left on an issue the clean-up closes: what it was closed for, in the
    words of the line the administrator approved, and how to take it back."""
    return (
        "Closed as not planned in a clean-up of the backlog that the project's administrator "
        "approved in Engineering Control.\n\n"
        f"Why: {reason}\n\n"
        "If this issue is still needed, reopen it."
    )


def sample_size(lines: int, least: int = SAMPLE_MIN, most: int = SAMPLE_MAX) -> int:
    """How many lines are judged before a batch may be approved: more for a larger batch,
    never fewer than the least where there are that many."""
    return min(lines, max(least, min(max(least, most), math.ceil(math.sqrt(lines)))))


# The kinds of change that may be applied without the administrator: they add or
# correct, and can be undone. With the check whose agreed answers feed each.
ALONE: Final = {"place": "issue_story", "primary": "story_epic"}
# The judged lines the record of a kind goes by: the latest of them.
RECORD: Final = 100
# The kinds of change a run of Organize carries out itself, case by case in its lists:
# no new batch is offered for them. One that is open is still decided as a batch.
ORGANIZED: Final = frozenset({"primary", "place", "unsupported", "extra_epics", "close"})


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def alone_standing(db: Session, project: UUID, kind: str) -> dict[str, Any]:
    """How far that kind of change goes without the administrator in this project, and
    the record that decides it: the sampled lines of its batches the administrator
    judged, the latest hundred, and how many of them wrong. Applying is in force when
    the settings ask for it, enough lines were judged, the share of wrong ones is within
    what the settings accept, and not too many lines applied before wait to be judged."""
    mine = review_settings.of(db, project)
    rows = db.execute(
        select(ReorgLine.verdict, ReorgBatch.state, ReorgBatch.alone, ReorgLine.updated_at)
        .join(ReorgBatch, ReorgBatch.id == ReorgLine.batch_id)
        .where(ReorgLine.project_id == project, ReorgBatch.kind == kind, ReorgLine.sampled)
        .order_by(ReorgLine.updated_at.desc(), ReorgLine.id)
    ).all()
    # What was judged before the models that decide were changed says nothing of those
    # that decide now: the record starts anew with them.
    since = mine.stage_since("decision")
    judged = [
        verdict
        for verdict, _state, _alone, at in rows
        if verdict in ("right", "wrong") and (since is None or _aware(at) >= since)
    ][:RECORD]
    wrong = sum(1 for verdict in judged if verdict == "wrong")
    waiting = sum(
        1
        for verdict, state, alone, _at in rows
        if alone and state == "approved" and verdict not in ("right", "wrong")
    )
    needed, accepted = mine.count("alone_from"), error_of(db, project, kind)
    setting = mine.goes(kind)
    why = ""
    if len(judged) < needed:
        why = (
            f"You judged {len(judged):,} sampled {'line' if len(judged) == 1 else 'lines'} of "
            f"these batches"
            + (" since the models that decide were changed" if since is not None else "")
            + f"; applying without you begins after {needed:,}."
        )
    elif wrong > accepted * len(judged):
        why = (
            f"Of the last {len(judged):,} sampled lines you judged, {wrong:,} were wrong: "
            f"more than the {accepted:.0%} the settings accept. It stopped, and proposes "
            "batches until the record is within that again."
        )
    elif waiting > mine.count("alone_waiting"):
        why = (
            f"{waiting:,} lines applied without you wait to be judged. Applying goes on "
            "once you have judged them."
        )
    return {
        "kind": kind,
        "title": KINDS[kind][3],
        "setting": setting,
        "judged": len(judged),
        "wrong": wrong,
        "needed": needed,
        "accepted": accepted,
        "waiting": waiting,
        # Whether the record would allow it, and whether it is in force now.
        "allowed": not why,
        "in_force": setting == "apply" and not why,
        "why": why,
    }


def error_of(db: Session, project: UUID, kind: str) -> float:
    """The share of wrong lines a batch of this kind may have: what the project's
    settings say for what adds or corrects, and for what removes or closes."""
    mine = review_settings.of(db, project)
    return mine.share("remove_error" if KINDS[kind][2] == REMOVE_ERROR else "add_error")


class Reorg:
    def __init__(
        self, sessions: sessionmaker[Session], planning: PlanningRecords, links: PlanningLinks
    ) -> None:
        self.sessions = sessions
        self.planning = planning
        self.links = links

    # ------------------------------------------------------------- what is ready

    def _ready(self, db: Session, project: ProjectRecord, kind: str) -> list[ScorerDecision]:
        """The scorer's decisions of that kind that can go into a batch: not in one
        already, not judged wrong before, and still true of the plan."""
        link_kind, outcome, _error, _title, _about = KINDS[kind]
        if kind == "close":
            return self._delivered(db, project)
        if kind in RETIRED:
            return self._retirable(db, project, kind)
        if kind == "extra_epics":
            return self._unrelated(db, project)
        decisions = db.scalars(
            select(ScorerDecision).where(
                ScorerDecision.project_id == project.id,
                ScorerDecision.kind == link_kind,
                ScorerDecision.outcome == outcome,
                ScorerDecision.parent_id.is_not(None),
            )
        ).all()
        # What a check answered and its judge agreed with goes into the same batch as
        # what the scorer is sure of: one thing to approve, with one sample.
        check = ALONE.get(kind)
        # Unless the settings keep them in the worklist, to be approved one by one.
        if check is not None and review_settings.of(db, project.id).goes(kind) != "off":
            mine = {(row.child_id, row.parent_id) for row in decisions}
            decisions = [
                *decisions,
                *(
                    row
                    for row in parent_choice.agreed_lines(db, project, check, link_kind, outcome)
                    if (row.child_id, row.parent_id) not in mine
                ),
            ]
        if not decisions:
            return []
        taken = {
            (line.child_id, line.parent_id)
            for line, batch in db.execute(
                select(ReorgLine, ReorgBatch)
                .join(ReorgBatch, ReorgBatch.id == ReorgLine.batch_id)
                .where(ReorgLine.project_id == project.id, ReorgBatch.kind == kind)
            )
            if line.verdict == "wrong"
            or batch.state == "sample"
            or (batch.state == "approved" and line.state == "applied")
        }
        live = {
            (row.child_id, row.parent_id): row
            for row in db.scalars(
                select(PlanningLink).where(
                    PlanningLink.project_id == project.id,
                    PlanningLink.kind == link_kind,
                    PlanningLink.removed_at.is_(None),
                )
            )
        }
        bugs = set(db.scalars(bugs_with_optional_stories(db, project.id)))
        with_parent = {child for child, _parent in live}
        found = []
        for decision in decisions:
            pair = (decision.child_id, decision.parent_id)
            if pair in taken or decision.child_id in bugs:
                continue
            link = live.get(pair)  # type: ignore[arg-type]
            signals = (decision.detail or {}).get("signals") or {}
            if kind == "reasons" and (link is None or explained(link)):
                continue
            if kind == "primary" and (
                link is None or (link.is_primary and link.primary_by == "administrator")
            ):
                continue
            if kind == "place" and decision.child_id in with_parent:
                continue
            if kind == "unsupported" and (
                link is None
                or link.origin in ("declared", "administrator")
                or float(signals.get("words_place") or 0) > LOW_PLACE
                or float(signals.get("meaning_place") or 0) > LOW_PLACE
            ):
                continue
            found.append(decision)
        return sorted(found, key=lambda row: (-row.confidence, str(row.child_id)))

    @staticmethod
    def _unrelated(db: Session, project: ProjectRecord) -> list[ScorerDecision]:
        """The links of a User Story to an Epic that a model found unrelated, as lines:
        not in a batch already, and not judged wrong before."""
        taken = {
            (line.child_id, line.parent_id)
            for line, batch in db.execute(
                select(ReorgLine, ReorgBatch)
                .join(ReorgBatch, ReorgBatch.id == ReorgLine.batch_id)
                .where(ReorgLine.project_id == project.id, ReorgBatch.kind == "extra_epics")
            )
            if line.verdict == "wrong"
            or batch.state == "sample"
            or (batch.state == "approved" and line.state == "applied")
        }
        return [
            row
            for row in epic_links.ready(db, project)
            if (row.child_id, row.parent_id) not in taken
        ]

    def _delivered(self, db: Session, project: ProjectRecord) -> list[ScorerDecision]:
        """The open User Stories that stand as delivered, as lines to close: each with
        the issue that shows it. Shaped like the scorer's decisions, which they are not."""
        taken = {
            line.child_id
            for line, batch in db.execute(
                select(ReorgLine, ReorgBatch)
                .join(ReorgBatch, ReorgBatch.id == ReorgLine.batch_id)
                .where(ReorgLine.project_id == project.id, ReorgBatch.kind == "close")
            )
            if line.verdict == "wrong"
            or batch.state == "sample"
            or (batch.state == "approved" and line.state == "applied")
        }
        found = []
        for row, title in db.execute(
            select(StoryDelivery, ProjectUserStory.title)
            .join(ProjectUserStory, ProjectUserStory.id == StoryDelivery.story_id)
            .where(
                StoryDelivery.project_id == project.id,
                StoryDelivery.state == "delivered",
                ProjectUserStory.closed_at.is_(None),
                ProjectUserStory.archived_at.is_(None),
            )
            .order_by(ProjectUserStory.title, StoryDelivery.story_id)
        ):
            evidence = row.evidence or {}
            linked = evidence.get("linked") or []
            if row.story_id in taken or not linked:
                continue
            shows = next((entry for entry in linked if entry.get("strength") == "full"), linked[0])
            found.append(
                ScorerDecision(
                    id=row.id,
                    project_id=project.id,
                    kind="story_close",
                    child_id=row.story_id,
                    parent_id=UUID(shows["id"]),
                    outcome="delivered",
                    confidence=1.0,
                    model="rule",
                    detail={
                        "why": (
                            f"{title}: all {len(linked)} of its issues are closed as done. "
                            + str(evidence.get("shown_by") or "")
                        )
                    },
                )
            )
        return found

    def _retirable(self, db: Session, project: ProjectRecord, kind: str) -> list[ScorerDecision]:
        """The User Stories or Epics the clean-up proposes to close, as lines: each with
        its reasons in words. Shaped like the scorer's decisions, which they are not."""
        taken = {
            line.child_id
            for line, batch in db.execute(
                select(ReorgLine, ReorgBatch)
                .join(ReorgBatch, ReorgBatch.id == ReorgLine.batch_id)
                .where(ReorgLine.project_id == project.id, ReorgBatch.kind == kind)
            )
            if line.verdict == "wrong"
            or batch.state == "sample"
            or (batch.state == "approved" and line.state == "applied")
        }
        found = []
        for entry in cleanup.candidates(db, project):
            key = UUID(entry["id"])
            if entry["kind"] != RETIRED[kind] or not entry["proposed"] or key in taken:
                continue
            said = []
            twin: UUID | None = None
            for reason in entry["reasons"]:
                text = str(reason["says"])
                if reason.get("twin"):
                    twin = twin or UUID(reason["twin"]["id"])
                    text += f" ({reason['twin']['title']})"
                if reason.get("why"):
                    text += f": {reason['why']}"
                said.append(text)
            found.append(
                ScorerDecision(
                    id=key,
                    project_id=project.id,
                    kind=KINDS[kind][0],
                    child_id=key,
                    # The record that says the same, where there is one; else itself.
                    parent_id=twin or key,
                    outcome="proposed",
                    confidence=1.0,
                    model="rule",
                    detail={"why": ". ".join(said) + ".", "said": said},
                )
            )
        return found

    @staticmethod
    def _measured(db: Session, project: UUID, kind: str) -> dict[str, Any]:
        """What the scorer measured for that kind of link, as its report says."""
        link_kind, _outcome, _error, _title, _about = KINDS[kind]
        error = error_of(db, project, kind)
        if kind in RULED:
            # A rule or a model's answer, not the scorer: nothing was measured, and the
            # sample is the check.
            return {"error": error, "rule": True}
        report = db.scalar(select(ScorerReport.report).where(ScorerReport.project_id == project))
        entry = next(
            (e for e in (report or {}).get("kinds", []) if e.get("kind") == link_kind), None
        )
        if not entry:
            return {"error": error}
        row = next(
            (m for m in entry.get("measured", []) if m.get("key") == entry.get("uses")), None
        )
        return {
            "error": error,
            "measured_here": bool(entry.get("measured_here")),
            "children": (row or {}).get("children"),
            "acted_count": (row or {}).get("acted_count"),
            "acted_right": (row or {}).get("acted_right"),
            "in_set": (row or {}).get("in_set"),
        }

    # ------------------------------------------------------------------ reading

    def _titles(
        self, db: Session, lines: list[ReorgLine]
    ) -> dict[UUID, tuple[str, str, int | None]]:
        """The kind, title and number of every record the lines name."""
        wanted = {line.child_id for line in lines} | {line.parent_id for line in lines}
        found: dict[UUID, tuple[str, str, int | None]] = {}
        for key, title, number in db.execute(
            select(BacklogItem.id, BacklogItem.title, BacklogItem.issue_number).where(
                BacklogItem.id.in_(wanted)
            )
        ):
            found[key] = ("issues", title, number)
        for key, title in db.execute(
            select(ProjectUserStory.id, ProjectUserStory.title).where(
                ProjectUserStory.id.in_(wanted)
            )
        ):
            found[key] = ("stories", title, None)
        for key, title in db.execute(
            select(ProjectEpic.id, ProjectEpic.title).where(ProjectEpic.id.in_(wanted))
        ):
            found[key] = ("epics", title, None)
        return found

    @staticmethod
    def _line(line: ReorgLine, titles: dict[UUID, tuple[str, str, int | None]]) -> dict[str, Any]:
        def side(key: UUID) -> dict[str, Any]:
            kind, title, number = titles.get(key, ("", "", None))
            return {
                "kind": kind,
                "id": str(key),
                "title": f"#{number} {title}" if number else title,
            }

        return {
            "id": str(line.id),
            "child": side(line.child_id),
            "parent": side(line.parent_id),
            "confidence": line.confidence,
            "reason": line.reason,
            "signals": {
                SIGNALS.get(name, name): value for name, value in (line.signals or {}).items()
            },
            "sampled": line.sampled,
            "verdict": line.verdict,
            "state": line.state,
            "detail": line.detail,
        }

    @staticmethod
    def _shown(db: Session, batch: ReorgBatch, lines: list[ReorgLine]) -> dict[str, Any]:
        days = review_settings.of(db, batch.project_id).count("undo_days")
        return Reorg._batch(batch, lines, days)

    @staticmethod
    def _batch(
        batch: ReorgBatch, lines: list[ReorgLine], undo_days: int = UNDO_DAYS
    ) -> dict[str, Any]:
        sampled = [line for line in lines if line.sampled]
        now = datetime.now(UTC)
        decided = batch.decided_at
        if decided is not None and decided.tzinfo is None:
            decided = decided.replace(tzinfo=UTC)
        until = decided + timedelta(days=undo_days) if decided else None
        return {
            "id": str(batch.id),
            "kind": batch.kind,
            # Applied without the administrator: its sample is judged afterwards.
            "alone": bool(batch.alone),
            "title": KINDS[batch.kind][3],
            "about": KINDS[batch.kind][4],
            "state": batch.state,
            "lines": len(lines),
            "sample": len(sampled),
            "sample_judged": sum(1 for line in sampled if line.verdict),
            "sample_wrong": sum(1 for line in sampled if line.verdict == "wrong"),
            "allowed_wrong": batch.allowed_wrong,
            "struck": sum(1 for line in lines if line.verdict == "wrong"),
            "applied": sum(1 for line in lines if line.state == "applied"),
            "skipped": sum(1 for line in lines if line.state == "skipped"),
            "undone": sum(1 for line in lines if line.state == "undone"),
            "measured": batch.measured,
            "made_at": batch.created_at.isoformat(),
            "decided_at": decided.isoformat() if decided else None,
            "undo_until": until.isoformat()
            if until and batch.state == "approved" and batch.kind not in FINAL
            else None,
            "can_undo": bool(
                batch.state == "approved" and until and now <= until and batch.kind not in FINAL
            ),
            "final": batch.kind in FINAL,
        }

    def overview(self, name: str) -> dict[str, Any]:
        """For each kind of change: how many the scorer has ready, what it measured, the
        batch that is open, and the batches decided before."""
        self.links.reconcile_project(name)
        with self.sessions() as db:
            project = active_project(db, name)
            batches = db.scalars(
                select(ReorgBatch)
                .where(ReorgBatch.project_id == project.id)
                .order_by(ReorgBatch.created_at.desc())
            ).all()
            lines: dict[UUID, list[ReorgLine]] = {batch.id: [] for batch in batches}
            for line in db.scalars(select(ReorgLine).where(ReorgLine.project_id == project.id)):
                lines.setdefault(line.batch_id, []).append(line)
            kinds = []
            for kind, (_link, _outcome, error, title, about) in KINDS.items():
                opened = next((b for b in batches if b.kind == kind and b.state == "sample"), None)
                kinds.append(
                    {
                        "kind": kind,
                        "organized": kind in ORGANIZED,
                        "title": title,
                        "about": about,
                        "error": error,
                        "ready": len(self._ready(db, project, kind)),
                        # For what may be applied without the administrator: how far
                        # it goes in this project, and the record that decides it.
                        "alone": alone_standing(db, project.id, kind) if kind in ALONE else None,
                        "measured": self._measured(db, project.id, kind),
                        "open": self._shown(db, opened, lines[opened.id]) if opened else None,
                    }
                )
            return {
                "kinds": kinds,
                "past": [
                    self._shown(db, batch, lines[batch.id])
                    for batch in batches
                    if batch.state != "sample"
                ][:20],
                "undo_days": review_settings.of(db, project.id).count("undo_days"),
            }

    def lines(self, name: str, batch_id: UUID, page: int, page_size: int) -> dict[str, Any]:
        """A batch's lines: the sampled ones first, then the surest."""
        with self.sessions() as db:
            project = active_project(db, name)
            batch = self._get(db, project, batch_id)
            every = db.scalars(select(ReorgLine).where(ReorgLine.batch_id == batch.id)).all()
            ordered = sorted(
                every, key=lambda line: (not line.sampled, -line.confidence, str(line.id))
            )
            pages = max(1, (len(ordered) + page_size - 1) // page_size)
            page = min(max(1, page), pages)
            shown = ordered[(page - 1) * page_size : page * page_size]
            titles = self._titles(db, shown)
            return {
                "batch": self._shown(db, batch, list(every)),
                "items": [self._line(line, titles) for line in shown],
                "total": len(ordered),
                "page": page,
                "pages": pages,
            }

    @staticmethod
    def _get(db: Session, project: ProjectRecord, batch_id: UUID) -> ReorgBatch:
        batch = db.get(ReorgBatch, batch_id)
        if batch is None or batch.project_id != project.id:
            raise ProjectNotFound(str(batch_id))
        return batch

    # ---------------------------------------------------------------- preparing

    def prepare(self, name: str, kind: str, alone: bool = False) -> dict[str, Any]:
        """Gather what the scorer has ready of that kind into a batch, and draw its
        sample. Nothing of the plan changes. A batch that is to be applied without the
        administrator draws the share of it the settings say, to be judged afterwards."""
        if kind not in KINDS:
            raise ProjectInvalid("There is no such kind of batch.")
        self.links.reconcile_project(name)
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            if db.scalar(
                select(ReorgBatch.id).where(
                    ReorgBatch.project_id == project.id,
                    ReorgBatch.kind == kind,
                    ReorgBatch.state == "sample",
                )
            ):
                raise ProjectConflict("A batch of this kind is already open.")
            ready = self._ready(db, project, kind)
            if not ready:
                raise ProjectConflict("The scorer has nothing ready of this kind.")
            mine = review_settings.of(db, project.id)
            count = (
                min(len(ready), max(1, math.ceil(len(ready) * mine.share("alone_sample"))))
                if alone
                else sample_size(len(ready), mine.count("sample_least"), mine.count("sample_most"))
            )
            error = error_of(db, project.id, kind)
            batch = ReorgBatch(
                project_id=project.id,
                kind=kind,
                state="sample",
                alone=alone,
                measured=self._measured(db, project.id, kind),
                allowed_wrong=math.floor(error * count),
            )
            db.add(batch)
            db.flush()
            # The sample is drawn by a mark that does not look at how sure the scorer is.
            drawn = {
                row.id
                for row in sorted(ready, key=lambda row: _spread("sample", batch.id, row.id))[
                    :count
                ]
            }
            for decision in ready:
                detail = decision.detail or {}
                signals = detail.get("signals") or {}
                assert decision.parent_id is not None
                db.add(
                    ReorgLine(
                        batch_id=batch.id,
                        project_id=project.id,
                        link_kind=decision.kind,
                        child_id=decision.child_id,
                        parent_id=decision.parent_id,
                        confidence=decision.confidence,
                        # A line a model made says why itself; the scorer's is worded
                        # from its signals.
                        reason=str(detail.get("why"))
                        if kind in RULED or detail.get("why")
                        else why(kind, decision.confidence, signals, int(detail.get("of") or 0)),
                        signals=signals,
                        sampled=decision.id in drawn,
                        state="pending",
                    )
                )
            db.flush()
            return {"id": str(batch.id), "lines": len(ready), "sample": count}

    # ------------------------------------------------------------------ judging

    def judge(
        self, name: str, batch_id: UUID, line_id: UUID, verdict: str | None
    ) -> dict[str, Any]:
        """The administrator's judgment of one line: right, wrong, or taken back. A line
        judged wrong is not applied. Too many wrong among the sampled, and the batch is
        withdrawn."""
        if verdict not in (None, "right", "wrong"):
            raise ProjectInvalid("A line is judged right or wrong.")
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            batch = self._get(db, project, batch_id)
            # A batch applied without the administrator is judged after it was applied.
            after = batch.alone and batch.state == "approved"
            if batch.state != "sample" and not after:
                raise ProjectConflict("This batch is decided already.")
            line = db.get(ReorgLine, line_id)
            if line is None or line.batch_id != batch.id:
                raise ProjectNotFound(str(line_id))
            line.verdict = verdict
            db.flush()
            lines = db.scalars(select(ReorgLine).where(ReorgLine.batch_id == batch.id)).all()
            wrong = sum(1 for row in lines if row.sampled and row.verdict == "wrong")
            if wrong > batch.allowed_wrong and not after:
                batch.state, batch.decided_at = "withdrawn", datetime.now(UTC)
            back = (
                (batch.kind, line.child_id, line.parent_id, line.before)
                if after and verdict == "wrong" and line.state == "applied"
                else None
            )
            if back is None:
                return self._shown(db, batch, list(lines))
        # A line that was applied and is judged wrong is put back at once.
        kind, child, parent, before = back
        try:
            self._revert(name, kind, child, parent, before or [])
            state, detail = "undone", "Judged wrong after it was applied, and put back."
        except (ProjectConflict, ProjectInvalid, ProjectNotFound) as error:
            state, detail = "applied", f"Judged wrong, and not put back: {error}"
        with self.sessions.begin() as db:
            row = db.get_one(ReorgLine, line_id)
            row.state, row.detail = state, detail
            batch = db.get_one(ReorgBatch, batch_id)
            lines = db.scalars(select(ReorgLine).where(ReorgLine.batch_id == batch_id)).all()
            return self._shown(db, batch, list(lines))

    def withdraw(self, name: str, batch_id: UUID) -> dict[str, Any]:
        """Set an open batch aside. What was judged in it is kept and learned from."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            batch = self._get(db, project, batch_id)
            if batch.state != "sample":
                raise ProjectConflict("This batch is decided already.")
            batch.state, batch.decided_at = "withdrawn", datetime.now(UTC)
            lines = db.scalars(select(ReorgLine).where(ReorgLine.batch_id == batch.id)).all()
            return self._shown(db, batch, list(lines))

    # ----------------------------------------------------------------- applying

    def apply_alone(self, name: str, kind: str) -> dict[str, Any] | None:
        """Apply what is ready of that kind without the administrator, where the
        project's settings ask for it and its record allows it. The batch can be undone
        like any other, and a share of its lines is marked for judging afterwards.
        Nothing where applying is not in force, a batch of the kind is open, or nothing
        is ready."""
        if kind not in ALONE:
            return None
        self.links.reconcile_project(name)
        with self.sessions() as db:
            project = active_project(db, name)
            if not alone_standing(db, project.id, kind)["in_force"]:
                return None
            if db.scalar(
                select(ReorgBatch.id).where(
                    ReorgBatch.project_id == project.id,
                    ReorgBatch.kind == kind,
                    ReorgBatch.state == "sample",
                )
            ):
                return None
            if not self._ready(db, project, kind):
                return None
        try:
            made = self.prepare(name, kind, alone=True)
        except ProjectConflict:
            return None
        return self.approve(name, UUID(made["id"]))

    def apply_all_alone(self) -> int:
        """Every project and kind where applying without the administrator is in force:
        apply what is ready. How many batches were applied."""
        with self.sessions() as db:
            asking = [
                (name, kind)
                for name, alone in db.execute(
                    select(ProjectRecord.name, ReviewSettings.alone)
                    .join(ReviewSettings, ReviewSettings.project_id == ProjectRecord.id)
                    .where(ProjectRecord.archived_at.is_(None), ReviewSettings.alone.is_not(None))
                )
                for kind, said in (alone or {}).items()
                if said == "apply"
            ]
        return sum(1 for name, kind in asking if self.apply_alone(name, kind) is not None)

    def approve(self, name: str, batch_id: UUID) -> dict[str, Any]:
        """Apply every line of the batch that was not judged wrong, once its sample is
        judged. Each line keeps how the link stood, so that the batch can be undone. A
        line the plan has moved away from is skipped and says why. A batch that is
        applied without the administrator has its sample judged afterwards."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            batch = self._get(db, project, batch_id)
            if batch.state != "sample":
                raise ProjectConflict("This batch is decided already.")
            lines = db.scalars(select(ReorgLine).where(ReorgLine.batch_id == batch.id)).all()
            if not batch.alone and any(line.sampled and line.verdict is None for line in lines):
                raise ProjectConflict("Judge every line of the sample first.")
            kind = batch.kind
            todo = [
                (line.id, line.child_id, line.parent_id, line.reason)
                for line in lines
                if line.verdict != "wrong"
            ]
            # Decided now, so that nothing else can decide it while the lines are applied.
            batch.state, batch.decided_at = "approved", datetime.now(UTC)
        results: dict[UUID, tuple[str, Any, str | None]] = {}
        for line_id, child, parent, reason in todo:
            try:
                before = self._apply(name, kind, child, parent, reason)
                results[line_id] = ("applied", before, None)
            except (ProjectConflict, ProjectInvalid, ProjectNotFound) as error:
                results[line_id] = ("skipped", None, str(error))
        with self.sessions.begin() as db:
            for line_id, (state, before, detail) in results.items():
                line = db.get_one(ReorgLine, line_id)
                line.state, line.before, line.detail = state, before, detail
            batch = db.get_one(ReorgBatch, batch_id)
            lines = db.scalars(select(ReorgLine).where(ReorgLine.batch_id == batch_id)).all()
            return self._shown(db, batch, list(lines))

    def _apply(self, name: str, kind: str, child: UUID, parent: UUID, reason: str) -> Any:
        """Make one line's change, and return how the link stood before."""
        if kind in RETIRED:
            side = RETIRED[kind]
            with self.sessions() as db:
                project = active_project(db, name)
                still = any(
                    entry["proposed"] and entry["kind"] == side and entry["id"] == str(child)
                    for entry in cleanup.candidates(db, project)
                )
            if not still:
                raise ProjectConflict("The reasons against it no longer hold.")
            impact = self.planning.lifecycle.preview(name, side, child, "close")  # type: ignore[arg-type]
            # Closing may reach the record's own children. Nothing but the record itself
            # is closed here: were a child still open, the line is left alone.
            if any(
                entry["action"] == "close" and entry["id"] != str(child)
                for entry in impact["items"]
            ):
                raise ProjectConflict("Closing it would close records under it that are open.")
            closure = None
            if side == "issues":
                mine = next(entry for entry in impact["items"] if entry["id"] == str(child))
                if mine["action"] != "close" or mine.get("github") != "close":
                    raise ProjectConflict("The issue is not an open issue on GitHub any more.")
                closure = ("not_planned", closing_comment(reason))
            self.planning.lifecycle.confirm(
                name,
                side,  # type: ignore[arg-type]
                child,
                "close",
                impact["token"],
                closure=closure,
            )
            return []
        if kind == "close":
            with self.sessions() as db:
                project = active_project(db, name)
                state = db.scalar(
                    select(StoryDelivery.state).where(
                        StoryDelivery.project_id == project.id, StoryDelivery.story_id == child
                    )
                )
            if state != "delivered":
                raise ProjectConflict("The User Story no longer stands as delivered.")
            impact = self.planning.lifecycle.preview(name, "stories", child, "close")
            # Closing a Story closes the issues that are only its own. A delivered Story
            # has none open; were one open by now, closing would reach GitHub: not here.
            if any(
                entry["action"] == "close" and entry["kind"] != "stories"
                for entry in impact["items"]
            ):
                raise ProjectConflict("An issue of the User Story is open by now.")
            self.planning.lifecycle.confirm(name, "stories", child, "close", impact["token"])
            return []
        if kind == "extra_epics":
            before = [
                entry
                for entry in self.links.snapshot(name, "stories", child)
                if entry["parent"] == str(parent)
            ]
            if not before or not epic_links.questionable(
                before[0]["origin"], bool(before[0]["is_primary"])
            ):
                raise ProjectConflict(
                    "The link is gone, or is the primary one, stated or yours by now."
                )
            self.links.before_change(name, "stories", child)
            if not self.planning.remove_epic_link(name, child, parent):
                raise ProjectConflict("The link is gone, or it is the User Story's last Epic.")
            self.links.after_change(name, "stories", child)
            return before
        if kind == "reasons":
            before = self.links.snapshot(name, "issues", child)
            if not self.links.explain(name, "issues", child, parent, reason):
                raise ProjectConflict("The link is gone, or says why by now.")
            return before
        if kind == "primary":
            before = self.links.snapshot(name, "stories", child)
            mine = next((entry for entry in before if entry["parent"] == str(parent)), None)
            if mine is None:
                raise ProjectConflict("The User Story is no longer under this Epic.")
            if any(e["is_primary"] and e["primary_by"] == "administrator" for e in before):
                raise ProjectConflict("You have chosen this User Story's primary Epic since.")
            self.links.set_primary(
                name, "stories", child, parent, why=Note("scorer", reason=reason)
            )
            return before
        if kind == "place":
            with self.sessions() as db:
                project = active_project(db, name)
                linked = db.scalar(
                    select(func.count())
                    .select_from(ProjectIssueStory)
                    .where(
                        ProjectIssueStory.project_id == project.id,
                        ProjectIssueStory.item_id == child,
                    )
                )
                if linked:
                    raise ProjectConflict("The issue has a User Story by now.")
                story = db.get(ProjectUserStory, parent)
                across = story is not None and story.project_id != project.id
            if across:
                # A User Story of a connected project: the link is one of their
                # connection, and says that a model's agreed answer made it.
                with self.sessions.begin() as db:
                    link_issue_across(
                        db, active_project(db, name), child, parent, reason, by="model"
                    )
                return [{"across": True}]
            self.links.before_change(name, "issues", child)
            self.planning.link_stories(name, child, [parent])
            self.links.noted(
                name, "issues", child, parent, Note("scorer", confirmed=True, reason=reason)
            )
            return []
        before = [
            entry
            for entry in self.links.snapshot(name, "issues", child)
            if entry["parent"] == str(parent)
        ]
        if not before or before[0]["origin"] in ("declared", "administrator"):
            raise ProjectConflict("The link is gone, or is stated or chosen by now.")
        self.links.before_change(name, "issues", child)
        if not self.planning.remove_story_link(name, child, parent):
            raise ProjectConflict("The link is gone.")
        self.links.after_change(name, "issues", child)
        return before

    def ready(self, name: str, kind: str) -> list[dict[str, Any]]:
        """The changes of that kind that are ready to be made, the surest first: each
        with the two records, how sure it is, and why."""
        with self.sessions() as db:
            return self.ready_in(db, active_project(db, name), kind)

    def ready_in(self, db: Session, project: ProjectRecord, kind: str) -> list[dict[str, Any]]:
        """The same, within a transaction that is open."""
        return [
            {
                "child": row.child_id,
                "parent": row.parent_id,
                "confidence": float(row.confidence),
                "why": str((row.detail or {}).get("why") or ""),
                "signals": (row.detail or {}).get("signals") or {},
            }
            for row in self._ready(db, project, kind)
        ]

    def apply_one(self, name: str, kind: str, child: UUID, parent: UUID, reason: str) -> Any:
        """Make one change of that kind by itself, outside a batch, and return how
        things stood before: for a run that applies one line at the administrator's
        word."""
        if kind not in KINDS:
            raise ProjectInvalid("That is no kind of change.")
        return self._apply(name, kind, child, parent, reason)

    def revert_one(self, name: str, kind: str, child: UUID, parent: UUID, before: Any) -> None:
        """Take one change back that was made by itself."""
        if kind not in KINDS or kind in FINAL:
            raise ProjectInvalid("That change cannot be taken back from here.")
        self._revert(name, kind, child, parent, before or [])

    def applied(self, name: str, batch_id: UUID) -> list[dict[str, Any]]:
        """The lines of a batch that were applied."""
        with self.sessions() as db:
            project = active_project(db, name)
            batch = self._get(db, project, batch_id)
            return [
                {
                    "id": line.id,
                    "child": line.child_id,
                    "parent": line.parent_id,
                    "reason": line.reason,
                    "before": line.before,
                }
                for line in db.scalars(
                    select(ReorgLine).where(
                        ReorgLine.batch_id == batch.id, ReorgLine.state == "applied"
                    )
                )
            ]

    def undo_line(self, name: str, line_id: UUID) -> None:
        """Put back what one applied line of an approved batch changed. The line counts
        as judged wrong from then on: it is not proposed again, and it is part of the
        record that decides how far its kind of change goes without the administrator."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            line = db.get(ReorgLine, line_id)
            batch = db.get(ReorgBatch, line.batch_id) if line else None
            if line is None or batch is None or batch.project_id != project.id:
                raise ProjectNotFound(str(line_id))
            if line.state != "applied" or batch.kind in FINAL:
                raise ProjectConflict("There is nothing to put back here.")
            kind, child, parent, before = batch.kind, line.child_id, line.parent_id, line.before
        self._revert(name, kind, child, parent, before or [])
        with self.sessions.begin() as db:
            line = db.get_one(ReorgLine, line_id)
            line.state, line.verdict = "undone", "wrong"

    def undo(self, name: str, batch_id: UUID) -> dict[str, Any]:
        """Put back everything an approved batch changed, within thirty days. A line
        whose link has changed since is left as it is now and says so."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            batch = self._get(db, project, batch_id)
            decided = batch.decided_at
            if decided is not None and decided.tzinfo is None:
                decided = decided.replace(tzinfo=UTC)
            if batch.state != "approved" or decided is None:
                raise ProjectConflict("Only an approved batch can be undone.")
            if batch.kind in FINAL:
                raise ProjectConflict(
                    "Issues closed on GitHub are not reopened from here. Reopen them on GitHub."
                )
            days = review_settings.of(db, batch.project_id).count("undo_days")
            if datetime.now(UTC) > decided + timedelta(days=days):
                raise ProjectConflict(f"A batch can be undone for {days} days.")
            kind = batch.kind
            todo = [
                (line.id, line.child_id, line.parent_id, line.before)
                for line in db.scalars(
                    select(ReorgLine).where(
                        ReorgLine.batch_id == batch.id, ReorgLine.state == "applied"
                    )
                )
            ]
            batch.state, batch.undone_at = "undone", datetime.now(UTC)
        results: dict[UUID, tuple[str, str | None]] = {}
        for line_id, child, parent, before in todo:
            try:
                self._revert(name, kind, child, parent, before or [])
                results[line_id] = ("undone", None)
            except (ProjectConflict, ProjectInvalid, ProjectNotFound) as error:
                results[line_id] = ("applied", f"Not put back: {error}")
        with self.sessions.begin() as db:
            for line_id, (state, detail) in results.items():
                line = db.get_one(ReorgLine, line_id)
                line.state, line.detail = state, detail
            batch = db.get_one(ReorgBatch, batch_id)
            lines = db.scalars(select(ReorgLine).where(ReorgLine.batch_id == batch_id)).all()
            return self._shown(db, batch, list(lines))

    def _revert(
        self, name: str, kind: str, child: UUID, parent: UUID, before: list[dict[str, Any]]
    ) -> None:
        if kind == "close" or kind in RETIRED:
            side = RETIRED.get(kind, "stories")
            self.planning.set_state(name, side, child, "open")  # type: ignore[arg-type]
            return
        if kind == "extra_epics":
            self.links.before_change(name, "stories", child)
            if not self.planning.restore_epic_link(name, child, parent):
                raise ProjectConflict("The User Story or the Epic is gone.")
            self.links.after_change(name, "stories", child)
            self.links.restore(name, "stories", child, before)
            return
        if kind in ("reasons", "primary"):
            side = "issues" if kind == "reasons" else "stories"
            if not self.links.restore(name, side, child, before):
                raise ProjectConflict("The link is gone.")
            return
        if kind == "place" and before and before[0].get("across"):
            with self.sessions.begin() as db:
                if not unlink_issue_across(db, active_project(db, name), child, parent):
                    raise ProjectConflict("The link is gone already.")
            return
        if kind == "place":
            self.links.before_change(name, "issues", child)
            if not self.planning.remove_story_link(name, child, parent, remember=False):
                raise ProjectConflict("The link is gone already.")
            self.links.after_change(name, "issues", child)
            return
        self.links.before_change(name, "issues", child)
        if not self.planning.restore_story_link(name, child, parent):
            raise ProjectConflict("The User Story is gone, or the issue is a bug by now.")
        self.links.after_change(name, "issues", child)
        self.links.restore(name, "issues", child, before)
