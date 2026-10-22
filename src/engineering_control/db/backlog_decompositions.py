"""NEW-234 PR5: durable backlog decompositions, their candidate binding and approval.

Modelled on db/ui_blueprints.py. One BacklogDecomposition row (migration 0022) holds a
plan run: the requested scope, the bound inventory (parts_json["inventory"]), every part
version, review and Hermes rejection (parts_json["parts"]), the reserved provider calls
(provider_calls, design section 3: at most 90), the assembled plan, its approval binding
and the operator's decision. Approval records the decision only: the fan-out is PR 6.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.models import BacklogDecomposition, Run
from engineering_control.db.runtime_binding import new_bound_run
from engineering_control.engineering.backlog import is_reauthenticated_backlog_operator
from engineering_control.engineering.decomposition import (
    APPROVAL_PHRASE,
    DECOMPOSITION_PROJECTS,
    EPIC_SELECTOR,
    MAX_EPIC_SELECTORS,
    MAX_PROVIDER_CALLS,
    WORKFLOW_PREFIX,
    approval_binding,
    decomposition_workflow_id,
    part_content_sha256,
    plan_problems,
    plan_sha256,
    render_markdown,
    risk_preview,
    scoped_inventory,
)
from engineering_control.engineering.decomposition_inventory import inventory_sha256
from engineering_control.engineering.ui_blueprint_parts import usage_summary

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_SHA = re.compile(r"^[a-f0-9]{40}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_CALL_KEY = re.compile(r"^(?:frame|ep[0-9]{2})-[a-f0-9]{16}:(?:draft|review):[1-9]$")
# How many earlier plans of the project are searched for a reusable reviewed part.
REUSE_SEARCH = 20


class DecompositionNotFoundError(ValueError):
    """The requested decomposition does not exist."""


class DecompositionBudgetExhaustedError(ValueError):
    """Design section 3: the plan's provider-call budget is used up."""


class BacklogDecompositionRepository:
    """Every decomposition state transition, each re-checked under a row lock."""

    def __init__(self, database_url: str | None = None, *, engine: Engine | None = None) -> None:
        if engine is None:
            if database_url is None:
                raise ValueError("backlog decomposition repository requires a database")
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
        self,
        project: str,
        requested_by: str,
        source_sha: str | None,
        epics: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.create_sync, project, requested_by, source_sha, epics)

    def create_sync(
        self,
        project: str,
        requested_by: str,
        source_sha: str | None,
        epics: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        if not is_reauthenticated_backlog_operator(requested_by):
            raise ValueError("backlog decompositions require an allowlisted operator channel")
        if project not in DECOMPOSITION_PROJECTS:
            raise ValueError(
                "backlog decomposition is configured only for "
                + ", ".join(sorted(DECOMPOSITION_PROJECTS))
            )
        if source_sha is not None and not _SHA.fullmatch(source_sha):
            raise ValueError("source ref must be a full 40-character commit SHA")
        selectors = sorted(set(epics or ()))
        if len(selectors) > MAX_EPIC_SELECTORS or any(
            not EPIC_SELECTOR.fullmatch(item) for item in selectors
        ):
            raise ValueError("--epic takes epic ids (EP-API) or numbers (1-99)")
        identity = uuid4()
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = BacklogDecomposition(
                id=identity,
                project=project,
                status="DRAFTING",
                workflow_id=decomposition_workflow_id(project, str(identity)),
                requested_by=requested_by,
                requested_source_sha=source_sha,
                parts_json={"requested_epics": selectors or None, "parts": {}, "calls": []},
                provider_calls=0,
                review_json={"rounds": []},
                evidence_json={},
                blockers_json=[],
                fanout_json={},
                decision_sequence=0,
                fanout_sequence=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._view(row)

    async def record_start_failure(self, decomposition_id: UUID) -> None:
        await asyncio.to_thread(
            self.record_terminal_sync, decomposition_id, "FAILED", "WORKFLOW_START_FAILED", []
        )

    async def get(self, decomposition_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_sync, decomposition_id)

    def get_sync(self, decomposition_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(BacklogDecomposition, decomposition_id)
            return None if row is None else self._view(row)

    async def markdown(self, decomposition_id: UUID) -> str | None:
        return await asyncio.to_thread(self.markdown_sync, decomposition_id)

    def markdown_sync(self, decomposition_id: UUID) -> str | None:
        with self.sessions() as session:
            row = session.get(BacklogDecomposition, decomposition_id)
            if row is None:
                raise DecompositionNotFoundError("backlog decomposition not found")
            return self._markdown(row) if row.status in {"CANDIDATE", "APPROVED"} else None

    async def list_decompositions(
        self, project: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_sync, project, status, limit)

    def list_sync(
        self, project: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        with self.sessions() as session:
            query = select(BacklogDecomposition)
            if project is not None:
                query = query.where(BacklogDecomposition.project == project)
            if status is not None:
                query = query.where(BacklogDecomposition.status == status)
            rows = session.scalars(
                query.order_by(BacklogDecomposition.created_at.desc()).limit(
                    max(1, min(limit, 200))
                )
            ).all()
            return [self._summary(row) for row in rows]

    async def approve(
        self,
        decomposition_id: UUID,
        binding: str,
        phrase: str,
        actor: str,
        current_pack: str,
        pack_problems: Sequence[str] = (),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.approve_sync,
            decomposition_id,
            binding,
            phrase,
            actor,
            current_pack,
            pack_problems,
        )

    def approve_sync(
        self,
        decomposition_id: UUID,
        binding: str,
        phrase: str,
        actor: str,
        current_pack: str,
        pack_problems: Sequence[str] = (),
    ) -> dict[str, Any]:
        """CANDIDATE -> APPROVED, bound to the exact plan and its sources (idempotent).

        Records the decision only (no fan-out, no GitHub write). Refused when the stored
        plan, inventory or binding no longer recompute, or when the released agent pack
        changed since the plan was drafted (`current_pack`, the pack revision now) or no
        longer passes the pack check (`pack_problems`, decomposition_pack_problems now)."""
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("decomposition approval requires an allowlisted operator channel")
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            stored = row.approval_binding_sha256 or ""
            bound = bool(stored) and hmac.compare_digest(stored, binding)
            phrase_matches = hmac.compare_digest(APPROVAL_PHRASE, phrase)
            if row.status == "APPROVED" and bound and phrase_matches:
                self._verify(row)
                return {**self._view(row), "replayed": True}
            if row.status != "CANDIDATE":
                raise ValueError("only a CANDIDATE backlog decomposition can be approved")
            if not bound:
                raise ValueError("backlog decomposition approval binding does not match")
            if not phrase_matches:
                raise ValueError(f"typed confirmation must be {APPROVAL_PHRASE}")
            self._verify(row)
            if pack_problems:
                raise ValueError(
                    "DECOMPOSITION_PACK_INVALID: the released agent pack fails its check: "
                    + "; ".join(str(item) for item in pack_problems)[:500]
                )
            bound_pack = str((row.parts_json or {}).get("pack_revision") or "")
            if not hmac.compare_digest(bound_pack, current_pack):
                raise ValueError(
                    "DECOMPOSITION_PACK_DRIFT: the released agent pack changed since this plan "
                    "was drafted; start a new backlog-decompose"
                )
            row.status = "APPROVED"
            row.decided_by = actor
            row.decided_at = datetime.now(UTC)
            row.decision_sequence += 1
            row.error_code = None
            session.flush()
            return {**self._view(row), "replayed": False}

    # Controller activities ----------------------------------------------------------------

    def register_sync(self, decomposition_id: UUID, workflow_id: str) -> dict[str, Any]:
        """Load the DRAFTING row and open the control Run evidence is bound to."""
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "DRAFTING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            if workflow_id != row.workflow_id:
                raise ValueError("decomposition workflow identity does not match its record")
            self._ensure_run(session, workflow_id, row.project)
            return {
                "status": "REGISTERED",
                "project": row.project,
                "requested_by": row.requested_by,
                "requested_source_sha": row.requested_source_sha,
                "requested_epics": (row.parts_json or {}).get("requested_epics"),
            }

    def record_inputs_sync(
        self,
        decomposition_id: UUID,
        inventory: dict[str, Any],
        epics: list[int] | None,
        revision: str,
    ) -> dict[str, Any]:
        """Bind the plan to its inventory (stored whole: every part input is built from
        it), scope and agent-pack revision."""
        digest = inventory_sha256(inventory)
        if inventory.get("inventory_sha256") != digest:
            raise ValueError("decomposition inventory does not match its digest")
        if not _DIGEST.fullmatch(revision):
            raise ValueError("agent pack revision is invalid")
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "DRAFTING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            parts = copy.deepcopy(dict(row.parts_json or {}))
            parts.update(
                inventory=inventory,
                inventory_sha256=digest,
                epics=epics,
                pack_revision=revision,
            )
            parts.setdefault("parts", {})
            parts.setdefault("calls", [])
            row.parts_json = parts
            row.source_sha = str(inventory["source_sha"])
            row.design_sha256 = str(inventory["design_sha256"])
            row.backlog_sha256 = str(inventory["backlog_sha256"])
            row.issues_sha256 = str(inventory["issues_sha256"])
            return {"status": "INPUTS_RECORDED", "inventory_sha256": digest}

    def inputs_sync(self, decomposition_id: UUID) -> dict[str, Any]:
        """The bound inventory, scope and pack revision."""
        with self.sessions() as session:
            row = session.get(BacklogDecomposition, decomposition_id)
            if row is None:
                raise DecompositionNotFoundError("backlog decomposition not found")
            parts = dict(row.parts_json or {})
            inventory = parts.get("inventory")
            if not isinstance(inventory, dict):
                raise ValueError("decomposition inputs were never recorded")
            return {
                "project": row.project,
                "inventory": inventory,
                "epics": parts.get("epics"),
                "pack_revision": str(parts.get("pack_revision") or ""),
            }

    def reserve_call_sync(self, decomposition_id: UUID, key: str) -> int:
        """Reserve one provider call (idempotent per part, phase and attempt); refuses the
        call past the plan's budget. Returns the calls reserved so far."""
        if not _CALL_KEY.fullmatch(key):
            raise ValueError("provider call key is invalid")
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            parts = copy.deepcopy(dict(row.parts_json or {}))
            calls = list(parts.get("calls") or [])
            if key in calls:
                return len(calls)
            if len(calls) >= MAX_PROVIDER_CALLS:
                raise DecompositionBudgetExhaustedError(
                    f"the plan used its {MAX_PROVIDER_CALLS} provider calls"
                )
            calls.append(key)
            parts["calls"] = calls
            row.parts_json = parts
            row.provider_calls = len(calls)
            return len(calls)

    def record_usage_sync(self, decomposition_id: UUID, entry: dict[str, Any]) -> None:
        """Append one provider call's token usage to the evidence (idempotent)."""
        names = ("part_id", "phase", "attempt", "run_id")
        key = tuple(entry.get(name) for name in names)
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            evidence = copy.deepcopy(dict(row.evidence_json or {}))
            usage = [
                item
                for item in evidence.get("usage") or []
                if tuple(item.get(name) for name in names) != key
            ]
            evidence["usage"] = [*usage, entry]
            row.evidence_json = evidence

    @staticmethod
    def _part_entry(parts: dict[str, Any], part_id: str, kind: str, number: int | None) -> Any:
        entry = parts.setdefault("parts", {}).setdefault(
            part_id,
            {
                "kind": kind,
                "number": number,
                "versions": [],
                "rejections": [],
                "approved_attempt": None,
                "reused_from": None,
            },
        )
        if entry.get("kind") != kind or entry.get("number") != number:
            raise ValueError("decomposition part identity changed")
        return entry

    def record_part_sync(
        self,
        decomposition_id: UUID,
        part_id: str,
        kind: str,
        number: int | None,
        attempt: int,
        content: dict[str, Any],
    ) -> str:
        """Store one Hermes-checked part draft (idempotent per attempt). A new draft of
        an approved part (an assembly redraft) withdraws that approval."""
        digest = part_content_sha256(content)
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "DRAFTING":
                raise ValueError("only a DRAFTING decomposition records part drafts")
            parts = copy.deepcopy(dict(row.parts_json or {}))
            entry = self._part_entry(parts, part_id, kind, number)
            entry["versions"] = [
                item for item in entry["versions"] if item.get("attempt") != attempt
            ] + [{"attempt": attempt, "content": content, "content_sha256": digest}]
            if entry.get("approved_attempt") is not None and entry["approved_attempt"] < attempt:
                entry["approved_attempt"] = None
            row.parts_json = parts
            return digest

    def record_part_rejection_sync(
        self,
        decomposition_id: UUID,
        part_id: str,
        kind: str,
        number: int | None,
        attempt: int,
        code: str,
        details: list[Any],
    ) -> None:
        """A Hermes rejection of a part draft (or of the assembled plan, for its owning
        part), kept for every later redraft and re-review of the part."""
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "DRAFTING":
                return
            parts = copy.deepcopy(dict(row.parts_json or {}))
            entry = self._part_entry(parts, part_id, kind, number)
            entry["rejections"] = [
                item for item in entry["rejections"] if item.get("attempt") != attempt
            ] + [{"attempt": attempt, "code": code[:80], "details": list(details)[:50]}]
            row.parts_json = parts

    def mark_part_reviewed_sync(
        self,
        decomposition_id: UUID,
        part_id: str,
        attempt: int,
        reviewed_sha256: str,
        review: dict[str, Any],
    ) -> None:
        """Bind a review to exactly the stored part version the reviewer read (Hermes's
        own hash of the staged file); an APPROVE makes that version the part's."""
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "DRAFTING":
                raise ValueError("only a DRAFTING decomposition records part reviews")
            parts = copy.deepcopy(dict(row.parts_json or {}))
            entry = (parts.get("parts") or {}).get(part_id)
            versions = [
                item for item in (entry or {}).get("versions") or [] if item["attempt"] == attempt
            ]
            if len(versions) != 1:
                raise ValueError("decomposition part version does not exist")
            version = versions[0]
            if not hmac.compare_digest(str(version["content_sha256"]), reviewed_sha256):
                raise ValueError("the reviewed part differs from the stored draft")
            version["reviewed_sha256"] = reviewed_sha256
            version["review"] = review
            assert entry is not None
            if review.get("decision") == "APPROVE":
                entry["approved_attempt"] = attempt
            row.parts_json = parts
            rounds = [
                item
                for item in (row.review_json or {}).get("rounds") or []
                if (item.get("part_id"), item.get("attempt")) != (part_id, attempt)
            ]
            rounds.append({"part_id": part_id, "attempt": attempt, **review})
            row.review_json = {"rounds": rounds}

    def part_sync(self, decomposition_id: UUID, part_id: str) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(BacklogDecomposition, decomposition_id)
            if row is None:
                raise DecompositionNotFoundError("backlog decomposition not found")
            entry = ((row.parts_json or {}).get("parts") or {}).get(part_id)
            return copy.deepcopy(entry) if isinstance(entry, dict) else None

    def approved_content_sync(self, decomposition_id: UUID, part_id: str) -> dict[str, Any]:
        """The content of the part's approved version (its hash re-verified)."""
        entry = self.part_sync(decomposition_id, part_id)
        attempt = (entry or {}).get("approved_attempt")
        versions = [
            item for item in (entry or {}).get("versions") or [] if item.get("attempt") == attempt
        ]
        if attempt is None or len(versions) != 1:
            raise ValueError(f"decomposition part {part_id} has no approved version")
        version = versions[0]
        content = version["content"]
        if not (
            part_content_sha256(content)
            == version["content_sha256"]
            == version.get("reviewed_sha256")
        ):
            raise ValueError(f"decomposition part {part_id} no longer matches its review")
        return {"content": content, "content_sha256": version["content_sha256"], **version}

    def reusable_part_sync(
        self, decomposition_id: UUID, project: str, part_id: str
    ) -> dict[str, Any] | None:
        """The newest independently approved version of this exact part id in an earlier
        plan of the project (the id binds the part's input and the pack revision)."""
        with self.sessions() as session:
            rows = session.scalars(
                select(BacklogDecomposition)
                .where(BacklogDecomposition.project == project)
                .where(BacklogDecomposition.id != decomposition_id)
                .order_by(BacklogDecomposition.created_at.desc())
                .limit(REUSE_SEARCH)
            ).all()
            for row in rows:
                entry = ((row.parts_json or {}).get("parts") or {}).get(part_id)
                if not isinstance(entry, dict) or entry.get("approved_attempt") is None:
                    continue
                attempt = entry["approved_attempt"]
                for version in entry.get("versions") or []:
                    content = version.get("content")
                    if (
                        version.get("attempt") == attempt
                        and isinstance(content, dict)
                        and isinstance(version.get("review"), dict)
                        and part_content_sha256(content)
                        == version.get("content_sha256")
                        == version.get("reviewed_sha256")
                    ):
                        return {
                            "decomposition_id": str(row.id),
                            "attempt": attempt,
                            "content": copy.deepcopy(content),
                            "content_sha256": version["content_sha256"],
                            "review": copy.deepcopy(version["review"]),
                        }
        return None

    def adopt_part_sync(
        self,
        decomposition_id: UUID,
        part_id: str,
        kind: str,
        number: int | None,
        reused: dict[str, Any],
    ) -> None:
        """Adopt an earlier plan's approved part as this plan's approved attempt 1."""
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "DRAFTING":
                raise ValueError("only a DRAFTING decomposition adopts parts")
            parts = copy.deepcopy(dict(row.parts_json or {}))
            entry = self._part_entry(parts, part_id, kind, number)
            digest = str(reused["content_sha256"])
            entry["versions"] = [
                {
                    "attempt": 1,
                    "content": reused["content"],
                    "content_sha256": digest,
                    "reviewed_sha256": digest,
                    "review": reused["review"],
                }
            ]
            entry["approved_attempt"] = 1
            entry["reused_from"] = {
                "decomposition_id": reused["decomposition_id"],
                "attempt": reused["attempt"],
            }
            row.parts_json = parts
            rounds = [
                item
                for item in (row.review_json or {}).get("rounds") or []
                if item.get("part_id") != part_id
            ]
            rounds.append({"part_id": part_id, "attempt": 1, "reused": True, **reused["review"]})
            row.review_json = {"rounds": rounds}

    def record_assembled_sync(
        self, decomposition_id: UUID, plan: dict[str, Any], digest: str
    ) -> None:
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "DRAFTING":
                raise ValueError("only a DRAFTING decomposition records its plan")
            if plan_sha256(plan) != digest:
                raise ValueError("assembled plan does not match its digest")
            row.plan_json = plan
            row.plan_sha256 = digest

    def persist_candidate_sync(
        self, decomposition_id: UUID, digest: str, evidence: dict[str, Any]
    ) -> dict[str, Any]:
        """Promote exactly the assembled plan to the approvable candidate."""
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "DRAFTING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            if not isinstance(row.plan_json, dict) or not row.plan_sha256:
                raise ValueError("the decomposition plan was never assembled")
            if not (
                hmac.compare_digest(plan_sha256(row.plan_json), digest)
                and hmac.compare_digest(str(row.plan_sha256), digest)
            ):
                raise ValueError("the decomposition plan changed after its assembly")
            row.approval_binding_sha256 = self._binding(row)
            markdown = self._markdown(row)
            row.markdown_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            row.evidence_json = {**(row.evidence_json or {}), **evidence}
            row.status = "CANDIDATE"
            row.error_code = None
            row.blockers_json = []
            # A newer candidate for the project replaces any older, unapproved one.
            older = session.scalars(
                select(BacklogDecomposition).where(
                    BacklogDecomposition.project == row.project,
                    BacklogDecomposition.status == "CANDIDATE",
                    BacklogDecomposition.id != row.id,
                )
            ).all()
            for item in older:
                item.status = "SUPERSEDED"
            session.flush()
            plan = row.plan_json
            return {
                "status": "CANDIDATE",
                "plan_sha256": digest,
                "approval_binding": row.approval_binding_sha256,
                "markdown_sha256": row.markdown_sha256,
                "superseded": [str(item.id) for item in older],
                "stories": len(plan.get("stories") or []),
                "tasks": sum(len(story["tasks"]) for story in plan.get("stories") or []),
            }

    def record_terminal_sync(
        self, decomposition_id: UUID, status: str, error_code: str, blockers: list[Any]
    ) -> dict[str, Any]:
        if status not in {"BLOCKED", "FAILED"}:
            raise ValueError("terminal decomposition status is invalid")
        code = error_code if _ERROR_CODE.fullmatch(error_code) else "DECOMPOSITION_FAILED"
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "DRAFTING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            row.status = status
            row.error_code = code
            row.blockers_json = [str(item)[:1000] for item in blockers][:50]
            return {"status": status, "error_code": code}

    def finalize_run_sync(self, workflow_id: str, status: str, error_code: str | None) -> bool:
        if not workflow_id.startswith(WORKFLOW_PREFIX):
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
    def _ensure_run(session: Session, workflow_id: str, project: str) -> None:
        existing = session.scalar(select(Run).where(Run.workflow_id == workflow_id).limit(1))
        if existing is not None:
            if existing.project != project:
                raise ValueError("control run project does not match the decomposition")
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
                run_type="backlog-decomposition-plan",
                status="RUNNING",
                project=project,
                worker="controller",
                started_at=datetime.now(UTC),
            )
        )
        session.flush()

    @staticmethod
    def _locked(session: Session, decomposition_id: UUID) -> BacklogDecomposition:
        row = session.get(BacklogDecomposition, decomposition_id, with_for_update=True)
        if row is None:
            raise DecompositionNotFoundError("backlog decomposition not found")
        return row

    @staticmethod
    def _binding(row: BacklogDecomposition) -> str:
        parts = row.parts_json or {}
        return approval_binding(
            decomposition_id=str(row.id),
            project=row.project,
            source_sha=str(row.source_sha),
            design_sha256=str(row.design_sha256),
            backlog_sha256=str(row.backlog_sha256),
            issues_sha256=str(row.issues_sha256),
            plan_sha256=str(row.plan_sha256),
            pack_revision=str(parts.get("pack_revision") or ""),
            epics=parts.get("epics"),
        )

    @classmethod
    def _verify(cls, row: BacklogDecomposition) -> None:
        """Tamper check: the stored plan, inventory and binding must recompute exactly,
        and the plan must still pass every plan rule against its inventory."""
        parts = row.parts_json or {}
        inventory = parts.get("inventory")
        if (
            not isinstance(row.plan_json, dict)
            or not isinstance(inventory, dict)
            or not row.approval_binding_sha256
        ):
            raise ValueError("stored backlog decomposition is incomplete")
        if inventory_sha256(inventory) != parts.get("inventory_sha256") or (
            inventory.get("source_sha"),
            inventory.get("design_sha256"),
            inventory.get("backlog_sha256"),
            inventory.get("issues_sha256"),
        ) != (row.source_sha, row.design_sha256, row.backlog_sha256, row.issues_sha256):
            raise ValueError("stored decomposition inventory no longer matches its sources")
        if plan_sha256(row.plan_json) != row.plan_sha256:
            raise ValueError("stored decomposition plan no longer matches its hash")
        if not hmac.compare_digest(cls._binding(row), row.approval_binding_sha256):
            raise ValueError("stored decomposition plan no longer matches its approval binding")
        if plan_problems(row.plan_json, scoped_inventory(inventory, parts.get("epics"))):
            raise ValueError("stored decomposition plan no longer passes the plan checks")

    @staticmethod
    def _markdown(row: BacklogDecomposition) -> str:
        parts = row.parts_json or {}
        return render_markdown(
            dict(row.plan_json or {}),
            decomposition_id=str(row.id),
            plan_sha256=str(row.plan_sha256),
            inventory=scoped_inventory(dict(parts.get("inventory") or {}), parts.get("epics")),
        )

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()

    @classmethod
    def _summary(cls, row: BacklogDecomposition) -> dict[str, Any]:
        plan = row.plan_json if isinstance(row.plan_json, dict) else {}
        stories = plan.get("stories") or []
        parts = row.parts_json or {}
        return {
            "id": str(row.id),
            "project": row.project,
            "status": row.status,
            "workflow_id": row.workflow_id,
            "source_sha": row.source_sha,
            "epics": parts.get("epics"),
            "plan_sha256": row.plan_sha256,
            "stories": len(stories),
            "tasks": sum(len(story.get("tasks") or []) for story in stories),
            "provider_calls": row.provider_calls,
            "error_code": row.error_code,
            "created_at": cls._iso(row.created_at),
            "updated_at": cls._iso(row.updated_at),
        }

    @staticmethod
    def _parts_view(parts: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "part_id": part_id,
                "kind": entry.get("kind"),
                "number": entry.get("number"),
                "drafts": len(entry.get("versions") or []),
                "rejections": len(entry.get("rejections") or []),
                "approved_attempt": entry.get("approved_attempt"),
                "reused_from": entry.get("reused_from"),
            }
            for part_id, entry in (parts.get("parts") or {}).items()
            if isinstance(entry, dict)
        ]

    @classmethod
    def _view(cls, row: BacklogDecomposition) -> dict[str, Any]:
        binding = row.approval_binding_sha256
        candidate = row.status == "CANDIDATE" and binding is not None
        parts = row.parts_json or {}
        rounds = (row.review_json or {}).get("rounds") or []
        plan = row.plan_json if isinstance(row.plan_json, dict) else None
        risks = risk_preview(plan) if plan and row.status in {"CANDIDATE", "APPROVED"} else []
        return {
            **cls._summary(row),
            "requested_by": row.requested_by,
            "requested_source_sha": row.requested_source_sha,
            "requested_epics": parts.get("requested_epics"),
            "design_sha256": row.design_sha256,
            "backlog_sha256": row.backlog_sha256,
            "issues_sha256": row.issues_sha256,
            "inventory_sha256": parts.get("inventory_sha256"),
            "pack_revision": parts.get("pack_revision"),
            "parts": cls._parts_view(parts),
            "review_rounds": len(rounds),
            "risk_escalations": [item for item in risks if item["escalated"]],
            "markdown_sha256": row.markdown_sha256,
            "evidence": {k: v for k, v in (row.evidence_json or {}).items() if k != "usage"},
            "approval_binding": binding,
            "confirmation_phrase": APPROVAL_PHRASE if candidate else None,
            "approve_command": (
                f"engineering-ctl backlog-decomposition-approve {row.id} {binding} "
                f"{APPROVAL_PHRASE}"
                if candidate
                else None
            ),
            "blockers": row.blockers_json,
            "decided_by": row.decided_by,
            "decided_at": cls._iso(row.decided_at),
            "decision_sequence": row.decision_sequence,
            "usage": usage_summary((row.evidence_json or {}).get("usage")),
        }
