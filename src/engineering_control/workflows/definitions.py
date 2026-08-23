from __future__ import annotations

from dataclasses import asdict, replace
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from engineering_control.workflows.inputs import (
    AcceptanceLifecycleInput,
    AuditCampaignInput,
    CleanupInput,
    DeliveryReconciliationInput,
    FindingVerificationInput,
    IssueDeliveryInput,
    PreviewDeploymentInput,
    QuotaRefreshInput,
    RecoveryInput,
    RuntimeVerificationInput,
    StagingDeploymentInput,
)

ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=5,
    non_retryable_error_types=[
        "PolicyViolation",
        "SecurityBlocker",
        "ApprovalMismatch",
        "DirtyWorktree",
        "NotConfigured",
        "SchemaViolation",
        "MANUAL_AUTH_REQUIRED_CODEX",
        "MANUAL_AUTH_REQUIRED_CLAUDE",
        "PROVIDER_CLI_MISSING_CODEX",
        "PROVIDER_CLI_MISSING_CLAUDE",
    ],
)


class ControlledWorkflow:
    _reported_deployment_errors = frozenset(
        {"ApprovalMismatch", "PolicyViolation", "SecurityBlocker", "ReconciliationPending"}
    )

    def __init__(self) -> None:
        self._paused = False
        self._cancelled = False
        self._approval: dict[str, Any] | None = None
        self._phase = "INITIALIZING"

    @workflow.signal(name="pause")
    async def pause(self) -> None:
        self._paused = True

    @workflow.signal(name="resume")
    async def resume(self) -> None:
        self._paused = False

    @workflow.signal(name="cancel")
    async def cancel(self) -> None:
        self._cancelled = True
        self._paused = False

    @workflow.signal(name="approval")
    async def approval(self, payload: dict[str, Any]) -> None:
        self._approval = payload

    @workflow.query(name="status")
    def status(self) -> dict[str, Any]:
        return {
            "phase": self._phase,
            "paused": self._paused,
            "cancelled": self._cancelled,
            "approval_received": self._approval is not None,
        }

    async def _checkpoint(self, phase: str) -> bool:
        self._phase = phase
        if self._paused:
            await workflow.wait_condition(lambda: not self._paused or self._cancelled)
        return not self._cancelled

    async def _step(
        self, step: str, payload: dict[str, Any], *, timeout_minutes: int = 30
    ) -> dict[str, Any]:
        result = await workflow.execute_activity(
            "execute_control_step",
            {"step": step, "payload": payload},
            task_queue=self._activity_queue(step, payload),
            start_to_close_timeout=timedelta(minutes=timeout_minutes),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=ACTIVITY_RETRY,
        )
        if not isinstance(result, dict):
            raise TypeError(f"activity {step} returned a non-object result")
        return result

    async def _deployment_failure(
        self, context: dict[str, Any], error: ActivityError
    ) -> dict[str, Any]:
        cause = error.cause
        blocker = (
            cause.type
            if isinstance(cause, ApplicationError)
            and cause.type in self._reported_deployment_errors
            else "DEPLOYMENT_ACTIVITY_FAILED"
        )
        return await self._step(
            "deployment.record_terminal",
            {**context, "terminal_status": "DEPLOYMENT_FAILED", "blocker": blocker},
        )

    @classmethod
    def _activity_queue(cls, step: str, payload: dict[str, Any]) -> str:
        if step in {"cleanup.inspect_evidence_files", "cleanup.execute_evidence_files"}:
            return "engineering-control"
        if step in {"cleanup.inspect_delivery_files", "cleanup.execute_delivery_files"}:
            return "engineering-delivery"
        if step in {"cleanup.inspect_browser_files", "cleanup.execute_browser_files"}:
            return "engineering-browser"
        if step in {"cleanup.inspect_codex_files", "cleanup.execute_codex_files"}:
            return "engineering-codex"
        if step in {"cleanup.inspect_preview_namespaces", "cleanup.execute_preview_namespaces"}:
            return "engineering-release"
        if step in {
            "project.snapshot",
            "finding.fresh_snapshot",
            "delivery.allocate_worktrees",
            "delivery.deterministic_prechecks",
            "delivery.validate_changeset",
            "audit.deterministic_cell",
            "runtime.deterministic_checks",
            "cleanup.protect_active",
            "cleanup.execute",
        }:
            return "engineering-scan"
        if step == "finding.reproduce":
            return f"engineering-{cls._nested_value(payload, 'provider', 'claude')}"
        if step == "audit.agent_cell":
            return f"engineering-{cls._nested_value(payload, 'provider', 'codex')}"
        if step == "delivery.invoke_spec":
            return f"engineering-{cls._nested_value(payload, 'spec_provider', 'claude')}"
        if step == "delivery.invoke_maker":
            return f"engineering-{cls._nested_value(payload, 'maker_provider', 'codex')}"
        if step == "delivery.invoke_deployment_maker":
            return f"engineering-{cls._nested_value(payload, 'maker_provider', 'codex')}"
        if step == "delivery.invoke_checker":
            return f"engineering-{cls._nested_value(payload, 'checker_provider', 'claude')}"
        if step == "quota.refresh":
            return f"engineering-{cls._nested_value(payload, 'provider', 'codex')}"
        if step == "runtime.independent_review":
            return f"engineering-{cls._nested_value(payload, 'provider', 'codex')}"
        if step == "delivery.publish":
            return "engineering-delivery"
        if step in {"reconcile.inspect_ci", "reconcile.publish_promotion"}:
            return "engineering-delivery"
        if step == "reconcile.apply_promotion":
            return "engineering-scan"
        if step in {"runtime.browser_checks"}:
            return "engineering-browser"
        if step in {
            "preview.preflight",
            "preview.verify",
            "staging.preflight",
            "preview.deploy",
            "staging.deploy",
            "recovery.execute",
        }:
            return "engineering-release"
        return "engineering-control"

    @staticmethod
    def _nested_value(value: Any, key: str, default: str) -> str:
        if isinstance(value, dict):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
            for nested in value.values():
                found = ControlledWorkflow._nested_value(nested, key, "")
                if found:
                    return found
        return default


@workflow.defn(name="AuditCampaignWorkflow")
class AuditCampaignWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: AuditCampaignInput) -> dict[str, Any]:
        if not await self._checkpoint("SNAPSHOT"):
            return {"status": "CANCELLED"}
        snapshot = await self._step("project.snapshot", asdict(request))
        campaign_id = request.campaign_id or snapshot["campaign_id"]
        cells = [
            {
                "domain": domain,
                "persona": persona,
                "lens": lens,
                "environment": environment,
            }
            for domain in request.domains
            for persona in request.personas
            for lens in request.lenses
            for environment in request.environments
        ]
        batch = cells[request.cell_offset : request.cell_offset + 50]
        await self._step(
            "campaign.persist_coverage",
            {"campaign_id": campaign_id, "cells": batch, "total": len(cells)},
        )
        for index, cell in enumerate(batch, start=request.cell_offset):
            if not await self._checkpoint(f"AUDIT_CELL_{index}"):
                return {"status": "CANCELLED", "campaign_id": campaign_id}
            deterministic = await self._step(
                "audit.deterministic_cell",
                {"campaign_id": campaign_id, "cell": cell, "snapshot": snapshot},
            )
            prepared = await self._step(
                "audit.prepare_agent_cell",
                {
                    "campaign_id": campaign_id,
                    "cell": cell,
                    "snapshot": snapshot,
                    "deterministic": deterministic,
                },
            )
            if prepared.get("status") in {"RATE_LIMITED", "BLOCKED"}:
                prepared = await self._step(
                    "campaign.persist_cell_blocker",
                    {
                        **prepared,
                        "deterministic_evidence_sha256": deterministic.get("evidence_sha256"),
                        "blocker": prepared.get("blocker", prepared["status"]),
                    },
                )
                return {
                    "status": prepared["status"],
                    "campaign_id": campaign_id,
                    "next_cell_offset": index,
                    "context": prepared,
                }
            agent_result = await self._step(
                "audit.agent_cell",
                prepared,
                timeout_minutes=120,
            )
            if agent_result.get("status") in {
                "RATE_LIMITED",
                "BLOCKED",
                "FAILED",
                "TIMED_OUT",
                "CANCELLED",
            }:
                if agent_result.get("status") in {"RATE_LIMITED", "BLOCKED"}:
                    agent_result = await self._step(
                        "campaign.persist_cell_blocker",
                        {
                            **agent_result,
                            "deterministic_evidence_sha256": deterministic.get("evidence_sha256"),
                            "blocker": agent_result.get("blocker", agent_result["status"]),
                        },
                    )
                return {
                    "status": agent_result["status"],
                    "campaign_id": campaign_id,
                    "next_cell_offset": index,
                    "context": agent_result,
                }
            await self._step(
                "campaign.persist_cell_result",
                {
                    "campaign_id": campaign_id,
                    "cell": cell,
                    "agent_result": agent_result.get("agent_result"),
                    "deterministic_evidence_sha256": deterministic.get("evidence_sha256"),
                },
            )
        next_offset = request.cell_offset + len(batch)
        if next_offset < len(cells):
            workflow.continue_as_new(
                replace(request, campaign_id=campaign_id, cell_offset=next_offset)
            )
        summary = await self._step(
            "campaign.finalize", {"campaign_id": campaign_id, "expected_cells": len(cells)}
        )
        return {
            "status": summary.get("status", "INCOMPLETE"),
            "campaign_id": campaign_id,
            "summary": summary,
        }


@workflow.defn(name="FindingVerificationWorkflow")
class FindingVerificationWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: FindingVerificationInput) -> dict[str, Any]:
        candidate = await self._step("finding.load_candidate", asdict(request))
        fresh = await self._step("finding.fresh_snapshot", candidate)
        attempts: list[dict[str, Any]] = []
        for ordinal in (1, 2):
            if not await self._checkpoint(f"REPRODUCTION_{ordinal}"):
                return {"status": "CANCELLED"}
            prepared = await self._step(
                "finding.prepare_verifier",
                {"finding": candidate, "snapshot": fresh, "ordinal": ordinal},
            )
            if prepared.get("status") in {"RATE_LIMITED", "BLOCKED"}:
                return {"status": prepared["status"], "context": prepared}
            reproduced = await self._step("finding.reproduce", prepared, timeout_minutes=60)
            if reproduced.get("status") != "SUCCESS":
                return {"status": reproduced.get("status", "FAILED"), "context": reproduced}
            attempts.append(reproduced)
        controls = await self._step(
            "finding.controls",
            {"finding": candidate, "snapshot": fresh, "attempts": attempts},
        )
        duplicate = await self._step(
            "finding.dedupe",
            {"finding": candidate, "attempts": attempts, "controls": controls},
        )
        return await self._step(
            "finding.finalize",
            {
                "finding": candidate,
                "attempts": attempts,
                "controls": controls,
                "duplicate": duplicate,
            },
        )


@workflow.defn(name="IssueDeliveryWorkflow")
class IssueDeliveryWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: IssueDeliveryInput) -> dict[str, Any]:
        context = await self._step("delivery.load_confirmed_finding", asdict(request))
        ordered_steps = (
            "delivery.ensure_issue",
            "delivery.acquire_leases",
            "delivery.route_providers",
            "delivery.allocate_worktrees",
            "delivery.persist_worktree_leases",
            "delivery.deterministic_prechecks",
            "delivery.prepare_spec",
            "delivery.invoke_spec",
            "delivery.create_openspec",
            "delivery.prepare_maker",
            "delivery.invoke_maker",
            "delivery.prepare_deployment_maker",
            "delivery.invoke_deployment_maker",
            "delivery.prepare_checker",
            "delivery.invoke_checker",
            "delivery.validate_changeset",
            "delivery.prepare_publication",
            "delivery.publish",
            "delivery.persist_publication",
        )
        for step in ordered_steps:
            if not await self._checkpoint(step.upper().replace(".", "_")):
                return {"status": "CANCELLED", "context": context}
            context = await self._step(
                step,
                {**context, "delivery_request": asdict(request)},
                timeout_minutes=120 if "invoke" in step else 30,
            )
            if (
                context.get("status")
                in {
                    "RATE_LIMITED",
                    "BLOCKED",
                    "FAILED",
                    "TIMED_OUT",
                    "CANCELLED",
                    "REQUEST_CHANGES",
                    "DRY_RUN",
                }
                and step != "delivery.publish"
            ):
                if step == "delivery.prepare_publication":
                    context = await self._step("delivery.persist_handoff", context)
                return {"status": context["status"], "context": context}
        return {"status": context.get("status", "SOURCE_PR"), "context": context}


@workflow.defn(name="DeliveryReconciliationWorkflow")
class DeliveryReconciliationWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: DeliveryReconciliationInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        ordered = (
            "reconcile.prepare",
            "reconcile.inspect_ci",
            "reconcile.persist_ci",
        )
        for step in ordered:
            if not await self._checkpoint(step.upper().replace(".", "_")):
                return {"status": "CANCELLED", "context": context}
            context = await self._step(step, context, timeout_minutes=30)
        if context.get("status") != "CANDIDATE_BUILT":
            return {"status": context.get("status", "BLOCKED"), "context": context}

        if not await self._checkpoint("RECONCILE_PREPARE_PROMOTION"):
            return {"status": "CANCELLED", "context": context}
        context = await self._step("reconcile.prepare_promotion", context)
        if context.get("status") == "PROMOTION_PUBLICATION_READY":
            pass
        elif context.get("status") != "PROMOTION_READY":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        else:
            if not await self._checkpoint("RECONCILE_APPLY_PROMOTION"):
                return {"status": "CANCELLED", "context": context}
            context = await self._step("reconcile.apply_promotion", context, timeout_minutes=60)
            context = await self._step("reconcile.persist_promotion", context)
            if context.get("status") != "PROMOTION_PUBLICATION_READY":
                return {"status": context.get("status", "BLOCKED"), "context": context}

        if not await self._checkpoint("RECONCILE_PUBLISH_PROMOTION"):
            return {"status": "CANCELLED", "context": context}
        context = await self._step("reconcile.publish_promotion", context, timeout_minutes=30)
        context = await self._step("reconcile.persist_promotion_publication", context)
        return {"status": context.get("status", "DEPLOY_PR"), "context": context}


@workflow.defn(name="PreviewDeploymentWorkflow")
class PreviewDeploymentWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: PreviewDeploymentInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        try:
            for step in ("preview.prepare", "preview.preflight"):
                if not await self._checkpoint(step.upper().replace(".", "_")):
                    context = await self._step(
                        "deployment.record_terminal",
                        {**context, "terminal_status": "CANCELLED"},
                    )
                    return {"status": "CANCELLED", "context": context}
                context = await self._step(step, context, timeout_minutes=60)
                if context.get("status") in {"BLOCKED", "FAILED", "CANCELLED"}:
                    return {"status": context["status"], "context": context}
            if request.dry_run:
                context = await self._step(
                    "deployment.record_terminal",
                    {**context, "terminal_status": "PREVIEW_PREFLIGHT_VALIDATED"},
                )
                return {"status": "PREVIEW_PREFLIGHT_VALIDATED", "context": context}
            context = await self._step("preview.record_candidate", context)
            if context.get("status") == "BLOCKED":
                return {"status": "BLOCKED", "context": context}
            automatic_approval = context.get("automatic_approval")
            if isinstance(automatic_approval, dict):
                self._approval = automatic_approval
            else:
                self._phase = "AWAITING_PREVIEW_APPROVAL"
                await workflow.wait_condition(lambda: self._approval is not None or self._cancelled)
                if self._cancelled:
                    context = await self._step(
                        "deployment.record_terminal",
                        {**context, "terminal_status": "CANCELLED"},
                    )
                    return {"status": "CANCELLED", "context": context}
            context = await self._step(
                "preview.validate_approval",
                {"context": context, "approval": self._approval},
            )
            if context.get("status") != "PREVIEW_APPROVED":
                return {"status": context.get("status", "BLOCKED"), "context": context}
            context = await self._step("preview.deploy", context, timeout_minutes=60)
            context = await self._step("preview.verify", context, timeout_minutes=60)
            context = await self._step("preview.finalize", context)
            return {"status": context.get("status", "PREVIEW_VERIFIED"), "context": context}
        except ActivityError as error:
            context = await self._deployment_failure(context, error)
            return {"status": "DEPLOYMENT_FAILED", "context": context}


@workflow.defn(name="StagingDeploymentWorkflow")
class StagingDeploymentWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: StagingDeploymentInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        try:
            if not await self._checkpoint("STAGING_PREPARE"):
                context = await self._step(
                    "deployment.record_terminal", {**context, "terminal_status": "CANCELLED"}
                )
                return {"status": "CANCELLED", "context": context}
            context = await self._step("staging.prepare", context)
            if context.get("status") == "BLOCKED":
                return {"status": "BLOCKED", "context": context}
            if not await self._checkpoint("STAGING_PREFLIGHT"):
                context = await self._step(
                    "deployment.record_terminal", {**context, "terminal_status": "CANCELLED"}
                )
                return {"status": "CANCELLED", "context": context}
            context = await self._step("staging.preflight", context, timeout_minutes=60)
            if context.get("status") in {"BLOCKED", "FAILED"}:
                return {"status": context["status"], "context": context}
            if request.dry_run:
                context = await self._step(
                    "deployment.record_terminal",
                    {**context, "terminal_status": "STAGING_PREFLIGHT_VALIDATED"},
                )
                return {"status": "STAGING_PREFLIGHT_VALIDATED", "context": context}
            context = await self._step("staging.record_candidate", context)
            if context.get("status") == "BLOCKED":
                context = await self._step(
                    "deployment.record_terminal",
                    {
                        **context,
                        "terminal_status": "BLOCKED",
                        "blocker": context.get("blocker", "STAGING_DEPLOYMENT_BLOCKED"),
                    },
                )
                return {"status": "BLOCKED", "context": context}
            automatic_approval = context.get("automatic_approval")
            if isinstance(automatic_approval, dict):
                self._approval = automatic_approval
            else:
                self._phase = "AWAITING_STAGING_APPROVAL"
                await workflow.wait_condition(lambda: self._approval is not None or self._cancelled)
                if self._cancelled:
                    context = await self._step(
                        "deployment.record_terminal",
                        {**context, "terminal_status": "CANCELLED"},
                    )
                    return {"status": "CANCELLED", "context": context}
            context = await self._step(
                "staging.validate_approval", {"context": context, "approval": self._approval}
            )
            if context.get("status") != "STAGING_APPROVED":
                return {"status": context.get("status", "BLOCKED"), "context": context}
            context = await self._step("staging.deploy", context, timeout_minutes=60)
            context = await self._step("staging.finalize", context)
            return {"status": context.get("status", "STAGING_DEPLOYED"), "context": context}
        except ActivityError as error:
            context = await self._deployment_failure(context, error)
            return {"status": "DEPLOYMENT_FAILED", "context": context}


@workflow.defn(name="RuntimeVerificationWorkflow")
class RuntimeVerificationWorkflow(ControlledWorkflow):
    _reported_runtime_errors = frozenset(
        {
            "MANUAL_AUTH_REQUIRED_CODEX",
            "MANUAL_AUTH_REQUIRED_CLAUDE",
            "PROVIDER_CLI_MISSING_CODEX",
            "PROVIDER_CLI_MISSING_CLAUDE",
            "RATE_LIMITED",
            "RuntimeObservationUnavailable",
            "BrowserRunnerFailure",
            "SecurityBlocker",
            "PolicyViolation",
            "SchemaViolation",
        }
    )

    @workflow.run
    async def run(self, request: RuntimeVerificationInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        try:
            for step in (
                "runtime.snapshot",
                "runtime.deterministic_checks",
                "runtime.browser_checks",
            ):
                if not await self._checkpoint(step.upper().replace(".", "_")):
                    context = await self._step(
                        "runtime.record_failure",
                        {**context, "blocker": "CANCELLED"},
                    )
                    return {"status": "CANCELLED", "context": context}
                context = await self._step(step, context, timeout_minutes=60)
                if step == "runtime.snapshot" and context.get("status") in {
                    "BLOCKED",
                    "RATE_LIMITED",
                }:
                    return {"status": context["status"], "context": context}
            context = await self._step("runtime.prepare_review", context)
            requests = context.get("runtime_review_requests")
            if not isinstance(requests, list) or not requests:
                raise ValueError("runtime review preparation returned no requests")
            review_results: list[dict[str, Any]] = []
            for request_value in requests:
                if not isinstance(request_value, dict):
                    raise TypeError("runtime review request must be an object")
                if not await self._checkpoint("RUNTIME_INDEPENDENT_REVIEW"):
                    context = await self._step(
                        "runtime.record_failure",
                        {**context, "blocker": "CANCELLED"},
                    )
                    return {"status": "CANCELLED", "context": context}
                reviewed = await self._step(
                    "runtime.independent_review",
                    {
                        **context,
                        "provider": request_value.get("provider"),
                        "runtime_review_request": request_value,
                    },
                    timeout_minutes=60,
                )
                review_status = reviewed.get("status")
                if review_status != "SUCCESS":
                    blocker = (
                        "RATE_LIMITED"
                        if review_status == "RATE_LIMITED"
                        else "RUNTIME_REVIEW_FAILED"
                    )
                    context = await self._step(
                        "runtime.record_failure",
                        {**context, "blocker": blocker},
                    )
                    return {"status": context.get("status", "BLOCKED"), "context": context}
                review_results.append(reviewed)
            context = await self._step(
                "runtime.finalize",
                {**context, "runtime_review_results": review_results},
            )
            return {"status": context.get("status", "FAILED"), "context": context}
        except ActivityError as error:
            cause = error.cause
            blocker = (
                cause.type
                if isinstance(cause, ApplicationError)
                and cause.type in self._reported_runtime_errors
                else "RUNTIME_ACTIVITY_FAILED"
            )
            context = await self._step(
                "runtime.record_failure",
                {**context, "blocker": blocker},
            )
            return {"status": context.get("status", "BLOCKED"), "context": context}


@workflow.defn(name="RecoveryWorkflow")
class RecoveryWorkflow(ControlledWorkflow):
    _reported_recovery_errors = frozenset(
        {
            "ApprovalMismatch",
            "PolicyViolation",
            "ProtectedRecoveryPRRequired",
        }
    )

    @workflow.run
    async def run(self, request: RecoveryInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        try:
            context = await self._step("recovery.inspect", context)
            if context.get("status") == "BLOCKED":
                return {"status": "BLOCKED", "context": context}
            context = await self._step("recovery.validate_binding", context)
            if request.dry_run or context.get("status") == "FORWARD_RECOVERY_REQUIRED":
                return {
                    "status": context.get("status", "RECOVERY_PLAN_VALIDATED"),
                    "context": context,
                }
            context = await self._step("recovery.prepare_candidate", context)
            if context.get("status") == "BLOCKED":
                return {"status": "BLOCKED", "context": context}
            self._phase = "AWAITING_RECOVERY_APPROVAL"
            await workflow.wait_condition(lambda: self._approval is not None or self._cancelled)
            if self._cancelled:
                context = await self._step(
                    "recovery.record_failure", {**context, "blocker": "CANCELLED"}
                )
                return {"status": "CANCELLED", "context": context}
            context = await self._step(
                "recovery.validate_approval",
                {"context": context, "approval": self._approval or {}},
            )
            if context.get("status") != "RECOVERY_APPROVED":
                return {"status": context.get("status", "BLOCKED"), "context": context}
            context = await self._step("recovery.execute", context, timeout_minutes=60)
            verified = await self._step("recovery.verify", context, timeout_minutes=60)
            return {"status": verified.get("status", "RECOVERED"), "context": verified}
        except ActivityError as error:
            cause = error.cause
            blocker = (
                cause.type
                if isinstance(cause, ApplicationError)
                and cause.type in self._reported_recovery_errors
                else "RECOVERY_ACTIVITY_FAILED"
            )
            context = await self._step("recovery.record_failure", {**context, "blocker": blocker})
            return {"status": context.get("status", "BLOCKED"), "context": context}


@workflow.defn(name="QuotaRefreshWorkflow")
class QuotaRefreshWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: QuotaRefreshInput) -> dict[str, Any]:
        snapshots = []
        for provider in request.providers:
            snapshots.append(await self._step("quota.refresh", {"provider": provider}))
        await self._step("quota.persist", {"snapshots": snapshots})
        return {"status": "COMPLETE", "snapshots": snapshots}


@workflow.defn(name="CleanupWorkflow")
class CleanupWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: CleanupInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        try:
            context = await self._step("cleanup.discover", context)
            context = await self._step("cleanup.protect_active", context)
            context = await self._step("cleanup.inspect_evidence_files", context)
            context = await self._step("cleanup.inspect_delivery_files", context)
            context = await self._step("cleanup.inspect_browser_files", context)
            context = await self._step("cleanup.inspect_codex_files", context)
            context = await self._step("cleanup.inspect_preview_namespaces", context)
            if request.dry_run:
                context = await self._step(
                    "cleanup.finalize", {**context, "terminal_status": "DRY_RUN"}
                )
                return {"status": "DRY_RUN", "context": context}
            context = await self._step("cleanup.authorize", context)
            if context.get("status") == "BLOCKED":
                context = await self._step(
                    "cleanup.finalize", {**context, "terminal_status": "BLOCKED"}
                )
                return {"status": "BLOCKED", "context": context}
            context = await self._step(
                "cleanup.execute_evidence_files", {**context, "dry_run": False}
            )
            context = await self._step(
                "cleanup.execute_delivery_files", {**context, "dry_run": False}
            )
            context = await self._step(
                "cleanup.execute_browser_files", {**context, "dry_run": False}
            )
            context = await self._step("cleanup.execute_codex_files", {**context, "dry_run": False})
            context = await self._step(
                "cleanup.execute_preview_namespaces", {**context, "dry_run": False}
            )
            context = await self._step("cleanup.execute", {**context, "dry_run": False})
            context = await self._step("cleanup.execute_metadata", {**context, "dry_run": False})
            context = await self._step(
                "cleanup.finalize", {**context, "terminal_status": "COMPLETE"}
            )
            return {"status": "COMPLETE", "context": context}
        except ActivityError:
            context = await self._step(
                "cleanup.finalize",
                {
                    **context,
                    "terminal_status": "BLOCKED",
                    "blocker": "CLEANUP_ACTIVITY_FAILED",
                },
            )
            return {"status": "BLOCKED", "context": context}


@workflow.defn(name="AcceptanceLifecycleWorkflow")
class AcceptanceLifecycleWorkflow(ControlledWorkflow):
    """Synthetic end-to-end lifecycle used only by the explicit validation worker."""

    @workflow.run
    async def run(self, request: AcceptanceLifecycleInput) -> dict[str, Any]:
        context: dict[str, Any] = {
            **asdict(request),
            "run_id": workflow.info().run_id,
            "created_at": workflow.now().isoformat(),
        }
        for step, timeout in (
            ("acceptance.project_snapshot", 2),
            ("acceptance.worktree", 2),
            ("acceptance.maker", 2),
            ("acceptance.checker", 2),
            ("acceptance.evidence", 2),
            ("acceptance.transition", 2),
        ):
            if not await self._checkpoint(step.upper().replace(".", "_")):
                return {"status": "CANCELLED", **context}
            context = await workflow.execute_activity(
                step,
                context,
                task_queue="engineering-acceptance",
                start_to_close_timeout=timedelta(minutes=timeout),
                heartbeat_timeout=timedelta(seconds=10),
                retry_policy=ACTIVITY_RETRY,
            )
        return context


ALL_WORKFLOWS = [
    AuditCampaignWorkflow,
    FindingVerificationWorkflow,
    IssueDeliveryWorkflow,
    DeliveryReconciliationWorkflow,
    PreviewDeploymentWorkflow,
    StagingDeploymentWorkflow,
    RuntimeVerificationWorkflow,
    RecoveryWorkflow,
    QuotaRefreshWorkflow,
    CleanupWorkflow,
    AcceptanceLifecycleWorkflow,
]
