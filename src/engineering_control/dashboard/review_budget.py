"""Admission and metering for a project's planning, coverage and connection reviews."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import review_flows
from engineering_control.dashboard.project_records import ProjectConflict, active_project
from engineering_control.dashboard.workspace_costs import record_project_usage
from engineering_control.db.models import ProjectReviewBudget
from engineering_control.models import shape
from engineering_control.models.service import ModelError, Models, Question, Reply, with_shape


class ReviewBudgetHeld(ProjectConflict):
    """No provider request was admitted."""


def held_projects() -> Select[tuple[UUID]]:
    return select(ProjectReviewBudget.project_id).where(ProjectReviewBudget.required.is_not(None))


def status(db: Session, project_id: UUID) -> dict[str, int | None]:
    row = db.get(ProjectReviewBudget, project_id)
    return {
        "limit": row.token_limit if row else None,
        "used": row.used if row else 0,
        "reserved": row.reserved if row else 0,
        "required": row.required if row else None,
    }


def require_available(db: Session, project_id: UUID) -> None:
    from engineering_control.dashboard import review_settings

    reached = review_settings.over(db, project_id)
    if reached:
        raise ReviewBudgetHeld(reached)
    row = db.get(ProjectReviewBudget, project_id)
    if row and row.required is not None:
        raise ReviewBudgetHeld(
            "The project's review tokens are used up. Raise the limit under Reviews, Settings."
        )


def locked_budget(db: Session, project_id: UUID) -> ProjectReviewBudget | None:
    # SQLite ignores SELECT FOR UPDATE. Acquire the same write lock on both databases.
    return db.scalar(
        update(ProjectReviewBudget)
        .where(ProjectReviewBudget.project_id == project_id)
        .values(reserved=ProjectReviewBudget.reserved)
        .returning(ProjectReviewBudget)
    )


def authorize(
    sessions: sessionmaker[Session], name: str, expected: int | None, limit: int
) -> dict[str, int | None]:
    with sessions.begin() as db:
        project = active_project(db, name, lock=True)
        row = locked_budget(db, project.id)
        if expected != (row.token_limit if row else None):
            raise ProjectConflict("The review token limit changed. Reload the project.")
        if not row:
            row = ProjectReviewBudget(project_id=project.id, used=0, reserved=0)
            db.add(row)
        row.token_limit = limit
        required = row.required or 1
        row.required = required if limit - row.used - row.reserved < required else None
        db.flush()
        return status(db, project.id)


def reservation_size(question: Question) -> int:
    # Reserve input conservatively by UTF-8 byte length. Providers can receive the schema
    # both in the text and as a structured-output parameter, plus message framing.
    if question.native_score is not None:
        from engineering_control.models import decisions, openrouter

        provider, model = question.choices[0]
        route = (
            openrouter.routing(question.upstreams, question.allow_fallbacks, decisions=True)
            if provider == "openrouter"
            else {}
        )
        payload = decisions.body(
            provider,
            model,
            question.system,
            question.prompt,
            question.native_score,
            route,
            question.native_rubric,
        )
        return (
            len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + question.max_tokens + 256
        )
    text = question.system + with_shape(question) + shape.words(question.schema)
    return len(text.encode("utf-8")) + question.max_tokens + 256


def reserve(sessions: sessionmaker[Session], project_id: UUID, needed: int) -> int:
    from engineering_control.dashboard import review_settings

    with sessions.begin() as db:
        # The limit in dollars comes first: it holds whatever the tokens say.
        reached = review_settings.over(db, project_id)
        if reached:
            raise ReviewBudgetHeld(reached)
        row = locked_budget(db, project_id)
        if not row:
            return 0
        if row.required is None and row.token_limit - row.used - row.reserved >= needed:
            row.reserved += needed
            return needed
        row.required = row.required or needed
        required = row.required
    # Commit the hold before reporting it. Every scheduler sees it after a restart.
    raise ReviewBudgetHeld(
        f"The project's review tokens are used up. Raise the limit by at least {required:,} "
        "tokens under Reviews, Settings."
    )


async def ask_review(
    sessions: sessionmaker[Session],
    project_id: UUID,
    models: Models,
    question: Question,
    workflow: str,
    *,
    retry: bool = False,
    run_id: UUID | None = None,
    stage: str = "all",
    budgeted: bool = True,
) -> Reply:
    if run_id:
        question = await asyncio.to_thread(
            review_flows.question_for_run, sessions, project_id, run_id, stage, question
        )
    refused = await asyncio.to_thread(not_sent, sessions, project_id, question.choices[0])
    if refused:
        # Nothing was asked: the project's texts stay where its settings allow.
        raise ModelError("configuration", refused)
    reserved = (
        await asyncio.to_thread(reserve, sessions, project_id, reservation_size(question))
        if budgeted
        else 0
    )
    reply = None
    try:
        reply = await models.ask(question, retry=retry and not reserved)
        return reply
    except ModelError as exc:
        reply = exc.metered
        if reserved and reply is None and exc.label in ("no_key", "no_account", "configuration"):
            # No inference ran. An ambiguous transport failure keeps its reservation.
            await asyncio.to_thread(release_unused, sessions, project_id, reserved)
        raise
    finally:
        if reply is not None:
            await asyncio.to_thread(
                record_project_usage,
                sessions,
                project_id,
                question.choices[0][1],
                reply,
                workflow,
                reserved,
                run_id,
            )


def not_sent(
    sessions: sessionmaker[Session], project_id: UUID, named: tuple[str, str]
) -> str | None:
    """Why a review of the project does not ask this model: its maker is not among those
    the project's texts may be sent to."""
    from engineering_control.dashboard import review_settings
    from engineering_control.models.providers import maker

    with sessions() as db:
        if review_settings.permits(db, project_id, *named):
            return None
    return (
        f"This project's texts are not sent to {maker(*named)}. Allow the maker under "
        "Reviews, Settings, or choose another model there."
    )


def judge_for(
    sessions: sessionmaker[Session], project_id: UUID, answered: tuple[str, str]
) -> tuple[str, str] | None:
    """The judge the project's settings name, where it can judge an answer of this
    model: one that can be asked, of a maker the project's texts may be sent to, and of
    another family than the model that answered. None otherwise."""
    from engineering_control.dashboard import review_settings

    with sessions() as db:
        return review_settings.judge_of(db, project_id, answered)


async def ask_judge(
    sessions: sessionmaker[Session],
    project_id: UUID,
    models: Models,
    question: Question,
    answered: Reply,
    workflow: str,
    *,
    run_id: UUID | None = None,
    stage: str = "all",
    budgeted: bool = True,
) -> Reply | None:
    """Ask the project's judge the question another model just answered, without showing
    it the answer. None where the project names no judge for that model, or the judge
    gave no usable answer: the first answer then stands as it would without a judge.
    What the judge used is counted under the workflow's name with ``_judge``."""
    judge = await asyncio.to_thread(
        judge_for, sessions, project_id, (answered.provider, answered.model)
    )
    if judge is None:
        return None
    if run_id:
        # The same instructions and limits as the first model had; not its model.
        question = await asyncio.to_thread(
            review_flows.question_for_run, sessions, project_id, run_id, stage, question
        )
    try:
        return await ask_review(
            sessions,
            project_id,
            models,
            replace(question, choices=(judge,)),
            workflow + "_judge",
            budgeted=budgeted,
        )
    except (ModelError, ReviewBudgetHeld):
        return None


def also_proposed(title: Any, theirs: list[Any]) -> bool:
    """Whether a judge proposed a record like this one: one of its titles shares at
    least half of this title's words."""

    def words(text: Any) -> set[str]:
        return set("".join(c if c.isalnum() else " " for c in str(text or "").lower()).split())

    mine = words(title)
    return bool(mine) and any(2 * len(mine & words(other)) >= len(mine) for other in theirs)


def release_unused(sessions: sessionmaker[Session], project_id: UUID, reserved: int) -> None:
    with sessions.begin() as db:
        db.execute(
            update(ProjectReviewBudget)
            .where(ProjectReviewBudget.project_id == project_id)
            .values(reserved=ProjectReviewBudget.reserved - reserved)
        )
