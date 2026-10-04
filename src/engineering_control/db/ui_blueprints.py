"""NEW-123: durable UI migration blueprints, their candidate binding and approval."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from datetime import UTC, datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.models import Run, UiBlueprint
from engineering_control.db.runtime_binding import new_bound_run
from engineering_control.engineering.backlog import is_reauthenticated_backlog_operator
from engineering_control.engineering.ui_blueprint import (
    APPROVAL_PHRASE,
    UI_PLAN_PROJECTS,
    approval_binding,
    blueprint_sha256,
    render_markdown,
    ui_fanout_workflow_id,
    ui_plan_workflow_id,
)

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_SHA = re.compile(r"^[a-f0-9]{40}$")
_RUN_PREFIXES = ("ui-plan-", "ui-blueprint-fanout-")


class UiBlueprintNotFoundError(ValueError):
    """The requested blueprint does not exist."""


class UiBlueprintRepository:
    """Every blueprint state transition, each re-checked under a row lock."""

    def __init__(self, database_url: str | None = None, *, engine: Engine | None = None) -> None:
        if engine is None:
            if database_url is None:
                raise ValueError("ui blueprint repository requires a database")
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

    async def create(
        self, project: str, requested_by: str, source_sha: str | None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.create_sync, project, requested_by, source_sha)

    def create_sync(
        self, project: str, requested_by: str, source_sha: str | None
    ) -> dict[str, Any]:
        if not is_reauthenticated_backlog_operator(requested_by):
            raise ValueError("ui plans require an allowlisted operator channel")
        if project not in UI_PLAN_PROJECTS:
            raise ValueError("ui migration planning is configured only for falcone")
        if source_sha is not None and not _SHA.fullmatch(source_sha):
            raise ValueError("source ref must be a full 40-character commit SHA")
        identity = uuid4()
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = UiBlueprint(
                id=identity,
                project=project,
                status="DRAFTING",
                workflow_id=ui_plan_workflow_id(project, str(identity)),
                requested_by=requested_by,
                requested_source_sha=source_sha,
                inventory_summary_json={},
                review_json={},
                evidence_json={},
                blockers_json=[],
                fanout_json={},
                drafts_json=[],
                fanout_sequence=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._view(row)

    async def get(self, blueprint_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_sync, blueprint_id)

    def get_sync(self, blueprint_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            return self._view(row) if row is not None else None

    async def markdown(self, blueprint_id: UUID) -> str | None:
        return await asyncio.to_thread(self.markdown_sync, blueprint_id)

    def markdown_sync(self, blueprint_id: UUID) -> str | None:
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            if row is None:
                raise UiBlueprintNotFoundError("ui blueprint not found")
            return self._markdown(row) if row.blueprint_json else None

    async def list_blueprints(
        self, project: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_sync, project, status, limit)

    def list_sync(
        self, project: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        with self.sessions() as session:
            query = select(UiBlueprint)
            if project is not None:
                query = query.where(UiBlueprint.project == project)
            if status is not None:
                query = query.where(UiBlueprint.status == status)
            rows = session.scalars(
                query.order_by(UiBlueprint.created_at.desc()).limit(max(1, min(limit, 200)))
            ).all()
            return [self._summary(row) for row in rows]

    async def approve(
        self, blueprint_id: UUID, binding: str, phrase: str, actor: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.approve_sync, blueprint_id, binding, phrase, actor)

    def approve_sync(
        self, blueprint_id: UUID, binding: str, phrase: str, actor: str
    ) -> dict[str, Any]:
        """CANDIDATE -> APPROVED, bound to the exact blueprint and inputs (idempotent)."""
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("ui blueprint approval requires an allowlisted operator channel")
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            stored = row.approval_binding_sha256 or ""
            bound = bool(stored) and hmac.compare_digest(stored, binding)
            phrase_matches = hmac.compare_digest(APPROVAL_PHRASE, phrase)
            if row.status == "APPROVED" and bound and phrase_matches:
                self._verify(row)
                # Replay: the caller re-issues (resumes) the deterministic fan-out.
                return {**self._view(row), "replayed": True}
            if row.status != "CANDIDATE":
                raise ValueError("only a CANDIDATE ui blueprint can be approved")
            if not bound:
                raise ValueError("ui blueprint approval binding does not match")
            if not phrase_matches:
                raise ValueError(f"typed confirmation must be {APPROVAL_PHRASE}")
            self._verify(row)
            row.status = "APPROVED"
            row.decided_by = actor
            row.decided_at = datetime.now(UTC)
            row.decision_sequence += 1
            row.error_code = None
            session.flush()
            return {**self._view(row), "replayed": False}

    async def record_start_failure(self, blueprint_id: UUID) -> None:
        await asyncio.to_thread(
            self.record_terminal_sync, blueprint_id, "FAILED", "WORKFLOW_START_FAILED", []
        )

    # Controller activities ----------------------------------------------------------------

    def register_sync(self, blueprint_id: UUID, workflow_id: str) -> dict[str, Any]:
        """Load the DRAFTING row and open the control Run evidence is bound to."""
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            if workflow_id != row.workflow_id:
                raise ValueError("ui plan workflow identity does not match its blueprint")
            self._ensure_run(session, workflow_id, row.project, "ui-migration-plan")
            return {
                "status": "REGISTERED",
                "project": row.project,
                "requested_by": row.requested_by,
                "requested_source_sha": row.requested_source_sha,
            }

    def record_inputs_sync(self, blueprint_id: UUID, inputs: dict[str, Any]) -> dict[str, Any]:
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            row.source_sha = str(inputs["source_sha"])
            row.inventory_sha256 = str(inputs["inventory_sha256"])
            row.inventory_summary_json = dict(inputs.get("inventory_summary") or {})
            row.kit_snapshot_sha256 = str(inputs["kit_snapshot_sha256"])
            row.gntik_ui_version = (
                str(inputs["gntik_ui_version"])[:100] if inputs.get("gntik_ui_version") else None
            )
            row.kit_image = str(inputs.get("kit_image") or "")[:300] or None
            row.evidence_json = {**(row.evidence_json or {}), **dict(inputs.get("evidence") or {})}
            return {"status": "INPUTS_RECORDED"}

    def record_round_sync(
        self, blueprint_id: UUID, draft_attempts: int, rounds: list[dict[str, Any]]
    ) -> None:
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status == "DRAFTING":
                row.draft_attempts = draft_attempts
                row.review_json = {"rounds": rounds}

    def record_draft_sync(
        self,
        blueprint_id: UUID,
        draft_attempts: int,
        blueprint: dict[str, Any],
        rounds: list[dict[str, Any]],
    ) -> str:
        """Append draft `draft_attempts` as a new immutable version (never overwritten)."""
        digest = blueprint_sha256(blueprint)
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                raise ValueError("ui blueprint is no longer drafting")
            drafts = list(row.drafts_json or [])
            existing = [entry for entry in drafts if entry.get("attempt") == draft_attempts]
            if existing:
                # NEW-123 review 2: an activity retry after a committed write (Temporal lost
                # the completion) replays the identical draft: accept it unchanged. Changed
                # content for an existing version is still refused.
                if existing[0].get("blueprint_sha256") == digest:
                    return digest
                raise ValueError("ui blueprint draft versions are immutable")
            drafts.append(
                {
                    "attempt": draft_attempts,
                    "blueprint": blueprint,
                    "blueprint_sha256": digest,
                    "reviewed_sha256": None,
                }
            )
            row.drafts_json = drafts
            row.draft_attempts = draft_attempts
            row.review_json = {"rounds": rounds}
        return digest

    def mark_reviewed_sync(
        self, blueprint_id: UUID, draft_attempts: int, staged_sha256: str
    ) -> dict[str, Any]:
        """Bind draft version `draft_attempts` to the blueprint hash Hermes computed from
        the exact file the reviewer read; refuse if the stored version differs from it."""
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                raise ValueError("ui blueprint is no longer drafting")
            drafts = list(row.drafts_json or [])
            entry = self._draft(drafts, draft_attempts)
            digest = blueprint_sha256(entry["blueprint"])
            if not (
                hmac.compare_digest(digest, staged_sha256)
                and hmac.compare_digest(str(entry.get("blueprint_sha256")), staged_sha256)
            ):
                raise ValueError("stored ui blueprint draft differs from the reviewed draft")
            if entry.get("reviewed_sha256") not in {None, staged_sha256}:
                raise ValueError("ui blueprint draft was already reviewed as other content")
            entry["reviewed_sha256"] = staged_sha256
            row.drafts_json = [
                entry if item.get("attempt") == draft_attempts else item for item in drafts
            ]
            return {"blueprint": entry["blueprint"], "blueprint_sha256": digest}

    @staticmethod
    def _draft(drafts: list[dict[str, Any]], attempt: int) -> dict[str, Any]:
        matches = [dict(entry) for entry in drafts if entry.get("attempt") == attempt]
        if len(matches) != 1 or not isinstance(matches[0].get("blueprint"), dict):
            raise ValueError("ui blueprint draft version does not exist")
        return matches[0]

    def persist_candidate_sync(
        self,
        blueprint_id: UUID,
        draft_attempts: int,
        reviewed_sha256: str,
        review_rounds: list[dict[str, Any]],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Promote exactly the reviewed draft version to the approvable candidate."""
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            if not (row.source_sha and row.inventory_sha256 and row.kit_snapshot_sha256):
                raise ValueError("ui blueprint inputs were never recorded")
            entry = self._draft(list(row.drafts_json or []), draft_attempts)
            digest = blueprint_sha256(entry["blueprint"])
            if not (
                hmac.compare_digest(digest, reviewed_sha256)
                and entry.get("reviewed_sha256") == reviewed_sha256
            ):
                raise ValueError("ui blueprint draft changed after its review")
            row.blueprint_json = entry["blueprint"]
            row.blueprint_sha256 = digest
            row.reviewed_sha256 = reviewed_sha256
            row.review_json = {"rounds": review_rounds}
            row.evidence_json = {**(row.evidence_json or {}), **evidence}
            row.approval_binding_sha256 = self._binding(row)
            markdown = self._markdown(row)
            row.markdown_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            row.status = "CANDIDATE"
            row.error_code = None
            row.blockers_json = []
            # A newer candidate for the project replaces any older, unapproved one.
            older = session.scalars(
                select(UiBlueprint).where(
                    UiBlueprint.project == row.project,
                    UiBlueprint.status == "CANDIDATE",
                    UiBlueprint.id != row.id,
                )
            ).all()
            for item in older:
                item.status = "SUPERSEDED"
            session.flush()
            blueprint = entry["blueprint"]
            return {
                "status": "CANDIDATE",
                "blueprint_sha256": digest,
                "approval_binding": row.approval_binding_sha256,
                "markdown_sha256": row.markdown_sha256,
                "superseded": [str(item.id) for item in older],
                "pages": len(blueprint.get("pages") or []),
                "gaps": len(blueprint.get("gaps") or []),
            }

    def record_terminal_sync(
        self, blueprint_id: UUID, status: str, error_code: str, blockers: list[Any]
    ) -> dict[str, Any]:
        if status not in {"BLOCKED", "FAILED"}:
            raise ValueError("terminal ui blueprint status is invalid")
        code = error_code if _ERROR_CODE.fullmatch(error_code) else "UI_PLAN_FAILED"
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            row.status = status
            row.error_code = code
            row.blockers_json = [str(item)[:1000] for item in blockers][:50]
            return {"status": status, "error_code": code}

    def load_approved_sync(self, blueprint_id: UUID) -> dict[str, Any]:
        """The approved blueprint for fan-out, re-verified against its binding."""
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            if row is None:
                raise UiBlueprintNotFoundError("ui blueprint not found")
            if row.status != "APPROVED":
                return {"status": "NOT_APPROVED", "current_status": row.status}
            self._verify(row)
            return {
                "status": "APPROVED",
                "project": row.project,
                "blueprint": row.blueprint_json,
                "blueprint_sha256": row.blueprint_sha256,
                "approval_binding": row.approval_binding_sha256,
                "decided_by": row.decided_by,
                "source_sha": row.source_sha,
                "kit_snapshot_sha256": row.kit_snapshot_sha256,
            }

    async def claim_fanout(self, blueprint_id: UUID) -> dict[str, Any]:
        return await asyncio.to_thread(self.claim_fanout_sync, blueprint_id)

    def claim_fanout_sync(self, blueprint_id: UUID) -> dict[str, Any]:
        """At most one active fan-out per blueprint (a DB-level compare-and-set).

        A new sequence is consumed only by the caller that wins the claim (START); every
        other caller is told which fan-out is active (JOIN)."""
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            if row is None:
                raise UiBlueprintNotFoundError("ui blueprint not found")
            if row.status != "APPROVED":
                raise ValueError("only an APPROVED ui blueprint fans out")
            if row.active_fanout_workflow_id:
                return {
                    "action": "JOIN",
                    "workflow_id": row.active_fanout_workflow_id,
                    "claimed_at": row.fanout_claimed_at,
                }
            seen = int(row.fanout_sequence or 0)
        workflow_id = ui_fanout_workflow_id(str(blueprint_id), seen + 1)
        with self.sessions.begin() as session:
            claimed = cast(
                CursorResult[Any],
                session.execute(
                    update(UiBlueprint)
                    .where(
                        UiBlueprint.id == blueprint_id,
                        UiBlueprint.status == "APPROVED",
                        UiBlueprint.active_fanout_workflow_id.is_(None),
                        UiBlueprint.fanout_sequence == seen,
                    )
                    .values(
                        active_fanout_workflow_id=workflow_id,
                        fanout_sequence=seen + 1,
                        fanout_claimed_at=datetime.now(UTC),
                    )
                    .execution_options(synchronize_session=False)
                ),
            ).rowcount
        if claimed == 1:
            return {"action": "START", "workflow_id": workflow_id, "sequence": seen + 1}
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            active = row.active_fanout_workflow_id if row is not None else None
            claimed_at = row.fanout_claimed_at if row is not None else None
        if not active:
            raise ValueError("ui fan-out claim raced; repeat the approval")
        return {"action": "JOIN", "workflow_id": active, "claimed_at": claimed_at}

    async def release_fanout(self, blueprint_id: UUID, workflow_id: str) -> bool:
        return await asyncio.to_thread(self.release_fanout_sync, blueprint_id, workflow_id)

    def release_fanout_sync(self, blueprint_id: UUID, workflow_id: str) -> bool:
        """Clear the active marker only if it still names this fan-out (compare-and-clear)."""
        with self.sessions.begin() as session:
            released = cast(
                CursorResult[Any],
                session.execute(
                    update(UiBlueprint)
                    .where(
                        UiBlueprint.id == blueprint_id,
                        UiBlueprint.active_fanout_workflow_id == workflow_id,
                    )
                    .values(active_fanout_workflow_id=None, fanout_claimed_at=None)
                    .execution_options(synchronize_session=False)
                ),
            ).rowcount
        return bool(released == 1)

    def fanout_owner_sync(self, blueprint_id: UUID, workflow_id: str) -> bool:
        """NEW-123 review 4: the fence. Only the fan-out the active marker names may draft;
        a superseded execution (a start delayed past a released stale claim) stops."""
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            return bool(
                row is not None and workflow_id and row.active_fanout_workflow_id == workflow_id
            )

    def register_fanout_sync(self, blueprint_id: UUID, workflow_id: str) -> None:
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            match = re.fullmatch(rf"ui-blueprint-fanout-{row.id}-([1-9][0-9]{{0,5}})", workflow_id)
            if match is None:
                raise ValueError("ui fan-out workflow identity does not match its blueprint")
            self._ensure_run(session, workflow_id, row.project, "ui-blueprint-fanout")

    def record_fanout_sync(self, blueprint_id: UUID, stats: dict[str, Any]) -> None:
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            row.fanout_json = {
                **dict(row.fanout_json or {}),
                "last_run": {**stats, "recorded_at": datetime.now(UTC).isoformat()},
            }

    def finalize_run_sync(self, workflow_id: str, status: str, error_code: str | None) -> bool:
        if not workflow_id.startswith(_RUN_PREFIXES):
            return False
        with self.sessions.begin() as session:
            runs = session.scalars(select(Run).where(Run.workflow_id == workflow_id)).all()
            now = datetime.now(UTC)
            for run in runs:
                run.status = status
                run.error_code = error_code
                run.finished_at = now
            return bool(runs)

    # Helpers --------------------------------------------------------------------------------

    @staticmethod
    def _ensure_run(session: Session, workflow_id: str, project: str, run_type: str) -> None:
        existing = session.scalar(select(Run).where(Run.workflow_id == workflow_id).limit(1))
        if existing is not None:
            if existing.project != project:
                raise ValueError("control run project does not match the blueprint")
            if existing.status != "RUNNING":
                existing.status = "RUNNING"
                existing.finished_at = None
            return
        session.add(
            new_bound_run(
                id=uuid5(NAMESPACE_URL, f"control-run:{workflow_id}"),
                campaign_id=None,
                change_set_id=None,
                workflow_id=workflow_id,
                run_type=run_type,
                status="RUNNING",
                project=project,
                worker="controller",
                started_at=datetime.now(UTC),
            )
        )
        session.flush()

    @staticmethod
    def _locked(session: Session, blueprint_id: UUID) -> UiBlueprint:
        row = session.get(UiBlueprint, blueprint_id, with_for_update=True)
        if row is None:
            raise UiBlueprintNotFoundError("ui blueprint not found")
        return row

    @staticmethod
    def _binding(row: UiBlueprint) -> str:
        return approval_binding(
            blueprint_id=str(row.id),
            project=row.project,
            blueprint_sha256=str(row.blueprint_sha256),
            reviewed_sha256=str(row.reviewed_sha256),
            inventory_sha256=str(row.inventory_sha256),
            kit_snapshot_sha256=str(row.kit_snapshot_sha256),
            source_sha=str(row.source_sha),
        )

    @classmethod
    def _verify(cls, row: UiBlueprint) -> None:
        """Tamper check: the stored blueprint and binding must recompute exactly."""
        if not isinstance(row.blueprint_json, dict) or not row.approval_binding_sha256:
            raise ValueError("stored ui blueprint is incomplete")
        digest = blueprint_sha256(row.blueprint_json)
        if digest != row.blueprint_sha256 or digest != row.reviewed_sha256:
            raise ValueError("stored ui blueprint no longer matches its reviewed hash")
        if not hmac.compare_digest(cls._binding(row), row.approval_binding_sha256):
            raise ValueError("stored ui blueprint no longer matches its approval binding")

    @staticmethod
    def _markdown(row: UiBlueprint) -> str:
        rounds = (row.review_json or {}).get("rounds") or []
        return render_markdown(
            dict(row.blueprint_json or {}),
            blueprint_id=str(row.id),
            project=row.project,
            source_sha=str(row.source_sha),
            inventory_sha256=str(row.inventory_sha256),
            kit_snapshot_sha256=str(row.kit_snapshot_sha256),
            gntik_ui_version=row.gntik_ui_version,
            blueprint_sha256=str(row.blueprint_sha256),
            review=rounds[-1] if rounds and isinstance(rounds[-1], dict) else None,
        )

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()

    @classmethod
    def _summary(cls, row: UiBlueprint) -> dict[str, Any]:
        blueprint = row.blueprint_json if isinstance(row.blueprint_json, dict) else {}
        return {
            "id": str(row.id),
            "project": row.project,
            "status": row.status,
            "workflow_id": row.workflow_id,
            "source_sha": row.source_sha,
            "blueprint_sha256": row.blueprint_sha256,
            "pages": len(blueprint.get("pages") or []),
            "gaps": len(blueprint.get("gaps") or []),
            "error_code": row.error_code,
            "created_at": cls._iso(row.created_at),
            "updated_at": cls._iso(row.updated_at),
        }

    @classmethod
    def _view(cls, row: UiBlueprint) -> dict[str, Any]:
        binding = row.approval_binding_sha256
        candidate = row.status == "CANDIDATE" and binding is not None
        rounds = (row.review_json or {}).get("rounds") or []
        return {
            **cls._summary(row),
            "requested_by": row.requested_by,
            "requested_source_sha": row.requested_source_sha,
            "inventory_sha256": row.inventory_sha256,
            "inventory_summary": row.inventory_summary_json,
            "kit_snapshot_sha256": row.kit_snapshot_sha256,
            "gntik_ui_version": row.gntik_ui_version,
            "kit_image": row.kit_image,
            "draft_attempts": row.draft_attempts,
            "review_rounds": len(rounds),
            "review_decision": rounds[-1].get("decision") if rounds else None,
            "review_findings": rounds[-1].get("findings", []) if rounds else [],
            "markdown_sha256": row.markdown_sha256,
            "evidence": row.evidence_json,
            "approval_binding": binding,
            "confirmation_phrase": APPROVAL_PHRASE if candidate else None,
            "approve_command": (
                f"engineering-ctl ui-blueprint-approve {row.id} {binding} {APPROVAL_PHRASE}"
                if candidate
                else None
            ),
            "blockers": row.blockers_json,
            "decided_by": row.decided_by,
            "decided_at": cls._iso(row.decided_at),
            "decision_sequence": row.decision_sequence,
            "fanout": row.fanout_json,
            "fanout_workflow_id": row.active_fanout_workflow_id,
            "fanout_sequence": row.fanout_sequence,
            "reviewed_sha256": row.reviewed_sha256,
        }
