"""The checks that run by themselves (J4 of docs/design/judged-reviews.md).

A check that asks a model about a project's records is started by the administrator,
from its worklist. Where the project's settings say so it is started here as well: as
records arrive that have no answer, or once a night. Such a run is like any other: it
takes the model that answers, the judge and the tokens it stops at from the settings,
stops at the project's limit in dollars, and can be stopped and ended from its worklist.

Nothing is started unless the settings ask for it: asking a model costs money, and that
is the administrator's to allow, check by check.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import parent_choice, review_settings
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.db.models import ChoiceRun, ProjectRecord, ReviewSettings

# Not started by itself; started when records wait that have no answer; started once a
# night for all that wait by then.
MODES: Final = ("off", "arrive", "night")
# Between two runs a check starts by itself as records arrive.
EVERY: Final = timedelta(minutes=30)
# The hours of the night, in UTC, in which a nightly run starts.
NIGHT: Final = (1, 5)
# A record a model gave no usable answer about is not asked again by itself before this.
RETRY: Final = timedelta(hours=24)
# The checks a run of Organize asks. They are not started by themselves on their own
# any more: the run is, where the settings say so, and it gathers what they answer.
BY_RUN: Final = frozenset({"issue_story", "story_epic", "epic_links"})


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def standing(
    db: Session, project: ProjectRecord, kind: str, now: datetime | None = None
) -> dict[str, Any]:
    """Whether the check runs by itself in this project, and what a run started now would
    wait for: the mode the settings say, whether a run is due, and why not where it is
    not."""
    now = now or datetime.now(UTC)
    mine = review_settings.of(db, project.id)
    mode = mine.runs.get(kind, "off")
    mode = mode if mode in MODES else "off"
    found: dict[str, Any] = {"check": kind, "mode": mode, "due": False, "why": "", "waiting": 0}
    if mode == "off":
        return found
    if parent_choice.planned(db, project.id, kind)["answer"] is None:
        return {**found, "why": parent_choice.NO_ANSWERING_MODEL}
    over = review_settings.over(db, project.id)
    if over:
        return {**found, "why": over}
    runs = db.scalars(
        select(ChoiceRun)
        .where(ChoiceRun.project_id == project.id, ChoiceRun.kind == kind)
        .order_by(ChoiceRun.created_at.desc(), ChoiceRun.id)
        .limit(20)
    ).all()
    if any(run.state in parent_choice.LIVE for run in runs):
        return {**found, "why": "A run of it is under way, stopped or on hold."}
    last = next((run for run in runs if run.auto), None)
    since = _aware(last.created_at) if last is not None else None
    if mode == "night":
        if not NIGHT[0] <= now.hour < NIGHT[1]:
            return {**found, "why": "It starts in the night, between 1 and 5 UTC."}
        if since is not None and since > now - timedelta(hours=12):
            return {**found, "why": "It ran this night already."}
    elif since is not None and since > now - EVERY:
        return {**found, "why": "It started by itself less than half an hour ago."}
    # What waits for an answer, without what a model failed on lately: a record no
    # model can answer is not paid for again every half hour.
    spec = parent_choice.KINDS[kind]
    scope = spec.scope(db, project)
    if scope.refusal():
        return {**found, "why": str(scope.refusal())}
    known = parent_choice.reviews(db, project.id, kind)
    waiting = 0
    for child in scope.children():
        review = known.get(child.id)
        current = parent_choice.standing(scope, child, scope.shortlist(child), review)
        if not parent_choice.to_ask(review, current, False):
            continue
        if (
            review is not None
            and review.state == "failed"
            and _aware(review.updated_at) > now - RETRY
        ):
            continue
        waiting += 1
    if not waiting:
        return {**found, "why": "Every record has an answer."}
    return {**found, "due": True, "waiting": waiting}


def digest_lines(sessions: sessionmaker[Session], since: datetime) -> list[str]:
    """For the daily digest: what the checks that run by themselves did since then, for
    each project: how many runs, how many answers, and what they cost. Nothing where
    none ran."""
    found: dict[str, list[float]] = {}
    with sessions() as db:
        for name, done, cost, first, judge, third in db.execute(
            select(
                ProjectRecord.name,
                ChoiceRun.done,
                ChoiceRun.cost,
                ChoiceRun.first_cost,
                ChoiceRun.judge_cost,
                ChoiceRun.third_cost,
            )
            .join(ProjectRecord, ProjectRecord.id == ChoiceRun.project_id)
            .where(ChoiceRun.auto, ChoiceRun.created_at > since)
            .order_by(ProjectRecord.name)
        ):
            entry = found.setdefault(name, [0, 0, 0.0])
            entry[0] += 1
            entry[1] += int(done or 0)
            entry[2] += sum(float(part or 0.0) for part in (cost, first, judge, third))
    lines = [
        f"- **{name}**: {int(runs):,} {'run' if runs == 1 else 'runs'}, "
        f"{int(answers):,} {'answer' if answers == 1 else 'answers'}, ${spent:,.2f}."
        for name, (runs, answers, spent) in found.items()
    ]
    return ["Checks that ran by themselves:", *lines] if lines else []


class AutoChecks:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        starters: Mapping[str, Callable[..., dict[str, Any]]],
    ) -> None:
        self.sessions = sessions
        # For each check: what starts a run of it over what has no answer, by the
        # project's name. The same a worklist's button calls.
        self.starters = starters

    def asked_for(self) -> list[tuple[str, str]]:
        """The projects and checks whose settings ask for a run by itself."""
        with self.sessions() as db:
            return [
                (name, kind)
                for name, runs in db.execute(
                    select(ProjectRecord.name, ReviewSettings.runs)
                    .join(ReviewSettings, ReviewSettings.project_id == ProjectRecord.id)
                    .where(
                        ProjectRecord.archived_at.is_(None),
                        ProjectRecord.paused_at.is_(None),
                        ReviewSettings.runs.is_not(None),
                    )
                    .order_by(ProjectRecord.name)
                )
                for kind, mode in (runs or {}).items()
                if mode in MODES[1:] and kind in self.starters and kind not in BY_RUN
            ]

    def start_due(self, now: datetime | None = None) -> list[tuple[str, str]]:
        """Start every run that is due. Which projects and checks were started."""
        now = now or datetime.now(UTC)
        started = []
        for name, kind in self.asked_for():
            try:
                with self.sessions() as db:
                    project = active_project(db, name)
                    if not standing(db, project, kind, now)["due"]:
                        continue
                made = self.starters[kind](name, "", "", None)
                with self.sessions.begin() as db:
                    run = db.get(ChoiceRun, UUID(str(made["id"])))
                    if run is not None:
                        run.auto = True
                started.append((name, kind))
            except (ProjectConflict, ProjectInvalid, ProjectNotFound):
                # Something else started it, the limit was reached, or nothing waits any
                # more: tried again at the next pass.
                continue
        return started
