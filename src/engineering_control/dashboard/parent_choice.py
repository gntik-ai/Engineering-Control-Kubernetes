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
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol
from uuid import NAMESPACE_OID, UUID, uuid5

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import review_budget, review_flows, review_settings
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
    ModelTrialLine,
    ProjectRecord,
    ProjectReviewBudget,
    ScorerDecision,
)
from engineering_control.models.providers import family_of, maker
from engineering_control.models.service import ModelError, Models, Question, Reply

log = logging.getLogger(__name__)

LIVE: Final = ("queued", "running", "paused", "held")
ASKING: Final = ("queued", "running")
# After how long a call that never reported back is taken to be lost, how many calls in
# a row may fail before the run waits for the administrator, and how long a run waits
# when every provider was busy.
LOST: Final = timedelta(minutes=8)
TRIES: Final = 3
# A model that thinks before it answers spends that thinking against the limit its
# answer has, and how much it thinks is its own choice. The limit of a short answer is
# therefore the answer plus this room: without it such a model is cut before its answer
# begins, and the call is paid for nothing.
THINKING_ROOM: Final = 8000
# What a model's trial on a check must show before a run asks it first by default: this
# many questions settled by itself, and this share of them the same as on record.
GATE_SETTLED: Final = 30
GATE_SAME: Final = 0.9
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


def in_dollars(spec: Any) -> bool:
    """Whether runs of the kind are limited in US dollars, and hold and use none of the
    project's review tokens: what they are asked to measure was allowed as a sum."""
    return bool(getattr(spec, "dollars", False))


def settles(spec: Any, read: dict[str, Any]) -> bool:
    """Whether an answer of a run's first model is kept, or the question goes on to the
    run's own model: it is kept when it says something other than that it cannot tell,
    is not given with low confidence, and every passage it quotes is in the texts. A
    kind may say otherwise for its own answers."""
    own = getattr(spec, "settles", None)
    if own is not None:
        return bool(own(read))
    return (
        read.get("answer") != "unclear"
        and bool(read.get("checked"))
        and read.get("confidence") != "low"
    )


def trial_gate(
    settled: int, same: int, least: int = GATE_SETTLED, share: float = GATE_SAME
) -> bool:
    """Whether a model's trial on a check lets it be asked first by default: it settled
    enough questions by itself, and of those it said the same as the answer on record
    often enough."""
    return settled >= least and same >= share * settled


def gate_of(db: Session, project: UUID) -> tuple[int, float]:
    """What a trial must reach in this project: answers settled, and the share the same."""
    mine = review_settings.of(db, project)
    return mine.count("gate_settled"), mine.share("gate_same")


def suggested_first(db: Session, project: UUID, kind: str) -> dict[str, Any] | None:
    """The model a run of this check asks first unless told otherwise: the one whose
    latest trial on the check, in this project, passed the gate. Among several, the one
    whose questions cost least. None when no trial passed: nothing is asked first then."""
    seen: set[tuple[str, str]] = set()
    passed: list[tuple[float, int, dict[str, Any]]] = []
    least, share = gate_of(db, project)
    runs = db.scalars(
        select(ChoiceRun)
        .where(
            ChoiceRun.project_id == project,
            ChoiceRun.kind == "try_" + kind,
            ChoiceRun.state == "complete",
        )
        .order_by(ChoiceRun.created_at.desc(), ChoiceRun.id)
        .limit(20)
    ).all()
    for place, run in enumerate(runs):
        if (run.provider, run.model) in seen:
            continue
        seen.add((run.provider, run.model))
        lines = db.execute(
            select(ModelTrialLine.settled, ModelTrialLine.agrees, ModelTrialLine.cost).where(
                ModelTrialLine.run_id == run.id
            )
        ).all()
        settled = sum(1 for kept, _same, _cost in lines if kept)
        same = sum(1 for kept, agrees, _cost in lines if kept and agrees)
        if not trial_gate(settled, same, least, share):
            continue
        priced = [cost for _kept, _same, cost in lines if cost is not None]
        passed.append(
            (
                sum(priced) / len(priced) if priced else float("inf"),
                place,
                {
                    "provider": run.provider,
                    "model": run.model,
                    "answers": len(lines),
                    "settled": settled,
                    "same": same,
                },
            )
        )
    return min(passed, key=lambda entry: entry[:2])[2] if passed else None


def standing(
    scope: Any, child: Any, listed: list[dict[str, Any]], review: ChoiceReview | None
) -> str:
    """What an answer is compared with to see whether it still answers the question:
    the question as it stands now, or, where the kind says the answer still stands
    although the candidates it would be shown today are others, the answer's own."""
    current = str(scope.revision(child, listed))
    stands = getattr(scope, "stands", None)
    if (
        review is not None
        and stands is not None
        and review.state == "answered"
        and review.revision != current
        and stands(child, review)
    ):
        return review.revision
    return current


def agreeing(spec: Any, mine: dict[str, Any], theirs: dict[str, Any]) -> bool:
    """Whether two answers to the same question say the same: the same answer and the
    same parent. A kind may say otherwise for its own answers."""
    own = getattr(spec, "agreeing", None)
    if own is not None:
        return bool(own(mine, theirs))
    return mine.get("answer") == theirs.get("answer") and str(mine.get("parent_id")) == str(
        theirs.get("parent_id")
    )


def _answer(review: ChoiceReview) -> dict[str, Any]:
    return {
        "answer": review.answer,
        "parent_id": review.parent_id,
        "evidence": review.evidence or [],
    }


def _answer_of(said: dict[str, Any]) -> dict[str, Any]:
    return {**said, "answer": said.get("answer"), "parent_id": said.get("parent_id")}


def _said(spec: Any, read: dict[str, Any], reply: Reply, tokens: int | None) -> dict[str, Any]:
    """What a judge answered, as it is kept beside the answer. A kind whose answers say
    more than one thing keeps that too."""
    kept = getattr(spec, "kept", None)
    return {
        **(kept(read) if kept is not None else {}),
        "answer": read["answer"],
        "parent_id": str(read["parent_id"]) if read["parent_id"] else None,
        "reason": read["reason"],
        "checked": bool(read["checked"]),
        "provider": reply.provider,
        "model": reply.model,
        "tokens": tokens,
    }


def third_left(db: Session, project: UUID) -> float:
    """What the third model may still cost this month, in US dollars: what the project's
    settings give it, less what the runs begun this month spent on it."""
    month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    spent = sum(
        float(cost or 0.0)
        for (cost,) in db.execute(
            select(ChoiceRun.third_cost).where(
                ChoiceRun.project_id == project, ChoiceRun.created_at >= month
            )
        )
    )
    return review_settings.number(db, project, "doubt_dollars") - spent


def _to_third(db: Session, run: ChoiceRun, review: ChoiceReview) -> bool:
    """Whether what stays in doubt goes to the run's third model first: there is one,
    it was not asked about this record yet, and its dollars are not used up."""
    if not run.third_model or (review.judge or {}).get("third") is not None:
        return False
    if third_left(db, run.project_id) <= 0:
        return False
    review.phase = "third"
    _TROUBLE.pop(run.id, None)
    return True


def _sent_back(review: ChoiceReview, listed: list[dict[str, Any]]) -> str:
    """What an answer is told when it goes back: what another reader chose, by its
    title, and that the texts decide."""
    said = review.judge or {}
    title = next(
        (str(entry.get("title")) for entry in listed if entry.get("id") == said.get("parent_id")),
        "",
    )
    chose = f"chose “{title}”" if title else f"answered “{said.get('answer')}”"
    return (
        f" You answered this before. Another reader, who did not see your answer, {chose}. "
        "Read the texts again and answer by them alone: keep your answer where they support "
        "it, change it where they support the other."
    )


def _conclude(
    db: Session,
    spec: Any,
    run: ChoiceRun,
    review: ChoiceReview,
    child: Any,
    reply: Reply | None,
    agreed: bool | None,
) -> None:
    """Both are in: the record's answer is agreed or in doubt, and counts as done."""
    review.state, review.phase, review.detail = "answered", "", ""
    review.agreed = bool(agreed)
    run.done += 1
    run.agreed += 1 if agreed else 0
    _TROUBLE.pop(run.id, None)
    keep = getattr(spec, "answered", None)
    if keep is not None and reply is not None:
        keep(db, run, review, child, reply)


def agreed_lines(
    db: Session, project: ProjectRecord, kind: str, link_kind: str, outcome: str
) -> list[Any]:
    """The answers of a check that a judge agreed with, as lines for a batch: the record,
    the parent both chose, and why. Only answers that name a parent, whose passages are
    in the texts, that still are about the texts as they stand and that wait for the
    administrator. Shaped like the scorer's decisions, which they are not."""
    spec = KINDS[kind]
    scope = spec.scope(db, project)
    known = reviews(db, project.id, kind)
    lines = []
    for child in scope.children():
        review = known.get(child.id)
        if (
            review is None
            or review.state != "answered"
            or not review.agreed
            # Agreed only after the answer went back is not agreed enough for a batch:
            # measured on issues whose Story is known, such answers were right little
            # more than half the time.
            or review.rounds
            or review.decision != "pending"
            or review.parent_id is None
            or not review.checked
            or review.revision != standing(scope, child, scope.shortlist(child), review)
        ):
            continue
        judge = (review.judge or {}).get("model") or "a second model"
        quotes = " ".join(
            f"The {entry.get('side')} says: “{entry.get('text')}”."
            for entry in review.evidence or []
            if entry.get("found")
        )
        lines.append(
            ScorerDecision(
                id=uuid5(NAMESPACE_OID, f"agreed:{review.id}:{review.parent_id}"),
                project_id=project.id,
                kind=link_kind,
                child_id=child.id,
                parent_id=review.parent_id,
                outcome=outcome,
                confidence=1.0,
                model="model",
                detail={
                    "why": (
                        f"{review.model} and {judge} each chose this without seeing the "
                        f"other's answer. {review.reason or ''} {quotes}"
                    ).strip()
                },
            )
        )
    return lines


def tokens_of(reply: Reply) -> int | None:
    tokens = reply.usage.get("tokens")
    if tokens is None and all(
        type(reply.usage.get(k)) is int and reply.usage[k] >= 0 for k in ("input", "output")
    ):
        tokens = reply.usage["input"] + reply.usage["output"]
    return tokens if type(tokens) is int and tokens >= 0 else None


def sizes(asked: Question) -> tuple[int, int]:
    """What a question holds of the project's review tokens while it is asked, counted by
    the size of its text, and what it is likely to use. The first runs used about 1.8
    times what four characters a token and a short answer gave: the texts are dense, the
    shape of the answer is counted too, and a model that thinks writes more than its
    answer. So: two and a half characters a token, and an answer with its thinking."""
    return (
        review_budget.reservation_size(asked),
        (len(asked.system) + len(asked.prompt)) * 2 // 5 + 700,
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
        # With a first model: what it was asked and settled, what the run cost, and
        # what it would have cost had the run's own model answered all of it, from
        # what that model's calls cost.
        "first": {
            "provider": run.first_provider,
            "model": run.first_model,
            "asked": run.first_asked,
            "settled": run.first_done,
            "tokens": run.first_spent,
            "cost": round(run.first_cost or 0.0, 6),
        }
        if run.first_model
        else None,
        "cost": round(
            (run.cost or 0.0)
            + (run.first_cost or 0.0)
            + (run.judge_cost or 0.0)
            + (run.third_cost or 0.0),
            6,
        )
        if run.priced or run.first_cost or run.judge_cost or run.third_cost
        else None,
        "alone": round(run.cost / run.priced * run.done, 6)
        if run.first_model and run.priced and run.done
        else None,
        "dollar_cap": run.dollar_cap,
        # With a judge: how many answers it was asked for, how many the two agreed on,
        # and what asking it cost.
        "judge": {
            "provider": run.judge_provider,
            "model": run.judge_model,
            "asked": run.judge_asked,
            "agreed": run.agreed,
            "cost": round(run.judge_cost or 0.0, 6),
        }
        if run.judge_model
        else None,
        # A third model, asked about what the two did not agree on.
        "third": {
            "provider": run.third_provider,
            "model": run.third_model,
            "asked": run.third_asked,
            "cost": round(run.third_cost or 0.0, 6),
        }
        if run.third_model
        else None,
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
        # With a judge: whether its own answer was the same, and what it said. False is
        # in doubt; none where no judge was asked.
        "agreed": review.agreed,
        "judge": review.judge,
        "rounds": review.rounds,
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


def planned(db: Session, project: UUID, kind: str) -> dict[str, Any]:
    """What a run of this check asks when it is started without saying: the model the
    project's settings name to answer, their judge where the check can have one and it
    is of another maker, or else the model whose trial on the check agreed, asked first.
    And the tokens the run stops at."""
    spec = KINDS[kind]
    mine = review_settings.of(db, project)
    offered = review_settings.models(db, project)

    def usable(role: tuple[str, str] | None) -> tuple[str, str] | None:
        return role if role and {"provider": role[0], "model": role[1]} in offered else None

    answer = usable(mine.answer)
    judge = first = third = None
    if answer is not None:
        if getattr(spec, "judged", False):
            judge = usable(mine.judge)
            if judge is not None and family_of(*judge) == family_of(*answer):
                judge = None
        if judge is not None:
            # Asked only where the two differ: another model than both.
            third = usable(mine.doubt)
            if third in (answer, judge):
                third = None
        if judge is None and mine.cheaper_first and getattr(spec, "two_models", False):
            found = suggested_first(db, project, kind)
            named = (found["provider"], found["model"]) if found else None
            if named != answer:
                first = usable(named)

    def shown(role: tuple[str, str] | None) -> dict[str, str] | None:
        return {"provider": role[0], "model": role[1]} if role else None

    return {
        "answer": shown(answer),
        "judge": shown(judge),
        "first": shown(first),
        "third": shown(third),
        "tokens": mine.count("run_tokens"),
        "rounds": mine.rounds,
    }


# The checks a run of which is started from a worklist, as they are called.
CHECKS: Final = {
    "issue_story": "Issues with no User Story",
    "story_epic": "Primary Epics",
    "story_needed": "Clean-up: User Stories nothing supports",
    "epic_links": "Extra Epic links",
    "epic_overview": "Epic overviews",
}


def question_cost(db: Session, kind: str, named: tuple[str, str]) -> float | None:
    """What one question of this check has cost with this model, in US dollars: from
    the runs in which it answered or judged, in any project. None where it never did, or
    its account does not say what it costs."""
    cost = count = 0.0
    for spent, asked, provider, model in (
        (ChoiceRun.cost, ChoiceRun.priced, ChoiceRun.provider, ChoiceRun.model),
        (
            ChoiceRun.judge_cost,
            ChoiceRun.judge_asked,
            ChoiceRun.judge_provider,
            ChoiceRun.judge_model,
        ),
    ):
        for paid, questions in db.execute(
            select(spent, asked).where(
                ChoiceRun.kind == kind, provider == named[0], model == named[1], asked > 0
            )
        ):
            if paid:
                cost, count = cost + float(paid), count + int(questions)
    return cost / count if count else None


def estimates(db: Session, project: ProjectRecord) -> list[dict[str, Any]]:
    """For each check: what a run started now would ask, how many records wait for an
    answer, and what asking about them is likely to cost, from what the same questions
    cost before."""
    found = []
    for kind, label in CHECKS.items():
        spec = KINDS.get(kind)
        if spec is None:
            continue
        plan = planned(db, project.id, kind)
        scope = spec.scope(db, project)
        known = reviews(db, project.id, kind)
        waiting = (
            0
            if scope.refusal()
            else sum(
                1
                for child in scope.children()
                if (review := known.get(child.id)) is None or review.state != "answered"
            )
        )
        each: float | None = None
        if plan["answer"]:
            each = question_cost(db, kind, (plan["answer"]["provider"], plan["answer"]["model"]))
            if each is not None and plan["judge"]:
                judged = question_cost(
                    db, kind, (plan["judge"]["provider"], plan["judge"]["model"])
                )
                each = each + judged if judged is not None else None
        found.append(
            {
                "check": kind,
                "label": label,
                "plan": plan,
                "waiting": waiting,
                "each": round(each, 6) if each is not None else None,
                "cost": round(each * waiting, 4) if each is not None else None,
            }
        )
    return found


NO_ANSWERING_MODEL: Final = (
    "No model is chosen to answer this project's reviews. Choose it under Reviews, Settings."
)


def start(
    sessions: sessionmaker[Session],
    name: str,
    kind: str,
    provider: str = "",
    model: str = "",
    token_cap: int | None = None,
    again: bool = False,
    only: Any = None,
    first: tuple[str, str] | None = None,
    dollars: float | None = None,
    judge: tuple[str, str] | None = None,
    third: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Begin a run over the records that wait and have no answer yet, or over all of them
    when asked to ask ``again``. ``only`` narrows them: it is given each record and its
    shortlist and says whether the run asks about it. ``first`` names a cheaper model
    that is asked before the run's own: what it settles is kept, the rest goes on.
    ``dollars`` is what a run of a kind that is limited in money stops at.

    A run that names no model takes what the project's settings say: the model that
    answers, the judge or the cheaper model first, and the tokens it stops at."""
    spec = KINDS[kind]
    free = in_dollars(spec)
    if free and not dollars:
        raise ProjectInvalid("Say how many dollars the run may cost.")
    with sessions.begin() as db:
        project = active_project(db, name, lock=True)
        if not provider and not model:
            plan = planned(db, project.id, kind)
            if plan["answer"] is None:
                raise ProjectInvalid(NO_ANSWERING_MODEL)
            provider, model = plan["answer"]["provider"], plan["answer"]["model"]
            if first is None and judge is None:
                first = (
                    (plan["first"]["provider"], plan["first"]["model"]) if plan["first"] else None
                )
                judge = (
                    (plan["judge"]["provider"], plan["judge"]["model"]) if plan["judge"] else None
                )
                third = (
                    (plan["third"]["provider"], plan["third"]["model"]) if plan["third"] else None
                )
        if token_cap is None:
            token_cap = review_settings.of(db, project.id).count("run_tokens")
        choices = review_flows.model_choices(db)
        more = getattr(spec, "more_choices", None)
        if more is not None:
            choices = [*choices, *more(db)]
        if {"provider": provider, "model": model} not in choices:
            raise ProjectInvalid("Choose a model of an account that can be asked.")
        # A project's texts go only to the makers its settings allow.
        for named in (first, judge, third, (provider, model)):
            if named is not None and not review_settings.permits(db, project.id, *named):
                raise ProjectInvalid(
                    f"This project's texts are not sent to {maker(*named)}. Allow the maker "
                    "under Reviews, Settings, or choose another model."
                )
        if first is not None:
            if not getattr(spec, "two_models", False):
                raise ProjectInvalid("This kind of run asks one model only.")
            if {"provider": first[0], "model": first[1]} not in choices:
                raise ProjectInvalid("Choose a first model of an account that can be asked.")
            if first == (provider, model):
                raise ProjectInvalid("The first model is the run's own model. Leave it out.")
        if judge is not None:
            if not getattr(spec, "judged", False):
                raise ProjectInvalid("This kind of run has no judge.")
            if first is not None:
                raise ProjectInvalid("A run has a judge or a cheaper model first, not both.")
            if {"provider": judge[0], "model": judge[1]} not in choices:
                raise ProjectInvalid("Choose a judge of an account that can be asked.")
            if family_of(*judge) == family_of(provider, model):
                raise ProjectInvalid(
                    "The judge is of the family that answers. Choose a model of another maker."
                )
        if third is not None:
            if judge is None:
                raise ProjectInvalid("A third model is asked only where a judge differs.")
            if {"provider": third[0], "model": third[1]} not in choices or third in (
                judge,
                (provider, model),
            ):
                raise ProjectInvalid(
                    "The third model has to be one that can be asked, and another than the "
                    "model that answers and the judge."
                )
        if db.scalar(
            select(ChoiceRun.id).where(
                ChoiceRun.project_id == project.id,
                ChoiceRun.kind == kind,
                ChoiceRun.state.in_(LIVE),
            )
        ):
            raise ProjectConflict("A run is under way for this project. End it first.")
        if not free:
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
            first_provider=first[0] if first else None,
            first_model=first[1] if first else None,
            dollar_cap=dollars if free else None,
            judge_provider=judge[0] if judge else None,
            judge_model=judge[1] if judge else None,
            third_provider=third[0] if third else None,
            third_model=third[1] if third else None,
        )
        db.add(run)
        db.flush()
        known = reviews(db, project.id, kind)
        answered = False
        for child in scope.children():
            listed = scope.shortlist(child)
            review = known.get(child.id)
            current = standing(scope, child, listed, review)
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
            review.checked, review.detail, review.passed = False, "", False
            review.phase, review.judge, review.agreed, review.rounds = "", None, None, 0
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
            if not in_dollars(KINDS.get(run.kind)):
                review_budget.require_available(db, project.id)
            run.state, run.detail = "queued", ""
            # What failed is asked again with the rest: a run stops after failures in a
            # row because something was wrong with the asking, not with the records.
            again = db.scalars(
                select(ChoiceReview).where(
                    ChoiceReview.run_id == run.id, ChoiceReview.state == "failed"
                )
            ).all()
            for review in again:
                review.state, review.detail = "queued", ""
            run.failed = max(0, run.failed - len(again))
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
    # Asked of the run's first model, not of its own.
    first: bool = False
    # Of a run limited in dollars: it holds none of the project's review tokens.
    free: bool = False
    # Where the record stands: "" for the answer, "judge", or "back".
    phase: str = ""


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
        # The first model is asked about what it has not passed on yet.
        first = bool(run.first_model) and not review.passed
        asker = (
            ChoiceRun(provider=run.first_provider or "", model=run.first_model or "")
            if first
            else run
        )
        phase = review.phase if run.judge_model else ""
        if phase == "judge":
            # The judge is asked the same question and is not shown the answer.
            first = False
            asker = ChoiceRun(provider=run.judge_provider or "", model=run.judge_model or "")
        if phase == "third":
            # So is the third model, about what the two did not agree on.
            first = False
            asker = ChoiceRun(provider=run.third_provider or "", model=run.third_model or "")
        asked = scope.question(asker, child, listed)
        # The makers may have changed since the run began.
        named = asked.choices[0]
        if getattr(spec, "more_choices", None) is None and not review_settings.permits(
            db, project.id, *named
        ):
            run.state = "held"
            run.detail = (
                f"This project's texts are not sent to {maker(*named)}. Allow the maker "
                "under Reviews, Settings, and resume, or end the run."
            )
            return _MOVED
        if phase == "back":
            asked = replace(asked, system=asked.system + _sent_back(review, listed))
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
        free = in_dollars(spec)
        paid = (run.cost or 0.0) + (run.first_cost or 0.0) + (run.judge_cost or 0.0)
        if free and run.dollar_cap is not None and paid >= run.dollar_cap:
            left = run.total - run.done - run.failed
            _finish(
                db,
                run,
                "complete",
                f"Stopped at its limit of ${run.dollar_cap:,.2f} with {left:,} not asked about.",
            )
            return _MOVED
        return _Call(run.id, review.id, project.id, asked, needed, first, free, phase)


def _admit(sessions: sessionmaker[Session], call: _Call) -> bool:
    """Hold the project's review tokens for the call and mark the run as asking."""
    try:
        held = 0 if call.free else review_budget.reserve(sessions, call.project, call.needed)
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
                db,
                run.project_id,
                run.judge_model
                if call.phase == "judge"
                else run.third_model
                if call.phase == "third"
                else run.first_model
                if call.first
                else run.model,
                reply,
                spec.workflow,
                held if tokens else 0,
                None,
            )
            cost = reply.usage.get("cost")
            if call.phase == "judge":
                run.judge_asked += 1
                run.judge_cost += float(cost) if isinstance(cost, int | float) else 0.0
            elif call.phase == "third":
                run.third_asked += 1
                run.third_cost += float(cost) if isinstance(cost, int | float) else 0.0
            elif call.first:
                run.first_asked += 1
                run.first_spent += tokens or 0
                run.first_cost += float(cost) if isinstance(cost, int | float) else 0.0
            elif isinstance(cost, int | float):
                run.cost, run.priced = run.cost + float(cost), run.priced + 1
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
                if call.phase and child is not None:
                    # The judge, or the answer that went back, gave nothing usable:
                    # the two did not agree, and the first answer stands in doubt.
                    _conclude(db, spec, run, review, child, reply, None)
                    return
                if call.first and child is not None:
                    # Nothing usable from the first model: the run's own is asked.
                    review.passed = True
                    _TROUBLE.pop(run.id, None)
                    return
            else:
                if call.first and not settles(spec, read):
                    review.passed = True
                    _TROUBLE.pop(run.id, None)
                    return
                run.first_done += 1 if call.first else 0
                if call.phase == "third":
                    # It decides nothing: the answer stays in doubt, and says with which
                    # of the two the third reader went, if with either.
                    said = _said(spec, read, reply, tokens)
                    earlier = review.judge or {}
                    said["sides"] = (
                        "answer"
                        if agreeing(spec, _answer(review), read)
                        else "judge"
                        if earlier.get("answer") is not None
                        and agreeing(spec, _answer_of(earlier), read)
                        else "neither"
                    )
                    review.judge = {**earlier, "third": said}
                    _conclude(db, spec, run, review, child, reply, False)
                    return
                if call.phase == "judge":
                    review.judge = _said(spec, read, reply, tokens)
                    if agreeing(spec, _answer(review), read):
                        _conclude(db, spec, run, review, child, reply, True)
                    elif review.rounds < review_settings.of(db, run.project_id).rounds:
                        # The two differ: the answer goes back, told what the other chose.
                        review.phase, review.rounds = "back", review.rounds + 1
                        _TROUBLE.pop(run.id, None)
                    elif not _to_third(db, run, review):
                        _conclude(db, spec, run, review, child, reply, False)
                    return
                review.detail = ""
                review.answer, review.parent_id = read["answer"], read["parent_id"]
                review.confidence, review.reason = read["confidence"], read["reason"]
                review.evidence, review.checked = read["evidence"], read["checked"]
                review.missing = read["missing"]
                review.provider, review.model, review.tokens = reply.provider, reply.model, tokens
                review.decision, review.decided_at = "pending", None
                if call.phase == "back":
                    same = agreeing(spec, read, _answer_of(review.judge or {}))
                    if same or not _to_third(db, run, review):
                        _conclude(db, spec, run, review, child, reply, same)
                    return
                if run.judge_model and read["answer"] != "unclear":
                    # Kept, and not counted as done until the judge has answered too.
                    review.phase = "judge"
                    _TROUBLE.pop(run.id, None)
                    return
                if run.judge_model:
                    # An answer that cannot tell is in doubt without asking a judge.
                    review.agreed = False
                    if _to_third(db, run, review):
                        return
                review.state = "answered"
                run.done += 1
                _TROUBLE.pop(run.id, None)
                # A kind may keep more of an answer than the review holds.
                keep = getattr(spec, "answered", None)
                if keep is not None:
                    keep(db, run, review, child, reply)
                return
        if label in NO_ACCOUNT:
            if run.state in ASKING:
                run.state = "paused"
                run.detail = f"{detail} Resume when the account can be asked."
            return
        if label == "busy":
            _WAIT[run.id] = time.monotonic() + BUSY_SECONDS
        elif review is not None and review.state == "queued" and call.phase == "third":
            # The third model gave no answer: the record stays in doubt without it.
            review.judge = {**(review.judge or {}), "third": {"failed": problem or "No answer."}}
            review.state, review.phase, review.detail = "answered", "", ""
            review.agreed = False
            run.done += 1
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
