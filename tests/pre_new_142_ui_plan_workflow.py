"""UiMigrationPlanWorkflow exactly as origin/main ed92f1d runs it, before NEW-142.

Replay tests record genuinely pre-change histories with this copy (a part never staged an
earlier run's draft). Copied verbatim from that commit's workflows/definitions.py; only the
class name and ``sandboxed=False`` differ. Do not edit.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError

from engineering_control.workflows.definitions import ControlledWorkflow
from engineering_control.workflows.inputs import UiMigrationPlanInput


@workflow.defn(name="UiMigrationPlanWorkflow", sandboxed=False)
class PreNew142UiMigrationPlanWorkflow(ControlledWorkflow):
    """NEW-123 flow A: the UI migration blueprint, persisted as an approvable candidate.

    snapshot -> deterministic inventory -> kit snapshot (BLOCKED without kit data) ->
    Claude draft (plan-ui-migration) -> Codex review; at most one redraft and a second
    review, otherwise BLOCKED with the findings. It never approves anything.

    NEW-131 (patch ui-plan-batched-blueprint-v1): Hermes fills the mechanical fields from the
    inventory; Claude drafts a frame, then page batches; Codex reviews each part (one redraft
    per part; NEW-138, patch new-138-two-redrafts-<part>: two); Hermes assembles the reviewed
    parts into the blueprint candidate.
    """

    @workflow.run
    async def run(self, request: UiMigrationPlanInput) -> dict[str, Any]:
        try:
            result = await self._plan(request)
        except ActivityError:
            result = await self._ui_plan_terminal(
                {"blueprint_id": request.blueprint_id, "project": request.project},
                "FAILED",
                "UI_PLAN_STEP_FAILED",
            )
        try:
            await self._step(
                "ui_plan.finalize_run",
                {
                    "blueprint_id": request.blueprint_id,
                    "terminal_status": result.get("status"),
                    "error_code": result.get("error_code"),
                },
            )
        except ActivityError:
            workflow.logger.warning("ui plan %s: Run finalization failed", request.blueprint_id)
        return result

    async def _ui_plan_terminal(
        self,
        context: dict[str, Any],
        status: str,
        blocker: str,
        blockers: list[Any] | None = None,
        *,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        recorded = await self._step(
            "ui_plan.record_terminal",
            {
                "blueprint_id": context.get("blueprint_id"),
                "project": context.get("project"),
                "terminal_status": status,
                "blocker": blocker,
                "blockers": list(blockers or [])[:50],
                # NEW-131 review: input only (no new command), so replay is unaffected.
                **({"part_usage": usage} if usage else {}),
            },
        )
        return {
            "status": recorded.get("status", status),
            "blueprint_id": context.get("blueprint_id"),
            "error_code": recorded.get("error_code", blocker),
            "blockers": list(blockers or [])[:50],
        }

    async def _plan(self, request: UiMigrationPlanInput) -> dict[str, Any]:
        context = await self._step("ui_plan.register", asdict(request))
        if context.get("status") == "BLOCKED":
            return {
                "status": "BLOCKED",
                "blueprint_id": request.blueprint_id,
                "error_code": context.get("blocker"),
                "blockers": context.get("blockers", []),
            }
        if context.get("status") != "REGISTERED":
            return {"status": "SUPERSEDED", "blueprint_id": request.blueprint_id}
        if not await self._checkpoint("UI_PLAN_SNAPSHOT"):
            return await self._ui_plan_terminal(context, "FAILED", "UI_PLAN_CANCELLED")
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
            return await self._ui_plan_terminal(context, "BLOCKED", "UI_PLAN_SNAPSHOT_UNAVAILABLE")
        source = {
            "source_worktree": snapshot.get("source_worktree"),
            "source_sha": snapshot.get("source_sha"),
        }
        if not await self._checkpoint("UI_PLAN_INVENTORY"):
            return await self._ui_plan_terminal(context, "FAILED", "UI_PLAN_CANCELLED")
        inventory = await self._step(
            "ui.inventory", {"project": request.project, "snapshot": source}
        )
        if inventory.get("status") != "UI_INVENTORY_PASS":
            if inventory.get("status") == "BLOCKED" and workflow.patched(
                self._INVENTORY_BLOCKED_PATCH
            ):
                # NEW-136 review: e.g. a symlinked console scan root (fail closed).
                return await self._ui_plan_terminal(
                    context,
                    "BLOCKED",
                    str(inventory.get("blocker") or "UI_INVENTORY_BLOCKED"),
                    [str(inventory.get("reason") or "")[:300]],
                )
            return await self._ui_plan_terminal(context, "FAILED", "UI_INVENTORY_FAILED")
        kit = await self._step("ui.kit_snapshot", {"project": request.project})
        if kit.get("status") == "BLOCKED":
            # No draft without kit data (design section 11: models get no MCP).
            return await self._ui_plan_terminal(
                context,
                "BLOCKED",
                str(kit.get("blocker") or "UI_MCP_UNAVAILABLE"),
                [str(kit.get("reason") or "")[:300]],
            )
        if kit.get("status") != "UI_KIT_SNAPSHOT_PASS":
            return await self._ui_plan_terminal(context, "FAILED", "UI_KIT_SNAPSHOT_FAILED")
        context = await self._step(
            "ui_plan.record_inputs",
            {
                **context,
                **source,
                "inventory_sha256": inventory.get("inventory_sha256"),
                "inventory_summary": inventory.get("summary"),
                "kit_snapshot_sha256": kit.get("snapshot_sha256"),
                "gntik_ui_version": kit.get("gntik_ui_version"),
                "kit_image": kit.get("image"),
                "evidence": {
                    "inventory": inventory.get("evidence", []),
                    "kit": kit.get("evidence", []),
                },
            },
        )
        if context.get("status") != "INPUTS_RECORDED":
            return {"status": "SUPERSEDED", "blueprint_id": request.blueprint_id}
        staged = await self._step(
            "ui_plan.stage_inputs",
            {
                "project": request.project,
                "blueprint_id": request.blueprint_id,
                "inventory_sha256": context.get("inventory_sha256"),
                "kit_snapshot_sha256": context.get("kit_snapshot_sha256"),
            },
        )
        if staged.get("status") != "STAGED":
            return await self._ui_plan_terminal(
                context,
                "BLOCKED",
                str(staged.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE"),
                [str(staged.get("reason") or "")[:300]],
            )
        context = {
            **context,
            "inputs": {
                "input_directory": staged.get("input_directory"),
                "files": staged.get("files"),
            },
            "review_rounds": [],
        }
        if workflow.patched(self._BATCHED_PATCH):
            # NEW-131: Hermes fills the mechanical fields; Claude drafts the frame and the
            # page batches, Codex reviews each part; Hermes assembles the reviewed parts.
            return await self._plan_batched(request, context)
        approved: dict[str, Any] | None = None
        for attempt in (1, 2):
            if not await self._checkpoint("UI_PLAN_DRAFT"):
                return await self._ui_plan_terminal(context, "FAILED", "UI_PLAN_CANCELLED")
            context = await self._step(
                "ui_plan.prepare_draft", {**context, "draft_attempt": attempt}
            )
            if context.get("status") != "DRAFT_READY":
                return await self._ui_plan_terminal(
                    context, "FAILED", str(context.get("blocker") or "DRAFT_INPUT_INVALID")
                )
            context = await self._agent_step("ui_plan.draft", context, timeout_minutes=200)
            if context.get("status") != "SUCCESS":
                return await self._ui_plan_terminal(
                    context, "BLOCKED", self._ui_provider_blocker("DRAFT", context)
                )
            recorded = await self._step("ui_plan.record_draft", context)
            if recorded.get("status") != "DRAFT_RECORDED":
                return await self._ui_plan_terminal(
                    recorded,
                    "BLOCKED",
                    str(recorded.get("blocker") or "DRAFT_SCHEMA_INVALID"),
                    recorded.get("blockers"),
                )
            context = {key: value for key, value in recorded.items() if key != "document"}
            draft = await self._step(
                "ui_plan.stage_document",
                {
                    "project": request.project,
                    "blueprint_id": request.blueprint_id,
                    "name": f"draft-{attempt}.json",
                    "document": recorded.get("document"),
                },
            )
            if draft.get("status") != "STAGED":
                return await self._ui_plan_terminal(
                    context, "BLOCKED", str(draft.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE")
                )
            context = {**context, "draft_file": draft.get("document")}
            if not await self._checkpoint("UI_PLAN_REVIEW"):
                return await self._ui_plan_terminal(context, "FAILED", "UI_PLAN_CANCELLED")
            context = await self._step("ui_plan.prepare_review", context)
            if context.get("status") != "REVIEW_READY":
                return await self._ui_plan_terminal(
                    context, "FAILED", str(context.get("blocker") or "REVIEW_INPUT_INVALID")
                )
            context = await self._agent_step("ui_plan.review", context, timeout_minutes=200)
            if context.get("status") != "SUCCESS":
                return await self._ui_plan_terminal(
                    context, "BLOCKED", self._ui_provider_blocker("REVIEW", context)
                )
            reviewed = await self._step("ui_plan.record_review", context)
            context = {
                key: value for key, value in reviewed.items() if key not in {"document", "markdown"}
            }
            if reviewed.get("status") == "REVIEW_APPROVED":
                approved = reviewed
                break
            if reviewed.get("status") != "REVIEW_REVISE":
                return await self._ui_plan_terminal(
                    context,
                    "BLOCKED",
                    str(reviewed.get("blocker") or "REVIEW_SCHEMA_INVALID"),
                    reviewed.get("blockers"),
                )
            review = await self._step(
                "ui_plan.stage_document",
                {
                    "project": request.project,
                    "blueprint_id": request.blueprint_id,
                    "name": f"review-{attempt}.json",
                    "document": reviewed.get("document"),
                },
            )
            if review.get("status") != "STAGED":
                return await self._ui_plan_terminal(
                    context, "BLOCKED", str(review.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE")
                )
            context = {**context, "review_file": review.get("document")}
        if approved is None:
            return await self._ui_plan_terminal(context, "BLOCKED", "BLUEPRINT_REVIEW_UNRESOLVED")
        if not await self._checkpoint("UI_PLAN_PERSIST"):
            return await self._ui_plan_terminal(context, "FAILED", "UI_PLAN_CANCELLED")
        published = await self._step(
            "ui_plan.stage_document",
            {
                "project": request.project,
                "blueprint_id": request.blueprint_id,
                "publish": True,
                "document": approved.get("document"),
                "markdown": approved.get("markdown"),
            },
        )
        persisted = await self._step(
            "ui_plan.persist",
            {
                "blueprint_id": request.blueprint_id,
                "project": request.project,
                "draft_attempt": approved.get("draft_attempt"),
                "reviewed_sha256": approved.get("reviewed_sha256"),
                "review_rounds": context.get("review_rounds", []),
                "evidence": published.get("evidence", []),
            },
        )
        return {
            "status": persisted.get("status", "FAILED"),
            "blueprint_id": request.blueprint_id,
            "error_code": persisted.get("error_code"),
            "blueprint_sha256": persisted.get("blueprint_sha256"),
            "approval_binding": persisted.get("approval_binding"),
            "review_rounds": len(context.get("review_rounds", [])),
        }

    _BATCHED_PATCH = "ui-plan-batched-blueprint-v1"
    # NEW-136 review: a BLOCKED inventory ends the plan BLOCKED with the inventory's blocker
    # (before: FAILED UI_INVENTORY_FAILED; the inventory step never returned BLOCKED then).
    _INVENTORY_BLOCKED_PATCH = "ui-plan-inventory-blocked-v1"
    # NEW-132: a first draft Hermes rejects (schema or structure) gets the part's redraft.
    _STRUCTURAL_REDRAFT_PATCH = "new-132-structural-redraft"
    _STRUCTURAL_REJECTIONS = frozenset({"DRAFT_SCHEMA_INVALID", "DRAFT_STRUCTURE_INVALID"})
    # NEW-138 (operator, 2026-10-05): two redrafts per part; the id is suffixed per part.
    _TWO_REDRAFTS_PATCH = "new-138-two-redrafts"
    # Per part provider call: the request's own timeout is PART_TIMEOUT_SECONDS (1800 s);
    # the activity bound leaves room for the CLI's start-up and result handling.
    _PART_TIMEOUT_MINUTES = 40

    async def _plan_batched(
        self, request: UiMigrationPlanInput, context: dict[str, Any]
    ) -> dict[str, Any]:
        staged = await self._step(
            "ui_plan.stage_parts",
            {
                "project": request.project,
                "blueprint_id": request.blueprint_id,
                "inventory_sha256": context.get("inventory_sha256"),
                "kit_snapshot_sha256": context.get("kit_snapshot_sha256"),
                "batch_pages": context.get("batch_pages"),
            },
            timeout_minutes=30,
        )
        if staged.get("status") != "STAGED":
            return await self._ui_plan_terminal(
                context,
                "BLOCKED",
                str(staged.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE"),
                [str(staged.get("reason") or "")[:300]],
            )
        recorded = await self._step(
            "ui_plan.record_parts",
            {
                **context,
                "mechanical": staged.get("mechanical"),
                "mechanical_sha256": staged.get("mechanical_sha256"),
                "parts": staged.get("parts"),
                "kit_slice": staged.get("kit_slice"),
            },
        )
        if recorded.get("status") != "PARTS_RECORDED":
            return await self._ui_plan_terminal(
                recorded, "FAILED", str(recorded.get("blocker") or "UI_PLAN_PARTS_INVALID")
            )
        context = {key: value for key, value in recorded.items() if key != "status"}
        approved: list[dict[str, Any]] = []
        for part in list(context.get("parts") or []):
            outcome = await self._ui_plan_part(
                request, {**context, "approved_parts": approved}, part
            )
            if outcome.get("status") != "PART_APPROVED":
                return await self._ui_plan_terminal(
                    outcome,
                    str(outcome.get("terminal") or "BLOCKED"),
                    str(outcome.get("blocker") or "UI_PLAN_PART_FAILED"),
                    outcome.get("blockers"),
                    usage=self._part_usage(outcome),
                )
            if part.get("kind") == "frame":
                # The complete reviewed frame, as a file every batch reads (never truncated).
                brief = await self._step(
                    "ui_plan.stage_document",
                    {
                        "project": request.project,
                        "blueprint_id": request.blueprint_id,
                        "name": f"part-{outcome.get('part_id')}-brief.json",
                        "document": outcome.get("frame_brief"),
                    },
                )
                if brief.get("status") != "STAGED":
                    return await self._ui_plan_terminal(
                        context,
                        "BLOCKED",
                        str(brief.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE"),
                    )
                context = {**context, "frame_brief_file": brief.get("document")}
            approved.append(
                {
                    "part_id": outcome.get("part_id"),
                    "kind": part.get("kind"),
                    "content_sha256": outcome.get("content_sha256"),
                }
            )
        if not await self._checkpoint("UI_PLAN_ASSEMBLE"):
            return await self._ui_plan_terminal(context, "FAILED", "UI_PLAN_CANCELLED")
        assembled = await self._step("ui_plan.assemble", {**context, "approved_parts": approved})
        if assembled.get("status") != "ASSEMBLED":
            return await self._ui_plan_terminal(
                context,
                "BLOCKED",
                str(assembled.get("blocker") or "ASSEMBLY_INVALID"),
                assembled.get("blockers"),
            )
        if not await self._checkpoint("UI_PLAN_PERSIST"):
            return await self._ui_plan_terminal(context, "FAILED", "UI_PLAN_CANCELLED")
        published = await self._step(
            "ui_plan.stage_document",
            {
                "project": request.project,
                "blueprint_id": request.blueprint_id,
                "publish": True,
                "document": assembled.get("document"),
                "markdown": assembled.get("markdown"),
            },
        )
        rounds = list(assembled.get("review_rounds") or [])
        persisted = await self._step(
            "ui_plan.persist",
            {
                "blueprint_id": request.blueprint_id,
                "project": request.project,
                "draft_attempt": assembled.get("draft_attempt"),
                "reviewed_sha256": assembled.get("reviewed_sha256"),
                "review_rounds": rounds,
                "evidence": published.get("evidence", []),
            },
        )
        return {
            "status": persisted.get("status", "FAILED"),
            "blueprint_id": request.blueprint_id,
            "error_code": persisted.get("error_code"),
            "blueprint_sha256": persisted.get("blueprint_sha256"),
            "approval_binding": persisted.get("approval_binding"),
            "review_rounds": len(rounds),
            "parts": len(approved),
        }

    @staticmethod
    def _structural_redraft(
        current: dict[str, Any], code: str, details: list[Any], usage: Any
    ) -> dict[str, Any]:
        """The context for attempt 2 after Hermes refused attempt 1 (no review ran)."""
        dropped = {
            "status",
            "document",
            "blocker",
            "failed_agent_usage",
            "failed_schema_findings",
            "agent_result",
        }
        redraft = {key: value for key, value in current.items() if key not in dropped}
        redraft["rejection"] = {"code": code, "details": [str(item) for item in details[:50]]}
        if isinstance(usage, dict):
            # Recorded by ui_plan.prepare_part (the refused call consumed tokens).
            redraft["rejected_usage"] = {
                "agent_result": usage,
                "part_id": current.get("part_id"),
                "phase": "draft",
                "attempt": current.get("draft_attempt"),
            }
        return redraft

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
            "phase": "review" if step == "ui_plan.review" else "draft",
            "attempt": outcome.get("draft_attempt"),
        }

    async def _ui_plan_part(
        self, request: UiMigrationPlanInput, context: dict[str, Any], part: dict[str, Any]
    ) -> dict[str, Any]:
        """Draft (Claude) and review (Codex) one part; at most two redrafts of that part
        (NEW-138; one before the per-part patch)."""
        current: dict[str, Any] = {**context, "part": part, "part_rounds": []}
        max_attempts = 2
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            if attempt == 2 and workflow.patched(
                f"{self._TWO_REDRAFTS_PATCH}-{part.get('kind')}-{part.get('index')}"
            ):
                # NEW-138: scoped per part, so a part replayed from before the deploy (no
                # marker, memoized false) never limits the parts that follow it.
                max_attempts = 3
            if not await self._checkpoint("UI_PLAN_PART_DRAFT"):
                return {**current, "terminal": "FAILED", "blocker": "UI_PLAN_CANCELLED"}
            current = await self._step(
                "ui_plan.prepare_part",
                {**current, "draft_attempt": attempt, "max_draft_attempts": max_attempts},
            )
            if current.get("status") == "PART_REUSED":
                return {**current, "status": "PART_APPROVED"}
            if current.get("status") != "DRAFT_READY":
                return {
                    **current,
                    "terminal": "FAILED",
                    "blocker": str(current.get("blocker") or "DRAFT_INPUT_INVALID"),
                }
            current = await self._agent_step(
                "ui_plan.draft", current, timeout_minutes=self._PART_TIMEOUT_MINUTES
            )
            if current.get("status") != "SUCCESS":
                if (
                    attempt < max_attempts
                    and current.get("blocker") == "SchemaViolation"
                    and workflow.patched(self._STRUCTURAL_REDRAFT_PATCH)
                ):
                    # NEW-132: the provider boundary refused the output against the same
                    # schema; its findings use one of the part's redrafts.
                    current = self._structural_redraft(
                        current,
                        "DRAFT_SCHEMA_INVALID",
                        list(current.get("failed_schema_findings") or []),
                        current.get("failed_agent_usage"),
                    )
                    continue
                if (
                    attempt == max_attempts
                    and current.get("blocker") == "SchemaViolation"
                    and (max_attempts == 3 or "rejection" in current)
                ):
                    # NEW-132: the last redraft was refused again; report its schema findings.
                    # NEW-138: on a part's third attempt, whatever came before it (an
                    # intervening REVISE clears the earlier rejection). Before the patch,
                    # unchanged. Only the returned blocker differs: no workflow command.
                    return {
                        **current,
                        "blocker": "DRAFT_SCHEMA_INVALID",
                        "blockers": [
                            f"{current.get('part_id')}: {item}"
                            for item in current.get("failed_schema_findings") or []
                        ][:50],
                    }
                return {**current, "blocker": self._ui_provider_blocker("DRAFT", current)}
            recorded = await self._step("ui_plan.record_part_draft", current)
            if recorded.get("status") != "DRAFT_RECORDED":
                if (
                    attempt < max_attempts
                    and recorded.get("blocker") in self._STRUCTURAL_REJECTIONS
                    and workflow.patched(self._STRUCTURAL_REDRAFT_PATCH)
                ):
                    # NEW-132: Hermes's own deterministic findings use one of the part's
                    # redrafts, like a REVISE (no review of the rejected draft).
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
            name = f"part-{current.get('part_id')}-draft-{attempt}.json"
            draft = await self._step(
                "ui_plan.stage_document",
                {
                    "project": request.project,
                    "blueprint_id": request.blueprint_id,
                    "name": name,
                    "document": recorded.get("document"),
                },
            )
            if draft.get("status") != "STAGED":
                return {
                    **current,
                    "blocker": str(draft.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE"),
                }
            current = {**current, "draft_file": draft.get("document")}
            if not await self._checkpoint("UI_PLAN_PART_REVIEW"):
                return {**current, "terminal": "FAILED", "blocker": "UI_PLAN_CANCELLED"}
            current = await self._step("ui_plan.prepare_part_review", current)
            if current.get("status") != "REVIEW_READY":
                return {
                    **current,
                    "terminal": "FAILED",
                    "blocker": str(current.get("blocker") or "REVIEW_INPUT_INVALID"),
                }
            current = await self._agent_step(
                "ui_plan.review", current, timeout_minutes=self._PART_TIMEOUT_MINUTES
            )
            if current.get("status") != "SUCCESS":
                return {**current, "blocker": self._ui_provider_blocker("REVIEW", current)}
            reviewed = await self._step("ui_plan.record_part_review", current)
            current = {key: value for key, value in reviewed.items() if key != "document"}
            if reviewed.get("status") == "PART_APPROVED":
                return current
            if reviewed.get("status") != "PART_REVISE":
                return {
                    **current,
                    "blocker": str(reviewed.get("blocker") or "REVIEW_SCHEMA_INVALID"),
                }
            review = await self._step(
                "ui_plan.stage_document",
                {
                    "project": request.project,
                    "blueprint_id": request.blueprint_id,
                    "name": f"part-{current.get('part_id')}-review-{attempt}.json",
                    "document": reviewed.get("document"),
                },
            )
            if review.get("status") != "STAGED":
                return {
                    **current,
                    "blocker": str(review.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE"),
                }
            # A REVISE redraft answers this review, not an earlier structural rejection.
            current = {
                **{key: value for key, value in current.items() if key != "rejection"},
                "review_file": review.get("document"),
            }
        return {**current, "blocker": "BLUEPRINT_PART_REVIEW_UNRESOLVED"}

    @staticmethod
    def _ui_provider_blocker(phase: str, context: dict[str, Any]) -> str:
        blocker = str(context.get("blocker") or context.get("status") or "")
        if blocker in {"SchemaViolation", "PolicyViolation"} or not blocker:
            return f"{phase}_PROVIDER_FAILED"
        return blocker if re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", blocker) else f"{phase}_FAILED"
