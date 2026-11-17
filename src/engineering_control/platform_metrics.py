"""The state of the work as numbers, read from the records themselves.

Nothing here is kept between two readings: a run's state is the run's record, an
account's state the account's, and so on. The launcher serves these beside its own
numbers (``health.py``); the alert rules in ``deploy/monitoring`` are written on them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.backlog_items import OPEN_STATES
from engineering_control.db.models import (
    BacklogItem,
    BacklogSync,
    ModelBatch,
    ProjectRecord,
    ProviderAccount,
    SandboxRun,
    SandboxStep,
    SupervisorIncident,
    WorkQueue,
)
from engineering_control.health import sample
from engineering_control.sandbox.runs import ACTIVE, CONVERSATION

ENDED: Final = ("succeeded", "failed", "cancelled")
ACCOUNT_STATES: Final = ("signed_out", "unchecked", "ready", "limited", "sign_in_required")
INCIDENT_STATES: Final = ("open", "looking", "waiting")
QUEUE_STATES: Final = ("running", "paused")

# Every metric of the platform's state, with what it says. The alert rules may use these
# names and no others (tests/test_health.py holds the two together).
DESCRIPTIONS: Final[dict[str, str]] = {
    "engineering_database_up": "One while the platform's database answers.",
    "engineering_runs": "Runs of the projects' work that have not ended, by state.",
    "engineering_runs_ended_last_hour": "Runs that ended in the last hour, by how.",
    "engineering_run_oldest_queued_seconds": "How long the oldest queued run has waited.",
    "engineering_steps_running": "Steps running in sandboxes right now.",
    "engineering_accounts": "Subscription accounts in use, by state.",
    "engineering_incidents": "Troubles with the work that are not closed, by state.",
    "engineering_queues": "Queues that are running or paused.",
    "engineering_backlog_open_items": "Items of the backlogs still to do.",
    "engineering_backlog_sync_failing": "Projects whose last sync with GitHub failed.",
    "engineering_batches_out": "Batches of questions handed to a provider and not ended.",
    "engineering_batch_oldest_seconds": "How long the oldest batch that is out has been out.",
}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _described(name: str, lines: list[str]) -> list[str]:
    return [f"# HELP {name} {DESCRIPTIONS[name]}", f"# TYPE {name} gauge", *lines]


def _by_state(session: Session, statement: Any) -> dict[str, int]:
    return {str(state): int(count) for state, count in session.execute(statement).all()}


def unavailable() -> list[str]:
    """What is said when the database does not answer."""
    return _described("engineering_database_up", [sample("engineering_database_up", 0)])


def platform_metrics(sessions: sessionmaker[Session], *, now: datetime | None = None) -> list[str]:
    moment = now or datetime.now(UTC)
    with sessions() as session:
        work = SandboxRun.kind != CONVERSATION
        active = _by_state(
            session,
            select(SandboxRun.state, func.count())
            .where(work, SandboxRun.state.in_(ACTIVE))
            .group_by(SandboxRun.state),
        )
        ended = _by_state(
            session,
            select(SandboxRun.state, func.count())
            .where(
                work,
                SandboxRun.state.in_(ENDED),
                SandboxRun.finished_at >= moment - timedelta(hours=1),
            )
            .group_by(SandboxRun.state),
        )
        oldest = session.scalar(
            select(func.min(SandboxRun.created_at)).where(work, SandboxRun.state == "queued")
        )
        steps = session.scalar(
            select(func.count()).select_from(SandboxStep).where(SandboxStep.state == "running")
        )
        accounts = _by_state(
            session,
            select(ProviderAccount.state, func.count())
            .where(ProviderAccount.archived_at.is_(None), ProviderAccount.paused.is_(False))
            .group_by(ProviderAccount.state),
        )
        incidents = _by_state(
            session,
            select(SupervisorIncident.state, func.count())
            .where(SupervisorIncident.state.in_(INCIDENT_STATES))
            .group_by(SupervisorIncident.state),
        )
        queues = _by_state(
            session,
            select(WorkQueue.state, func.count())
            .where(WorkQueue.state.in_(QUEUE_STATES))
            .group_by(WorkQueue.state),
        )
        items = session.scalar(
            select(func.count())
            .select_from(BacklogItem)
            .join(ProjectRecord, BacklogItem.project_id == ProjectRecord.id)
            .where(BacklogItem.state.in_(OPEN_STATES), ProjectRecord.archived_at.is_(None))
        )
        failing = session.scalar(
            select(func.count())
            .select_from(BacklogSync)
            .join(ProjectRecord, BacklogSync.project_id == ProjectRecord.id)
            .where(BacklogSync.error.is_not(None), ProjectRecord.archived_at.is_(None))
        )
        out, first = session.execute(
            select(func.count(), func.min(ModelBatch.created_at)).where(
                ModelBatch.state.in_(("submitting", "waiting", "stopping"))
            )
        ).one()
    waited = max(0.0, (moment - _utc(oldest)).total_seconds()) if oldest else 0.0
    batch_age = max(0.0, (moment - _utc(first)).total_seconds()) if first else 0.0
    lines = _described("engineering_database_up", [sample("engineering_database_up", 1)])
    lines += _described(
        "engineering_runs",
        [sample("engineering_runs", int(active.get(state, 0)), state=state) for state in ACTIVE],
    )
    lines += _described(
        "engineering_runs_ended_last_hour",
        [
            sample("engineering_runs_ended_last_hour", int(ended.get(state, 0)), state=state)
            for state in ENDED
        ],
    )
    lines += _described(
        "engineering_run_oldest_queued_seconds",
        [sample("engineering_run_oldest_queued_seconds", waited)],
    )
    lines += _described(
        "engineering_steps_running", [sample("engineering_steps_running", int(steps or 0))]
    )
    lines += _described(
        "engineering_accounts",
        [
            sample("engineering_accounts", int(accounts.get(state, 0)), state=state)
            for state in ACCOUNT_STATES
        ],
    )
    lines += _described(
        "engineering_incidents",
        [
            sample("engineering_incidents", int(incidents.get(state, 0)), state=state)
            for state in INCIDENT_STATES
        ],
    )
    lines += _described(
        "engineering_queues",
        [
            sample("engineering_queues", int(queues.get(state, 0)), state=state)
            for state in QUEUE_STATES
        ],
    )
    lines += _described(
        "engineering_backlog_open_items",
        [sample("engineering_backlog_open_items", int(items or 0))],
    )
    lines += _described(
        "engineering_backlog_sync_failing",
        [sample("engineering_backlog_sync_failing", int(failing or 0))],
    )
    lines += _described(
        "engineering_batches_out", [sample("engineering_batches_out", int(out or 0))]
    )
    lines += _described(
        "engineering_batch_oldest_seconds",
        [sample("engineering_batch_oldest_seconds", batch_age)],
    )
    return lines
