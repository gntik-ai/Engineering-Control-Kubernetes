"""NEW-234 PR5: durable backlog decompositions, their candidate binding and approval.

Modelled on db/ui_blueprints.py. One BacklogDecomposition row (migration 0022) holds a
plan run: the requested scope, the bound inventory (parts_json["inventory"]), every part
version, review and Hermes rejection (parts_json["parts"]), the reserved provider calls
(provider_calls, design section 3: at most 90), the assembled plan, its approval binding
and the operator's decision.

NEW-234 PR6: the approved plan's fan-out (modelled on db/ui_blueprints.py, NEW-123/146):
the single active fan-out marker, its runs and statistics (fanout_json), the tasks an
existing request of the same lineage superseded, the audited retry of FAILED tasks, the
ready set of requests whose predecessors are all published (bulk approval bound to the
plan and their exact bindings) and the story amendments the operator publishes.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.change_requests import ChangeRequestRepository
from engineering_control.db.models import BacklogDecomposition, ChangeRequest, DashboardEvent, Run
from engineering_control.db.runtime_binding import new_bound_run
from engineering_control.engineering.backlog import is_reauthenticated_backlog_operator
from engineering_control.engineering.change_requests import approval_record
from engineering_control.engineering.decomposition import (
    APPROVAL_PHRASE,
    DECOMPOSITION_PROJECTS,
    EPIC_SELECTOR,
    FINAL_REFUSAL,
    MAX_EPIC_SELECTORS,
    MAX_PROVIDER_CALLS,
    REJECTIONS_EXHAUSTED,
    WORKFLOW_PREFIX,
    apply_declaration_changes,
    approval_binding,
    decomposition_profile,
    decomposition_workflow_id,
    part_content_sha256,
    plan_problems,
    plan_sha256,
    render_markdown,
    risk_preview,
    scoped_inventory,
)
from engineering_control.engineering.decomposition_fanout import (
    FANOUT_RUN_TYPE,
    FANOUT_WORKFLOW_PREFIX,
    RETRY_FANOUT_PHRASE,
    amended_story_body,
    amendment_diff,
    fanout_tasks,
    fanout_workflow_id,
    ready_binding,
    task_change_request_id,
)
from engineering_control.engineering.decomposition_inventory import body_sha256, inventory_sha256
from engineering_control.engineering.ui_blueprint_parts import usage_summary

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_SHA = re.compile(r"^[a-f0-9]{40}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
# NEW-244: a declaration fix-up is its own provider call (`<part>:fixup:<base attempt>`).
_CALL_KEY = re.compile(r"^(?:frame|ep[0-9]{2})-[a-f0-9]{16}:(?:draft|review|fixup):[1-9]$")
# How many earlier plans of the project are searched for a reusable reviewed part.
REUSE_SEARCH = 20


def _strictly_reviewed(version: dict[str, Any]) -> bool:
    """The version's content is exactly what its review bound (content and review hashes)."""
    content = version.get("content")
    return (
        isinstance(content, dict)
        and "fixup" not in version
        and part_content_sha256(content)
        == version.get("content_sha256")
        == version.get("reviewed_sha256")
    )


def _derived_from_review(entry: dict[str, Any], version: dict[str, Any]) -> bool:
    """NEW-244: a declaration fix-up's version: its base version (fixup.base_attempt) is
    strictly reviewed with the review hash and review it carries, and replaying its changes
    on that base reproduces its content and content_sha256 exactly."""
    fixup = version.get("fixup")
    content = version.get("content")
    if not isinstance(fixup, dict) or not isinstance(content, dict):
        return False
    bases = [
        item
        for item in entry.get("versions") or []
        if item is not version and item.get("attempt") == fixup.get("base_attempt")
    ]
    if len(bases) != 1 or not _strictly_reviewed(bases[0]):
        return False
    base = bases[0]
    if not (
        base["reviewed_sha256"] == fixup.get("base_sha256") == version.get("reviewed_sha256")
        and base.get("review") == version.get("review")
        and isinstance(fixup.get("changes"), list)
        and fixup["changes"]
    ):
        return False
    try:
        replayed = apply_declaration_changes(base["content"], fixup["changes"])
    except (ValueError, TypeError, AttributeError, KeyError):
        return False
    digest = part_content_sha256(content)
    return part_content_sha256(replayed) == digest and digest == str(version["content_sha256"])


def reviewed_version(entry: dict[str, Any], version: dict[str, Any]) -> bool:
    """A version whose content its review covers: strictly, or (NEW-244) as a fix-up
    derived from a strictly reviewed base. Everything else is refused."""
    return _strictly_reviewed(version) or _derived_from_review(entry, version)


def _with_terminal_refusal(
    row: BacklogDecomposition, part_id: str, entry: dict[str, Any]
) -> list[dict[str, Any]]:
    """The part's stored rejections, plus the provider schema refusal that ended it when
    the plan stopped on it: a plan that ended before that refusal was recorded as a
    terminal rejection (_record_final_refusal) keeps it only in its terminal blockers."""
    rejections = copy.deepcopy(list(entry.get("rejections") or []))
    blockers = [str(item) for item in row.blockers_json or []]
    match = FINAL_REFUSAL.fullmatch(blockers[0]) if blockers else None
    if row.error_code != REJECTIONS_EXHAUSTED or match is None or match.group(1) != part_id:
        return rejections
    attempt = 1 + max(
        [0]
        + [int(item.get("attempt") or 0) for item in entry.get("versions") or []]
        + [int(item.get("attempt") or 0) for item in rejections]
    )
    if any(item.get("terminal") is True for item in rejections):
        return rejections  # recorded by _record_final_refusal
    terminal = {"attempt": attempt, "code": "DRAFT_SCHEMA_INVALID", "details": blockers[1:50]}
    return [*rejections, terminal]


class DecompositionNotFoundError(ValueError):
    """The requested decomposition does not exist."""


class DecompositionBudgetExhaustedError(ValueError):
    """Design section 3: the plan's provider-call budget is used up."""


class DecompositionFanoutActiveError(ValueError):
    """NEW-234 PR6: a fan-out holds the decomposition's active marker."""

    def __init__(self, workflow_id: str) -> None:
        super().__init__(f"a decomposition fan-out is active ({workflow_id})")
        self.workflow_id = workflow_id


# How many retries and bulk approvals each decomposition keeps in its fan-out record.
FANOUT_HISTORY = 20


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
                # NEW-234 PR6: a replay (re)starts the fan-out, so it re-runs the same
                # checks first.
                self._verify(row)
                self._check_pack(row, current_pack, pack_problems)
                return {**self._view(row), "replayed": True}
            if row.status != "CANDIDATE":
                raise ValueError("only a CANDIDATE backlog decomposition can be approved")
            if not bound:
                raise ValueError("backlog decomposition approval binding does not match")
            if not phrase_matches:
                raise ValueError(f"typed confirmation must be {APPROVAL_PHRASE}")
            self._verify(row)
            self._check_pack(row, current_pack, pack_problems)
            row.status = "APPROVED"
            row.decided_by = actor
            row.decided_at = datetime.now(UTC)
            row.decision_sequence += 1
            row.error_code = None
            session.flush()
            return {**self._view(row), "replayed": False}

    @staticmethod
    def _check_pack(
        row: BacklogDecomposition, current_pack: str, pack_problems: Sequence[str]
    ) -> None:
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

    # Fan-out: operator API (NEW-234 PR6) --------------------------------------------------

    async def check_retry(
        self,
        decomposition_id: UUID,
        binding: str,
        phrase: str,
        actor: str,
        current_pack: str,
        pack_problems: Sequence[str] = (),
    ) -> None:
        await asyncio.to_thread(
            self.check_retry_sync,
            decomposition_id,
            binding,
            phrase,
            actor,
            current_pack,
            pack_problems,
        )

    def check_retry_sync(
        self,
        decomposition_id: UUID,
        binding: str,
        phrase: str,
        actor: str,
        current_pack: str,
        pack_problems: Sequence[str] = (),
    ) -> None:
        """The retry's authorization, checked before a stale marker is released."""
        with self.sessions() as session:
            row = session.get(BacklogDecomposition, decomposition_id)
            if row is None:
                raise DecompositionNotFoundError("backlog decomposition not found")
            self._check_retry(row, binding, phrase, actor)
            self._check_pack(row, current_pack, pack_problems)

    @classmethod
    def _check_retry(cls, row: BacklogDecomposition, binding: str, phrase: str, actor: str) -> None:
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("decomposition fan-out retry requires an allowlisted operator channel")
        if row.status != "APPROVED":
            raise ValueError("only an APPROVED backlog decomposition's fan-out can be retried")
        stored = row.approval_binding_sha256 or ""
        if not (stored and hmac.compare_digest(stored, binding)):
            raise ValueError("backlog decomposition approval binding does not match")
        if not hmac.compare_digest(RETRY_FANOUT_PHRASE, phrase):
            raise ValueError(f"typed confirmation must be {RETRY_FANOUT_PHRASE}")
        cls._verify(row)

    async def retry_fanout(
        self, decomposition_id: UUID, binding: str, phrase: str, actor: str, *, limit: int
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.retry_fanout_sync, decomposition_id, binding, phrase, actor, limit=limit
        )

    def retry_fanout_sync(
        self, decomposition_id: UUID, binding: str, phrase: str, actor: str, *, limit: int = 250
    ) -> dict[str, Any]:
        try:
            return self._retry_fanout(decomposition_id, binding, phrase, actor, limit=limit)
        except IntegrityError as error:
            # Migration 0023: another active request took a reset task's lineage meanwhile.
            raise ValueError(
                "a failed task's lineage was taken by another request during the retry; "
                "repeat the retry"
            ) from error

    def _retry_fanout(
        self, decomposition_id: UUID, binding: str, phrase: str, actor: str, *, limit: int
    ) -> dict[str, Any]:
        """NEW-146 for flow D: reset the approved plan's FAILED fan-out tasks to the fresh
        pending state (DRAFTING, revision 1) in one audited transaction, so the next
        fan-out renders and reviews them again under their unchanged ids.

        Only a failure a fan-out recorded (FAILED at revision 1, bound to this plan's task)
        is reset, at most ``limit`` in task order; candidates, approved, published, rejected
        and operator-revised requests are never touched. Refused while a fan-out holds the
        marker. Idempotent: reset tasks are pending, not FAILED."""
        limit = max(1, int(limit))
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            self._check_retry(row, binding, phrase, actor)
            if row.active_fanout_workflow_id:
                raise DecompositionFanoutActiveError(row.active_fanout_workflow_id)
            digest = str(row.plan_sha256)
            tasks = fanout_tasks(dict(row.plan_json or {}))
            identities = {task_change_request_id(digest, task.key): task for task in tasks}
            # The one ordered change-request lock of this transaction (global lock order).
            requests = ChangeRequestRepository._lock_requests(session, list(identities))
            now = datetime.now(UTC)
            reset: list[dict[str, Any]] = []
            superseded: list[dict[str, Any]] = []
            still_failed = 0
            for change_request_id, task in identities.items():
                request = requests.get(change_request_id)
                if request is None or request.status != "FAILED" or request.revision != 1:
                    continue
                context = request.decomposition_context_json or {}
                if (
                    context.get("decomposition_id") != str(row.id)
                    or context.get("task_key") != task.short_key
                ):
                    continue
                if ChangeRequestRepository._lineage_taken(session, request):
                    # Another active request (another plan's) holds the task now.
                    superseded.append(
                        {"change_request_id": str(change_request_id), "task_key": task.key}
                    )
                    continue
                if len(reset) >= limit:
                    still_failed += 1
                    continue
                reset.append(
                    {
                        "change_request_id": str(change_request_id),
                        "task_key": task.key,
                        "error_code": request.error_code,
                    }
                )
                # Back to exactly what create_decomposition_task_sync writes (the context
                # without any predecessor issue: persist fills them in again).
                request.status = "DRAFTING"
                request.error_code = None
                request.proposal_json = {}
                request.review_json = {}
                request.dedupe_json = {}
                request.fingerprint = None
                request.issue_title = None
                request.issue_body = None
                request.approval_binding_sha256 = None
                request.decomposition_context_json = {**context, "depends_on": []}
                request.updated_at = now
            record = {
                "actor": actor,
                "at": now.isoformat(),
                "limit": limit,
                "reset": len(reset),
                "still_failed": still_failed,
                "superseded": len(superseded),
            }
            fanout = dict(row.fanout_json or {})
            fanout["retries"] = [
                *list(fanout.get("retries") or [])[-(FANOUT_HISTORY - 1) :],
                record,
            ]
            row.fanout_json = fanout
            session.add(
                DashboardEvent(
                    occurred_at=now,
                    level="INFO",
                    event_type="decomposition_fanout.retry_requested",
                    source="controller",
                    worker_kind="controller",
                    project=row.project,
                    entity_type="backlog_decomposition",
                    entity_id=str(row.id),
                    current_phase="DECOMPOSITION_FANOUT_RETRY",
                    message=f"Decomposition fan-out retry: {len(reset)} failed task(s) reset",
                    metadata_json={
                        **record,
                        "change_request_ids": [entry["change_request_id"] for entry in reset],
                    },
                )
            )
            session.flush()
            return {
                "decomposition_id": str(row.id),
                "status": row.status,
                "reset_count": len(reset),
                "reset": reset,
                "still_failed": still_failed,
                "superseded": superseded,
            }

    async def ready(self, decomposition_id: UUID) -> dict[str, Any]:
        return await asyncio.to_thread(self.ready_sync, decomposition_id)

    def ready_sync(self, decomposition_id: UUID) -> dict[str, Any]:
        """The approved plan's requests whose predecessors are all published, and the ready
        binding one bulk approval confirms. A candidate a publication raced (its body lacks
        a published predecessor) is re-rendered first, deterministically."""
        with self.sessions() as session:
            row = self._approved_row(session, decomposition_id)
            state = self._ready_state(session, row)
        if state["stale"]:
            ChangeRequestRepository(engine=self.engine).refresh_dependencies_sync(
                [UUID(entry["change_request_id"]) for entry in state["stale"]]
            )
            with self.sessions() as session:
                row = self._approved_row(session, decomposition_id)
                state = self._ready_state(session, row)
        return state

    async def approve_ready(
        self, decomposition_id: UUID, binding: str, actor: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.approve_ready_sync, decomposition_id, binding, actor)

    def approve_ready_sync(
        self, decomposition_id: UUID, binding: str, actor: str
    ) -> dict[str, Any]:
        """Approve every ready request in one audited transaction, bound to the approved
        plan and to exactly the ready set the operator saw (each request's current
        binding): a re-render or a newly ready request in between refuses the whole
        approval. Each request is approved as change-approve would (tamper check, decision
        sequence, approval record); the operator still publishes each with change-publish."""
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("decomposition bulk approval requires an allowlisted operator channel")
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "APPROVED":
                raise ValueError("only an APPROVED backlog decomposition's requests are approved")
            self._verify(row)
            digest = str(row.plan_sha256)
            identities = [
                task_change_request_id(digest, task.key)
                for task in fanout_tasks(dict(row.plan_json or {}))
            ]
            # The plan's requests in the one ordered change-request lock (global order).
            ChangeRequestRepository._lock_requests(session, identities)
            state = self._ready_state(session, row)
            if not state["ready"]:
                raise ValueError("no request of this backlog decomposition is ready for approval")
            if not hmac.compare_digest(str(state["ready_binding"]), binding):
                raise ValueError(
                    "DECOMPOSITION_READY_SET_CHANGED: the ready requests changed since they "
                    "were listed; run backlog-decomposition-ready again"
                )
            now = datetime.now(UTC)
            approved: list[dict[str, Any]] = []
            for entry in state["ready"]:
                request = session.get(ChangeRequest, UUID(entry["change_request_id"]))
                if request is None or request.status != "CANDIDATE":
                    raise ValueError("a ready request changed during the bulk approval")
                ChangeRequestRepository._verify_stored_content(request)
                request.status = "APPROVED"
                request.decided_by = actor
                request.decided_at = now
                request.decision_reason = None
                request.error_code = None
                request.decision_sequence += 1
                request.approval_snapshot_json = approval_record(
                    **ChangeRequestRepository._approval_fields(request)
                )
                approved.append(
                    {
                        "change_request_id": str(request.id),
                        "task_key": entry["task_key"],
                        "target_repository": request.target_repository,
                        "publish_command": f"engineering-ctl change-publish {request.id}",
                    }
                )
            record = {"actor": actor, "at": now.isoformat(), "ready_binding": binding,
                      "approved": len(approved)}  # fmt: skip
            fanout = dict(row.fanout_json or {})
            fanout["bulk_approvals"] = [
                *list(fanout.get("bulk_approvals") or [])[-(FANOUT_HISTORY - 1) :],
                record,
            ]
            row.fanout_json = fanout
            session.add(
                DashboardEvent(
                    occurred_at=now,
                    level="INFO",
                    event_type="decomposition_fanout.ready_approved",
                    source="controller",
                    worker_kind="controller",
                    project=row.project,
                    entity_type="backlog_decomposition",
                    entity_id=str(row.id),
                    current_phase="DECOMPOSITION_APPROVE_READY",
                    message=f"Decomposition bulk approval: {len(approved)} request(s)",
                    metadata_json={
                        **record,
                        "change_request_ids": [item["change_request_id"] for item in approved],
                    },
                )
            )
            session.flush()
            return {
                "decomposition_id": str(row.id),
                "approved_count": len(approved),
                "approved": approved,
                "note": "Hermes never publishes: publish each request with its "
                "publish_command (the operator's own gh).",
            }

    async def story_amendments(self, decomposition_id: UUID) -> dict[str, Any]:
        return await asyncio.to_thread(self.story_amendments_sync, decomposition_id)

    def story_amendments_sync(self, decomposition_id: UUID) -> dict[str, Any]:
        """Each amended story of the approved plan as an operator-published edit: the
        amended body (rendered deterministically from the bound body), its diff and the
        bound body hash to verify the issue against first. Hermes never edits an issue."""
        with self.sessions() as session:
            row = self._approved_row(session, decomposition_id)
            plan = dict(row.plan_json or {})
            inventory = dict((row.parts_json or {}).get("inventory") or {})
            stories = {str(item["id"]): item for item in inventory.get("stories") or []}
            project = decomposition_profile(str(row.project)).project
            source_repository = decomposition_profile(project).source_repository
            amendments: list[dict[str, Any]] = []
            for story in plan.get("stories") or []:
                amendment = story.get("story_amendment")
                if not amendment:
                    continue
                bound = stories[str(story["story_id"])]
                current = str(bound["body"])
                amended, manual = amended_story_body(
                    current,
                    str(story["story_id"]),
                    amendment,
                    decomposition_id=str(row.id),
                    plan_sha256=str(row.plan_sha256),
                    project=project,
                )
                issue = int(story["story_issue"])
                amendments.append(
                    {
                        "story_id": str(story["story_id"]),
                        "repository": source_repository,
                        "issue": issue,
                        "reason": amendment.get("reason"),
                        "current_body_sha256": str(bound["body_sha256"]),
                        "amended_body": amended,
                        "amended_body_sha256": body_sha256(amended),
                        "diff": amendment_diff(
                            current, amended, repository=source_repository, issue=issue
                        ),
                        "manual": manual,
                    }
                )
            return {
                "decomposition_id": str(row.id),
                "plan_sha256": row.plan_sha256,
                "source_sha": row.source_sha,
                "amendments": amendments,
            }

    # Fan-out: controller activities ----------------------------------------------------------

    def load_approved_sync(self, decomposition_id: UUID) -> dict[str, Any]:
        """The approved plan for the fan-out, re-verified against its binding."""
        with self.sessions() as session:
            row = session.get(BacklogDecomposition, decomposition_id)
            if row is None:
                raise DecompositionNotFoundError("backlog decomposition not found")
            if row.status != "APPROVED":
                return {"status": "NOT_APPROVED", "current_status": row.status}
            self._verify(row)
            fanout = row.fanout_json or {}
            return {
                "status": "APPROVED",
                "project": row.project,
                "plan": row.plan_json,
                "plan_sha256": row.plan_sha256,
                "approval_binding": row.approval_binding_sha256,
                "decided_by": row.decided_by,
                "pack_revision": (row.parts_json or {}).get("pack_revision"),
                "superseded": dict(fanout.get("superseded") or {}),
            }

    async def claim_fanout(self, decomposition_id: UUID) -> dict[str, Any]:
        return await asyncio.to_thread(self.claim_fanout_sync, decomposition_id)

    def claim_fanout_sync(self, decomposition_id: UUID) -> dict[str, Any]:
        """At most one active fan-out per decomposition (a DB-level compare-and-set)."""
        with self.sessions() as session:
            row = session.get(BacklogDecomposition, decomposition_id)
            if row is None:
                raise DecompositionNotFoundError("backlog decomposition not found")
            if row.status != "APPROVED":
                raise ValueError("only an APPROVED backlog decomposition fans out")
            if row.active_fanout_workflow_id:
                return {
                    "action": "JOIN",
                    "workflow_id": row.active_fanout_workflow_id,
                    "claimed_at": row.fanout_claimed_at,
                }
            seen = int(row.fanout_sequence or 0)
        workflow_id = fanout_workflow_id(str(decomposition_id), seen + 1)
        with self.sessions.begin() as session:
            claimed = cast(
                CursorResult[Any],
                session.execute(
                    update(BacklogDecomposition)
                    .where(
                        BacklogDecomposition.id == decomposition_id,
                        BacklogDecomposition.status == "APPROVED",
                        BacklogDecomposition.active_fanout_workflow_id.is_(None),
                        BacklogDecomposition.fanout_sequence == seen,
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
            row = session.get(BacklogDecomposition, decomposition_id)
            active = row.active_fanout_workflow_id if row is not None else None
            claimed_at = row.fanout_claimed_at if row is not None else None
        if not active:
            raise ValueError("decomposition fan-out claim raced; repeat the approval")
        return {"action": "JOIN", "workflow_id": active, "claimed_at": claimed_at}

    async def release_fanout(self, decomposition_id: UUID, workflow_id: str) -> bool:
        return await asyncio.to_thread(self.release_fanout_sync, decomposition_id, workflow_id)

    def release_fanout_sync(self, decomposition_id: UUID, workflow_id: str) -> bool:
        """Clear the active marker only if it still names this fan-out (compare-and-clear)."""
        with self.sessions.begin() as session:
            released = cast(
                CursorResult[Any],
                session.execute(
                    update(BacklogDecomposition)
                    .where(
                        BacklogDecomposition.id == decomposition_id,
                        BacklogDecomposition.active_fanout_workflow_id == workflow_id,
                    )
                    .values(active_fanout_workflow_id=None, fanout_claimed_at=None)
                    .execution_options(synchronize_session=False)
                ),
            ).rowcount
        return bool(released == 1)

    async def active_fanout(self, decomposition_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.active_fanout_sync, decomposition_id)

    def active_fanout_sync(self, decomposition_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(BacklogDecomposition, decomposition_id)
            if row is None:
                raise DecompositionNotFoundError("backlog decomposition not found")
            if not row.active_fanout_workflow_id:
                return None
            return {
                "workflow_id": str(row.active_fanout_workflow_id),
                "claimed_at": row.fanout_claimed_at,
            }

    def fanout_owner_sync(self, decomposition_id: UUID, workflow_id: str) -> bool:
        """The fence: only the fan-out the active marker names may write."""
        with self.sessions() as session:
            row = session.get(BacklogDecomposition, decomposition_id)
            return bool(
                row is not None and workflow_id and row.active_fanout_workflow_id == workflow_id
            )

    def register_fanout_sync(self, decomposition_id: UUID, workflow_id: str) -> None:
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            match = re.fullmatch(
                rf"{re.escape(FANOUT_WORKFLOW_PREFIX)}{row.id}-([1-9][0-9]{{0,5}})", workflow_id
            )
            if match is None:
                raise ValueError("decomposition fan-out workflow identity does not match")
            self._ensure_run(session, workflow_id, row.project, FANOUT_RUN_TYPE)

    def finish_fanout_sync(
        self,
        decomposition_id: UUID,
        workflow_id: str,
        stats: dict[str, Any],
        notify: Callable[[Session], Any] | None = None,
    ) -> bool:
        """NEW-234 PR6 review: end a fan-out run atomically under the ownership fence. Only
        the fan-out the active marker names records its statistics, emits its notification
        (``notify``, in the same transaction) and clears the marker; a superseded execution
        changes nothing and is told so (False)."""
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if not workflow_id or row.active_fanout_workflow_id != workflow_id:
                return False
            row.fanout_json = {
                **dict(row.fanout_json or {}),
                "last_run": {**stats, "recorded_at": datetime.now(UTC).isoformat()},
            }
            row.active_fanout_workflow_id = None
            row.fanout_claimed_at = None
            if notify is not None:
                notify(session)
            return True

    def record_superseded_sync(
        self, decomposition_id: UUID, workflow_id: str, key: str, entry: dict[str, Any]
    ) -> bool:
        """A task an existing request of its lineage superseded (fenced by the marker)."""
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if not workflow_id or row.active_fanout_workflow_id != workflow_id:
                return False
            fanout = dict(row.fanout_json or {})
            superseded = dict(fanout.get("superseded") or {})
            superseded[key] = {**entry, "recorded_at": datetime.now(UTC).isoformat()}
            fanout["superseded"] = superseded
            row.fanout_json = fanout
            return True

    @staticmethod
    def _approved_row(session: Session, decomposition_id: UUID) -> BacklogDecomposition:
        row = session.get(BacklogDecomposition, decomposition_id)
        if row is None:
            raise DecompositionNotFoundError("backlog decomposition not found")
        if row.status != "APPROVED":
            raise ValueError("the backlog decomposition is not APPROVED")
        BacklogDecompositionRepository._verify(row)
        return row

    @staticmethod
    def _ready_state(session: Session, row: BacklogDecomposition) -> dict[str, Any]:
        digest = str(row.plan_sha256)
        tasks = fanout_tasks(dict(row.plan_json or {}))
        identities = {task_change_request_id(digest, task.key): task for task in tasks}
        requests = {
            request.id: request
            for request in session.scalars(
                select(ChangeRequest).where(ChangeRequest.id.in_(list(identities)))
            ).all()
        }
        superseded = dict((row.fanout_json or {}).get("superseded") or {})
        counts: dict[str, int] = {}
        ready: list[dict[str, Any]] = []
        awaiting: list[dict[str, Any]] = []
        individual: list[dict[str, Any]] = []
        stale: list[dict[str, Any]] = []
        for identity, task in identities.items():
            request = requests.get(identity)
            if request is not None and (request.decomposition_context_json or {}).get(
                "decomposition_id"
            ) != str(row.id):
                # An identical plan's earlier decomposition owns this request.
                request = None
                superseded.setdefault(task.key, {})
            status = (
                request.status
                if request is not None
                else "SUPERSEDED_BY_EXISTING"
                if task.key in superseded
                else "PENDING"
            )
            counts[status] = counts.get(status, 0) + 1
            if request is None or request.status != "CANDIDATE":
                continue
            context = dict(request.decomposition_context_json or {})
            dedupe = request.dedupe_json if isinstance(request.dedupe_json, dict) else {}
            review = request.review_json if isinstance(request.review_json, dict) else {}
            entry = {
                "change_request_id": str(request.id),
                "task_key": task.key,
                "target_repository": request.target_repository,
                "issue_title": request.issue_title,
                "approval_binding": request.approval_binding_sha256,
                "import_risk": dedupe.get("import_risk"),
            }
            refs, waiting = ChangeRequestRepository._dependency_state(
                session, request.project, context
            )
            if waiting:
                awaiting.append({**entry, "awaiting_dependencies": waiting})
            elif refs != list(context.get("depends_on") or []):
                stale.append(entry)
            elif review.get("decision") != "APPROVE" or review.get("unresolved_findings"):
                # A reviewer's unresolved findings are approved one by one (change-approve).
                individual.append({**entry, "reason": "REVIEW_UNRESOLVED"})
            elif not isinstance(entry["import_risk"], dict) or entry["import_risk"].get(
                "escalated"
            ):
                # Design section 9: the import would classify it above its proposed risk.
                individual.append({**entry, "reason": "IMPORT_RISK_ESCALATED"})
            else:
                ready.append(entry)
        binding = (
            ready_binding(
                decomposition_id=str(row.id),
                plan_sha256=digest,
                decomposition_binding=str(row.approval_binding_sha256),
                items=[
                    (entry["change_request_id"], str(entry["approval_binding"])) for entry in ready
                ],
            )
            if ready
            else None
        )
        return {
            "decomposition_id": str(row.id),
            "plan_sha256": digest,
            "tasks": len(tasks),
            "counts": dict(sorted(counts.items())),
            "ready": ready,
            "ready_binding": binding,
            "approve_ready_command": (
                f"engineering-ctl backlog-decomposition-approve-ready {row.id} {binding}"
                if binding
                else None
            ),
            "awaiting_dependencies": awaiting,
            "needs_individual_approval": individual,
            "stale": stale,
        }

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
        *,
        normalized: list[dict[str, str]] | None = None,
    ) -> str:
        """Store one Hermes-checked part draft (idempotent per attempt). A new draft of
        an approved part (an assembly redraft) withdraws that approval. NEW-244:
        `normalized` are the consumed contracts Hermes added to the draft (kept with the
        version only when there are any)."""
        digest = part_content_sha256(content)
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "DRAFTING":
                raise ValueError("only a DRAFTING decomposition records part drafts")
            parts = copy.deepcopy(dict(row.parts_json or {}))
            entry = self._part_entry(parts, part_id, kind, number)
            entry["versions"] = [
                item for item in entry["versions"] if item.get("attempt") != attempt
            ] + [
                {
                    "attempt": attempt,
                    "content": content,
                    "content_sha256": digest,
                    **({"normalized": list(normalized)} if normalized else {}),
                }
            ]
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
        *,
        terminal: bool = False,
    ) -> None:
        """A Hermes rejection of a part draft (or of the assembled plan, for its owning
        part), kept for every later redraft and re-review of the part; `terminal` marks
        the provider schema refusal that ended the part (see _with_terminal_refusal)."""
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "DRAFTING":
                return
            parts = copy.deepcopy(dict(row.parts_json or {}))
            entry = self._part_entry(parts, part_id, kind, number)
            entry["rejections"] = [
                item for item in entry["rejections"] if item.get("attempt") != attempt
            ] + [
                {
                    "attempt": attempt,
                    "code": code[:80],
                    "details": list(details)[:50],
                    **({"terminal": True} if terminal else {}),
                }
            ]
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
        assert entry is not None
        if not reviewed_version(entry, version):
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
                        and reviewed_version(entry, version)
                    ):
                        found = {
                            "decomposition_id": str(row.id),
                            "attempt": attempt,
                            "content": copy.deepcopy(content),
                            "content_sha256": version["content_sha256"],
                            "review": copy.deepcopy(version["review"]),
                        }
                        if "fixup" in version:
                            # NEW-244: adopted with its reviewed base, so it stays verifiable.
                            base = next(
                                item
                                for item in entry["versions"]
                                if item.get("attempt") == version["fixup"]["base_attempt"]
                            )
                            found["fixup"] = copy.deepcopy(version["fixup"])
                            found["base"] = {
                                "content": copy.deepcopy(base["content"]),
                                "content_sha256": base["content_sha256"],
                            }
                        return found
        return None

    def previous_unresolved_sync(
        self, decomposition_id: UUID, project: str, part_id: str
    ) -> dict[str, Any] | None:
        """Cross-run continuation (as NEW-142): the most recent earlier, ended plan of the
        project that reviewed a draft of this exact part (same id: same input and pack
        revision) and never approved it, with the part's last reviewed draft and every
        review and Hermes rejection of it in that plan. None when that plan approved the
        part (reuse wins). Plans that never reviewed a draft of the part are skipped; the
        search pages through every earlier ended plan of the project."""
        with self.sessions() as session:
            row = session.get(BacklogDecomposition, decomposition_id)
            if row is None:
                raise DecompositionNotFoundError("backlog decomposition not found")
            query = (
                select(BacklogDecomposition)
                .where(BacklogDecomposition.project == project)
                .where(BacklogDecomposition.id != decomposition_id)
                .where(BacklogDecomposition.status != "DRAFTING")
                .where(BacklogDecomposition.created_at <= row.created_at)
                .order_by(BacklogDecomposition.created_at.desc(), BacklogDecomposition.id)
            )
            # Paged, so plans that never drafted the part do not hide an older one.
            offset = 0
            while True:
                rows = session.scalars(query.offset(offset).limit(REUSE_SEARCH)).all()
                offset += len(rows)
                for other in rows:
                    entry = ((other.parts_json or {}).get("parts") or {}).get(part_id)
                    if not isinstance(entry, dict) or entry.get("reused_from"):
                        continue
                    if entry.get("approved_attempt") is not None:
                        return None
                    found = self._unresolved_source(other, part_id, entry, None, None)
                    if found is not None:
                        return found
                if len(rows) < REUSE_SEARCH:
                    break
        return None

    @staticmethod
    def _unresolved_source(
        row: BacklogDecomposition,
        part_id: str,
        entry: dict[str, Any],
        attempt: int | None,
        content_sha256: str | None,
    ) -> dict[str, Any] | None:
        """The part's last REVISE-reviewed draft in `row` (or exactly draft `attempt` with
        `content_sha256`, for a pinned continuation), its hashes re-verified."""
        if entry.get("approved_attempt") is not None:
            return None
        reviewed = [
            version
            for version in entry.get("versions") or []
            if isinstance(version.get("review"), dict)
            and version["review"].get("decision") == "REVISE"
            and type(version.get("attempt")) is int
            and isinstance(version.get("content"), dict)
            and reviewed_version(entry, version)
            and (attempt is None or version["attempt"] == attempt)
            and (content_sha256 is None or version["content_sha256"] == content_sha256)
        ]
        if not reviewed:
            return None
        last = max(reviewed, key=lambda version: int(version["attempt"]))
        return {
            "decomposition_id": str(row.id),
            "attempt": int(last["attempt"]),
            "content": copy.deepcopy(last["content"]),
            "content_sha256": last["content_sha256"],
            "reviews": [
                {"attempt": version["attempt"], **copy.deepcopy(version["review"])}
                for version in entry.get("versions") or []
                if isinstance(version.get("review"), dict)
            ],
            "rejections": _with_terminal_refusal(row, part_id, entry),
        }

    def pin_continuation_sync(
        self,
        decomposition_id: UUID,
        part_id: str,
        kind: str,
        number: int | None,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        """Pin the earlier plan and draft this part continues (its continued_from, shown
        by backlog-decomposition-status), so every later step of the part uses exactly
        that source. An existing pin wins (idempotent)."""
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "DRAFTING":
                raise ValueError("only a DRAFTING decomposition pins a continuation")
            parts = copy.deepcopy(dict(row.parts_json or {}))
            entry = self._part_entry(parts, part_id, kind, number)
            if not isinstance(entry.get("continued_from"), dict):
                entry["continued_from"] = {
                    "decomposition_id": str(source["decomposition_id"]),
                    "attempt": int(source["attempt"]),
                    "content_sha256": str(source["content_sha256"]),
                }
                row.parts_json = parts
            return dict(entry["continued_from"])

    def pinned_continuation_sync(
        self, decomposition_id: UUID, part_id: str
    ) -> tuple[bool, dict[str, Any] | None]:
        """(whether this part pinned a continuation, the pinned source re-read and
        re-verified: None when it no longer matches its pin)."""
        pin = (self.part_sync(decomposition_id, part_id) or {}).get("continued_from")
        if not isinstance(pin, dict):
            return False, None
        try:
            source_id = UUID(str(pin.get("decomposition_id")))
        except ValueError:
            return True, None
        with self.sessions() as session:
            row = session.get(BacklogDecomposition, source_id)
            entry = ((row.parts_json or {}).get("parts") or {}).get(part_id) if row else None
            if row is None or not isinstance(entry, dict):
                return True, None
            return True, self._unresolved_source(
                row,
                part_id,
                entry,
                int(pin.get("attempt") or 0),
                str(pin.get("content_sha256")),
            )

    def record_part_fixup_sync(
        self,
        decomposition_id: UUID,
        part_id: str,
        base_attempt: int,
        attempt: int,
        content: dict[str, Any],
        changes: list[dict[str, Any]],
        problems: list[Any],
    ) -> str:
        """NEW-244: store a declaration fix-up of the part's approved, strictly reviewed
        version `base_attempt` as version `attempt` (its review and review hash copied
        from the base, `fixup` recording how it derives) and make it the approved one. At
        most one fix-up per part and plan; the changes must reproduce `content`."""
        digest = part_content_sha256(content)
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "DRAFTING":
                raise ValueError("only a DRAFTING decomposition records a fix-up")
            parts = copy.deepcopy(dict(row.parts_json or {}))
            entry = (parts.get("parts") or {}).get(part_id)
            if not isinstance(entry, dict) or entry.get("approved_attempt") != base_attempt:
                raise ValueError("the fix-up's base is not the part's approved version")
            if isinstance(entry.get("fixup"), dict) or any(
                "fixup" in item for item in entry.get("versions") or []
            ):
                raise ValueError("the part already had a fix-up")
            if any(item.get("attempt") == attempt for item in entry.get("versions") or []):
                raise ValueError("the fix-up's attempt is already recorded")
            bases = [item for item in entry["versions"] if item.get("attempt") == base_attempt]
            if len(bases) != 1 or not _strictly_reviewed(bases[0]):
                raise ValueError("the fix-up's base no longer matches its review")
            base = bases[0]
            version = {
                "attempt": attempt,
                "content": content,
                "content_sha256": digest,
                "reviewed_sha256": base["reviewed_sha256"],
                "review": copy.deepcopy(base["review"]),
                "fixup": {
                    "base_attempt": base_attempt,
                    "base_sha256": base["reviewed_sha256"],
                    "changes": changes,
                    "problems": list(problems)[:50],
                },
            }
            entry["versions"].append(version)
            if not _derived_from_review(entry, version):
                raise ValueError("the fix-up's changes do not reproduce its content")
            entry["approved_attempt"] = attempt
            entry["fixup"] = {"status": "APPLIED", "base_attempt": base_attempt, "attempt": attempt}
            row.parts_json = parts
            return digest

    def record_fixup_refusal_sync(
        self, decomposition_id: UUID, part_id: str, base_attempt: int, reasons: list[str]
    ) -> None:
        """NEW-244: a refused declaration fix-up (the part is redrafted instead); it also
        uses the part's one fix-up in this plan."""
        with self.sessions.begin() as session:
            row = self._locked(session, decomposition_id)
            if row.status != "DRAFTING":
                return
            parts = copy.deepcopy(dict(row.parts_json or {}))
            entry = (parts.get("parts") or {}).get(part_id)
            if not isinstance(entry, dict) or isinstance(entry.get("fixup"), dict):
                return
            entry["fixup"] = {
                "status": "REFUSED",
                "base_attempt": base_attempt,
                "reasons": [str(item)[:300] for item in reasons][:20],
            }
            row.parts_json = parts

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
            if isinstance(reused.get("fixup"), dict):
                # NEW-244: a fix-up's version is adopted as attempt 2 over its reviewed
                # base (attempt 1), so its review hash and changes stay verifiable.
                base = reused["base"]
                fixup = reused["fixup"]
                entry["versions"] = [
                    {
                        "attempt": 1,
                        "content": base["content"],
                        "content_sha256": base["content_sha256"],
                        "reviewed_sha256": base["content_sha256"],
                        "review": reused["review"],
                    },
                    {
                        "attempt": 2,
                        "content": reused["content"],
                        "content_sha256": digest,
                        "reviewed_sha256": base["content_sha256"],
                        "review": reused["review"],
                        "fixup": {**fixup, "base_attempt": 1},
                    },
                ]
                entry["approved_attempt"] = 2
                if not reviewed_version(entry, entry["versions"][1]):
                    raise ValueError("the reused fix-up no longer matches its reviewed base")
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
    def _ensure_run(
        session: Session,
        workflow_id: str,
        project: str,
        run_type: str = "backlog-decomposition-plan",
    ) -> None:
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
                run_type=run_type,
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
                "continued_from": entry.get("continued_from"),
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
            # NEW-234 PR6: the fan-out record (runs, superseded tasks, retries, bulk
            # approvals), its active marker and the operator's next commands.
            "fanout": row.fanout_json or {},
            "fanout_workflow_id": row.active_fanout_workflow_id,
            "fanout_sequence": row.fanout_sequence,
            "ready_command": (
                f"engineering-ctl backlog-decomposition-ready {row.id}"
                if row.status == "APPROVED"
                else None
            ),
            "fanout_retry_command": (
                f"engineering-ctl backlog-decomposition-fanout-retry {row.id} {binding} "
                f"{RETRY_FANOUT_PHRASE}"
                if row.status == "APPROVED"
                and ((row.fanout_json or {}).get("last_run") or {}).get("failed_total")
                else None
            ),
        }
