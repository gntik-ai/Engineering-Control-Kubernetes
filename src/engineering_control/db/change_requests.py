"""NEW-108: durable operator change requests and their bound state transitions."""

from __future__ import annotations

import asyncio
import hmac
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from engineering_control.db.models import ChangeRequest, Run
from engineering_control.engineering.backlog import (
    is_reauthenticated_backlog_operator,
)
from engineering_control.engineering.change_requests import (
    MAXIMUM_ISSUE_BODY_CHARACTERS,
    ChangeProposal,
    ChangeRequestIntake,
    ChangeReviewRecord,
    RenderedChangeRequest,
    UiChangeContext,
    approval_phrase,
    approval_record,
    approval_record_matches,
    change_request_workflow_id,
    render_change_request,
    validate_proposal,
)
from engineering_control.security.redaction import SecretRedactor, bounded_detail

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_ISSUE_URL = re.compile(
    r"^https://github\.com/gntik-ai/"
    r"(falcone|falcone-charts|musematic|musematic-deploy|llmwiki|gntik-ui)"
    r"/issues/([1-9][0-9]*)$"
)


class ChangeRequestNotFoundError(ValueError):
    """The requested change request does not exist."""


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
    ) -> dict[str, Any]:
        """NEW-123: one fanned-out blueprint item, idempotent under its deterministic id.

        A repeated (resumed) fan-out finds the existing row and never creates a second one;
        a row under the same id for a different item is refused."""
        if not is_reauthenticated_backlog_operator(requested_by):
            raise ValueError("change requests require an allowlisted operator channel")
        context = ui_context.model_dump(mode="json")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(ChangeRequest, change_request_id, with_for_update=True)
            if row is not None:
                stored = row.ui_context_json if isinstance(row.ui_context_json, dict) else {}
                if (
                    stored.get("blueprint_id") != context["blueprint_id"]
                    or stored.get("item_key") != context["item_key"]
                ):
                    raise ValueError("change request id is bound to another blueprint item")
                return {**self._summary(row), "created": False}
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
            return self._view(row) if row is not None else None

    async def approve(
        self, change_request_id: UUID, binding: str, phrase: str, actor: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.approve_sync, change_request_id, binding, phrase, actor)

    def approve_sync(
        self, change_request_id: UUID, binding: str, phrase: str, actor: str
    ) -> dict[str, Any]:
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("change request approval requires an allowlisted operator channel")
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
        return replayed

    async def revise(self, change_request_id: UUID, note: str, actor: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.revise_sync, change_request_id, note, actor)

    def revise_sync(self, change_request_id: UUID, note: str, actor: str) -> dict[str, Any]:
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("change request revision requires an allowlisted operator channel")
        safe_note = str(SecretRedactor().redact_text(note.strip()).value)
        if not 3 <= len(safe_note) <= 2000:
            raise ValueError("revision note must be 3 to 2000 characters")
        with self.sessions.begin() as session:
            row = self._locked(session, change_request_id)
            if not self._decision_open(row, {"CANDIDATE", "FAILED"}):
                raise ValueError(
                    "only a CANDIDATE, FAILED or unpublished APPROVED change request can be revised"
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
            return {**self._view(row), "replayed": False}

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
    ) -> dict[str, Any]:
        with self.sessions.begin() as session:
            row = self._locked(session, change_request_id)
            if row.status != "DRAFTING" or row.revision != revision:
                return {"status": "SUPERSEDED", "current_status": row.status}
            validate_proposal(proposal.model_dump(mode="json"), row.project, row.target_repository)
            rendered = render_change_request(
                change_request_id=str(row.id),
                revision=row.revision,
                project=row.project,
                target_repository=row.target_repository,
                proposal=proposal,
                review=review,
                description_redacted=row.description_redacted,
                ui_context=self._ui_context(row),
            )
            error = self._render_error(rendered)
            if error is not None:
                row.status = "FAILED"
                row.error_code = error
                session.flush()
                return {"status": "FAILED", "error_code": error}
            row.proposal_json = proposal.model_dump(mode="json")
            row.review_json = review.model_dump(mode="json")
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
                        "item_key": context.get("item_key"),
                        "revision": row.revision,
                        "error_code": row.error_code,
                        **self._failure(row),
                    }
                )
            return failures

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
        row = session.get(ChangeRequest, change_request_id, with_for_update=True)
        if row is None:
            raise ChangeRequestNotFoundError("change request not found")
        return row  # type: ignore[no-any-return]

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
        )

    @staticmethod
    def _ui_context(row: ChangeRequest) -> UiChangeContext | None:
        """NEW-123: the fanned-out blueprint item, part of the rendered (bound) body."""
        value = row.ui_context_json
        return UiChangeContext.model_validate(value) if value else None

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
            # NEW-146: why a FAILED request failed (blocker and bounded, redacted detail).
            "failure": cls._failure(row) or None,
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
