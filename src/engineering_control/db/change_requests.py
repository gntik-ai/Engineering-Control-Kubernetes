"""NEW-108: durable operator change requests and their bound state transitions."""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from engineering_control.db.models import (
    ACTIVE_DECOMPOSITION_STATUSES,
    BacklogDecomposition,
    ChangeRequest,
    Run,
    UiBlueprint,
)
from engineering_control.engineering.backlog import (
    is_reauthenticated_backlog_operator,
)
from engineering_control.engineering.change_requests import (
    CHANGE_REQUEST_REPOSITORY_ALTERNATION,
    MAXIMUM_ISSUE_BODY_CHARACTERS,
    ChangeProposal,
    ChangeRequestIntake,
    ChangeReviewRecord,
    DecompositionChangeContext,
    RenderedChangeRequest,
    UiChangeContext,
    approval_phrase,
    approval_record,
    approval_record_matches,
    change_request_workflow_id,
    decomposition_lineage,
    render_change_request,
    ui_context_json,
    validate_proposal,
)
from engineering_control.engineering.decomposition_fanout import (
    DEPENDENCIES_AWAITING,
    DEPENDENCIES_STALE,
    LINEAGE_BLOCKING_STATUSES,
    SUPERSEDED_BY_EXISTING,
    import_risk,
    lineage_of,
)
from engineering_control.security.redaction import SecretRedactor, bounded_detail

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_ISSUE_URL = re.compile(
    r"^https://github\.com/gntik-ai/"
    rf"({CHANGE_REQUEST_REPOSITORY_ALTERNATION})"
    r"/issues/([1-9][0-9]*)$"
)


# NEW-146 review 1: what a fan-out item write that no longer owns its blueprint returns.
UI_FANOUT_SUPERSEDED = "UI_FANOUT_SUPERSEDED"
# NEW-234: the same, for a write that no longer owns its backlog decomposition.
DECOMPOSITION_FANOUT_SUPERSEDED = "DECOMPOSITION_FANOUT_SUPERSEDED"
# NEW-154: merge hops blocker resolution follows (a canonical request may be merged again);
# consolidation refuses chains longer than this (engineering.ui_gap_consolidation).
from engineering_control.engineering.ui_gap_consolidation import MAX_MERGE_HOPS  # noqa: E402


class ChangeRequestNotFoundError(ValueError):
    """The requested change request does not exist."""


class DependenciesAwaitingError(ValueError):
    """NEW-234 PR6: a decomposition request whose predecessors are not all published."""


LOGGER = logging.getLogger(__name__)
# NEW-234 PR6 review: the session flag of the one change-request lock a transaction takes.
_REQUEST_LOCKS = "engineering_control.change_request_locks"


class ChangeRequestRepository:
    """Every change-request state transition, each re-checked under a row lock."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
    ) -> None:
        if engine is None:
            if database_url is None:
                raise ValueError("change request repository requires a database")
            engine = create_engine(
                database_url,
                pool_pre_ping=True,
                **(
                    {"pool_size": 2, "max_overflow": 2, "connect_args": {"connect_timeout": 5}}
                    if database_url.startswith("postgresql")
                    else {}
                ),
            )
        self.engine = engine
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    # Operator API -----------------------------------------------------------------------

    async def create(self, intake: ChangeRequestIntake, requested_by: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.create_sync, intake, requested_by)

    def create_sync(self, intake: ChangeRequestIntake, requested_by: str) -> dict[str, Any]:
        if not is_reauthenticated_backlog_operator(requested_by):
            raise ValueError("change requests require an allowlisted operator channel")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = ChangeRequest(
                id=uuid4(),
                request_key=f"chg-{uuid4().hex}",
                project=intake.project,
                status="DRAFTING",
                revision=1,
                description_redacted=intake.description_redacted,
                description_sha256=intake.description_sha256,
                redacted=intake.redacted,
                revision_note=None,
                proposal_json={},
                review_json={},
                dedupe_json={},
                target_repository=intake.target_repository,
                requested_by=requested_by,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._view(row)

    def create_ui_item_sync(
        self,
        change_request_id: UUID,
        intake: ChangeRequestIntake,
        requested_by: str,
        ui_context: UiChangeContext,
        *,
        fanout_writer: str | None = None,
    ) -> dict[str, Any]:
        """NEW-123: one fanned-out blueprint item, idempotent under its deterministic id.

        A repeated (resumed) fan-out finds the existing row and never creates a second one;
        a row under the same id for a different item is refused. NEW-146: with a
        ``fanout_writer``, only the blueprint's active fan-out may create or load it."""
        if not is_reauthenticated_backlog_operator(requested_by):
            raise ValueError("change requests require an allowlisted operator channel")
        context = ui_context_json(ui_context)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            if fanout_writer is not None and not self._fanout_owns(
                session, str(context["blueprint_id"]), fanout_writer
            ):
                return {"status": "SUPERSEDED", "blocker": UI_FANOUT_SUPERSEDED}
            row = session.get(ChangeRequest, change_request_id, with_for_update=True)
            if row is not None:
                stored = row.ui_context_json if isinstance(row.ui_context_json, dict) else {}
                if (
                    stored.get("blueprint_id") != context["blueprint_id"]
                    or stored.get("item_key") != context["item_key"]
                ):
                    raise ValueError("change request id is bound to another blueprint item")
                return {**self._summary(row), "created": False}
            # NEW-154: an item created after a gap consolidation is blocked by the canonical
            # request, never by a merged one.
            context["blocked_by"] = self._resolved_dependencies(
                session, list(context.get("blocked_by") or [])
            )
            row = ChangeRequest(
                id=change_request_id,
                request_key=f"ui-{change_request_id.hex}",
                project=intake.project,
                status="DRAFTING",
                revision=1,
                description_redacted=intake.description_redacted,
                description_sha256=intake.description_sha256,
                redacted=intake.redacted,
                revision_note=None,
                proposal_json={},
                review_json={},
                dedupe_json={},
                target_repository=intake.target_repository,
                requested_by=requested_by,
                ui_context_json=context,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return {**self._summary(row), "created": True}

    def create_decomposition_task_sync(
        self,
        change_request_id: UUID,
        intake: ChangeRequestIntake,
        requested_by: str,
        context: DecompositionChangeContext,
        *,
        fanout_writer: str,
        replace: bool = False,
    ) -> dict[str, Any]:
        """NEW-234 PR6: one fanned-out decomposition task, idempotent under its id.

        Fenced by the decomposition's active fan-out marker (atomic with the row write). The
        first dedupe of design section 4: another active request of the same lineage
        (project:story:key: drafting, a candidate, approved or published, typically from an
        earlier plan) supersedes this task (SKIPPED SUPERSEDED_BY_EXISTING). With
        ``replace`` an unpublished one is rejected (an operator decision, recorded with the
        approving operator) and this task is filed instead; a published one always
        supersedes it. The partial unique index of migration 0023 (one active request per
        lineage) makes the check race-proof across decompositions: a concurrent insert of
        the same lineage fails and is reported as SKIPPED SUPERSEDED_BY_EXISTING."""
        if not is_reauthenticated_backlog_operator(requested_by):
            raise ValueError("change requests require an allowlisted operator channel")
        stored_context = context.model_dump(mode="json")
        lineage = decomposition_lineage(intake.project, context)
        try:
            with self.sessions.begin() as session:
                return self._create_decomposition_task(
                    session,
                    change_request_id,
                    intake,
                    requested_by,
                    context,
                    stored_context,
                    lineage,
                    fanout_writer=fanout_writer,
                    replace=replace,
                )
        except IntegrityError:
            # Another decomposition filed this lineage (or this id) concurrently.
            with self.sessions() as session:
                existing = session.scalars(
                    select(ChangeRequest)
                    .where(
                        (ChangeRequest.id == change_request_id)
                        | (
                            (ChangeRequest.decomposition_lineage == lineage)
                            # The constraint's own predicate (migration 0023).
                            & ChangeRequest.status.in_(ACTIVE_DECOMPOSITION_STATUSES)
                        )
                    )
                    .order_by(ChangeRequest.created_at, ChangeRequest.id)
                ).all()
                return self._superseded_task(lineage, existing)

    def _create_decomposition_task(
        self,
        session: Any,
        change_request_id: UUID,
        intake: ChangeRequestIntake,
        requested_by: str,
        context: DecompositionChangeContext,
        stored_context: dict[str, Any],
        lineage: str,
        *,
        fanout_writer: str,
        replace: bool,
    ) -> dict[str, Any]:
        if not self._decomposition_fanout_owns(session, context.decomposition_id, fanout_writer):
            return {"status": "SUPERSEDED", "blocker": DECOMPOSITION_FANOUT_SUPERSEDED}
        active = list(
            session.scalars(
                select(ChangeRequest.id).where(
                    ChangeRequest.decomposition_lineage == lineage,
                    ChangeRequest.id != change_request_id,
                    ChangeRequest.status.in_(sorted(LINEAGE_BLOCKING_STATUSES)),
                )
            ).all()
        )
        # The one change-request lock of this transaction: this id and its active matches.
        locked = self._lock_requests(session, [change_request_id, *active])
        row = locked.get(change_request_id)
        if row is not None:
            stored = (
                row.decomposition_context_json
                if isinstance(row.decomposition_context_json, dict)
                else {}
            )
            if stored.get("task_key") != context.task_key or row.decomposition_lineage != lineage:
                raise ValueError("change request id is bound to another decomposition task")
            if stored.get("decomposition_id") != context.decomposition_id:
                # The same task of an identical plan (same plan hash), fanned out by an
                # earlier decomposition: that request stands for this task.
                return self._superseded_task(lineage, [row])
            return {**self._summary(row), "created": False}
        existing = sorted(
            (
                item
                for identity, item in locked.items()
                if identity != change_request_id
                and item.decomposition_lineage == lineage
                and item.status in LINEAGE_BLOCKING_STATUSES
            ),
            key=lambda item: (self._iso(item.created_at) or "", str(item.id)),
        )
        matches = [self._lineage_match(item) for item in existing]
        if existing and (not replace or any(item.status == "PUBLISHED" for item in existing)):
            return self._superseded_task(lineage, existing)
        now = datetime.now(UTC)
        for item in existing:
            # --replace: the operator decided this task replaces the unpublished request.
            item.status = "REJECTED"
            item.decided_by = requested_by
            item.decided_at = now
            item.decision_reason = (
                f"replaced by backlog decomposition {context.decomposition_id} (--replace)"
            )
            item.decision_sequence += 1
            item.updated_at = now
        session.flush()
        row = ChangeRequest(
            id=change_request_id,
            request_key=f"dcp-{change_request_id.hex}",
            project=intake.project,
            status="DRAFTING",
            revision=1,
            description_redacted=intake.description_redacted,
            description_sha256=intake.description_sha256,
            redacted=intake.redacted,
            revision_note=None,
            proposal_json={},
            review_json={},
            # Replaced lineage matches stay visible on the new request.
            dedupe_json={"lineage_matches": matches[:5]} if matches else {},
            target_repository=intake.target_repository,
            requested_by=requested_by,
            decomposition_context_json=stored_context,
            decomposition_lineage=lineage,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return {**self._summary(row), "created": True, "replaced": matches[:5]}

    @staticmethod
    def _lineage_match(item: ChangeRequest) -> dict[str, Any]:
        return {
            "change_request_id": str(item.id),
            "status": item.status,
            "issue_url": item.issue_url,
            "decomposition_id": (item.decomposition_context_json or {}).get("decomposition_id"),
        }

    @classmethod
    def _superseded_task(cls, lineage: str, existing: Sequence[ChangeRequest]) -> dict[str, Any]:
        return {
            "status": "SKIPPED",
            "blocker": SUPERSEDED_BY_EXISTING,
            "lineage": lineage,
            "published": any(item.status == "PUBLISHED" for item in existing),
            "existing": [cls._lineage_match(item) for item in existing][:5],
        }

    def item_states_sync(self, change_request_ids: list[UUID]) -> dict[str, dict[str, Any]]:
        """NEW-123: the current status of fanned-out items (missing ids are absent)."""
        if not change_request_ids:
            return {}
        with self.sessions() as session:
            rows = session.scalars(
                select(ChangeRequest).where(ChangeRequest.id.in_(change_request_ids))
            ).all()
            return {str(row.id): {"status": row.status, "revision": row.revision} for row in rows}

    async def list_requests(
        self, project: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_requests_sync, project, status, limit)

    def list_requests_sync(
        self, project: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        with self.sessions() as session:
            query = select(ChangeRequest)
            if project is not None:
                query = query.where(ChangeRequest.project == project)
            if status is not None:
                query = query.where(ChangeRequest.status == status)
            rows = session.scalars(
                query.order_by(ChangeRequest.created_at.desc()).limit(max(1, min(limit, 200)))
            ).all()
            return [self._summary(row) for row in rows]

    async def get(self, change_request_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_sync, change_request_id)

    def get_sync(self, change_request_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(ChangeRequest, change_request_id)
            if row is None:
                return None
            view = self._view(row)
            # NEW-154: a merged request resolves to its canonical one, and every blocker is
            # shown resolved (a published body may still name a merged request).
            if row.merged_into_id is not None:
                view["merged_into"] = self._resolution(session, row)
            context = row.ui_context_json if isinstance(row.ui_context_json, dict) else {}
            resolved = [
                {
                    "change_request_id": entry.get("change_request_id"),
                    "item_key": entry.get("item_key"),
                    **self._resolve_id(session, entry.get("change_request_id")),
                }
                for entry in context.get("blocked_by") or []
                if isinstance(entry, dict)
            ]
            if any(entry["merged"] for entry in resolved):
                # Only when a blocker was merged (e.g. a published body that still names a
                # merged request): every blocker as the request it stands for now.
                view["blocked_by_resolved"] = resolved
            if row.decomposition_lineage and row.status == "CANDIDATE":
                # NEW-234 PR6: approval is refused (409) until every predecessor is published.
                _refs, awaiting = self._dependency_state(
                    session, row.project, self._decomposition_dict(row)
                )
                view["awaiting_dependencies"] = awaiting
                if awaiting:
                    view["approve_command"] = None
            return view

    async def resolve(self, change_request_id: UUID) -> dict[str, Any]:
        return await asyncio.to_thread(self.resolve_sync, change_request_id)

    def resolve_sync(self, change_request_id: UUID) -> dict[str, Any]:
        """NEW-154: the request a (possibly merged) change-request id stands for: merged ids
        resolve, hop by hop, to their canonical request. Delivery and blocker resolution
        use this for every change-request reference (a published body may name a merged
        request)."""
        with self.sessions() as session:
            row = session.get(ChangeRequest, change_request_id)
            if row is None:
                raise ChangeRequestNotFoundError("change request not found")
            return self._resolution(session, row)

    @classmethod
    def _resolve_id(cls, session: Any, change_request_id: Any) -> dict[str, Any]:
        try:
            identity = UUID(str(change_request_id))
        except ValueError:
            return {"resolved_id": None, "merged": False}
        row = session.get(ChangeRequest, identity)
        if row is None:
            return {"resolved_id": str(identity), "merged": False}
        resolved = cls._resolution(session, row)
        return {
            "resolved_id": resolved["change_request_id"],
            "resolved_item_key": resolved["item_key"],
            "resolved_status": resolved["status"],
            "resolved_issue_url": resolved["issue_url"],
            "merged": bool(resolved["merged_via"]),
        }

    @staticmethod
    def _resolution(session: Any, row: ChangeRequest) -> dict[str, Any]:
        chain: list[str] = []
        current = row
        while current.status == "MERGED" and current.merged_into_id is not None:
            if len(chain) >= MAX_MERGE_HOPS or str(current.id) in chain:
                raise ValueError("change request merge chain is too long or cyclic")
            chain.append(str(current.id))
            following = session.get(ChangeRequest, current.merged_into_id)
            if following is None:
                raise ValueError("merged change request names a missing canonical request")
            current = following
        context = current.ui_context_json if isinstance(current.ui_context_json, dict) else {}
        return {
            "change_request_id": str(current.id),
            "item_key": context.get("item_key"),
            "status": current.status,
            "issue_url": current.issue_url,
            "merged_via": chain,
        }

    @classmethod
    def _resolved_dependencies(
        cls, session: Any, dependencies: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        resolved: dict[str, dict[str, Any]] = {}
        for entry in dependencies:
            target = dict(entry)
            try:
                row = session.get(ChangeRequest, UUID(str(entry.get("change_request_id"))))
            except ValueError:
                row = None
            if row is not None and row.status == "MERGED":
                found = cls._resolution(session, row)
                if found["item_key"]:
                    target = {
                        "change_request_id": found["change_request_id"],
                        "item_key": found["item_key"],
                    }
            resolved.setdefault(str(target["change_request_id"]), target)
        return list(resolved.values())

    async def approve(
        self, change_request_id: UUID, binding: str, phrase: str, actor: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.approve_sync, change_request_id, binding, phrase, actor)

    def approve_sync(
        self, change_request_id: UUID, binding: str, phrase: str, actor: str
    ) -> dict[str, Any]:
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("change request approval requires an allowlisted operator channel")
        stale = False
        with self.sessions.begin() as session:
            row = self._locked(session, change_request_id)
            stored = row.approval_binding_sha256 or ""
            bound = bool(stored) and hmac.compare_digest(stored, binding)
            expected_phrase = approval_phrase(row.project, stored) if stored else ""
            phrase_matches = bool(stored) and hmac.compare_digest(expected_phrase, phrase)
            if row.status == "APPROVED" and bound and phrase_matches:
                # Idempotent replay: the caller re-issues the deterministic publish workflow.
                replayed = {**self._view(row), "replayed": True}
            else:
                replayed = None
            if replayed is None:
                if row.status != "CANDIDATE":
                    raise ValueError("only a CANDIDATE change request can be approved")
                if not bound:
                    raise ValueError("change request approval binding changed")
                if not phrase_matches:
                    raise ValueError("typed confirmation does not bind to this change request")
                self._verify_stored_content(row)
                # NEW-234 PR6: a decomposition task is approvable only once every
                # predecessor is published and its body names each one (final content).
                stale = self._dependency_gate(session, row)
            if replayed is None and not stale:
                now = datetime.now(UTC)
                row.status = "APPROVED"
                row.decided_by = actor
                row.decided_at = now
                row.decision_reason = None
                row.error_code = None
                row.decision_sequence += 1
                row.approval_snapshot_json = approval_record(**self._approval_fields(row))
                session.flush()
                replayed = {**self._view(row), "replayed": False}
        if stale:
            # A race left the body without a published predecessor: re-render it (a new
            # binding) and refuse this approval of the old content.
            self.refresh_dependencies_sync([change_request_id])
            raise ValueError(
                f"{DEPENDENCIES_STALE}: the request was re-rendered with its published "
                "dependencies; approve it with its new binding"
            )
        if replayed is None:
            raise ValueError("change request approval did not complete")
        return replayed

    @classmethod
    def _dependency_gate(cls, session: Any, row: ChangeRequest) -> bool:
        """Refuse (409) a decomposition request with unpublished predecessors; True when
        its body is stale (a predecessor published without its re-render)."""
        if not row.decomposition_lineage:
            return False
        context = cls._decomposition_dict(row)
        refs, awaiting = cls._dependency_state(session, row.project, context)
        if awaiting:
            raise DependenciesAwaitingError(
                f"{DEPENDENCIES_AWAITING}: awaiting_dependencies "
                + ", ".join(awaiting)
                + " (approve after every predecessor is published)"
            )
        return refs != list(context.get("depends_on") or [])

    async def revise(self, change_request_id: UUID, note: str, actor: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.revise_sync, change_request_id, note, actor)

    def revise_sync(self, change_request_id: UUID, note: str, actor: str) -> dict[str, Any]:
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("change request revision requires an allowlisted operator channel")
        safe_note = str(SecretRedactor().redact_text(note.strip()).value)
        if not 3 <= len(safe_note) <= 2000:
            raise ValueError("revision note must be 3 to 2000 characters")
        try:
            return self._revise(change_request_id, safe_note, actor)
        except IntegrityError as error:
            # NEW-234 PR6 review: a FAILED decomposition request whose lineage another
            # active request took meanwhile (migration 0023's index).
            raise ValueError(
                f"{SUPERSEDED_BY_EXISTING}: another active request now holds this task"
            ) from error

    def _revise(self, change_request_id: UUID, safe_note: str, actor: str) -> dict[str, Any]:
        with self.sessions.begin() as session:
            row = self._locked(session, change_request_id)
            if not self._decision_open(row, {"CANDIDATE", "FAILED"}):
                raise ValueError(
                    "only a CANDIDATE, FAILED or unpublished APPROVED change request can be revised"
                )
            if row.status == "FAILED" and self._lineage_taken(session, row):
                raise ValueError(
                    f"{SUPERSEDED_BY_EXISTING}: another active request now holds this task"
                )
            now = datetime.now(UTC)
            row.status = "DRAFTING"
            row.revision += 1
            row.revision_note = safe_note
            # The previous proposal stays as drafting context for the next revision.
            row.review_json = {}
            row.dedupe_json = {}
            row.fingerprint = None
            row.issue_title = None
            row.issue_body = None
            row.approval_binding_sha256 = None
            row.error_code = None
            row.approval_snapshot_json = None
            row.decision_sequence += 1
            row.decided_by = actor
            row.decided_at = now
            row.decision_reason = None
            session.flush()
            return self._view(row)

    async def reject(self, change_request_id: UUID, reason: str, actor: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.reject_sync, change_request_id, reason, actor)

    def reject_sync(self, change_request_id: UUID, reason: str, actor: str) -> dict[str, Any]:
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("change request rejection requires an allowlisted operator channel")
        safe_reason = str(SecretRedactor().redact_text(reason.strip()).value)
        if not 3 <= len(safe_reason) <= 1000:
            raise ValueError("rejection reason must be 3 to 1000 characters")
        with self.sessions.begin() as session:
            row = self._locked(session, change_request_id)
            if not self._decision_open(row, {"DRAFTING", "CANDIDATE", "FAILED"}):
                raise ValueError("change request cannot be rejected from its current state")
            row.status = "REJECTED"
            row.decided_by = actor
            row.decided_at = datetime.now(UTC)
            row.decision_reason = safe_reason
            row.decision_sequence += 1
            session.flush()
            return self._view(row)

    async def mark_published(
        self,
        change_request_id: UUID,
        issue_url: str,
        binding: str,
        decision_sequence: int,
        actor: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.mark_published_sync,
            change_request_id,
            issue_url,
            binding,
            decision_sequence,
            actor,
        )

    def mark_published_sync(
        self,
        change_request_id: UUID,
        issue_url: str,
        binding: str,
        decision_sequence: int,
        actor: str,
    ) -> dict[str, Any]:
        """Record the issue the operator published with their own gh login (final state).

        Idempotent for the same URL; bound to the approved content and decision.
        """
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("change request publication requires an allowlisted operator")
        match = _ISSUE_URL.fullmatch(issue_url)
        if match is None:
            raise ValueError("published issue reference is invalid")
        with self.sessions.begin() as session:
            row = self._locked(session, change_request_id)
            stored = row.approval_binding_sha256 or ""
            if not stored or not hmac.compare_digest(stored, binding):
                raise ValueError("change request approval binding changed")
            if row.decision_sequence != decision_sequence:
                raise ValueError("change request decision changed since approval")
            if f"gntik-ai/{match.group(1)}" != row.target_repository:
                raise ValueError("published issue is outside the approved repository")
            if row.status == "PUBLISHED":
                if row.issue_url != issue_url:
                    raise ValueError("change request was already published elsewhere")
                return {**self._view(row), "replayed": True}
            if row.status != "APPROVED":
                raise ValueError("only an APPROVED change request can be published")
            self._verify_stored_content(row)
            if not approval_record_matches(
                row.approval_snapshot_json, **self._approval_fields(row)
            ):
                raise ValueError("change request content no longer matches its approval record")
            row.status = "PUBLISHED"
            row.issue_url = issue_url
            row.issue_number = int(match.group(2))
            row.published_at = datetime.now(UTC)
            row.error_code = None
            session.flush()
            published = {**self._view(row), "replayed": False}
            dependents = self._dependents(session, row)
        if not dependents:
            return published
        # NEW-234 PR6 review: the publication is committed first (the operator's issue
        # exists; it is never rolled back over a re-render). Each dependent is then
        # re-rendered with `Depends on: gntik-ai/<repo>#N` in its own transaction under
        # the global lock order. A failed re-render is deferred, never lost: staleness is
        # recomputed from the published predecessors, so approval refuses the old body
        # (and re-renders it), and backlog-decomposition-ready repairs it before listing.
        try:
            rerendered = self.refresh_dependencies_sync(dependents)
        except Exception:  # noqa: BLE001 - deferred; see above
            LOGGER.warning(
                "change request %s published; dependent re-render deferred", change_request_id
            )
            return {**published, "rerender_deferred": [str(item) for item in dependents]}
        return {**published, **({"rerendered_dependents": rerendered} if rerendered else {})}

    async def record_start_failure(self, change_request_id: UUID, revision: int) -> None:
        await asyncio.to_thread(
            self.record_failure_sync, change_request_id, revision, "WORKFLOW_START_FAILED"
        )

    # Controller activities ----------------------------------------------------------------

    def load_draft_sync(self, change_request_id: UUID, revision: int) -> dict[str, Any]:
        with self.sessions() as session:
            row = session.get(ChangeRequest, change_request_id)
            if row is None:
                raise ChangeRequestNotFoundError("change request not found")
            if row.status != "DRAFTING" or row.revision != revision:
                return {"status": "SUPERSEDED", "current_status": row.status}
            return {
                "status": "DRAFTING",
                "project": row.project,
                "target_repository": row.target_repository,
                "description_redacted": row.description_redacted,
                "revision_note": row.revision_note,
                "previous_proposal": row.proposal_json if row.revision > 1 else None,
                # NEW-123 review 2: the continued sections are part of a UI item's proposal
                # (retained verbatim in its issue body), so revisions draft and review them.
                "ui_continued": _ui_continued(row.ui_context_json),
            }

    def persist_candidate_sync(
        self,
        change_request_id: UUID,
        revision: int,
        proposal: ChangeProposal,
        review: ChangeReviewRecord,
        dedupe: dict[str, Any],
        *,
        fanout_writer: str | None = None,
    ) -> dict[str, Any]:
        with self.sessions.begin() as session:
            if not self._fanout_fence(session, change_request_id, revision, fanout_writer):
                return self._fanout_superseded(session, change_request_id)
            # NEW-234 PR6: the request and every request of its predecessor lineages, in
            # the one ordered lock of this transaction (the global lock order); a
            # predecessor published meanwhile is either seen here or re-renders this
            # candidate after both commit.
            row = self._lock_requests(
                session, [change_request_id, *self._predecessor_ids(session, [change_request_id])]
            ).get(change_request_id)
            if row is None:
                raise ChangeRequestNotFoundError("change request not found")
            if row.status != "DRAFTING" or row.revision != revision:
                return {"status": "SUPERSEDED", "current_status": row.status}
            validate_proposal(proposal.model_dump(mode="json"), row.project, row.target_repository)
            if row.decomposition_lineage:
                context = self._decomposition_dict(row)
                refs, _awaiting = self._dependency_state(session, row.project, context)
                row.decomposition_context_json = DecompositionChangeContext.model_validate(
                    {**context, "depends_on": refs}
                ).model_dump(mode="json")
            rendered = render_change_request(
                change_request_id=str(row.id),
                revision=row.revision,
                project=row.project,
                target_repository=row.target_repository,
                proposal=proposal,
                review=review,
                description_redacted=row.description_redacted,
                ui_context=self._ui_context(row),
                decomposition_context=self._decomposition_context(row),
            )
            error = self._render_error(rendered)
            if error is not None:
                row.status = "FAILED"
                row.error_code = error
                session.flush()
                return {"status": "FAILED", "error_code": error}
            row.proposal_json = proposal.model_dump(mode="json")
            row.review_json = review.model_dump(mode="json")
            if row.decomposition_lineage:
                # NEW-234 PR6: the import risk of the real rendered body, and any replaced
                # lineage match, are kept with the request (not part of its binding).
                kept = row.dedupe_json if isinstance(row.dedupe_json, dict) else {}
                dedupe = {
                    **dedupe,
                    **(
                        {"lineage_matches": kept["lineage_matches"]}
                        if kept.get("lineage_matches")
                        else {}
                    ),
                    "import_risk": import_risk(
                        row.target_repository,
                        proposal.kind,
                        proposal.proposed_risk,
                        rendered.title,
                        rendered.body,
                    ),
                }
            row.dedupe_json = dedupe
            row.fingerprint = rendered.fingerprint
            row.issue_title = rendered.title
            row.issue_body = rendered.body
            row.approval_binding_sha256 = rendered.approval_binding
            row.error_code = None
            row.status = "CANDIDATE"
            session.flush()
            return {
                "status": "CANDIDATE",
                "fingerprint": rendered.fingerprint,
                "approval_binding": rendered.approval_binding,
                "review_decision": review.decision,
            }

    def record_failure_sync(
        self,
        change_request_id: UUID,
        revision: int,
        error_code: str,
        *,
        phase: str | None = None,
        blocker: str | None = None,
        detail: str | None = None,
        fanout_writer: str | None = None,
    ) -> dict[str, Any]:
        code = error_code if _ERROR_CODE.fullmatch(error_code) else "CHANGE_REQUEST_FAILED"
        # NEW-146: what failed, kept with the request (review_json is empty until a
        # candidate is persisted, and every redraft clears it): the step's own blocker and
        # its bounded, redacted message.
        failure: dict[str, Any] = {}
        if phase:
            failure["phase"] = bounded_detail(phase, limit=40)
        if blocker:
            failure["blocker"] = bounded_detail(blocker, limit=120)
        if detail:
            failure["detail"] = bounded_detail(detail)
        with self.sessions.begin() as session:
            if not self._fanout_fence(session, change_request_id, revision, fanout_writer):
                return self._fanout_superseded(session, change_request_id)
            row = self._locked(session, change_request_id)
            if row.status != "DRAFTING" or row.revision != revision:
                return {"status": "SUPERSEDED", "current_status": row.status}
            row.status = "FAILED"
            row.error_code = code
            if failure:
                row.review_json = {"failure": failure}
            session.flush()
            return {
                "status": "FAILED",
                "error_code": code,
                **({"failure": failure} if failure else {}),
            }

    def item_failures_sync(self, change_request_ids: list[UUID]) -> list[dict[str, Any]]:
        """NEW-146: the FAILED fanned-out items among these ids, in the given order, with
        their recorded failure."""
        if not change_request_ids:
            return []
        with self.sessions() as session:
            rows = {
                row.id: row
                for row in session.scalars(
                    select(ChangeRequest).where(
                        ChangeRequest.id.in_(change_request_ids),
                        ChangeRequest.status == "FAILED",
                    )
                ).all()
            }
            failures = []
            for change_request_id in change_request_ids:
                row = rows.get(change_request_id)
                if row is None:
                    continue
                context = row.ui_context_json if isinstance(row.ui_context_json, dict) else {}
                failures.append(
                    {
                        "change_request_id": str(row.id),
                        # NEW-234 PR6: a decomposition task is named by its task key.
                        "item_key": context.get("item_key")
                        or self._decomposition_dict(row).get("task_key"),
                        "revision": row.revision,
                        "error_code": row.error_code,
                        **self._failure(row),
                    }
                )
            return failures

    # NEW-234 PR6: decomposition dependency gating ---------------------------------------

    def refresh_dependencies_sync(self, change_request_ids: list[UUID]) -> list[str]:
        """Re-render every CANDIDATE among these decomposition requests whose body does not
        name exactly its published predecessors (deterministic and idempotent: the repair
        of a publication that raced the candidate's persist). Returns the re-rendered ids."""
        if not change_request_ids:
            return []
        rerendered: list[str] = []
        with self.sessions.begin() as session:
            # The requests and all their predecessors in the one ordered lock.
            locked = self._lock_requests(
                session,
                [*change_request_ids, *self._predecessor_ids(session, change_request_ids)],
            )
            for identity in sorted(set(change_request_ids)):
                row = locked.get(identity)
                if row is None or row.status != "CANDIDATE" or not row.decomposition_lineage:
                    continue
                context = self._decomposition_dict(row)
                refs, _awaiting = self._dependency_state(session, row.project, context)
                if refs != list(context.get("depends_on") or []):
                    self._rerender_dependencies(row, refs)
                    rerendered.append(str(row.id))
            session.flush()
        return rerendered

    @staticmethod
    def _decomposition_dict(row: ChangeRequest) -> dict[str, Any]:
        value = row.decomposition_context_json
        return dict(value) if isinstance(value, dict) else {}

    @classmethod
    def _dependency_state(
        cls, session: Any, project: str, context: dict[str, Any]
    ) -> tuple[list[str], list[str]]:
        """(the published predecessors' issue references, in task order; the predecessor
        task keys not yet published). A predecessor is any request of its lineage, so a
        task superseded by an earlier plan's request waits for that request."""
        keys = [str(key) for key in context.get("depends_on_tasks") or []]
        if not keys:
            return [], []
        lineages = {lineage_of(project, key): key for key in keys}
        rows = session.scalars(
            select(ChangeRequest).where(ChangeRequest.decomposition_lineage.in_(sorted(lineages)))
        ).all()
        published: dict[str, ChangeRequest] = {}
        for item in sorted(
            rows, key=lambda value: (cls._iso(value.published_at) or "", str(value.id))
        ):
            lineage = str(item.decomposition_lineage)
            if item.status == "PUBLISHED" and item.issue_number and lineage not in published:
                published[lineage] = item
        refs: list[str] = []
        awaiting: list[str] = []
        for lineage, key in lineages.items():
            found = published.get(lineage)
            if found is None:
                awaiting.append(key)
            else:
                refs.append(f"{found.target_repository}#{found.issue_number}")
        return list(dict.fromkeys(refs)), awaiting

    @staticmethod
    def _lock_requests(session: Any, identities: Iterable[UUID]) -> dict[UUID, ChangeRequest]:
        """NEW-234 PR6 review: the global lock order. A transaction locks change requests
        once: every row it may change or whose state it depends on, in one
        SELECT ... FOR UPDATE ORDER BY id, after any blueprint or decomposition row (the
        fan-out fences) and before nothing else. Two such transactions acquire their
        common rows in the same order and none waits for a row while holding a later one,
        so the decomposition paths (persist, re-render, publication, approval, bulk
        approval, retry) cannot deadlock. A second lock in one transaction is refused."""
        if session.info.get(_REQUEST_LOCKS):
            raise RuntimeError("a transaction locks its change requests once (global lock order)")
        session.info[_REQUEST_LOCKS] = True
        ordered = sorted(set(identities))
        if not ordered:
            return {}
        rows = session.scalars(
            select(ChangeRequest)
            .where(ChangeRequest.id.in_(ordered))
            .order_by(ChangeRequest.id)
            .with_for_update()
        ).all()
        return {row.id: row for row in rows}

    @staticmethod
    def _predecessor_ids(session: Any, change_request_ids: Iterable[UUID]) -> list[UUID]:
        """Every request of these requests' predecessor lineages (read, not locked)."""
        lineages: set[str] = set()
        for project, context in session.execute(
            select(ChangeRequest.project, ChangeRequest.decomposition_context_json).where(
                ChangeRequest.id.in_(list(change_request_ids))
            )
        ).all():
            if isinstance(context, dict):
                lineages.update(
                    lineage_of(str(project), str(key))
                    for key in context.get("depends_on_tasks") or []
                )
        if not lineages:
            return []
        return list(
            session.scalars(
                select(ChangeRequest.id).where(
                    ChangeRequest.decomposition_lineage.in_(sorted(lineages))
                )
            ).all()
        )

    @staticmethod
    def _dependents(session: Any, published: ChangeRequest) -> list[UUID]:
        """The CANDIDATE requests whose predecessor tasks include this one's lineage."""
        lineage = published.decomposition_lineage
        if not lineage:
            return []
        candidates = session.execute(
            select(ChangeRequest.id, ChangeRequest.decomposition_context_json).where(
                ChangeRequest.project == published.project,
                ChangeRequest.decomposition_lineage.is_not(None),
                ChangeRequest.status == "CANDIDATE",
            )
        ).all()
        return sorted(
            identity
            for identity, context in candidates
            if isinstance(context, dict)
            and any(
                lineage_of(published.project, str(key)) == lineage
                for key in context.get("depends_on_tasks") or []
            )
        )

    @staticmethod
    def _lineage_taken(session: Any, row: ChangeRequest) -> bool:
        """Another active request holds this decomposition request's lineage."""
        if not row.decomposition_lineage:
            return False
        return (
            session.scalar(
                select(ChangeRequest.id)
                .where(
                    ChangeRequest.decomposition_lineage == row.decomposition_lineage,
                    ChangeRequest.id != row.id,
                    ChangeRequest.status.in_(sorted(LINEAGE_BLOCKING_STATUSES)),
                )
                .limit(1)
            )
            is not None
        )

    @classmethod
    def _rerender_dependencies(cls, row: ChangeRequest, refs: list[str]) -> None:
        """Re-render a CANDIDATE decomposition request with these predecessor issues: a new
        body and binding (the operator approves the final content, never the old one)."""
        context = DecompositionChangeContext.model_validate(
            {**cls._decomposition_dict(row), "depends_on": refs}
        )
        proposal = validate_proposal(row.proposal_json, row.project, row.target_repository)
        rendered = render_change_request(
            change_request_id=str(row.id),
            revision=row.revision,
            project=row.project,
            target_repository=row.target_repository,
            proposal=proposal,
            review=ChangeReviewRecord.model_validate(row.review_json),
            description_redacted=row.description_redacted,
            decomposition_context=context,
        )
        row.decomposition_context_json = context.model_dump(mode="json")
        error = cls._render_error(rendered)
        if error is not None:
            row.status = "FAILED"
            row.error_code = error
            row.approval_binding_sha256 = None
            return
        row.fingerprint = rendered.fingerprint
        row.issue_title = rendered.title
        row.issue_body = rendered.body
        row.approval_binding_sha256 = rendered.approval_binding
        dedupe = row.dedupe_json if isinstance(row.dedupe_json, dict) else {}
        row.dedupe_json = {
            **dedupe,
            "import_risk": import_risk(
                row.target_repository,
                proposal.kind,
                proposal.proposed_risk,
                rendered.title,
                rendered.body,
            ),
        }
        row.updated_at = datetime.now(UTC)

    # NEW-146 review 1: the fan-out ownership fence -----------------------------------------

    @classmethod
    def _fanout_fence(
        cls,
        session: Any,
        change_request_id: UUID,
        revision: int,
        fanout_writer: str | None,
    ) -> bool:
        """May this writer change the request? Checked in the write's own transaction.

        Revision 1 of a fanned-out item belongs to the fan-out that holds its blueprint's
        active marker: a write from any other execution (a cancelled or timed-out activity of
        an earlier fan-out still running in its thread, after a retry reset the item) is
        refused. Callers that are not activities (``fanout_writer`` None), operator requests
        and operator revisions (revision 2 or later, ChangeRequestWorkflow) are not fenced.
        The blueprint row is locked before the request row, the order the retry uses.
        NEW-234: a fanned-out decomposition task is fenced the same way by its backlog
        decomposition's active marker; a request with neither context is not fenced."""
        if fanout_writer is None or revision != 1:
            return True
        context, decomposition = session.execute(
            select(ChangeRequest.ui_context_json, ChangeRequest.decomposition_context_json).where(
                ChangeRequest.id == change_request_id
            )
        ).one_or_none() or (None, None)
        blueprint_id = context.get("blueprint_id") if isinstance(context, dict) else None
        if blueprint_id:
            return cls._fanout_owns(session, str(blueprint_id), fanout_writer)
        decomposition_id = (
            decomposition.get("decomposition_id") if isinstance(decomposition, dict) else None
        )
        if decomposition_id:
            return cls._decomposition_fanout_owns(session, str(decomposition_id), fanout_writer)
        return True

    @staticmethod
    def _fanout_owns(session: Any, blueprint_id: str, fanout_writer: str) -> bool:
        try:
            identity = UUID(blueprint_id)
        except ValueError:
            return False
        marker = session.scalar(
            select(UiBlueprint.active_fanout_workflow_id)
            .where(UiBlueprint.id == identity)
            .with_for_update()
        )
        return bool(fanout_writer) and marker == fanout_writer

    @staticmethod
    def _decomposition_fanout_owns(session: Any, decomposition_id: str, fanout_writer: str) -> bool:
        try:
            identity = UUID(decomposition_id)
        except ValueError:
            return False
        marker = session.scalar(
            select(BacklogDecomposition.active_fanout_workflow_id)
            .where(BacklogDecomposition.id == identity)
            .with_for_update()
        )
        return bool(fanout_writer) and marker == fanout_writer

    @staticmethod
    def _fanout_superseded(session: Any, change_request_id: UUID) -> dict[str, Any]:
        status, decomposition = session.execute(
            select(ChangeRequest.status, ChangeRequest.decomposition_context_json).where(
                ChangeRequest.id == change_request_id
            )
        ).one_or_none() or (None, None)
        blocker = (
            DECOMPOSITION_FANOUT_SUPERSEDED
            if isinstance(decomposition, dict) and decomposition.get("decomposition_id")
            else UI_FANOUT_SUPERSEDED
        )
        return {"status": "SUPERSEDED", "current_status": status, "blocker": blocker}

    @staticmethod
    def _failure(row: ChangeRequest) -> dict[str, Any]:
        review = row.review_json if isinstance(row.review_json, dict) else {}
        failure = review.get("failure") if row.status == "FAILED" else None
        if not isinstance(failure, dict):
            return {}
        return {key: failure[key] for key in ("phase", "blocker", "detail") if key in failure}

    @classmethod
    def _approval_fields(cls, row: ChangeRequest) -> dict[str, Any]:
        return {
            "project": row.project,
            "target_repository": row.target_repository,
            "proposal": row.proposal_json,
            "review": row.review_json,
            "description_redacted": row.description_redacted,
            "issue_title": row.issue_title,
            "issue_body": row.issue_body,
            "fingerprint": row.fingerprint,
            "revision": row.revision,
            "decision_sequence": row.decision_sequence,
            "decided_by": row.decided_by,
            "decided_at": cls._iso(row.decided_at),
            "approval_binding": row.approval_binding_sha256,
        }

    @staticmethod
    def _decision_open(row: ChangeRequest, statuses: set[str]) -> bool:
        """Revise/reject are open in these statuses, or while APPROVED but unpublished."""
        return row.status in statuses or (row.status == "APPROVED" and row.issue_url is None)

    def finalize_run_sync(self, workflow_id: str, status: str, error_code: str | None) -> bool:
        """Close the control Run the provider telemetry opened for this workflow."""
        if not workflow_id.startswith("change-request-"):
            return False
        with self.sessions.begin() as session:
            runs = session.scalars(select(Run).where(Run.workflow_id == workflow_id)).all()
            now = datetime.now(UTC)
            for run in runs:
                run.status = status
                run.error_code = error_code
                run.finished_at = run.finished_at or now
            return bool(runs)

    # Helpers --------------------------------------------------------------------------------

    @staticmethod
    def _locked(session: Any, change_request_id: UUID) -> ChangeRequest:
        row = ChangeRequestRepository._lock_requests(session, [change_request_id]).get(
            change_request_id
        )
        if row is None:
            raise ChangeRequestNotFoundError("change request not found")
        return row

    @staticmethod
    def rendered(row: ChangeRequest) -> RenderedChangeRequest:
        proposal = validate_proposal(row.proposal_json, row.project, row.target_repository)
        review = ChangeReviewRecord.model_validate(row.review_json)
        return render_change_request(
            change_request_id=str(row.id),
            revision=row.revision,
            project=row.project,
            target_repository=row.target_repository,
            proposal=proposal,
            review=review,
            description_redacted=row.description_redacted,
            ui_context=ChangeRequestRepository._ui_context(row),
            decomposition_context=ChangeRequestRepository._decomposition_context(row),
        )

    @staticmethod
    def _ui_context(row: ChangeRequest) -> UiChangeContext | None:
        """NEW-123: the fanned-out blueprint item, part of the rendered (bound) body."""
        value = row.ui_context_json
        return UiChangeContext.model_validate(value) if value else None

    @staticmethod
    def _decomposition_context(row: ChangeRequest) -> DecompositionChangeContext | None:
        """NEW-234: the fanned-out decomposition task, part of the rendered (bound) body."""
        value = row.decomposition_context_json
        return DecompositionChangeContext.model_validate(value) if value else None

    @classmethod
    def _verify_stored_content(cls, row: ChangeRequest) -> None:
        """Tamper check: the stored binding must be recomputable from stored content."""
        try:
            rendered = cls.rendered(row)
        except ValueError as error:
            raise ValueError("stored change request content is invalid") from error
        stored = row.approval_binding_sha256 or ""
        if (
            not hmac.compare_digest(rendered.approval_binding, stored)
            or rendered.title != row.issue_title
            or rendered.body != row.issue_body
        ):
            raise ValueError("stored change request content no longer matches its binding")

    @staticmethod
    def _render_error(rendered: RenderedChangeRequest) -> str | None:
        if len(rendered.body) > MAXIMUM_ISSUE_BODY_CHARACTERS:
            return "RENDERED_BODY_TOO_LARGE"
        # Publication re-redacts the body; it must already be stable or the binding breaks.
        if SecretRedactor().redact_text(rendered.body).value != rendered.body:
            return "RENDERED_BODY_NOT_REDACTION_STABLE"
        return None

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()

    @classmethod
    def _summary(cls, row: ChangeRequest) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "request_key": row.request_key,
            "project": row.project,
            "status": row.status,
            "revision": row.revision,
            "target_repository": row.target_repository,
            "issue_title": row.issue_title,
            "fingerprint": row.fingerprint,
            "error_code": row.error_code,
            "issue_url": row.issue_url,
            "merged_into_id": str(row.merged_into_id) if row.merged_into_id else None,
            "created_at": cls._iso(row.created_at),
            "updated_at": cls._iso(row.updated_at),
        }

    @classmethod
    def _view(cls, row: ChangeRequest) -> dict[str, Any]:
        review = row.review_json if isinstance(row.review_json, dict) else {}
        dedupe = row.dedupe_json if isinstance(row.dedupe_json, dict) else {}
        binding = row.approval_binding_sha256
        candidate = row.status == "CANDIDATE" and binding is not None
        phrase = approval_phrase(row.project, binding) if candidate and binding else None
        return {
            **cls._summary(row),
            "redacted": row.redacted,
            "description_redacted": row.description_redacted,
            "description_sha256": row.description_sha256,
            "revision_note": row.revision_note,
            "proposal": row.proposal_json,
            "review": review,
            "review_decision": review.get("decision"),
            "review_findings": review.get("unresolved_findings", []),
            "dedupe_warnings": dedupe.get("matches", []),
            "dedupe_status": dedupe.get("status"),
            "issue_body": row.issue_body,
            "approval_binding": binding,
            "confirmation_phrase": phrase,
            "approval_snapshot": row.approval_snapshot_json,
            "publish_command": (
                f"engineering-ctl change-publish {row.id}" if row.status == "APPROVED" else None
            ),
            "approve_command": (
                f"engineering-ctl change-approve {row.id} {binding} {phrase}"
                if phrase is not None
                else None
            ),
            "issue_number": row.issue_number,
            "requested_by": row.requested_by,
            "decided_by": row.decided_by,
            "decision_reason": row.decision_reason,
            "decided_at": cls._iso(row.decided_at),
            "published_at": cls._iso(row.published_at),
            "decision_sequence": row.decision_sequence,
            "workflow_id": change_request_workflow_id(str(row.id), row.revision),
            "ui_context": row.ui_context_json or None,
            # NEW-234: the decomposition task a fanned-out technical request came from.
            "decomposition_context": row.decomposition_context_json or None,
            # NEW-146: why a FAILED request failed (blocker and bounded, redacted detail).
            "failure": cls._failure(row) or None,
            # NEW-154: merge, canonical and re-pointing records of a gap consolidation.
            "consolidation": row.consolidation_json or None,
        }


def _ui_continued(context: Any) -> list[dict[str, Any]]:
    if not isinstance(context, dict):
        return []
    sections = context.get("continued")
    return (
        [dict(section) for section in sections if isinstance(section, dict)]
        if isinstance(sections, list)
        else []
    )
