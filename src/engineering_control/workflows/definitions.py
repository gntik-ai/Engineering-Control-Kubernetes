from __future__ import annotations

from dataclasses import asdict, replace
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

from engineering_control.workflows.inputs import (
    AcceptanceLifecycleInput,
    AuditCampaignInput,
    CleanupInput,
    DeliveryReconciliationInput,
    EnhancementDeliveryInput,
    EnhancementDiscoveryInput,
    EnhancementSolutionLoopInput,
    ExistingGitHubBacklogLoopInput,
    FindingVerificationInput,
    GitHubBacklogImportInput,
    IssueDeliveryInput,
    IssueSolutionLoopInput,
    LLMWikiStagingBootstrapInput,
    MultiProjectAuditInput,
    ObservabilityAcceptanceInput,
    PreviewDeploymentInput,
    QuotaRefreshInput,
    RecoveryInput,
    RuntimeVerificationInput,
    StagingDeploymentInput,
)
from engineering_control.workflows.worker_registry import (
    CONTROL_TASK_QUEUE,
    GITHUB_TASK_QUEUE,
    provider_task_queue,
    task_queue_for,
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
        self._pause_after_current = False
        self._stop_after_current = False
        self._approval: dict[str, Any] | None = None
        self._phase = "INITIALIZING"
        self._skip_reason: str | None = None
        self._retry_step: str | None = None
        self._write_policy_reason: str | None = None

    @workflow.signal(name="pause")
    async def pause(self) -> None:
        self._paused = True

    @workflow.signal(name="resume")
    async def resume(self) -> None:
        self._paused = False
        self._pause_after_current = False

    @workflow.signal(name="cancel")
    async def cancel(self) -> None:
        self._cancelled = True
        self._paused = False
        self._pause_after_current = False
        self._stop_after_current = False

    @workflow.signal(name="pause_after_current")
    async def pause_after_current(self) -> None:
        if not self._cancelled and not self._stop_after_current:
            self._pause_after_current = True

    @workflow.signal(name="stop_after_current")
    async def stop_after_current(self) -> None:
        if not self._cancelled:
            self._stop_after_current = True
            self._pause_after_current = False

    @workflow.signal(name="approval")
    async def approval(self, payload: dict[str, Any]) -> None:
        self._approval = payload

    @workflow.signal(name="skip")
    async def skip(self, reason: str) -> None:
        self._skip_reason = reason[:500]

    @workflow.signal(name="retry")
    async def retry(self, step: str) -> None:
        self._retry_step = step[:120]

    @workflow.query(name="status")
    def status(self) -> dict[str, Any]:
        return {
            "phase": self._phase,
            "paused": self._paused,
            "cancelled": self._cancelled,
            "pause_after_current": self._pause_after_current,
            "stop_after_current": self._stop_after_current,
            "approval_received": self._approval is not None,
            "skip_reason": self._skip_reason,
            "retry_step": self._retry_step,
            "write_policy_reason": self._write_policy_reason,
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

    async def _github_write_step(
        self, step: str, payload: dict[str, Any], *, timeout_minutes: int = 30
    ) -> dict[str, Any]:
        """Wait durably for an explicit retry signal after a terminal policy denial."""
        self._retry_step = None
        while True:
            result = await self._step(step, payload, timeout_minutes=timeout_minutes)
            if result.get("status") != "BLOCKED_BY_POLICY":
                self._write_policy_reason = None
                return result
            self._phase = "AWAITING_WRITE_POLICY"
            self._write_policy_reason = str(result.get("reason_code", "POLICY_INVALID"))[:120]
            await workflow.wait_condition(lambda: self._cancelled or self._retry_step == step)
            if self._cancelled:
                return {**result, "status": "CANCELLED", "workflow_state": "CANCELLED"}
            self._retry_step = None

    async def _agent_step(
        self, step: str, payload: dict[str, Any], *, timeout_minutes: int
    ) -> dict[str, Any]:
        """Persist an invocation boundary before and after a native provider activity."""
        started = await self._step(
            "agent.record_start",
            {**payload, "provider_step": step},
        )
        try:
            result = await self._step(step, started, timeout_minutes=timeout_minutes)
        except ActivityError as error:
            cause = error.cause
            blocker = (
                cause.type
                if isinstance(cause, ApplicationError)
                and cause.type
                in {
                    "MANUAL_AUTH_REQUIRED_CODEX",
                    "MANUAL_AUTH_REQUIRED_CLAUDE",
                    "PROVIDER_CLI_MISSING_CODEX",
                    "PROVIDER_CLI_MISSING_CLAUDE",
                    "RATE_LIMITED",
                    "SchemaViolation",
                    "PolicyViolation",
                }
                else "PROVIDER_ACTIVITY_FAILED"
            )
            return await self._step(
                "agent.persist_result",
                {**started, "status": "FAILED", "blocker": blocker},
            )
        return await self._step("agent.persist_result", result)

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
            return CONTROL_TASK_QUEUE
        if step in {"cleanup.inspect_delivery_files", "cleanup.execute_delivery_files"}:
            return task_queue_for("delivery")
        if step in {"cleanup.inspect_browser_files", "cleanup.execute_browser_files"}:
            return task_queue_for("browser")
        if step in {"cleanup.inspect_codex_files", "cleanup.execute_codex_files"}:
            return task_queue_for("codex")
        if step in {"cleanup.inspect_preview_namespaces", "cleanup.execute_preview_namespaces"}:
            return task_queue_for("release")
        if step in {
            "project.snapshot",
            "project.snapshot_pair",
            "deployment.compare_live",
            "audit.static_source",
            "audit.static_deployment",
            "audit.kubernetes_observe",
            "audit.correlate_runtime",
            "finding.fresh_snapshot",
            "delivery.allocate_worktrees",
            "delivery.deterministic_prechecks",
            "delivery.validate_changeset",
            "audit.deterministic_cell",
            "runtime.deterministic_checks",
            "identity.observer_attest",
            "cleanup.protect_active",
            "cleanup.execute",
            "observability.prepare_workspace",
            "observability.cleanup_workspace",
        }:
            return task_queue_for("scan")
        if step == "finding.reproduce":
            return provider_task_queue(
                cls._nested_value(payload, "provider", "claude"), default="claude"
            )
        if step == "audit.agent_cell":
            return provider_task_queue(
                cls._nested_value(payload, "provider", "codex"), default="codex"
            )
        if step in {
            "audit.api_flow",
            "audit.browser_flow",
            "audit.llmwiki_consumer_suite",
            "identity.superadmin_attest",
            "identity.inspect_stale",
            "identity.recover_stale",
        }:
            return task_queue_for("browser")
        if step in {"deployment.render"}:
            return task_queue_for("release")
        if step in {
            "finding.dedupe",
            "finding.publish",
            "enhancement.publish",
            "enhancement.github_dedupe",
        }:
            return GITHUB_TASK_QUEUE
        if step == "llmwiki.bootstrap.plan":
            return GITHUB_TASK_QUEUE
        if step in {"github.refresh_mirrors", "backlog.github_import"}:
            return GITHUB_TASK_QUEUE
        if step == "llmwiki.bootstrap.execute":
            return task_queue_for("release")
        if step in {"enhancement.discover", "enhancement.product_review"}:
            return provider_task_queue(
                cls._nested_value(payload, "provider", "codex"), default="codex"
            )
        if step == "delivery.invoke_spec":
            return provider_task_queue(
                cls._nested_value(payload, "spec_provider", "claude"), default="claude"
            )
        if step == "delivery.invoke_maker":
            return provider_task_queue(
                cls._nested_value(payload, "maker_provider", "codex"), default="codex"
            )
        if step == "delivery.invoke_deployment_maker":
            return provider_task_queue(
                cls._nested_value(payload, "maker_provider", "codex"), default="codex"
            )
        if step == "delivery.invoke_checker":
            return provider_task_queue(
                cls._nested_value(payload, "checker_provider", "claude"), default="claude"
            )
        if step == "observability.invoke_codex":
            return provider_task_queue("codex", default="codex")
        if step == "observability.invoke_claude":
            return provider_task_queue("claude", default="claude")
        if step == "reconcile.invoke_release_reviewer":
            return provider_task_queue(
                cls._nested_value(payload, "release_review_provider", "claude"),
                default="claude",
            )
        if step == "quota.refresh":
            return provider_task_queue(
                cls._nested_value(payload, "provider", "codex"), default="codex"
            )
        if step == "runtime.independent_review":
            return provider_task_queue(
                cls._nested_value(payload, "provider", "codex"), default="codex"
            )
        if step == "delivery.publish":
            return GITHUB_TASK_QUEUE
        if step in {"reconcile.inspect_ci", "reconcile.publish_promotion"}:
            return GITHUB_TASK_QUEUE
        if step == "reconcile.apply_promotion":
            return task_queue_for("scan")
        if step in {"runtime.browser_checks"}:
            return task_queue_for("browser")
        if step in {
            "preview.preflight",
            "preview.verify",
            "staging.preflight",
            "preview.deploy",
            "staging.deploy",
            "recovery.execute",
        }:
            return task_queue_for("release")
        return CONTROL_TASK_QUEUE

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
        plan_payload = asdict(request)
        if request.preset in {"e2e-smoke", "full-e2e", "security", "falcone-intensive"}:
            authority = "falcone" if request.project == "llmwiki" else request.project
            plan_payload["worker_attestation"] = await self._step(
                "identity.superadmin_attest", {"project": authority}
            )
        if request.preset != "static-full":
            plan_payload["observer_attestation"] = await self._step(
                "identity.observer_attest", {"project": request.project}
            )
        plan = await self._step("campaign.plan", plan_payload)
        if request.plan_only:
            return plan
        repository_surfaces = request.repository_surfaces or list(
            plan.get("repository_surfaces", [])
        )
        domains = request.domains or list(plan.get("domains", ["product"]))
        personas = request.personas or list(plan.get("personas", ["anonymous"]))
        flows = request.flows or list(plan.get("flows", ["repository-static"]))
        lenses = request.lenses or list(plan.get("lenses", ["correctness"]))
        environments = request.environments or [request.environment]
        if not flows:
            flows = ["repository-static"]
        if not repository_surfaces:
            repository_surfaces = ["source", "deployment", "pair"]
        if not await self._checkpoint("SNAPSHOT"):
            return {"status": "CANCELLED"}
        cells = [
            {
                "repository_surface": repository_surface,
                "domain": domain,
                "persona": persona,
                "flow": flow,
                "lens": lens,
                "environment": environment,
            }
            for repository_surface in repository_surfaces
            for domain in domains
            for persona in personas
            for flow in flows
            for lens in lenses
            for environment in environments
        ]
        if len(cells) > 100_000:
            return {"status": "BLOCKED", "blocker": "COVERAGE_MATRIX_LIMIT"}
        batch = cells[request.cell_offset : request.cell_offset + 50]
        snapshot: dict[str, Any] | None = None
        snapshot_blocker = request.snapshot_blocker
        root_blocker_classification = request.root_blocker_classification
        if snapshot_blocker is None:
            try:
                await self._step("github.refresh_mirrors", {"project": request.project})
                snapshot = await self._step("project.snapshot", asdict(request))
            except ActivityError:
                snapshot_blocker = "PROJECT_SNAPSHOT_UNAVAILABLE"
                root_blocker_classification = "EXTERNAL_DEPENDENCY_FAILURE"
        campaign_id = request.campaign_id or (snapshot or {}).get("campaign_id")
        if not campaign_id:
            return {"status": "BLOCKED", "blocker": "PROJECT_SNAPSHOT_UNAVAILABLE"}
        if snapshot_blocker is not None:
            await self._step(
                "campaign.persist_snapshot_blockers",
                {
                    "campaign_id": campaign_id,
                    "cells": batch,
                    "total": len(cells),
                    "blocker": snapshot_blocker,
                    "root_blocker_classification": root_blocker_classification,
                },
            )
            next_offset = request.cell_offset + len(batch)
            if next_offset < len(cells):
                workflow.continue_as_new(
                    replace(
                        request,
                        campaign_id=campaign_id,
                        cell_offset=next_offset,
                        snapshot_blocker=snapshot_blocker,
                        root_blocker_classification=root_blocker_classification,
                    )
                )
            summary = await self._step(
                "campaign.finalize",
                {
                    "campaign_id": campaign_id,
                    "expected_cells": len(cells),
                    "terminal_blocker": (
                        snapshot_blocker
                        if snapshot_blocker == "BOOTSTRAP_VALIDATION_SUPERSEDED"
                        else None
                    ),
                },
            )
            return {
                "status": summary.get("status", "INCOMPLETE"),
                "campaign_id": campaign_id,
                "blocker": snapshot_blocker,
                "summary": summary,
            }
        if snapshot is None:
            raise RuntimeError("project snapshot result is missing")
        await self._step(
            "project.persist_snapshot",
            {
                "campaign_id": campaign_id,
                "project": request.project,
                "snapshot": snapshot,
            },
        )
        if plan.get("blockers"):
            return {
                "status": "BLOCKED_BY_CREDENTIAL",
                "campaign_id": campaign_id,
                "blockers": plan["blockers"],
                "plan": plan,
                "snapshot": snapshot,
            }
        e2e = "runtime" in repository_surfaces
        if e2e:
            pair_snapshot = await self._step(
                "project.snapshot_pair", {**asdict(request), "snapshot": snapshot}
            )
            await self._step(
                "project.persist_snapshot",
                {
                    "campaign_id": campaign_id,
                    "project": request.project,
                    "snapshot": snapshot,
                    "pair_snapshot": pair_snapshot,
                },
            )
            if pair_snapshot.get("status") in {"BLOCKED", "LIVE_REVISION_DRIFT"}:
                return {
                    "status": "BLOCKED",
                    "campaign_id": campaign_id,
                    "blocker": pair_snapshot.get("blocker", pair_snapshot.get("status")),
                    "snapshot": pair_snapshot,
                }
            snapshot = {**snapshot, "pair_snapshot": pair_snapshot}
        stage_context: dict[str, Any] = {
            **asdict(request),
            "campaign_id": campaign_id,
            "snapshot": snapshot,
            "plan": plan,
        }
        if request.preset == "falcone-intensive":
            await self._step("github.refresh_mirrors", {"project": "llmwiki"})
            stage_context["llmwiki_consumer_snapshot"] = await self._step(
                "project.snapshot",
                {
                    "project": "llmwiki",
                    "campaign_id": campaign_id,
                    "environment": "staging",
                },
            )
        stages = self._audit_stages(e2e=e2e, preset=request.preset)
        for stage in stages:
            if not await self._checkpoint(stage.upper().replace(".", "_")):
                return {"status": "CANCELLED", "campaign_id": campaign_id}
            stage_context = await self._step(stage, stage_context, timeout_minutes=120)
            if stage_context.get("status") in {
                "BLOCKED",
                "BLOCKED_BY_CREDENTIAL",
                "FAILED",
            }:
                return {
                    "status": "BLOCKED",
                    "campaign_id": campaign_id,
                    "blocker": stage_context.get("blocker", stage_context["status"]),
                    "context": stage_context,
                }
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
                await self._step(
                    "campaign.persist_cell_blocker",
                    {
                        **prepared,
                        "deterministic_evidence_sha256": deterministic.get("evidence_sha256"),
                        "blocker": prepared.get("blocker", prepared["status"]),
                    },
                )
                continue
            agent_result = await self._agent_step(
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
                await self._step(
                    "campaign.persist_cell_blocker",
                    {
                        **agent_result,
                        "deterministic_evidence_sha256": deterministic.get("evidence_sha256"),
                        "blocker": agent_result.get("blocker", agent_result["status"]),
                    },
                )
                continue
            persisted = await self._step(
                "campaign.persist_cell_result",
                {
                    "campaign_id": campaign_id,
                    "cell": cell,
                    "agent_result": agent_result.get("agent_result"),
                    "deterministic_evidence_sha256": deterministic.get("evidence_sha256"),
                },
            )
            if persisted.get("status") == "CANDIDATE_FOUND":
                finding_id = str(persisted.get("finding_id", ""))
                if not finding_id:
                    raise ValueError("candidate coverage result did not return a finding ID")
                verification = await workflow.execute_child_workflow(
                    "FindingVerificationWorkflow",
                    FindingVerificationInput(
                        finding_id=finding_id,
                        project=request.project,
                        fresh_snapshot=True,
                    ),
                    id=f"finding-verify-{finding_id}",
                    task_queue=workflow.info().task_queue,
                    execution_timeout=timedelta(hours=4),
                )
                if verification.get("status") in {"RATE_LIMITED", "BLOCKED", "FAILED"}:
                    continue
                if verification.get("status") == "CONFIRMED" and request.publish_findings:
                    publication = await self._step(
                        "finding.prepare_publication",
                        {"finding_id": finding_id, "campaign_id": campaign_id},
                    )
                    if publication.get("status") == "PUBLICATION_READY":
                        publication = await self._github_write_step(
                            "finding.publish", {**publication, "dry_run": False}
                        )
                    if publication.get("status") == "PUBLISHED":
                        publication = await self._step("finding.persist_publication", publication)
                    if publication.get("status") not in {"PUBLISHED", "ALREADY_PUBLISHED"}:
                        return {
                            "status": "BLOCKED",
                            "campaign_id": campaign_id,
                            "blocker": publication.get("blocker", "FINDING_PUBLICATION_FAILED"),
                            "publication": publication,
                        }
        next_offset = request.cell_offset + len(batch)
        if next_offset < len(cells):
            workflow.continue_as_new(
                replace(
                    request,
                    campaign_id=campaign_id,
                    cell_offset=next_offset,
                    snapshot_blocker=None,
                )
            )
        summary = await self._step(
            "campaign.finalize", {"campaign_id": campaign_id, "expected_cells": len(cells)}
        )
        return {
            "status": summary.get("status", "INCOMPLETE"),
            "campaign_id": campaign_id,
            "summary": summary,
        }

    @staticmethod
    def _audit_stages(*, e2e: bool, preset: str) -> list[str]:
        stages = [
            "audit.static_source",
            "audit.static_deployment",
            "deployment.render",
        ]
        if e2e:
            stages.extend(
                [
                    "deployment.compare_live",
                    "audit.api_flow",
                    "audit.browser_flow",
                    "audit.kubernetes_observe",
                    "audit.correlate_runtime",
                ]
            )
        if preset == "falcone-intensive":
            stages.append("audit.llmwiki_consumer_suite")
        stages.extend(
            [
                "audit.classify_candidate",
                "audit.discover_enhancements",
                "audit.verify_cleanup",
            ]
        )
        return stages


@workflow.defn(name="MultiProjectAuditWorkflow")
class MultiProjectAuditWorkflow(ControlledWorkflow):
    """Execute the fixed project catalog sequentially under one durable parent."""

    _projects = ("falcone", "musematic", "llmwiki")
    _presets = frozenset(
        {
            "snapshot",
            "static-full",
            "e2e-smoke",
            "full-e2e",
            "security",
            "ux",
            "deployment",
        }
    )
    _child_statuses = frozenset(
        {
            "BLOCKED",
            "BLOCKED_BY_CREDENTIAL",
            "CANCELLED",
            "COMPLETE",
            "FAILED",
            "INCOMPLETE",
            "SUCCESS",
            "TIMED_OUT",
            "VERIFICATION_REQUIRED",
        }
    )

    @workflow.run
    async def run(self, request: MultiProjectAuditInput) -> dict[str, Any]:
        if (
            request.environment != "staging"
            or request.preset not in self._presets
            or set(request.campaign_ids) != set(self._projects)
            or set(request.child_workflow_ids) != set(self._projects)
        ):
            raise ApplicationError(
                "all-project audit binding is invalid",
                type="PolicyViolation",
                non_retryable=True,
            )
        parent_workflow_id = workflow.info().workflow_id
        await self._step(
            "campaign.group_status",
            {
                "workflow_id": parent_workflow_id,
                "status": "RUNNING",
                "preset": request.preset,
                "environment": request.environment,
                "project_results": {},
            },
        )
        results: dict[str, dict[str, str | None]] = {}
        for project in self._projects:
            if not await self._checkpoint(f"AUDIT_{project.upper()}"):
                await self._step(
                    "campaign.group_status",
                    {
                        "workflow_id": parent_workflow_id,
                        "status": "CANCELLED",
                        "preset": request.preset,
                        "environment": request.environment,
                        "project_results": results,
                    },
                )
                return {"status": "CANCELLED", "project_results": results}
            try:
                child = await workflow.execute_child_workflow(
                    "AuditCampaignWorkflow",
                    AuditCampaignInput(
                        project=project,
                        name=f"{request.preset}-staging-all-projects",
                        preset=request.preset,
                        environment="staging",
                        dry_run=False,
                        publish_findings=request.publish_findings,
                        campaign_id=request.campaign_ids[project],
                    ),
                    id=request.child_workflow_ids[project],
                    task_queue=workflow.info().task_queue,
                    execution_timeout=timedelta(days=7),
                )
            except ChildWorkflowError:
                child = {"status": "FAILED", "blocker": "AUDIT_CHILD_WORKFLOW_FAILED"}
            if not isinstance(child, dict):
                child = {"status": "FAILED", "blocker": "AUDIT_CHILD_RESULT_INVALID"}
            child_status = str(child.get("status", "FAILED")).upper()
            raw_blocker = child.get("blocker")
            blocker = str(raw_blocker).upper() if raw_blocker is not None else None
            if child_status not in self._child_statuses:
                child_status = "FAILED"
                blocker = "UNKNOWN_AUDIT_CHILD_STATUS"
            if blocker is not None and (
                not 1 <= len(blocker) <= 120
                or blocker[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                or any(
                    character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
                    for character in blocker
                )
            ):
                blocker = "AUDIT_CHILD_BLOCKED"
            result = {
                "status": child_status,
                "campaign_id": request.campaign_ids[project],
                "workflow_id": request.child_workflow_ids[project],
                "blocker": blocker,
            }
            results[project] = result
            await self._step(
                "campaign.group_status",
                {
                    "workflow_id": parent_workflow_id,
                    "status": "RUNNING",
                    "preset": request.preset,
                    "environment": request.environment,
                    "current_project": project,
                    "project_results": results,
                },
            )
        child_statuses = {str(result["status"]).upper() for result in results.values()}
        if child_statuses & {"FAILED", "TIMED_OUT"}:
            terminal = "FAILED"
        elif child_statuses - {"COMPLETE", "VERIFICATION_REQUIRED", "SUCCESS"}:
            terminal = "BLOCKED"
        else:
            terminal = "SUCCESS"
        await self._step(
            "campaign.group_status",
            {
                "workflow_id": parent_workflow_id,
                "status": terminal,
                "preset": request.preset,
                "environment": request.environment,
                "project_results": results,
            },
        )
        return {"status": terminal, "project_results": results}


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
                prepared = await self._step("finding.record_terminal", prepared)
                return {"status": prepared["status"], "context": prepared}
            reproduced = await self._agent_step("finding.reproduce", prepared, timeout_minutes=60)
            if reproduced.get("status") != "SUCCESS":
                reproduced = await self._step("finding.record_terminal", reproduced)
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
            step_payload = {**context, "delivery_request": asdict(request)}
            if step in {
                "delivery.invoke_spec",
                "delivery.invoke_maker",
                "delivery.invoke_deployment_maker",
                "delivery.invoke_checker",
            }:
                context = await self._agent_step(step, step_payload, timeout_minutes=120)
            else:
                if step == "delivery.publish":
                    context = await self._github_write_step(step, step_payload, timeout_minutes=30)
                else:
                    context = await self._step(step, step_payload, timeout_minutes=30)
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
        context = await self._github_write_step(
            "reconcile.publish_promotion", context, timeout_minutes=30
        )
        context = await self._step("reconcile.persist_promotion_publication", context)
        if context.get("status") != "RELEASE_REVIEW_REQUIRED":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        if not await self._checkpoint("RECONCILE_RELEASE_REVIEW"):
            return {"status": "CANCELLED", "context": context}
        context = await self._step("reconcile.prepare_release_review", context)
        if context.get("status") != "RELEASE_REVIEW_READY":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        context = await self._agent_step(
            "reconcile.invoke_release_reviewer", context, timeout_minutes=90
        )
        if context.get("agent_invocation_status") != "SUCCESS":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        context = await self._step("reconcile.persist_release_review", context)
        return {"status": context.get("status", "DEPLOY_PR"), "context": context}


@workflow.defn(name="PreviewDeploymentWorkflow")
class PreviewDeploymentWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: PreviewDeploymentInput) -> dict[str, Any]:
        if request.project == "llmwiki":
            raise ApplicationError(
                "LLMWiki rejects preview and production targets",
                type="PolicyViolation",
                non_retryable=True,
            )
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
                reviewed = await self._agent_step(
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
            context = await self._step("identity.inspect_stale", context)
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
            context = await self._step("identity.recover_stale", {**context, "dry_run": False})
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
        if request.activity_task_queue != "engineering-acceptance" and not (
            request.activity_task_queue.startswith("engineering-acceptance-validation-")
            and len(request.activity_task_queue) <= 128
        ):
            raise ValueError("acceptance activity task queue is outside its bounded namespace")
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
                task_queue=request.activity_task_queue,
                start_to_close_timeout=timedelta(minutes=timeout),
                heartbeat_timeout=timedelta(seconds=10),
                retry_policy=ACTIVITY_RETRY,
            )
        return context


@workflow.defn(name="ObservabilityAcceptanceWorkflow")
class ObservabilityAcceptanceWorkflow(ControlledWorkflow):
    """Read-only native Codex then Claude canary with no product or repository mutation."""

    @workflow.run
    async def run(self, request: ObservabilityAcceptanceInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        context = await self._step("observability.register", context)
        context = await self._step("observability.prepare_workspace", context)
        try:
            context = await self._step("observability.prepare_codex", context)
            context = await self._agent_step(
                "observability.invoke_codex", context, timeout_minutes=10
            )
            if context.get("status") != "SUCCESS":
                return await self._step(
                    "observability.finalize",
                    {**context, "terminal_status": "FAILED", "blocker": context.get("blocker")},
                )
            context = await self._step("observability.prepare_claude", context)
            context = await self._agent_step(
                "observability.invoke_claude", context, timeout_minutes=10
            )
            terminal = "COMPLETED" if context.get("status") == "SUCCESS" else "FAILED"
            return await self._step(
                "observability.finalize", {**context, "terminal_status": terminal}
            )
        finally:
            await self._step("observability.cleanup_workspace", context)


@workflow.defn(name="IssueSolutionLoopWorkflow")
class IssueSolutionLoopWorkflow(ControlledWorkflow):
    """Durable one-issue-at-a-time solution loop; production mode is intentionally absent."""

    @workflow.run
    async def run(self, request: IssueSolutionLoopInput) -> dict[str, Any]:
        if request.execution_mode not in {"plan-only", "source-pr", "preview", "staging"}:
            raise ValueError("unsupported issue-loop execution mode")
        if not 1 <= request.maximum_items <= 100 or not 1 <= request.component_concurrency <= 4:
            raise ValueError("issue-loop bounds are invalid")
        context = await self._step("loop.load_selection", asdict(request))
        raw_items = context.get("items", [])
        if not isinstance(raw_items, list):
            raise TypeError("issue-loop selection must be a list")
        items = raw_items[: request.maximum_items]
        results: list[dict[str, Any]] = []
        for ordinal, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise TypeError("issue-loop item must be an object")
            if not await self._checkpoint(f"ISSUE_{ordinal}_ELIGIBILITY"):
                return {"status": "CANCELLED", "results": results}
            if self._skip_reason:
                skipped = await self._step(
                    "loop.record_skip",
                    {**context, "item": item, "reason": self._skip_reason},
                )
                results.append(skipped)
                self._skip_reason = None
                continue
            leased = await self._step("loop.acquire_component", {**context, "item": item})
            if leased.get("status") == "BLOCKED":
                return {"status": "PAUSED", "blocker": leased.get("blocker"), "results": results}
            try:
                delivery: dict[str, Any] = {}
                for review_attempt in range(1, 4):
                    delivery = await workflow.execute_child_workflow(
                        "IssueDeliveryWorkflow",
                        IssueDeliveryInput(
                            finding_id=str(item["finding_id"]),
                            risk_class=str(item["risk_class"]),
                            dry_run=request.dry_run or request.execution_mode == "plan-only",
                        ),
                        id=f"{workflow.info().workflow_id}-issue-{ordinal}-review-{review_attempt}",
                        task_queue=workflow.info().task_queue,
                        execution_timeout=timedelta(hours=12),
                    )
                    if delivery.get("status") != "REQUEST_CHANGES":
                        break
                if delivery.get("status") in {
                    "BLOCKED",
                    "FAILED",
                    "RATE_LIMITED",
                    "TIMED_OUT",
                    "REQUEST_CHANGES",
                }:
                    blocked = await self._step(
                        "loop.record_blocker",
                        {**leased, "item": item, "delivery": delivery},
                    )
                    return {
                        "status": "PAUSED",
                        "blocker": blocked.get("blocker", delivery.get("status")),
                        "results": results,
                    }
                if request.execution_mode in {"preview", "staging"} and not request.dry_run:
                    change_set_id = str(delivery.get("context", {}).get("change_set_id", ""))
                    if not change_set_id:
                        return {
                            "status": "PAUSED",
                            "blocker": "CHANGE_SET_BINDING_MISSING",
                            "results": results,
                        }
                    reconciled = await workflow.execute_child_workflow(
                        "DeliveryReconciliationWorkflow",
                        DeliveryReconciliationInput(
                            change_set_id=change_set_id,
                            dry_run=False,
                        ),
                        id=f"{workflow.info().workflow_id}-issue-{ordinal}-reconcile",
                        task_queue=workflow.info().task_queue,
                        execution_timeout=timedelta(hours=12),
                    )
                    if reconciled.get("status") not in {"READY", "COMPLETE", "PASS"}:
                        return {
                            "status": "PAUSED",
                            "blocker": reconciled.get("status", "RECONCILIATION_FAILED"),
                            "results": results,
                        }
                    deployment = await workflow.execute_child_workflow(
                        "PreviewDeploymentWorkflow"
                        if request.execution_mode == "preview"
                        else "StagingDeploymentWorkflow",
                        PreviewDeploymentInput(
                            change_set_id=change_set_id,
                            project=request.project,
                            dry_run=False,
                        )
                        if request.execution_mode == "preview"
                        else StagingDeploymentInput(
                            change_set_id=change_set_id,
                            environment="staging",
                            dry_run=False,
                        ),
                        id=f"{workflow.info().workflow_id}-issue-{ordinal}-deploy",
                        task_queue=workflow.info().task_queue,
                        execution_timeout=timedelta(hours=12),
                    )
                    if deployment.get("status") not in {"COMPLETE", "PASS", "VERIFIED"}:
                        return {
                            "status": "PAUSED",
                            "blocker": deployment.get("status", "DEPLOYMENT_FAILED"),
                            "results": results,
                        }
                completed = await self._step(
                    "loop.record_item", {**leased, "item": item, "delivery": delivery}
                )
                results.append(completed)
            finally:
                await self._step("loop.release_component", {**leased, "item": item})
        finalized = await self._step(
            "loop.finalize", {**context, "processed": results, "requested_items": len(items)}
        )
        return {"status": finalized.get("status", "COMPLETE"), "results": results}


@workflow.defn(name="GitHubBacklogImportWorkflow")
class GitHubBacklogImportWorkflow(ControlledWorkflow):
    """Read and persist one paired existing-issue backlog without any GitHub write."""

    @workflow.run
    async def run(self, request: GitHubBacklogImportInput) -> dict[str, Any]:
        if request.project not in {"falcone", "musematic"}:
            raise ApplicationError(
                "existing GitHub backlog supports only Falcone and Musematic",
                type="PolicyViolation",
                non_retryable=True,
            )
        if request.source != "github-open" or request.import_filter not in {
            "bugs",
            "bugs-and-docs",
            "security",
            "enhancements",
            "all",
        }:
            raise ApplicationError(
                "backlog source or filter is not allowlisted",
                type="PolicyViolation",
                non_retryable=True,
            )
        if not 1 <= request.maximum_items <= 500:
            raise ValueError("backlog maximum-items bound is invalid")
        context = asdict(request)
        for step in ("backlog.github_import", "backlog.persist_import", "backlog.plan"):
            if not await self._checkpoint(step.upper().replace(".", "_")):
                return {"status": "CANCELLED", "backlog_campaign_id": request.backlog_campaign_id}
            context = await self._step(step, context, timeout_minutes=30)
            if context.get("status") in {"BLOCKED", "FAILED"}:
                return context
        return context


@workflow.defn(name="ExistingGitHubBacklogLoopWorkflow")
class ExistingGitHubBacklogLoopWorkflow(ControlledWorkflow):
    """Durably control imported issues; fail closed until current reproduction is proven."""

    @workflow.run
    async def run(self, request: ExistingGitHubBacklogLoopInput) -> dict[str, Any]:
        if request.project not in {"falcone", "musematic"}:
            raise ApplicationError(
                "existing backlog loop project is not allowlisted",
                type="PolicyViolation",
                non_retryable=True,
            )
        if request.delivery_mode not in {"source-pr", "full-delivery"}:
            raise ApplicationError(
                "existing backlog delivery mode is invalid",
                type="PolicyViolation",
                non_retryable=True,
            )
        register_child_runs = workflow.patched("existing-backlog-register-child-runs-v1")
        sequential_source_pr = workflow.patched("existing-backlog-sequential-source-pr-v1")
        after_current_controls = workflow.patched("existing-backlog-after-current-controls-v1")
        context = await self._step("backlog.load_execution", asdict(request))
        if context.get("status") == "BLOCKED":
            return context
        raw_items = context.get("items")
        if not isinstance(raw_items, list):
            raise TypeError("existing backlog selection must be a list")
        results: list[dict[str, Any]] = []
        for ordinal, item in enumerate(raw_items, start=1):
            if not isinstance(item, dict):
                raise TypeError("existing backlog issue must be an object")
            continue_to_next_issue = False
            while True:
                if not await self._checkpoint(f"BACKLOG_ISSUE_{ordinal}"):
                    await self._step(
                        "backlog.record_loop_state",
                        {**asdict(request), "loop_status": "CANCELLED"},
                    )
                    return {"status": "CANCELLED", "results": results}
                gate = await self._step(
                    "backlog.revalidate_gate",
                    {**asdict(request), "item": item},
                )
                if gate.get("status") == "READY":
                    break
                blocker = str(gate.get("blocker") or gate.get("status") or "BLOCKED")
                if blocker in {
                    "CURRENT_SOURCE_REVALIDATION_REQUIRED",
                    "REVALIDATED_ISSUE_FINDING_MATERIALIZATION_REQUIRED",
                }:
                    candidate = await self._step(
                        "backlog.materialize_candidate",
                        {**asdict(request), "item": item},
                    )
                    verification_workflow_id = (
                        f"{workflow.info().workflow_id}-issue-{ordinal}-"
                        f"revalidate-{item['issue_number']}"
                    )
                    if register_child_runs:
                        await self._step(
                            "backlog.register_child_run",
                            {
                                **asdict(request),
                                "child_kind": "finding-verification",
                                "child_workflow_id": verification_workflow_id,
                                "finding_id": str(candidate["finding_id"]),
                            },
                        )
                    verification = await workflow.execute_child_workflow(
                        "FindingVerificationWorkflow",
                        FindingVerificationInput(
                            finding_id=str(candidate["finding_id"]),
                            project=request.project,
                            fresh_snapshot=True,
                        ),
                        id=verification_workflow_id,
                        task_queue=workflow.info().task_queue,
                        execution_timeout=timedelta(hours=4),
                    )
                    await self._step(
                        "backlog.record_revalidation",
                        {
                            **asdict(request),
                            "item": item,
                            "verification": verification,
                        },
                    )
                    refreshed = await self._step("backlog.load_execution", asdict(request))
                    candidates = refreshed.get("items")
                    if isinstance(candidates, list):
                        item = next(
                            (
                                value
                                for value in candidates
                                if isinstance(value, dict)
                                and value.get("repository") == item.get("repository")
                                and value.get("issue_number") == item.get("issue_number")
                            ),
                            item,
                        )
                    if verification.get("status") == "CONFIRMED":
                        continue
                    blocker = f"CURRENT_REVALIDATION_{verification.get('status', 'FAILED')}"
                issue_ref = f"{item['repository']}#{item['issue_number']}"
                if (
                    sequential_source_pr
                    and request.continue_after_blocked_issue
                    and not self._requires_operator_pause(blocker)
                ):
                    results.append(
                        {
                            "status": "BLOCKED",
                            "blocker": blocker,
                            "repository": item["repository"],
                            "issue_number": item["issue_number"],
                        }
                    )
                    continue_to_next_issue = True
                    break
                self._phase = f"AWAITING_OPERATOR:{issue_ref}:{blocker}"
                self._skip_reason = None
                self._retry_step = None
                await self._step(
                    "backlog.record_loop_state",
                    {**asdict(request), "loop_status": "PAUSED", "blocker": blocker},
                )
                await workflow.wait_condition(
                    lambda: (
                        self._cancelled
                        or self._skip_reason is not None
                        or self._retry_step is not None
                    )
                )
                if self._cancelled:
                    await self._step(
                        "backlog.record_loop_state",
                        {**asdict(request), "loop_status": "CANCELLED"},
                    )
                    return {"status": "CANCELLED", "results": results}
                if self._skip_reason is not None:
                    skip_value = self._skip_reason
                    reference, separator, reason = skip_value.rpartition("|")
                    if not separator or reference != issue_ref:
                        self._skip_reason = None
                        continue
                    skipped = await self._step(
                        "backlog.record_skip",
                        {
                            **asdict(request),
                            "repository": item["repository"],
                            "issue_number": item["issue_number"],
                            "reason_code": reason,
                        },
                    )
                    results.append(skipped)
                    self._skip_reason = None
                    break
                if self._retry_step != issue_ref:
                    self._retry_step = None
                    continue
                self._retry_step = None
                refreshed = await self._step("backlog.load_execution", asdict(request))
                candidates = refreshed.get("items")
                if isinstance(candidates, list):
                    item = next(
                        (
                            candidate
                            for candidate in candidates
                            if isinstance(candidate, dict)
                            and candidate.get("repository") == item.get("repository")
                            and candidate.get("issue_number") == item.get("issue_number")
                        ),
                        item,
                    )
            if continue_to_next_issue or (results and results[-1].get("status") == "SKIPPED"):
                boundary = await self._after_issue_boundary(
                    request,
                    results,
                    enabled=after_current_controls,
                    has_more=ordinal < len(raw_items),
                )
                if boundary is not None:
                    return boundary
                continue
            delivery: dict[str, Any]
            if sequential_source_pr:
                delivery = {"status": "FAILED", "blocker": "DELIVERY_NOT_STARTED"}
                for attempt in range(1, request.correction_limit + 2):
                    delivery_workflow_id = (
                        f"{workflow.info().workflow_id}-issue-{ordinal}-attempt-{attempt}"
                    )
                    if register_child_runs:
                        await self._step(
                            "backlog.register_child_run",
                            {
                                **asdict(request),
                                "child_kind": "issue-delivery",
                                "child_workflow_id": delivery_workflow_id,
                                "finding_id": str(gate["finding_id"]),
                            },
                        )
                    delivery = await workflow.execute_child_workflow(
                        "IssueDeliveryWorkflow",
                        IssueDeliveryInput(
                            finding_id=str(gate["finding_id"]),
                            risk_class=str(gate["risk_class"]),
                            dry_run=False,
                        ),
                        id=delivery_workflow_id,
                        task_queue=workflow.info().task_queue,
                        execution_timeout=timedelta(hours=12),
                    )
                    results.append({**delivery, "attempt": attempt})
                    if delivery.get("status") != "REQUEST_CHANGES":
                        break
            else:
                delivery_workflow_id = f"{workflow.info().workflow_id}-issue-{ordinal}"
                if register_child_runs:
                    await self._step(
                        "backlog.register_child_run",
                        {
                            **asdict(request),
                            "child_kind": "issue-delivery",
                            "child_workflow_id": delivery_workflow_id,
                            "finding_id": str(gate["finding_id"]),
                        },
                    )
                delivery = await workflow.execute_child_workflow(
                    "IssueDeliveryWorkflow",
                    IssueDeliveryInput(
                        finding_id=str(gate["finding_id"]),
                        risk_class=str(gate["risk_class"]),
                        dry_run=False,
                    ),
                    id=delivery_workflow_id,
                    task_queue=workflow.info().task_queue,
                    execution_timeout=timedelta(hours=12),
                )
                results.append(delivery)
            if delivery.get("status") in {"BLOCKED", "FAILED", "RATE_LIMITED", "TIMED_OUT"}:
                blocker = str(delivery.get("blocker") or delivery.get("status"))
                if (
                    sequential_source_pr
                    and request.continue_after_blocked_issue
                    and not self._requires_operator_pause(blocker)
                ):
                    boundary = await self._after_issue_boundary(
                        request,
                        results,
                        enabled=after_current_controls,
                        has_more=ordinal < len(raw_items),
                    )
                    if boundary is not None:
                        return boundary
                    continue
                await self._step(
                    "backlog.record_loop_state",
                    {**asdict(request), "loop_status": "PAUSED", "blocker": blocker},
                )
                return {"status": "PAUSED", "results": results}
            if sequential_source_pr and delivery.get("status") == "REQUEST_CHANGES":
                await self._step(
                    "backlog.record_loop_state",
                    {
                        **asdict(request),
                        "loop_status": "PAUSED",
                        "blocker": "CORRECTION_LIMIT_EXHAUSTED",
                    },
                )
                return {"status": "PAUSED", "results": results}
            if request.delivery_mode == "source-pr":
                if sequential_source_pr:
                    boundary = await self._after_issue_boundary(
                        request,
                        results,
                        enabled=after_current_controls,
                        has_more=ordinal < len(raw_items),
                    )
                    if boundary is not None:
                        return boundary
                    continue
                return {"status": "AWAITING_STAGING_DELIVERY", "results": results}
            return {
                "status": "BLOCKED_BY_DELIVERY",
                "blocker": "FULL_DELIVERY_REQUIRES_PROJECT_BUILD_AND_STAGING_GATES",
                "results": results,
            }
        await self._step(
            "backlog.record_loop_state", {**asdict(request), "loop_status": "COMPLETE"}
        )
        return {"status": "COMPLETE", "results": results}

    async def _after_issue_boundary(
        self,
        request: ExistingGitHubBacklogLoopInput,
        results: list[dict[str, Any]],
        *,
        enabled: bool,
        has_more: bool,
    ) -> dict[str, Any] | None:
        """Apply campaign controls only between issues, never during provider work."""
        if not enabled or not has_more:
            return None
        if self._stop_after_current:
            self._stop_after_current = False
            self._phase = "STOPPED_AFTER_CURRENT"
            await self._step(
                "backlog.record_loop_state",
                {
                    **asdict(request),
                    "loop_status": "STOPPED",
                    "control_state": "STOPPED_AFTER_CURRENT",
                },
            )
            return {"status": "STOPPED", "results": results}
        if not self._pause_after_current:
            return None
        self._pause_after_current = False
        self._paused = True
        self._phase = "PAUSED_AFTER_CURRENT"
        await self._step(
            "backlog.record_loop_state",
            {
                **asdict(request),
                "loop_status": "PAUSED",
                "control_state": "PAUSED_AFTER_CURRENT",
            },
        )
        await workflow.wait_condition(lambda: not self._paused or self._cancelled)
        if not self._cancelled:
            return None
        await self._step(
            "backlog.record_loop_state",
            {**asdict(request), "loop_status": "CANCELLED"},
        )
        return {"status": "CANCELLED", "results": results}

    @staticmethod
    def _requires_operator_pause(blocker: str) -> bool:
        normalized = blocker.upper()
        return any(
            marker in normalized
            for marker in (
                "R3",
                "MIGRATION",
                "SECRET",
                "CREDENTIAL",
                "CROSS_TENANT",
                "CROSS-TENANT",
                "PRODUCTION",
                "EXTERNAL_DRIFT",
                "REVISION_DRIFT",
                "APPROVAL",
            )
        )


@workflow.defn(name="EnhancementDiscoveryWorkflow")
class EnhancementDiscoveryWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: EnhancementDiscoveryInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        for step in ("enhancement.load_evidence", "enhancement.prepare_discovery"):
            if not await self._checkpoint(step.upper().replace(".", "_")):
                return {"status": "CANCELLED", "context": context}
            context = await self._step(step, context)
            if context.get("status") in {"BLOCKED", "FAILED"}:
                return {"status": context["status"], "context": context}
        if request.dry_run:
            return {"status": "DRY_RUN", "context": context}
        for step in (
            "enhancement.discover",
            "enhancement.validate_evidence",
            "enhancement.dedupe",
            "enhancement.github_dedupe",
            "enhancement.product_review",
            "enhancement.persist",
        ):
            if not await self._checkpoint(step.upper().replace(".", "_")):
                return {"status": "CANCELLED", "context": context}
            if step in {"enhancement.discover", "enhancement.product_review"}:
                context = await self._agent_step(step, context, timeout_minutes=120)
            else:
                context = await self._step(step, context)
            if (
                step in {"enhancement.dedupe", "enhancement.github_dedupe"}
                and context.get("status") == "DUPLICATE"
            ):
                return {"status": "DUPLICATE", "context": context}
            if context.get("status") in {"BLOCKED", "FAILED", "RATE_LIMITED"}:
                return {"status": context["status"], "context": context}
        return {"status": "CANDIDATE_ENHANCEMENT", "context": context}


@workflow.defn(name="EnhancementDeliveryWorkflow")
class EnhancementDeliveryWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: EnhancementDeliveryInput) -> dict[str, Any]:
        context = await self._step("enhancement.load_approved", asdict(request))
        if context.get("status") != "APPROVED":
            return {"status": "BLOCKED", "blocker": "ENHANCEMENT_APPROVAL_REQUIRED"}
        context = await self._step("enhancement.create_openspec", context)
        if not request.publish:
            context = await self._step("enhancement.publication_dry_run", context)
            return {"status": "DRY_RUN", "context": context}
        context = await self._github_write_step(
            "enhancement.publish", {**context, "dry_run": False}
        )
        if context.get("status") != "PUBLISHED":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        context = await self._step("enhancement.persist_publication", context)
        return {"status": context.get("status", "PUBLISHED"), "context": context}


@workflow.defn(name="EnhancementSolutionLoopWorkflow")
class EnhancementSolutionLoopWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: EnhancementSolutionLoopInput) -> dict[str, Any]:
        approved = await self._step("enhancement.load_approved", asdict(request))
        if approved.get("status") != "APPROVED":
            return {"status": "BLOCKED", "blocker": "ENHANCEMENT_APPROVAL_REQUIRED"}
        prepared = await self._step("enhancement.prepare_solution", approved)
        if request.dry_run or request.execution_mode == "plan-only":
            return {"status": "DRY_RUN", "context": prepared}
        result = await workflow.execute_child_workflow(
            "IssueSolutionLoopWorkflow",
            IssueSolutionLoopInput(
                project=str(prepared["project"]),
                campaign_id=str(prepared["campaign_id"]),
                maximum_risk=request.risk,
                maximum_items=1,
                execution_mode=request.execution_mode,
                component_concurrency=1,
                stop_policy="pause-on-blocker",
                dry_run=False,
            ),
            id=f"{workflow.info().workflow_id}-solution",
            task_queue=workflow.info().task_queue,
            execution_timeout=timedelta(days=7),
        )
        if not isinstance(result, dict):
            raise RuntimeError("enhancement solution child returned an invalid result")
        return result


@workflow.defn(name="LLMWikiStagingBootstrapWorkflow")
class LLMWikiStagingBootstrapWorkflow(ControlledWorkflow):
    """Plan and, only after an exact binding, reconcile first-stage LLMWiki staging."""

    @workflow.run
    async def run(self, request: LLMWikiStagingBootstrapInput) -> dict[str, Any]:
        if request.project != "llmwiki" or request.environment != "staging":
            raise ApplicationError(
                "LLMWiki bootstrap rejects non-staging targets",
                type="PolicyViolation",
                non_retryable=True,
            )
        plan = await self._step("llmwiki.bootstrap.plan", asdict(request))
        if not request.execute:
            return plan
        if plan.get("blockers"):
            return {**plan, "status": "BLOCKED"}
        if plan.get("approval_binding") != request.approval_binding:
            raise ApplicationError(
                "LLMWiki bootstrap binding changed",
                type="ApprovalMismatch",
                non_retryable=True,
            )
        return await self._step(
            "llmwiki.bootstrap.execute",
            {**plan, "dry_run": False, "approval_binding_valid": True},
            timeout_minutes=120,
        )


ALL_WORKFLOWS = [
    AuditCampaignWorkflow,
    MultiProjectAuditWorkflow,
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
    ObservabilityAcceptanceWorkflow,
    IssueSolutionLoopWorkflow,
    GitHubBacklogImportWorkflow,
    ExistingGitHubBacklogLoopWorkflow,
    EnhancementDiscoveryWorkflow,
    EnhancementDeliveryWorkflow,
    EnhancementSolutionLoopWorkflow,
    LLMWikiStagingBootstrapWorkflow,
]
