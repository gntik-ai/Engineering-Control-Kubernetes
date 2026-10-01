"""NEW-108: durable operator change requests and their bound state transitions."""

from __future__ import annotations

import asyncio
import hmac
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from engineering_control.db.models import ChangeRequest
from engineering_control.engineering.backlog import (
    canonical_sha256,
    is_reauthenticated_backlog_operator,
)
from engineering_control.engineering.change_requests import (
    MAXIMUM_ISSUE_BODY_CHARACTERS,
    ChangeProposal,
    ChangeRequestIntake,
    ChangeReviewRecord,
    RenderedChangeRequest,
    approval_phrase,
    change_request_publish_workflow_id,
    change_request_workflow_id,
    render_change_request,
    validate_proposal,
)
from engineering_control.policy.change_request_attestation import (
    ATTESTATION_TTL,
    AttestationDirectory,
    ChangeRequestAttestation,
    RequestDirectory,
    request_attestation,
)
from engineering_control.security.redaction import SecretRedactor

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_ISSUE_URL = re.compile(
    r"^https://github\.com/gntik-ai/(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)"
    r"/issues/([1-9][0-9]*)$"
)


# An unresolved publication outcome must be reconciled before revise or reject.
UNRESOLVED_PUBLICATION_ERRORS = frozenset({"PUBLISH_OUTCOME_UNKNOWN", "PUBLISH_CONFLICT"})


class ChangeRequestNotFoundError(ValueError):
    """The requested change request does not exist."""


class ChangeRequestRepository:
    """Every change-request state transition, each re-checked under a row lock."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
        attestation_request_root: Path | None = None,
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
        # Where approval changes ask the root reconciler to (re)attest from durable state.
        self.attestation_request_root = attestation_request_root

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
                row.approval_snapshot_json = self._approval_snapshot(row)
                session.flush()
                replayed = {**self._view(row), "replayed": False}
            sequence = row.decision_sequence
        self._request_attestation(change_request_id, sequence)
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
                    " (an unresolved publication must be reconciled first)"
                )
            revoked = row.status == "APPROVED"
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
            # publish_attempted_at survives: any earlier revision may already be on GitHub.
            row.approval_snapshot_json = None
            row.decision_sequence += 1
            row.decided_by = actor
            row.decided_at = now
            row.decision_reason = None
            session.flush()
            view = self._view(row)
            sequence = row.decision_sequence
        if revoked:
            self._request_attestation(change_request_id, sequence)
        return view

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
                raise ValueError(
                    "change request cannot be rejected from its current state"
                    " (an unresolved publication must be reconciled first)"
                )
            revoked = row.status == "APPROVED"
            row.status = "REJECTED"
            row.decided_by = actor
            row.decided_at = datetime.now(UTC)
            row.decision_reason = safe_reason
            row.decision_sequence += 1
            session.flush()
            view = self._view(row)
            sequence = row.decision_sequence
        if revoked:
            self._request_attestation(change_request_id, sequence)
        return view

    async def reconcile_publication(
        self, change_request_id: UUID, issue_url: str | None, reason: str, actor: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.reconcile_publication_sync, change_request_id, issue_url, reason, actor
        )

    def reconcile_publication_sync(
        self, change_request_id: UUID, issue_url: str | None, reason: str, actor: str
    ) -> dict[str, Any]:
        """Operator resolution of PUBLISH_OUTCOME_UNKNOWN or PUBLISH_CONFLICT.

        With an issue URL the request becomes PUBLISHED; without one the operator states
        that no publication exists, which reopens revise/reject. The intent timestamp is
        kept either way, so any later publication still looks up conclusively.
        """
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("publication reconciliation requires an allowlisted operator")
        safe_reason = str(SecretRedactor().redact_text(reason.strip()).value)
        if not 3 <= len(safe_reason) <= 1000:
            raise ValueError("reconciliation reason must be 3 to 1000 characters")
        match = _ISSUE_URL.fullmatch(issue_url) if issue_url is not None else None
        if issue_url is not None and match is None:
            raise ValueError("reconciled issue reference is invalid")
        with self.sessions.begin() as session:
            row = self._locked(session, change_request_id)
            if row.status != "APPROVED" or row.error_code not in UNRESOLVED_PUBLICATION_ERRORS:
                raise ValueError("only an unresolved publication can be reconciled")
            if match is not None:
                if f"gntik-ai/{match.group(1)}" != row.target_repository:
                    raise ValueError("reconciled issue is outside the approved repository")
                row.status = "PUBLISHED"
                row.issue_url = issue_url
                row.issue_number = int(match.group(2))
                row.published_at = datetime.now(UTC)
            row.error_code = None
            row.decided_by = actor
            row.decided_at = datetime.now(UTC)
            row.decision_reason = f"publication reconciled: {safe_reason}"
            row.decision_sequence += 1
            session.flush()
            view = self._view(row)
            sequence = row.decision_sequence
        self._request_attestation(change_request_id, sequence)
        return view

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
            validate_proposal(proposal.model_dump(mode="json"), row.project)
            rendered = render_change_request(
                change_request_id=str(row.id),
                revision=row.revision,
                project=row.project,
                target_repository=row.target_repository,
                proposal=proposal,
                review=review,
                description_redacted=row.description_redacted,
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
        self, change_request_id: UUID, revision: int, error_code: str
    ) -> dict[str, Any]:
        code = error_code if _ERROR_CODE.fullmatch(error_code) else "CHANGE_REQUEST_FAILED"
        with self.sessions.begin() as session:
            row = self._locked(session, change_request_id)
            if row.status != "DRAFTING" or row.revision != revision:
                return {"status": "SUPERSEDED", "current_status": row.status}
            row.status = "FAILED"
            row.error_code = code
            session.flush()
            return {"status": "FAILED", "error_code": code}

    def load_approved_sync(self, change_request_id: UUID, binding: str) -> dict[str, Any]:
        with self.sessions() as session:
            row = session.get(ChangeRequest, change_request_id)
            if row is None:
                raise ChangeRequestNotFoundError("change request not found")
            stored = row.approval_binding_sha256 or ""
            if not stored or not hmac.compare_digest(stored, binding):
                return {"status": "BLOCKED", "blocker": "APPROVAL_BINDING_MISMATCH"}
            if row.status == "PUBLISHED":
                return {
                    "status": "PUBLISHED",
                    "issue_url": row.issue_url,
                    "issue_number": row.issue_number,
                }
            if row.status != "APPROVED":
                return {"status": "BLOCKED", "blocker": "CHANGE_REQUEST_NOT_APPROVED"}
            try:
                self._verify_stored_content(row)
            except ValueError:
                return {"status": "BLOCKED", "blocker": "CHANGE_REQUEST_CONTENT_CHANGED"}
            # No content travels to the GitHub worker: it publishes only what the root
            # reconciler attested from this same durable row.
            return {
                "status": "APPROVED",
                "revision": row.revision,
                "decision_sequence": row.decision_sequence,
            }

    def record_publish_intent_sync(
        self, change_request_id: UUID, binding: str, revision: int, decision_sequence: int
    ) -> dict[str, Any]:
        """Durably record, before any create, that a publication may have started.

        Returns the earliest intent ever recorded for the request (any revision); the
        GitHub worker always looks up conclusively back past it before creating.
        """
        with self.sessions.begin() as session:
            row = self._locked(session, change_request_id)
            stored = row.approval_binding_sha256 or ""
            if (
                row.status != "APPROVED"
                or row.revision != revision
                or row.decision_sequence != decision_sequence
                or not stored
                or not hmac.compare_digest(stored, binding)
            ):
                return {"status": "BLOCKED", "blocker": "CHANGE_REQUEST_NOT_APPROVED"}
            if row.publish_attempted_at is None:
                row.publish_attempted_at = datetime.now(UTC)
                session.flush()
            return {
                "status": "INTENT_RECORDED",
                "publish_intent_at": self._iso(row.publish_attempted_at),
            }

    def persist_publication_sync(
        self, change_request_id: UUID, binding: str, issue_url: str, issue_number: int
    ) -> dict[str, Any]:
        match = _ISSUE_URL.fullmatch(issue_url)
        if match is None or int(match.group(2)) != issue_number:
            raise ValueError("published issue reference is invalid")
        with self.sessions.begin() as session:
            row = self._locked(session, change_request_id)
            stored = row.approval_binding_sha256 or ""
            if not stored or not hmac.compare_digest(stored, binding):
                raise ValueError("change request approval binding changed")
            if f"gntik-ai/{match.group(1)}" != row.target_repository:
                raise ValueError("published issue is outside the approved repository")
            if row.status == "PUBLISHED":
                if row.issue_url != issue_url:
                    raise ValueError("change request was already published elsewhere")
                return {"status": "PUBLISHED", "issue_url": row.issue_url}
            if row.status != "APPROVED":
                raise ValueError("only an APPROVED change request can be published")
            row.status = "PUBLISHED"
            row.issue_url = issue_url
            row.issue_number = issue_number
            row.published_at = datetime.now(UTC)
            row.error_code = None
            row.decision_sequence += 1
            session.flush()
            sequence = row.decision_sequence
        self._request_attestation(change_request_id, sequence)
        return {"status": "PUBLISHED", "issue_url": issue_url, "issue_number": issue_number}

    def record_publish_failure_sync(
        self, change_request_id: UUID, binding: str, error_code: str
    ) -> dict[str, Any]:
        """Keep the row APPROVED (re-approval replays the publish) but surface the cause."""
        code = error_code if _ERROR_CODE.fullmatch(error_code) else "PUBLISH_FAILED"
        with self.sessions.begin() as session:
            row = self._locked(session, change_request_id)
            stored = row.approval_binding_sha256 or ""
            if row.status != "APPROVED" or not hmac.compare_digest(stored, binding):
                return {"status": "SUPERSEDED", "current_status": row.status}
            row.error_code = code
            session.flush()
            return {"status": "PUBLISH_FAILED", "error_code": code}

    # Root attestation reconciler ------------------------------------------------------------

    def attestation_sync(
        self, change_request_id: UUID, *, now: datetime
    ) -> ChangeRequestAttestation | None:
        """An attestation only for an APPROVED, unpublished row that still matches the
        decision record written at approval time (defense in depth against row rewrites)."""
        with self.sessions() as session:
            row = session.get(ChangeRequest, change_request_id)
            if (
                row is None
                or row.status != "APPROVED"
                or row.issue_url is not None
                or row.decided_at is None
                or not isinstance(row.decided_by, str)
                or not is_reauthenticated_backlog_operator(row.decided_by)
            ):
                return None
            try:
                self._verify_stored_content(row)
            except ValueError:
                return None
            snapshot = row.approval_snapshot_json
            if not isinstance(snapshot, dict) or snapshot != self._approval_snapshot(row):
                return None
            return ChangeRequestAttestation(
                change_request_id=str(row.id),
                revision=row.revision,
                decision_sequence=row.decision_sequence,
                project=row.project,  # type: ignore[arg-type]
                target_repository=row.target_repository,
                approval_binding=str(row.approval_binding_sha256),
                fingerprint=str(row.fingerprint),
                proposal=row.proposal_json,
                review=row.review_json,
                description_redacted=row.description_redacted,
                issue_title=str(row.issue_title),
                issue_body=str(row.issue_body),
                approved_by=row.decided_by,
                approved_at=row.decided_at,
                attested_at=now,
                expires_at=now + ATTESTATION_TTL,
            )

    def reconcile_attestations_sync(
        self,
        request_root: Path,
        attestation_root: Path,
        *,
        now: datetime,
        request_owner: int | None = None,
        group: int | None = None,
        strict: bool = False,
    ) -> dict[str, Any]:
        """Run as root: (re)write or revoke every requested or existing attestation.

        A decision committed while this runs is never lost: an attestation is re-checked
        after it is written, and a request is consumed only if unchanged since it was read.
        """
        attested: list[str] = []
        revoked: list[str] = []
        retained: list[str] = []
        with (
            RequestDirectory(
                request_root, owner=request_owner, group=group, strict=strict
            ) as requests,
            AttestationDirectory(attestation_root, group=group, strict=strict) as attestations,
        ):
            pending, quarantined = requests.scan()
            identifiers = {item.change_request_id for item in pending}
            identifiers.update(attestations.identifiers())
            for identifier in sorted(identifiers, key=str):
                attestation = self.attestation_sync(identifier, now=now)
                if attestation is None:
                    if attestations.revoke(identifier):
                        revoked.append(str(identifier))
                    continue
                attestations.write(attestation)
                current = self.attestation_sync(identifier, now=now)
                if current is None or (
                    current.decision_sequence,
                    current.revision,
                    current.approval_binding,
                ) != (
                    attestation.decision_sequence,
                    attestation.revision,
                    attestation.approval_binding,
                ):
                    attestations.revoke(identifier)
                    revoked.append(str(identifier))
                    continue
                attested.append(str(identifier))
            for request in pending:
                if not requests.consume(request):
                    retained.append(request.name)
        return {
            "attested": attested,
            "revoked": revoked,
            "quarantined": quarantined,
            "retained_requests": retained,
        }

    def _request_attestation(self, change_request_id: UUID, decision_sequence: int) -> None:
        if self.attestation_request_root is not None:
            request_attestation(
                self.attestation_request_root,
                change_request_id,
                decision_sequence=decision_sequence,
                now=datetime.now(UTC),
            )

    @classmethod
    def _approval_snapshot(cls, row: ChangeRequest) -> dict[str, Any]:
        """The approval decision record and a hash of exactly the approved content."""
        return {
            "revision": row.revision,
            "decision_sequence": row.decision_sequence,
            "decided_by": row.decided_by,
            "decided_at": cls._iso(row.decided_at),
            "approval_binding": row.approval_binding_sha256,
            "content_sha256": canonical_sha256(
                {
                    "project": row.project,
                    "target_repository": row.target_repository,
                    "proposal": row.proposal_json,
                    "review": row.review_json,
                    "description_redacted": row.description_redacted,
                    "issue_title": row.issue_title,
                    "issue_body": row.issue_body,
                    "fingerprint": row.fingerprint,
                }
            ),
        }

    @staticmethod
    def _decision_open(row: ChangeRequest, statuses: set[str]) -> bool:
        """Revise/reject are open in these statuses, or while APPROVED but unpublished."""
        if row.status == "APPROVED":
            return row.issue_url is None and row.error_code not in UNRESOLVED_PUBLICATION_ERRORS
        return row.status in statuses

    # Helpers --------------------------------------------------------------------------------

    @staticmethod
    def _locked(session: Any, change_request_id: UUID) -> ChangeRequest:
        row = session.get(ChangeRequest, change_request_id, with_for_update=True)
        if row is None:
            raise ChangeRequestNotFoundError("change request not found")
        return row  # type: ignore[no-any-return]

    @staticmethod
    def rendered(row: ChangeRequest) -> RenderedChangeRequest:
        proposal = validate_proposal(row.proposal_json, row.project)
        review = ChangeReviewRecord.model_validate(row.review_json)
        return render_change_request(
            change_request_id=str(row.id),
            revision=row.revision,
            project=row.project,
            target_repository=row.target_repository,
            proposal=proposal,
            review=review,
            description_redacted=row.description_redacted,
        )

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
            "publish_attempted_at": cls._iso(row.publish_attempted_at),
            "decision_sequence": row.decision_sequence,
            "workflow_id": change_request_workflow_id(str(row.id), row.revision),
            "publish_workflow_id": change_request_publish_workflow_id(str(row.id), row.revision),
        }
