"""BacklogDecompositionPlanWorkflow exactly as origin/main e8771c5 ran it, before the
per-part rejection budget (decomposition-part-rejection-budget-v1).

Copied verbatim from that commit's workflows/definitions.py (only the class name and
``sandboxed=False`` differ) so replay tests record genuinely pre-change histories (three
drafts in all per part, a Hermes rejection included). Do not edit.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError

from engineering_control.workflows.definitions import SINGLE_ATTEMPT, ControlledWorkflow
from engineering_control.workflows.inputs import BacklogDecompositionPlanInput


@workflow.defn(name="BacklogDecompositionPlanWorkflow", sandboxed=False)
class PreRejectionBudgetDecompositionPlanWorkflow(ControlledWorkflow):
    """NEW-234 PR5 flow D: the backlog decomposition plan, persisted as an approvable
    candidate. Modelled on UiMigrationPlanWorkflow's batched plan (a new workflow type: the
    UI workflows and their patch markers are untouched).

    snapshot at the pinned source SHA -> read-only GitHub issue snapshot -> deterministic
    inventory -> the frame, then one batch per epic (PR 4's epic_order): Claude drafts
    (decompose-backlog), Codex reviews (review-decomposition), at most three drafts and
    three reviews per part -> assembly, where a whole-plan problem a part owns goes back to
    that part as a redraft -> CANDIDATE with its approval binding. It never approves,
    publishes or edits anything.
    """

    # Design section 3 (engineering.decomposition.MAX_PART_DRAFTS / MAX_PART_REVIEWS).
    _MAX_PART_DRAFTS = 3
    _ASSEMBLY_ROUNDS = 3
    # Per part provider call: the request's own timeout is PART_TIMEOUT_SECONDS (1800 s).
    _PART_TIMEOUT_MINUTES = 40
    _STRUCTURAL_REJECTIONS = frozenset(
        {"DRAFT_SCHEMA_INVALID", "DRAFT_STRUCTURE_INVALID", "DRAFT_BINDING_MISMATCH"}
    )
    # A provider call runs once per budget reservation (review: Temporal retries and
    # capacity retries would execute it again uncharged). A failed call ends the plan; a
    # new plan reuses every part already approved.

    @workflow.run
    async def run(self, request: BacklogDecompositionPlanInput) -> dict[str, Any]:
        try:
            result = await self._plan(request)
        except ActivityError:
            result = await self._decomposition_terminal(
                {"decomposition_id": request.decomposition_id, "project": request.project},
                "FAILED",
                "DECOMPOSITION_STEP_FAILED",
            )
        try:
            await self._step(
                "decomposition.finalize_run",
                {
                    "decomposition_id": request.decomposition_id,
                    "terminal_status": result.get("status"),
                    "error_code": result.get("error_code"),
                },
            )
        except ActivityError:
            workflow.logger.warning(
                "decomposition %s: Run finalization failed", request.decomposition_id
            )
        return result

    async def _decomposition_terminal(
        self,
        context: dict[str, Any],
        status: str,
        blocker: str,
        blockers: list[Any] | None = None,
        *,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        recorded = await self._step(
            "decomposition.record_terminal",
            {
                "decomposition_id": context.get("decomposition_id"),
                "project": context.get("project"),
                "terminal_status": status,
                "blocker": blocker,
                "blockers": list(blockers or [])[:50],
                **({"part_usage": usage} if usage else {}),
            },
        )
        return {
            "status": recorded.get("status", status),
            "decomposition_id": context.get("decomposition_id"),
            "error_code": recorded.get("error_code", blocker),
            "blockers": list(blockers or [])[:50],
        }

    async def _plan(self, request: BacklogDecompositionPlanInput) -> dict[str, Any]:
        context = await self._step("decomposition.register", asdict(request))
        if context.get("status") == "BLOCKED":
            return {
                "status": "BLOCKED",
                "decomposition_id": request.decomposition_id,
                "error_code": context.get("blocker"),
                "blockers": context.get("blockers", []),
            }
        if context.get("status") != "REGISTERED":
            return {"status": "SUPERSEDED", "decomposition_id": request.decomposition_id}
        if not await self._checkpoint("DECOMPOSITION_SNAPSHOT"):
            return await self._decomposition_terminal(context, "FAILED", "DECOMPOSITION_CANCELLED")
        try:
            snapshot = await self._step(
                "project.snapshot",
                {
                    "project": request.project,
                    "campaign_id": context["campaign_id"],
                    "source_sha": context.get("source_sha"),
                },
                timeout_minutes=60,
            )
        except ActivityError:
            return await self._decomposition_terminal(
                context, "BLOCKED", "DECOMPOSITION_SNAPSHOT_UNAVAILABLE"
            )
        source = {
            "source_worktree": snapshot.get("source_worktree"),
            "source_sha": snapshot.get("source_sha"),
        }
        if not await self._checkpoint("DECOMPOSITION_INVENTORY"):
            return await self._decomposition_terminal(context, "FAILED", "DECOMPOSITION_CANCELLED")
        issues = await self._step(
            "decomposition.read_issues",
            {"project": request.project, "decomposition_id": request.decomposition_id},
        )
        if issues.get("status") != "ISSUES_READ":
            return await self._decomposition_terminal(
                context,
                "BLOCKED",
                str(issues.get("blocker") or "DECOMPOSITION_ISSUES_UNAVAILABLE"),
                [str(issues.get("reason") or "")[:300]],
            )
        inventory = await self._step(
            "decomposition.inventory",
            {
                "project": request.project,
                "decomposition_id": request.decomposition_id,
                "snapshot": source,
                "issues": issues.get("issues"),
            },
        )
        if inventory.get("status") == "BLOCKED":
            return await self._decomposition_terminal(
                context,
                "BLOCKED",
                str(inventory.get("blocker") or "DECOMPOSITION_SOURCE_INVALID"),
                inventory.get("blockers"),
            )
        if inventory.get("status") != "INVENTORY_PASS":
            return await self._decomposition_terminal(
                context, "FAILED", "DECOMPOSITION_INVENTORY_FAILED"
            )
        recorded = await self._step(
            "decomposition.record_inputs",
            {**context, **source, "inventory": inventory.get("inventory")},
        )
        if recorded.get("status") in {"BLOCKED", "FAILED"}:
            return await self._decomposition_terminal(
                context,
                str(recorded.get("status")),
                str(recorded.get("blocker") or "DECOMPOSITION_INVENTORY_MISMATCH"),
                recorded.get("blockers"),
            )
        if recorded.get("status") != "INPUTS_RECORDED":
            return {"status": "SUPERSEDED", "decomposition_id": request.decomposition_id}
        context = {key: value for key, value in recorded.items() if key != "status"}
        approved: list[dict[str, Any]] = []
        for part in list(context.get("parts") or []):
            outcome = await self._decomposition_part(
                request, {**context, "approved_parts": list(approved)}, part
            )
            if outcome.get("status") != "PART_APPROVED":
                return await self._part_terminal(context, outcome)
            approved.append(self._approved_entry(part, outcome))
        assembled: dict[str, Any] = {}
        for assembly_round in range(self._ASSEMBLY_ROUNDS + 1):
            if not await self._checkpoint("DECOMPOSITION_ASSEMBLE"):
                return await self._decomposition_terminal(
                    context, "FAILED", "DECOMPOSITION_CANCELLED"
                )
            assembled = await self._step(
                "decomposition.assemble", {**context, "approved_parts": approved}
            )
            if assembled.get("status") == "ASSEMBLED":
                break
            redrafts = list(assembled.get("redrafts") or [])
            if (
                assembled.get("status") != "ASSEMBLY_REDRAFT"
                or assembly_round == self._ASSEMBLY_ROUNDS
            ):
                return await self._decomposition_terminal(
                    context,
                    "BLOCKED",
                    str(assembled.get("blocker") or "ASSEMBLY_INVALID"),
                    assembled.get("blockers") or self._redraft_blockers(redrafts),
                )
            for redraft in redrafts:
                # A whole-plan problem goes back to the part that owns it, as its next
                # draft (the part's draft budget is shared with its review rounds).
                index = next(
                    (
                        position
                        for position, item in enumerate(approved)
                        if item["part_id"] == redraft.get("part_id")
                    ),
                    None,
                )
                if index is None:
                    return await self._decomposition_terminal(
                        context, "FAILED", "DECOMPOSITION_PART_UNKNOWN"
                    )
                entry = approved[index]
                if int(entry["attempt"]) >= self._MAX_PART_DRAFTS:
                    return await self._decomposition_terminal(
                        context, "BLOCKED", "ASSEMBLY_INVALID", self._redraft_blockers([redraft])
                    )
                outcome = await self._decomposition_part(
                    request,
                    {**context, "approved_parts": approved[:index]},
                    {"kind": entry["kind"], "number": entry["number"]},
                    redraft={
                        "part_id": entry["part_id"],
                        "part_input": entry["part_input"],
                        "part_input_files": entry.get("part_input_files"),
                        "first_attempt": int(entry["attempt"]) + 1,
                        "rejection": {
                            "code": "ASSEMBLY_INVALID",
                            "details": list(redraft.get("problems") or [])[:50],
                        },
                    },
                )
                if outcome.get("status") != "PART_APPROVED":
                    return await self._part_terminal(context, outcome)
                approved[index] = self._approved_entry(
                    {"kind": entry["kind"], "number": entry["number"]}, outcome
                )
        if not await self._checkpoint("DECOMPOSITION_PERSIST"):
            return await self._decomposition_terminal(context, "FAILED", "DECOMPOSITION_CANCELLED")
        published = await self._step(
            "decomposition.stage_document",
            {
                "project": request.project,
                "decomposition_id": request.decomposition_id,
                "publish": True,
                "document": assembled.get("document"),
                "markdown": assembled.get("markdown"),
            },
        )
        persisted = await self._step(
            "decomposition.persist",
            {
                "decomposition_id": request.decomposition_id,
                "project": request.project,
                "plan_sha256": assembled.get("plan_sha256"),
                "evidence": published.get("evidence", []),
            },
        )
        return {
            "status": persisted.get("status", "FAILED"),
            "decomposition_id": request.decomposition_id,
            "error_code": persisted.get("error_code"),
            "plan_sha256": persisted.get("plan_sha256"),
            "approval_binding": persisted.get("approval_binding"),
            "parts": len(approved),
            "stories": persisted.get("stories"),
            "tasks": persisted.get("tasks"),
        }

    @staticmethod
    def _approved_entry(part: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
        return {
            "part_id": outcome.get("part_id"),
            "kind": part.get("kind"),
            "number": part.get("number"),
            "content_sha256": outcome.get("content_sha256"),
            "attempt": int(outcome.get("draft_attempt") or 1),
            "part_input": outcome.get("part_input"),
            "part_input_files": outcome.get("part_input_files"),
        }

    @staticmethod
    def _redraft_blockers(redrafts: list[Any]) -> list[str]:
        lines = []
        for redraft in redrafts:
            for problem in redraft.get("problems") or []:
                lines.append(
                    f"{redraft.get('part_id')}: [{problem.get('code')}] "
                    f"{problem.get('subject')}: {problem.get('detail')}"
                )
        return lines[:50]

    async def _part_terminal(
        self, context: dict[str, Any], outcome: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._decomposition_terminal(
            context,
            str(outcome.get("terminal") or "BLOCKED"),
            str(outcome.get("blocker") or "DECOMPOSITION_PART_FAILED"),
            outcome.get("blockers"),
            usage=self._part_usage(outcome),
        )

    @staticmethod
    def _part_usage(outcome: dict[str, Any]) -> dict[str, Any] | None:
        """A failed part's last provider result, so its token usage is still recorded."""
        result = outcome.get("agent_result")
        if not isinstance(result, dict):
            result = outcome.get("failed_agent_usage")
        if not isinstance(result, dict) or outcome.get("status") == "SUCCESS":
            return None
        step = str(outcome.get("provider_step") or "")
        return {
            "agent_result": result,
            "part_id": outcome.get("part_id"),
            "phase": "review" if step == "decomposition.review" else "draft",
            "attempt": outcome.get("draft_attempt"),
        }

    @staticmethod
    def _structural_redraft(
        current: dict[str, Any], code: str, details: list[Any], usage: Any
    ) -> dict[str, Any]:
        """The context for the next draft after Hermes or the provider boundary refused
        this one (no review ran)."""
        dropped = {
            "status",
            "document",
            "blocker",
            "blockers",
            "failed_agent_usage",
            "failed_schema_findings",
            "agent_result",
        }
        redraft = {key: value for key, value in current.items() if key not in dropped}
        redraft["rejection"] = {"code": code, "details": list(details)[:50]}
        if isinstance(usage, dict):
            # Recorded by decomposition.prepare_part (the refused call consumed tokens).
            redraft["rejected_usage"] = {
                "agent_result": usage,
                "part_id": current.get("part_id"),
                "attempt": current.get("draft_attempt"),
            }
        return redraft

    @staticmethod
    def _provider_blocker(phase: str, context: dict[str, Any]) -> str:
        blocker = str(context.get("blocker") or context.get("status") or "")
        if blocker in {"SchemaViolation", "PolicyViolation"} or not blocker:
            return f"{phase}_PROVIDER_FAILED"
        return blocker if re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", blocker) else f"{phase}_FAILED"

    async def _stage_part_input(
        self, request: BacklogDecompositionPlanInput, context: dict[str, Any], part: dict[str, Any]
    ) -> dict[str, Any]:
        """The part's id and staged input (hash-verified against what
        decomposition.prepare_part_input bound into the part), or a terminal outcome."""
        prepared = await self._step("decomposition.prepare_part_input", {**context, "part": part})
        if prepared.get("status") == "BLOCKED":
            return {**prepared, "terminal": "BLOCKED"}
        if prepared.get("status") not in {"INPUT_READY", "PART_REUSED"}:
            return {**prepared, "terminal": "FAILED", "blocker": "DRAFT_INPUT_INVALID"}
        staged = await self._step(
            "decomposition.stage_inputs",
            {
                "project": request.project,
                "decomposition_id": request.decomposition_id,
                "files": prepared.get("files"),
            },
        )
        receipts = staged.get("files") or {}
        hashes = prepared.get("file_hashes") or {}
        if staged.get("status") != "STAGED" or any(
            (receipts.get(name) or {}).get("sha256") != digest for name, digest in hashes.items()
        ):
            return {
                "part_id": prepared.get("part_id"),
                "status": "BLOCKED",
                "terminal": "BLOCKED",
                "blocker": str(staged.get("blocker") or "DECOMPOSITION_INPUTS_UNAVAILABLE"),
                "blockers": staged.get("blockers"),
            }
        # An input over 64 KiB is staged as numbered pieces (data only: no new command).
        names = [str(item) for item in prepared.get("input_files") or [prepared.get("input_name")]]
        return {
            "status": prepared.get("status"),
            "part_id": prepared.get("part_id"),
            "content_sha256": prepared.get("content_sha256"),
            "draft_attempt": prepared.get("draft_attempt"),
            "part_input": receipts.get(names[0]),
            "part_input_files": [receipts.get(name) for name in names],
            "reference_files": {
                key: value for key, value in receipts.items() if key not in set(names)
            },
        }

    async def _decomposition_part(
        self,
        request: BacklogDecompositionPlanInput,
        context: dict[str, Any],
        part: dict[str, Any],
        *,
        redraft: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Draft (Claude) and review (Codex) one part: at most three drafts, each reviewed
        unless Hermes refused it; an assembly `redraft` continues the part's attempts."""
        if redraft is None:
            staged = await self._stage_part_input(request, context, part)
            if staged.get("status") == "PART_REUSED":
                return {**staged, "status": "PART_APPROVED"}
            if staged.get("status") != "INPUT_READY":
                return staged
            current: dict[str, Any] = {
                **context,
                "part": part,
                "part_id": staged.get("part_id"),
                "part_input": staged.get("part_input"),
                "part_input_files": staged.get("part_input_files"),
                "reference_files": staged.get("reference_files"),
                "part_rounds": [],
            }
            attempt = 0
        else:
            current = {
                **context,
                "part": part,
                "part_id": redraft["part_id"],
                "part_input": redraft["part_input"],
                "part_input_files": redraft.get("part_input_files"),
                "part_rounds": [],
                "rejection": redraft["rejection"],
            }
            attempt = int(redraft["first_attempt"]) - 1
        while attempt < self._MAX_PART_DRAFTS:
            attempt += 1
            if not await self._checkpoint("DECOMPOSITION_PART_DRAFT"):
                return {**current, "terminal": "FAILED", "blocker": "DECOMPOSITION_CANCELLED"}
            current = await self._step(
                "decomposition.prepare_part", {**current, "draft_attempt": attempt}
            )
            if current.get("status") == "BLOCKED":
                return {**current, "terminal": "BLOCKED"}
            if current.get("status") != "DRAFT_READY":
                return {
                    **current,
                    "terminal": "FAILED",
                    "blocker": str(current.get("blocker") or "DRAFT_INPUT_INVALID"),
                }
            current = await self._agent_step(
                "decomposition.draft",
                current,
                timeout_minutes=self._PART_TIMEOUT_MINUTES,
                retry_policy=SINGLE_ATTEMPT,
            )
            if current.get("status") != "SUCCESS":
                if attempt < self._MAX_PART_DRAFTS and current.get("blocker") == "SchemaViolation":
                    # The provider boundary refused the output against the part schema; its
                    # findings use one of the part's redrafts.
                    current = self._structural_redraft(
                        current,
                        "DRAFT_SCHEMA_INVALID",
                        list(current.get("failed_schema_findings") or []),
                        current.get("failed_agent_usage"),
                    )
                    continue
                return {**current, "blocker": self._provider_blocker("DRAFT", current)}
            recorded = await self._step("decomposition.record_part_draft", current)
            if recorded.get("status") != "DRAFT_RECORDED":
                if (
                    attempt < self._MAX_PART_DRAFTS
                    and recorded.get("blocker") in self._STRUCTURAL_REJECTIONS
                ):
                    # Hermes's own deterministic findings use one of the part's redrafts,
                    # like a REVISE (no review of the rejected draft).
                    current = self._structural_redraft(
                        recorded,
                        str(recorded.get("blocker")),
                        list(recorded.get("blockers") or []),
                        None,
                    )
                    continue
                return {
                    **recorded,
                    "blocker": str(recorded.get("blocker") or "DRAFT_SCHEMA_INVALID"),
                }
            current = {key: value for key, value in recorded.items() if key != "document"}
            draft = await self._step(
                "decomposition.stage_document",
                {
                    "project": request.project,
                    "decomposition_id": request.decomposition_id,
                    "name": f"part-{current.get('part_id')}-draft-{attempt}.json",
                    "document": recorded.get("document"),
                },
            )
            if draft.get("status") != "STAGED":
                return {
                    **current,
                    "blocker": str(draft.get("blocker") or "DECOMPOSITION_INPUTS_UNAVAILABLE"),
                    "blockers": draft.get("blockers"),
                }
            current = {**current, "draft_file": draft.get("document")}
            if not await self._checkpoint("DECOMPOSITION_PART_REVIEW"):
                return {**current, "terminal": "FAILED", "blocker": "DECOMPOSITION_CANCELLED"}
            current = await self._step("decomposition.prepare_part_review", current)
            if current.get("status") == "BLOCKED":
                return {**current, "terminal": "BLOCKED"}
            if current.get("status") != "REVIEW_READY":
                return {
                    **current,
                    "terminal": "FAILED",
                    "blocker": str(current.get("blocker") or "REVIEW_INPUT_INVALID"),
                }
            current = await self._agent_step(
                "decomposition.review",
                current,
                timeout_minutes=self._PART_TIMEOUT_MINUTES,
                retry_policy=SINGLE_ATTEMPT,
            )
            if current.get("status") != "SUCCESS":
                return {**current, "blocker": self._provider_blocker("REVIEW", current)}
            reviewed = await self._step("decomposition.record_part_review", current)
            current = {key: value for key, value in reviewed.items() if key != "document"}
            if reviewed.get("status") == "PART_APPROVED":
                return current
            if reviewed.get("status") != "PART_REVISE":
                return {
                    **current,
                    "blocker": str(reviewed.get("blocker") or "REVIEW_SCHEMA_INVALID"),
                }
            review = await self._step(
                "decomposition.stage_document",
                {
                    "project": request.project,
                    "decomposition_id": request.decomposition_id,
                    "name": f"part-{current.get('part_id')}-review-{attempt}.json",
                    "document": reviewed.get("document"),
                },
            )
            if review.get("status") != "STAGED":
                return {
                    **current,
                    "blocker": str(review.get("blocker") or "DECOMPOSITION_INPUTS_UNAVAILABLE"),
                    "blockers": review.get("blockers"),
                }
            # A REVISE redraft answers this review, not an earlier Hermes rejection.
            current = {
                **{key: value for key, value in current.items() if key != "rejection"},
                "review_file": review.get("document"),
            }
        return {**current, "blocker": "DECOMPOSITION_PART_REVIEW_UNRESOLVED"}
