"""Resolve and pin review definitions at the durable worker's logical run boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import ProjectInvalid, active_project
from engineering_control.db.models import (
    EpicExecutionPlan,
    FlowVersion,
    PlanningReview,
    ProjectConnection,
    ProjectFlow,
    ProviderAccount,
    ReviewFlowAssignment,
    ReviewFlowRun,
    WorkspaceImport,
)
from engineering_control.engine.document import Document
from engineering_control.engine.library import document_in_force
from engineering_control.engine.reviews import ACTIONS, Policy, action_of, default_key
from engineering_control.models import openrouter
from engineering_control.models.service import Question

Owner = WorkspaceImport | PlanningReview | ProjectConnection | EpicExecutionPlan


def resolve(db: Session, pid: UUID, action: str) -> tuple[Document, int]:
    assignment = db.get(ReviewFlowAssignment, (pid, action))
    key, version = (
        (assignment.flow_key, assignment.version) if assignment else (default_key(action), None)
    )
    found = document_in_force(db, key, version, review=True)
    if not found or action_of(found[0]) != action:
        raise ProjectInvalid("Choose a published flow for this review in project Flow assignments.")
    return found


def assignments(sessions: sessionmaker[Session], name: str) -> dict[str, Any]:
    with sessions() as db:
        project = active_project(db, name)
        configured = {
            r.action: r
            for r in db.scalars(
                select(ReviewFlowAssignment).where(ReviewFlowAssignment.project_id == project.id)
            )
        }
        return {
            "items": [
                {
                    "action": a,
                    "name": title,
                    "flow": configured[a].flow_key if a in configured else default_key(a),
                    "version": configured[a].version if a in configured else None,
                    "inherited": a not in configured,
                }
                for a, (title, _) in ACTIONS.items()
            ]
        }


def assign(
    sessions: sessionmaker[Session], name: str, action: str, key: str | None, version: int | None
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise ProjectInvalid("Unknown review action.")
    with sessions.begin() as db:
        project = active_project(db, name, lock=True)
        row = db.get(ReviewFlowAssignment, (project.id, action))
        if key is None:
            if version is not None:
                raise ProjectInvalid("Select a flow before pinning a version.")
            if row:
                db.delete(row)
        else:
            # Serialize assignment with deletion of a published custom definition.
            db.execute(
                select(FlowVersion.id).where(FlowVersion.flow_key == key).with_for_update()
            ).all()
            owner = db.get(ProjectFlow, key)
            found = document_in_force(db, key, version, review=True)
            if (
                (owner and owner.project_id != project.id)
                or not found
                or action_of(found[0]) != action
            ):
                raise ProjectInvalid(
                    "Choose a published global or project flow for this review action."
                )
            if not row:
                row = ReviewFlowAssignment(project_id=project.id, action=action)
                db.add(row)
            row.flow_key, row.version = key, version
    return {"action": action}


def finish(db: Session, owner: Owner, outcome: str) -> None:
    row = db.get(ReviewFlowRun, owner.review_run_id) if owner.review_run_id else None
    if row and row.finished_at is None:
        row.outcome, row.finished_at = outcome, datetime.now(UTC)


def start(
    db: Session, owner: Owner, pid: UUID, action: str, *, fresh: bool = False, resume: bool = False
) -> UUID:
    old = db.get(ReviewFlowRun, owner.review_run_id) if owner.review_run_id else None
    if old and old.action == action and not fresh and (old.finished_at is None or resume):
        if resume:
            old.outcome, old.finished_at = None, None
        return old.id
    document, version = resolve(db, pid, action)
    if old and old.finished_at is None:
        finish(db, owner, "cancelled")
    policy = Policy.from_document(document).settings()
    if policy.get("evidence_nodes"):
        policy["document"] = document.as_dict()
    row = ReviewFlowRun(
        project_id=pid,
        flow_key=document.key,
        flow_name=document.name,
        action=action,
        version=version,
        settings=policy,
    )
    db.add(row)
    db.flush()
    owner.review_run_id = row.id
    return row.id


def wording(settings: dict[str, Any], stage: str) -> str:
    return "\n".join(
        text for scope, text in settings.get("instructions", []) if scope in ("all", stage)
    )


def configure(db: Session, pid: UUID, run_id: UUID, stage: str, question: Question) -> Question:
    run = db.get(ReviewFlowRun, run_id)
    if not run or run.project_id != pid:
        raise ProjectInvalid("The review flow run does not belong to this project.")
    extra = wording(run.settings, stage)
    generation = run.settings.get("generation") if question.native_score is None else None
    return replace(
        question,
        choices=(tuple(generation.split(":", 1)),) if generation else question.choices,
        upstreams=run.settings.get("upstreams", question.upstreams)
        if question.native_score is None
        else question.upstreams,
        allow_fallbacks=run.settings.get("allow_fallbacks", question.allow_fallbacks)
        if question.native_score is None
        else question.allow_fallbacks,
        system=question.system + ("\n\nAdministrator instructions\n" + extra if extra else ""),
        max_tokens=min(question.max_tokens, run.settings.get("max_output") or question.max_tokens),
    )


def question_for_run(
    sessions: sessionmaker[Session], pid: UUID, run_id: UUID, stage: str, question: Question
) -> Question:
    with sessions() as db:
        return configure(db, pid, run_id, stage, question)


def effective_settings(
    db: Session, pid: UUID, action: str, run_id: UUID | None = None, *, active: bool = False
) -> dict[str, Any]:
    run = db.get(ReviewFlowRun, run_id) if run_id else None
    if (
        run
        and run.project_id == pid
        and (not active or (run.action == action and run.finished_at is None))
    ):
        return run.settings
    return Policy.from_document(resolve(db, pid, action)[0]).settings()


def matching_revision(db: Session, pid: UUID, action: str, run_id: UUID | None = None) -> str:
    settings = effective_settings(db, pid, action, run_id)
    return matching_key(settings)


def matching_key(settings: dict[str, Any]) -> str:
    from engineering_control.dashboard.connection_records import digest

    text = wording(settings, "matching")
    if (
        settings.get("generation")
        or settings.get("upstreams")
        or settings.get("allow_fallbacks") is False
    ):
        return digest(
            [
                text,
                settings.get("generation"),
                settings.get("upstreams"),
                settings.get("allow_fallbacks", True),
            ]
        )
    return text


def model_choices(session: Session) -> list[dict[str, str]]:
    fields = session.execute(
        select(
            ProviderAccount.provider, ProviderAccount.models, ProviderAccount.model_catalog
        ).where(
            ProviderAccount.archived_at.is_(None),
            ProviderAccount.paused.is_(False),
            ProviderAccount.sealed_key.is_not(None),
            ProviderAccount.state.in_(("ready", "unchecked", "limited")),
        )
    )
    choices = {
        (p, m)
        for p, models, catalog in fields
        for m in (models or [])
        if isinstance(m, str)
        and (p != "openrouter" or openrouter.supports((catalog or {}).get(m, {})))
    }
    return [{"provider": p, "model": m} for p, m in sorted(choices)]


def decision_models(db: Session) -> list[dict[str, Any]]:
    accounts = db.execute(
        select(
            ProviderAccount.provider,
            ProviderAccount.models,
            ProviderAccount.model_catalog,
            ProviderAccount.prices,
        )
        .where(
            ProviderAccount.provider.in_(("openai", "openrouter")),
            ProviderAccount.archived_at.is_(None),
            ProviderAccount.paused.is_(False),
            ProviderAccount.sealed_key.is_not(None),
            ProviderAccount.state.in_(("ready", "unchecked", "limited")),
        )
        .order_by(ProviderAccount.name)
    )
    options: dict[tuple[str, str], dict[str, Any]] = {}
    rates: dict[str, Any] | None
    for provider, models, catalog, prices in accounts:
        for model in models or []:
            info = (catalog or {}).get(model, {})
            if provider == "openai":
                if model != openrouter.DIRECT_MODEL:
                    continue
                endpoint, rates, questions = (
                    openrouter.DIRECT_ENDPOINT,
                    openrouter.DIRECT_PRICE,
                    openrouter.TYPES,
                )
            else:
                if not openrouter.supports(info, decision=True):
                    continue
                endpoint, rates, questions = (
                    openrouter.DECISIONS,
                    (prices or {}).get(model),
                    info["questions"],
                )
            options.setdefault(
                (provider, model),
                {
                    "provider": provider,
                    "model": model,
                    "endpoint": endpoint,
                    "rates": rates,
                    "questions": list(questions),
                },
            )
    return [options[k] for k in sorted(options)]


def designer_models(sessions: sessionmaker[Session]) -> dict[str, Any]:
    with sessions() as db:
        return {"api_models": model_choices(db), "decision_models": decision_models(db)}
