"""Admission and metering for a project's planning, coverage and connection reviews."""

from __future__ import annotations

import asyncio
import json
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
    row = db.get(ProjectReviewBudget, project_id)
    if row and row.required is not None:
        raise ReviewBudgetHeld("Authorize review tokens on the owning project’s Home tab first.")


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
            provider, model, question.system, question.prompt, question.native_score, route
        )
        return (
            len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + question.max_tokens + 256
        )
    text = question.system + with_shape(question) + shape.words(question.schema)
    return len(text.encode("utf-8")) + question.max_tokens + 256


def reserve(sessions: sessionmaker[Session], project_id: UUID, needed: int) -> int:
    with sessions.begin() as db:
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
        f"Project review token limit on hold. Authorize capacity for {required:,} tokens "
        "under Review token budget on the owning project's Home tab."
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


def release_unused(sessions: sessionmaker[Session], project_id: UUID, reserved: int) -> None:
    with sessions.begin() as db:
        db.execute(
            update(ProjectReviewBudget)
            .where(ProjectReviewBudget.project_id == project_id)
            .values(reserved=ProjectReviewBudget.reserved - reserved)
        )
