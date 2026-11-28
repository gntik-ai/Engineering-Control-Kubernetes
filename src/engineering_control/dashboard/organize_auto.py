"""The runs that start by themselves (O6 of docs/design/organize-and-coverage.md).

A run of Organize or of Coverage is started by the administrator, with a button. Where
the project's settings say so it is started here as well: as records and documents
arrive that it has something to do about, or once a night. Such a run is the same run:
it reads, matches and verifies with the models of the settings, applies only what the
settings let it apply without the administrator, puts the rest before them, and stops
at the project's limits. What it did is told in the daily digest and on the Dashboard.

Nothing is started unless the settings ask for it: asking a model costs money, and that
is the administrator's to allow, run by run. A run that is estimated to pass the month's
limit is not started by itself at all: it waits for the administrator's word.

A run that starts by itself takes the place of what ran record by record before: the
checks it asks (they are no longer started by themselves on their own) and the review
each new record got as it arrived.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import auto_checks, review_settings
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.db.models import (
    OrganizeLine,
    OrganizeRun,
    ProjectConnection,
    ProjectRecord,
    ReviewSettings,
)

# What can run by itself, and what it is called.
RUNS: Final = {
    "epics_stories": "Organize: Epics and User Stories",
    "stories_issues": "Organize: User Stories and Issues",
    "coverage": "Coverage",
    "connection": "Connections: carrying out a plan that changed",
}
# The checks a run asks. A setting that let one of them run by itself lets the run now,
# until the settings are saved again and say it for the run.
TAKES: Final = {
    "issue_story": "stories_issues",
    "story_epic": "epics_stories",
    "epic_links": "epics_stories",
}
PAGES: Final = {"epics_stories": "Organize", "stories_issues": "Organize", "coverage": "Coverage"}
LIVE: Final = ("running", "held")
# A run that left as much to do as it found is not started again by itself before this:
# what it could not do, it cannot do half an hour later either.
RETRY: Final = timedelta(hours=24)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def mode_of(runs: Mapping[str, str], kind: str) -> str:
    """Whether that run starts by itself in a project: not at all, as records arrive, or
    once a night."""
    own = runs.get(kind)
    if own in auto_checks.MODES:
        return str(own)
    asked = [runs.get(check) for check, run in TAKES.items() if run == kind]
    return "arrive" if "arrive" in asked else "night" if "night" in asked else "off"


def organized(db: Session, project: UUID) -> bool:
    """Whether a run of Organize starts by itself in the project: new records are then
    organized by the run, and not reviewed one by one as they arrive."""
    runs = review_settings.of(db, project).runs
    return any(mode_of(runs, kind) != "off" for kind in ("epics_stories", "stories_issues"))


def organized_projects(db: Session) -> list[UUID]:
    return [
        key
        for key, runs in db.execute(
            select(ReviewSettings.project_id, ReviewSettings.runs).where(
                ReviewSettings.runs.is_not(None)
            )
        )
        if any(mode_of(runs or {}, kind) != "off" for kind in ("epics_stories", "stories_issues"))
    ]


def _runs(db: Session, project: UUID, kind: str) -> list[OrganizeRun]:
    """The latest runs of a kind in a project that were started: for connections, of
    every connection the project owns."""
    where = [OrganizeRun.project_id == project, OrganizeRun.kind == kind]
    where.append(
        OrganizeRun.connection_id.is_not(None)
        if kind == "connection"
        else OrganizeRun.connection_id.is_(None)
    )
    return list(
        db.scalars(
            select(OrganizeRun)
            .where(*where, OrganizeRun.started_at.is_not(None))
            .order_by(OrganizeRun.started_at.desc(), OrganizeRun.id)
            .limit(20)
        )
    )


def signature(shown: Mapping[str, Any]) -> str:
    """What a run would find to do, as far as the page says it before the run: so that
    a run that changed nothing of it is not started again and again."""
    plan, create = shown.get("plan") or {}, shown.get("create") or {}
    return json.dumps(
        [
            *(
                plan.get(key)
                for key in (
                    "to_ask",
                    "ask_blocked",
                    "stated",
                    "settled",
                    "only",
                    "links_to_ask",
                    "unsupported",
                    "reread",
                )
            ),
            *(create.get(key) for key in ("to_ask", "blocked", "delivered", "splits")),
            bool((shown.get("stands") or {}).get("read", True)),
        ],
        sort_keys=True,
        default=str,
    )


def standing(
    db: Session,
    project: ProjectRecord,
    kind: str,
    now: datetime | None = None,
    shown: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Whether that run starts by itself in this project, and what a start now would
    wait for. Without what the run's page shows, only the mode and the timing are said:
    whether there is something to do is the page's to say."""
    now = now or datetime.now(UTC)
    mine = review_settings.of(db, project.id)
    mode = mode_of(mine.runs, kind)
    runs = _runs(db, project.id, kind)
    last = next((run for run in runs if run.auto), None)
    since = _aware(last.started_at) if last is not None and last.started_at else None
    found: dict[str, Any] = {
        "check": kind,
        "label": RUNS[kind],
        "mode": mode,
        "due": False,
        "why": "",
        "waiting": 0,
        "last": since.isoformat() if since else None,
    }
    if mode == "off":
        return found
    if project.paused_at is not None:
        return {**found, "why": "The project is paused."}
    if any(run.state in LIVE for run in runs):
        return {**found, "why": "A run of it is under way or on hold."}
    if mode == "night":
        if not auto_checks.NIGHT[0] <= now.hour < auto_checks.NIGHT[1]:
            return {**found, "why": "It starts in the night, between 1 and 5 UTC."}
        if since is not None and since > now - timedelta(hours=12):
            return {**found, "why": "It ran this night already."}
    elif since is not None and since > now - auto_checks.EVERY:
        return {**found, "why": "It started by itself less than half an hour ago."}
    if shown is None:
        return found
    if not shown.get("can_start"):
        return {**found, "why": str(shown.get("why_not") or "Nothing waits for it.")}
    plan, create = shown.get("plan") or {}, shown.get("create") or {}
    asks = int(plan.get("to_ask") or 0) if not plan.get("ask_blocked") else 0
    writes = int(create.get("to_ask") or 0) if not create.get("blocked") else 0
    if plan.get("would_pass") or create.get("would_pass"):
        return {
            **found,
            "waiting": asks + writes,
            "why": "It is estimated to pass the month's limit: it waits for your word.",
        }
    if (
        last is not None
        and since is not None
        and since > now - RETRY
        and (last.estimate or {}).get("left") == signature(shown)
    ):
        return {
            **found,
            "waiting": asks + writes,
            "why": "Its last run by itself left as much to do as it found: it is not "
            "started again within a day unless something changes.",
        }
    return {**found, "due": True, "waiting": asks + writes}


def lately(db: Session, project: UUID, since: datetime) -> dict[str, Any]:
    """What the runs that started by themselves did in a project since then: how many,
    what they applied, created and put before the administrator, and what they cost."""
    runs = db.execute(
        select(OrganizeRun.id, OrganizeRun.cost).where(
            OrganizeRun.project_id == project, OrganizeRun.auto, OrganizeRun.started_at > since
        )
    ).all()
    found = {"runs": len(runs), "done": 0, "created": 0, "needs": 0, "dollars": 0.0}
    if not runs:
        return found
    found["dollars"] = round(sum(float(cost or 0.0) for _key, cost in runs), 4)
    for listed, count in db.execute(
        select(OrganizeLine.listed, func.count())
        .where(OrganizeLine.run_id.in_([key for key, _cost in runs]))
        .group_by(OrganizeLine.listed)
    ):
        found[{"needs_you": "needs"}.get(listed, listed)] = int(count)
    return found


def waiting(db: Session, project: UUID) -> dict[str, int]:
    """How many cases wait for the administrator on each page of Reviews."""
    found = dict.fromkeys(("Organize", "Coverage"), 0)
    for kind, count in db.execute(
        select(OrganizeRun.kind, func.count())
        .select_from(OrganizeLine)
        .join(OrganizeRun, OrganizeRun.id == OrganizeLine.run_id)
        .where(
            OrganizeLine.project_id == project,
            OrganizeLine.listed == "needs_you",
            OrganizeLine.state == "open",
            OrganizeRun.connection_id.is_(None),
        )
        .group_by(OrganizeRun.kind)
    ):
        if kind in PAGES:
            found[PAGES[kind]] += int(count)
    return found


def _some(count: int, one: str, many: str) -> str:
    return f"{count:,} {one if count == 1 else many}"


def digest_lines(sessions: sessionmaker[Session], since: datetime) -> list[str]:
    """For the daily digest: for each project what the runs that started by themselves
    did since then, and how many cases wait for the administrator. Nothing for a project
    where neither is so."""
    lines = []
    with sessions() as db:
        for key, name in db.execute(
            select(ProjectRecord.id, ProjectRecord.name)
            .where(ProjectRecord.archived_at.is_(None))
            .order_by(ProjectRecord.name)
        ):
            did, waits = lately(db, key, since), waiting(db, key)
            parts = []
            if did["runs"]:
                parts.append(
                    f"{_some(did['runs'], 'run', 'runs')} by "
                    f"{'itself' if did['runs'] == 1 else 'themselves'}: "
                    f"{did['done']:,} applied, {did['created']:,} created, "
                    f"{did['needs']:,} put before you, ${did['dollars']:,.2f}"
                )
            if any(waits.values()):
                parts.append(
                    _some(sum(waits.values()), "case waits", "cases wait")
                    + " for you ("
                    + ", ".join(f"{count:,} under {page}" for page, count in waits.items() if count)
                    + ")"
                )
            if parts:
                lines.append(f"- **{name}**: {'; '.join(parts)}.")
    return ["Organize and Coverage:", *lines] if lines else []


class AutoRuns:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        services: Mapping[str, Any],
        connections: Any = None,
    ) -> None:
        self.sessions = sessions
        # For each kind of run: what shows its page and starts it, as its button does.
        self.services = services
        self.connections = connections

    def asked_for(self) -> list[tuple[str, str]]:
        """The projects and runs whose settings ask for a start by itself."""
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
                for kind in RUNS
                if mode_of(runs or {}, kind) != "off"
            ]

    def standing(self, name: str, kind: str, now: datetime | None = None) -> dict[str, Any]:
        """Whether the run starts by itself in the project, with what its page says
        there is to do."""
        shown = None
        if kind in self.services:
            with self.sessions() as db:
                project = active_project(db, name)
                # The page is worked out only where the run is set to start by itself.
                wanted = mode_of(review_settings.of(db, project.id).runs, kind) != "off"
            if wanted:
                shown = self.services[kind].shown(name)
        with self.sessions() as db:
            project = active_project(db, name)
            return standing(db, project, kind, now, shown)

    def standings(self, name: str) -> dict[str, Any]:
        """For each run: whether it starts by itself in the project, and what it waits
        for now."""
        return {"runs": [self.standing(name, kind) for kind in RUNS]}

    def _mark(self, key: UUID, said: str | None) -> None:
        with self.sessions.begin() as db:
            run = db.get(OrganizeRun, key)
            if run is not None:
                run.auto = True
                if said is not None:
                    run.estimate = {**(run.estimate or {}), "by_itself": said}

    def settle(self) -> int:
        """For each run that started by itself and ended: note what it left to do, as its
        page says it then. A run is started again by itself when that changes: a run
        that could not do what is left cannot do it half an hour later either."""
        with self.sessions() as db:
            ended = [
                (run.id, name, run.kind)
                for run, name in db.execute(
                    select(OrganizeRun, ProjectRecord.name)
                    .join(ProjectRecord, ProjectRecord.id == OrganizeRun.project_id)
                    .where(
                        OrganizeRun.auto,
                        OrganizeRun.connection_id.is_(None),
                        OrganizeRun.state.not_in(LIVE),
                        OrganizeRun.started_at > datetime.now(UTC) - RETRY,
                        ProjectRecord.archived_at.is_(None),
                    )
                )
                if run.kind in self.services and "left" not in (run.estimate or {})
            ]
        for key, name, kind in ended:
            try:
                said = signature(self.services[kind].shown(name))
            except (ProjectConflict, ProjectInvalid, ProjectNotFound):
                continue
            with self.sessions.begin() as db:
                run = db.get(OrganizeRun, key)
                if run is not None:
                    run.estimate = {**(run.estimate or {}), "left": said}
        return len(ended)

    def _carry_out(self, name: str, now: datetime) -> bool:
        """Carry out the plan of every connection the project owns that changed and
        waits for the word, as far as nothing stands against it."""
        if self.connections is None:
            return False
        with self.sessions() as db:
            project = active_project(db, name)
            if not standing(db, project, "connection", now, {"can_start": True})["due"]:
                return False
            keys = list(
                db.scalars(
                    select(ProjectConnection.id)
                    .where(
                        ProjectConnection.owner_id == project.id,
                        ProjectConnection.archived_at.is_(None),
                        ProjectConnection.status != "paused",
                    )
                    .order_by(ProjectConnection.created_at, ProjectConnection.id)
                )
            )
        started = False
        for key in keys:
            try:
                plan = self.connections.plan(name, key)
                ask = (plan.get("plan") or {}).get("ask") or {}
                # A plan estimated to pass the limit waits for the administrator.
                if not plan["waits"] or not plan["can_carry_out"] or ask.get("over"):
                    continue
                made = self.connections.carry_out(name, key)
                self._mark(UUID(str(made["id"])), None)
                started = True
            except (ProjectConflict, ProjectInvalid, ProjectNotFound):
                continue
        return started

    def start_due(self, now: datetime | None = None) -> list[tuple[str, str]]:
        """Start every run that is due. Which projects and runs were started."""
        now = now or datetime.now(UTC)
        started = []
        for name, kind in self.asked_for():
            try:
                if kind == "connection":
                    if self._carry_out(name, now):
                        started.append((name, kind))
                    continue
                service = self.services.get(kind)
                if service is None:
                    continue
                with self.sessions() as db:
                    project = active_project(db, name)
                    if standing(db, project, kind, now)["why"]:
                        # The timing says no: the page is not worked out for nothing.
                        continue
                shown = service.shown(name)
                with self.sessions() as db:
                    project = active_project(db, name)
                    if not standing(db, project, kind, now, shown)["due"]:
                        continue
                made = service.start(name)
                self._mark(UUID(str(made["id"])), signature(shown))
                started.append((name, kind))
            except (ProjectConflict, ProjectInvalid, ProjectNotFound):
                # Something else started it, a limit was reached, or nothing waits any
                # more: tried again at the next pass.
                continue
        return started


__all__ = [
    "RUNS",
    "TAKES",
    "AutoRuns",
    "digest_lines",
    "lately",
    "mode_of",
    "organized",
    "organized_projects",
    "standing",
    "waiting",
]
