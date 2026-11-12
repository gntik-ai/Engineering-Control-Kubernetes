"""Runs and their steps, as records.

The console creates a run and reads it; the launcher moves it forward. A run is ``queued``
until its first sandbox starts, ``running`` while a step runs or waits for its turn, and
ends ``succeeded``, ``failed`` or ``cancelled``. An account is leased to a step for as long
as the step is running: that, counted against the account's limit, is all a lease is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
)
from engineering_control.db.models import ProviderAccount, SandboxRun, SandboxStep

ACTIVE: Final = ("queued", "running", "waiting")
FINISHED: Final = ("succeeded", "failed", "cancelled")
MAX_LOG_CHARACTERS: Final = 400_000
# Clients that renew their sign-in in place need a volume of the account's own.
KEEPS_HOME: Final = frozenset({"chatgpt"})


@dataclass(frozen=True)
class StepWork:
    """What the launcher needs to know about the step it is to move forward."""

    run_id: UUID
    run_kind: str
    run_data: dict[str, Any]
    cancel_requested: bool
    step_id: UUID
    name: str
    position: int
    state: str
    provider: str | None
    account_id: UUID | None
    pod_name: str | None
    attempt: int
    started_at: datetime | None


@dataclass(frozen=True)
class Lease:
    account_id: UUID
    provider: str
    generation: int

    @property
    def keeps_home(self) -> bool:
        return self.provider in KEEPS_HOME


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _step_row(step: SandboxStep, *, with_log: bool) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": str(step.id),
        "position": step.position,
        "name": step.name,
        "state": step.state,
        "provider": step.provider,
        "account_id": str(step.account_id) if step.account_id else None,
        "attempt": step.attempt,
        "exit_code": step.exit_code,
        "outcome": step.outcome,
        "detail": step.detail,
        "started_at": _when(step.started_at),
        "finished_at": _when(step.finished_at),
    }
    if with_log:
        row["log"] = step.log or ""
        row["output"] = step.output
    return row


def _run_row(run: SandboxRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "kind": run.kind,
        "state": run.state,
        "title": run.title,
        "outcome": run.outcome,
        "detail": run.detail,
        "cancel_requested": run.cancel_requested,
        "account_id": str(run.account_id) if run.account_id else None,
        "data": run.data or {},
        "created_at": _when(run.created_at),
        "started_at": _when(run.started_at),
        "finished_at": _when(run.finished_at),
        "run": run.title,
    }


class Runs:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    # --- the console ---------------------------------------------------------------------

    def start_account_check(self, account_id: UUID) -> dict[str, Any]:
        """Try an account: one step that asks its client a trivial question."""
        with self.sessions.begin() as session:
            account = session.scalar(
                select(ProviderAccount)
                .where(ProviderAccount.id == account_id, ProviderAccount.archived_at.is_(None))
                .with_for_update()
            )
            if account is None:
                raise ProjectNotFound(str(account_id))
            if account.credential_generation < 1 or account.state == "signed_out":
                raise ProjectInvalid("Sign the account in first: there is nothing to try yet")
            busy = session.scalar(
                select(func.count())
                .select_from(SandboxRun)
                .where(
                    SandboxRun.kind == "account_check",
                    SandboxRun.account_id == account.id,
                    SandboxRun.state.in_(ACTIVE),
                )
            )
            if busy:
                raise ProjectConflict(f"{account.name} is already being tried")
            run = SandboxRun(
                kind="account_check",
                state="queued",
                title=f"Try {account.name}",
                account_id=account.id,
                cancel_requested=False,
                data={"provider": account.provider, "account": account.name},
            )
            session.add(run)
            session.flush()
            session.add(
                SandboxStep(
                    run_id=run.id,
                    position=1,
                    name="check",
                    state="pending",
                    provider=account.provider,
                    account_id=account.id,
                    attempt=0,
                )
            )
            session.flush()
            return _run_row(run)

    def accounts_being_tried(self) -> set[str]:
        with self.sessions() as session:
            return {
                str(account_id)
                for account_id in session.scalars(
                    select(SandboxRun.account_id).where(
                        SandboxRun.kind == "account_check",
                        SandboxRun.state.in_(ACTIVE),
                        SandboxRun.account_id.is_not(None),
                    )
                )
            }

    def account_in_use(self, account_id: UUID) -> bool:
        """Whether a sandbox is working with the account, or a trial of it is under way."""
        with self.sessions() as session:
            running = session.scalar(
                select(func.count())
                .select_from(SandboxStep)
                .where(SandboxStep.account_id == account_id, SandboxStep.state == "running")
            )
            trying = session.scalar(
                select(func.count())
                .select_from(SandboxRun)
                .where(SandboxRun.account_id == account_id, SandboxRun.state.in_(ACTIVE))
            )
            return bool(running or trying)

    def listing(self, *, limit: int = 50) -> dict[str, Any]:
        with self.sessions() as session:
            runs = session.scalars(
                select(SandboxRun).order_by(SandboxRun.created_at.desc()).limit(limit)
            ).all()
            return {"items": [_run_row(run) for run in runs], "total": len(runs)}

    def get(self, run_id: UUID) -> dict[str, Any]:
        with self.sessions() as session:
            run = session.get(SandboxRun, run_id)
            if run is None:
                raise ProjectNotFound(str(run_id))
            steps = session.scalars(
                select(SandboxStep)
                .where(SandboxStep.run_id == run.id)
                .order_by(SandboxStep.position)
            ).all()
            return {**_run_row(run), "steps": [_step_row(step, with_log=True) for step in steps]}

    def request_cancel(self, run_id: UUID) -> dict[str, Any]:
        with self.sessions.begin() as session:
            run = session.scalar(
                select(SandboxRun).where(SandboxRun.id == run_id).with_for_update()
            )
            if run is None:
                raise ProjectNotFound(str(run_id))
            if run.state in FINISHED:
                raise ProjectConflict("The run has already ended")
            run.cancel_requested = True
            session.flush()
            return _run_row(run)

    # --- the launcher --------------------------------------------------------------------

    def work(self) -> list[StepWork]:
        """For every run that is not finished, its first step that is not finished either,
        oldest run first. A run with no such step is returned with state ``done``."""
        found: list[StepWork] = []
        with self.sessions() as session:
            runs = session.scalars(
                select(SandboxRun)
                .where(SandboxRun.state.in_(("queued", "running")))
                .order_by(SandboxRun.created_at)
            ).all()
            for run in runs:
                step = session.scalar(
                    select(SandboxStep)
                    .where(
                        SandboxStep.run_id == run.id,
                        SandboxStep.state.in_(("pending", "running")),
                    )
                    .order_by(SandboxStep.position)
                    .limit(1)
                )
                if step is None:
                    last = session.scalar(
                        select(SandboxStep)
                        .where(SandboxStep.run_id == run.id)
                        .order_by(SandboxStep.position.desc())
                        .limit(1)
                    )
                    if last is None:
                        continue
                    step, state = last, "done"
                else:
                    state = step.state
                found.append(
                    StepWork(
                        run_id=run.id,
                        run_kind=run.kind,
                        run_data=dict(run.data or {}),
                        cancel_requested=run.cancel_requested,
                        step_id=step.id,
                        name=step.name,
                        position=step.position,
                        state=state,
                        provider=step.provider,
                        account_id=step.account_id,
                        pod_name=step.pod_name,
                        attempt=step.attempt,
                        started_at=step.started_at,
                    )
                )
        return found

    def lease(self, step_id: UUID) -> Lease | None:
        """An account for the step, or None when none is free right now.

        A step that names its account (trying an account) gets that one, whatever state it
        is in. Otherwise the least recently used account of the step's provider that is
        signed in, not paused, not limited and below its limit of sandboxes.
        """
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            step = session.get(SandboxStep, step_id)
            if step is None or step.provider is None:
                return None
            using = (
                select(SandboxStep.account_id, func.count().label("running"))
                .where(SandboxStep.state == "running", SandboxStep.account_id.is_not(None))
                .group_by(SandboxStep.account_id)
                .subquery()
            )
            query = (
                select(ProviderAccount, func.coalesce(using.c.running, 0))
                .outerjoin(using, using.c.account_id == ProviderAccount.id)
                .where(
                    ProviderAccount.archived_at.is_(None),
                    ProviderAccount.provider == step.provider,
                    ProviderAccount.credential_generation > 0,
                )
                .order_by(ProviderAccount.last_used_at.is_not(None), ProviderAccount.last_used_at)
                .with_for_update(of=ProviderAccount)
            )
            if step.account_id is not None:
                query = query.where(ProviderAccount.id == step.account_id)
            for account, running in session.execute(query):
                if step.account_id is None:
                    limited = (
                        account.state == "limited"
                        and account.limited_until is not None
                        and _aware(account.limited_until) > now
                    )
                    if account.paused or limited:
                        continue
                    if account.state not in ("unchecked", "ready", "limited"):
                        continue
                if running >= account.max_concurrent:
                    continue
                step.account_id = account.id
                account.last_used_at = now
                session.flush()
                return Lease(account.id, account.provider, account.credential_generation)
        return None

    def step_started(self, step_id: UUID, pod_name: str) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            step = session.get(SandboxStep, step_id)
            if step is None:
                return
            step.state = "running"
            step.pod_name = pod_name
            step.attempt += 1
            step.started_at = now
            step.detail = None
            run = session.get(SandboxRun, step.run_id)
            if run is not None and run.state == "queued":
                run.state = "running"
                run.started_at = now

    def step_waiting(self, step_id: UUID, detail: str) -> None:
        with self.sessions.begin() as session:
            step = session.get(SandboxStep, step_id)
            if step is not None and step.detail != detail:
                step.detail = detail[:600]

    def step_log(self, step_id: UUID, log: str) -> None:
        with self.sessions.begin() as session:
            step = session.get(SandboxStep, step_id)
            if step is not None:
                step.log = log[-MAX_LOG_CHARACTERS:]

    def step_finished(
        self,
        step_id: UUID,
        *,
        state: str,
        outcome: str,
        detail: str | None,
        exit_code: int | None = None,
        output: dict[str, Any] | None = None,
        log: str | None = None,
        keep_account: bool = True,
    ) -> None:
        with self.sessions.begin() as session:
            step = session.get(SandboxStep, step_id)
            if step is None:
                return
            step.state = state
            step.outcome = outcome[:40]
            step.detail = detail[:600] if detail else None
            step.exit_code = exit_code
            step.output = output
            if log is not None:
                step.log = log[-MAX_LOG_CHARACTERS:]
            step.finished_at = datetime.now(UTC)
            if not keep_account:
                step.account_id = None

    def run_finished(self, run_id: UUID, *, state: str, outcome: str, detail: str | None) -> None:
        with self.sessions.begin() as session:
            run = session.get(SandboxRun, run_id)
            if run is None or run.state in FINISHED:
                return
            run.state = state
            run.outcome = outcome[:40]
            run.detail = detail[:600] if detail else None
            run.finished_at = datetime.now(UTC)
            if run.started_at is None:
                run.started_at = run.finished_at
            # Steps that never ran are not left looking as if they still might.
            for step in session.scalars(
                select(SandboxStep).where(
                    SandboxStep.run_id == run.id, SandboxStep.state.in_(("pending", "running"))
                )
            ):
                step.state = "cancelled" if state == "cancelled" else "skipped"
                step.finished_at = run.finished_at

    def account_learned(
        self,
        account_id: UUID,
        *,
        state: str,
        reason: str | None,
        limited_until: datetime | None,
    ) -> None:
        """Record what a step taught the platform about the account it worked with."""
        with self.sessions.begin() as session:
            account = session.get(ProviderAccount, account_id)
            if account is None or account.archived_at is not None:
                return
            account.state = state
            account.state_reason = reason[:300] if reason else None
            account.limited_until = limited_until
            account.checked_at = datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
