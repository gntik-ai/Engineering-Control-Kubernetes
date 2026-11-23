"""Runs that ask a model, one record at a time, which parent a planning record belongs to.

Two things are chosen this way, each by its own module: the User Story of an open issue
that has none (``orphans``), and the primary Epic of a User Story that is under several
(``primary_epics``). What they share is here: a run the administrator starts with a model
and a token limit, one question per record, the answer checked and stored, the project's
review tokens held while a question is asked and given back when it is over, and the
controls to stop, resume and end a run.

Nothing is changed by an answer. Each module says what the administrator's approval does.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import review_budget, review_flows
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.workspace_costs import add_project_usage
from engineering_control.db.models import (
    ChoiceReview,
    ChoiceRun,
    ProjectRecord,
    ProjectReviewBudget,
)
from engineering_control.models.service import ModelError, Models, Question, Reply

log = logging.getLogger(__name__)

LIVE: Final = ("queued", "running", "paused", "held")
ASKING: Final = ("queued", "running")
# After how long a call that never reported back is taken to be lost, how many calls in
# a row may fail before the run waits for the administrator, and how long a run waits
# when every provider was busy.
LOST: Final = timedelta(minutes=8)
TRIES: Final = 3
BUSY_SECONDS: Final = 60.0
# No inference ran, or the provider turned the question down before answering: nothing
# was used. Any other failure leaves unknown what the call used.
UNBILLED: Final = frozenset({"no_key", "no_account", "configuration", "refused", "invalid", "busy"})
NO_ACCOUNT: Final = frozenset({"no_key", "no_account", "configuration"})


class Unusable(ValueError):
    """The model answered, but not with something that can be shown as an answer."""


class Scope(Protocol):
    """The records of one project that wait for a parent, as one transaction sees them."""

    def children(self) -> list[Any]:
        """The records that wait, in the order they are asked about. Each has an ``id``."""

    def record(self, key: UUID) -> Any | None:
        """That record, when it still waits."""

    def refusal(self) -> str | None:
        """Why no run can be started at all, when that is so."""

    def shortlist(self, child: Any) -> list[dict[str, Any]]:
        """The parents it is shown with, best first. Each entry has ``id`` and ``title``."""

    def revision(self, child: Any, listed: list[dict[str, Any]]) -> str:
        """What a question about it is about, as a digest."""

    def question(self, run: ChoiceRun, child: Any, listed: list[dict[str, Any]]) -> Question:
        """The question that is asked about it."""

    def read(
        self, data: dict[str, Any], child: Any, listed: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """The answer as it is stored: ``answer``, ``parent_id``, ``confidence``,
        ``reason``, ``evidence``, ``checked`` and ``missing``. Raises ``Unusable``."""


class Kind(Protocol):
    """One thing that is chosen: which records wait, and what is asked about each."""

    name: str
    # What the tokens are counted under.
    workflow: str
    # The answers that approval can act on.
    actionable: tuple[str, ...]
    # What the administrator is told when every record that waits has an answer, and
    # when no record waits.
    nothing_to_ask: str
    nothing_waits: str

    def scope(self, db: Session, project: ProjectRecord) -> Scope: ...


KINDS: dict[str, Kind] = {}


def register(kind: Kind) -> None:
    KINDS[kind.name] = kind


def plain(text: str) -> str:
    return " ".join(text.split()).casefold()


def within(quote: str, text: str) -> bool:
    """Whether the quoted words are in the text, as written but for case and spacing."""
    said = plain(quote).strip("\"'“”‘’ ")
    return len(said) >= 12 and said in plain(text)


def passage(side: str, quote: Any, text: str) -> dict[str, Any] | None:
    """A quoted passage with whether its words are in the text it is said to be from."""
    said = " ".join(str(quote or "").split())[:600]
    return {"side": side, "text": said, "found": within(said, text)} if said else None


def position(named: Any, letter: str, listed: list[dict[str, Any]]) -> UUID | None:
    """The parent the answer names by its short id, such as S3 or E2."""
    said = str(named or "").strip().upper()
    place = int(said[1:]) if said[:1] == letter and said[1:].isdigit() else 0
    return UUID(listed[place - 1]["id"]) if 1 <= place <= len(listed) else None


def tokens_of(reply: Reply) -> int | None:
    tokens = reply.usage.get("tokens")
    if tokens is None and all(
        type(reply.usage.get(k)) is int and reply.usage[k] >= 0 for k in ("input", "output")
    ):
        tokens = reply.usage["input"] + reply.usage["output"]
    return tokens if type(tokens) is int and tokens >= 0 else None


def sizes(asked: Question) -> tuple[int, int]:
    """What a question holds of the project's review tokens while it is asked, counted by
    the size of its text, and what it is likely to use, at four characters a token and a
    short answer."""
    return (
        review_budget.reservation_size(asked),
        (len(asked.system) + len(asked.prompt)) // 4 + 350,
    )


def _give_back(db: Session, project: UUID, held: int) -> None:
    if held:
        db.execute(
            update(ProjectReviewBudget)
            .where(ProjectReviewBudget.project_id == project)
            .values(reserved=ProjectReviewBudget.reserved - held)
        )


def _finish(db: Session, run: ChoiceRun, state: str, detail: str = "") -> None:
    """End the run. What it had not asked yet is no longer waiting to be asked."""
    run.state, run.detail, run.ended_at = state, detail, datetime.now(UTC)
    db.execute(
        delete(ChoiceReview).where(ChoiceReview.run_id == run.id, ChoiceReview.state == "queued")
    )


def _locked(db: Session, run_id: UUID) -> ChoiceRun | None:
    # SQLite ignores SELECT FOR UPDATE. Acquire the same write lock on both databases.
    return db.scalar(
        update(ChoiceRun)
        .where(ChoiceRun.id == run_id)
        .values(updated_at=ChoiceRun.updated_at)
        .returning(ChoiceRun)
    )


def shown_run(run: ChoiceRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "state": run.state,
        "provider": run.provider,
        "model": run.model,
        "token_cap": run.token_cap,
        "spent": run.spent,
        "total": run.total,
        "done": run.done,
        "failed": run.failed,
        "uncounted": run.uncounted,
        "detail": run.detail,
        "asking": run.claimed_at is not None,
        "started_at": run.created_at.isoformat(),
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
    }


def latest_run(db: Session, project: UUID, kind: str) -> dict[str, Any] | None:
    run = db.scalar(
        select(ChoiceRun)
        .where(ChoiceRun.project_id == project, ChoiceRun.kind == kind)
        .order_by(ChoiceRun.created_at.desc(), ChoiceRun.id)
        .limit(1)
    )
    return shown_run(run) if run is not None else None


def reviews(db: Session, project: UUID, kind: str) -> dict[UUID, ChoiceReview]:
    return {
        review.child_id: review
        for review in db.scalars(
            select(ChoiceReview).where(
                ChoiceReview.project_id == project, ChoiceReview.kind == kind
            )
        )
    }


def review_of(db: Session, project: UUID, kind: str, child: UUID) -> ChoiceReview | None:
    return db.scalar(
        select(ChoiceReview).where(
            ChoiceReview.project_id == project,
            ChoiceReview.kind == kind,
            ChoiceReview.child_id == child,
        )
    )


def shown_answer(review: ChoiceReview, current: str, titles: dict[str, str]) -> dict[str, Any]:
    parent = str(review.parent_id) if review.parent_id else None
    return {
        "state": review.state,
        "answer": review.answer,
        "parent_id": parent,
        "parent": titles.get(parent) if parent else None,
        "confidence": review.confidence,
        "reason": review.reason,
        "evidence": review.evidence or [],
        "checked": review.checked,
        "missing": review.missing,
        "provider": review.provider,
        "model": review.model,
        "tokens": review.tokens,
        "detail": review.detail,
        "decision": review.decision,
        # The record or its shortlist changed since it was asked about.
        "stale": review.state == "answered" and review.revision != current,
        "answered_at": review.updated_at.isoformat(),
    }


def waiting(shown: dict[str, Any] | None) -> bool:
    """Whether an answer waits for the administrator."""
    return bool(shown and shown["state"] == "answered" and shown["decision"] == "pending")


def approvable(shown: dict[str, Any] | None, actionable: tuple[str, ...]) -> bool:
    """An answer the administrator can approve with others: it says what to do, its
    passages are in the texts, and the texts are still the ones it was asked about."""
    return bool(
        shown
        and waiting(shown)
        and shown["answer"] in actionable
        and shown["checked"]
        and not shown["stale"]
    )


def to_ask(review: ChoiceReview | None, current: str, again: bool) -> bool:
    """Whether a run asks about the record: it has no answer to this question yet."""
    if again or review is None or review.state != "answered":
        return True
    return review.revision != current


def decide(review: ChoiceReview | None, agreed: bool) -> None:
    """The administrator decided about the record: with the answer, or otherwise."""
    if review is not None and review.state == "answered":
        review.decision = "approved" if agreed else "dismissed"
        review.decided_at = datetime.now(UTC)


def dismiss(sessions: sessionmaker[Session], name: str, kind: str, child: UUID) -> dict[str, Any]:
    """Set the review's answer aside. Nothing else changes."""
    with sessions.begin() as db:
        project = active_project(db, name, lock=True)
        review = review_of(db, project.id, kind, child)
        if review is None or review.state != "answered" or review.decision != "pending":
            raise ProjectConflict("There is no answer waiting for this record.")
        review.decision, review.decided_at = "dismissed", datetime.now(UTC)
    return {"id": str(child), "dismissed": True}


def start(
    sessions: sessionmaker[Session],
    name: str,
    kind: str,
    provider: str,
    model: str,
    token_cap: int,
    again: bool = False,
    only: Any = None,
) -> dict[str, Any]:
    """Begin a run over the records that wait and have no answer yet, or over all of them
    when asked to ask ``again``. ``only`` narrows them: it is given each record and its
    shortlist and says whether the run asks about it."""
    spec = KINDS[kind]
    with sessions.begin() as db:
        project = active_project(db, name, lock=True)
        if {"provider": provider, "model": model} not in review_flows.model_choices(db):
            raise ProjectInvalid("Choose a model of an account that can be asked.")
        if db.scalar(
            select(ChoiceRun.id).where(
                ChoiceRun.project_id == project.id,
                ChoiceRun.kind == kind,
                ChoiceRun.state.in_(LIVE),
            )
        ):
            raise ProjectConflict("A run is under way for this project. End it first.")
        review_budget.require_available(db, project.id)
        scope = spec.scope(db, project)
        refused = scope.refusal()
        if refused:
            raise ProjectInvalid(refused)
        run = ChoiceRun(
            project_id=project.id,
            kind=kind,
            state="queued",
            provider=provider,
            model=model,
            token_cap=token_cap,
        )
        db.add(run)
        db.flush()
        known = reviews(db, project.id, kind)
        answered = False
        for child in scope.children():
            listed = scope.shortlist(child)
            current = scope.revision(child, listed)
            review = known.get(child.id)
            if not to_ask(review, current, again):
                answered = True
                continue
            if only is not None and not only(child, listed):
                continue
            if review is None:
                review = ChoiceReview(project_id=project.id, kind=kind, child_id=child.id)
                db.add(review)
            review.run_id, review.state = run.id, "queued"
            review.revision, review.shortlist = current, listed
            review.answer = review.parent_id = review.confidence = review.reason = None
            review.evidence = review.missing = review.tokens = None
            review.provider = review.model = None
            review.checked, review.detail = False, ""
            review.decision, review.decided_at = "pending", None
            run.total += 1
        if not run.total:
            raise ProjectInvalid(spec.nothing_to_ask if answered else spec.nothing_waits)
        db.flush()
        return shown_run(run)


def control(
    sessions: sessionmaker[Session], name: str, kind: str, run_id: UUID, action: str
) -> dict[str, Any]:
    with sessions.begin() as db:
        project = active_project(db, name, lock=True)
        run = _locked(db, run_id)
        if run is None or run.project_id != project.id or run.kind != kind:
            raise ProjectNotFound(str(run_id))
        if run.state not in LIVE:
            raise ProjectConflict("The run is over.")
        if action == "stop":
            if run.state not in ASKING:
                raise ProjectConflict("The run is not asking.")
            run.state, run.detail = "paused", "You stopped it. Resume to ask the rest."
        elif action == "resume":
            if run.state in ASKING:
                raise ProjectConflict("The run is asking already.")
            review_budget.require_available(db, project.id)
            run.state, run.detail = "queued", ""
            _TROUBLE.pop(run.id, None)
            _WAIT.pop(run.id, None)
        else:
            # A call under way still reports what it used when it comes back.
            _finish(db, run, "cancelled", "You ended it.")
        db.flush()
        return shown_run(run)


# ---------------------------------------------------------------------------- the worker


@dataclass
class _Call:
    run: UUID
    review: UUID
    project: UUID
    asked: Question
    needed: int


# What a tick did when it asked nothing: there was nothing to do, or it moved a run on.
_IDLE: Final = "idle"
_MOVED: Final = "moved"
# Kept in memory, so a restart starts again: how many calls of a run failed in a row,
# and until when a run waits because every provider was busy.
_TROUBLE: dict[UUID, int] = {}
_WAIT: dict[UUID, float] = {}


def _lost(db: Session, run: ChoiceRun) -> None:
    """A call that never reported back: what it held is given back, and the record it
    asked about is marked. What the call used is unknown and is not counted."""
    _give_back(db, run.project_id, run.reserved)
    review = db.scalar(
        select(ChoiceReview).where(
            ChoiceReview.run_id == run.id,
            ChoiceReview.child_id == run.current_child,
            ChoiceReview.state == "queued",
        )
    )
    if review is not None:
        review.state = "failed"
        review.detail = "The call was interrupted before it answered. Run again to ask."
        run.failed += 1
    run.uncounted += 1
    run.claimed_at, run.current_child, run.reserved = None, None, 0


def _prepare(sessions: sessionmaker[Session]) -> _Call | str:
    """The next question of the oldest run that is asking, or what was done instead."""
    with sessions.begin() as db:
        now = datetime.now(UTC)
        for stale in db.scalars(
            select(ChoiceRun.id).where(
                ChoiceRun.claimed_at.is_not(None), ChoiceRun.claimed_at < now - LOST
            )
        ).all():
            run = _locked(db, stale)
            if (
                run is not None
                and run.claimed_at
                and run.claimed_at.replace(tzinfo=UTC) < now - LOST
            ):
                _lost(db, run)
        waiting_runs = [key for key, until in _WAIT.items() if until > time.monotonic()]
        found = db.scalar(
            select(ChoiceRun.id)
            .where(
                ChoiceRun.state.in_(ASKING),
                ChoiceRun.claimed_at.is_(None),
                ChoiceRun.id.not_in(waiting_runs),
            )
            .order_by(ChoiceRun.created_at, ChoiceRun.id)
            .limit(1)
        )
        run = _locked(db, found) if found else None
        if run is None or run.state not in ASKING or run.claimed_at is not None:
            return _IDLE
        project = db.get(ProjectRecord, run.project_id)
        if project is None or project.archived_at is not None:
            _finish(db, run, "cancelled", "The project was archived.")
            return _MOVED
        spec = KINDS.get(run.kind)
        if spec is None:
            _finish(db, run, "failed", "This kind of run is no longer known.")
            return _MOVED
        review = db.scalar(
            select(ChoiceReview)
            .where(ChoiceReview.run_id == run.id, ChoiceReview.state == "queued")
            .order_by(ChoiceReview.created_at, ChoiceReview.id)
            .limit(1)
        )
        if review is None:
            _finish(db, run, "complete")
            return _MOVED
        scope = spec.scope(db, project)
        child = scope.record(review.child_id)
        if child is None:
            # Decided since the run began: nothing is left to ask about it.
            db.delete(review)
            run.total = max(run.done + run.failed, run.total - 1)
            return _MOVED
        listed = scope.shortlist(child)
        review.shortlist, review.revision = listed, scope.revision(child, listed)
        asked = scope.question(run, child, listed)
        needed = review_budget.reservation_size(asked)
        if run.spent + needed > run.token_cap:
            left = run.total - run.done - run.failed
            _finish(
                db,
                run,
                "complete",
                f"Stopped at its limit of {run.token_cap:,} tokens with {left:,} "
                f"not asked about. Run again to ask the rest.",
            )
            return _MOVED
        return _Call(run.id, review.id, project.id, asked, needed)


def _admit(sessions: sessionmaker[Session], call: _Call) -> bool:
    """Hold the project's review tokens for the call and mark the run as asking."""
    try:
        held = review_budget.reserve(sessions, call.project, call.needed)
    except review_budget.ReviewBudgetHeld as held_back:
        with sessions.begin() as db:
            run = _locked(db, call.run)
            if run is not None and run.state in ASKING:
                run.state, run.detail = "held", str(held_back)
        return False
    with sessions.begin() as db:
        run = _locked(db, call.run)
        if run is None or run.state not in ASKING or run.claimed_at is not None:
            _give_back(db, call.project, held)
            return False
        review = db.get(ChoiceReview, call.review)
        if review is None or review.state != "queued":
            _give_back(db, call.project, held)
            return False
        run.state, run.detail = "running", ""
        run.claimed_at, run.current_child, run.reserved = datetime.now(UTC), review.child_id, held
        return True


def _settle(
    sessions: sessionmaker[Session], call: _Call, reply: Reply | None, label: str, detail: str
) -> None:
    """Record what the call used and answered, and decide whether the run goes on."""
    with sessions.begin() as db:
        run = _locked(db, call.run)
        if run is None:
            return
        spec = KINDS[run.kind]
        held = run.reserved
        run.claimed_at, run.current_child, run.reserved = None, None, 0
        review = db.get(ChoiceReview, call.review)
        tokens = tokens_of(reply) if reply is not None else None
        if reply is not None:
            add_project_usage(
                db, run.project_id, run.model, reply, spec.workflow, held if tokens else 0, None
            )
        if tokens:
            run.spent += tokens
        else:
            _give_back(db, run.project_id, held)
            if label not in UNBILLED:
                run.uncounted += 1
        problem = detail
        if reply is not None and not label and review is not None:
            scope = spec.scope(db, db.get_one(ProjectRecord, run.project_id))
            child = scope.record(review.child_id)
            try:
                if child is None:
                    raise Unusable("It was decided while the question was asked.")
                read = scope.read(reply.data, child, review.shortlist)
            except (Unusable, KeyError, ValueError) as unusable:
                problem = str(unusable) or "The answer could not be read."
            else:
                review.state, review.detail = "answered", ""
                review.answer, review.parent_id = read["answer"], read["parent_id"]
                review.confidence, review.reason = read["confidence"], read["reason"]
                review.evidence, review.checked = read["evidence"], read["checked"]
                review.missing = read["missing"]
                review.provider, review.model, review.tokens = reply.provider, reply.model, tokens
                review.decision, review.decided_at = "pending", None
                run.done += 1
                _TROUBLE.pop(run.id, None)
                return
        if label in NO_ACCOUNT:
            if run.state in ASKING:
                run.state = "paused"
                run.detail = f"{detail} Resume when the account can be asked."
            return
        if label == "busy":
            _WAIT[run.id] = time.monotonic() + BUSY_SECONDS
        elif review is not None and review.state == "queued":
            review.state, review.detail = "failed", problem or "The model gave no answer."
            run.failed += 1
        _TROUBLE[run.id] = _TROUBLE.get(run.id, 0) + 1
        if _TROUBLE[run.id] >= TRIES and run.state in ASKING:
            _TROUBLE.pop(run.id, None)
            run.state = "paused"
            run.detail = (
                f"{TRIES} calls in a row failed, the last with: "
                f"{problem or 'no answer'} Resume to ask the rest."
            )


async def tick(sessions: sessionmaker[Session], models: Models) -> bool:
    """Ask one question of one run. Whether something was done."""
    call = await asyncio.to_thread(_prepare, sessions)
    if isinstance(call, str):
        return call == _MOVED
    if not await asyncio.to_thread(_admit, sessions, call):
        return True
    try:
        reply = await models.ask(call.asked, retry=False)
    except ModelError as error:
        await asyncio.to_thread(_settle, sessions, call, error.metered, error.label, error.detail)
    except Exception:
        await asyncio.to_thread(
            _settle, sessions, call, None, "transport", "The call could not be made."
        )
        raise
    else:
        await asyncio.to_thread(_settle, sessions, call, reply, "", "")
    return True


async def worker(sessions: sessionmaker[Session], models: Models) -> None:
    while True:
        try:
            if await tick(sessions, models):
                continue
        except Exception:
            log.exception("A run that chooses parents failed")
        await asyncio.sleep(3)
