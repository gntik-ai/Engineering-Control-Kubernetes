"""Triggers: what starts a section's flow without anyone asking.

A trigger belongs to a section of a project and says when its flow starts:

- ``schedule``: every day or every week at an hour (UTC). It runs what the section can
  run by itself, an audit or a search, with the scope the trigger names.
- ``push``: when the project's default branch has moved since the trigger last looked,
  and not more often than its pause between runs. It runs the same.
- ``item``: when an item arrives in the section (routed there, open, with no run yet), it
  is started, as many in a day as the trigger allows.

A trigger starts nothing the administrator could not start by hand, and under the same
conditions: a paused project starts nothing, budgets hold, and a run that is already
under way is not started twice. What a trigger did, or why it did nothing, is kept with
it and shown on the project's page.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
)
from engineering_control.db.models import (
    BacklogItem,
    FlowTrigger,
    ProjectRecord,
    ProjectSection,
    SandboxRun,
)
from engineering_control.engine.library import document_in_force, section_action
from engineering_control.github_app import GitHubApp, GitHubAppError
from engineering_control.sandbox.deliver import toolchain_for
from engineering_control.sandbox.queues import Budgets
from engineering_control.sandbox.runs import Runs

LOG = structlog.get_logger(__name__)
KINDS: Final = ("schedule", "push", "item")
WEEKDAYS: Final = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_PER_SECTION: Final = 6
# How often a moved branch is looked for, whatever the triggers say.
PUSH_LOOK_EVERY: Final = timedelta(minutes=10)
ITEM_SOURCES: Final = ("any", "github", "flow")


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _int(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def clean_spec(kind: str, raw: dict[str, Any] | None) -> dict[str, Any]:
    """The details of a trigger, bounded and in a known shape."""
    raw = raw if isinstance(raw, dict) else {}
    scope = " ".join(str(raw.get("scope") or "").split())[:2000]
    if kind == "schedule":
        every = "week" if raw.get("every") == "week" else "day"
        spec: dict[str, Any] = {"every": every, "hour": _int(raw.get("hour"), 0, 23, 3)}
        if every == "week":
            spec["weekday"] = _int(raw.get("weekday"), 0, 6, 0)
        return {**spec, "scope": scope}
    if kind == "push":
        # Not more often than this, however often the branch moves.
        return {"hours": _int(raw.get("hours"), 1, 24 * 14, 24), "scope": scope}
    if kind == "item":
        source = raw.get("source") if raw.get("source") in ITEM_SOURCES else "any"
        return {"source": source, "per_day": _int(raw.get("per_day"), 1, 50, 5)}
    raise ProjectInvalid("A trigger is a schedule, a moved branch or an arriving item")


def next_due(spec: dict[str, Any], after: datetime) -> datetime:
    """The next time a schedule is due after ``after``."""
    due = after.astimezone(UTC).replace(minute=0, second=0, microsecond=0, hour=int(spec["hour"]))
    if spec.get("every") == "week":
        due += timedelta(days=(int(spec.get("weekday", 0)) - due.weekday()) % 7)
        step = timedelta(days=7)
    else:
        step = timedelta(days=1)
    while due <= after:
        due += step
    return due


def describe(kind: str, spec: dict[str, Any]) -> str:
    if kind == "schedule":
        when = (
            f"Every {WEEKDAYS[int(spec.get('weekday', 0))]}"
            if spec.get("every") == "week"
            else "Every day"
        )
        return f"{when} at {int(spec['hour']):02d}:00 UTC"
    if kind == "push":
        hours = int(spec["hours"])
        pause = "a day" if hours == 24 else f"{hours} hours" if hours != 1 else "an hour"
        return f"When the default branch has moved, at most once in {pause}"
    words = {"any": "an item", "github": "an issue from GitHub", "flow": "an item a flow produced"}
    return (
        f"When {words[str(spec.get('source', 'any'))]} arrives in the section, "
        f"at most {int(spec['per_day'])} in a day"
    )


class Triggers:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        runs: Runs,
        budgets: Budgets,
    ) -> None:
        self.sessions = sessions
        self.runs = runs
        self.budgets = budgets

    # --- records --------------------------------------------------------------------------

    @staticmethod
    def _row(trigger: FlowTrigger, section: ProjectSection | None) -> dict[str, Any]:
        return {
            "id": str(trigger.id),
            "section": trigger.section_key,
            "section_name": section.name if section is not None else trigger.section_key,
            "kind": trigger.kind,
            "spec": dict(trigger.spec or {}),
            "says": describe(trigger.kind, trigger.spec or {}),
            "enabled": trigger.enabled,
            "next_due_at": _when(trigger.next_due_at),
            "last_fired_at": _when(trigger.last_fired_at),
            "last_result": trigger.last_result,
            "fired": trigger.fired,
        }

    @staticmethod
    def _project(session: Session, name: str) -> ProjectRecord:
        record = session.scalar(
            select(ProjectRecord).where(
                ProjectRecord.name == name, ProjectRecord.archived_at.is_(None)
            )
        )
        if record is None:
            raise ProjectNotFound(name)
        return record

    @staticmethod
    def _section(session: Session, project: ProjectRecord, key: str) -> ProjectSection:
        section = session.scalar(
            select(ProjectSection).where(
                ProjectSection.project_id == project.id, ProjectSection.key == key
            )
        )
        if section is None:
            raise ProjectNotFound(key)
        return section

    def of_project(self, project: str) -> dict[str, Any]:
        with self.sessions() as session:
            record = self._project(session, project)
            sections = {
                section.key: section
                for section in session.scalars(
                    select(ProjectSection).where(ProjectSection.project_id == record.id)
                )
            }
            rows = session.scalars(
                select(FlowTrigger)
                .where(FlowTrigger.project_id == record.id)
                .order_by(FlowTrigger.created_at)
            ).all()
            return {"items": [self._row(row, sections.get(row.section_key)) for row in rows]}

    @staticmethod
    def _fits(session: Session, section: ProjectSection, kind: str) -> None:
        """Whether the section's flow is one that kind of trigger can start."""
        found = document_in_force(session, section.flow_key) if section.flow_key else None
        if found is None:
            raise ProjectInvalid(f"The section {section.name} has no flow that runs here")
        by_itself = section_action(found[0]) is not None
        if kind in ("schedule", "push") and not by_itself:
            raise ProjectInvalid(
                f"The flow of the section {section.name} works on items: it cannot be "
                "started at a time or by a moved branch. Start its items as they arrive."
            )

    def create(
        self, project: str, section: str, *, kind: str, spec: dict[str, Any] | None
    ) -> dict[str, Any]:
        if kind not in KINDS:
            raise ProjectInvalid("A trigger is a schedule, a moved branch or an arriving item")
        cleaned = clean_spec(kind, spec)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            record = self._project(session, project)
            found = self._section(session, record, section)
            self._fits(session, found, kind)
            count = session.scalar(
                select(func.count())
                .select_from(FlowTrigger)
                .where(FlowTrigger.project_id == record.id, FlowTrigger.section_key == section)
            )
            if (count or 0) >= MAX_PER_SECTION:
                raise ProjectConflict(f"A section has at most {MAX_PER_SECTION} triggers")
            trigger = FlowTrigger(
                project_id=record.id,
                section_key=section,
                kind=kind,
                spec=cleaned,
                enabled=True,
                # An item trigger starts what arrives from now on, not what was there.
                state={"since": now.isoformat()} if kind == "item" else {},
                next_due_at=next_due(cleaned, now) if kind == "schedule" else None,
                fired=0,
            )
            session.add(trigger)
            session.flush()
            return {**self._row(trigger, found), "project": record.name}

    def update(
        self, trigger_id: UUID, *, enabled: bool | None = None, spec: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            trigger = session.get(FlowTrigger, trigger_id, with_for_update=True)
            if trigger is None:
                raise ProjectNotFound(str(trigger_id))
            record = session.get(ProjectRecord, trigger.project_id)
            if record is None:
                raise ProjectNotFound(str(trigger_id))
            if spec is not None:
                trigger.spec = clean_spec(trigger.kind, spec)
                if trigger.kind == "schedule":
                    trigger.next_due_at = next_due(trigger.spec, now)
            if enabled is not None and enabled != trigger.enabled:
                trigger.enabled = enabled
                if enabled and trigger.kind == "schedule":
                    # Switched on again: the times missed meanwhile are not made up for.
                    trigger.next_due_at = next_due(trigger.spec, now)
                if enabled and trigger.kind == "item":
                    trigger.state = {**(trigger.state or {}), "since": now.isoformat()}
            section = session.scalar(
                select(ProjectSection).where(
                    ProjectSection.project_id == record.id,
                    ProjectSection.key == trigger.section_key,
                )
            )
            session.flush()
            return {**self._row(trigger, section), "project": record.name}

    def remove(self, trigger_id: UUID) -> dict[str, Any]:
        with self.sessions.begin() as session:
            trigger = session.get(FlowTrigger, trigger_id, with_for_update=True)
            if trigger is None:
                raise ProjectNotFound(str(trigger_id))
            record = session.get(ProjectRecord, trigger.project_id)
            gone = {**self._row(trigger, None), "project": record.name if record else ""}
            session.delete(trigger)
            return gone

    # --- firing ---------------------------------------------------------------------------

    def _note(self, trigger_id: UUID, result: str, *, fired: bool, **state: Any) -> None:
        with self.sessions.begin() as session:
            trigger = session.get(FlowTrigger, trigger_id)
            if trigger is None:
                return
            trigger.last_result = " ".join(result.split())[:300]
            if fired:
                trigger.last_fired_at = datetime.now(UTC)
                trigger.fired = (trigger.fired or 0) + 1
            if state:
                trigger.state = {**(trigger.state or {}), **state}

    def _start_section(self, project: str, section: str, scope: str) -> tuple[bool, str]:
        """Start what the section runs by itself. Says whether it started, and what or
        why not."""
        refusal = self.budgets.may_start(project)
        if refusal is not None:
            return False, f"Not started: {refusal}"
        try:
            run = self.runs.start_for_section(
                project, section, scope=scope, toolchain=toolchain_for(project).as_dict()
            )
        except (ProjectConflict, ProjectInvalid, ProjectNotFound) as error:
            return False, f"Not started: {error}"
        return True, f"Started “{run['title']}”."

    def fire(self, trigger_id: UUID) -> dict[str, Any]:
        """Do now what the trigger would do when its time comes (asked for by hand)."""
        with self.sessions() as session:
            trigger = session.get(FlowTrigger, trigger_id)
            if trigger is None:
                raise ProjectNotFound(str(trigger_id))
            record = session.get(ProjectRecord, trigger.project_id)
            if record is None:
                raise ProjectNotFound(str(trigger_id))
            project, section, kind = record.name, trigger.section_key, trigger.kind
            scope = str((trigger.spec or {}).get("scope") or "")
        if kind == "item":
            started, result = self._start_items(trigger_id)
        else:
            started, result = self._start_section(project, section, scope)
        self._note(trigger_id, result, fired=started)
        if not started:
            raise ProjectConflict(result)
        with self.sessions() as session:
            trigger = session.get(FlowTrigger, trigger_id)
            if trigger is None:
                raise ProjectNotFound(str(trigger_id))
            return {**self._row(trigger, None), "project": project}

    def _start_items(self, trigger_id: UUID) -> tuple[bool, str]:
        """Start the items that arrived in the trigger's section and have no run yet."""
        now = datetime.now(UTC)
        with self.sessions() as session:
            trigger = session.get(FlowTrigger, trigger_id)
            if trigger is None:
                return False, "The trigger is gone."
            record = session.get(ProjectRecord, trigger.project_id)
            section = (
                session.scalar(
                    select(ProjectSection).where(
                        ProjectSection.project_id == trigger.project_id,
                        ProjectSection.key == trigger.section_key,
                    )
                )
                if record is not None
                else None
            )
            if record is None or section is None:
                return False, "Not started: the section is gone."
            spec = dict(trigger.spec or {})
            state = dict(trigger.state or {})
            since = _aware(datetime.fromisoformat(str(state.get("since") or now.isoformat())))
            day = now.date().isoformat()
            used = int(state.get("started", 0)) if state.get("day") == day else 0
            left = int(spec.get("per_day", 5)) - used
            query = (
                select(BacklogItem)
                .where(
                    BacklogItem.project_id == record.id,
                    BacklogItem.section_id == section.id,
                    BacklogItem.state.in_(("proposed", "accepted")),
                    BacklogItem.workspace_review.is_(None)
                    | (BacklogItem.workspace_review == "approved"),
                    BacklogItem.created_at > since,
                )
                .order_by(BacklogItem.created_at)
            )
            if spec.get("source") == "github":
                query = query.where(BacklogItem.issue_number.is_not(None))
            elif spec.get("source") == "flow":
                query = query.where(BacklogItem.source_run_id.is_not(None))
            waiting = []
            for item in session.scalars(query.limit(50)):
                ran = session.scalar(
                    select(func.count())
                    .select_from(SandboxRun)
                    .where(SandboxRun.item_id == item.id)
                )
                if not ran:
                    waiting.append((item.id, item.title))
            project = record.name
        if not waiting:
            return False, "Nothing has arrived."
        if left <= 0:
            return False, f"Not started: {len(waiting)} wait; the trigger's day is used up."
        started: list[str] = []
        why = ""
        for item_id, title in waiting[:left]:
            refusal = self.budgets.may_start(project)
            if refusal is not None:
                why = refusal
                break
            try:
                self.runs.start_item(item_id, toolchain=toolchain_for(project).as_dict())
            except (ProjectConflict, ProjectInvalid, ProjectNotFound) as error:
                why = str(error)
                continue
            started.append(title)
        if not started:
            return False, f"Not started: {why}"
        self._note(trigger_id, "", fired=False, day=day, started=used + len(started))
        more = len(waiting) - len(started)
        return True, (
            f"Started {len(started)}: {'; '.join(title[:60] for title in started[:3])}"
            + (f". {more} more wait." if more else ".")
        )

    def advance(self) -> int:
        """Fire the schedules that are due and start the items that arrived. Returns how
        many triggers started something. Moved branches are looked for by ``look``."""
        now = datetime.now(UTC)
        fired = 0
        with self.sessions() as session:
            due = session.execute(
                select(FlowTrigger, ProjectRecord)
                .join(ProjectRecord, FlowTrigger.project_id == ProjectRecord.id)
                .where(
                    FlowTrigger.enabled.is_(True),
                    ProjectRecord.archived_at.is_(None),
                    FlowTrigger.kind.in_(("schedule", "item")),
                )
            ).all()
            work = [
                (
                    trigger.id,
                    trigger.kind,
                    record.name,
                    trigger.section_key,
                    dict(trigger.spec or {}),
                )
                for trigger, record in due
                if trigger.kind == "item"
                or (trigger.next_due_at is not None and _aware(trigger.next_due_at) <= now)
            ]
        for trigger_id, kind, project, section, spec in work:
            if kind == "item":
                started, result = self._start_items(trigger_id)
                if result != "Nothing has arrived.":
                    self._note(trigger_id, result, fired=started)
            else:
                started, result = self._start_section(
                    project, section, str(spec.get("scope") or "")
                )
                self._note(trigger_id, result, fired=started)
                with self.sessions.begin() as session:
                    trigger = session.get(FlowTrigger, trigger_id)
                    if trigger is not None:
                        # Due again at its next time, whether or not it could start now.
                        trigger.next_due_at = next_due(spec, now)
            fired += started
        return fired

    async def look(self, github: GitHubApp) -> int:
        """See whether the default branch of each project with a push trigger has moved,
        and start what those triggers start. Returns how many started something."""
        now = datetime.now(UTC)
        with self.sessions() as session:
            rows = session.execute(
                select(FlowTrigger, ProjectRecord)
                .join(ProjectRecord, FlowTrigger.project_id == ProjectRecord.id)
                .where(
                    FlowTrigger.enabled.is_(True),
                    FlowTrigger.kind == "push",
                    ProjectRecord.archived_at.is_(None),
                )
            ).all()
            watched = [
                (
                    trigger.id,
                    record.name,
                    record.source_repository,
                    record.default_branch or "main",
                    trigger.section_key,
                    dict(trigger.spec or {}),
                    dict(trigger.state or {}),
                    trigger.last_fired_at,
                )
                for trigger, record in rows
                if record.source_repository
            ]
        heads: dict[tuple[str, str], str | None] = {}
        fired = 0
        for trigger_id, project, repository, branch, section, spec, state, last in watched:
            if (repository, branch) not in heads:
                heads[(repository, branch)] = await self._head(github, repository, branch)
            head = heads[(repository, branch)]
            if head is None:
                continue
            seen = state.get("head")
            if seen is None:
                # The first look only notes where the branch is: nothing has moved yet.
                self._note(trigger_id, "Watching the branch.", fired=False, head=head)
                continue
            if head == seen:
                continue
            pause = timedelta(hours=int(spec.get("hours", 24)))
            if last is not None and now - _aware(last) < pause:
                continue  # it moved; the trigger waits out its pause and then runs once
            started, result = self._start_section(project, section, str(spec.get("scope") or ""))
            # What was seen is noted either way: one move starts one run, when it can.
            self._note(trigger_id, result, fired=started, **({"head": head} if started else {}))
            fired += started
        return fired

    @staticmethod
    async def _head(github: GitHubApp, repository: str, branch: str) -> str | None:
        try:
            token = await github.repository_token(repository, {"contents": "read"})
        except GitHubAppError as error:
            LOG.warning("trigger_head_unreadable", repository=repository, error=str(error)[:200])
            return None
        try:
            return await github.branch_head(repository, branch, token=token)
        except GitHubAppError as error:
            LOG.warning("trigger_head_unreadable", repository=repository, error=str(error)[:200])
            return None
        finally:
            await github.revoke(token)
