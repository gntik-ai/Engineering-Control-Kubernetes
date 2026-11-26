"""One list of what waits for the administrator, across all of Reviews (J6 of
docs/design/judged-reviews.md).

Every workflow of Reviews has its own view, and each says what waits there. This reads
them all and says it once, in rows of three groups:

- ``decide``: what is the administrator's to decide: batches to approve, lines applied
  without them to judge, answers of a check two models did not settle, reviews that ask
  a question, proposals.
- ``held``: what stopped at a limit, a pause or a failure and goes on at their word.
- ``start``: records no model was asked about yet, where the check is started by hand.
- ``doubt``: answers two models did not agree on where that decides nothing: the record
  is left as it is and nothing is proposed. Not a decision that waits, and not a next
  step: it is there to be looked at.

A row says how many, what, and where it is decided: a view of Reviews and the panel in
it. Nothing is decided here and nothing is counted in a way of its own: each number is
the one its view shows. The next steps of a project, on the dashboard and in the daily
digest, are these rows.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from engineering_control.dashboard import (
    auto_checks,
    evidence_verdicts,
    parent_choice,
    reorg,
    review_settings,
    structure,
)
from engineering_control.dashboard.issue_types import bugs_with_optional_stories
from engineering_control.dashboard.planning_analysis import selection
from engineering_control.dashboard.planning_drift import drift
from engineering_control.dashboard.project_records import active_project
from engineering_control.dashboard.reorg import Reorg
from engineering_control.dashboard.signals import BUCKETS, ISSUE_STORY
from engineering_control.db.models import (
    ChoiceRun,
    GoldPair,
    PlanningReview,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectUserStory,
    RecordVector,
    ReviewEvidenceJob,
    ScorerReport,
    WorkspaceCandidate,
    WorkspaceImport,
)

GROUPS: Final = ("decide", "held", "start", "doubt")
# The all-projects table reads every project's list: a list is used again for this long
# there. A project's own view always works it out anew.
FRESH: Final = timedelta(minutes=5)
_KEPT: dict[UUID, tuple[datetime, dict[str, Any]]] = {}

# Each check: the view and the panel its answers are decided in, what a record of it is
# called, and what its answers say.
CHECKS: Final[dict[str, dict[str, str]]] = {
    "issue_story": {
        "view": "related-work",
        "anchor": "issues-without-story",
        "one": "open issue with no User Story",
        "many": "open issues with no User Story",
        "about": "which User Story an issue belongs to",
    },
    "story_epic": {
        "view": "planning",
        "anchor": "primary-epics",
        "one": "User Story under several Epics",
        "many": "User Stories under several Epics",
        "about": "the primary Epic of a User Story",
    },
    "story_needed": {
        "view": "reorganize",
        "anchor": "clean-up",
        "one": "User Story that nothing supports",
        "many": "User Stories that nothing supports",
        "about": "whether a User Story that nothing supports is still needed",
    },
    "epic_links": {
        "view": "reorganize",
        "anchor": "epic-links",
        "one": "User Story with Epics beyond its primary one",
        "many": "User Stories with Epics beyond their primary one",
        "about": "whether a User Story has to do with its other Epics",
    },
    "epic_overview": {
        "view": "epics",
        "anchor": "epic-pages",
        "one": "Epic overview",
        "many": "Epic overviews",
        "about": "what an Epic's overview says",
    },
}
# The checks whose worklist tells every way an answer can need the administrator; of the
# others, an answer needs them where the two models differ.
WORKLISTS: Final = frozenset(reorg.ALONE.values())
DIFFER: Final = ("differ", "unclear")
# For those others: what the two models differ on, and what follows from it. Nothing
# does: the record is left as it is.
LEFT: Final = {
    "story_needed": (
        "the two models did not agree whether it is still needed.",
        "the two models did not agree whether they are still needed.",
        "A Story they differ on is not proposed for closing.",
    ),
    "epic_links": (
        "the two models did not say the same of every link.",
        "the two models did not say the same of every link.",
        "A link they differ on stays, and is marked in doubt there.",
    ),
    "epic_overview": (
        "the judge found a sentence the records do not support.",
        "the judge found a sentence the records do not support.",
        "The sentence is marked on the Epic's page.",
    ),
}
TABLES: Final = {"epics": ("Epic", "Epics"), "stories": ("User Story", "User Stories")}


def _some(count: int, one: str, many: str) -> str:
    return f"{count:,} {one if count == 1 else many}"


def _row(
    key: str,
    group: str,
    count: int,
    says: str,
    view: str,
    anchor: str = "",
    detail: str = "",
    tab: str = "",
) -> dict[str, Any]:
    return {
        "key": key,
        "group": group,
        "count": count,
        "says": says,
        "detail": detail,
        # Where it is decided: a view of Reviews, "bugs", or "tables" with the tab of
        # the planning tables; and the panel of the view.
        "view": view,
        "anchor": anchor,
        "tab": tab,
    }


def _count(db: Session, model: Any, *where: Any) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def _changes(overview: dict[str, Any]) -> list[dict[str, Any]]:
    """The batches: changes that wait for approval, and lines applied without the
    administrator that wait to be judged."""
    rows: list[dict[str, Any]] = []
    parts, total = [], 0
    for kind in overview["kinds"]:
        opened = kind["open"]
        count = int(kind["ready"]) + (int(opened["lines"]) if opened else 0)
        if not count:
            continue
        total += count
        part = f"{kind['title']}: {count:,}"
        if opened:
            part += (
                f" (a batch is open; {opened['sample_judged']:,} of "
                f"{opened['sample']:,} sampled lines judged)"
            )
        parts.append(part)
    if total:
        rows.append(
            _row(
                "changes",
                "decide",
                total,
                _some(total, "change waits", "changes wait") + " for your approval.",
                "reorganize",
                "changes",
                "; ".join(parts) + ".",
            )
        )
    judged = [
        (kind["title"], int(kind["alone"]["waiting"]))
        for kind in overview["kinds"]
        if kind["alone"] and kind["alone"]["waiting"]
    ]
    if judged:
        waiting = sum(count for _title, count in judged)
        rows.append(
            _row(
                "applied",
                "decide",
                waiting,
                _some(waiting, "line applied without you waits", "lines applied without you wait")
                + " to be judged.",
                "reorganize",
                "changes",
                "; ".join(f"{title}: {count:,}" for title, count in judged) + ".",
            )
        )
    for kind in overview["kinds"]:
        alone = kind["alone"]
        # Applying was asked for and its own record stopped it.
        if alone and alone["setting"] == "apply" and alone["judged"] >= alone["needed"]:
            if alone["why"]:
                rows.append(
                    _row(
                        "stopped:" + kind["kind"],
                        "held",
                        0,
                        f"“{kind['title']}” is no longer applied without you.",
                        "reorganize",
                        "changes",
                        alone["why"],
                    )
                )
    return rows


def _checks(db: Session, project: ProjectRecord) -> list[dict[str, Any]]:
    """For each check: the answers that need the administrator, a run that stopped, and
    the records nobody was asked about where the check does not start by itself."""
    rows: list[dict[str, Any]] = []
    over = review_settings.over(db, project.id)
    held = False
    for kind, said in CHECKS.items():
        found: Counter[str] = parent_choice.standings(db, project, kind)
        needs = parent_choice.NEEDS if kind in WORKLISTS else DIFFER
        doubt = sum(found[key] for key in needs)
        if doubt and kind in WORKLISTS:
            # An issue with no User Story, a Story with no primary Epic: it stays
            # undecided until the administrator says.
            why = "; ".join(
                f"{found[key]:,}: {parent_choice.SAID[key].lower()}" for key in needs if found[key]
            )
            rows.append(
                _row(
                    "doubt:" + kind,
                    "decide",
                    doubt,
                    _some(doubt, "answer", "answers")
                    + f" about {said['about']} "
                    + ("needs" if doubt == 1 else "need")
                    + " you.",
                    said["view"],
                    said["anchor"],
                    why + ".",
                )
            )
        elif doubt:
            one, many, left = LEFT[kind]
            rows.append(
                _row(
                    "doubt:" + kind,
                    "doubt",
                    doubt,
                    _some(doubt, said["one"], said["many"]) + ": " + (one if doubt == 1 else many),
                    said["view"],
                    said["anchor"],
                    left,
                )
            )
        run = db.scalar(
            select(ChoiceRun)
            .where(ChoiceRun.project_id == project.id, ChoiceRun.kind == kind)
            .order_by(ChoiceRun.created_at.desc(), ChoiceRun.id)
            .limit(1)
        )
        name = parent_choice.CHECKS[kind]
        unasked = found["unasked"] + found["stale"]
        if run is not None and run.state in ("held", "paused"):
            held = held or run.state == "held"
            rows.append(
                _row(
                    "run:" + kind,
                    "held",
                    max(0, int(run.total or 0) - int(run.done or 0) - int(run.failed or 0)),
                    f"The check “{name}” "
                    + ("is on hold" if run.state == "held" else "was stopped")
                    + f" at {int(run.done or 0):,} of {int(run.total or 0):,}.",
                    said["view"],
                    said["anchor"],
                    run.detail
                    or (
                        "Resume it or end it."
                        if run.state == "paused"
                        else "It goes on when what holds it is lifted."
                    ),
                )
            )
        elif run is not None and run.state == "failed" and unasked:
            rows.append(
                _row(
                    "run:" + kind,
                    "held",
                    unasked,
                    f"The last run of the check “{name}” failed.",
                    said["view"],
                    said["anchor"],
                    run.detail or "",
                )
            )
        elif unasked and (run is None or run.state not in parent_choice.LIVE):
            mode = auto_checks.standing(db, project, kind)
            if mode["mode"] == "off":
                rows.append(
                    _row(
                        "unasked:" + kind,
                        "start",
                        unasked,
                        _some(unasked, said["one"] + " has", said["many"] + " have")
                        + " no answer of a model."
                        if kind != "epic_overview"
                        else _some(unasked, "Epic overview is", "Epic overviews are")
                        + " not written, or older than the Epic's records.",
                        said["view"],
                        said["anchor"],
                        "Start the check there, or let it run by itself under Settings.",
                    )
                )
            elif mode["why"] == parent_choice.NO_ANSWERING_MODEL:
                rows.append(
                    _row(
                        "unasked:" + kind,
                        "held",
                        unasked,
                        f"The check “{name}” is set to run by itself and has no model to ask.",
                        "settings",
                        "",
                        parent_choice.NO_ANSWERING_MODEL,
                    )
                )
    if over and not held:
        rows.append(
            _row("limit", "held", 0, "The reviews are at their limit.", "settings", "", over)
        )
    return rows


def _planning_reviews(db: Session, project: ProjectRecord) -> list[dict[str, Any]]:
    counts = dict(
        db.execute(
            select(PlanningReview.status, func.count())
            .where(PlanningReview.project_id == project.id)
            .group_by(PlanningReview.status)
        )
        .tuples()
        .all()
    )
    rows: list[dict[str, Any]] = []
    asking, failed, queued = (
        int(counts.get(key, 0)) for key in ("needs_input", "failed", "queued")
    )
    if asking:
        rows.append(
            _row(
                "planning:asking",
                "decide",
                asking,
                _some(asking, "planning review asks", "planning reviews ask") + " you a question.",
                "planning",
                "planning-reviews",
                "Where a judge named other parents than the review, nothing was linked.",
            )
        )
    if failed:
        rows.append(
            _row(
                "planning:failed",
                "held",
                failed,
                _some(failed, "planning review", "planning reviews") + " failed.",
                "planning",
                "planning-reviews",
                "Each says why, and can be tried again.",
            )
        )
    if queued:
        provider, model = selection(project, db.get(WorkspaceImport, project.id))
        why = (
            "The automatic reviews of this project are stopped. Resume them there."
            if project.planning_reviews_paused_at
            else "The project is paused."
            if project.paused_at
            else "No model is chosen for them."
            if not (provider and model)
            else ""
        )
        if why:
            rows.append(
                _row(
                    "planning:queued",
                    "held",
                    queued,
                    _some(queued, "planning review is", "planning reviews are")
                    + " queued and none is asked.",
                    "planning",
                    "planning-reviews",
                    why,
                )
            )
    return rows


def _proposals(db: Session, project: ProjectRecord) -> list[dict[str, Any]]:
    """The proposals that wait in the planning tables, as the tables list them."""
    found = db.execute(
        select(WorkspaceCandidate.kind, WorkspaceCandidate.judged)
        .outerjoin(WorkspaceImport, WorkspaceImport.project_id == WorkspaceCandidate.project_id)
        .where(
            WorkspaceCandidate.project_id == project.id,
            WorkspaceCandidate.decision == "pending",
            or_(
                WorkspaceImport.stage == WorkspaceCandidate.kind,
                WorkspaceCandidate.origin != "import",
            ),
        )
    ).all()
    by_kind = Counter(kind for kind, _judged in found)
    doubted = sum(1 for _kind, judged in found if judged and judged.get("agreed") is False)
    words = {**TABLES, "issues": ("Issue", "Issues")}
    rows = []
    if found:
        first = next((kind for kind in words if by_kind.get(kind)), "epics")
        rows.append(
            _row(
                "proposals",
                "decide",
                len(found),
                _some(len(found), "proposal waits", "proposals wait")
                + " for your decision in the planning tables.",
                "tables",
                "",
                ", ".join(_some(by_kind[kind], *words[kind]) for kind in words if by_kind.get(kind))
                + (f"; the judge did not propose {doubted:,} of them." if doubted else "."),
                first,
            )
        )
    return rows


def _evidence(db: Session, project: ProjectRecord) -> list[dict[str, Any]]:
    rows = []
    waiting = evidence_verdicts.waiting(db, project.id)
    if waiting:
        rows.append(
            _row(
                "evidence",
                "decide",
                waiting,
                _some(
                    waiting, "verdict of Review evidence waits", "verdicts of Review evidence wait"
                )
                + " for your review.",
                "related-work",
                "review-evidence",
            )
        )
    cut_off = _count(
        db,
        ReviewEvidenceJob,
        ReviewEvidenceJob.project_id == project.id,
        ReviewEvidenceJob.state == "uncertain",
    )
    if cut_off:
        rows.append(
            _row(
                "evidence:cut-off",
                "held",
                cut_off,
                _some(cut_off, "run of Review evidence was", "runs of Review evidence were")
                + " cut off and never said what "
                + ("it" if cut_off == 1 else "they")
                + " used.",
                "related-work",
                "review-evidence",
                "Each keeps its tokens reserved until you release it.",
            )
        )
    return rows


def _rest(db: Session, project: ProjectRecord) -> list[dict[str, Any]]:
    """Proposals for the structure, bugs that still carry a link, and the pairs to judge
    while the scorer is not measured on this project's own links."""
    pid = project.id
    rows = []
    shape = structure.survey(db, project)
    splits, missing = len(shape["splits"]), len(shape["stories"])
    if splits + missing:
        rows.append(
            _row(
                "structure",
                "decide",
                splits + missing,
                _some(
                    splits + missing,
                    "proposal for the structure waits",
                    "proposals for the structure wait",
                )
                + " for your word.",
                "reorganize",
                "structure",
                "; ".join(
                    part
                    for part in (
                        _some(splits, "Epic to split", "Epics to split") if splits else "",
                        _some(
                            missing, "User Story that is missing", "User Stories that are missing"
                        )
                        if missing
                        else "",
                    )
                    if part
                )
                + ".",
            )
        )
    linked = int(
        db.scalar(
            select(func.count(func.distinct(ProjectIssueStory.item_id))).where(
                ProjectIssueStory.project_id == pid,
                ProjectIssueStory.item_id.in_(bugs_with_optional_stories(db, pid)),
            )
        )
        or 0
    )
    if linked:
        rows.append(
            _row(
                "bugs",
                "decide",
                linked,
                _some(linked, "bug still carries", "bugs still carry") + " a link to a User Story.",
                "bugs",
                "",
                "A bug stands apart from the plan: it has no User Story and no Epic.",
            )
        )
    vectors = _count(db, RecordVector, RecordVector.project_id == pid)
    if not vectors:
        rows.append(
            _row(
                "ranking",
                "held",
                0,
                "The ranking service has not read its records yet.",
                "reorganize",
                "parents-found",
                "Candidates are found by shared words alone until it has.",
            )
        )
        return rows
    report = db.scalar(select(ScorerReport.report).where(ScorerReport.project_id == pid))
    measured = {
        entry["kind"]: bool(entry.get("measured_here")) for entry in (report or {}).get("kinds", [])
    }
    if not all(measured.get(kind) for kind in (ISSUE_STORY, "story_epic")):
        judged = _count(db, GoldPair, GoldPair.project_id == pid, GoldPair.label.is_not(None))
        drawn = _count(db, GoldPair, GoldPair.project_id == pid)
        wanted = max(drawn, sum(count for _kind, count in BUCKETS.values()))
        rows.append(
            _row(
                "pairs",
                "decide",
                max(0, wanted - judged),
                f"Judge the pairs ({judged:,} of {wanted:,} judged).",
                "reorganize",
                "pairs",
                "The scorer is not yet measured on this project's own links.",
            )
        )
    return rows


def _lately(db: Session, project: ProjectRecord, now: datetime) -> dict[str, Any]:
    """What the checks did by themselves in the last day, and what moved between the
    last two measurements of the plan."""
    runs = db.execute(
        select(
            ChoiceRun.done,
            ChoiceRun.cost,
            ChoiceRun.first_cost,
            ChoiceRun.judge_cost,
            ChoiceRun.third_cost,
        ).where(
            ChoiceRun.project_id == project.id,
            ChoiceRun.auto,
            ChoiceRun.created_at > now - timedelta(days=1),
        )
    ).all()
    moved = drift(db, project.id)
    return {
        "runs": len(runs),
        "answers": sum(int(done or 0) for done, *_costs in runs),
        "dollars": round(
            sum(float(part or 0.0) for _done, *costs in runs for part in costs),
            4,
        ),
        "moved": (moved or {}).get("moved", []),
        "before": (moved or {}).get("before"),
        "measured": (moved or {}).get("day"),
    }


class NeedsYou:
    def __init__(self, reorg: Reorg) -> None:
        self.sessions = reorg.sessions
        self.reorg = reorg

    def listed(self, name: str, *, fresh: bool = True) -> dict[str, Any]:
        """Everything in Reviews that waits for the administrator in one project."""
        now = datetime.now(UTC)
        with self.sessions() as db:
            project = active_project(db, name)
            kept = _KEPT.get(project.id)
            if not fresh and kept is not None and now - kept[0] < FRESH:
                return kept[1]
            pid = project.id
            planned = bool(
                _count(
                    db,
                    ProjectEpic,
                    ProjectEpic.project_id == pid,
                    ProjectEpic.archived_at.is_(None),
                )
                and _count(
                    db,
                    ProjectUserStory,
                    ProjectUserStory.project_id == pid,
                    ProjectUserStory.archived_at.is_(None),
                    ProjectUserStory.closed_at.is_(None),
                )
            )
        rows: list[dict[str, Any]]
        lately: dict[str, Any] = {}
        if not planned:
            rows = [
                _row(
                    "discovery",
                    "start",
                    0,
                    "It has no Epics and no User Stories: bring it through Discovery first.",
                    "discovery",
                    "",
                    "Until then there is nothing to reorganize.",
                )
            ]
        else:
            # The batches bring the records of the links in step first, so that every
            # number below counts the plan as it is.
            overview = self.reorg.overview(name)
            with self.sessions() as db:
                project = active_project(db, name)
                rows = [
                    *_changes(overview),
                    *_checks(db, project),
                    *_planning_reviews(db, project),
                    *_proposals(db, project),
                    *_evidence(db, project),
                    *_rest(db, project),
                ]
                lately = _lately(db, project, now)
        rows.sort(key=lambda row: GROUPS.index(row["group"]))
        found = {
            "project": name,
            "planned": planned,
            "rows": rows,
            "counts": {group: sum(1 for row in rows if row["group"] == group) for group in GROUPS},
            "lately": lately,
            "computed_at": now.isoformat(),
        }
        _KEPT[pid] = (now, found)
        return found

    def steps(self, name: str, *, fresh: bool = True) -> list[dict[str, str]]:
        """A project's next steps: its rows that wait for something. For the table of
        all projects they are taken as they were worked out at most a few minutes ago."""
        return [
            {"view": row["view"], "says": row["says"], "anchor": row["anchor"], "tab": row["tab"]}
            for row in self.listed(name, fresh=fresh)["rows"]
            # What was left in doubt and decides nothing is no step.
            if row["group"] != "doubt"
        ]
