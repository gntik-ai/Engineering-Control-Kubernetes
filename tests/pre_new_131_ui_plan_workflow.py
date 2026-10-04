"""UiMigrationPlanWorkflow exactly as origin/main be78691 ran it, before NEW-131.

Copied verbatim from that commit's workflows/definitions.py (only the class name and
``sandboxed=False`` differ) so replay tests record genuinely pre-change histories (one
whole-blueprint draft and review), not histories of the current class with its patch
forced off. Do not edit.
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
class ParentUiMigrationPlanWorkflow(ControlledWorkflow):
    """NEW-123 flow A: the UI migration blueprint, persisted as an approvable candidate.

    snapshot -> deterministic inventory -> kit snapshot (BLOCKED without kit data) ->
    Claude draft (plan-ui-migration) -> Codex review; at most one redraft and a second
    review, otherwise BLOCKED with the findings. It never approves anything.
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
    ) -> dict[str, Any]:
        recorded = await self._step(
            "ui_plan.record_terminal",
            {
                "blueprint_id": context.get("blueprint_id"),
                "project": context.get("project"),
                "terminal_status": status,
                "blocker": blocker,
                "blockers": list(blockers or [])[:50],
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

    @staticmethod
    def _ui_provider_blocker(phase: str, context: dict[str, Any]) -> str:
        blocker = str(context.get("blocker") or context.get("status") or "")
        if blocker in {"SchemaViolation", "PolicyViolation"} or not blocker:
            return f"{phase}_PROVIDER_FAILED"
        return blocker if re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", blocker) else f"{phase}_FAILED"
