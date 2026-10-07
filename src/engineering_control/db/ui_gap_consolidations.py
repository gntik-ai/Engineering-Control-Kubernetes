"""NEW-154: durable UI gap consolidations -- the stored plan and its audited apply."""

from __future__ import annotations

import asyncio
import hmac
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.change_requests import ChangeRequestRepository
from engineering_control.db.models import (
    ChangeRequest,
    DashboardEvent,
    Run,
    UiBlueprint,
    UiGapConsolidation,
)
from engineering_control.db.ui_blueprints import (
    UiBlueprintNotFoundError,
    UiBlueprintRepository,
    UiFanoutActiveError,
    blueprint_canonical_items,
)
from engineering_control.engineering.backlog import is_reauthenticated_backlog_operator
from engineering_control.engineering.change_requests import (
    ChangeReviewRecord,
    UiChangeContext,
    prepare_intake,
    render_change_request,
    ui_context_json,
    validate_proposal,
)
from engineering_control.engineering.ui_blueprint import (
    FanoutItem,
    fanout_items,
    item_change_request_id,
    item_description,
)
from engineering_control.engineering.ui_gap_consolidation import (
    CONSOLIDATE_PHRASE,
    CONSOLIDATION_KINDS,
    MERGED,
    UI_GAP_INPUTS_CHANGED,
    UI_GAP_NOTHING_TO_CONSOLIDATE,
    ConsolidationRejected,
    build_plan,
    candidate,
    canonical_context,
    canonical_item,
    canonical_record,
    check_plan,
    consolidation_workflow_id,
    gap_links,
    inputs_sha256,
    is_mergeable,
    plan_preview,
    plan_sha256,
)

_SHA12 = re.compile(r"^[a-f0-9]{12}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_OPEN = ("PLANNING", "PROPOSED")


class UiGapConsolidationNotFoundError(ValueError):
    """The requested consolidation (or plan) does not exist."""


@dataclass
class GapState:
    """The gap requests of one blueprint at one moment (blueprint gaps and canonicals)."""

    items: dict[str, FanoutItem] = field(default_factory=dict)
    ids: dict[str, UUID] = field(default_factory=dict)
    rows: dict[str, ChangeRequest] = field(default_factory=dict)
    known: dict[str, str] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)


class UiGapConsolidationRepository:
    """Every consolidation transition, re-checked under the blueprint row lock."""

    def __init__(self, database_url: str | None = None, *, engine: Engine | None = None) -> None:
        if engine is None:
            if database_url is None:
                raise ValueError("ui gap consolidation repository requires a database")
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

    # Gap state ------------------------------------------------------------------------------

    @staticmethod
    def _gap_state(session: Session, blueprint: UiBlueprint, kind: str | None) -> GapState:
        """Every gap request of the blueprint (its gaps and earlier canonicals) of `kind`, with
        the mergeable ones as plan candidates in blueprint order."""
        document = dict(blueprint.blueprint_json or {})
        digest = str(blueprint.blueprint_sha256)
        items = [
            item
            for item in [*fanout_items(document), *blueprint_canonical_items(session, blueprint.id)]
            if item.is_gap and (kind is None or item.kind == kind)
        ]
        state = GapState(
            items={item.key: item for item in items},
            ids={item.key: item_change_request_id(digest, item.key) for item in items},
        )
        rows = {
            row.id: row
            for row in session.scalars(
                select(ChangeRequest).where(ChangeRequest.id.in_(list(state.ids.values())))
            ).all()
        }
        for item in items:
            row = rows.get(state.ids[item.key])
            context = (row.ui_context_json if row is not None else None) or {}
            if (
                row is None
                or context.get("blueprint_id") != str(blueprint.id)
                or context.get("item_key") != item.key
            ):
                state.known[item.key] = "NOT_CREATED"
                continue
            state.rows[item.key] = row
            state.known[item.key] = row.status
            if not is_mergeable(row.status, row.revision, row.issue_url):
                continue
            links = (
                list(item.spec.get("needed_by") or [])
                if item.spec.get("variants")
                else gap_links(document, str(item.spec.get("id")))
            )
            state.candidates.append(
                candidate(
                    item,
                    change_request_id=row.id,
                    status=row.status,
                    revision=row.revision,
                    proposal=row.proposal_json if isinstance(row.proposal_json, dict) else None,
                    linked=links,
                )
            )
        return state

    @staticmethod
    def _input_sha(blueprint: UiBlueprint, kind: str | None, state: GapState) -> str:
        return inputs_sha256(
            blueprint_id=str(blueprint.id),
            blueprint_sha256=str(blueprint.blueprint_sha256),
            kind=kind,
            candidates=state.candidates,
        )

    @staticmethod
    def _approved(session: Session, blueprint_id: UUID, *, lock: bool) -> UiBlueprint:
        if lock:
            row = UiBlueprintRepository._locked(session, blueprint_id)
        else:
            found = session.get(UiBlueprint, blueprint_id)
            if found is None:
                raise UiBlueprintNotFoundError("ui blueprint not found")
            row = found
        if row.status != "APPROVED":
            raise ValueError("only an APPROVED ui blueprint's gap requests can be consolidated")
        UiBlueprintRepository._verify(row)
        return row

    # Operator: plan --------------------------------------------------------------------------

    async def request_plan(
        self, blueprint_id: UUID, actor: str, kind: str | None, replan: bool
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.request_plan_sync, blueprint_id, actor, kind, replan)

    def request_plan_sync(
        self, blueprint_id: UUID, actor: str, kind: str | None, replan: bool = False
    ) -> dict[str, Any]:
        """The current plan for the blueprint's mergeable gap requests (REUSE), or a new
        PLANNING record whose workflow the caller starts (START). One plan per input set:
        a repeated preview never runs the model again unless `replan` is asked for."""
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("ui gap consolidation requires an allowlisted operator channel")
        if kind is not None and kind not in CONSOLIDATION_KINDS:
            raise ValueError("ui gap consolidation kind is not known")
        with self.sessions.begin() as session:
            blueprint = self._approved(session, blueprint_id, lock=True)
            state = self._gap_state(session, blueprint, kind)
            if len(state.candidates) < 2:
                raise ValueError(
                    f"{UI_GAP_NOTHING_TO_CONSOLIDATE}: fewer than two mergeable gap requests"
                )
            digest = self._input_sha(blueprint, kind, state)
            open_rows = session.scalars(
                select(UiGapConsolidation)
                .where(UiGapConsolidation.blueprint_id == blueprint.id)
                .where(UiGapConsolidation.status.in_(_OPEN))
                .with_for_update()
            ).all()
            same = [
                row
                for row in open_rows
                if row.kind == kind and row.input_sha256 == digest and not replan
            ]
            if same:
                row = max(same, key=lambda item: item.created_at)
                action = "START" if row.status == "PLANNING" else "REUSE"
                return {"action": action, "consolidation": self._view(row)}
            now = datetime.now(UTC)
            for row in open_rows:
                if row.kind == kind:
                    # Planned for other inputs (or replaced): never applicable any more.
                    row.status = "SUPERSEDED"
                    row.updated_at = now
            identity = uuid4()
            row = UiGapConsolidation(
                id=identity,
                blueprint_id=blueprint.id,
                project=blueprint.project,
                kind=kind,
                status="PLANNING",
                workflow_id=consolidation_workflow_id(str(identity)),
                requested_by=actor,
                input_sha256=digest,
                inputs_json=state.candidates,
                problems_json=[],
                result_json={},
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return {"action": "START", "consolidation": self._view(row)}

    async def record_start_failure(self, consolidation_id: UUID) -> None:
        await asyncio.to_thread(
            self.record_failure_sync, consolidation_id, "WORKFLOW_START_FAILED", None
        )

    async def list_consolidations(self, blueprint_id: UUID) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_sync, blueprint_id)

    def list_sync(self, blueprint_id: UUID) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(UiGapConsolidation)
                .where(UiGapConsolidation.blueprint_id == blueprint_id)
                .order_by(UiGapConsolidation.created_at.desc())
                .limit(20)
            ).all()
            return [self._view(row) for row in rows]

    def get_sync(self, consolidation_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(UiGapConsolidation, consolidation_id)
            return self._view(row) if row is not None else None

    # Controller activities: plan ---------------------------------------------------------------

    def load_sync(self, consolidation_id: UUID, workflow_id: str) -> dict[str, Any]:
        """Open the plan step: the record must still be PLANNING for unchanged inputs."""
        with self.sessions.begin() as session:
            row = session.get(UiGapConsolidation, consolidation_id, with_for_update=True)
            if row is None:
                raise UiGapConsolidationNotFoundError("ui gap consolidation not found")
            if row.status != "PLANNING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            if workflow_id != row.workflow_id:
                raise ValueError("ui gap consolidation workflow identity does not match")
            try:
                blueprint = self._approved(session, row.blueprint_id, lock=False)
            except ValueError:
                self._fail(row, "UI_BLUEPRINT_NOT_APPROVED", [])
                return {"status": "BLOCKED", "blocker": "UI_BLUEPRINT_NOT_APPROVED"}
            state = self._gap_state(session, blueprint, row.kind)
            if self._input_sha(blueprint, row.kind, state) != row.input_sha256:
                row.status = "SUPERSEDED"
                row.error_code = UI_GAP_INPUTS_CHANGED
                return {"status": "BLOCKED", "blocker": UI_GAP_INPUTS_CHANGED}
            UiBlueprintRepository._ensure_run(
                session, row.workflow_id, row.project, "ui-gap-consolidation"
            )
            return {
                "status": "READY",
                "project": row.project,
                "blueprint_id": str(row.blueprint_id),
                "kind": row.kind,
                "input_sha256": row.input_sha256,
                "candidate_count": len(state.candidates),
            }

    def plan_inputs_sync(self, consolidation_id: UUID) -> dict[str, Any]:
        with self.sessions() as session:
            row = session.get(UiGapConsolidation, consolidation_id)
            if row is None:
                raise UiGapConsolidationNotFoundError("ui gap consolidation not found")
            return {
                "status": row.status,
                "input_sha256": row.input_sha256,
                "candidates": list(row.inputs_json or []),
                "kind": row.kind,
            }

    def record_plan_sync(
        self, consolidation_id: UUID, structured: Any, drafter: dict[str, Any]
    ) -> dict[str, Any]:
        """Check the drafted plan deterministically and store it (PROPOSED) with its sha256,
        or keep why it was refused (INVALID)."""
        with self.sessions.begin() as session:
            row = session.get(UiGapConsolidation, consolidation_id, with_for_update=True)
            if row is None:
                raise UiGapConsolidationNotFoundError("ui gap consolidation not found")
            if row.status != "PLANNING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            blueprint = self._approved(session, row.blueprint_id, lock=False)
            state = self._gap_state(session, blueprint, row.kind)
            if self._input_sha(blueprint, row.kind, state) != row.input_sha256:
                row.status = "SUPERSEDED"
                row.error_code = UI_GAP_INPUTS_CHANGED
                return {"status": "BLOCKED", "blocker": UI_GAP_INPUTS_CHANGED}
            try:
                checked = check_plan(
                    structured,
                    input_sha256=row.input_sha256,
                    candidates=list(row.inputs_json or []),
                    known=state.known,
                )
            except ConsolidationRejected as rejected:
                row.status = "INVALID"
                row.error_code = rejected.code
                row.problems_json = list(rejected.details)
                return {
                    "status": "INVALID",
                    "blocker": rejected.code,
                    "problems": list(rejected.details),
                }
            plan = build_plan(
                checked,
                blueprint_id=str(blueprint.id),
                blueprint_sha256=str(blueprint.blueprint_sha256),
                kind=row.kind,
                input_sha256=row.input_sha256,
                candidates=list(row.inputs_json or []),
                drafter=drafter,
            )
            digest = plan_sha256(plan)
            row.plan_json = plan
            row.plan_sha256 = digest
            row.status = "PROPOSED"
            row.error_code = None
            row.problems_json = []
            session.add(
                self._event(
                    row,
                    "ui_gap_consolidation.proposed",
                    f"UI gap consolidation plan {digest[:12]}: {len(plan['clusters'])} clusters",
                    {"plan_sha256": digest, "clusters": len(plan["clusters"])},
                )
            )
            return {
                "status": "PROPOSED",
                "plan_sha256": digest,
                "clusters": len(plan["clusters"]),
                "merged": sum(len(cluster["members"]) for cluster in plan["clusters"]),
            }

    def record_failure_sync(
        self, consolidation_id: UUID, error_code: str, detail: str | None
    ) -> dict[str, Any]:
        with self.sessions.begin() as session:
            row = session.get(UiGapConsolidation, consolidation_id, with_for_update=True)
            if row is None:
                raise UiGapConsolidationNotFoundError("ui gap consolidation not found")
            if row.status != "PLANNING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            self._fail(row, error_code, [detail] if detail else [])
            return {"status": "FAILED", "error_code": row.error_code}

    @staticmethod
    def _fail(row: UiGapConsolidation, error_code: str, details: list[str]) -> None:
        row.status = "FAILED"
        row.error_code = error_code if _ERROR_CODE.fullmatch(error_code) else "UI_GAP_PLAN_FAILED"
        row.problems_json = [str(item)[:500] for item in details][:20]

    def finalize_run_sync(self, workflow_id: str, status: str, error_code: str | None) -> bool:
        if not workflow_id.startswith("ui-gap-consolidate-"):
            return False
        with self.sessions.begin() as session:
            runs = session.scalars(select(Run).where(Run.workflow_id == workflow_id)).all()
            now = datetime.now(UTC)
            for run in runs:
                run.status = status
                run.error_code = error_code
                run.finished_at = now
            return bool(runs)

    # Operator: apply ---------------------------------------------------------------------------

    async def check_apply(
        self, blueprint_id: UUID, plan_sha12: str, phrase: str, actor: str
    ) -> None:
        await asyncio.to_thread(self.check_apply_sync, blueprint_id, plan_sha12, phrase, actor)

    def check_apply_sync(
        self, blueprint_id: UUID, plan_sha12: str, phrase: str, actor: str
    ) -> None:
        """The apply's authorization, checked before a stale fan-out marker is released."""
        with self.sessions() as session:
            self._authorized(session, blueprint_id, plan_sha12, phrase, actor, lock=False)

    def _authorized(
        self,
        session: Session,
        blueprint_id: UUID,
        plan_sha12: str,
        phrase: str,
        actor: str,
        *,
        lock: bool,
    ) -> tuple[UiBlueprint, UiGapConsolidation]:
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("ui gap consolidation requires an allowlisted operator channel")
        if not hmac.compare_digest(CONSOLIDATE_PHRASE, phrase):
            raise ValueError(f"typed confirmation must be {CONSOLIDATE_PHRASE}")
        if not _SHA12.fullmatch(plan_sha12):
            raise ValueError("plan sha must be the 12 hex characters the preview shows")
        blueprint = self._approved(session, blueprint_id, lock=lock)
        query = (
            select(UiGapConsolidation)
            .where(UiGapConsolidation.blueprint_id == blueprint.id)
            .where(UiGapConsolidation.status.in_(("PROPOSED", "APPLIED")))
        )
        rows = session.scalars(query.with_for_update() if lock else query).all()
        matches = [row for row in rows if (row.plan_sha256 or "").startswith(plan_sha12)]
        if len(matches) != 1:
            raise UiGapConsolidationNotFoundError(
                f"no proposed gap consolidation plan {plan_sha12} for this blueprint"
            )
        row = matches[0]
        if not isinstance(row.plan_json, dict) or plan_sha256(row.plan_json) != row.plan_sha256:
            raise ValueError("stored gap consolidation plan no longer matches its sha256")
        return blueprint, row

    async def apply(
        self, blueprint_id: UUID, plan_sha12: str, phrase: str, actor: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.apply_sync, blueprint_id, plan_sha12, phrase, actor)

    def apply_sync(
        self, blueprint_id: UUID, plan_sha12: str, phrase: str, actor: str
    ) -> dict[str, Any]:
        """Apply a PROPOSED plan in one audited transaction (idempotent for its sha):
        create one canonical request per cluster (DRAFTING, drafted by the next fan-out),
        mark every member MERGED with merged_into, and re-point every blocked_by entry that
        names a member. Published, rejected and merged requests keep their (bound) content
        and record the merged reference instead. Refused while a fan-out holds the
        blueprint's active marker, and refused whole if any member is no longer mergeable."""
        with self.sessions.begin() as session:
            blueprint, row = self._authorized(
                session, blueprint_id, plan_sha12, phrase, actor, lock=True
            )
            if row.status == "APPLIED":
                return {**self._view(row), "replayed": True}
            if blueprint.active_fanout_workflow_id:
                raise UiFanoutActiveError(blueprint.active_fanout_workflow_id)
            plan = dict(row.plan_json or {})
            digest = str(blueprint.blueprint_sha256)
            if plan.get("blueprint_sha256") != digest:
                raise ValueError("gap consolidation plan belongs to another blueprint version")
            # Lock the members first (an operator approve or revise of one waits, or wins
            # and the member check below refuses), then read the current gap state.
            members = [
                UUID(str(member["change_request_id"]))
                for cluster in plan.get("clusters") or []
                for member in cluster.get("members") or []
            ]
            session.scalars(
                select(ChangeRequest).where(ChangeRequest.id.in_(members)).with_for_update()
            ).all()
            state = self._gap_state(session, blueprint, None)
            self._check_members(plan, state, digest)
            now = datetime.now(UTC)
            sha = str(row.plan_sha256)
            stamp = {
                "consolidation_id": str(row.id),
                "plan_sha256": sha,
                "at": now.isoformat(),
                "actor": actor,
            }
            variants = {item["item_key"]: item for item in state.candidates}
            merge_map: dict[str, dict[str, str]] = {}
            canonicals: list[dict[str, Any]] = []
            for cluster in plan["clusters"]:
                record = canonical_record(cluster, member_items=state.items, variants=variants)
                self._create_canonical(session, blueprint, record, actor, stamp, now)
                canonicals.append(record)
                for member in cluster["members"]:
                    request = state.rows[member["item_key"]]
                    previous = {"status": request.status, "revision": request.revision}
                    request.status = MERGED
                    request.merged_into_id = UUID(record["canonical_id"])
                    request.decided_by = actor
                    request.decided_at = now
                    request.decision_reason = (
                        f"merged into {record['canonical_id']} ({record['cluster_key']}) by "
                        f"gap consolidation {sha[:12]}"
                    )
                    request.decision_sequence += 1
                    request.error_code = None
                    request.updated_at = now
                    request.consolidation_json = {
                        **dict(request.consolidation_json or {}),
                        "merged": {
                            **stamp,
                            "merged_into": record["canonical_id"],
                            "cluster_key": record["cluster_key"],
                            "previous": previous,
                        },
                    }
                    merge_map[str(request.id)] = {
                        "change_request_id": record["canonical_id"],
                        "item_key": record["cluster_key"],
                    }
            references = self._repoint(session, blueprint, merge_map, actor, stamp, now)
            result = {
                **stamp,
                "canonicals": canonicals,
                "merged": sorted(merge_map),
                **references,
            }
            row.status = "APPLIED"
            row.applied_by = actor
            row.applied_at = now
            row.result_json = result
            row.updated_at = now
            session.add(
                self._event(
                    row,
                    "ui_gap_consolidation.applied",
                    f"UI gap consolidation {sha[:12]} applied: {len(merge_map)} gap requests "
                    f"merged into {len(canonicals)}",
                    {
                        "plan_sha256": sha,
                        "actor": actor,
                        "canonical_ids": [item["canonical_id"] for item in canonicals],
                        "merged_ids": sorted(merge_map),
                        "repointed": len(references["repointed"]),
                        "reopened": references["reopened"],
                        "merged_references": len(references["merged_references"]),
                    },
                )
            )
            session.flush()
            return {**self._view(row), "replayed": False}

    @staticmethod
    def _check_members(plan: dict[str, Any], state: GapState, digest: str) -> None:
        problems: list[str] = []
        seen: set[str] = set()
        blueprint_keys = {key for key, item in state.items.items() if not item.spec.get("variants")}
        for cluster in plan.get("clusters") or []:
            if cluster["cluster_key"] in blueprint_keys:
                problems.append(f"{cluster['cluster_key']}: collides with a blueprint gap")
            repositories = {cluster["target_repository"]}
            for member in cluster["members"]:
                key = member["item_key"]
                if key in seen:
                    problems.append(f"{key}: in more than one cluster")
                seen.add(key)
                request = state.rows.get(key)
                if (
                    request is None
                    or str(request.id) != member["change_request_id"]
                    or request.id != item_change_request_id(digest, key)
                ):
                    problems.append(f"{key}: not a gap request of this blueprint")
                    continue
                if not is_mergeable(request.status, request.revision, request.issue_url):
                    problems.append(f"{key}: is {request.status} and cannot be merged")
                repositories.add(request.target_repository)
                if state.items[key].kind != cluster["item_kind"]:
                    problems.append(f"{key}: is not a {cluster['item_kind']}")
            if len(repositories) > 1:
                names = ", ".join(sorted(repositories))
                problems.append(f"{cluster['cluster_key']}: mixes repositories {names}")
        if problems:
            raise ConsolidationRejected("UI_GAP_APPLY_REFUSED", problems)

    @staticmethod
    def _create_canonical(
        session: Session,
        blueprint: UiBlueprint,
        record: dict[str, Any],
        actor: str,
        stamp: dict[str, Any],
        now: datetime,
    ) -> None:
        """The canonical request, exactly as the fan-out's prepare_item would create it."""
        item = canonical_item(record)
        identity = UUID(record["canonical_id"])
        context = canonical_context(
            item, blueprint_id=str(blueprint.id), blueprint_sha256=str(blueprint.blueprint_sha256)
        )
        existing = session.get(ChangeRequest, identity, with_for_update=True)
        if existing is not None:
            raise ConsolidationRejected(
                "UI_GAP_APPLY_REFUSED", [f"{record['cluster_key']}: canonical request exists"]
            )
        intake = prepare_intake(
            blueprint.project,
            item_description(item, blueprint_id=str(blueprint.id)),
            item.target_repository,
        )
        session.add(
            ChangeRequest(
                id=identity,
                request_key=f"ui-{identity.hex}",
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
                requested_by=actor,
                ui_context_json=ui_context_json(context),
                consolidation_json={
                    "canonical": {
                        **stamp,
                        "cluster_key": record["cluster_key"],
                        "members": list(record["spec"]["members"]),
                    }
                },
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()

    @staticmethod
    def _repoint(
        session: Session,
        blueprint: UiBlueprint,
        merge_map: dict[str, dict[str, str]],
        actor: str,
        stamp: dict[str, Any],
        now: datetime,
    ) -> dict[str, list[Any]]:
        """Re-point every blocked_by entry naming a merged member to its canonical request.

        DRAFTING and FAILED requests only change their context; CANDIDATE requests are
        rendered again (new binding); an APPROVED (unpublished) request is rendered again
        and goes back to CANDIDATE for a fresh approval. PUBLISHED, REJECTED and MERGED
        requests keep their bound content: the merged reference is recorded instead, and
        blocker resolution follows merged_into."""
        repointed: list[dict[str, Any]] = []
        merged_references: list[dict[str, Any]] = []
        reopened: list[str] = []
        rows = session.scalars(
            select(ChangeRequest)
            .where(ChangeRequest.project == blueprint.project)
            .where(ChangeRequest.ui_context_json.is_not(None))
            .with_for_update()
        ).all()
        for request in rows:
            context = dict(request.ui_context_json or {})
            if context.get("blueprint_id") != str(blueprint.id):
                continue
            dependencies = list(context.get("blocked_by") or [])
            hits = [
                {
                    "from": entry["change_request_id"],
                    "from_key": entry["item_key"],
                    "to": merge_map[entry["change_request_id"]]["change_request_id"],
                    "to_key": merge_map[entry["change_request_id"]]["item_key"],
                }
                for entry in dependencies
                if entry.get("change_request_id") in merge_map
            ]
            if not hits:
                continue
            note = {**stamp, "entries": hits}
            if request.status in {"PUBLISHED", "REJECTED", MERGED}:
                request.consolidation_json = {
                    **dict(request.consolidation_json or {}),
                    "merged_references": [
                        *list((request.consolidation_json or {}).get("merged_references") or []),
                        note,
                    ],
                }
                merged_references.append(
                    {
                        "change_request_id": str(request.id),
                        "item_key": context.get("item_key"),
                        "status": request.status,
                        "issue_url": request.issue_url,
                        "entries": hits,
                    }
                )
                continue
            replaced: dict[str, dict[str, str]] = {}
            for entry in dependencies:
                target = merge_map.get(entry["change_request_id"], entry)
                replaced.setdefault(
                    target["change_request_id"],
                    {
                        "change_request_id": target["change_request_id"],
                        "item_key": target["item_key"],
                    },
                )
            context["blocked_by"] = list(replaced.values())
            request.ui_context_json = context
            request.consolidation_json = {
                **dict(request.consolidation_json or {}),
                "repointed": [
                    *list((request.consolidation_json or {}).get("repointed") or []),
                    note,
                ],
            }
            request.updated_at = now
            if request.status in {"CANDIDATE", "APPROVED"}:
                UiGapConsolidationRepository._render_again(request, context)
                if request.status == "APPROVED":
                    request.status = "CANDIDATE"
                    request.approval_snapshot_json = None
                    request.decision_sequence += 1
                    request.decided_by = actor
                    request.decided_at = now
                    request.decision_reason = (
                        "blocked_by re-pointed by gap consolidation "
                        f"{str(stamp['plan_sha256'])[:12]}; approve it again"
                    )
                    reopened.append(str(request.id))
            repointed.append(
                {
                    "change_request_id": str(request.id),
                    "item_key": context.get("item_key"),
                    "status": request.status,
                    "entries": hits,
                }
            )
        return {
            "repointed": repointed,
            "merged_references": merged_references,
            "reopened": reopened,
        }

    @staticmethod
    def _render_again(request: ChangeRequest, context: dict[str, Any]) -> None:
        proposal = validate_proposal(
            request.proposal_json, request.project, request.target_repository
        )
        rendered = render_change_request(
            change_request_id=str(request.id),
            revision=request.revision,
            project=request.project,
            target_repository=request.target_repository,
            proposal=proposal,
            review=ChangeReviewRecord.model_validate(request.review_json),
            description_redacted=request.description_redacted,
            ui_context=UiChangeContext.model_validate(context),
        )
        error = ChangeRequestRepository._render_error(rendered)
        if error is not None:
            raise ConsolidationRejected(
                "UI_GAP_APPLY_REFUSED", [f"{request.id}: re-pointed rendering fails ({error})"]
            )
        request.fingerprint = rendered.fingerprint
        request.issue_title = rendered.title
        request.issue_body = rendered.body
        request.approval_binding_sha256 = rendered.approval_binding

    # Views -----------------------------------------------------------------------------------

    @staticmethod
    def _event(
        row: UiGapConsolidation, event_type: str, message: str, metadata: dict[str, Any]
    ) -> DashboardEvent:
        return DashboardEvent(
            occurred_at=datetime.now(UTC),
            level="INFO",
            event_type=event_type,
            source="controller",
            worker_kind="controller",
            project=row.project,
            entity_type="ui_blueprint",
            entity_id=str(row.blueprint_id),
            current_phase="UI_GAP_CONSOLIDATION",
            message=message,
            metadata_json={"consolidation_id": str(row.id), "kind": row.kind, **metadata},
        )

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()

    @classmethod
    def summary(cls, row: UiGapConsolidation) -> dict[str, Any]:
        plan = row.plan_json if isinstance(row.plan_json, dict) else {}
        clusters = plan.get("clusters") or []
        return {
            "id": str(row.id),
            "kind": row.kind,
            "status": row.status,
            "plan_sha12": (row.plan_sha256 or "")[:12] or None,
            "clusters": len(clusters),
            "members": sum(len(cluster.get("members") or []) for cluster in clusters),
            "error_code": row.error_code,
            "created_at": cls._iso(row.created_at),
            "applied_at": cls._iso(row.applied_at),
        }

    @classmethod
    def _view(cls, row: UiGapConsolidation) -> dict[str, Any]:
        plan = row.plan_json if isinstance(row.plan_json, dict) else None
        sha12 = (row.plan_sha256 or "")[:12]
        kind = f" --kind {row.kind}" if row.kind else ""
        return {
            **cls.summary(row),
            "blueprint_id": str(row.blueprint_id),
            "project": row.project,
            "workflow_id": row.workflow_id,
            "requested_by": row.requested_by,
            "input_sha256": row.input_sha256,
            "candidates": len(row.inputs_json or []),
            "plan_sha256": row.plan_sha256,
            "plan": plan,
            "preview": plan_preview(plan) if plan else [],
            "problems": list(row.problems_json or []),
            "apply_command": (
                f"engineering-ctl ui-blueprint-gap-consolidate {row.blueprint_id}{kind} "
                f"--apply {sha12} {CONSOLIDATE_PHRASE}"
                if row.status == "PROPOSED" and sha12
                else None
            ),
            "applied_by": row.applied_by,
            "result": row.result_json or None,
        }
