"""Queues of work, and the budgets that say how much may run.

A queue is items of one project in the order they are to be worked on. The administrator
makes one from the backlog, or the supervisor proposes one with its reasons; either way it
is a draft until the administrator starts it. The launcher then moves it forward on every
pass: it follows the runs of the items under way and starts the next ones, as many at the
same time as the queue allows and the budgets leave room for.

A budget limits a scope: the whole platform, one project, or one subscription account. For
the platform and a project it counts runs of work (at the same time, and started within a
day); for an account it counts the steps given to it within a day. Budgets hold back what
the platform starts by itself: a queue's next item, and work the supervisor starts. What
the administrator starts by hand is their own decision, except that an account past its
budget is given to no step at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.backlog_items import OPEN_STATES
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
)
from engineering_control.db.models import (
    BacklogItem,
    Budget,
    ProjectRecord,
    ProjectSection,
    ProviderAccount,
    SandboxRun,
    SandboxStep,
    WorkQueue,
    WorkQueueEntry,
)
from engineering_control.sandbox.deliver import toolchain_for
from engineering_control.sandbox.runs import Runs

WORK: Final = "deliver_issue"
UNDER_WAY: Final = ("draft", "running", "paused")
MAX_ITEMS: Final = 200
# After this many items in a row failed, the queue stops and waits for the administrator.
FAILURES_IN_A_ROW: Final = 2
DAY: Final = timedelta(hours=24)


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _words(value: str | None, limit: int) -> str | None:
    text = " ".join((value or "").split())[:limit]
    return text or None


class Budgets:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    @staticmethod
    def _limit(value: Any, *, most: int, what: str) -> int | None:
        if value is None or value == "":
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ProjectInvalid(f"{what} is a whole number, or empty for no limit") from None
        if not 1 <= number <= most:
            raise ProjectInvalid(f"{what} is between 1 and {most}, or empty for no limit")
        return number

    def listing(self) -> dict[str, Any]:
        """Every scope with its limits and what it has used, for the console."""
        since = datetime.now(UTC) - DAY
        with self.sessions() as session:
            stored = {
                (budget.scope, budget.scope_id): budget
                for budget in session.scalars(select(Budget))
            }

            def row(scope: str, scope_id: UUID | None, **more: Any) -> dict[str, Any]:
                budget = stored.get((scope, scope_id))
                return {
                    "scope": scope,
                    "scope_id": str(scope_id) if scope_id else None,
                    "at_once": budget.at_once if budget else None,
                    "per_day": budget.per_day if budget else None,
                    **more,
                }

            projects = session.scalars(
                select(ProjectRecord)
                .where(ProjectRecord.archived_at.is_(None))
                .order_by(func.lower(ProjectRecord.display_name))
            ).all()
            accounts = session.scalars(
                select(ProviderAccount)
                .where(ProviderAccount.archived_at.is_(None))
                .order_by(func.lower(ProviderAccount.name))
            ).all()
            return {
                "platform": row(
                    "platform",
                    None,
                    running=self._running(session, None),
                    started_today=self._started(session, None, since),
                ),
                "projects": [
                    row(
                        "project",
                        project.id,
                        name=project.name,
                        display_name=project.display_name,
                        running=self._running(session, project.id),
                        started_today=self._started(session, project.id, since),
                    )
                    for project in projects
                ],
                "accounts": [
                    row(
                        "account",
                        account.id,
                        name=account.name,
                        provider=account.provider,
                        at_once=None,
                        started_today=self._steps(session, account.id, since),
                    )
                    for account in accounts
                ],
            }

    def save(
        self, scope: str, scope_id: UUID | None, *, at_once: Any, per_day: Any
    ) -> dict[str, Any]:
        if scope not in ("platform", "project", "account"):
            raise ProjectInvalid("A budget is for the platform, a project or an account")
        limit_at_once = self._limit(at_once, most=100, what="How many at the same time")
        limit_per_day = self._limit(per_day, most=10_000, what="How many in a day")
        with self.sessions.begin() as session:
            name = "the platform"
            if scope == "platform":
                scope_id = None
            elif scope == "project":
                project = session.get(ProjectRecord, scope_id) if scope_id else None
                if project is None or project.archived_at is not None:
                    raise ProjectNotFound(str(scope_id))
                name = project.display_name
            else:
                account = session.get(ProviderAccount, scope_id) if scope_id else None
                if account is None or account.archived_at is not None:
                    raise ProjectNotFound(str(scope_id))
                name = account.name
                if limit_at_once is not None:
                    raise ProjectInvalid(
                        "How many sandboxes an account runs at once is set on the account itself"
                    )
            query = select(Budget).where(Budget.scope == scope)
            query = query.where(
                Budget.scope_id.is_(None) if scope_id is None else Budget.scope_id == scope_id
            )
            budget = session.scalar(query)
            if budget is None:
                budget = Budget(scope=scope, scope_id=scope_id)
                session.add(budget)
            budget.at_once = limit_at_once
            budget.per_day = limit_per_day
            session.flush()
            return {
                "scope": scope,
                "scope_id": str(scope_id) if scope_id else None,
                "at_once": budget.at_once,
                "per_day": budget.per_day,
                "budget": name,
            }

    @staticmethod
    def _running(session: Session, project_id: UUID | None) -> int:
        query = (
            select(func.count())
            .select_from(SandboxRun)
            .where(SandboxRun.kind == WORK, SandboxRun.state.in_(("queued", "running")))
        )
        if project_id is not None:
            query = query.where(SandboxRun.project_id == project_id)
        return int(session.scalar(query) or 0)

    @staticmethod
    def _started(session: Session, project_id: UUID | None, since: datetime) -> int:
        query = (
            select(func.count())
            .select_from(SandboxRun)
            .where(SandboxRun.kind == WORK, SandboxRun.created_at > since)
        )
        if project_id is not None:
            query = query.where(SandboxRun.project_id == project_id)
        return int(session.scalar(query) or 0)

    @staticmethod
    def _steps(session: Session, account_id: UUID, since: datetime) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(SandboxStep)
                .where(SandboxStep.account_id == account_id, SandboxStep.started_at > since)
            )
            or 0
        )

    def refusal(self, session: Session, project_id: UUID) -> str | None:
        """Why no further run of work may start for the project right now, or None."""
        since = datetime.now(UTC) - DAY
        budgets = session.scalars(
            select(Budget).where(
                (Budget.scope == "platform")
                | ((Budget.scope == "project") & (Budget.scope_id == project_id))
            )
        ).all()
        for budget in sorted(budgets, key=lambda item: item.scope != "platform"):
            of = None if budget.scope == "platform" else project_id
            where = "the platform" if of is None else "this project"
            if budget.at_once is not None and self._running(session, of) >= budget.at_once:
                return f"The budget of {where} allows {budget.at_once} at the same time."
            if budget.per_day is not None and self._started(session, of, since) >= budget.per_day:
                return f"The budget of {where} allows {budget.per_day} in a day; that is used up."
        return None

    def may_start(self, project: str) -> str | None:
        with self.sessions() as session:
            project_id = session.scalar(
                select(ProjectRecord.id).where(ProjectRecord.name == project)
            )
            return self.refusal(session, project_id) if project_id else None


class Queues:
    def __init__(self, sessions: sessionmaker[Session], runs: Runs, budgets: Budgets) -> None:
        self.sessions = sessions
        self.runs = runs
        self.budgets = budgets

    # --- reading -------------------------------------------------------------------------

    @staticmethod
    def _counts(session: Session, queue_id: UUID) -> dict[str, int]:
        rows = session.execute(
            select(WorkQueueEntry.state, func.count())
            .where(WorkQueueEntry.queue_id == queue_id)
            .group_by(WorkQueueEntry.state)
        ).all()
        counts = {state: 0 for state in ("waiting", "running", "done", "failed", "skipped")}
        counts.update({str(state): int(count) for state, count in rows})
        counts["total"] = sum(counts.values())
        return counts

    def _row(self, session: Session, queue: WorkQueue, project: ProjectRecord) -> dict[str, Any]:
        return {
            "id": str(queue.id),
            "name": queue.name,
            "project": project.name,
            "project_name": project.display_name,
            "state": queue.state,
            "at_once": queue.at_once,
            "proposed_by": queue.proposed_by,
            "reason": queue.reason,
            "pause_reason": queue.pause_reason,
            "counts": self._counts(session, queue.id),
            "created_at": _when(queue.created_at),
            "started_at": _when(queue.started_at),
            "finished_at": _when(queue.finished_at),
            "queue": queue.name,
        }

    def listing(self, *, project: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Queues under way first, then the finished ones, newest first."""
        with self.sessions() as session:
            query = (
                select(WorkQueue, ProjectRecord)
                .join(ProjectRecord, WorkQueue.project_id == ProjectRecord.id)
                .where(ProjectRecord.archived_at.is_(None))
                .order_by(WorkQueue.state == "finished", WorkQueue.created_at.desc())
                .limit(max(1, min(limit, 200)))
            )
            if project is not None:
                query = query.where(ProjectRecord.name == project)
            rows = session.execute(query).all()
            return {
                "items": [self._row(session, queue, record) for queue, record in rows],
                "total": len(rows),
            }

    @staticmethod
    def _one(
        session: Session, queue_id: UUID, *, lock: bool = False
    ) -> tuple[WorkQueue, ProjectRecord]:
        query = (
            select(WorkQueue, ProjectRecord)
            .join(ProjectRecord, WorkQueue.project_id == ProjectRecord.id)
            .where(WorkQueue.id == queue_id, ProjectRecord.archived_at.is_(None))
        )
        if lock:
            query = query.with_for_update(of=WorkQueue)
        row = session.execute(query).first()
        if row is None:
            raise ProjectNotFound(str(queue_id))
        return row[0], row[1]

    def _detail(self, session: Session, queue: WorkQueue, project: ProjectRecord) -> dict[str, Any]:
        rows = session.execute(
            select(WorkQueueEntry, BacklogItem, ProjectSection, SandboxRun)
            .join(BacklogItem, WorkQueueEntry.item_id == BacklogItem.id)
            .outerjoin(ProjectSection, BacklogItem.section_id == ProjectSection.id)
            .outerjoin(SandboxRun, WorkQueueEntry.run_id == SandboxRun.id)
            .where(WorkQueueEntry.queue_id == queue.id)
            .order_by(WorkQueueEntry.position)
        ).all()
        return {
            **self._row(session, queue, project),
            "waiting_for": self.budgets.refusal(session, project.id)
            if queue.state == "running"
            else None,
            "entries": [
                {
                    "item_id": str(item.id),
                    "position": entry.position,
                    "state": entry.state,
                    "reason": entry.reason,
                    "detail": entry.detail,
                    "title": item.title,
                    "issue_number": item.issue_number,
                    "issue_url": item.issue_url,
                    "item_state": item.state,
                    "section": {"key": section.key, "name": section.name, "flow": section.flow_key}
                    if section
                    else None,
                    "run": {"id": str(run.id), "state": run.state, "outcome": run.outcome}
                    if run
                    else None,
                }
                for entry, item, section, run in rows
            ],
        }

    def get(self, queue_id: UUID) -> dict[str, Any]:
        with self.sessions() as session:
            queue, project = self._one(session, queue_id)
            return self._detail(session, queue, project)

    def for_items(self, item_ids: list[str]) -> dict[str, dict[str, Any]]:
        """For each of the items, the queue under way it is in."""
        if not item_ids:
            return {}
        with self.sessions() as session:
            rows = session.execute(
                select(WorkQueueEntry.item_id, WorkQueue.id, WorkQueue.name, WorkQueue.state)
                .join(WorkQueue, WorkQueueEntry.queue_id == WorkQueue.id)
                .where(
                    WorkQueueEntry.item_id.in_([UUID(item_id) for item_id in item_ids]),
                    WorkQueue.state.in_(UNDER_WAY),
                )
            ).all()
            return {
                str(item_id): {"id": str(queue_id), "name": name, "state": state}
                for item_id, queue_id, name, state in rows
            }

    # --- changing ------------------------------------------------------------------------

    @staticmethod
    def _name(value: str | None) -> str:
        name = " ".join((value or "").split())
        if not 1 <= len(name) <= 120:
            raise ProjectInvalid("A queue's name has between 1 and 120 characters")
        return name

    @staticmethod
    def _at_once(value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ProjectInvalid("How many items run at the same time is a number") from None
        if not 1 <= number <= 4:
            raise ProjectInvalid("Between 1 and 4 items of a queue run at the same time")
        return number

    def _add(
        self,
        session: Session,
        queue: WorkQueue,
        item_ids: list[UUID],
        reasons: dict[UUID, str] | None = None,
    ) -> int:
        """Append items that belong to the queue's project, are open, and are in no other
        queue that is under way. Items already in this queue are left where they are."""
        if len(set(item_ids)) != len(item_ids):
            raise ProjectInvalid("An item can be in a queue only once")
        present = set(
            session.scalars(
                select(WorkQueueEntry.item_id).where(WorkQueueEntry.queue_id == queue.id)
            )
        )
        wanted = [item_id for item_id in item_ids if item_id not in present]
        if len(present) + len(wanted) > MAX_ITEMS:
            raise ProjectInvalid(f"A queue holds at most {MAX_ITEMS} items")
        if not wanted:
            return 0
        items = {
            item.id: item
            for item in session.scalars(select(BacklogItem).where(BacklogItem.id.in_(wanted)))
        }
        elsewhere: dict[UUID, str] = {
            found: name
            for found, name in session.execute(
                select(WorkQueueEntry.item_id, WorkQueue.name)
                .join(WorkQueue, WorkQueueEntry.queue_id == WorkQueue.id)
                .where(
                    WorkQueueEntry.item_id.in_(wanted),
                    WorkQueue.state.in_(UNDER_WAY),
                    WorkQueue.id != queue.id,
                    WorkQueueEntry.state.in_(("waiting", "running")),
                )
            ).all()
        }
        last = session.scalar(
            select(func.max(WorkQueueEntry.position)).where(WorkQueueEntry.queue_id == queue.id)
        )
        position = last or 0
        for item_id in wanted:
            item = items.get(item_id)
            if item is None or item.project_id != queue.project_id:
                raise ProjectInvalid("A queue holds items of its own project only")
            if item.state not in OPEN_STATES:
                raise ProjectInvalid(f"“{item.title[:80]}” is not open any more")
            if item_id in elsewhere:
                raise ProjectConflict(
                    f"“{item.title[:80]}” is already in the queue {elsewhere[item_id]}"
                )
            position += 1
            session.add(
                WorkQueueEntry(
                    queue_id=queue.id,
                    item_id=item_id,
                    position=position,
                    state="waiting",
                    reason=_words((reasons or {}).get(item_id), 300),
                )
            )
        if queue.state == "finished":
            # A finished queue that is given more to do is under way again, paused.
            queue.state = "paused"
            queue.finished_at = None
            queue.pause_reason = "Items were added after it had finished. Resume it to run them."
        return len(wanted)

    def create(
        self,
        project: str,
        *,
        name: str,
        item_ids: list[UUID],
        at_once: Any = 1,
        proposed_by: str | None = None,
        reason: str | None = None,
        reasons: dict[UUID, str] | None = None,
    ) -> dict[str, Any]:
        if not item_ids:
            raise ProjectInvalid("A queue needs at least one item")
        with self.sessions.begin() as session:
            record = session.scalar(
                select(ProjectRecord).where(
                    ProjectRecord.name == project, ProjectRecord.archived_at.is_(None)
                )
            )
            if record is None:
                raise ProjectNotFound(project)
            queue = WorkQueue(
                project_id=record.id,
                name=self._name(name),
                state="draft",
                at_once=self._at_once(at_once),
                proposed_by=proposed_by,
                reason=_words(reason, 600),
            )
            session.add(queue)
            session.flush()
            self._add(session, queue, item_ids, reasons)
            session.flush()
            return self._detail(session, queue, record)

    def add_items(self, queue_id: UUID, item_ids: list[UUID]) -> dict[str, Any]:
        with self.sessions.begin() as session:
            queue, project = self._one(session, queue_id, lock=True)
            self._add(session, queue, item_ids)
            session.flush()
            return self._detail(session, queue, project)

    def remove_item(self, queue_id: UUID, item_id: UUID) -> dict[str, Any]:
        with self.sessions.begin() as session:
            queue, project = self._one(session, queue_id, lock=True)
            entry = session.scalar(
                select(WorkQueueEntry).where(
                    WorkQueueEntry.queue_id == queue.id, WorkQueueEntry.item_id == item_id
                )
            )
            if entry is None:
                raise ProjectNotFound(str(item_id))
            if entry.state != "waiting":
                raise ProjectConflict("Only an item that still waits can be taken out")
            session.delete(entry)
            session.flush()
            return self._detail(session, queue, project)

    def reorder(self, queue_id: UUID, item_ids: list[UUID]) -> dict[str, Any]:
        """Put the items that still wait in the order given. Every waiting item is named
        once; items that ran or are running keep their place ahead of them."""
        with self.sessions.begin() as session:
            queue, project = self._one(session, queue_id, lock=True)
            entries = session.scalars(
                select(WorkQueueEntry)
                .where(WorkQueueEntry.queue_id == queue.id)
                .order_by(WorkQueueEntry.position)
            ).all()
            waiting = {entry.item_id: entry for entry in entries if entry.state == "waiting"}
            if set(item_ids) != set(waiting) or len(item_ids) != len(waiting):
                raise ProjectConflict(
                    "The queue changed meanwhile: name every item that still waits, once"
                )
            ahead = [entry for entry in entries if entry.state != "waiting"]
            ordered = ahead + [waiting[item_id] for item_id in item_ids]
            # Two passes, so that no two entries share a position at any moment.
            for index, entry in enumerate(ordered):
                entry.position = -(index + 1)
            session.flush()
            for index, entry in enumerate(ordered):
                entry.position = index + 1
            session.flush()
            return self._detail(session, queue, project)

    def update(
        self, queue_id: UUID, *, name: str | None = None, at_once: Any = None
    ) -> dict[str, Any]:
        with self.sessions.begin() as session:
            queue, project = self._one(session, queue_id, lock=True)
            if name is not None:
                queue.name = self._name(name)
            if at_once is not None:
                queue.at_once = self._at_once(at_once)
            session.flush()
            return self._detail(session, queue, project)

    def start(self, queue_id: UUID) -> dict[str, Any]:
        """Start a draft, or resume a paused queue."""
        with self.sessions.begin() as session:
            queue, project = self._one(session, queue_id, lock=True)
            if queue.state not in ("draft", "paused"):
                raise ProjectConflict(
                    "The queue is already running"
                    if queue.state == "running"
                    else "The queue has finished"
                )
            if not self._counts(session, queue.id)["waiting"]:
                raise ProjectInvalid("No item of the queue waits to be worked on")
            queue.state = "running"
            queue.pause_reason = None
            queue.started_at = queue.started_at or datetime.now(UTC)
            session.flush()
            return self._detail(session, queue, project)

    def pause(self, queue_id: UUID, reason: str | None = None) -> dict[str, Any]:
        """Start nothing further. What is running runs to its end."""
        with self.sessions.begin() as session:
            queue, project = self._one(session, queue_id, lock=True)
            if queue.state != "running":
                raise ProjectConflict("Only a running queue can be paused")
            queue.state = "paused"
            queue.pause_reason = _words(reason, 300)
            session.flush()
            return self._detail(session, queue, project)

    def discard(self, queue_id: UUID) -> dict[str, Any]:
        """Remove a queue that is not running anything. Its items stay in the backlog."""
        with self.sessions.begin() as session:
            queue, project = self._one(session, queue_id, lock=True)
            if self._counts(session, queue.id)["running"]:
                raise ProjectConflict("An item of the queue is running: pause it and wait")
            if queue.state == "running":
                raise ProjectConflict("Pause the queue first")
            row = self._row(session, queue, project)
            session.delete(queue)
            return row

    # --- the launcher --------------------------------------------------------------------

    def advance(self) -> int:
        """One pass over the running and paused queues. Returns how many runs it started."""
        with self.sessions() as session:
            queue_ids = list(
                session.scalars(
                    select(WorkQueue.id)
                    .where(WorkQueue.state.in_(("running", "paused")))
                    .order_by(WorkQueue.started_at)
                )
            )
        return sum(self._advance(queue_id) for queue_id in queue_ids)

    def _advance(self, queue_id: UUID) -> int:
        with self.sessions.begin() as session:
            row = session.execute(
                select(WorkQueue, ProjectRecord)
                .join(ProjectRecord, WorkQueue.project_id == ProjectRecord.id)
                .where(WorkQueue.id == queue_id)
                .with_for_update(of=WorkQueue)
            ).first()
            if row is None:
                return 0
            queue, project = row
            entries = session.scalars(
                select(WorkQueueEntry)
                .where(WorkQueueEntry.queue_id == queue.id)
                .order_by(WorkQueueEntry.position)
            ).all()
            # What became of the items under way.
            concluded = False
            for entry in entries:
                if entry.state != "running":
                    continue
                run = session.get(SandboxRun, entry.run_id) if entry.run_id else None
                if run is None:
                    entry.state, entry.detail = "failed", "Its run is no longer there."
                elif run.state == "waiting":
                    entry.state, entry.detail = "done", "A change waits for your decision."
                elif run.state == "succeeded":
                    entry.state, entry.detail = "done", None
                elif run.state in ("failed", "cancelled"):
                    entry.state = "failed"
                    entry.detail = _words(run.detail or run.outcome or run.state, 300)
                else:
                    continue
                concluded = True
            if queue.state != "running":
                return 0
            ended = [entry for entry in entries if entry.state in ("done", "failed")]
            if (
                concluded
                and len(ended) >= FAILURES_IN_A_ROW
                and all(entry.state == "failed" for entry in ended[-FAILURES_IN_A_ROW:])
            ):
                queue.state = "paused"
                queue.pause_reason = (
                    f"{FAILURES_IN_A_ROW} items in a row failed. Look at their runs, then "
                    "resume the queue."
                )
                return 0
            running = sum(1 for entry in entries if entry.state == "running")
            waiting = [entry for entry in entries if entry.state == "waiting"]
            if not waiting and not running:
                queue.state = "finished"
                queue.finished_at = datetime.now(UTC)
                queue.pause_reason = None
                return 0
            room = queue.at_once - running
            candidates = [(entry.id, entry.item_id) for entry in waiting[: max(room, 0)]]
            project_id, project_name = project.id, project.name
        started = 0
        for entry_id, item_id in candidates:
            with self.sessions() as session:
                refusal = self.budgets.refusal(session, project_id)
            if refusal is not None:
                break
            run_id: UUID | None = None
            detail: str | None = None
            try:
                begun = self.runs.start_delivery(
                    item_id, toolchain=toolchain_for(project_name).as_dict()
                )
            except (ProjectInvalid, ProjectConflict, ProjectNotFound) as error:
                state, detail = "skipped", str(error) or "The item is gone."
            else:
                state, run_id = "running", UUID(begun["id"])
                started += 1
            with self.sessions.begin() as session:
                waiting_entry = session.get(WorkQueueEntry, entry_id)
                if waiting_entry is not None:
                    waiting_entry.state = state
                    waiting_entry.run_id = run_id
                    waiting_entry.detail = _words(detail, 300)
        return started

    def inbox(self) -> list[dict[str, Any]]:
        """Queues that wait for the administrator: proposed by the supervisor and not
        started, or paused by the platform."""
        with self.sessions() as session:
            rows = session.execute(
                select(WorkQueue, ProjectRecord)
                .join(ProjectRecord, WorkQueue.project_id == ProjectRecord.id)
                .where(
                    ProjectRecord.archived_at.is_(None),
                    ((WorkQueue.state == "draft") & (WorkQueue.proposed_by == "supervisor"))
                    | ((WorkQueue.state == "paused") & WorkQueue.pause_reason.is_not(None)),
                )
                .order_by(WorkQueue.created_at)
            ).all()
            return [
                {
                    "kind": "queue_proposed" if queue.state == "draft" else "queue_paused",
                    "project": project.name,
                    "project_name": project.display_name,
                    "section": None,
                    "count": self._counts(session, queue.id)["waiting"],
                    "detail": queue.reason if queue.state == "draft" else queue.pause_reason,
                    "queue_id": str(queue.id),
                    "queue": queue.name,
                }
                for queue, project in rows
            ]
