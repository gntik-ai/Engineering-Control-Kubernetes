from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError

from engineering_control.activities.retention import (
    FileCleanupCandidate,
    PreviewNamespaceCleanupCandidate,
    cleanup_authorization_sha256,
)
from engineering_control.config import Settings
from engineering_control.db.models import (
    ActionCandidate,
    AgentExecution,
    AgentInvocation,
    AgentLogChunk,
    Approval,
    ApprovalCandidate,
    Artifact,
    BacklogIssueRecord,
    Campaign,
    ChangeSet,
    ComponentLease,
    CoverageCell,
    DashboardEvent,
    Deployment,
    Finding,
    FindingVerification,
    GitHubBacklogCampaign,
    Incident,
    Issue,
    OperationExport,
    ProjectSnapshot,
    ProviderUsage,
    QuotaSnapshot,
    Run,
    VerificationResult,
    WorktreeLease,
)
from engineering_control.engineering.backlog import (
    BACKLOG_APPROVAL_PHRASE,
    BacklogApprovalBinding,
    BacklogFinalClosureEvidence,
    DocumentationImpact,
    evaluate_final_closure,
)
from engineering_control.engineering.github import FindingPublication
from engineering_control.engineering.models import ProjectManifest
from engineering_control.leases.component import ComponentLeaseConflict, ComponentLeaseManager
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.core import ChangeSet as ChangeSetModel
from engineering_control.models.enums import (
    AuditLens,
    ChangeSetState,
    ModelClass,
    Project,
    Provider,
    RiskClass,
    RootBlockerClassification,
)
from engineering_control.models.final_closure import (
    FinalArtifactPlan,
    FinalClosurePlan,
    FinalDeploymentMergePlan,
    FinalIssueMutationPlan,
    ProtectedMergeResult,
    ProtectedPullRequestBinding,
)
from engineering_control.models.reconciliation import (
    CandidateArtifactManifest,
    CIReconciliationPlan,
    DeliveryControlPolicy,
    PromotionPlan,
    PromotionPublication,
)
from engineering_control.observability.agent import normalize_agent_role
from engineering_control.policy.approvals import ApprovalService
from engineering_control.quota.broker import QuotaBroker
from engineering_control.quota.broker import QuotaSnapshot as BrokerQuotaSnapshot
from engineering_control.security.redaction import SecretRedactor


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class QuotaSnapshotRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Provider
    captured_at: datetime
    remaining_capacity: float | None = Field(default=None, ge=0)
    total_capacity: float | None = Field(default=None, ge=0)
    reset_at: datetime | None = None
    running_jobs: int = Field(default=0, ge=0)
    rate_limited: bool = False
    authenticated: bool | None = None
    cli_present: bool | None = None
    status: str | None = Field(default=None, min_length=1, max_length=80)
    source: str = Field(min_length=1, max_length=80)


class InstructionBundleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    sources: list[str] = Field(min_length=1, max_length=20)
    allowed_tools: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def safe_sources_and_tools(self) -> InstructionBundleRecord:
        for value in self.sources:
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or not value or len(value) > 240:
                raise ValueError("instruction source path is unsafe")
        allowed = {"Read", "Glob", "Grep", "Bash", "Edit", "Write"}
        if not set(self.allowed_tools).issubset(allowed):
            raise ValueError("instruction bundle contains a non-allowlisted tool")
        return self


class CampaignCellRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_surface: Literal["source", "deployment", "pair", "runtime"] = "source"
    domain: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    persona: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    flow: str = Field(default="repository-static", pattern=r"^[a-z][a-z0-9-]{0,119}$")
    lens: AuditLens
    environment: str = Field(pattern=r"^[a-z][a-z0-9-]{0,79}$")


class RootBlockerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root_blocker_id: str = Field(pattern=r"^rb-[a-f0-9]{32}$")
    classification: RootBlockerClassification
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,119}$")
    dependent_cell_count: int = Field(default=0, ge=0)
    root_finding_id: UUID | None = None


class FindingCandidateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: str = Field(pattern=r"^(low|medium|high|critical)$")
    confidence: float = Field(ge=0, le=1)
    expected_behavior: str = Field(min_length=1, max_length=20_000)
    actual_behavior: str = Field(min_length=1, max_length=20_000)
    minimal_reproduction: list[str] = Field(min_length=1, max_length=50)
    affected_component: str = Field(min_length=1, max_length=200)
    security_impact: str | None = Field(default=None, max_length=20_000)
    data_impact: str | None = Field(default=None, max_length=20_000)
    recommended_action: str = Field(min_length=1, max_length=20_000)


class DeploymentTargetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    enabled: bool
    project: Project
    environment: Literal["preview", "staging"]
    strategy: Literal["helm", "argocd-gitops", "repo-local-manifest"]
    target_context: str = Field(min_length=1, max_length=160)
    target_namespace: str = Field(min_length=1, max_length=160)
    blocker: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,119}$")
    configuration: dict[str, Any]

    @model_validator(mode="after")
    def strict_project_strategy_configuration(self) -> DeploymentTargetRecord:
        expected_strategy = {
            Project.FALCONE: "helm",
            Project.MUSEMATIC: "argocd-gitops",
            Project.LLMWIKI: "repo-local-manifest",
        }[self.project]
        if self.strategy != expected_strategy:
            raise ValueError("deployment target strategy does not match project policy")
        if not self.enabled:
            if self.blocker is None:
                raise ValueError("disabled deployment target requires an explicit blocker")
            if self.configuration:
                raise ValueError("disabled deployment target must not contain active configuration")
            return self
        if self.target_context == "UNCONFIGURED" or self.target_namespace == "UNCONFIGURED":
            raise ValueError("enabled deployment target cannot use placeholder identity")
        allowed = (
            {
                "chart_subdirectory",
                "values_files",
                "chart_version",
                "release_name",
                "kube_context",
                "kubeconfig_path",
                "timeout_minutes",
                "cleanup_enabled",
                "cleanup_namespace_prefix",
                "cleanup_kube_context",
                "cleanup_kubeconfig_path",
            }
            if self.strategy == "helm"
            else {
                "argo_application",
                "manifest_paths",
                "cleanup_enabled",
                "cleanup_namespace_prefix",
                "cleanup_kube_context",
                "cleanup_kubeconfig_path",
            }
        )
        unknown = set(self.configuration) - allowed
        if unknown:
            raise ValueError("deployment target contains non-allowlisted configuration fields")
        required = (
            {"values_files", "release_name", "kube_context", "kubeconfig_path"}
            if self.strategy == "helm"
            else {"argo_application"}
            if self.strategy == "argocd-gitops"
            else {"manifest_paths"}
        )
        if required - set(self.configuration):
            raise ValueError("enabled deployment target configuration is incomplete")
        cleanup_fields = {
            "cleanup_namespace_prefix",
            "cleanup_kube_context",
            "cleanup_kubeconfig_path",
        }
        cleanup_enabled = self.configuration.get("cleanup_enabled", False)
        if not isinstance(cleanup_enabled, bool):
            raise ValueError("preview cleanup enablement must be boolean")
        if cleanup_enabled and self.environment != "preview":
            raise ValueError("namespace cleanup is restricted to preview targets")
        configured_cleanup = cleanup_fields.intersection(self.configuration)
        if cleanup_enabled and configured_cleanup != cleanup_fields:
            raise ValueError("enabled preview cleanup configuration is incomplete")
        if not cleanup_enabled and configured_cleanup:
            raise ValueError("disabled preview cleanup cannot retain active configuration")
        return self


class RuntimeTargetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    enabled: bool
    project: Project
    environment: Literal["staging"]
    target_context: str = Field(min_length=1, max_length=160)
    target_namespace: str = Field(min_length=1, max_length=160)
    blocker: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,119}$")
    configuration: dict[str, Any]

    @model_validator(mode="after")
    def strict_runtime_configuration(self) -> RuntimeTargetRecord:
        allowed = {
            "observer_kubeconfig_path",
            "observed_resources",
            "revision_annotation",
            "argo_application",
            "journey_plan_path",
        }
        if set(self.configuration) - allowed:
            raise ValueError("runtime target contains non-allowlisted configuration fields")
        if not self.enabled:
            if self.blocker is None or self.configuration:
                raise ValueError("disabled runtime target requires only an explicit blocker")
            return self
        if self.target_context == "UNCONFIGURED" or self.target_namespace == "UNCONFIGURED":
            raise ValueError("enabled runtime target cannot use placeholder identity")
        if len(self.target_namespace) > 63 or not re.fullmatch(
            r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", self.target_namespace
        ):
            raise ValueError("enabled runtime target requires a DNS-label namespace")
        required = {
            "observer_kubeconfig_path",
            "observed_resources",
            "journey_plan_path",
            "argo_application" if self.project is Project.MUSEMATIC else "revision_annotation",
        }
        if required - set(self.configuration):
            raise ValueError("enabled runtime target configuration is incomplete")
        resources = self.configuration.get("observed_resources")
        if not isinstance(resources, list) or not resources:
            raise ValueError("runtime target requires observed resources")
        return self


class MetadataActivityHandler:
    """Controller-side authoritative metadata writes for idempotent activity results."""

    _provider_request_keys = {
        "audit.agent_cell": "agent_request",
        "finding.reproduce": "verifier_request",
        "delivery.invoke_spec": "spec_request",
        "delivery.invoke_maker": "maker_request",
        "delivery.invoke_deployment_maker": "deployment_maker_request",
        "delivery.invoke_checker": "checker_request",
        "reconcile.invoke_release_reviewer": "release_review_request",
        "runtime.independent_review": "runtime_review_request",
        "enhancement.discover": "discovery_request",
        "enhancement.product_review": "product_review_request",
        "observability.invoke_codex": "maker_request",
        "observability.invoke_claude": "checker_request",
        "observability.invoke_claude_maker": "maker_request",
        "observability.invoke_codex_checker": "checker_request",
    }

    supported_steps = frozenset(
        {
            "audit.prepare_agent_cell",
            "campaign.persist_coverage",
            "campaign.persist_snapshot_blockers",
            "campaign.persist_cell_blocker",
            "campaign.persist_cell_result",
            "campaign.finalize",
            "campaign.group_status",
            "finding.load_candidate",
            "finding.prepare_verifier",
            "finding.controls",
            "finding.finalize",
            "finding.prepare_publication",
            "finding.persist_publication",
            "finding.record_terminal",
            "agent.record_start",
            "agent.persist_result",
            "delivery.load_confirmed_finding",
            "delivery.ensure_issue",
            "delivery.acquire_leases",
            "delivery.route_providers",
            "delivery.persist_worktree_leases",
            "delivery.prepare_spec",
            "delivery.create_openspec",
            "delivery.prepare_maker",
            "delivery.prepare_deployment_maker",
            "delivery.prepare_checker",
            "delivery.persist_handoff",
            "delivery.prepare_publication",
            "delivery.persist_publication",
            "reconcile.prepare",
            "reconcile.persist_ci",
            "reconcile.prepare_promotion",
            "reconcile.persist_promotion",
            "reconcile.persist_promotion_publication",
            "reconcile.prepare_release_review",
            "reconcile.persist_release_review",
            "final.prepare",
            "final.persist_source_merge",
            "final.persist_artifact",
            "final.persist_deployment_merge",
            "final.prepare_issue_close",
            "final.persist_comment",
            "final.persist_issue_close",
            "preview.prepare",
            "preview.record_candidate",
            "preview.validate_approval",
            "preview.finalize",
            "staging.prepare",
            "staging.record_candidate",
            "staging.validate_approval",
            "staging.finalize",
            "deployment.record_terminal",
            "runtime.snapshot",
            "runtime.prepare_review",
            "runtime.finalize",
            "runtime.record_failure",
            "recovery.inspect",
            "recovery.validate_binding",
            "recovery.prepare_candidate",
            "recovery.validate_approval",
            "recovery.verify",
            "recovery.record_failure",
            "cleanup.discover",
            "cleanup.authorize",
            "cleanup.execute_metadata",
            "cleanup.finalize",
            "quota.persist",
            "observability.register",
            "observability.prepare_codex",
            "observability.prepare_claude",
            "observability.prepare_claude_maker",
            "observability.prepare_codex_checker",
            "observability.finalize",
        }
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redactor = SecretRedactor()
        database_url = settings.database_url.get_secret_value()
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=2,
            connect_args={"connect_timeout": 5} if database_url.startswith("postgresql") else {},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if step == "audit.prepare_agent_cell":
                return await asyncio.to_thread(self._prepare_agent_cell, payload)
            if step == "campaign.persist_coverage":
                return await asyncio.to_thread(self._persist_coverage, payload)
            if step == "campaign.persist_snapshot_blockers":
                return await asyncio.to_thread(self._persist_snapshot_blockers, payload)
            if step == "campaign.persist_cell_blocker":
                return await asyncio.to_thread(self._persist_cell_blocker, payload)
            if step == "campaign.persist_cell_result":
                return await asyncio.to_thread(self._persist_cell_result, payload)
            if step == "campaign.finalize":
                return await asyncio.to_thread(self._finalize_campaign, payload)
            if step == "campaign.group_status":
                return await asyncio.to_thread(self._record_campaign_group_status, payload)
            if step == "finding.load_candidate":
                return await asyncio.to_thread(self._load_candidate, payload)
            if step == "finding.prepare_verifier":
                return await asyncio.to_thread(self._prepare_verifier, payload)
            if step == "finding.controls":
                return await asyncio.to_thread(self._evaluate_controls, payload)
            if step == "finding.finalize":
                return await asyncio.to_thread(self._finalize_finding, payload)
            if step == "finding.prepare_publication":
                return await asyncio.to_thread(self._prepare_finding_publication, payload)
            if step == "finding.persist_publication":
                return await asyncio.to_thread(self._persist_finding_publication, payload)
            if step == "finding.record_terminal":
                return await asyncio.to_thread(self._record_finding_terminal, payload)
            if step == "agent.record_start":
                return await asyncio.to_thread(self._record_agent_start, payload)
            if step == "agent.persist_result":
                return await asyncio.to_thread(self._persist_agent_result, payload)
            if step == "delivery.load_confirmed_finding":
                return await asyncio.to_thread(self._load_confirmed_finding, payload)
            if step == "delivery.ensure_issue":
                return await asyncio.to_thread(self._ensure_issue, payload)
            if step == "delivery.acquire_leases":
                try:
                    return await asyncio.to_thread(self._acquire_delivery_leases, payload)
                except ComponentLeaseConflict:
                    return {
                        **payload,
                        "status": "BLOCKED",
                        "blocker": "COMPONENT_LEASE_CONFLICT",
                    }
            if step == "delivery.route_providers":
                return await asyncio.to_thread(self._route_delivery, payload)
            if step == "delivery.persist_worktree_leases":
                return await asyncio.to_thread(self._persist_worktree_leases, payload)
            if step == "delivery.prepare_spec":
                return self._prepare_spec(payload)
            if step == "delivery.create_openspec":
                return self._create_openspec(payload)
            if step == "delivery.prepare_maker":
                return self._prepare_maker(payload, deployment=False)
            if step == "delivery.prepare_deployment_maker":
                return self._prepare_maker(payload, deployment=True)
            if step == "delivery.prepare_checker":
                return self._prepare_checker(payload)
            if step == "delivery.persist_handoff":
                return await asyncio.to_thread(self._persist_delivery_handoff, payload)
            if step == "delivery.prepare_publication":
                return self._prepare_publication(payload)
            if step == "delivery.persist_publication":
                return await asyncio.to_thread(self._persist_publication, payload)
            if step == "reconcile.prepare":
                return await asyncio.to_thread(self._prepare_reconciliation, payload)
            if step == "reconcile.persist_ci":
                return await asyncio.to_thread(self._persist_reconciled_ci, payload)
            if step == "reconcile.prepare_promotion":
                try:
                    return await asyncio.to_thread(self._prepare_promotion, payload)
                except ComponentLeaseConflict:
                    return await asyncio.to_thread(
                        self._block_reconciliation,
                        payload,
                        "PROMOTION_COMPONENT_LEASE_CONFLICT",
                    )
            if step == "reconcile.persist_promotion":
                return await asyncio.to_thread(self._persist_promotion, payload)
            if step == "reconcile.persist_promotion_publication":
                return await asyncio.to_thread(self._persist_promotion_publication, payload)
            if step == "reconcile.prepare_release_review":
                return await asyncio.to_thread(self._prepare_release_review, payload)
            if step == "reconcile.persist_release_review":
                return await asyncio.to_thread(self._persist_release_review, payload)
            if step == "final.prepare":
                return await asyncio.to_thread(self._prepare_final_closure, payload)
            if step == "final.persist_source_merge":
                return await asyncio.to_thread(self._persist_final_source_merge, payload)
            if step == "final.persist_artifact":
                return await asyncio.to_thread(self._persist_final_artifact, payload)
            if step == "final.persist_deployment_merge":
                return await asyncio.to_thread(self._persist_final_deployment_merge, payload)
            if step == "final.prepare_issue_close":
                return await asyncio.to_thread(self._prepare_final_issue_close, payload)
            if step == "final.persist_comment":
                return await asyncio.to_thread(self._persist_final_comment, payload)
            if step == "final.persist_issue_close":
                return await asyncio.to_thread(self._persist_final_issue_close, payload)
            if step == "preview.prepare":
                return await asyncio.to_thread(self._prepare_deployment, payload, "preview")
            if step == "preview.record_candidate":
                return await asyncio.to_thread(self._record_preview_candidate, payload)
            if step == "preview.validate_approval":
                return await asyncio.to_thread(self._validate_preview_approval, payload)
            if step == "preview.finalize":
                return await asyncio.to_thread(self._finalize_preview, payload)
            if step == "staging.prepare":
                return await asyncio.to_thread(self._prepare_deployment, payload, "staging")
            if step == "staging.record_candidate":
                try:
                    return await asyncio.to_thread(self._record_staging_candidate, payload)
                except ComponentLeaseConflict:
                    return {
                        **payload,
                        "status": "BLOCKED",
                        "blocker": "STAGING_DEPLOYMENT_LEASE_CONFLICT",
                    }
            if step == "staging.validate_approval":
                return await asyncio.to_thread(self._validate_staging_approval, payload)
            if step == "staging.finalize":
                return await asyncio.to_thread(self._finalize_staging, payload)
            if step == "deployment.record_terminal":
                return await asyncio.to_thread(self._record_deployment_terminal, payload)
            if step == "runtime.snapshot":
                return await asyncio.to_thread(self._runtime_snapshot, payload)
            if step == "runtime.prepare_review":
                return self._prepare_runtime_review(payload)
            if step == "runtime.finalize":
                return await asyncio.to_thread(self._finalize_runtime, payload)
            if step == "runtime.record_failure":
                return await asyncio.to_thread(self._record_runtime_failure, payload)
            if step == "recovery.inspect":
                return await asyncio.to_thread(self._inspect_recovery, payload)
            if step == "recovery.validate_binding":
                return await asyncio.to_thread(self._validate_recovery_binding, payload)
            if step == "recovery.prepare_candidate":
                try:
                    return await asyncio.to_thread(self._prepare_recovery_candidate, payload)
                except ComponentLeaseConflict:
                    return await asyncio.to_thread(
                        self._record_recovery_failure,
                        {**payload, "blocker": "RECOVERY_DEPLOYMENT_LEASE_CONFLICT"},
                    )
            if step == "recovery.validate_approval":
                return await asyncio.to_thread(self._validate_recovery_approval, payload)
            if step == "recovery.verify":
                return await asyncio.to_thread(self._verify_recovery, payload)
            if step == "recovery.record_failure":
                return await asyncio.to_thread(self._record_recovery_failure, payload)
            if step == "cleanup.discover":
                return await asyncio.to_thread(self._discover_cleanup, payload)
            if step == "cleanup.authorize":
                return await asyncio.to_thread(self._authorize_cleanup, payload)
            if step == "cleanup.execute_metadata":
                return await asyncio.to_thread(self._execute_cleanup_metadata, payload)
            if step == "cleanup.finalize":
                return await asyncio.to_thread(self._finalize_cleanup, payload)
            if step == "quota.persist":
                return await self._quota(payload)
            if step == "observability.register":
                return await asyncio.to_thread(self._register_observability, payload)
            if step == "observability.prepare_codex":
                return self._prepare_observability_codex(payload)
            if step == "observability.prepare_claude":
                return self._prepare_observability_claude(payload)
            if step == "observability.prepare_claude_maker":
                return self._prepare_observability_claude_maker(payload)
            if step == "observability.prepare_codex_checker":
                return self._prepare_observability_codex_checker(payload)
            if step == "observability.finalize":
                return await asyncio.to_thread(self._finalize_observability, payload)
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise ApplicationError(
                f"invalid metadata activity input for {step}",
                type="PolicyViolation",
                non_retryable=True,
            ) from error
        raise ApplicationError("unsupported metadata activity", non_retryable=True)

    def _record_agent_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self._agent_request(payload)
        workflow_id = self._workflow_id(payload)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            run = self._provider_control_run(session, workflow_id, request, payload, now)
            invocation = session.get(AgentInvocation, request.run_id)
            if invocation is None:
                invocation = AgentInvocation(
                    id=request.run_id,
                    run_id=run.id,
                    provider=request.provider.value,
                    role=request.role,
                    model_class=request.model_class.value,
                    agent_pack_revision=request.agent_pack_revision,
                    instruction_bundle_sha256=request.instruction_bundle_sha256,
                    instruction_sources_json=list(request.instruction_bundle_sources),
                    allowed_tools_json=list(request.allowed_tools),
                    provider_session_id=None,
                    status="RUNNING",
                    summary=None,
                    structured_result={},
                    error_code=None,
                    started_at=now,
                    finished_at=None,
                )
                session.add(invocation)
            else:
                self._validate_invocation_binding(invocation, run, request)
                if invocation.finished_at is None:
                    invocation.status = "RUNNING"
            if run.started_at is None:
                run.started_at = now
            if run.status == "QUEUED":
                run.status = "RUNNING"
            execution = session.get(AgentExecution, request.run_id)
            if execution is None:
                orchestration = payload.get("orchestration")
                context = orchestration if isinstance(orchestration, dict) else {}
                repository_full_name: str | None = None
                issue_number: int | None = None
                if run.change_set_id is not None:
                    change_set = session.get(ChangeSet, run.change_set_id)
                    if change_set is not None:
                        repository_full_name = change_set.source_repository
                        if change_set.issue_id is not None:
                            issue = session.get(Issue, change_set.issue_id)
                            if issue is not None:
                                issue_number = issue.number
                                repository_full_name = issue.repository or repository_full_name
                raw_issue = payload.get("issue_number")
                if issue_number is None and isinstance(raw_issue, int) and raw_issue > 0:
                    issue_number = raw_issue
                attempt = int(context.get("attempt", 1))
                execution = AgentExecution(
                    id=request.run_id,
                    workflow_id=workflow_id,
                    temporal_run_id=str(context.get("workflow_run_id") or "") or None,
                    activity_id=str(context.get("activity_id") or "") or None,
                    project=request.project.value,
                    repository_full_name=repository_full_name,
                    issue_number=issue_number,
                    campaign_id=request.campaign_id or run.campaign_id,
                    change_set_id=request.change_set_id or run.change_set_id,
                    provider=request.provider.value,
                    role=normalize_agent_role(request.role),
                    host=None,
                    worker_kind=request.provider.value,
                    task_queue=None,
                    status="QUEUED",
                    phase=str(payload["provider_step"])[:120],
                    selection_reason="workflow routing decision",
                    cli_path_identifier=request.provider.value,
                    cli_version=None,
                    attempt=max(1, attempt),
                    max_attempts=max(1, attempt),
                    started_at=None,
                    last_heartbeat_at=None,
                    ended_at=None,
                    exit_code=None,
                    retryable=None,
                    blocker_code=None,
                    error_summary=None,
                    output_truncated=False,
                    transcript_incomplete=False,
                    total_log_bytes=0,
                    redaction_count=0,
                )
                session.add(execution)
            elif (
                execution.workflow_id != workflow_id
                or execution.provider != request.provider.value
                or execution.role != normalize_agent_role(request.role)
            ):
                raise ValueError("agent execution identity conflicts with controller registration")
        return {
            **payload,
            "agent_invocation_id": str(request.run_id),
            "agent_invocation_status": "RUNNING",
        }

    def _observability_workspace(self, payload: dict[str, Any]) -> Path:
        workspace_id = UUID(str(payload["workspace_id"]))
        expected = self.settings.worktree_root / "observability-canary" / str(workspace_id)
        workspace = Path(str(payload["workspace"]))
        if not workspace.is_absolute() or workspace != expected or ".." in workspace.parts:
            raise ValueError("observability canary workspace identity is invalid")
        return workspace

    def _register_observability(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = UUID(str(payload["workspace_id"]))
        workflow_id = self._workflow_id(payload)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            run = session.scalar(
                select(Run)
                .where(Run.workflow_id == workflow_id)
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            if run is None:
                run = Run(
                    id=uuid5(NAMESPACE_URL, f"control-run:{workflow_id}"),
                    workflow_id=workflow_id,
                    run_type="synthetic-observability",
                    status="QUEUED",
                    project=Project.FALCONE.value,
                    worker="controller",
                )
                session.add(run)
            elif run.run_type != "synthetic-observability" or run.project != "falcone":
                raise ValueError("observability workflow identity conflicts with a control run")
            queued = session.scalar(
                select(DashboardEvent.id)
                .where(
                    DashboardEvent.workflow_id == workflow_id,
                    DashboardEvent.event_type == "workflow.queued",
                )
                .limit(1)
            )
            if queued is None:
                session.add(
                    DashboardEvent(
                        occurred_at=now,
                        level="INFO",
                        event_type="workflow.queued",
                        source="controller",
                        worker_kind="controller",
                        project="falcone",
                        workflow_id=workflow_id,
                        entity_type="synthetic_observability_acceptance",
                        entity_id=str(workspace_id),
                        current_phase="queued",
                        message="Native provider observability canary queued",
                        metadata_json={"status": "QUEUED"},
                    )
                )
        return {**payload, "status": "QUEUED"}

    @staticmethod
    def _observability_schema(provider: str) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "provider", "summary"],
            "properties": {
                "decision": {"type": "string", "const": "PASS"},
                "provider": {"type": "string", "const": provider},
                "summary": {"type": "string", "minLength": 1, "maxLength": 240},
            },
        }

    def _prepare_observability_codex(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace = self._observability_workspace(payload)
        workflow_id = self._workflow_id(payload)
        request = AgentRequest(
            run_id=uuid5(NAMESPACE_URL, f"observability-codex:{workflow_id}"),
            project=Project.FALCONE,
            role="implementer",
            provider=Provider.CODEX,
            model_class=ModelClass.WORKER,
            prompt=(
                "This is a read-only Engineering Control observability canary. Do not call tools, "
                "read files, modify files, access credentials, or make external requests. Return "
                "only the required JSON with decision PASS, provider codex, and a short safe "
                "summary confirming visible-output streaming."
            ),
            working_directory=str(workspace),
            output_schema=self._observability_schema("codex"),
            timeout_seconds=300,
            effort="low",
            writable=False,
            skip_git_repository_check=True,
            allowed_tools=("Read", "Glob", "Grep"),
        )
        return {
            **payload,
            "status": "PREPARING",
            "maker_provider": "codex",
            "provider_selection_reason": "read-only canary routes maker to native Codex CLI",
            "maker_request": request.model_dump(mode="json"),
        }

    def _prepare_observability_claude(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace = self._observability_workspace(payload)
        raw_codex = payload.get("agent_result")
        codex_result = AgentResult.model_validate(raw_codex)
        if (
            codex_result.status.value != "SUCCESS"
            or codex_result.structured_result.get("decision") != "PASS"
            or codex_result.structured_result.get("provider") != "codex"
        ):
            raise ValueError("Codex canary result did not satisfy its immutable contract")
        workflow_id = self._workflow_id(payload)
        request = AgentRequest(
            run_id=uuid5(NAMESPACE_URL, f"observability-claude:{workflow_id}"),
            project=Project.FALCONE,
            role="code-reviewer",
            provider=Provider.CLAUDE,
            model_class=ModelClass.JUDGE,
            prompt=(
                "This is an independent read-only Engineering Control observability canary. Do "
                "not call tools, read files, modify files, access credentials, or make external "
                "requests. Return only the required JSON with decision PASS, provider claude, "
                "and a short safe summary confirming checker visible-output streaming."
            ),
            working_directory=str(workspace),
            output_schema=self._observability_schema("claude"),
            timeout_seconds=300,
            effort="low",
            writable=False,
            allowed_tools=("Read", "Glob", "Grep"),
        )
        return {
            **payload,
            "status": "PREPARING",
            "codex_result": codex_result.model_dump(mode="json"),
            "checker_provider": "claude",
            "provider_selection_reason": "read-only canary routes checker to native Claude CLI",
            "checker_request": request.model_dump(mode="json"),
        }

    def _prepare_observability_claude_maker(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace = self._observability_workspace(payload)
        workflow_id = self._workflow_id(payload)
        request = AgentRequest(
            run_id=uuid5(NAMESPACE_URL, f"observability-claude-maker:{workflow_id}"),
            project=Project.FALCONE,
            role="implementer",
            provider=Provider.CLAUDE,
            model_class=ModelClass.WORKER,
            prompt=(
                "This is a read-only Engineering Control reverse observability canary. Do not "
                "call tools, read files, modify files, access credentials, or make external "
                "requests. Return only the required JSON with decision PASS, provider claude, "
                "and a short safe summary confirming maker visible-output streaming."
            ),
            working_directory=str(workspace),
            output_schema=self._observability_schema("claude"),
            timeout_seconds=300,
            effort="low",
            writable=False,
            allowed_tools=("Read", "Glob", "Grep"),
        )
        return {
            **payload,
            "status": "PREPARING",
            "maker_provider": "claude",
            "provider_selection_reason": (
                "read-only reverse canary routes maker to native Claude CLI"
            ),
            "maker_request": request.model_dump(mode="json"),
        }

    def _prepare_observability_codex_checker(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace = self._observability_workspace(payload)
        claude_result = AgentResult.model_validate(payload.get("agent_result"))
        if (
            claude_result.status.value != "SUCCESS"
            or claude_result.structured_result.get("decision") != "PASS"
            or claude_result.structured_result.get("provider") != "claude"
        ):
            raise ValueError("Claude canary result did not satisfy its immutable contract")
        workflow_id = self._workflow_id(payload)
        request = AgentRequest(
            run_id=uuid5(NAMESPACE_URL, f"observability-codex-checker:{workflow_id}"),
            project=Project.FALCONE,
            role="code-reviewer",
            provider=Provider.CODEX,
            model_class=ModelClass.JUDGE,
            prompt=(
                "This is an independent read-only Engineering Control reverse observability "
                "canary. Do not call tools, read files, modify files, access credentials, or "
                "make external requests. Return only the required JSON with decision PASS, "
                "provider codex, and a short safe summary confirming checker "
                "visible-output streaming."
            ),
            working_directory=str(workspace),
            output_schema=self._observability_schema("codex"),
            timeout_seconds=300,
            effort="low",
            writable=False,
            skip_git_repository_check=True,
            allowed_tools=("Read", "Glob", "Grep"),
        )
        return {
            **payload,
            "status": "PREPARING",
            "claude_result": claude_result.model_dump(mode="json"),
            "checker_provider": "codex",
            "provider_selection_reason": (
                "read-only reverse canary routes checker to native Codex CLI"
            ),
            "checker_request": request.model_dump(mode="json"),
        }

    def _finalize_observability(self, payload: dict[str, Any]) -> dict[str, Any]:
        terminal = str(payload.get("terminal_status", "FAILED"))
        if terminal not in {"COMPLETED", "FAILED"}:
            raise ValueError("observability canary terminal status is invalid")
        workflow_id = self._workflow_id(payload)
        if terminal == "COMPLETED":
            reverse = payload.get("provider_direction") == "claude-to-codex"
            claude = AgentResult.model_validate(
                payload.get("claude_result") if reverse else payload.get("agent_result")
            )
            codex = AgentResult.model_validate(
                payload.get("agent_result") if reverse else payload.get("codex_result")
            )
            if (
                codex.provider is not Provider.CODEX
                or claude.provider is not Provider.CLAUDE
                or codex.structured_result.get("decision") != "PASS"
                or claude.structured_result.get("decision") != "PASS"
                or codex.structured_result.get("provider") != "codex"
                or claude.structured_result.get("provider") != "claude"
            ):
                raise ValueError("observability canary provider independence is incomplete")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            run = session.scalar(
                select(Run).where(Run.workflow_id == workflow_id).order_by(Run.created_at.desc())
            )
            if run is None:
                raise ValueError("observability canary control run is missing")
            run.status = terminal
            run.finished_at = now
            run.error_code = str(payload.get("blocker") or "")[:120] or None
            session.add(
                DashboardEvent(
                    occurred_at=now,
                    level="INFO" if terminal == "COMPLETED" else "ERROR",
                    event_type=(
                        "workflow.completed" if terminal == "COMPLETED" else "workflow.failed"
                    ),
                    source="controller",
                    worker_kind="controller",
                    project=run.project,
                    workflow_id=workflow_id,
                    entity_type="synthetic_observability_acceptance",
                    entity_id=str(payload["workspace_id"]),
                    current_phase="observability.finalize",
                    message=(
                        "Native provider observability canary completed"
                        if terminal == "COMPLETED"
                        else "Native provider observability canary failed"
                    ),
                    metadata_json={"status": terminal},
                )
            )
        return {**payload, "status": terminal}

    def _persist_agent_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self._agent_request(payload)
        workflow_id = self._workflow_id(payload)
        raw_result = payload.get("agent_result")
        result = AgentResult.model_validate(raw_result) if isinstance(raw_result, dict) else None
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            run = self._provider_control_run(session, workflow_id, request, payload, now)
            invocation = session.get(AgentInvocation, request.run_id)
            if invocation is None:
                raise ValueError("provider result has no recorded invocation start")
            self._validate_invocation_binding(invocation, run, request)
            bundle = self._instruction_bundle(payload)
            expected_binding = (
                bundle.revision if bundle else request.agent_pack_revision,
                bundle.sha256 if bundle else request.instruction_bundle_sha256,
                bundle.sources if bundle else list(request.instruction_bundle_sources),
                bundle.allowed_tools if bundle else list(request.allowed_tools),
            )
            observed_binding = (
                invocation.agent_pack_revision,
                invocation.instruction_bundle_sha256,
                invocation.instruction_sources_json or [],
                invocation.allowed_tools_json or [],
            )
            if invocation.finished_at is not None and observed_binding != expected_binding:
                raise ValueError("agent instruction provenance conflicts with stored invocation")
            (
                invocation.agent_pack_revision,
                invocation.instruction_bundle_sha256,
                invocation.instruction_sources_json,
                invocation.allowed_tools_json,
            ) = expected_binding
            if result is not None:
                if (
                    result.run_id != request.run_id
                    or result.provider is not request.provider
                    or result.role != request.role
                ):
                    raise ValueError(
                        "provider result identity does not match its immutable request"
                    )
                if invocation.finished_at is not None and (
                    invocation.provider_session_id != result.provider_session_id
                    or invocation.status != result.status.value
                    or invocation.summary != result.summary
                    or invocation.structured_result != result.structured_result
                    or invocation.error_code != result.error_code
                ):
                    raise ValueError(
                        "agent invocation idempotency key conflicts with stored result"
                    )
                invocation.provider_session_id = result.provider_session_id
                invocation.status = result.status.value
                invocation.summary = result.summary
                invocation.structured_result = result.structured_result
                invocation.error_code = result.error_code
                invocation.started_at = result.started_at
                invocation.finished_at = result.finished_at
                usage_id = uuid5(NAMESPACE_URL, f"provider-usage:{request.run_id}")
                usage = session.get(ProviderUsage, usage_id)
                if usage is None:
                    usage = ProviderUsage(
                        id=usage_id,
                        provider=result.provider.value,
                        run_id=run.id,
                        role=result.role,
                        input_tokens=result.usage.input_tokens,
                        output_tokens=result.usage.output_tokens,
                        cached_input_tokens=result.usage.cached_input_tokens,
                        estimated_cost=result.usage.estimated_cost,
                        source=result.usage.source,
                    )
                    session.add(usage)
                else:
                    self._validate_usage_binding(usage, run, result)
                if result.status.value != "SUCCESS":
                    run.status = result.status.value
                    run.error_code = result.error_code or result.status.value
                    if result.status.value != "RATE_LIMITED":
                        run.finished_at = result.finished_at
                execution = session.get(AgentExecution, request.run_id)
                if execution is not None:
                    observability = payload.get("observability")
                    observation = observability if isinstance(observability, dict) else {}
                    execution.status = result.status.value
                    execution.started_at = execution.started_at or result.started_at
                    execution.last_heartbeat_at = result.finished_at
                    execution.ended_at = result.finished_at
                    execution.exit_code = result.exit_code
                    execution.retryable = result.status.value == "RATE_LIMITED"
                    execution.blocker_code = result.error_code
                    execution.error_summary = (
                        str(self.redactor.redact_text(result.summary).value)[:4000]
                        if result.status.value != "SUCCESS" and result.summary
                        else None
                    )
                    if execution.host is None:
                        execution.transcript_incomplete = True
                    execution.transcript_incomplete = bool(
                        execution.transcript_incomplete
                        or observation.get("transcript_incomplete") is True
                    )
                    execution.output_truncated = bool(
                        execution.output_truncated or observation.get("output_truncated") is True
                    )
                    incomplete_event = session.scalar(
                        select(DashboardEvent.id)
                        .where(
                            DashboardEvent.event_type == "provider.observability_incomplete",
                            DashboardEvent.entity_id == str(execution.id),
                        )
                        .limit(1)
                    )
                    if execution.transcript_incomplete and incomplete_event is None:
                        session.add(
                            DashboardEvent(
                                occurred_at=now,
                                level="WARNING",
                                event_type="provider.observability_incomplete",
                                source="controller",
                                worker_kind="controller",
                                project=run.project,
                                workflow_id=run.workflow_id,
                                campaign_id=str(run.campaign_id) if run.campaign_id else None,
                                entity_type="agent_execution",
                                entity_id=str(execution.id),
                                current_phase=execution.phase,
                                message="Provider transcript observability is incomplete",
                                metadata_json={
                                    "provider": execution.provider,
                                    "role": execution.role,
                                    "status": execution.status,
                                },
                            )
                        )
            else:
                status = str(payload.get("status", "BLOCKED"))
                blocker = str(payload.get("blocker", status))
                if status not in {"BLOCKED", "RATE_LIMITED", "FAILED", "TIMED_OUT", "CANCELLED"}:
                    raise ValueError("provider response without a result must be terminal")
                if invocation.finished_at is not None and (
                    invocation.status != status or invocation.error_code != blocker
                ):
                    raise ValueError(
                        "agent invocation idempotency key conflicts with stored blocker"
                    )
                invocation.status = status
                invocation.summary = blocker
                invocation.error_code = blocker
                invocation.finished_at = now
                run.status = status
                run.error_code = blocker
                if status != "RATE_LIMITED":
                    run.finished_at = now
                execution = session.get(AgentExecution, request.run_id)
                if execution is not None:
                    execution.status = status
                    execution.last_heartbeat_at = now
                    execution.ended_at = now
                    execution.retryable = status == "RATE_LIMITED"
                    execution.blocker_code = blocker
                    execution.error_summary = blocker
                    if execution.host is None:
                        execution.transcript_incomplete = True
        return {
            **payload,
            "agent_invocation_id": str(request.run_id),
            "agent_invocation_status": result.status.value if result is not None else status,
        }

    @staticmethod
    def _instruction_bundle(payload: dict[str, Any]) -> InstructionBundleRecord | None:
        raw = payload.get("instruction_bundle")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("instruction bundle provenance must be an object")
        return InstructionBundleRecord.model_validate(raw)

    def _agent_request(self, payload: dict[str, Any]) -> AgentRequest:
        provider_step = str(payload.get("provider_step", ""))
        request_key = self._provider_request_keys.get(provider_step)
        if request_key is None:
            raise ValueError("provider telemetry step is not allowlisted")
        raw_request = payload.get(request_key)
        if not isinstance(raw_request, dict):
            raise ValueError("provider telemetry is missing its normalized request")
        return AgentRequest.model_validate(raw_request)

    @staticmethod
    def _workflow_id(payload: dict[str, Any]) -> str:
        orchestration = payload.get("orchestration")
        if not isinstance(orchestration, dict):
            raise ValueError("provider telemetry requires Temporal orchestration identity")
        workflow_id = str(orchestration.get("workflow_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,239}", workflow_id):
            raise ValueError("provider telemetry workflow identity is invalid")
        return workflow_id

    def _provider_control_run(
        self,
        session: Session,
        workflow_id: str,
        request: AgentRequest,
        payload: dict[str, Any],
        now: datetime,
    ) -> Run:
        run = session.scalar(
            select(Run)
            .where(Run.workflow_id == workflow_id)
            .order_by(Run.created_at.desc())
            .limit(1)
        )
        if run is not None:
            if run.project != request.project.value:
                raise ValueError("provider telemetry project does not match its control run")
            return run
        run = Run(
            id=uuid5(NAMESPACE_URL, f"control-run:{workflow_id}"),
            campaign_id=request.campaign_id,
            change_set_id=request.change_set_id,
            workflow_id=workflow_id,
            run_type=self._provider_run_type(str(payload["provider_step"])),
            status="RUNNING",
            project=request.project.value,
            worker="controller",
            started_at=now,
        )
        session.add(run)
        session.flush()
        return run

    @staticmethod
    def _provider_run_type(provider_step: str) -> str:
        if provider_step.startswith("audit."):
            return "audit-campaign"
        if provider_step.startswith("finding."):
            return "finding-verification"
        if provider_step.startswith(("delivery.", "reconcile.")):
            return "issue-delivery"
        if provider_step.startswith("runtime."):
            return "runtime-verification"
        if provider_step.startswith("observability."):
            return "synthetic-observability"
        raise ValueError("provider telemetry step has no bounded run type")

    @staticmethod
    def _validate_invocation_binding(
        invocation: AgentInvocation, run: Run, request: AgentRequest
    ) -> None:
        if (
            invocation.run_id != run.id
            or invocation.provider != request.provider.value
            or invocation.role != request.role
            or invocation.model_class != request.model_class.value
        ):
            raise ValueError("agent invocation idempotency key conflicts with stored metadata")

    @staticmethod
    def _validate_usage_binding(usage: ProviderUsage, run: Run, result: AgentResult) -> None:
        expected = (
            run.id,
            result.provider.value,
            result.role,
            result.usage.input_tokens,
            result.usage.output_tokens,
            result.usage.cached_input_tokens,
            result.usage.estimated_cost,
            result.usage.source,
        )
        observed = (
            usage.run_id,
            usage.provider,
            usage.role,
            usage.input_tokens,
            usage.output_tokens,
            usage.cached_input_tokens,
            usage.estimated_cost,
            usage.source,
        )
        if observed != expected:
            raise ValueError("provider usage idempotency key conflicts with stored telemetry")

    async def _quota(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_snapshots = payload.get("snapshots")
        if not isinstance(raw_snapshots, list):
            raise ApplicationError(
                "quota persistence requires snapshot records",
                type="PolicyViolation",
                non_retryable=True,
            )
        try:
            snapshots = [QuotaSnapshotRecord.model_validate(item) for item in raw_snapshots]
        except ValidationError as error:
            raise ApplicationError(
                "quota snapshot failed strict validation",
                type="PolicyViolation",
                non_retryable=True,
            ) from error
        await asyncio.to_thread(self._persist_quota, snapshots)
        return {**payload, "status": "PERSISTED", "persisted_snapshots": len(snapshots)}

    def _persist_coverage(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["campaign_id"]))
        raw_cells = payload.get("cells")
        total = int(payload["total"])
        if not isinstance(raw_cells, list) or total < len(raw_cells) or total < 1:
            raise ValueError("coverage batch and total are inconsistent")
        cells = [CampaignCellRecord.model_validate(item) for item in raw_cells]
        with self.sessions.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise ValueError("campaign does not exist")
            campaign.required_cells = total
            campaign.status = "IN_PROGRESS"
            for cell in cells:
                identity = self._cell_id(campaign_id, cell)
                if session.get(CoverageCell, identity) is not None:
                    continue
                session.add(
                    CoverageCell(
                        id=identity,
                        campaign_id=campaign_id,
                        repository_surface=cell.repository_surface,
                        domain=cell.domain,
                        persona=cell.persona,
                        flow=cell.flow,
                        lens=cell.lens.value,
                        environment=cell.environment,
                        status="NOT_STARTED",
                        blocker=None,
                        exclusion_approval_id=None,
                        evidence_json=[],
                        result_json={},
                    )
                )
        return {**payload, "status": "COVERAGE_PERSISTED", "persisted_cells": len(cells)}

    def _persist_cell_blocker(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["campaign_id"]))
        cell = CampaignCellRecord.model_validate(payload["cell"])
        blocker = str(payload.get("blocker") or payload.get("status") or "BLOCKED")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", blocker):
            raise ValueError("audit cell blocker is invalid")
        deterministic_sha = str(payload.get("deterministic_evidence_sha256", ""))
        if not re.fullmatch(r"[a-f0-9]{64}", deterministic_sha):
            raise ValueError("audit cell blocker requires deterministic evidence")
        evidence = [
            {
                "kind": "deterministic-snapshot",
                "reference": (
                    f"coverage:{cell.domain}:{cell.persona}:{cell.lens.value}:{cell.environment}"
                ),
                "sha256": deterministic_sha,
            }
        ]
        self._root_blocker_classification(payload, blocker)
        with self.sessions.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            coverage = session.get(CoverageCell, self._cell_id(campaign_id, cell))
            if campaign is None or coverage is None:
                raise ValueError("campaign coverage cell does not exist")
            coverage.status = "BLOCKED"
            coverage.blocker = blocker
            coverage.evidence_json = evidence
            coverage.result_json = {
                "provider": payload.get("provider"),
                "status": "BLOCKED",
                "blocker": blocker,
            }
            roots = self._reconcile_campaign_root_blockers(session, campaign)
            root = roots[blocker]
        return {
            **payload,
            "blocker": blocker,
            "coverage_status": "BLOCKED",
            "blocker_evidence": evidence,
            "root_blocker": root.model_dump(mode="json"),
        }

    def _persist_snapshot_blockers(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["campaign_id"]))
        raw_cells = payload.get("cells")
        total = int(payload["total"])
        blocker = str(payload.get("blocker", ""))
        allowed_blockers = {
            "PROJECT_SNAPSHOT_UNAVAILABLE",
            "BOOTSTRAP_VALIDATION_SUPERSEDED",
        }
        if (
            not isinstance(raw_cells, list)
            or not raw_cells
            or total < len(raw_cells)
            or blocker not in allowed_blockers
        ):
            raise ValueError("snapshot blocker batch is invalid")
        cells = [CampaignCellRecord.model_validate(item) for item in raw_cells]
        self._root_blocker_classification(payload, blocker)
        with self.sessions.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise ValueError("campaign does not exist")
            if blocker == "BOOTSTRAP_VALIDATION_SUPERSEDED" and (
                campaign.metadata_json.get("dry_run") is not True
                or campaign.status not in {"QUEUED", "IN_PROGRESS"}
            ):
                raise ValueError("only a nonterminal dry-run campaign may be superseded")
            campaign.required_cells = total
            campaign.status = "IN_PROGRESS"
            for cell in cells:
                identity = self._cell_id(campaign_id, cell)
                canonical = json.dumps(
                    {
                        "blocker": blocker,
                        "campaign_id": str(campaign_id),
                        "cell": cell.model_dump(mode="json"),
                        "deployment_sha": campaign.deployment_sha,
                        "project": campaign.project,
                        "source_sha": campaign.source_sha,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
                evidence = [
                    {
                        "kind": (
                            "snapshot-blocker"
                            if blocker == "PROJECT_SNAPSHOT_UNAVAILABLE"
                            else "recovery-blocker"
                        ),
                        "reference": (
                            f"project-snapshot:{campaign.project}"
                            if blocker == "PROJECT_SNAPSHOT_UNAVAILABLE"
                            else f"campaign-recovery:{campaign.project}"
                        ),
                        "sha256": hashlib.sha256(canonical).hexdigest(),
                    }
                ]
                coverage = session.get(CoverageCell, identity)
                if coverage is None:
                    coverage = CoverageCell(
                        id=identity,
                        campaign_id=campaign_id,
                        repository_surface=cell.repository_surface,
                        domain=cell.domain,
                        persona=cell.persona,
                        flow=cell.flow,
                        lens=cell.lens.value,
                        environment=cell.environment,
                        status="BLOCKED",
                        blocker=blocker,
                        exclusion_approval_id=None,
                        evidence_json=evidence,
                        result_json={},
                    )
                    session.add(coverage)
                else:
                    coverage.status = "BLOCKED"
                    coverage.blocker = blocker
                    coverage.evidence_json = evidence
                coverage.result_json = {"status": "BLOCKED", "blocker": blocker}
            roots = self._reconcile_campaign_root_blockers(session, campaign)
            root = roots[blocker]
            run = session.scalar(
                select(Run)
                .where(Run.campaign_id == campaign_id, Run.run_type == "audit-campaign")
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            if run is not None:
                if run.started_at is None:
                    run.started_at = datetime.now(UTC)
                run.status = "RUNNING"
        return {
            **payload,
            "status": "BLOCKED_COVERAGE_PERSISTED",
            "persisted_cells": len(cells),
            "root_blocker": root.model_dump(mode="json"),
        }

    @staticmethod
    def _root_blocker_classification(
        payload: dict[str, Any], blocker: str
    ) -> RootBlockerClassification:
        defaults = {
            "PROJECT_SNAPSHOT_UNAVAILABLE": RootBlockerClassification.EXTERNAL_DEPENDENCY_FAILURE,
            "BOOTSTRAP_VALIDATION_SUPERSEDED": (
                RootBlockerClassification.ENGINEERING_CONTROL_DEFECT
            ),
            "PAIR_SNAPSHOT_NOT_PASSING": RootBlockerClassification.RUNTIME_PROVENANCE_GAP,
            "KUBERNETES_EVIDENCE_MISSING": RootBlockerClassification.RUNTIME_PROVENANCE_GAP,
            "RUNTIME_CORRELATION_MISSING": RootBlockerClassification.RUNTIME_PROVENANCE_GAP,
            "PERSONA_CREDENTIAL_UNAVAILABLE": RootBlockerClassification.MISSING_OPERATOR_INPUT,
            "BLOCKED_BY_CREDENTIAL": RootBlockerClassification.MISSING_OPERATOR_INPUT,
            "BLOCKED_BY_RUNTIME_HEALTH": RootBlockerClassification.DEPLOYMENT_DEFECT,
        }
        prefixes = {
            "PRODUCT_DEFECT_": RootBlockerClassification.PRODUCT_DEFECT,
            "DEPLOYMENT_DEFECT_": RootBlockerClassification.DEPLOYMENT_DEFECT,
            "ENGINEERING_CONTROL_DEFECT_": RootBlockerClassification.ENGINEERING_CONTROL_DEFECT,
            "TEST_HARNESS_DEFECT_": RootBlockerClassification.TEST_HARNESS_DEFECT,
            "MISSING_OPERATOR_INPUT_": RootBlockerClassification.MISSING_OPERATOR_INPUT,
            "EXTERNAL_DEPENDENCY_FAILURE_": (RootBlockerClassification.EXTERNAL_DEPENDENCY_FAILURE),
            "RUNTIME_PROVENANCE_GAP_": RootBlockerClassification.RUNTIME_PROVENANCE_GAP,
            "MANUAL_AUTH_REQUIRED_": RootBlockerClassification.MISSING_OPERATOR_INPUT,
        }
        classification = defaults.get(blocker)
        if classification is None:
            classification = next(
                (candidate for prefix, candidate in prefixes.items() if blocker.startswith(prefix)),
                RootBlockerClassification.EXTERNAL_DEPENDENCY_FAILURE,
            )
        supplied = payload.get("root_blocker_classification")
        if supplied is not None and RootBlockerClassification(str(supplied)) is not classification:
            raise ValueError("root blocker classification conflicts with its authoritative code")
        return classification

    @classmethod
    def _reconcile_campaign_root_blockers(
        cls, session: Session, campaign: Campaign
    ) -> dict[str, RootBlockerRecord]:
        blocked = session.scalars(
            select(CoverageCell).where(
                CoverageCell.campaign_id == campaign.id,
                CoverageCell.status == "BLOCKED",
            )
        ).all()
        by_code: dict[str, list[CoverageCell]] = {}
        for coverage in blocked:
            code = str(coverage.blocker or "BLOCKED")
            by_code.setdefault(code, []).append(coverage)
        metadata = dict(campaign.metadata_json or {})
        raw_roots = metadata.get("root_blockers", {})
        existing_roots = dict(raw_roots) if isinstance(raw_roots, dict) else {}
        records: dict[str, RootBlockerRecord] = {}
        roots: dict[str, dict[str, Any]] = {}
        for code, dependents in sorted(by_code.items()):
            identity = "rb-" + hashlib.sha256(f"{campaign.id}:{code}".encode()).hexdigest()[:32]
            existing = existing_roots.get(identity, {})
            root_finding_id = (
                existing.get("root_finding_id") if isinstance(existing, dict) else None
            )
            record = RootBlockerRecord(
                root_blocker_id=identity,
                classification=cls._root_blocker_classification({}, code),
                code=code,
                dependent_cell_count=len(dependents),
                root_finding_id=root_finding_id,
            )
            serialized = record.model_dump(mode="json")
            roots[identity] = serialized
            records[code] = record
            for dependent in dependents:
                current = dict(dependent.result_json or {})
                current["root_blocker"] = serialized
                dependent.result_json = current
        metadata["root_blockers"] = roots
        campaign.metadata_json = metadata
        return records

    def _prepare_agent_cell(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["campaign_id"]))
        cell = CampaignCellRecord.model_validate(payload["cell"])
        snapshot = self._object(payload.get("snapshot"), "snapshot")
        deterministic = self._object(payload.get("deterministic"), "deterministic")
        project = Project(str(snapshot["project"]))
        risk = (
            RiskClass.R3
            if cell.lens
            in {AuditLens.SECURITY, AuditLens.AUTHORIZATION, AuditLens.TENANT_ISOLATION}
            else RiskClass.R1
        )
        role = "security-auditor" if risk is RiskClass.R3 else "domain-auditor"
        model_class = ModelClass.JUDGE if risk is RiskClass.R3 else ModelClass.ROUTINE
        broker = self._quota_broker()
        decision = broker.route(role=role, model_class=model_class, risk_class=risk)
        if decision.primary is None:
            return {
                **payload,
                "status": decision.status,
                "routing_reason": decision.reason,
            }
        workflow_id = self._workflow_id(payload)
        request_id = uuid5(
            NAMESPACE_URL,
            "audit-agent:"
            f"{workflow_id}:{campaign_id}:{cell.repository_surface}:{cell.domain}:"
            f"{cell.persona}:{cell.flow}:{cell.lens.value}:{cell.environment}",
        )
        prompt = (
            "Audit exactly one immutable coverage cell. Do not modify files. "
            "Use the deterministic evidence supplied below and inspect only the bounded source "
            "and deployment worktrees. Report PASS only with evidence. Report FAIL only with a "
            "minimal reproducible finding.\n\n"
            f"Project: {project.value}\nRepository surface: {cell.repository_surface}\n"
            f"Domain: {cell.domain}\nPersona: {cell.persona}\nFlow: {cell.flow}\n"
            f"Lens: {cell.lens.value}\nEnvironment: {cell.environment}\n"
            f"Source SHA: {snapshot['source_sha']}\n"
            f"Deployment SHA: {snapshot['deployment_sha']}\n"
            f"Deployment worktree: {snapshot['deployment_worktree']}\n"
            f"Deterministic evidence SHA256: {deterministic['evidence_sha256']}\n"
        )
        request = AgentRequest(
            run_id=request_id,
            project=project,
            role=role,
            provider=decision.primary,
            model_class=model_class,
            prompt=prompt,
            working_directory=str(snapshot["source_worktree"]),
            output_schema=self._audit_output_schema(),
            timeout_seconds=3600,
            effort="high" if risk is RiskClass.R3 else "medium",
            writable=False,
            campaign_id=campaign_id,
        )
        return {
            **payload,
            "provider": decision.primary.value,
            "routing_reason": decision.reason,
            "agent_request": request.model_dump(mode="json"),
            "status": "READY",
        }

    def _persist_cell_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["campaign_id"]))
        cell = CampaignCellRecord.model_validate(payload["cell"])
        agent_result = self._object(payload.get("agent_result"), "agent_result")
        structured = self._object(agent_result.get("structured_result"), "structured_result")
        decision = str(structured.get("decision", ""))
        evidence = structured.get("evidence")
        if decision not in {"PASS", "FAIL", "BLOCKED", "REQUEST_CHANGES"}:
            raise ValueError("audit result has an unknown decision")
        finding_payload = structured.get("finding")
        if decision == "FAIL" and not isinstance(finding_payload, dict):
            raise ValueError("failed audit result requires a finding")
        if decision != "FAIL" and finding_payload is not None:
            raise ValueError("only a failed audit result may contain a finding")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("audit result requires explicit evidence")
        deterministic_sha = str(payload.get("deterministic_evidence_sha256", ""))
        if len(deterministic_sha) != 64 or not any(
            isinstance(item, dict) and item.get("sha256") == deterministic_sha for item in evidence
        ):
            raise ValueError("audit result does not bind to deterministic evidence")
        status = {
            "PASS": "NO_FINDING",
            "FAIL": "CANDIDATE_FOUND",
            "BLOCKED": "BLOCKED",
            "REQUEST_CHANGES": "RETEST_REQUIRED",
        }[decision]
        blocker = str(structured.get("summary", "")) if decision == "BLOCKED" else None
        finding_id: UUID | None = None
        fingerprint: str | None = None
        with self.sessions.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            coverage = session.get(CoverageCell, self._cell_id(campaign_id, cell))
            if campaign is None or coverage is None:
                raise ValueError("campaign coverage cell does not exist")
            coverage.status = status
            coverage.blocker = blocker
            coverage.evidence_json = evidence
            coverage.result_json = {
                "provider": agent_result.get("provider"),
                "run_id": agent_result.get("run_id"),
                "summary": structured.get("summary"),
                "decision": decision,
                "open_questions": structured.get("open_questions", []),
            }
            if decision == "FAIL":
                candidate = FindingCandidateRecord.model_validate(finding_payload)
                fingerprint = self._fingerprint(
                    campaign,
                    cell,
                    candidate.affected_component,
                    candidate.minimal_reproduction,
                )
                existing = session.scalar(select(Finding).where(Finding.fingerprint == fingerprint))
                finding_id = existing.id if existing else uuid5(NAMESPACE_URL, fingerprint)
                if existing is None:
                    risk = (
                        "R3"
                        if cell.lens
                        in {
                            AuditLens.SECURITY,
                            AuditLens.AUTHORIZATION,
                            AuditLens.TENANT_ISOLATION,
                        }
                        else "R2"
                    )
                    session.add(
                        Finding(
                            id=finding_id,
                            campaign_id=campaign_id,
                            coverage_cell_id=coverage.id,
                            fingerprint=fingerprint,
                            project=campaign.project,
                            domain=cell.domain,
                            persona=cell.persona,
                            severity=candidate.severity,
                            confidence=candidate.confidence,
                            risk_class=risk,
                            disposition="CANDIDATE",
                            expected_behavior=candidate.expected_behavior,
                            actual_behavior=candidate.actual_behavior,
                            evidence_json=evidence,
                            minimal_reproduction=candidate.minimal_reproduction,
                            affected_source_sha=campaign.source_sha,
                            affected_deployment_sha=campaign.deployment_sha,
                            affected_image_digest=None,
                            affected_component=candidate.affected_component,
                            impact_json={
                                "security": candidate.security_impact,
                                "data": candidate.data_impact,
                                "repository_surface": cell.repository_surface,
                                "flow": cell.flow,
                                "cleanup_result": self._cleanup_result_for_flow(
                                    campaign.project,
                                    cell.repository_surface,
                                    cell.flow,
                                ),
                            },
                            recommended_action=candidate.recommended_action,
                        )
                    )
        return {
            **payload,
            "status": status,
            "finding_id": str(finding_id) if finding_id else None,
            "fingerprint": fingerprint,
        }

    def _finalize_campaign(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["campaign_id"]))
        expected = int(payload["expected_cells"])
        terminal_blocker = payload.get("terminal_blocker")
        if terminal_blocker not in {None, "BOOTSTRAP_VALIDATION_SUPERSEDED"}:
            raise ValueError("campaign terminal blocker is invalid")
        with self.sessions.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise ValueError("campaign does not exist")
            cells = session.scalars(
                select(CoverageCell).where(CoverageCell.campaign_id == campaign_id)
            ).all()
            if len(cells) != expected or campaign.required_cells != expected:
                campaign.status = "INCOMPLETE"
            elif any(cell.status in {"CANDIDATE_FOUND", "RETEST_REQUIRED"} for cell in cells):
                campaign.status = "VERIFICATION_REQUIRED"
            elif all(
                cell.exclusion_approval_id is not None
                or (cell.status == "BLOCKED" and bool(cell.blocker))
                or (
                    cell.status in {"NO_FINDING", "CONFIRMED_FINDING", "VERIFIED_FIXED"}
                    and bool(cell.evidence_json)
                )
                for cell in cells
            ):
                campaign.status = "COMPLETE"
            else:
                campaign.status = "INCOMPLETE"
            status = campaign.status
            run = session.scalar(
                select(Run)
                .where(Run.campaign_id == campaign_id, Run.run_type == "audit-campaign")
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            if run is not None:
                run.status = (
                    "CANCELLED"
                    if terminal_blocker == "BOOTSTRAP_VALIDATION_SUPERSEDED"
                    else "SUCCESS"
                    if status in {"COMPLETE", "VERIFICATION_REQUIRED"}
                    else "BLOCKED"
                )
                run.finished_at = datetime.now(UTC)
                run.error_code = (
                    str(terminal_blocker)
                    if terminal_blocker is not None
                    else None
                    if run.status == "SUCCESS"
                    else status
                )
                run.summary = f"campaign={campaign_id};status={status};cells={len(cells)}"
        return {**payload, "status": status, "completed_cells": len(cells)}

    def _record_campaign_group_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow_id = self._workflow_id(payload)
        if payload.get("workflow_id") != workflow_id:
            raise ValueError("multi-project audit workflow identity changed")
        status = str(payload.get("status", ""))
        allowed_statuses = {"RUNNING", "SUCCESS", "BLOCKED", "FAILED", "CANCELLED"}
        if status not in allowed_statuses:
            raise ValueError("multi-project audit status is invalid")
        preset = str(payload.get("preset", ""))
        if preset not in {
            "snapshot",
            "static-full",
            "e2e-smoke",
            "full-e2e",
            "security",
            "ux",
            "deployment",
        }:
            raise ValueError("multi-project audit preset is invalid")
        if payload.get("environment") != "staging":
            raise ValueError("multi-project audit environment is invalid")
        current_project = payload.get("current_project")
        if current_project is not None and current_project not in {
            "falcone",
            "musematic",
            "llmwiki",
        }:
            raise ValueError("multi-project audit current project is invalid")
        raw_results = payload.get("project_results", {})
        if not isinstance(raw_results, dict) or not set(raw_results).issubset(
            {"falcone", "musematic", "llmwiki"}
        ):
            raise ValueError("multi-project audit results are invalid")
        results: dict[str, dict[str, str | None]] = {}
        for project, raw_result in raw_results.items():
            if not isinstance(raw_result, dict):
                raise ValueError("multi-project audit project result is invalid")
            child_status = str(raw_result.get("status", ""))
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", child_status):
                raise ValueError("multi-project audit child status is invalid")
            campaign_id = str(UUID(str(raw_result.get("campaign_id", ""))))
            child_workflow_id = str(raw_result.get("workflow_id", ""))
            if not re.fullmatch(r"[A-Za-z0-9._:-]{1,240}", child_workflow_id):
                raise ValueError("multi-project audit child workflow ID is invalid")
            blocker_value = raw_result.get("blocker")
            blocker = str(blocker_value) if blocker_value is not None else None
            if blocker is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", blocker):
                raise ValueError("multi-project audit blocker is invalid")
            results[str(project)] = {
                "status": child_status,
                "campaign_id": campaign_id,
                "workflow_id": child_workflow_id,
                "blocker": blocker,
            }
        now = datetime.now(UTC)
        summary = json.dumps(
            {
                "preset": preset,
                "environment": "staging",
                "current_project": current_project,
                "project_results": results,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.sessions.begin() as session:
            run = session.scalar(
                select(Run)
                .where(
                    Run.workflow_id == workflow_id,
                    Run.run_type == "multi-project-audit",
                )
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            if run is None:
                raise ValueError("multi-project audit run does not exist")
            run.status = status
            run.started_at = run.started_at or now
            run.finished_at = now if status != "RUNNING" else None
            run.error_code = status if status in {"BLOCKED", "FAILED", "CANCELLED"} else None
            run.summary = summary[:4000]
            event_type = (
                "workflow.started"
                if status == "RUNNING" and not results
                else "workflow.progress"
                if status == "RUNNING"
                else "workflow.completed"
                if status == "SUCCESS"
                else "workflow.failed"
            )
            phase = f"audit_{current_project}" if current_project is not None else status.lower()
            existing_event = session.scalar(
                select(DashboardEvent.id)
                .where(
                    DashboardEvent.workflow_id == workflow_id,
                    DashboardEvent.event_type == event_type,
                    DashboardEvent.current_phase == phase,
                )
                .limit(1)
            )
            if existing_event is None:
                session.add(
                    DashboardEvent(
                        occurred_at=now,
                        level="INFO" if status in {"RUNNING", "SUCCESS"} else "WARNING",
                        event_type=event_type,
                        source="controller",
                        worker_kind="controller",
                        project="platform",
                        workflow_id=workflow_id,
                        entity_type="multi_project_audit",
                        entity_id=workflow_id,
                        current_phase=phase,
                        message=(
                            "All-project audit advanced"
                            if status == "RUNNING"
                            else f"All-project audit reached {status.lower()}"
                        ),
                        metadata_json={
                            "status": status,
                            "current_project": current_project,
                            "completed_projects": sorted(results),
                        },
                    )
                )
        return {**payload, "project_results": results}

    def _load_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        finding_id = UUID(str(payload["finding_id"]))
        workflow_id = self._workflow_id(payload)
        with self.sessions.begin() as session:
            finding = session.get(Finding, finding_id)
            if finding is None or finding.disposition not in {"CANDIDATE", "FLAKY", "BLOCKED"}:
                raise ValueError("verification requires a CANDIDATE, FLAKY, or BLOCKED finding")
            if str(payload.get("project", "")) != finding.project:
                raise ValueError("verification project does not match the authoritative finding")
            coverage = session.get(CoverageCell, finding.coverage_cell_id)
            if coverage is None:
                raise ValueError("finding coverage metadata is missing")
            discoverer = coverage.result_json.get("provider")
            if discoverer not in {Provider.CODEX.value, Provider.CLAUDE.value}:
                raise ValueError("finding discoverer provider is missing")
            run = session.scalar(
                select(Run)
                .where(Run.workflow_id == workflow_id)
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            if run is None:
                run = Run(
                    id=uuid5(NAMESPACE_URL, f"control-run:{workflow_id}"),
                    campaign_id=finding.campaign_id,
                    workflow_id=workflow_id,
                    run_type="finding-verification",
                    status="RUNNING",
                    project=finding.project,
                    worker="controller",
                    started_at=datetime.now(UTC),
                    summary=str(finding.id),
                )
                session.add(run)
            elif (
                run.campaign_id != finding.campaign_id
                or run.project != finding.project
                or run.run_type != "finding-verification"
            ):
                raise ValueError("finding verification workflow conflicts with stored run metadata")
            else:
                if run.status == "QUEUED":
                    run.status = "RUNNING"
                if run.started_at is None:
                    run.started_at = datetime.now(UTC)
            return {
                **payload,
                "status": "CANDIDATE_LOADED",
                "finding_id": str(finding.id),
                "campaign_id": str(finding.campaign_id),
                "coverage_cell_id": str(finding.coverage_cell_id),
                "fingerprint": finding.fingerprint,
                "project": finding.project,
                "domain": finding.domain,
                "persona": finding.persona,
                "severity": finding.severity,
                "confidence": finding.confidence,
                "risk_class": finding.risk_class,
                "expected_behavior": finding.expected_behavior,
                "actual_behavior": finding.actual_behavior,
                "minimal_reproduction": finding.minimal_reproduction,
                "affected_source_sha": finding.affected_source_sha,
                "affected_deployment_sha": finding.affected_deployment_sha,
                "affected_component": finding.affected_component,
                "repository_surface": finding.impact_json.get("repository_surface", "source"),
                "flow": finding.impact_json.get("flow", "repository-static"),
                "discoverer_provider": discoverer,
            }

    def _prepare_verifier(self, payload: dict[str, Any]) -> dict[str, Any]:
        finding = self._object(payload.get("finding"), "finding")
        snapshot = self._object(payload.get("snapshot"), "snapshot")
        ordinal = int(payload["ordinal"])
        if ordinal not in {1, 2}:
            raise ValueError("verification ordinal must be one or two")
        maker = Provider(str(finding["discoverer_provider"]))
        risk = RiskClass(str(finding["risk_class"]))
        routing = self._quota_broker().route(
            role="finding-verifier",
            model_class=ModelClass.JUDGE,
            risk_class=risk,
            maker=maker,
        )
        if routing.primary is None:
            return {**payload, "status": routing.status, "routing_reason": routing.reason}
        workflow_id = self._workflow_id(payload)
        run_id = uuid5(
            NAMESPACE_URL,
            f"finding-verifier:{workflow_id}:{finding['finding_id']}:"
            f"{snapshot['source_sha']}:{ordinal}",
        )
        prompt = (
            "Independently verify one candidate finding from a fresh immutable snapshot. "
            "Do not modify files. Reproduce only the supplied minimal steps, establish a "
            "positive and a negative control, and reject environment failures. Do not file an "
            "issue.\n\n"
            f"Finding fingerprint: {finding['fingerprint']}\n"
            f"Expected: {finding['expected_behavior']}\n"
            f"Actual: {finding['actual_behavior']}\n"
            f"Minimal reproduction: {json.dumps(finding['minimal_reproduction'])}\n"
            f"Affected component: {finding['affected_component']}\n"
            f"Source SHA: {snapshot['source_sha']}\n"
            f"Deployment SHA: {snapshot['deployment_sha']}\n"
            f"Deployment worktree: {snapshot['deployment_worktree']}\n"
            f"Fresh snapshot evidence SHA256: {snapshot['snapshot_evidence_sha256']}\n"
            f"Independent reproduction ordinal: {ordinal}\n"
        )
        request = AgentRequest(
            run_id=run_id,
            project=Project(str(finding["project"])),
            role="finding-verifier",
            provider=routing.primary,
            model_class=ModelClass.JUDGE,
            prompt=prompt,
            working_directory=str(snapshot["source_worktree"]),
            output_schema=self._verification_output_schema(),
            timeout_seconds=3600,
            effort="high",
            writable=False,
            campaign_id=UUID(str(finding["campaign_id"])),
        )
        return {
            **payload,
            "provider": routing.primary.value,
            "routing_reason": routing.reason,
            "verifier_request": request.model_dump(mode="json"),
            "status": "READY",
        }

    def _evaluate_controls(self, payload: dict[str, Any]) -> dict[str, Any]:
        attempts = payload.get("attempts")
        snapshot = self._object(payload.get("snapshot"), "snapshot")
        if not isinstance(attempts, list) or len(attempts) != 2:
            raise ValueError("finding verification requires exactly two attempts")
        results: list[dict[str, Any]] = []
        anchor = str(snapshot["snapshot_evidence_sha256"])
        for attempt in attempts:
            outer = self._object(attempt, "attempt")
            agent = self._object(outer.get("agent_result"), "agent_result")
            structured = self._object(agent.get("structured_result"), "structured_result")
            evidence = structured.get("evidence")
            if not isinstance(evidence, list) or not any(
                isinstance(item, dict) and item.get("sha256") == anchor for item in evidence
            ):
                raise ValueError("verification attempt is not bound to fresh snapshot evidence")
            results.append(structured)
        environment_valid = all(bool(item.get("environment_valid")) for item in results)
        reproduced = [bool(item.get("reproduced")) for item in results]
        positive = all(bool(item.get("positive_control")) for item in results)
        negative = all(bool(item.get("negative_control")) for item in results)
        if not environment_valid:
            disposition = "BLOCKED"
        elif reproduced == [True, True] and positive and negative:
            disposition = "CONFIRMED"
        elif reproduced == [False, False]:
            disposition = "REJECTED"
        else:
            disposition = "FLAKY"
        return {
            **payload,
            "status": disposition,
            "disposition": disposition,
            "reproduction_count": sum(reproduced),
            "positive_control": positive,
            "negative_control": negative,
            "environment_valid": environment_valid,
        }

    def _finalize_finding(self, payload: dict[str, Any]) -> dict[str, Any]:
        finding_payload = self._object(payload.get("finding"), "finding")
        controls = self._object(payload.get("controls"), "controls")
        duplicate = self._object(payload.get("duplicate"), "duplicate")
        attempts = payload.get("attempts")
        if not isinstance(attempts, list) or len(attempts) != 2:
            raise ValueError("finding finalization requires two attempts")
        disposition = str(controls["disposition"])
        if duplicate.get("status") == "BLOCKED":
            disposition = "BLOCKED"
        elif duplicate.get("status") == "DUPLICATE":
            disposition = "DUPLICATE"
        finding_id = UUID(str(finding_payload["finding_id"]))
        workflow_id = self._workflow_id(payload)
        evidence: list[Any] = []
        verifier_provider: str | None = None
        for attempt in attempts:
            agent = self._object(self._object(attempt, "attempt").get("agent_result"), "agent")
            verifier_provider = verifier_provider or str(agent.get("provider"))
            structured = self._object(agent.get("structured_result"), "structured_result")
            attempt_evidence = structured.get("evidence")
            if isinstance(attempt_evidence, list):
                evidence.extend(attempt_evidence)
        with self.sessions.begin() as session:
            finding = session.get(Finding, finding_id)
            if finding is None:
                raise ValueError("finding does not exist")
            run = session.scalar(
                select(Run)
                .where(Run.workflow_id == workflow_id)
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            if run is None or run.project != finding.project:
                raise ValueError("finding verification control run is missing")
            verification_id = uuid5(
                NAMESPACE_URL,
                f"finding-verification:{finding_id}:{workflow_id}",
            )
            existing = session.get(FindingVerification, verification_id)
            if existing is not None:
                expected = (
                    finding_id,
                    run.id,
                    verifier_provider or "unknown",
                    disposition,
                    int(controls.get("reproduction_count", 0)),
                    bool(controls.get("positive_control")),
                    bool(controls.get("negative_control")),
                    duplicate.get("duplicate_issue_url"),
                )
                observed = (
                    existing.finding_id,
                    existing.run_id,
                    existing.verifier_provider,
                    existing.disposition,
                    existing.reproduction_count,
                    existing.positive_control,
                    existing.negative_control,
                    existing.duplicate_issue_url,
                )
                if observed != expected or finding.disposition != disposition:
                    raise ValueError(
                        "finding verification idempotency key conflicts with stored result"
                    )
                return {
                    **payload,
                    "status": disposition,
                    "disposition": disposition,
                    "finding_id": str(finding_id),
                    "duplicate_issue_url": duplicate.get("duplicate_issue_url"),
                }
            if finding.disposition not in {"CANDIDATE", "FLAKY", "BLOCKED"}:
                raise ValueError("finding is no longer independently verifiable")
            finding.disposition = disposition
            finding.evidence_json = [*finding.evidence_json, *evidence]
            coverage = session.get(CoverageCell, finding.coverage_cell_id)
            if coverage is None:
                raise ValueError("finding coverage metadata is missing")
            coverage.status = {
                "CONFIRMED": "CONFIRMED_FINDING",
                "REJECTED": "NO_FINDING",
                "DUPLICATE": "NO_FINDING",
                "FLAKY": "RETEST_REQUIRED",
                "BLOCKED": "BLOCKED",
            }[disposition]
            if disposition == "BLOCKED":
                coverage.blocker = str(duplicate.get("blocker") or "FINDING_VERIFICATION_BLOCKED")
            else:
                coverage.blocker = None
            session.add(
                FindingVerification(
                    id=verification_id,
                    finding_id=finding_id,
                    run_id=run.id,
                    verifier_provider=verifier_provider or "unknown",
                    disposition=disposition,
                    reproduction_count=int(controls.get("reproduction_count", 0)),
                    positive_control=bool(controls.get("positive_control")),
                    negative_control=bool(controls.get("negative_control")),
                    duplicate_issue_url=duplicate.get("duplicate_issue_url"),
                    summary=f"independent verification result: {disposition}",
                )
            )
            run.status = (
                "SUCCESS" if disposition in {"CONFIRMED", "REJECTED", "DUPLICATE"} else "BLOCKED"
            )
            run.finished_at = datetime.now(UTC)
            run.error_code = None if run.status == "SUCCESS" else disposition
            run.summary = f"independent verification result: {disposition}"
        return {
            **payload,
            "status": disposition,
            "disposition": disposition,
            "finding_id": str(finding_id),
            "duplicate_issue_url": duplicate.get("duplicate_issue_url"),
        }

    def _prepare_finding_publication(self, payload: dict[str, Any]) -> dict[str, Any]:
        finding_id = UUID(str(payload["finding_id"]))
        with self.sessions() as session:
            finding = session.get(Finding, finding_id)
            if finding is None or finding.disposition != "CONFIRMED":
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "CONFIRMED_FINDING_REQUIRED",
                }
            existing = session.scalar(select(Issue).where(Issue.finding_id == finding_id))
            if existing is not None:
                return {
                    **payload,
                    "status": "ALREADY_PUBLISHED",
                    "issue_url": existing.url,
                    "issue_repository": existing.repository,
                }
            surface = str(finding.impact_json.get("repository_surface", "source"))
            flow = str(finding.impact_json.get("flow", "repository-static"))
            cleanup_result = finding.impact_json.get("cleanup_result")
            if not isinstance(cleanup_result, str) or not cleanup_result:
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "CLEANUP_RESULT_REQUIRED_FOR_PUBLICATION",
                }
            project = Project(finding.project)
            source_repository, deployment_repository = self._project_issue_repositories(project)
            classification = {
                "source": "application",
                "deployment": "deployment",
                "pair": "cross-repository",
                "runtime": "cross-repository",
            }.get(surface)
            if finding.impact_json.get("ownership_classification") == "falcone-contract-gap":
                classification = "falcone-contract-gap"
            if classification is None:
                raise ValueError("finding publication repository surface is invalid")
            typed_classification = cast(
                Literal["application", "deployment", "cross-repository", "falcone-contract-gap"],
                classification,
            )
            affected_repositories = {
                "application": [source_repository],
                "deployment": [deployment_repository],
                "cross-repository": [source_repository, deployment_repository],
                "falcone-contract-gap": [
                    "gntik-ai/falcone",
                    "gntik-ai/falcone-charts",
                    "gntik-ai/llmwiki",
                ],
            }[typed_classification]
            snapshot = session.scalar(
                select(ProjectSnapshot).where(ProjectSnapshot.campaign_id == finding.campaign_id)
            )
            running_revision = (
                finding.affected_image_digest
                or (snapshot.live_revision if snapshot is not None else None)
                or f"deployment-sha:{finding.affected_deployment_sha}"
            )
            evidence_handles = []
            for item in finding.evidence_json:
                if not isinstance(item, dict):
                    continue
                reference = str(item.get("reference", "evidence"))
                digest = str(item.get("sha256", ""))
                if re.fullmatch(r"[a-f0-9]{64}", digest):
                    evidence_handles.append(f"{reference}:{digest}")
            if not evidence_handles:
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "EVIDENCE_HANDLES_REQUIRED_FOR_PUBLICATION",
                }
            publication = FindingPublication(
                project=project.value,
                classification=typed_classification,
                affected_repositories=affected_repositories,
                source_sha=finding.affected_source_sha,
                deployment_sha=str(finding.affected_deployment_sha),
                running_revision=running_revision,
                persona=finding.persona,
                flow=flow,
                expected_behavior=finding.expected_behavior,
                actual_behavior=finding.actual_behavior,
                minimal_reproduction=[str(item) for item in finding.minimal_reproduction],
                evidence_handles=evidence_handles,
                severity=finding.severity,
                risk=cast(Literal["R0", "R1", "R2", "R3"], finding.risk_class),
                fingerprint=finding.fingerprint,
                acceptance_criteria=[finding.recommended_action],
                cleanup_result=cleanup_result,
                falcone_source_sha=finding.impact_json.get("falcone_source_sha"),
                falcone_charts_sha=finding.impact_json.get("falcone_charts_sha"),
                llmwiki_sha=finding.impact_json.get("llmwiki_sha"),
                consumer_contract=finding.impact_json.get("consumer_contract"),
                staging_target=finding.impact_json.get("staging_target"),
            )
        return {
            **payload,
            "status": "PUBLICATION_READY",
            "finding_publication": {
                "finding_id": str(finding_id),
                "repository": publication.primary_repository(),
                "title": f"Finding: {finding.actual_behavior.splitlines()[0]}"[:200],
                "body": publication.body(),
                "fingerprint": publication.fingerprint,
            },
        }

    def _persist_finding_publication(self, payload: dict[str, Any]) -> dict[str, Any]:
        publication = self._object(payload.get("finding_publication"), "finding_publication")
        finding_id = UUID(str(publication["finding_id"]))
        repository = str(publication["repository"])
        issue_url = str(payload.get("issue_url", ""))
        match = re.fullmatch(
            rf"https://github\.com/{re.escape(repository)}/issues/([1-9][0-9]*)",
            issue_url,
        )
        if payload.get("status") != "PUBLISHED" or match is None:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "FINDING_PUBLICATION_EVIDENCE_INVALID",
            }
        with self.sessions.begin() as session:
            finding = session.get(Finding, finding_id)
            if finding is None or finding.disposition != "CONFIRMED":
                raise ValueError("published issue lost its confirmed finding binding")
            existing = session.scalar(select(Issue).where(Issue.finding_id == finding_id))
            issue_id = uuid5(NAMESPACE_URL, f"finding-issue:{finding_id}")
            if existing is None:
                existing = Issue(
                    id=issue_id,
                    finding_id=finding_id,
                    repository=repository,
                    number=int(match.group(1)),
                    url=issue_url,
                    state="OPEN",
                    openspec_change=None,
                )
                session.add(existing)
            elif existing.repository != repository or existing.url != issue_url:
                raise ValueError("finding issue publication idempotency binding changed")
            change_set = session.scalar(select(ChangeSet).where(ChangeSet.finding_id == finding_id))
            if change_set is not None:
                change_set.issue_id = existing.id
        return {**payload, "status": "PUBLISHED", "issue_id": str(issue_id)}

    def _record_finding_terminal(self, payload: dict[str, Any]) -> dict[str, Any]:
        finding_payload = self._object(payload.get("finding"), "finding")
        finding_id = UUID(str(finding_payload["finding_id"]))
        workflow_id = self._workflow_id(payload)
        status = str(payload.get("status", "BLOCKED"))
        if status not in {"BLOCKED", "RATE_LIMITED", "FAILED", "TIMED_OUT", "CANCELLED"}:
            raise ValueError("finding terminal status is not allowlisted")
        blocker = str(payload.get("blocker") or payload.get("routing_reason") or status)
        if not blocker or len(blocker) > 500:
            raise ValueError("finding terminal blocker is invalid")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            finding = session.get(Finding, finding_id)
            run = session.scalar(
                select(Run)
                .where(Run.workflow_id == workflow_id)
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            if finding is None or run is None or run.campaign_id != finding.campaign_id:
                raise ValueError("finding terminal metadata binding is incomplete")
            if finding.disposition not in {"CANDIDATE", "FLAKY", "BLOCKED"}:
                raise ValueError("finding is no longer independently verifiable")
            finding.disposition = "BLOCKED"
            coverage = session.get(CoverageCell, finding.coverage_cell_id)
            if coverage is None:
                raise ValueError("finding coverage metadata is missing")
            coverage.status = "BLOCKED"
            coverage.blocker = blocker
            run.status = status
            run.error_code = blocker
            run.summary = f"independent verification blocked: {blocker}"
            if status != "RATE_LIMITED":
                run.finished_at = now
            verification_id = uuid5(
                NAMESPACE_URL,
                f"finding-verification-terminal:{finding_id}:{workflow_id}",
            )
            if session.get(FindingVerification, verification_id) is None:
                session.add(
                    FindingVerification(
                        id=verification_id,
                        finding_id=finding_id,
                        run_id=run.id,
                        verifier_provider=str(payload.get("provider") or "unassigned"),
                        disposition="BLOCKED",
                        reproduction_count=0,
                        positive_control=False,
                        negative_control=False,
                        duplicate_issue_url=None,
                        summary=f"independent verification blocked: {blocker}",
                    )
                )
        return {
            **payload,
            "status": status,
            "disposition": "BLOCKED",
            "blocker": blocker,
            "finding_id": str(finding_id),
        }

    def _load_confirmed_finding(self, payload: dict[str, Any]) -> dict[str, Any]:
        finding_id = UUID(str(payload["finding_id"]))
        requested_risk = RiskClass(str(payload["risk_class"]))
        orchestration = self._object(payload.get("orchestration"), "orchestration")
        workflow_id = str(orchestration.get("workflow_id", ""))
        if not workflow_id:
            raise ValueError("delivery workflow identity is missing")
        with self.sessions.begin() as session:
            finding = session.get(Finding, finding_id)
            if finding is None or finding.disposition != "CONFIRMED":
                raise ValueError("delivery requires a CONFIRMED finding")
            if requested_risk.value != finding.risk_class:
                raise ValueError("delivery risk class must match authoritative finding risk")
            if not finding.affected_deployment_sha:
                raise ValueError("delivery requires an immutable deployment repository SHA")
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if run is None:
                raise ValueError("delivery run metadata is missing")
            project = Project(finding.project)
            source_repository, deployment_repository = {
                Project.FALCONE: ("falcone", "falcone-charts"),
                Project.MUSEMATIC: ("musematic", "musematic-deploy"),
                Project.LLMWIKI: ("llmwiki", "llmwiki"),
            }[project]
            surface = str(finding.impact_json.get("repository_surface", "source"))
            source_issue_repository, deployment_issue_repository = self._project_issue_repositories(
                project
            )
            issue_repository = {
                "source": source_issue_repository,
                "deployment": deployment_issue_repository,
                "pair": source_issue_repository,
                "runtime": source_issue_repository,
            }.get(surface)
            if issue_repository is None:
                raise ValueError("confirmed finding repository surface is invalid")
            change_set_id = uuid5(NAMESPACE_URL, f"changeset:{finding.id}")
            change_set = session.get(ChangeSet, change_set_id)
            if change_set is None:
                change_set = ChangeSet(
                    id=change_set_id,
                    finding_id=finding.id,
                    issue_id=None,
                    project=project.value,
                    risk_class=finding.risk_class,
                    state="CONFIRMED",
                    version=1,
                    source_repository=source_repository,
                    source_branch=None,
                    source_base_sha=finding.affected_source_sha,
                    source_head_sha=None,
                    source_pr_url=None,
                    deployment_repository=deployment_repository,
                    deployment_branch=None,
                    deployment_base_sha=finding.affected_deployment_sha,
                    deployment_head_sha=None,
                    deployment_pr_url=None,
                    image_digests={},
                    chart_version=None,
                    recovery_json={},
                )
                session.add(change_set)
            run.change_set_id = change_set_id
            run.status = "RUNNING"
            run.project = project.value
            issue = session.scalar(select(Issue).where(Issue.finding_id == finding.id))
            if issue is not None and issue.repository != issue_repository:
                raise ValueError("bound issue repository does not match finding classification")
            return {
                **payload,
                "status": "CONFIRMED_LOADED",
                "run_id": str(run.id),
                "change_set_id": str(change_set_id),
                "project": project.value,
                "risk_class": finding.risk_class,
                "finding_id": str(finding.id),
                "fingerprint": finding.fingerprint,
                "domain": finding.domain,
                "persona": finding.persona,
                "expected_behavior": finding.expected_behavior,
                "actual_behavior": finding.actual_behavior,
                "minimal_reproduction": finding.minimal_reproduction,
                "affected_component": finding.affected_component,
                "repository_surface": surface,
                "issue_repository": issue_repository,
                "source_repository": source_repository,
                "source_base_sha": finding.affected_source_sha,
                "source_base_ref": (
                    "refs/heads/master" if project is Project.LLMWIKI else "refs/heads/main"
                ),
                "deployment_repository": deployment_repository,
                "deployment_base_sha": finding.affected_deployment_sha,
                "deployment_base_ref": (
                    "refs/heads/master" if project is Project.LLMWIKI else "refs/heads/main"
                ),
                "issue_number": issue.number if issue else None,
                "issue_url": issue.url if issue else None,
            }

    def _ensure_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("dry_run") is not False:
            return {
                **payload,
                "status": "DRY_RUN",
                "dry_run_summary": (
                    "confirmed finding and ChangeSet loaded; GitHub issue creation, leases, "
                    "worktrees, and writable provider execution were not started"
                ),
            }
        if payload.get("issue_number") and payload.get("issue_url"):
            return {**payload, "status": "ISSUE_READY"}
        return {
            **payload,
            "status": "BLOCKED",
            "blocker": "EXPLICIT_FINDING_PUBLICATION_REQUIRED",
        }

    def _acquire_delivery_leases(self, payload: dict[str, Any]) -> dict[str, Any]:
        project = Project(str(payload["project"]))
        component = self._component_key(project, str(payload["affected_component"]))
        run_id = UUID(str(payload["run_id"]))
        manager = ComponentLeaseManager()
        with self.sessions.begin() as session:
            lease = manager.acquire(
                session,
                component_key=component,
                owner_run_id=run_id,
                mode="implementation",
                ttl=timedelta(hours=4),
            )
        return {
            **payload,
            "status": "LEASES_ACQUIRED",
            "component_key": component,
            "component_lease_id": str(lease.id),
        }

    def _route_delivery(self, payload: dict[str, Any]) -> dict[str, Any]:
        risk = RiskClass(str(payload["risk_class"]))
        broker = self._quota_broker()
        maker = broker.route(role="implementer", model_class=ModelClass.WORKER, risk_class=risk)
        spec = broker.route(
            role="product-spec-architect",
            model_class=ModelClass.JUDGE,
            risk_class=risk,
        )
        if maker.primary is None or maker.checker is None or spec.primary is None:
            return {
                **payload,
                "status": "RATE_LIMITED" if maker.status == "RATE_LIMITED" else "BLOCKED",
                "routing_reason": f"maker={maker.reason};spec={spec.reason}",
            }
        issue = str(payload.get("issue_number") or f"finding-{str(payload['fingerprint'])[:12]}")
        allocation = {
            "change_set_id": payload["change_set_id"],
            "project": payload["project"],
            "issue": issue,
            "source_repository": payload["source_repository"],
            "source_base_sha": payload["source_base_sha"],
            "source_base_ref": payload["source_base_ref"],
            "deployment_repository": payload["deployment_repository"],
            "deployment_base_sha": payload["deployment_base_sha"],
            "deployment_base_ref": payload["deployment_base_ref"],
            "maker_provider": maker.primary.value,
            "checker_provider": maker.checker.value,
        }
        return {
            **payload,
            "status": "ROUTED",
            "spec_provider": spec.primary.value,
            "maker_provider": maker.primary.value,
            "checker_provider": maker.checker.value,
            "routing_reason": f"maker={maker.reason};spec={spec.reason}",
            "allocation_request": allocation,
        }

    def _persist_worktree_leases(self, payload: dict[str, Any]) -> dict[str, Any]:
        change_set_id = UUID(str(payload["change_set_id"]))
        owner_run_id = UUID(str(payload["run_id"]))
        expected_root = PurePosixPath(self.settings.worktree_root.as_posix())
        records = (
            (
                str(payload["source_repository"]),
                str(payload["source_worktree"]),
                str(payload["source_branch"]),
                str(payload["source_base_sha"]),
            ),
            (
                str(payload["deployment_repository"]),
                str(payload["deployment_worktree"]),
                str(payload["deployment_branch"]),
                str(payload["deployment_base_sha"]),
            ),
        )
        expires_at = datetime.now(UTC) + timedelta(hours=8)
        lease_ids: list[str] = []
        with self.sessions.begin() as session:
            for repository, worktree_path, branch, base_sha in records:
                expected_path = expected_root / repository / str(change_set_id)
                if PurePosixPath(worktree_path) != expected_path:
                    raise ValueError("worktree lease path does not match its ChangeSet allocation")
                existing = session.scalar(
                    select(WorktreeLease).where(
                        WorktreeLease.change_set_id == change_set_id,
                        WorktreeLease.repository == repository,
                        WorktreeLease.released_at.is_(None),
                    )
                )
                if existing is not None:
                    if (
                        existing.owner_run_id != owner_run_id
                        or existing.worktree_path != worktree_path
                        or existing.branch != branch
                        or existing.base_sha != base_sha
                    ):
                        raise ValueError("active worktree lease conflicts with this delivery run")
                    lease_ids.append(str(existing.id))
                    continue
                identity = uuid5(NAMESPACE_URL, f"worktree-lease:{change_set_id}:{repository}")
                lease = WorktreeLease(
                    id=identity,
                    change_set_id=change_set_id,
                    repository=repository,
                    worktree_path=worktree_path,
                    branch=branch,
                    owner_run_id=owner_run_id,
                    base_sha=base_sha,
                    expires_at=expires_at,
                    released_at=None,
                )
                session.add(lease)
                lease_ids.append(str(identity))
        return {**payload, "status": "WORKTREE_LEASES_PERSISTED", "worktree_lease_ids": lease_ids}

    def _prepare_spec(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = Provider(str(payload["spec_provider"]))
        change_set_id = UUID(str(payload["change_set_id"]))
        workflow_id = self._workflow_id(payload)
        prompt = (
            "Produce a bounded provider-neutral OpenSpec for exactly one confirmed GitHub issue. "
            "Do not modify files and do not propose deployment execution. The source and "
            "deployment repositories belong to one ChangeSet.\n\n"
            f"Issue: {payload['issue_url']}\nFingerprint: {payload['fingerprint']}\n"
            f"Expected: {payload['expected_behavior']}\nActual: {payload['actual_behavior']}\n"
            f"Minimal reproduction: {json.dumps(payload['minimal_reproduction'])}\n"
            f"Risk class: {payload['risk_class']}\n"
            f"Source SHA: {payload['source_base_sha']}\n"
            f"Deployment SHA: {payload['deployment_base_sha']}\n"
            f"Deployment worktree: {payload['deployment_worktree']}\n"
        )
        request = AgentRequest(
            run_id=uuid5(NAMESPACE_URL, f"delivery-spec:{workflow_id}:{change_set_id}"),
            project=Project(str(payload["project"])),
            role="product-spec-architect",
            provider=provider,
            model_class=ModelClass.JUDGE,
            prompt=prompt,
            working_directory=str(payload["source_worktree"]),
            output_schema=self._spec_output_schema(),
            timeout_seconds=3600,
            effort="high",
            writable=False,
            change_set_id=change_set_id,
        )
        return {
            **payload,
            "status": "SPEC_READY",
            "spec_request": request.model_dump(mode="json"),
        }

    def _create_openspec(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._object(payload.get("agent_result"), "agent_result")
        structured = self._object(result.get("structured_result"), "structured_result")
        if structured.get("decision") == "BLOCKED":
            return {**payload, "status": "BLOCKED", "blocker": "SPEC_ARCHITECT_BLOCKED"}
        if structured.get("decision") != "READY":
            raise ValueError("spec architect returned an unknown decision")
        change_set_id = UUID(str(payload["change_set_id"]))
        identifier = f"engineering/{change_set_id}"
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, change_set_id)
            if change_set is None or change_set.issue_id is None:
                raise ValueError("OpenSpec requires a ChangeSet with a GitHub issue")
            issue = session.get(Issue, change_set.issue_id)
            if issue is None:
                raise ValueError("OpenSpec issue metadata is missing")
            issue.openspec_change = identifier
            change_set.state = "PLANNED"
            change_set.version += 1
        return {
            **payload,
            "status": "OPENSPEC_READY",
            "spec_agent_result": result,
            "openspec_identifier": identifier,
            "openspec_document": structured,
        }

    def _prepare_maker(self, payload: dict[str, Any], *, deployment: bool) -> dict[str, Any]:
        provider = Provider(str(payload["maker_provider"]))
        change_set_id = UUID(str(payload["change_set_id"]))
        workflow_id = self._workflow_id(payload)
        target = "deployment" if deployment else "source"
        worktree_key = "deployment_worktree" if deployment else "source_worktree"
        request_key = "deployment_maker_request" if deployment else "maker_request"
        prior = payload.get("agent_result") if deployment else None
        if deployment:
            prior_result = self._object(prior, "source maker result")
            prior_structured = self._object(
                prior_result.get("structured_result"), "source maker structured result"
            )
            if prior_structured.get("decision") != "COMPLETE":
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "SOURCE_MAKER_DID_NOT_COMPLETE",
                }
        prompt = (
            f"Implement only the {target} repository portion of one bounded ChangeSet. "
            "Do not deploy, merge, push, read credentials, or alter unrelated files. Run bounded "
            "repository tests, then create one local commit on the assigned branch. Preserve all "
            "existing safety gates.\n\n"
            f"Issue: {payload['issue_url']}\nRisk: {payload['risk_class']}\n"
            f"OpenSpec: {json.dumps(payload['openspec_document'], sort_keys=True)}\n"
            f"Source worktree: {payload['source_worktree']}\n"
            f"Deployment worktree: {payload['deployment_worktree']}\n"
            f"Prior source maker result: {json.dumps(prior, sort_keys=True, default=str)}\n"
        )
        request = AgentRequest(
            run_id=uuid5(NAMESPACE_URL, f"delivery-{target}-maker:{workflow_id}:{change_set_id}"),
            project=Project(str(payload["project"])),
            role="implementer",
            provider=provider,
            model_class=ModelClass.WORKER,
            prompt=prompt,
            working_directory=str(payload[worktree_key]),
            output_schema=self._maker_output_schema(),
            timeout_seconds=7200,
            effort="high",
            writable=True,
            change_set_id=change_set_id,
        )
        response: dict[str, Any] = {
            **payload,
            "status": f"{target.upper()}_MAKER_READY",
            request_key: request.model_dump(mode="json"),
        }
        if deployment:
            response["source_maker_result"] = prior
        return response

    def _prepare_checker(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = Provider(str(payload["checker_provider"]))
        change_set_id = UUID(str(payload["change_set_id"]))
        workflow_id = self._workflow_id(payload)
        source_result = self._object(payload.get("agent_result"), "agent_result")
        source_maker = self._object(payload.get("source_maker_result"), "source_maker_result")
        for label, result in (
            ("source", source_maker),
            ("deployment", source_result),
        ):
            structured = self._object(result.get("structured_result"), f"{label}_maker_result")
            if structured.get("decision") != "COMPLETE":
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": f"{label.upper()}_MAKER_DID_NOT_COMPLETE",
                }
        prompt = (
            "Independently review both bounded worktrees for one ChangeSet. Do not modify files. "
            "Validate the OpenSpec, source and deployment diffs, tests, immutable image policy, "
            "and risk gates. Request changes on any uncertainty.\n\n"
            f"Issue: {payload['issue_url']}\nRisk: {payload['risk_class']}\n"
            f"OpenSpec: {json.dumps(payload['openspec_document'], sort_keys=True)}\n"
            f"Source worktree: {payload['source_worktree']}\n"
            f"Deployment worktree: {payload['deployment_worktree']}\n"
            f"Latest maker result: {json.dumps(source_result, sort_keys=True, default=str)}\n"
        )
        request = AgentRequest(
            run_id=uuid5(NAMESPACE_URL, f"delivery-checker:{workflow_id}:{change_set_id}"),
            project=Project(str(payload["project"])),
            role="code-reviewer",
            provider=provider,
            model_class=ModelClass.JUDGE,
            prompt=prompt,
            working_directory=str(payload["source_worktree"]),
            output_schema=self._checker_output_schema(),
            timeout_seconds=5400,
            effort="high",
            writable=False,
            change_set_id=change_set_id,
        )
        return {
            **payload,
            "status": "CHECKER_READY",
            "deployment_maker_result": source_result,
            "checker_request": request.model_dump(mode="json"),
        }

    def _prepare_publication(self, payload: dict[str, Any]) -> dict[str, Any]:
        evidence = self._object(payload.get("change_evidence"), "change_evidence")
        source = self._object(evidence.get("source"), "source change evidence")
        deployment = self._object(evidence.get("deployment"), "deployment change evidence")
        change_set_id = UUID(str(payload["change_set_id"]))
        source_head = str(source.get("head_sha", ""))
        deployment_head = str(deployment.get("head_sha", ""))
        if not self._is_git_sha(source_head) or not self._is_git_sha(deployment_head):
            raise ValueError("publication requires exact committed repository heads")
        if source_head == payload.get("source_base_sha"):
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "SOURCE_CHANGES_NOT_COMMITTED",
            }
        if int(deployment.get("changed_file_count", 0)) > 0 and deployment_head == payload.get(
            "deployment_base_sha"
        ):
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "DEPLOYMENT_CHANGES_NOT_COMMITTED",
            }
        source_repository = str(payload["source_repository"])
        deployment_repository = str(payload["deployment_repository"])
        repositories = {
            "falcone": "gntik-ai/falcone",
            "falcone-charts": "gntik-ai/falcone-charts",
            "musematic": "gntik-ai/musematic",
            "musematic-deploy": "gntik-ai/musematic-deploy",
            "llmwiki": "gntik-ai/llmwiki",
        }
        if source_repository not in repositories or deployment_repository not in repositories:
            raise ValueError("publication repository is not configured")
        issue = str(payload["issue_url"])
        body = (
            "## Engineering Control ChangeSet\n\n"
            f"Issue: {issue}\n\n"
            f"ChangeSet: `{change_set_id}`\n\n"
            f"OpenSpec: `{payload['openspec_identifier']}`\n\n"
            f"Risk: `{payload['risk_class']}`\n\n"
            "This draft was implemented in one bounded worktree and independently reviewed "
            "by the opposite provider. Merge remains governed by repository protection.\n"
        )
        title = f"changeset {str(change_set_id)[:8]}: {payload['domain']} finding"
        publications = [
            {
                "repository": repositories[source_repository],
                "repository_slug": source_repository,
                "working_directory": str(payload["source_worktree"]),
                "branch": str(payload["source_branch"]),
                "head_sha": source_head,
                "base_branch": "master" if source_repository == "llmwiki" else "main",
                "title": title,
                "body": body + "\nRepository role: source\n",
            }
        ]
        deferred_deployment: dict[str, Any] | None = None
        if (
            deployment_repository != source_repository
            and int(deployment.get("changed_file_count", 0)) > 0
        ):
            publications.append(
                {
                    "repository": repositories[deployment_repository],
                    "repository_slug": deployment_repository,
                    "working_directory": str(payload["deployment_worktree"]),
                    "branch": str(payload["deployment_branch"]),
                    "head_sha": deployment_head,
                    "base_branch": "main",
                    "title": title + " (deployment)",
                    "body": body + "\nRepository role: deployment\n",
                }
            )
        elif deployment_repository != source_repository:
            deferred_deployment = {
                "repository_slug": deployment_repository,
                "branch": str(payload["deployment_branch"]),
                "head_sha": deployment_head,
                "base_sha": str(payload["deployment_base_sha"]),
                "reason": "AWAITING_IMMUTABLE_ARTIFACT",
            }
        return {
            **payload,
            "status": "PUBLICATION_READY",
            "publication_plan": {
                "change_set_id": str(change_set_id),
                "publications": publications,
                "deferred_deployment": deferred_deployment,
            },
        }

    def _persist_publication(self, payload: dict[str, Any]) -> dict[str, Any]:
        change_set_id = UUID(str(payload["change_set_id"]))
        run_id = UUID(str(payload["run_id"]))
        status = str(payload.get("status", ""))
        plan = self._object(payload.get("publication_plan"), "publication_plan")
        raw_publications = plan.get("publications")
        if not isinstance(raw_publications, list) or len(raw_publications) not in {1, 2}:
            raise ValueError("publication plan metadata is incomplete")
        planned = {
            str(item.get("repository_slug")): item
            for item in raw_publications
            if isinstance(item, dict)
        }
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, change_set_id)
            run = session.get(Run, run_id)
            if change_set is None or run is None or run.change_set_id != change_set_id:
                raise ValueError("publication metadata binding is missing")
            source_plan = self._object(
                planned.get(change_set.source_repository), "source publication plan"
            )
            raw_deployment_plan = planned.get(change_set.deployment_repository)
            deferred = plan.get("deferred_deployment")
            deployment_plan = (
                self._object(raw_deployment_plan, "deployment publication plan")
                if raw_deployment_plan is not None
                else self._object(deferred, "deferred deployment plan")
            )
            if deployment_plan.get("repository_slug") != change_set.deployment_repository:
                raise ValueError("publication deployment binding is invalid")
            for record in (source_plan, deployment_plan):
                if not self._is_git_sha(record.get("head_sha")):
                    raise ValueError("publication plan head is invalid")
            change_set.source_branch = str(source_plan["branch"])
            change_set.source_head_sha = str(source_plan["head_sha"])
            change_set.deployment_branch = str(deployment_plan["branch"])
            change_set.deployment_head_sha = str(deployment_plan["head_sha"])
            if status != "SOURCE_PRS_CREATED":
                blocker = str(payload.get("blocker", "GITHUB_PUBLICATION_FAILED"))
                change_set.state = ChangeSetState.IMPLEMENTING.value
                change_set.version += 1
                run.status = "BLOCKED"
                run.error_code = blocker
                run.summary = "validated local commits preserved pending scoped publication"
                run.finished_at = datetime.now(UTC)
                self._release_implementation_leases(session, run)
                return {**payload, "status": "BLOCKED", "blocker": blocker}
            raw_results = payload.get("publication_results")
            if not isinstance(raw_results, list) or len(raw_results) != len(raw_publications):
                raise ValueError("publication result count changed after planning")
            results = {
                str(item.get("repository_slug")): item
                for item in raw_results
                if isinstance(item, dict)
            }
            source = self._object(results.get(change_set.source_repository), "source PR result")
            raw_deployment = results.get(change_set.deployment_repository)
            deployment = (
                self._object(raw_deployment, "deployment PR result")
                if raw_deployment is not None
                else None
            )
            expected_results: list[tuple[dict[str, Any], str]] = [
                (source, str(change_set.source_head_sha or ""))
            ]
            if deployment is not None:
                expected_results.append((deployment, str(change_set.deployment_head_sha or "")))
            for record, expected_sha in expected_results:
                if record.get("head_sha") != expected_sha or record.get("pr_state") != "OPEN":
                    raise ValueError("published PR is not open at the validated head")
                if not re.fullmatch(
                    r"https://github\.com/gntik-ai/[a-z0-9-]+/pull/[1-9][0-9]*",
                    str(record.get("pr_url", "")),
                ):
                    raise ValueError("published PR URL is invalid")
            change_set.source_pr_url = str(source["pr_url"])
            change_set.deployment_pr_url = (
                str(deployment["pr_url"]) if deployment is not None else None
            )
            checker = payload.get("agent_result")
            if not isinstance(checker, dict) or not isinstance(
                checker.get("structured_result"), dict
            ):
                raise ValueError("publication has no persisted independent checker result")
            structured = checker["structured_result"]
            if structured.get("decision") != "PASS":
                raise ValueError("publication independent checker did not pass")
            evidence = self._object(payload.get("change_evidence"), "change_evidence")
            release_impacts: dict[str, dict[str, Any]] = {}
            release_change_evidence: dict[str, dict[str, Any]] = {}
            risk_order = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}
            risk_floor = change_set.risk_class
            for repository_role in ("source", "deployment"):
                repository_evidence = self._object(
                    evidence.get(repository_role), f"{repository_role} change evidence"
                )
                impact = self._validated_release_impact(
                    repository_evidence.get("release_impact"), repository_role
                )
                expected_repository = (
                    change_set.source_repository
                    if repository_role == "source"
                    else change_set.deployment_repository
                )
                expected_base_sha = (
                    change_set.source_base_sha
                    if repository_role == "source"
                    else change_set.deployment_base_sha
                )
                expected_head_sha = (
                    change_set.source_head_sha
                    if repository_role == "source"
                    else change_set.deployment_head_sha
                )
                changed_paths = repository_evidence.get("changed_paths")
                if not isinstance(changed_paths, list) or any(
                    not isinstance(path, str) for path in changed_paths
                ):
                    raise ValueError(f"{repository_role} changed paths are invalid")
                changed_paths_sha = hashlib.sha256(
                    "\n".join(path.lower() for path in changed_paths).encode()
                ).hexdigest()
                expected_floor = (
                    "R3"
                    if impact["categories"]
                    else "R2"
                    if repository_role == "deployment" and changed_paths
                    else "R1"
                )
                if (
                    repository_evidence.get("base_sha") != expected_base_sha
                    or repository_evidence.get("head_sha") != expected_head_sha
                    or repository_evidence.get("changed_file_count") != len(changed_paths)
                    or impact["repository"] != expected_repository
                    or impact["diff_sha256"] != repository_evidence.get("diff_sha256")
                    or impact["changed_paths_sha256"] != changed_paths_sha
                    or impact["risk_floor"] != expected_floor
                ):
                    raise ValueError(f"{repository_role} release impact is not diff-bound")
                release_impacts[repository_role] = impact
                if risk_order[impact["risk_floor"]] > risk_order[risk_floor]:
                    risk_floor = impact["risk_floor"]
                release_change_evidence[repository_role] = {
                    key: repository_evidence.get(key)
                    for key in (
                        "base_sha",
                        "head_sha",
                        "changed_file_count",
                        "changed_paths",
                        "status_sha256",
                        "diff_sha256",
                        "diff_redacted_sha256",
                    )
                }
                release_change_evidence[repository_role]["release_impact"] = impact
            change_set.risk_class = risk_floor
            recovery = dict(change_set.recovery_json)
            recovery["provider_chain"] = {
                "maker": str(payload.get("maker_provider", "")),
                "checker": str(payload.get("checker_provider", "")),
            }
            recovery["independent_review"] = {
                "decision": "PASS",
                "provider": str(payload.get("checker_provider", "")),
                "sha256": hashlib.sha256(
                    json.dumps(
                        structured,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode()
                ).hexdigest(),
            }
            recovery["change_evidence"] = release_change_evidence
            recovery["release_impact"] = {
                "risk_floor": risk_floor,
                "migration": any(impact["migration"] for impact in release_impacts.values()),
                "categories": sorted(
                    {
                        category
                        for impact in release_impacts.values()
                        for category in impact["categories"]
                    }
                ),
                "classifier_sha256": hashlib.sha256(
                    json.dumps(release_impacts, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
            change_set.recovery_json = recovery
            change_set.state = ChangeSetState.SOURCE_PR.value
            change_set.version += 1
            run.status = "SUCCESS"
            run.summary = (
                "source and deployment draft PRs published at validated commits"
                if deployment is not None
                else "source draft PR published; deployment waits for immutable artifact"
            )
            run.finished_at = datetime.now(UTC)
            self._release_implementation_leases(session, run)
        return {
            **payload,
            "status": "SOURCE_PR",
            "source_pr": source["pr_url"],
            "deployment_pr": deployment["pr_url"] if deployment is not None else None,
            "deployment_deferred": deployment is None,
        }

    def _prepare_final_closure(self, payload: dict[str, Any]) -> dict[str, Any]:
        change_set_id = UUID(str(payload["change_set_id"]))
        campaign_id = UUID(str(payload["backlog_campaign_id"]))
        issue_repository = str(payload["issue_repository"])
        issue_number = int(payload["issue_number"])
        project = Project(str(payload["project"]))
        workflow_id = self._workflow_id(payload)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            campaign = session.get(GitHubBacklogCampaign, campaign_id)
            issue = session.scalar(
                select(BacklogIssueRecord).where(
                    BacklogIssueRecord.backlog_campaign_id == campaign_id,
                    BacklogIssueRecord.repository == issue_repository,
                    BacklogIssueRecord.issue_number == issue_number,
                )
            )
            change_set = session.get(ChangeSet, change_set_id)
            if (
                campaign is None
                or issue is None
                or change_set is None
                or campaign.project != project.value
                or issue.change_set_id != change_set.id
                or change_set.project != project.value
            ):
                raise ValueError("final closure campaign, issue, or ChangeSet binding is missing")
            if (
                campaign.delivery_mode != "full-delivery"
                or campaign.status not in {"APPROVED_INVENTORY", "APPROVED", "RUNNING"}
                or not campaign.approval_binding
                or not isinstance(campaign.approval_json, dict)
            ):
                raise ValueError("final closure requires a current full-delivery approval")
            approval_value = campaign.approval_json
            raw_binding = approval_value.get("binding")
            binding_value = raw_binding if isinstance(raw_binding, dict) else approval_value
            raw_approval = approval_value.get("approval")
            approval_record = raw_approval if isinstance(raw_approval, dict) else approval_value
            approval = BacklogApprovalBinding.model_validate(
                {field: binding_value.get(field) for field in BacklogApprovalBinding.model_fields}
            )
            if (
                approval.sha256() != campaign.approval_binding
                or approval.backlog_campaign_id != str(campaign.id)
                or approval.project != project.value
                or approval.delivery_mode != "full-delivery"
                or approval.merge_policy != "protected-explicit"
                or approval.issue_close_policy != "verified-final-runtime"
                or "protected-pr-merge" not in approval.allowed_github_operations
                or "issue-close-after-verification" not in approval.allowed_github_operations
                or approval_record.get("typed_confirmation") != BACKLOG_APPROVAL_PHRASE
                or not approval_record.get("approved_by")
                or _as_utc(approval.expires_at) <= now
            ):
                raise ValueError("final closure approval is absent, expired, or changed")
            if (
                change_set.state == ChangeSetState.ISSUE_CLOSED.value
                and issue.state == "CLOSED"
                and issue.final_closure_status == "FIXED_CLOSURE_COMPLETE"
            ):
                return {
                    **payload,
                    "status": "FIXED",
                    "change_set_id": str(change_set.id),
                }
            closure_states = {
                ChangeSetState.RUNTIME_VERIFIED.value,
                ChangeSetState.SOURCE_MERGED.value,
                ChangeSetState.FINAL_ARTIFACT_BUILT.value,
                ChangeSetState.DEPLOY_PR.value,
                ChangeSetState.AWAITING_STAGING_APPROVAL.value,
                ChangeSetState.STAGING_APPROVED.value,
                ChangeSetState.STAGING_DEPLOYED.value,
            }
            if change_set.state not in closure_states:
                raise ValueError("final closure is not in a resumable verified state")
            if (
                issue.state != "OPEN"
                or issue.documentation_impact
                == DocumentationImpact.BLOCKED_BY_UNRESOLVED_DOCUMENTATION_IMPACT.value
            ):
                raise ValueError("issue is not eligible for final closure")
            if issue.calculated_risk == RiskClass.R3.value:
                r3_candidates = session.scalars(
                    select(ActionCandidate)
                    .where(
                        ActionCandidate.action == "r3_issue_approval",
                        ActionCandidate.target_id == f"{issue.repository}#{issue.issue_number}",
                        ActionCandidate.used_at.is_not(None),
                        ActionCandidate.expires_at > now,
                    )
                    .order_by(ActionCandidate.created_at.desc())
                    .limit(10)
                ).all()
                if not any(
                    candidate.payload_json.get("issue_body_sha256") == issue.body_sha256
                    and candidate.payload_json.get("issue_updated_at")
                    == issue.github_updated_at.isoformat()
                    and candidate.payload_json.get("production") is False
                    for candidate in r3_candidates
                ):
                    raise ValueError("final closure requires a current exact R3 issue approval")
            recovery = dict(change_set.recovery_json)
            existing_closure_value = recovery.get("final_closure")
            existing_closure = (
                dict(existing_closure_value) if isinstance(existing_closure_value, dict) else {}
            )
            chain = self._object(recovery.get("provider_chain"), "provider chain")
            release_evidence = self._object(
                recovery.get("release_evidence", {}), "release evidence"
            )
            independent = existing_closure.get(
                "independent_review_sha256",
                release_evidence.get("independent_review_sha256"),
            )
            if not self._is_digest(independent):
                raise ValueError("final closure independent review evidence is missing")
            maker = str(chain.get("maker", ""))
            checker = str(chain.get("checker", ""))
            if maker not in {"codex", "claude"} or checker not in {"codex", "claude"}:
                raise ValueError("final closure provider chain is incomplete")
            run_id = uuid5(NAMESPACE_URL, f"final-closure:{workflow_id}:{change_set.id}")
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if run is None:
                run = Run(
                    id=run_id,
                    campaign_id=None,
                    change_set_id=change_set.id,
                    workflow_id=workflow_id,
                    run_type="final-closure",
                    status="RUNNING",
                    project=project.value,
                    worker="controller",
                    started_at=now,
                    finished_at=None,
                    error_code=None,
                    summary="Exact protected-merge final closure is running",
                )
                session.add(run)
            elif (
                run.id != run_id
                or run.run_type != "final-closure"
                or run.change_set_id != change_set.id
            ):
                raise ValueError("final closure workflow idempotency binding changed")
            policy = self._delivery_control_policy(project)
            stored_plan = existing_closure.get("plan")
            if stored_plan is not None:
                plan = FinalClosurePlan.model_validate(stored_plan)
                if (
                    plan.change_set_id != change_set.id
                    or plan.run_id != run.id
                    or plan.workflow_id != workflow_id
                    or plan.backlog_campaign_id != campaign.id
                    or plan.approval_binding_sha256 != campaign.approval_binding
                    or plan.project != project
                    or plan.issue_repository != issue.repository
                    or plan.issue_number != issue.issue_number
                    or plan.maker_provider != maker
                    or plan.checker_provider != checker
                    or plan.documentation_impact.value != issue.documentation_impact
                    or plan.policy != policy
                ):
                    raise ValueError("persisted final closure plan binding changed")
            else:
                if change_set.state != ChangeSetState.RUNTIME_VERIFIED.value:
                    raise ValueError("final closure plan is missing after irreversible progress")
                source = self._final_pr_binding(
                    repository=f"gntik-ai/{change_set.source_repository}",
                    url=change_set.source_pr_url,
                    branch=change_set.source_branch,
                    head_sha=change_set.source_head_sha,
                    base_branch=approval.source_branch,
                )
                deployment = self._final_pr_binding(
                    repository=f"gntik-ai/{change_set.deployment_repository}",
                    url=change_set.deployment_pr_url,
                    branch=change_set.deployment_branch,
                    head_sha=change_set.deployment_head_sha,
                    base_branch=approval.deployment_branch,
                )
                plan = FinalClosurePlan(
                    change_set_id=change_set.id,
                    run_id=run.id,
                    workflow_id=workflow_id,
                    backlog_campaign_id=campaign.id,
                    approval_binding_sha256=campaign.approval_binding,
                    project=project,
                    issue_repository=issue.repository,
                    issue_number=issue.issue_number,
                    source=source,
                    deployment=deployment,
                    maker_provider=cast(Literal["codex", "claude"], maker),
                    checker_provider=cast(Literal["codex", "claude"], checker),
                    documentation_impact=DocumentationImpact(issue.documentation_impact),
                    policy=policy,
                    production=False,
                )
                existing_closure["plan"] = plan.model_dump(mode="json")
                existing_closure["independent_review_sha256"] = independent
                recovery["final_closure"] = existing_closure
                change_set.recovery_json = recovery
            run.status = "RUNNING"
            run.error_code = None
            run.finished_at = None
            stored_issue_plan = existing_closure.get("final_issue_plan")
            if stored_issue_plan is not None:
                issue_plan = FinalIssueMutationPlan.model_validate(stored_issue_plan)
                if issue_plan.closure != plan or issue.final_closure_status not in {
                    "FINAL_EVIDENCE_READY",
                    "FINAL_EVIDENCE_PERSISTED",
                }:
                    raise ValueError("persisted final issue plan binding changed")
                return {
                    **payload,
                    "status": "FINAL_ISSUE_CLOSE_READY",
                    "final_closure_plan": plan.model_dump(mode="json"),
                    "final_issue_plan": issue_plan.model_dump(mode="json"),
                    "independent_review_sha256": independent,
                }
            issue.final_closure_status = "FINAL_CLOSURE_RUNNING"
        return {
            **payload,
            "status": "FINAL_CLOSURE_READY",
            "final_closure_plan": plan.model_dump(mode="json"),
            "independent_review_sha256": independent,
        }

    def _persist_final_source_merge(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = FinalClosurePlan.model_validate(payload.get("final_closure_plan"))
        merge = ProtectedMergeResult.model_validate(payload.get("source_merge"))
        if (
            payload.get("status") != "SOURCE_MERGED"
            or merge.repository != plan.source.repository
            or merge.pull_request_number != plan.source.number
            or merge.head_sha != plan.source.head_sha
        ):
            raise ValueError("final source merge is not bound to its protected PR")
        with self.sessions.begin() as session:
            run = session.get(Run, plan.run_id)
            change_set = session.get(ChangeSet, plan.change_set_id)
            if run is None or change_set is None or run.change_set_id != change_set.id:
                raise ValueError("final source merge persistence binding is missing")
            recovery = dict(change_set.recovery_json)
            existing = recovery.get("final_closure")
            closure = dict(existing) if isinstance(existing, dict) else {}
            prior = closure.get("source_merge")
            value = merge.model_dump(mode="json")
            if prior is not None and prior != value:
                raise ValueError("final source merge evidence changed")
            already_persisted = prior == value and change_set.source_head_sha == merge.merge_sha
            if not already_persisted:
                if (
                    change_set.state != ChangeSetState.RUNTIME_VERIFIED.value
                    or change_set.source_head_sha != plan.source.head_sha
                ):
                    raise ValueError("final source merge state changed before persistence")
                closure["source_merge"] = value
                recovery["final_closure"] = closure
                change_set.recovery_json = recovery
                change_set.source_head_sha = merge.merge_sha
                change_set.state = ChangeSetState.SOURCE_MERGED.value
                change_set.version += 1
                run.status = "RUNNING"
                run.summary = "Protected source PR merged; exact final build is required"
            artifact = FinalArtifactPlan(
                closure=plan,
                final_source_sha=merge.merge_sha,
                policy=plan.policy,
            )
        return {
            **payload,
            "status": "AWAITING_FINAL_BUILD",
            "final_artifact_plan": artifact.model_dump(mode="json"),
        }

    def _persist_final_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = FinalArtifactPlan.model_validate(payload.get("final_artifact_plan"))
        manifest = CandidateArtifactManifest.model_validate(payload.get("final_artifact_manifest"))
        evidence = self._object(payload.get("final_artifact_evidence"), "final artifact evidence")
        if (
            payload.get("status") != "FINAL_ARTIFACT_VERIFIED"
            or manifest.source_sha != plan.final_source_sha
            or manifest.project is not plan.closure.project
            or manifest.source_repository != plan.closure.source.repository
            or manifest.workflow_run_id != payload.get("final_build_run_id")
        ):
            raise ValueError("final artifact is not bound to the exact source merge")
        expected_repositories = plan.policy.immutable_promotion.image_repositories
        repositories = {image.component: image.repository for image in manifest.images}
        digests = {image.component: image.digest for image in manifest.images}
        attestations = evidence.get("attestation_sha256")
        if (
            repositories != expected_repositories
            or not self._is_digest(evidence.get("manifest_sha256"))
            or not isinstance(attestations, dict)
            or set(attestations) != set(digests)
            or any(not self._is_digest(value) for value in attestations.values())
        ):
            raise ValueError("final artifact provenance evidence is incomplete")
        with self.sessions.begin() as session:
            run = session.get(Run, plan.closure.run_id)
            change_set = session.get(ChangeSet, plan.closure.change_set_id)
            if (
                run is None
                or change_set is None
                or change_set.source_head_sha != plan.final_source_sha
                or change_set.state
                not in {
                    ChangeSetState.SOURCE_MERGED.value,
                    ChangeSetState.FINAL_ARTIFACT_BUILT.value,
                    ChangeSetState.DEPLOY_PR.value,
                    ChangeSetState.AWAITING_STAGING_APPROVAL.value,
                    ChangeSetState.STAGING_APPROVED.value,
                    ChangeSetState.STAGING_DEPLOYED.value,
                    ChangeSetState.RUNTIME_VERIFIED.value,
                }
            ):
                raise ValueError("final artifact persistence binding is missing")
            recovery = dict(change_set.recovery_json)
            closure = dict(self._object(recovery.get("final_closure"), "final closure"))
            final_artifact = {
                "workflow_run_id": manifest.workflow_run_id,
                "source_sha": manifest.source_sha,
                "manifest_sha256": evidence["manifest_sha256"],
                "attestation_sha256": attestations,
                "image_digests": digests,
            }
            prior = closure.get("final_artifact")
            if prior is not None and prior != final_artifact:
                raise ValueError("final artifact evidence changed")
            already_persisted = (
                prior == final_artifact
                and change_set.image_digests == digests
                and change_set.state
                in {
                    ChangeSetState.FINAL_ARTIFACT_BUILT.value,
                    ChangeSetState.DEPLOY_PR.value,
                    ChangeSetState.AWAITING_STAGING_APPROVAL.value,
                    ChangeSetState.STAGING_APPROVED.value,
                    ChangeSetState.STAGING_DEPLOYED.value,
                    ChangeSetState.RUNTIME_VERIFIED.value,
                }
            )
            if not already_persisted:
                if change_set.state != ChangeSetState.SOURCE_MERGED.value:
                    raise ValueError("final artifact state changed before persistence")
                closure["final_artifact"] = final_artifact
                recovery["final_closure"] = closure
                change_set.recovery_json = recovery
                change_set.image_digests = digests
                change_set.state = ChangeSetState.FINAL_ARTIFACT_BUILT.value
                change_set.version += 1
                run.summary = "Signed immutable artifacts verified at exact source merge"
            reconciliation = CIReconciliationPlan(
                change_set_id=change_set.id,
                run_id=run.id,
                workflow_id=plan.closure.workflow_id,
                project=plan.closure.project,
                repository=plan.closure.source.repository,
                source_pr_number=plan.closure.source.number,
                source_pr_url=plan.closure.source.url,
                source_branch=plan.closure.source.head_branch,
                source_head_sha=plan.final_source_sha,
                source_base_branch=plan.closure.source.base_branch,
                candidate_build_run_id=manifest.workflow_run_id,
                policy=plan.policy,
                independent_review_sha256=str(payload["independent_review_sha256"]),
                independent_review_provider=plan.closure.checker_provider,
                maker_provider=plan.closure.maker_provider,
            )
        return {
            **payload,
            "status": "FINAL_ARTIFACT_BUILT",
            "reconciliation_plan": reconciliation.model_dump(mode="json"),
        }

    def _persist_final_deployment_merge(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = FinalClosurePlan.model_validate(payload.get("final_closure_plan"))
        deployment_plan = FinalDeploymentMergePlan.model_validate(
            payload.get("final_deployment_merge_plan")
        )
        merge = ProtectedMergeResult.model_validate(payload.get("deployment_merge"))
        if (
            payload.get("status") != "DEPLOYMENT_MERGED"
            or deployment_plan.closure != plan
            or merge.repository != deployment_plan.deployment.repository
            or merge.pull_request_number != deployment_plan.deployment.number
            or merge.head_sha != deployment_plan.deployment.head_sha
        ):
            raise ValueError("final deployment merge is not bound to its reviewed PR head")
        with self.sessions.begin() as session:
            run = session.get(Run, plan.run_id)
            change_set = session.get(ChangeSet, plan.change_set_id)
            if (
                run is None
                or change_set is None
                or change_set.deployment_head_sha not in {merge.head_sha, merge.merge_sha}
                or change_set.state
                not in {
                    ChangeSetState.DEPLOY_PR.value,
                    ChangeSetState.AWAITING_STAGING_APPROVAL.value,
                    ChangeSetState.STAGING_APPROVED.value,
                    ChangeSetState.STAGING_DEPLOYED.value,
                    ChangeSetState.RUNTIME_VERIFIED.value,
                }
            ):
                raise ValueError("final deployment merge persistence binding is missing")
            recovery = dict(change_set.recovery_json)
            closure = dict(self._object(recovery.get("final_closure"), "final closure"))
            value = merge.model_dump(mode="json")
            prior = closure.get("deployment_merge")
            if prior is not None and prior != value:
                raise ValueError("final deployment merge evidence changed")
            already_persisted = prior == value and change_set.deployment_head_sha == merge.merge_sha
            if not already_persisted:
                if change_set.deployment_head_sha != merge.head_sha:
                    raise ValueError("deployment PR head changed before merge persistence")
                closure["deployment_merge"] = value
                recovery["final_closure"] = closure
                change_set.recovery_json = recovery
                change_set.deployment_head_sha = merge.merge_sha
                change_set.version += 1
                run.summary = "Protected deployment PR merged; final staging verification required"
        return {**payload, "status": "FINAL_DEPLOYMENT_MERGE_PERSISTED"}

    def _prepare_final_issue_close(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = FinalClosurePlan.model_validate(payload.get("final_closure_plan"))
        source = ProtectedMergeResult.model_validate(payload.get("source_merge"))
        deployment_merge = ProtectedMergeResult.model_validate(payload.get("deployment_merge"))
        deployment_id = UUID(str(payload["deployment_id"]))
        raw_synchronization = payload.get("final_worktree_sync")
        if payload.get("status") != "FINAL_WORKTREES_SYNCHRONIZED" or not isinstance(
            raw_synchronization, list
        ):
            raise ValueError("final clean-worktree evidence is missing")
        synchronization = {
            str(item.get("repository")): item
            for item in raw_synchronization
            if isinstance(item, dict)
        }
        expected_synchronization = {
            source.repository: (source.merge_sha, source.merge_tree_sha),
            deployment_merge.repository: (
                deployment_merge.merge_sha,
                deployment_merge.merge_tree_sha,
            ),
        }
        if len(synchronization) != len(expected_synchronization) or any(
            not isinstance(item := synchronization.get(repository), dict)
            or item.get("merge_sha") != merge_sha
            or item.get("merge_tree_sha") != tree_sha
            or item.get("clean") is not True
            for repository, (merge_sha, tree_sha) in expected_synchronization.items()
        ):
            raise ValueError("final clean-worktree evidence changed")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            run = session.get(Run, plan.run_id)
            change_set = session.get(ChangeSet, plan.change_set_id)
            deployment = session.get(Deployment, deployment_id)
            issue = session.scalar(
                select(BacklogIssueRecord).where(
                    BacklogIssueRecord.backlog_campaign_id == plan.backlog_campaign_id,
                    BacklogIssueRecord.repository == plan.issue_repository,
                    BacklogIssueRecord.issue_number == plan.issue_number,
                )
            )
            verification = session.scalar(
                select(VerificationResult)
                .where(
                    VerificationResult.deployment_id == deployment_id,
                    VerificationResult.verification_type == "post-staging-runtime",
                    VerificationResult.status == "RUNTIME_VERIFIED",
                )
                .order_by(VerificationResult.created_at.desc())
            )
            active_agents = (
                int(
                    session.scalar(
                        select(func.count(AgentExecution.id)).where(
                            AgentExecution.change_set_id == change_set.id,
                            AgentExecution.ended_at.is_(None),
                        )
                    )
                    or 0
                )
                if change_set is not None
                else 0
            )
            worktree_leases = (
                session.scalars(
                    select(WorktreeLease).where(
                        WorktreeLease.change_set_id == change_set.id,
                    )
                ).all()
                if change_set is not None
                else []
            )
            if (
                run is None
                or change_set is None
                or deployment is None
                or issue is None
                or verification is None
                or deployment.change_set_id != change_set.id
                or deployment.desired_sha != deployment_merge.merge_sha
                or deployment.status != "RUNTIME_VERIFIED"
                or change_set.state != ChangeSetState.RUNTIME_VERIFIED.value
                or change_set.source_head_sha != source.merge_sha
                or change_set.deployment_head_sha != deployment_merge.merge_sha
                or active_agents
            ):
                raise ValueError(
                    "final runtime, cleanup, or immutable revision evidence is missing"
                )
            lease_repositories = {lease.repository for lease in worktree_leases}
            expected_lease_repositories = {
                plan.source.repository.rsplit("/", 1)[-1],
                plan.deployment.repository.rsplit("/", 1)[-1],
            }
            if lease_repositories != expected_lease_repositories:
                raise ValueError("final worktree cleanup lease binding is incomplete")
            for lease in worktree_leases:
                lease.released_at = lease.released_at or now
            recovery = dict(change_set.recovery_json)
            closure = dict(self._object(recovery.get("final_closure"), "final closure"))
            artifact = self._object(closure.get("final_artifact"), "final artifact")
            evidence = BacklogFinalClosureEvidence(
                final_source_commit=source.merge_sha,
                final_deployment_commit=deployment_merge.merge_sha,
                immutable_artifact_digests=tuple(sorted(change_set.image_digests.values())),
                staging_revision_verified=True,
                issue_acceptance_passed=True,
                regression_suite_passed=True,
                maker_provider=plan.maker_provider,
                checker_provider=plan.checker_provider,
                documentation_impact=plan.documentation_impact,
                cleanup_passed=True,
                safe_evidence_comment_ready=True,
                deployed_change=True,
            )
            if evaluate_final_closure(evidence) != "FIXED_CLOSURE_ELIGIBLE":
                raise ValueError("final closure evidence gate rejected issue closure")
            evidence_value = evidence.model_dump(mode="json")
            evidence_value["final_artifact_manifest_sha256"] = artifact["manifest_sha256"]
            evidence_value["runtime_verification_id"] = str(verification.id)
            evidence_sha = hashlib.sha256(
                json.dumps(evidence_value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            comment = (
                f"<!-- engineering-control-final-closure:{change_set.id} -->\n"
                "## Engineering Control final verification\n\n"
                f"- ChangeSet: `{change_set.id}`\n"
                f"- Final source commit: `{source.merge_sha}`\n"
                f"- Final deployment commit: `{deployment_merge.merge_sha}`\n"
                f"- Immutable artifact evidence: `{artifact['manifest_sha256']}`\n"
                f"- Runtime verification: `{verification.id}`\n"
                f"- Final evidence: `{evidence_sha}`\n"
                "- Result: final staging and runtime verification passed\n"
            )
            issue_plan = FinalIssueMutationPlan(
                closure=plan,
                final_source_sha=source.merge_sha,
                final_deployment_sha=deployment_merge.merge_sha,
                deployment_id=deployment.id,
                evidence_sha256=evidence_sha,
                comment_body=comment,
            )
            closure["evidence"] = evidence_value
            closure["evidence_sha256"] = evidence_sha
            closure["final_issue_plan"] = issue_plan.model_dump(mode="json")
            prior_cleanup = closure.get("worktree_cleanup")
            cleanup_released_at = (
                str(prior_cleanup.get("released_at"))
                if isinstance(prior_cleanup, dict) and prior_cleanup.get("released_at")
                else now.isoformat()
            )
            closure["worktree_cleanup"] = {
                "released_at": cleanup_released_at,
                "repositories": sorted(lease_repositories),
                "retention_cleanup_pending": True,
            }
            recovery["final_closure"] = closure
            change_set.recovery_json = recovery
            issue.final_closure_status = "FINAL_EVIDENCE_READY"
            run.summary = "Final runtime verified; bounded evidence comment is ready"
        return {
            **payload,
            "status": "FINAL_ISSUE_CLOSE_READY",
            "final_issue_plan": issue_plan.model_dump(mode="json"),
        }

    def _persist_final_comment(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = FinalIssueMutationPlan.model_validate(payload.get("final_issue_plan"))
        result = self._object(payload.get("comment_result"), "comment result")
        if (
            payload.get("status") != "FINAL_EVIDENCE_COMMENTED"
            or result.get("status") not in {"COMMENTED", "COMMENT_PRESENT"}
            or not re.fullmatch(
                rf"https://github\.com/{re.escape(plan.closure.issue_repository)}/issues/"
                rf"{plan.closure.issue_number}#issuecomment-[1-9][0-9]*",
                str(result.get("comment_url", "")),
            )
        ):
            raise ValueError("final evidence comment result is invalid")
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, plan.closure.change_set_id)
            issue = session.scalar(
                select(BacklogIssueRecord).where(
                    BacklogIssueRecord.backlog_campaign_id == plan.closure.backlog_campaign_id,
                    BacklogIssueRecord.repository == plan.closure.issue_repository,
                    BacklogIssueRecord.issue_number == plan.closure.issue_number,
                )
            )
            if change_set is None or issue is None:
                raise ValueError("final comment persistence binding is missing")
            recovery = dict(change_set.recovery_json)
            closure = dict(self._object(recovery.get("final_closure"), "final closure"))
            if closure.get("final_issue_plan") is None:
                raise ValueError("final issue closure requires persisted comment evidence")
            persisted_plan = FinalIssueMutationPlan.model_validate(closure.get("final_issue_plan"))
            if (
                persisted_plan != plan
                or change_set.state != ChangeSetState.RUNTIME_VERIFIED.value
                or issue.final_closure_status
                not in {"FINAL_EVIDENCE_READY", "FINAL_EVIDENCE_PERSISTED"}
            ):
                raise ValueError("final comment state changed after runtime verification")
            prior = closure.get("comment")
            if prior is not None:
                prior_comment = self._object(prior, "prior final comment evidence")
                if prior_comment.get("status") not in {
                    "COMMENTED",
                    "COMMENT_PRESENT",
                } or prior_comment.get("comment_url") != result.get("comment_url"):
                    raise ValueError("final comment evidence changed")
                result = prior_comment
            closure["comment"] = result
            recovery["final_closure"] = closure
            change_set.recovery_json = recovery
            issue.final_closure_status = "FINAL_EVIDENCE_PERSISTED"
        return {**payload, "status": "FINAL_EVIDENCE_PERSISTED"}

    def _persist_final_issue_close(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = FinalIssueMutationPlan.model_validate(payload.get("final_issue_plan"))
        result = self._object(payload.get("issue_close_result"), "issue close result")
        if (
            payload.get("status") != "ISSUE_CLOSED"
            or result.get("status") != "CLOSED"
            or result.get("issue_url")
            != (
                f"https://github.com/{plan.closure.issue_repository}/issues/"
                f"{plan.closure.issue_number}"
            )
        ):
            raise ValueError("final issue closure was not confirmed")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            run = session.get(Run, plan.closure.run_id)
            change_set = session.get(ChangeSet, plan.closure.change_set_id)
            issue = session.scalar(
                select(BacklogIssueRecord).where(
                    BacklogIssueRecord.backlog_campaign_id == plan.closure.backlog_campaign_id,
                    BacklogIssueRecord.repository == plan.closure.issue_repository,
                    BacklogIssueRecord.issue_number == plan.closure.issue_number,
                )
            )
            if run is None or change_set is None or issue is None:
                raise ValueError("final issue closure persistence binding is missing")
            if (
                change_set.state == ChangeSetState.ISSUE_CLOSED.value
                and issue.state == "CLOSED"
                and issue.final_closure_status == "FIXED_CLOSURE_COMPLETE"
            ):
                return {
                    **payload,
                    "status": "FIXED",
                    "change_set_id": str(plan.closure.change_set_id),
                }
            recovery = dict(change_set.recovery_json)
            closure = dict(self._object(recovery.get("final_closure"), "final closure"))
            if closure.get("final_issue_plan") is None:
                raise ValueError("final issue closure requires persisted comment evidence")
            persisted_plan = FinalIssueMutationPlan.model_validate(closure.get("final_issue_plan"))
            if persisted_plan != plan:
                raise ValueError("final issue plan changed after runtime verification")
            persisted_comment = closure.get("comment")
            if (
                issue.final_closure_status != "FINAL_EVIDENCE_PERSISTED"
                or not isinstance(persisted_comment, dict)
                or persisted_comment.get("status") not in {"COMMENTED", "COMMENT_PRESENT"}
            ):
                raise ValueError("final issue closure requires persisted comment evidence")
            comment = self._object(closure.get("comment"), "final comment evidence")
            if (
                change_set.state != ChangeSetState.RUNTIME_VERIFIED.value
                or issue.state != "OPEN"
                or issue.final_closure_status != "FINAL_EVIDENCE_PERSISTED"
                or comment.get("status") not in {"COMMENTED", "COMMENT_PRESENT"}
            ):
                raise ValueError("final issue state changed before closure persistence")
            closure["issue_close"] = result
            closure["completed_at"] = now.isoformat()
            recovery["final_closure"] = closure
            change_set.recovery_json = recovery
            change_set.state = ChangeSetState.ISSUE_CLOSED.value
            change_set.version += 1
            issue.state = "CLOSED"
            issue.candidate_status = "FIXED"
            issue.eligibility_status = "COMPLETE"
            issue.eligibility_reason = "FINAL_RUNTIME_VERIFIED"
            issue.delivery_status = "COMPLETE"
            issue.final_closure_status = "FIXED_CLOSURE_COMPLETE"
            snapshot = dict(issue.github_snapshot_json)
            snapshot["state"] = "CLOSED"
            snapshot["closed_at"] = result.get("closed_at")
            issue.github_snapshot_json = snapshot
            run.status = "SUCCESS"
            run.error_code = None
            run.summary = "Protected merges, final runtime verification, and issue closure complete"
            run.finished_at = now
        return {**payload, "status": "FIXED", "change_set_id": str(plan.closure.change_set_id)}

    @staticmethod
    def _final_pr_binding(
        *,
        repository: str,
        url: str | None,
        branch: str | None,
        head_sha: str | None,
        base_branch: Literal["main", "master"],
    ) -> ProtectedPullRequestBinding:
        if url is None or branch is None or head_sha is None:
            raise ValueError("final closure PR binding is incomplete")
        match = re.fullmatch(
            rf"https://github\.com/{re.escape(repository)}/pull/([1-9][0-9]*)", url
        )
        if match is None:
            raise ValueError("final closure PR URL does not match its repository")
        return ProtectedPullRequestBinding(
            repository=repository,
            number=int(match.group(1)),
            url=url,
            head_branch=branch,
            head_sha=head_sha,
            base_branch=base_branch,
        )

    def _prepare_reconciliation(self, payload: dict[str, Any]) -> dict[str, Any]:
        change_set_id = UUID(str(payload["change_set_id"]))
        orchestration = self._object(payload.get("orchestration"), "orchestration")
        workflow_id = str(orchestration.get("workflow_id", ""))
        candidate_build_run_id = payload.get("candidate_build_run_id")
        policy: DeliveryControlPolicy
        with self.sessions.begin() as session:
            run = session.scalar(
                select(Run).where(
                    Run.workflow_id == workflow_id,
                    Run.change_set_id == change_set_id,
                    Run.run_type == "delivery-reconciliation",
                )
            )
            change_set = session.get(ChangeSet, change_set_id)
            if run is None or change_set is None:
                raise ValueError("delivery reconciliation metadata binding is missing")
            if change_set.state not in {
                ChangeSetState.SOURCE_PR.value,
                ChangeSetState.CI_VALIDATED.value,
                ChangeSetState.CANDIDATE_BUILT.value,
            }:
                raise ValueError("ChangeSet is not in a reconcilable state")
            if not change_set.source_pr_url or not change_set.source_head_sha:
                raise ValueError("reconciliation requires an exact published source PR")
            if not change_set.source_branch:
                raise ValueError("reconciliation requires the bounded source branch")
            match = re.fullmatch(
                rf"https://github\.com/gntik-ai/{re.escape(change_set.source_repository)}/pull/([1-9][0-9]*)",
                change_set.source_pr_url,
            )
            if match is None:
                raise ValueError("source PR URL does not match its authoritative repository")
            recovery = dict(change_set.recovery_json)
            provider_chain_value = recovery.get("provider_chain")
            review_value = recovery.get("independent_review")
            provider_chain = provider_chain_value if isinstance(provider_chain_value, dict) else {}
            review = review_value if isinstance(review_value, dict) else {}
            maker = str(provider_chain.get("maker", ""))
            checker = str(provider_chain.get("checker", ""))
            if (
                maker not in {"codex", "claude"}
                or checker not in {"codex", "claude"}
                or maker == checker
                or review.get("decision") != "PASS"
                or review.get("provider") != checker
                or not re.fullmatch(r"[a-f0-9]{64}", str(review.get("sha256", "")))
            ):
                run.status = "BLOCKED"
                run.error_code = "INDEPENDENT_REVIEW_EVIDENCE_MISSING"
                run.summary = "source PR lacks a persisted cross-provider review binding"
                run.finished_at = datetime.now(UTC)
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "INDEPENDENT_REVIEW_EVIDENCE_MISSING",
                }
            project = Project(change_set.project)
            policy = self._delivery_control_policy(project)
            run.status = "RUNNING"
            run.started_at = run.started_at or datetime.now(UTC)
            plan = CIReconciliationPlan(
                change_set_id=change_set.id,
                run_id=run.id,
                workflow_id=workflow_id,
                project=project,
                repository=f"gntik-ai/{change_set.source_repository}",
                source_pr_number=int(match.group(1)),
                source_pr_url=change_set.source_pr_url,
                source_branch=change_set.source_branch,
                source_head_sha=change_set.source_head_sha,
                candidate_build_run_id=(
                    int(candidate_build_run_id) if candidate_build_run_id is not None else None
                ),
                policy=policy,
                independent_review_sha256=str(review["sha256"]),
                independent_review_provider=checker,
                maker_provider=maker,
            )
        return {
            **payload,
            "status": "RECONCILIATION_READY",
            "run_id": str(plan.run_id),
            "project": plan.project.value,
            "reconciliation_plan": plan.model_dump(mode="json"),
        }

    def _persist_reconciled_ci(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = CIReconciliationPlan.model_validate(payload.get("reconciliation_plan"))
        status = str(payload.get("status", ""))
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            run = session.get(Run, plan.run_id)
            change_set = session.get(ChangeSet, plan.change_set_id)
            if run is None or change_set is None or run.change_set_id != change_set.id:
                raise ValueError("reconciliation persistence binding is missing")
            if (
                run.workflow_id != plan.workflow_id
                or change_set.project != plan.project.value
                or change_set.source_pr_url != plan.source_pr_url
                or change_set.source_branch != plan.source_branch
                or change_set.source_head_sha != plan.source_head_sha
                or plan.policy != self._delivery_control_policy(plan.project)
            ):
                raise ValueError("source SHA changed during CI reconciliation")
            if status not in {"CI_VALIDATED", "CANDIDATE_VERIFIED"}:
                blocker = str(payload.get("blocker", status or "CI_RECONCILIATION_FAILED"))
                run.status = "BLOCKED"
                run.error_code = blocker[:120]
                run.summary = "source PR CI could not be authoritatively reconciled"
                run.finished_at = now
                return {**payload, "status": "BLOCKED", "blocker": blocker}
            evidence = self._object(payload.get("ci_evidence"), "ci_evidence")
            if (
                evidence.get("source_head_sha") != plan.source_head_sha
                or not re.fullmatch(r"[a-f0-9]{64}", str(evidence.get("checks_sha256", "")))
                or evidence.get("independent_review_sha256") != plan.independent_review_sha256
            ):
                raise ValueError("CI evidence is not bound to immutable source and review evidence")
            verification_id = uuid5(
                NAMESPACE_URL, f"ci-reconciliation:{plan.change_set_id}:{plan.source_head_sha}"
            )
            verification = session.get(VerificationResult, verification_id)
            verification_evidence = {
                "source_pr_url": plan.source_pr_url,
                "source_head_sha": plan.source_head_sha,
                "checks_sha256": evidence["checks_sha256"],
                "check_count": evidence.get("check_count"),
                "independent_review_sha256": plan.independent_review_sha256,
            }
            if verification is None:
                session.add(
                    VerificationResult(
                        id=verification_id,
                        change_set_id=change_set.id,
                        deployment_id=None,
                        run_id=run.id,
                        verification_type="source-pr-ci",
                        verifier_provider=None,
                        status="PASS",
                        evidence_json=verification_evidence,
                    )
                )
            elif verification.evidence_json != verification_evidence:
                raise ValueError("CI reconciliation evidence changed for the same source SHA")
            recovery = dict(change_set.recovery_json)
            recovery["ci"] = {
                **verification_evidence,
                "artifact": payload.get("ci_evidence_artifact"),
            }
            change_set.recovery_json = recovery
            if change_set.state == ChangeSetState.SOURCE_PR.value:
                change_set.state = ChangeSetState.CI_VALIDATED.value
                change_set.version += 1
            if status == "CANDIDATE_VERIFIED":
                if not plan.policy.candidate_build.enabled:
                    raise ValueError("candidate build policy is not enabled")
                manifest = CandidateArtifactManifest.model_validate(
                    payload.get("candidate_manifest")
                )
                expected_repositories = plan.policy.immutable_promotion.image_repositories
                repositories = {image.component: image.repository for image in manifest.images}
                digests = {image.component: image.digest for image in manifest.images}
                if (
                    manifest.source_sha != plan.source_head_sha
                    or manifest.workflow_run_id != plan.candidate_build_run_id
                    or repositories != expected_repositories
                ):
                    raise ValueError("candidate manifest changed after delivery verification")
                if change_set.image_digests and change_set.image_digests != digests:
                    raise ValueError("candidate image digests changed after persistence")
                change_set.image_digests = digests
                candidate_evidence = self._object(evidence.get("candidate"), "candidate evidence")
                attestations = candidate_evidence.get("attestation_sha256")
                if (
                    candidate_evidence.get("workflow_run_id") != manifest.workflow_run_id
                    or candidate_evidence.get("source_sha") != manifest.source_sha
                    or not re.fullmatch(
                        r"[a-f0-9]{64}",
                        str(candidate_evidence.get("manifest_sha256", "")),
                    )
                    or not isinstance(attestations, dict)
                    or set(attestations) != set(digests)
                    or any(
                        not re.fullmatch(r"[a-f0-9]{64}", str(value))
                        for value in attestations.values()
                    )
                ):
                    raise ValueError("candidate provenance evidence is incomplete")
                candidate_verification_id = uuid5(
                    NAMESPACE_URL,
                    (
                        f"candidate-artifact:{plan.change_set_id}:"
                        f"{manifest.workflow_run_id}:{manifest.source_sha}"
                    ),
                )
                candidate_record = {
                    "workflow_run_id": manifest.workflow_run_id,
                    "source_sha": manifest.source_sha,
                    "manifest_sha256": candidate_evidence.get("manifest_sha256"),
                    "attestation_sha256": candidate_evidence.get("attestation_sha256"),
                    "image_digests": digests,
                    "artifact": payload.get("ci_evidence_artifact"),
                }
                existing_candidate = session.get(VerificationResult, candidate_verification_id)
                if existing_candidate is None:
                    session.add(
                        VerificationResult(
                            id=candidate_verification_id,
                            change_set_id=change_set.id,
                            deployment_id=None,
                            run_id=run.id,
                            verification_type="candidate-artifact-provenance",
                            verifier_provider=None,
                            status="PASS",
                            evidence_json=candidate_record,
                        )
                    )
                elif existing_candidate.evidence_json != candidate_record:
                    raise ValueError("candidate provenance changed for the same workflow run")
                recovery["candidate"] = candidate_record
                change_set.recovery_json = recovery
                if change_set.state == ChangeSetState.CI_VALIDATED.value:
                    change_set.state = ChangeSetState.CANDIDATE_BUILT.value
                    change_set.version += 1
                run.status = "RUNNING"
                run.error_code = None
                run.summary = "exact-SHA CI and signed immutable candidate images verified"
                return {**payload, "status": "CANDIDATE_BUILT"}
            blocker = str(payload.get("candidate_status") or "AWAITING_IMMUTABLE_ARTIFACT")
            run.status = "BLOCKED"
            run.error_code = blocker[:120]
            run.summary = "source PR CI passed; immutable candidate artifact remains unavailable"
            run.finished_at = now
        return {**payload, "status": "CI_VALIDATED", "blocker": blocker}

    def _prepare_promotion(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = CIReconciliationPlan.model_validate(payload.get("reconciliation_plan"))
        dry_run = bool(payload.get("dry_run", True))
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            run = session.get(Run, plan.run_id)
            change_set = session.get(ChangeSet, plan.change_set_id)
            if run is None or change_set is None or run.change_set_id != change_set.id:
                raise ValueError("promotion metadata binding is missing")
            allowed_states = (
                {
                    ChangeSetState.FINAL_ARTIFACT_BUILT.value,
                    ChangeSetState.DEPLOY_PR.value,
                }
                if run.run_type == "final-closure"
                else {ChangeSetState.CANDIDATE_BUILT.value}
            )
            if change_set.state not in allowed_states:
                raise ValueError("immutable promotion requires a verified candidate build")
            if dry_run:
                run.status = "SUCCESS"
                run.summary = (
                    "CI and signed candidate verified; promotion dry-run stopped "
                    "before Git mutation"
                )
                run.finished_at = now
                return {
                    **payload,
                    "status": "DRY_RUN",
                    "next_state": ChangeSetState.CANDIDATE_BUILT.value,
                }
            promotion_policy = plan.policy.immutable_promotion
            if not promotion_policy.enabled:
                blocker = str(promotion_policy.blocker)
                run.status = "BLOCKED"
                run.error_code = blocker
                run.summary = "immutable deployment rendering is not enabled for this project"
                run.finished_at = now
                return {**payload, "status": "BLOCKED", "blocker": blocker}
            recovery = dict(change_set.recovery_json)
            existing_promotion = recovery.get("promotion")
            closure_value = recovery.get("final_closure")
            closure = closure_value if isinstance(closure_value, dict) else {}
            stored_merge_plan_value = closure.get("final_deployment_merge_plan")
            if stored_merge_plan_value is not None and run.run_type != "final-closure":
                raise ValueError("final deployment merge plan escaped its closure run")
            stored_merge_plan = (
                FinalDeploymentMergePlan.model_validate(stored_merge_plan_value)
                if stored_merge_plan_value is not None
                else None
            )
            expected_promoted_head = (
                stored_merge_plan.deployment.head_sha
                if stored_merge_plan is not None
                else change_set.deployment_head_sha
            )
            exact_promotion = (
                isinstance(existing_promotion, dict)
                and existing_promotion.get("source_sha") == change_set.source_head_sha
                and existing_promotion.get("image_digests") == change_set.image_digests
            )
            legacy_candidate_promotion = (
                isinstance(existing_promotion, dict)
                and run.run_type == "delivery-reconciliation"
                and change_set.state == ChangeSetState.CANDIDATE_BUILT.value
                and "source_sha" not in existing_promotion
                and "image_digests" not in existing_promotion
            )
            if (
                isinstance(existing_promotion, dict)
                and existing_promotion.get("promoted_head_sha") == expected_promoted_head
                and (exact_promotion or legacy_candidate_promotion)
                and re.fullmatch(
                    r"[a-f0-9]{40}", str(existing_promotion.get("promoted_head_sha", ""))
                )
            ):
                if stored_merge_plan is not None:
                    return {
                        **payload,
                        "status": "DEPLOY_PR",
                        "final_deployment_merge_plan": stored_merge_plan.model_dump(mode="json"),
                        "release_evidence": self._object(
                            recovery.get("release_evidence"), "release evidence"
                        ),
                    }
                publication = self._promotion_publication(
                    change_set, str(existing_promotion["promoted_head_sha"])
                )
                return {
                    **payload,
                    "status": "PROMOTION_PUBLICATION_READY",
                    "promotion_publication": publication.model_dump(mode="json"),
                }
            lease = session.scalar(
                select(WorktreeLease).where(
                    WorktreeLease.change_set_id == change_set.id,
                    WorktreeLease.repository == change_set.deployment_repository,
                )
            )
            if (
                lease is None
                or not change_set.deployment_branch
                or not change_set.deployment_head_sha
            ):
                raise ValueError("promotion requires the original bounded deployment worktree")
            expected_path = (
                self.settings.worktree_root / change_set.deployment_repository / str(change_set.id)
            ).as_posix()
            if lease.worktree_path != expected_path or lease.branch != change_set.deployment_branch:
                raise ValueError("promotion worktree lease identity changed")
            prior_run = session.get(Run, lease.owner_run_id)
            if (
                prior_run is not None
                and prior_run.id != run.id
                and prior_run.status
                in {
                    "QUEUED",
                    "RUNNING",
                    "AWAITING_APPROVAL",
                }
            ):
                raise ComponentLeaseConflict("prior worktree owner remains active")
            lease.owner_run_id = run.id
            lease.expires_at = now + timedelta(hours=4)
            lease.released_at = None
            component = ComponentLeaseManager().acquire(
                session,
                component_key=promotion_policy.component_lease,
                owner_run_id=run.id,
                mode="implementation",
                ttl=timedelta(hours=4),
            )
            recovery["promotion_component_lease"] = component.component_key
            change_set.recovery_json = recovery
            promotion = PromotionPlan(
                change_set_id=change_set.id,
                run_id=run.id,
                workflow_id=plan.workflow_id,
                project=Project.FALCONE,
                repository="gntik-ai/falcone-charts",
                repository_slug="falcone-charts",
                working_directory=Path(lease.worktree_path),
                branch=change_set.deployment_branch,
                expected_head_sha=change_set.deployment_head_sha,
                deployment_file=promotion_policy.deployment_file,
                chart_directory=str(promotion_policy.chart_directory),
                values_files=promotion_policy.values_files,
                digest_paths=promotion_policy.digest_paths,
                image_digests={
                    str(key): str(value) for key, value in change_set.image_digests.items()
                },
                chart_version=change_set.chart_version,
            )
        return {
            **payload,
            "status": "PROMOTION_READY",
            "promotion_plan": promotion.model_dump(mode="json"),
        }

    def _persist_promotion(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = PromotionPlan.model_validate(payload.get("promotion_plan"))
        with self.sessions.begin() as session:
            run = session.get(Run, plan.run_id)
            change_set = session.get(ChangeSet, plan.change_set_id)
            if run is None or change_set is None:
                raise ValueError("promotion persistence binding is missing")
            if payload.get("status") != "PROMOTION_COMMITTED":
                blocker = str(payload.get("blocker", "IMMUTABLE_PROMOTION_VALIDATION_FAILED"))
                run.status = "BLOCKED"
                run.error_code = blocker[:120]
                run.summary = "deployment worktree was preserved after promotion failure"
                run.finished_at = datetime.now(UTC)
                self._release_reconciliation_component(session, change_set, run)
                return {**payload, "status": "BLOCKED", "blocker": blocker}
            promoted_head = str(payload.get("promoted_head_sha", ""))
            evidence = self._object(payload.get("promotion_evidence"), "promotion_evidence")
            if (
                not re.fullmatch(r"[a-f0-9]{40}", promoted_head)
                or evidence.get("base_sha") != plan.expected_head_sha
                or evidence.get("promoted_sha") != promoted_head
                or evidence.get("image_digests") != plan.image_digests
            ):
                raise ValueError("promotion evidence is not bound to its exact Git transaction")
            change_set.deployment_head_sha = promoted_head
            change_set.chart_version = str(evidence.get("chart_version") or "") or None
            recovery = dict(change_set.recovery_json)
            recovery["promotion"] = {
                "base_sha": plan.expected_head_sha,
                "promoted_head_sha": promoted_head,
                "source_sha": change_set.source_head_sha,
                "image_digests": plan.image_digests,
                "diff_sha256": evidence.get("diff_sha256"),
                "rendered_manifest_sha256": evidence.get("rendered_manifest_sha256"),
                "artifacts": payload.get("promotion_artifacts"),
            }
            change_set.recovery_json = recovery
            change_set.version += 1
            publication = self._promotion_publication(change_set, promoted_head)
        return {
            **payload,
            "status": "PROMOTION_PUBLICATION_READY",
            "promotion_publication": publication.model_dump(mode="json"),
        }

    def _persist_promotion_publication(self, payload: dict[str, Any]) -> dict[str, Any]:
        publication = PromotionPublication.model_validate(payload.get("promotion_publication"))
        with self.sessions.begin() as session:
            workflow_id = self._workflow_id(payload)
            run = session.scalar(
                select(Run).where(
                    Run.workflow_id == workflow_id,
                    Run.change_set_id == publication.change_set_id,
                    Run.run_type.in_({"delivery-reconciliation", "final-closure"}),
                    Run.status == "RUNNING",
                )
            )
            change_set = session.get(ChangeSet, publication.change_set_id)
            if run is None or change_set is None:
                raise ValueError("promotion publication metadata binding is missing")
            if payload.get("status") != "DEPLOYMENT_PR_CREATED":
                blocker = str(payload.get("blocker", "GITHUB_PROMOTION_PUBLICATION_FAILED"))
                run.status = "BLOCKED"
                run.error_code = blocker[:120]
                run.summary = "immutable promotion commit preserved pending scoped publication"
                run.finished_at = datetime.now(UTC)
                self._release_reconciliation_component(session, change_set, run)
                return {**payload, "status": "BLOCKED", "blocker": blocker}
            result = self._object(payload.get("promotion_result"), "promotion_result")
            if (
                result.get("repository_slug") != change_set.deployment_repository
                or result.get("head_sha") != change_set.deployment_head_sha
                or result.get("pr_state") != "OPEN"
                or not re.fullmatch(
                    r"https://github\.com/gntik-ai/falcone-charts/pull/[1-9][0-9]*",
                    str(result.get("pr_url", "")),
                )
            ):
                raise ValueError("deployment PR does not bind to the promoted Git head")
            change_set.deployment_pr_url = str(result["pr_url"])
            change_set.state = ChangeSetState.DEPLOY_PR.value
            change_set.version += 1
            run.status = "RUNNING"
            run.error_code = None
            run.summary = (
                "signed candidate digests published; independent release-plan review pending"
            )
        return {
            **payload,
            "status": "RELEASE_REVIEW_REQUIRED",
            "deployment_pr": result["pr_url"],
            "deployment_sha": result["head_sha"],
        }

    def _prepare_release_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        change_set_id = UUID(str(payload["change_set_id"]))
        workflow_id = self._workflow_id(payload)
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, change_set_id)
            run = session.scalar(
                select(Run).where(
                    Run.workflow_id == workflow_id,
                    Run.change_set_id == change_set_id,
                    Run.run_type.in_({"delivery-reconciliation", "final-closure"}),
                )
            )
            if change_set is None or run is None:
                raise ValueError("release review metadata binding is missing")
            if (
                change_set.state != ChangeSetState.DEPLOY_PR.value
                or not change_set.deployment_pr_url
                or not change_set.deployment_head_sha
            ):
                raise ValueError("release review requires an exact protected deployment PR")
            recovery = dict(change_set.recovery_json)
            chain = self._object(recovery.get("provider_chain"), "provider_chain")
            provider = Provider(str(chain.get("checker", "")))
            if provider.value == chain.get("maker"):
                raise ValueError("release review provider is not independent from the maker")
            review_plan = self._build_release_review_plan(change_set)
            lease = session.scalar(
                select(WorktreeLease).where(
                    WorktreeLease.change_set_id == change_set.id,
                    WorktreeLease.repository == change_set.deployment_repository,
                )
            )
            if lease is None:
                raise ValueError("release review deployment worktree is missing")
            prompt = (
                "Independently review this immutable deterministic release and recovery plan. "
                "Do not modify files, deploy, approve, merge, read credentials, inspect Secret "
                "values, or invoke Kubernetes/Helm/Argo commands. Fail closed on any mismatch "
                "between the plan and the bounded deployment worktree. Return only the required "
                "structured review result.\n\nRelease plan:\n"
                + json.dumps(review_plan, sort_keys=True, separators=(",", ":"))
            )
            request = AgentRequest(
                run_id=uuid5(
                    NAMESPACE_URL,
                    (
                        f"release-plan-review:{change_set.id}:"
                        f"{change_set.deployment_head_sha}:"
                        f"{review_plan['plan_sha256']}"
                    ),
                ),
                project=Project(change_set.project),
                role="release-plan-reviewer",
                provider=provider,
                model_class=ModelClass.JUDGE,
                prompt=prompt,
                working_directory=lease.worktree_path,
                output_schema=self._release_review_output_schema(),
                timeout_seconds=5400,
                effort="high",
                writable=False,
                change_set_id=change_set.id,
            )
        return {
            **payload,
            "status": "RELEASE_REVIEW_READY",
            "release_review_provider": provider.value,
            "release_review_plan": review_plan,
            "release_review_request": request.model_dump(mode="json"),
        }

    def _persist_release_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        change_set_id = UUID(str(payload["change_set_id"]))
        workflow_id = self._workflow_id(payload)
        request = AgentRequest.model_validate(payload.get("release_review_request"))
        result = AgentResult.model_validate(payload.get("agent_result"))
        structured = result.structured_result
        decision = str(structured.get("decision", ""))
        final_deployment_merge_plan: FinalDeploymentMergePlan | None = None
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, change_set_id)
            run = session.scalar(
                select(Run).where(
                    Run.workflow_id == workflow_id,
                    Run.change_set_id == change_set_id,
                    Run.run_type.in_({"delivery-reconciliation", "final-closure"}),
                )
            )
            if change_set is None or run is None:
                raise ValueError("release review persistence binding is missing")
            expected_plan = self._build_release_review_plan(change_set)
            supplied_plan = self._object(payload.get("release_review_plan"), "release review plan")
            if supplied_plan != expected_plan:
                raise ValueError("release review plan changed during independent review")
            recovery = dict(change_set.recovery_json)
            chain = self._object(recovery.get("provider_chain"), "provider_chain")
            original_review = self._object(recovery.get("independent_review"), "independent_review")
            lease = session.scalar(
                select(WorktreeLease).where(
                    WorktreeLease.change_set_id == change_set.id,
                    WorktreeLease.repository == change_set.deployment_repository,
                )
            )
            if (
                result.run_id != request.run_id
                or result.provider is not request.provider
                or result.role != request.role
                or request.change_set_id != change_set.id
                or lease is None
                or request.working_directory != lease.worktree_path
            ):
                raise ValueError("release review result identity changed during execution")
            if (
                result.provider.value != chain.get("checker")
                or result.provider.value == chain.get("maker")
                or result.role != "release-plan-reviewer"
            ):
                raise ValueError("release review result is not independently provider-bound")
            release_review_sha = hashlib.sha256(
                json.dumps(structured, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if decision != "PASS":
                run.status = "BLOCKED"
                run.error_code = (
                    "RELEASE_PLAN_REQUEST_CHANGES"
                    if decision == "REQUEST_CHANGES"
                    else "RELEASE_PLAN_REVIEW_BLOCKED"
                )
                run.summary = "independent release-plan reviewer did not approve the plan"
                run.finished_at = datetime.now(UTC)
                if decision == "REQUEST_CHANGES":
                    change_set.state = ChangeSetState.REQUEST_CHANGES.value
                    change_set.version += 1
                recovery["release_plan_review"] = {
                    "decision": decision,
                    "provider": result.provider.value,
                    "sha256": release_review_sha,
                }
                change_set.recovery_json = recovery
                self._release_reconciliation_component(session, change_set, run)
                return {
                    **payload,
                    "status": "REQUEST_CHANGES" if decision == "REQUEST_CHANGES" else "BLOCKED",
                    "blocker": run.error_code,
                }
            code_review_sha = str(original_review.get("sha256", ""))
            if not self._is_digest(code_review_sha):
                raise ValueError("original code review evidence is not immutable")
            combined_review_sha = hashlib.sha256(
                json.dumps(
                    {
                        "code_review_sha256": code_review_sha,
                        "release_plan_review_sha256": release_review_sha,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            rollback_plan = self._object(expected_plan.get("rollback_plan"), "rollback_plan")
            release_evidence = {
                "version": 1,
                "migration": bool(expected_plan["migration"]),
                "backup_requirement": expected_plan["backup_requirement"],
                "rollback_strategy": rollback_plan["strategy"],
                "rollback_plan_sha256": expected_plan["rollback_plan_sha256"],
                "ci_result_sha256": expected_plan["ci_result_sha256"],
                "code_review_sha256": code_review_sha,
                "release_plan_review_sha256": release_review_sha,
                "independent_review_sha256": combined_review_sha,
                "release_plan_sha256": expected_plan["plan_sha256"],
                "impact_classifier_sha256": expected_plan["impact_classifier_sha256"],
                "backup_evidence_sha256": None,
                "parity_evidence_sha256": None,
            }
            recovery["release_plan_review"] = {
                "decision": "PASS",
                "provider": result.provider.value,
                "sha256": release_review_sha,
            }
            recovery["rollback_plan"] = rollback_plan
            recovery["release_evidence"] = release_evidence
            change_set.recovery_json = recovery
            if run.run_type == "final-closure":
                closure_plan = FinalClosurePlan.model_validate(payload.get("final_closure_plan"))
                persisted_closure = self._object(
                    recovery.get("final_closure"), "persisted final closure"
                )
                persisted_closure_plan = FinalClosurePlan.model_validate(
                    persisted_closure.get("plan")
                )
                if (
                    closure_plan != persisted_closure_plan
                    or closure_plan.change_set_id != change_set.id
                ):
                    raise ValueError("final release review ChangeSet binding changed")
                deployment_binding = self._final_pr_binding(
                    repository=f"gntik-ai/{change_set.deployment_repository}",
                    url=change_set.deployment_pr_url,
                    branch=change_set.deployment_branch,
                    head_sha=change_set.deployment_head_sha,
                    base_branch=closure_plan.deployment.base_branch,
                )
                final_deployment_merge_plan = FinalDeploymentMergePlan(
                    closure=closure_plan,
                    deployment=deployment_binding,
                    independent_review_sha256=combined_review_sha,
                )
                closure_value = recovery.get("final_closure")
                closure = dict(closure_value) if isinstance(closure_value, dict) else {}
                closure["final_deployment_merge_plan"] = final_deployment_merge_plan.model_dump(
                    mode="json"
                )
                recovery["final_closure"] = closure
                change_set.recovery_json = recovery
            run.status = "RUNNING" if run.run_type == "final-closure" else "SUCCESS"
            run.error_code = None
            run.summary = "immutable release and recovery plan independently reviewed"
            run.finished_at = None if run.run_type == "final-closure" else datetime.now(UTC)
            self._release_reconciliation_component(session, change_set, run)
        response = {
            **payload,
            "status": "DEPLOY_PR",
            "release_evidence": release_evidence,
        }
        if final_deployment_merge_plan is not None:
            response["final_deployment_merge_plan"] = final_deployment_merge_plan.model_dump(
                mode="json"
            )
        return response

    def _promotion_publication(
        self, change_set: ChangeSet, promoted_head: str
    ) -> PromotionPublication:
        if not change_set.deployment_branch or not change_set.source_pr_url:
            raise ValueError("promotion publication requires authoritative branches and source PR")
        worktree = (
            self.settings.worktree_root / change_set.deployment_repository / str(change_set.id)
        )
        body = (
            "## Engineering Control immutable promotion\n\n"
            f"ChangeSet: `{change_set.id}`\n\n"
            f"Source PR: {change_set.source_pr_url}\n\n"
            f"Source SHA: `{change_set.source_head_sha}`\n\n"
            f"Deployment SHA: `{promoted_head}`\n\n"
            "Every first-party image is bound by an exact sha256 digest and verified "
            "GitHub artifact attestation. Merge and deployment remain protected.\n"
        )
        return PromotionPublication(
            change_set_id=change_set.id,
            repository="gntik-ai/falcone-charts",
            repository_slug="falcone-charts",
            working_directory=worktree,
            branch=change_set.deployment_branch,
            head_sha=promoted_head,
            title=f"changeset {str(change_set.id)[:8]}: promote immutable candidate",
            body=body,
        )

    def _build_release_review_plan(self, change_set: ChangeSet) -> dict[str, Any]:
        recovery = dict(change_set.recovery_json)
        ci = self._object(recovery.get("ci"), "release CI evidence")
        original_review = self._object(
            recovery.get("independent_review"), "release independent review"
        )
        impact = self._object(recovery.get("release_impact"), "release impact")
        promotion = self._object(recovery.get("promotion"), "immutable promotion evidence")
        ci_sha = str(ci.get("checks_sha256", ""))
        code_review_sha = str(original_review.get("sha256", ""))
        impact_sha = str(impact.get("classifier_sha256", ""))
        promotion_diff_sha = str(promotion.get("diff_sha256", ""))
        rendered_sha = str(promotion.get("rendered_manifest_sha256", ""))
        if any(
            not self._is_digest(value)
            for value in (
                ci_sha,
                code_review_sha,
                impact_sha,
                promotion_diff_sha,
                rendered_sha,
            )
        ):
            raise ValueError("release plan evidence is incomplete")
        if (
            not change_set.source_head_sha
            or not change_set.source_pr_url
            or not change_set.deployment_head_sha
            or not change_set.deployment_pr_url
            or not change_set.image_digests
            or promotion.get("promoted_head_sha") != change_set.deployment_head_sha
        ):
            raise ValueError("release plan immutable ChangeSet identity is incomplete")
        migration = bool(impact.get("migration", False))
        categories = impact.get("categories")
        if not isinstance(categories, list):
            raise ValueError("release impact categories are invalid")
        if change_set.project == Project.FALCONE.value:
            strategy = "forward-recovery-only" if migration else "helm-revision-rollback"
        else:
            strategy = "protected-git-revert-pr"
        rollback_plan = {
            "version": 1,
            "strategy": strategy,
            "project": change_set.project,
            "change_set_id": str(change_set.id),
            "source_base_sha": change_set.source_base_sha,
            "source_head_sha": change_set.source_head_sha,
            "deployment_base_sha": change_set.deployment_base_sha,
            "deployment_head_sha": change_set.deployment_head_sha,
            "migration": migration,
            "requires_second_approval": True,
            "prohibits_direct_git_or_cluster_mutation": True,
        }
        rollback_plan_sha = hashlib.sha256(
            json.dumps(rollback_plan, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        plan = {
            "version": 1,
            "project": change_set.project,
            "change_set_id": str(change_set.id),
            "risk_class": change_set.risk_class,
            "source_pr": change_set.source_pr_url,
            "source_sha": change_set.source_head_sha,
            "deployment_pr": change_set.deployment_pr_url,
            "deployment_sha": change_set.deployment_head_sha,
            "image_digests": dict(sorted(change_set.image_digests.items())),
            "chart_version": change_set.chart_version,
            "migration": migration,
            "impact_categories": sorted(str(category) for category in categories),
            "impact_classifier_sha256": impact_sha,
            "promotion_diff_sha256": promotion_diff_sha,
            "rendered_manifest_sha256": rendered_sha,
            "ci_result_sha256": ci_sha,
            "code_review_sha256": code_review_sha,
            "backup_requirement": (
                "real-backup-and-parity-required" if migration else "not-required"
            ),
            "rollback_plan": rollback_plan,
            "rollback_plan_sha256": rollback_plan_sha,
            "paid_api_fallback": False,
        }
        plan["plan_sha256"] = hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return plan

    def _block_reconciliation(self, payload: dict[str, Any], blocker: str) -> dict[str, Any]:
        plan = CIReconciliationPlan.model_validate(payload.get("reconciliation_plan"))
        with self.sessions.begin() as session:
            run = session.get(Run, plan.run_id)
            change_set = session.get(ChangeSet, plan.change_set_id)
            if run is None or change_set is None:
                raise ValueError("blocked reconciliation metadata binding is missing")
            run.status = "BLOCKED"
            run.error_code = blocker
            run.summary = "immutable promotion is waiting for its bounded component lease"
            run.finished_at = datetime.now(UTC)
            self._release_reconciliation_component(session, change_set, run)
        return {**payload, "status": "BLOCKED", "blocker": blocker}

    def _delivery_control_policy(self, project: Project) -> DeliveryControlPolicy:
        root = self.settings.project_config_root.resolve()
        path = (root / f"{project.value}.yaml").resolve()
        if not path.is_file() or not path.is_relative_to(root):
            raise ValueError("project delivery-control policy is missing")
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("project") != project.value:
            raise ValueError("project delivery-control policy identity is invalid")
        return DeliveryControlPolicy.model_validate(value.get("delivery_control"))

    def _project_issue_repositories(self, project: Project) -> tuple[str, str]:
        root = self.settings.project_config_root.resolve()
        path = (root / f"{project.value}.yaml").resolve()
        if not path.is_file() or not path.is_relative_to(root):
            raise ValueError("project repository-pair manifest is missing")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            manifest = ProjectManifest.model_validate(raw)
        except ValidationError as error:
            raise ValueError("project repository-pair manifest is invalid") from error
        if manifest.project != project.value:
            raise ValueError("project repository-pair manifest identity is invalid")
        return (
            str(manifest.repositories.source.issue_repository),
            str(manifest.repositories.deployment.issue_repository),
        )

    def _cleanup_result_for_flow(
        self, project: str, repository_surface: str, flow_id: str
    ) -> str | None:
        if repository_surface != "runtime" or flow_id == "repository-static":
            return "NOT_APPLICABLE_NON_MUTATING_REPOSITORY_AUDIT"
        root = (self.settings.flow_config_root.resolve() / project).resolve()
        if not root.is_dir() or not root.is_relative_to(self.settings.flow_config_root.resolve()):
            return None
        for path in sorted(root.glob("*.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            flows = raw.get("flows") if isinstance(raw, dict) else None
            if not isinstance(flows, list):
                continue
            for flow in flows:
                if not isinstance(flow, dict) or flow.get("id") != flow_id:
                    continue
                return (
                    "NOT_APPLICABLE_NON_MUTATING_FLOW"
                    if flow.get("mutation") == "non-mutating"
                    else None
                )
        return None

    @staticmethod
    def _release_reconciliation_component(session: Any, change_set: ChangeSet, run: Run) -> None:
        recovery = dict(change_set.recovery_json)
        component = recovery.get("promotion_component_lease")
        if isinstance(component, str):
            ComponentLeaseManager().release(session, component_key=component, owner_run_id=run.id)

    def _persist_delivery_handoff(self, payload: dict[str, Any]) -> dict[str, Any]:
        evidence = self._object(payload.get("change_evidence"), "change_evidence")
        source = self._object(evidence.get("source"), "source change evidence")
        deployment = self._object(evidence.get("deployment"), "deployment change evidence")
        if source.get("head_sha") == payload.get("source_base_sha"):
            blocker = "SOURCE_CHANGES_NOT_COMMITTED"
        elif int(deployment.get("changed_file_count", 0)) > 0 and deployment.get(
            "head_sha"
        ) == payload.get("deployment_base_sha"):
            blocker = "DEPLOYMENT_CHANGES_NOT_COMMITTED"
        else:
            blocker = "GIT_PUSH_IDENTITY_NOT_CONFIGURED"
        change_set_id = UUID(str(payload["change_set_id"]))
        run_id = UUID(str(payload["run_id"]))
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, change_set_id)
            run = session.get(Run, run_id)
            if change_set is None or run is None:
                raise ValueError("delivery handoff metadata is missing")
            change_set.source_branch = str(payload["source_branch"])
            change_set.deployment_branch = str(payload["deployment_branch"])
            change_set.source_head_sha = str(source["head_sha"])
            change_set.deployment_head_sha = str(deployment["head_sha"])
            recovery = dict(change_set.recovery_json)
            recovery["provider_chain"] = {
                "maker": str(payload["maker_provider"]),
                "checker": str(payload["checker_provider"]),
            }
            change_set.recovery_json = recovery
            change_set.state = "IMPLEMENTING"
            change_set.version += 1
            run.status = "BLOCKED"
            run.error_code = blocker
            run.summary = "bounded local changes are preserved pending scoped Git push identity"
        return {
            **payload,
            "status": "BLOCKED",
            "blocker": blocker,
            "handoff": {
                "change_set_id": str(change_set_id),
                "source_branch": payload["source_branch"],
                "source_head_sha": source["head_sha"],
                "deployment_branch": payload["deployment_branch"],
                "deployment_head_sha": deployment["head_sha"],
                "change_evidence": evidence,
            },
        }

    def _prepare_deployment(
        self,
        payload: dict[str, Any],
        environment: Literal["preview", "staging"],
    ) -> dict[str, Any]:
        change_set_id = UUID(str(payload["change_set_id"]))
        orchestration = self._object(payload.get("orchestration"), "orchestration")
        workflow_id = str(orchestration.get("workflow_id", ""))
        if not workflow_id:
            raise ValueError("deployment workflow identity is missing")
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, change_set_id)
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if change_set is None or run is None or run.change_set_id != change_set_id:
                raise ValueError("deployment ChangeSet/run binding is missing")
            project = Project(change_set.project)
            target = self._deployment_target(project, environment)
            if not target.enabled:
                run.status = "BLOCKED"
                run.error_code = target.blocker or "DEPLOYMENT_TARGET_DISABLED"
                return {
                    **payload,
                    "project": project.value,
                    "environment": environment,
                    "status": "BLOCKED",
                    "blocker": target.blocker or "DEPLOYMENT_TARGET_DISABLED",
                    "target_context": target.target_context,
                    "target_namespace": target.target_namespace,
                }
            missing = self._release_missing(change_set)
            release_evidence = self._object(
                change_set.recovery_json.get("release_evidence", {}), "release_evidence"
            )
            for key in (
                "rollback_plan_sha256",
                "ci_result_sha256",
                "independent_review_sha256",
            ):
                if not self._is_digest(release_evidence.get(key)):
                    missing.append(key)
            if bool(release_evidence.get("migration", False)):
                for key in ("backup_evidence_sha256", "parity_evidence_sha256"):
                    if not self._is_digest(release_evidence.get(key)):
                        missing.append(key)
            if missing:
                run.status = "BLOCKED"
                run.error_code = "RELEASE_EVIDENCE_INCOMPLETE"
                return {
                    **payload,
                    "project": project.value,
                    "environment": environment,
                    "status": "BLOCKED",
                    "blocker": "RELEASE_EVIDENCE_INCOMPLETE",
                    "missing_requirements": sorted(set(missing)),
                }
            lease = session.scalar(
                select(WorktreeLease).where(
                    WorktreeLease.change_set_id == change_set_id,
                    WorktreeLease.repository == change_set.deployment_repository,
                    WorktreeLease.released_at.is_(None),
                )
            )
            if lease is None:
                run.status = "BLOCKED"
                run.error_code = "DEPLOYMENT_WORKTREE_LEASE_MISSING"
                return {
                    **payload,
                    "project": project.value,
                    "environment": environment,
                    "status": "BLOCKED",
                    "blocker": "DEPLOYMENT_WORKTREE_LEASE_MISSING",
                }
            release_plan = self._release_plan(change_set, lease, target, release_evidence)
            deployment_id = uuid5(
                NAMESPACE_URL,
                f"deployment:{change_set_id}:{environment}:{change_set.deployment_head_sha}",
            )
            deployment = session.get(Deployment, deployment_id)
            if deployment is None:
                deployment = Deployment(
                    id=deployment_id,
                    change_set_id=change_set_id,
                    project=project.value,
                    environment=environment,
                    strategy=target.strategy,
                    target_context=target.target_context,
                    target_namespace=target.target_namespace,
                    desired_sha=str(change_set.deployment_head_sha),
                    status="PREPARING",
                    transaction_id=None,
                    started_at=None,
                    finished_at=None,
                    recovery_json={
                        "rollback_plan_sha256": release_evidence["rollback_plan_sha256"]
                    },
                )
                session.add(deployment)
            run.status = "RUNNING"
            return {
                **payload,
                "project": project.value,
                "risk_class": change_set.risk_class,
                "environment": environment,
                "deployment_id": str(deployment_id),
                "status": "DEPLOYMENT_PLAN_PREPARED",
                "target_context": target.target_context,
                "target_namespace": target.target_namespace,
                "release_evidence": release_evidence,
                "release_plan": release_plan,
            }

    def _record_preview_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._record_deployment_candidate(payload, "preview")

    def _record_staging_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._record_deployment_candidate(payload, "staging")

    def _record_deployment_candidate(
        self,
        payload: dict[str, Any],
        environment: Literal["preview", "staging"],
    ) -> dict[str, Any]:
        change_set_id = UUID(str(payload["change_set_id"]))
        preflight = self._object(payload.get("release_preflight"), "release_preflight")
        preflight_hash = hashlib.sha256(
            json.dumps(preflight, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        release_evidence = self._object(payload.get("release_evidence"), "release_evidence")
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, change_set_id)
            if change_set is None:
                raise ValueError(f"{environment} candidate ChangeSet is missing")
            workflow_id = str(payload["orchestration"]["workflow_id"])
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if run is None or run.change_set_id != change_set_id:
                raise ValueError(f"{environment} candidate Run binding is missing")
            deployment_lease = ComponentLeaseManager().acquire(
                session,
                component_key=f"{change_set.project}:deploy:{environment}",
                owner_run_id=run.id,
                mode="deployment",
                ttl=timedelta(hours=8),
            )
            model = self._changeset_model(session, change_set)
            approval_payload = ApprovalService.artifact_payload(
                model,
                target_context=str(payload["target_context"]),
                target_namespace=str(payload["target_namespace"]),
                migration=bool(release_evidence.get("migration", False)),
                backup_requirement=str(release_evidence.get("backup_requirement", "not-required")),
                rollback_plan_sha256=str(release_evidence["rollback_plan_sha256"]),
                ci_result_sha256=str(release_evidence["ci_result_sha256"]),
                independent_review_sha256=str(release_evidence["independent_review_sha256"]),
                release_preflight_sha256=preflight_hash,
            )
            binding = ApprovalService.digest(approval_payload)
            existing = session.scalar(
                select(ApprovalCandidate).where(
                    ApprovalCandidate.change_set_id == change_set_id,
                    ApprovalCandidate.invalidated_at.is_(None),
                )
            )
            if existing is not None and existing.artifact_binding_sha256 != binding:
                existing.invalidated_at = datetime.now(UTC)
                existing = None
            if existing is not None and existing.workflow_id != workflow_id:
                raise ValueError("approval candidate belongs to another active deployment workflow")
            candidate_id = uuid5(NAMESPACE_URL, f"approval-candidate:{change_set_id}:{binding}")
            candidate_payload = {
                **approval_payload,
                "approval_type": environment,
                "artifact_binding_sha256": binding,
                "release_preflight": preflight,
                "deployment_id": payload["deployment_id"],
            }
            if existing is None:
                session.add(
                    ApprovalCandidate(
                        id=candidate_id,
                        change_set_id=change_set_id,
                        workflow_id=workflow_id,
                        risk_class=change_set.risk_class,
                        artifact_binding_sha256=binding,
                        payload_json=candidate_payload,
                        invalidated_at=None,
                    )
                )
            automatic_approval: dict[str, Any] | None = None
            if change_set.risk_class in {RiskClass.R0.value, RiskClass.R1.value}:
                approval_id = uuid5(
                    NAMESPACE_URL,
                    f"policy-{environment}-approval:{change_set_id}:{binding}",
                )
                persisted = session.get(Approval, approval_id)
                if persisted is None:
                    persisted = Approval(
                        id=approval_id,
                        change_set_id=change_set_id,
                        approval_type=environment,
                        decision="approve",
                        artifact_binding_sha256=binding,
                        approver_subject=f"policy:low-risk-{environment}",
                        approved_at=datetime.now(UTC),
                        invalidated_at=None,
                        metadata_json={
                            "candidate_id": str(candidate_id),
                            "automatic": True,
                            "risk_class": change_set.risk_class,
                        },
                    )
                    session.add(persisted)
                automatic_approval = {
                    "change_set_id": str(change_set_id),
                    "artifact_binding_sha256": binding,
                    "decision": "approve",
                    "typed_confirmation": None,
                    "approver_subject": persisted.approver_subject,
                }
            plan = self._object(payload.get("release_plan"), "release_plan")
            plan["approval_binding_sha256"] = binding
            if environment == "staging":
                change_set.state = ChangeSetState.AWAITING_STAGING_APPROVAL.value
                change_set.version += 1
            deployment = session.get(Deployment, UUID(str(payload["deployment_id"])))
            if deployment is not None:
                deployment.status = f"AWAITING_{environment.upper()}_APPROVAL"
            run.status = "AWAITING_APPROVAL"
            return {
                **payload,
                "status": f"AWAITING_{environment.upper()}_APPROVAL",
                "deployment_lease_id": str(deployment_lease.id),
                "deployment_component_key": deployment_lease.component_key,
                "artifact_binding_sha256": binding,
                "approval_candidate": candidate_payload,
                "requires_human_approval": automatic_approval is None,
                "automatic_approval": automatic_approval,
                "release_plan": plan,
            }

    def _validate_preview_approval(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._validate_deployment_approval(payload, "preview")

    def _validate_staging_approval(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._validate_deployment_approval(payload, "staging")

    def _validate_deployment_approval(
        self,
        payload: dict[str, Any],
        environment: Literal["preview", "staging"],
    ) -> dict[str, Any]:
        context = self._object(payload.get("context"), "context")
        approval = self._object(payload.get("approval"), "approval")
        change_set_id = UUID(str(context["change_set_id"]))
        binding = str(context["artifact_binding_sha256"])
        decision = str(approval.get("decision", ""))
        if (
            decision not in {"approve", "reject"}
            or approval.get("artifact_binding_sha256") != binding
        ):
            return {
                **context,
                "status": "BLOCKED",
                "blocker": f"{environment.upper()}_APPROVAL_REJECTED",
            }
        with self.sessions.begin() as session:
            candidate = session.scalar(
                select(ApprovalCandidate).where(
                    ApprovalCandidate.change_set_id == change_set_id,
                    ApprovalCandidate.artifact_binding_sha256 == binding,
                    ApprovalCandidate.invalidated_at.is_(None),
                )
            )
            persisted = session.scalar(
                select(Approval).where(
                    Approval.change_set_id == change_set_id,
                    Approval.artifact_binding_sha256 == binding,
                    Approval.decision == decision,
                    Approval.invalidated_at.is_(None),
                )
            )
            if candidate is None or persisted is None:
                raise ValueError(f"{environment} approval is not current authoritative metadata")
            change_set = session.get(ChangeSet, change_set_id)
            if change_set is None:
                raise ValueError("approved ChangeSet is missing")
            workflow_id = str(context["orchestration"]["workflow_id"])
            if candidate.workflow_id != workflow_id:
                raise ValueError("approval candidate belongs to another deployment workflow")
            if decision == "reject":
                now = datetime.now(UTC)
                candidate.invalidated_at = now
                change_set.state = ChangeSetState.REQUEST_CHANGES.value
                change_set.version += 1
                deployment = session.get(Deployment, UUID(str(context["deployment_id"])))
                if deployment is not None:
                    deployment.status = "REJECTED"
                    deployment.finished_at = now
                run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
                if run is not None:
                    run.status = "BLOCKED"
                    run.error_code = f"{environment.upper()}_APPROVAL_REJECTED"
                    run.finished_at = now
                    self._release_deployment_lease(session, change_set, run)
                return {
                    **context,
                    "status": "BLOCKED",
                    "blocker": f"{environment.upper()}_APPROVAL_REJECTED",
                }
            candidate_payload = self._object(candidate.payload_json, "approval candidate")
            current_payload = ApprovalService.artifact_payload(
                self._changeset_model(session, change_set),
                target_context=str(candidate_payload["target_context"]),
                target_namespace=str(candidate_payload["target_namespace"]),
                migration=bool(candidate_payload["migration"]),
                backup_requirement=str(candidate_payload["backup_requirement"]),
                rollback_plan_sha256=str(candidate_payload["rollback_plan_sha256"]),
                ci_result_sha256=str(candidate_payload["ci_result_sha256"]),
                independent_review_sha256=str(candidate_payload["independent_review_sha256"]),
                release_preflight_sha256=str(candidate_payload["release_preflight_sha256"]),
            )
            if ApprovalService.digest(current_payload) != binding:
                invalidated_at = datetime.now(UTC)
                candidate.invalidated_at = invalidated_at
                persisted.invalidated_at = invalidated_at
                change_set.state = ChangeSetState.REQUEST_CHANGES.value
                change_set.version += 1
                deployment = session.get(Deployment, UUID(str(context["deployment_id"])))
                if deployment is not None:
                    deployment.status = "APPROVAL_INVALIDATED"
                    deployment.finished_at = invalidated_at
                run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
                if run is not None:
                    run.status = "BLOCKED"
                    run.error_code = "APPROVAL_ARTIFACT_DRIFT"
                    run.finished_at = invalidated_at
                    self._release_deployment_lease(session, change_set, run)
                return {
                    **context,
                    "status": "BLOCKED",
                    "blocker": "APPROVAL_ARTIFACT_DRIFT",
                }
            if environment == "staging":
                change_set.state = ChangeSetState.STAGING_APPROVED.value
                change_set.version += 1
            deployment = session.get(Deployment, UUID(str(context["deployment_id"])))
            if deployment is not None:
                deployment.status = f"{environment.upper()}_APPROVED"
            return {
                **context,
                "status": f"{environment.upper()}_APPROVED",
                "approval_validation": {
                    "current": True,
                    "artifact_binding_sha256": binding,
                    "approval_id": str(persisted.id),
                    "approver_subject": persisted.approver_subject,
                },
            }

    def _finalize_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("status") != "PREVIEW_VERIFIED":
            raise ValueError("preview finalization requires deterministic verification success")
        change_set_id = UUID(str(payload["change_set_id"]))
        deployment_id = UUID(str(payload["deployment_id"]))
        workflow_id = str(payload["orchestration"]["workflow_id"])
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, change_set_id)
            deployment = session.get(Deployment, deployment_id)
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if change_set is None or deployment is None or run is None:
                raise ValueError("preview finalization metadata is incomplete")
            change_set.state = ChangeSetState.PREVIEW_VERIFIED.value
            change_set.version += 1
            deployment.status = "PREVIEW_VERIFIED"
            deployment.transaction_id = str(payload.get("transaction_id", "")) or None
            deployment.finished_at = datetime.now(UTC)
            deployment.recovery_json = {
                **dict(deployment.recovery_json),
                "deployed_artifact_binding_sha256": str(payload["artifact_binding_sha256"]),
                "release_plan": self._object(payload.get("release_plan"), "release_plan"),
                "release_preflight": self._object(
                    payload.get("release_preflight"), "release_preflight"
                ),
                "preview_verification": self._object(
                    payload.get("preview_verification"), "preview_verification"
                ),
            }
            run.status = "SUCCESS"
            run.finished_at = datetime.now(UTC)
            self._release_deployment_lease(session, change_set, run)
        return {**payload, "status": "PREVIEW_VERIFIED"}

    def _finalize_staging(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("status") != "DEPLOYED":
            raise ValueError("staging finalization requires deterministic deployment success")
        change_set_id = UUID(str(payload["change_set_id"]))
        deployment_id = UUID(str(payload["deployment_id"]))
        workflow_id = str(payload["orchestration"]["workflow_id"])
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, change_set_id)
            deployment = session.get(Deployment, deployment_id)
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if change_set is None or deployment is None or run is None:
                raise ValueError("staging finalization metadata is incomplete")
            change_set.state = ChangeSetState.STAGING_DEPLOYED.value
            change_set.version += 1
            deployment.status = "STAGING_DEPLOYED"
            deployment.transaction_id = str(payload.get("transaction_id", "")) or None
            deployment.finished_at = datetime.now(UTC)
            existing_recovery = dict(deployment.recovery_json)
            existing_recovery.update(
                {
                    "deployed_artifact_binding_sha256": str(payload["artifact_binding_sha256"]),
                    "release_plan": self._object(payload.get("release_plan"), "release_plan"),
                    "release_preflight": self._object(
                        payload.get("release_preflight"), "release_preflight"
                    ),
                    "release_evidence": self._object(
                        payload.get("release_evidence"), "release_evidence"
                    ),
                    "previous_deployment_sha": change_set.deployment_base_sha,
                }
            )
            deployment.recovery_json = existing_recovery
            run.status = "SUCCESS"
            run.finished_at = datetime.now(UTC)
            self._release_deployment_lease(session, change_set, run)
        return {**payload, "status": "STAGING_DEPLOYED"}

    def _record_deployment_terminal(self, payload: dict[str, Any]) -> dict[str, Any]:
        terminal = str(payload.get("terminal_status", ""))
        run_status = {
            "PREVIEW_PREFLIGHT_VALIDATED": "SUCCESS",
            "STAGING_PREFLIGHT_VALIDATED": "SUCCESS",
            "BLOCKED": "BLOCKED",
            "CANCELLED": "CANCELLED",
            "DEPLOYMENT_FAILED": "FAILED",
        }.get(terminal)
        if run_status is None:
            raise ValueError("deployment terminal status is not allowlisted")
        workflow_id = str(payload["orchestration"]["workflow_id"])
        change_set_id = UUID(str(payload["change_set_id"]))
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if run is None or run.change_set_id != change_set_id:
                raise ValueError("deployment terminal Run binding is missing")
            run.status = run_status
            run.finished_at = now
            blocker = payload.get("blocker")
            run.error_code = str(blocker) if blocker else None
            deployment_id = payload.get("deployment_id")
            if deployment_id:
                deployment = session.get(Deployment, UUID(str(deployment_id)))
                if deployment is None or deployment.change_set_id != change_set_id:
                    raise ValueError("deployment terminal metadata binding is missing")
                deployment.status = (
                    "PREFLIGHT_VALIDATED"
                    if run_status == "SUCCESS"
                    else "DEPLOYMENT_FAILED"
                    if terminal == "DEPLOYMENT_FAILED"
                    else run_status
                )
                deployment.finished_at = now
            if terminal in {"BLOCKED", "CANCELLED", "DEPLOYMENT_FAILED"}:
                candidate = session.scalar(
                    select(ApprovalCandidate).where(
                        ApprovalCandidate.change_set_id == change_set_id,
                        ApprovalCandidate.invalidated_at.is_(None),
                    )
                )
                if candidate is not None and candidate.workflow_id == workflow_id:
                    candidate.invalidated_at = now
                change_set = session.get(ChangeSet, change_set_id)
                if change_set is not None:
                    self._release_deployment_lease(session, change_set, run)
                    mutation_attempted = (
                        terminal == "DEPLOYMENT_FAILED"
                        and payload.get("environment") == "staging"
                        and isinstance(payload.get("approval_validation"), dict)
                        and payload["approval_validation"].get("current") is True
                    )
                    if mutation_attempted:
                        change_set.state = ChangeSetState.DEPLOYMENT_FAILED.value
                        change_set.version += 1
                        if deployment_id:
                            deployment = session.get(Deployment, UUID(str(deployment_id)))
                            if deployment is not None:
                                recovery = dict(deployment.recovery_json)
                                recovery.update(
                                    {
                                        "deployed_artifact_binding_sha256": str(
                                            payload.get("artifact_binding_sha256", "")
                                        ),
                                        "release_plan": self._object(
                                            payload.get("release_plan"), "release_plan"
                                        ),
                                        "release_preflight": self._object(
                                            payload.get("release_preflight"),
                                            "release_preflight",
                                        ),
                                        "release_evidence": self._object(
                                            payload.get("release_evidence"),
                                            "release_evidence",
                                        ),
                                        "previous_deployment_sha": (change_set.deployment_base_sha),
                                    }
                                )
                                deployment.recovery_json = recovery
                                incident = self._ensure_recovery_incident(
                                    session, change_set, deployment
                                )
                                incident.recovery_state = "DEPLOYMENT_FAILED"
        return {**payload, "status": terminal}

    def _runtime_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        deployment_id = UUID(str(payload["deployment_id"]))
        workflow_id = str(payload["orchestration"]["workflow_id"])
        with self.sessions.begin() as session:
            deployment = session.get(Deployment, deployment_id)
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if (
                deployment is None
                or run is None
                or run.change_set_id != deployment.change_set_id
                or run.summary != str(deployment_id)
            ):
                raise ValueError("runtime verification deployment/Run binding is missing")
            change_set = session.get(ChangeSet, deployment.change_set_id)
            if change_set is None or change_set.project != deployment.project:
                raise ValueError("runtime verification ChangeSet binding is missing")
            target = self._runtime_target(Project(deployment.project), deployment.environment)
            if not target.enabled:
                return self._runtime_block(
                    payload,
                    run,
                    target.blocker or "RUNTIME_TARGET_DISABLED",
                    project=deployment.project,
                    change_set_id=change_set.id,
                )
            if (
                deployment.status not in {"STAGING_DEPLOYED", "VERIFICATION_FAILED"}
                or change_set.deployment_head_sha != deployment.desired_sha
            ):
                return self._runtime_block(
                    payload,
                    run,
                    "RUNTIME_ARTIFACT_IDENTITY_MISMATCH",
                    project=deployment.project,
                    change_set_id=change_set.id,
                )
            provider_chain = self._object(
                change_set.recovery_json.get("provider_chain"), "provider_chain"
            )
            maker_value = provider_chain.get("maker")
            if maker_value not in {Provider.CODEX.value, Provider.CLAUDE.value}:
                return self._runtime_block(
                    payload,
                    run,
                    "IMPLEMENTATION_PROVIDER_PROVENANCE_MISSING",
                    project=deployment.project,
                    change_set_id=change_set.id,
                )
            maker = Provider(str(maker_value))
            routing = self._quota_broker().route(
                role="runtime-verifier",
                model_class=ModelClass.JUDGE,
                risk_class=RiskClass(change_set.risk_class),
                maker=maker,
            )
            if routing.primary is None:
                return self._runtime_block(
                    payload,
                    run,
                    routing.status,
                    project=deployment.project,
                    change_set_id=change_set.id,
                )
            reviewers = [routing.primary]
            if change_set.risk_class == RiskClass.R3.value:
                if routing.checker is None:
                    return self._runtime_block(
                        payload,
                        run,
                        "R3_RUNTIME_CROSS_PROVIDER_REQUIRED",
                        project=deployment.project,
                        change_set_id=change_set.id,
                    )
                reviewers.append(routing.checker)
            candidate = session.scalar(
                select(ApprovalCandidate).where(
                    ApprovalCandidate.change_set_id == change_set.id,
                    ApprovalCandidate.invalidated_at.is_(None),
                )
            )
            approval = (
                session.scalar(
                    select(Approval).where(
                        Approval.change_set_id == change_set.id,
                        Approval.artifact_binding_sha256 == candidate.artifact_binding_sha256,
                        Approval.decision == "approve",
                        Approval.invalidated_at.is_(None),
                    )
                )
                if candidate is not None
                else None
            )
            if candidate is None or approval is None:
                return self._runtime_block(
                    payload,
                    run,
                    "CURRENT_STAGING_APPROVAL_MISSING",
                    project=deployment.project,
                    change_set_id=change_set.id,
                )
            if not self._candidate_binding_is_current(session, change_set, candidate):
                return self._runtime_block(
                    payload,
                    run,
                    "RUNTIME_APPROVAL_ARTIFACT_DRIFT",
                    project=deployment.project,
                    change_set_id=change_set.id,
                )
            leases = session.scalars(
                select(WorktreeLease).where(
                    WorktreeLease.change_set_id == change_set.id,
                    WorktreeLease.released_at.is_(None),
                )
            ).all()
            paths = {lease.repository: lease.worktree_path for lease in leases}
            if (
                change_set.source_repository not in paths
                or change_set.deployment_repository not in paths
            ):
                return self._runtime_block(
                    payload,
                    run,
                    "RUNTIME_WORKTREE_LEASES_MISSING",
                    project=deployment.project,
                    change_set_id=change_set.id,
                )
            for lease in leases:
                if lease.owner_run_id == run.id:
                    continue
                owner = session.get(Run, lease.owner_run_id)
                if (
                    owner is not None
                    and owner.finished_at is None
                    and owner.status
                    in {
                        "QUEUED",
                        "RUNNING",
                        "AWAITING_APPROVAL",
                    }
                ):
                    return self._runtime_block(
                        payload,
                        run,
                        "RUNTIME_WORKTREE_LEASE_CONFLICT",
                        project=deployment.project,
                        change_set_id=change_set.id,
                    )
            for lease in leases:
                lease.owner_run_id = run.id
                lease.expires_at = datetime.now(UTC) + timedelta(hours=8)
            configuration = target.configuration
            run.status = "RUNNING"
            run.started_at = run.started_at or datetime.now(UTC)
            output_directory = (
                self.settings.browser_evidence_root / str(deployment_id) / str(run.id)
            )
            return {
                **payload,
                "status": "RUNTIME_SNAPSHOT_READY",
                "project": deployment.project,
                "change_set_id": str(change_set.id),
                "risk_class": change_set.risk_class,
                "source_worktree": paths[change_set.source_repository],
                "deployment_worktree": paths[change_set.deployment_repository],
                "expected_source_sha": change_set.source_head_sha,
                "expected_deployment_sha": deployment.desired_sha,
                "expected_image_digests": change_set.image_digests,
                "artifact_binding_sha256": candidate.artifact_binding_sha256,
                "review_providers": [provider.value for provider in reviewers],
                "runtime_observation_plan": {
                    "project": deployment.project,
                    "deployment_id": str(deployment.id),
                    "change_set_id": str(change_set.id),
                    "kubeconfig_path": configuration["observer_kubeconfig_path"],
                    "kube_context": target.target_context,
                    "namespace": target.target_namespace,
                    "resources": configuration["observed_resources"],
                    "source_worktree": paths[change_set.source_repository],
                    "deployment_worktree": paths[change_set.deployment_repository],
                    "expected_source_sha": change_set.source_head_sha,
                    "expected_deployment_sha": deployment.desired_sha,
                    "expected_image_digests": change_set.image_digests,
                    "revision_annotation": configuration.get("revision_annotation"),
                    "argo_application": configuration.get("argo_application"),
                },
                "journey_plan_path": configuration["journey_plan_path"],
                "credential_file": None,
                "output_directory": str(output_directory),
            }

    def _record_runtime_failure(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(payload["orchestration"]["workflow_id"])
        deployment_id = UUID(str(payload["deployment_id"]))
        blocker = str(payload.get("blocker", "RUNTIME_ACTIVITY_FAILED"))
        allowed = {
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
            "RUNTIME_REVIEW_FAILED",
            "RUNTIME_ACTIVITY_FAILED",
            "CANCELLED",
        }
        if blocker not in allowed:
            blocker = "RUNTIME_ACTIVITY_FAILED"
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            deployment = session.get(Deployment, deployment_id)
            if run is None or deployment is None or run.change_set_id != deployment.change_set_id:
                raise ValueError("runtime failure deployment/Run binding is missing")
            if run.finished_at is None:
                run.status = (
                    "RATE_LIMITED"
                    if blocker == "RATE_LIMITED"
                    else "CANCELLED"
                    if blocker == "CANCELLED"
                    else "BLOCKED"
                )
                run.error_code = blocker
                run.finished_at = datetime.now(UTC)
        return {**payload, "status": run.status, "blocker": blocker}

    def _prepare_runtime_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        observation = self._object(payload.get("runtime_observation"), "runtime_observation")
        browser_result = self._object(payload.get("result"), "browser result")
        providers = payload.get("review_providers")
        if not isinstance(providers, list) or not providers:
            raise ValueError("runtime review providers are missing")
        workflow_id = self._workflow_id(payload)
        requests: list[dict[str, Any]] = []
        for provider_value in providers:
            provider = Provider(str(provider_value))
            prompt = (
                "Independently judge one deployed ChangeSet using only the supplied redacted "
                "observer and browser summaries. Do not modify files, access Kubernetes, read "
                "credentials, deploy, merge, or approve. Require exact approved revision and "
                "immutable image digests; recommend an incident on any failed invariant.\n\n"
                f"Artifact binding: {payload['artifact_binding_sha256']}\n"
                f"Expected deployment SHA: {payload['expected_deployment_sha']}\n"
                "Expected image digests: "
                f"{json.dumps(payload['expected_image_digests'], sort_keys=True)}\n"
                f"Observer summary: {json.dumps(observation, sort_keys=True)}\n"
                f"Browser summary: {json.dumps(browser_result, sort_keys=True)}\n"
            )
            request = AgentRequest(
                run_id=uuid5(
                    NAMESPACE_URL,
                    f"runtime-verifier:{workflow_id}:{payload['deployment_id']}:{provider.value}",
                ),
                project=Project(str(payload["project"])),
                role="runtime-verifier",
                provider=provider,
                model_class=ModelClass.JUDGE,
                prompt=prompt,
                working_directory=str(payload["source_worktree"]),
                output_schema=self._runtime_verification_output_schema(),
                timeout_seconds=3600,
                effort="high",
                writable=False,
                change_set_id=UUID(str(payload["change_set_id"])),
            )
            requests.append(request.model_dump(mode="json"))
        return {
            **payload,
            "status": "RUNTIME_REVIEW_READY",
            "runtime_review_requests": requests,
        }

    def _finalize_runtime(self, payload: dict[str, Any]) -> dict[str, Any]:
        deployment_id = UUID(str(payload["deployment_id"]))
        change_set_id = UUID(str(payload["change_set_id"]))
        workflow_id = str(payload["orchestration"]["workflow_id"])
        observation = self._object(payload.get("runtime_observation"), "runtime_observation")
        browser_result = self._object(payload.get("result"), "browser result")
        reviews = payload.get("runtime_review_results")
        if not isinstance(reviews, list) or not reviews:
            raise ValueError("runtime verification requires independent review results")
        structured_reviews: list[dict[str, Any]] = []
        providers: list[str] = []
        reviews_pass = True
        expected_revision = str(payload["expected_deployment_sha"])
        expected_digests = set(
            self._object(payload.get("expected_image_digests"), "expected image digests").values()
        )
        for review in reviews:
            outer = self._object(review, "runtime review")
            agent = self._object(outer.get("agent_result"), "runtime agent result")
            structured = self._object(agent.get("structured_result"), "runtime structured result")
            providers.append(str(agent.get("provider", "unknown")))
            structured_reviews.append(structured)
            observed_digests = self._object(
                structured.get("observed_digests"), "runtime observed digests"
            )
            checks = structured.get("checks")
            claims_match = (
                structured.get("observed_revision") == expected_revision
                and expected_digests.issubset(set(observed_digests.values()))
                and isinstance(checks, list)
                and bool(checks)
                and all(
                    isinstance(check, dict) and check.get("status") == "PASS" for check in checks
                )
                and structured.get("incident_required") is False
            )
            if (
                outer.get("status") != "SUCCESS"
                or structured.get("decision") != "RUNTIME_VERIFIED"
                or not claims_match
            ):
                reviews_pass = False
        expected_reviewers = payload.get("review_providers")
        if (
            not isinstance(expected_reviewers, list)
            or providers != expected_reviewers
            or len(set(providers)) != len(providers)
            or (payload.get("risk_class") == RiskClass.R3.value and len(providers) != 2)
        ):
            reviews_pass = False
        passed = (
            observation.get("status") == "RUNTIME_OBSERVATION_PASS"
            and browser_result.get("status") == "PASS"
            and reviews_pass
        )
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            deployment = session.get(Deployment, deployment_id)
            change_set = session.get(ChangeSet, change_set_id)
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if (
                deployment is None
                or change_set is None
                or run is None
                or deployment.change_set_id != change_set_id
                or run.change_set_id != change_set_id
            ):
                raise ValueError("runtime finalization metadata binding is incomplete")
            if run.finished_at is not None:
                return {
                    **payload,
                    "status": "RUNTIME_VERIFIED" if run.status == "SUCCESS" else run.status,
                }
            for provider, structured in zip(providers, structured_reviews, strict=True):
                invocation_id = uuid5(
                    NAMESPACE_URL,
                    f"runtime-invocation:{run.id}:{provider}",
                )
                if session.get(AgentInvocation, invocation_id) is None:
                    session.add(
                        AgentInvocation(
                            id=invocation_id,
                            run_id=run.id,
                            provider=provider,
                            role="runtime-verifier",
                            model_class=ModelClass.JUDGE.value,
                            provider_session_id=None,
                            status=str(structured.get("decision", "BLOCKED")),
                            summary=str(structured.get("summary", "runtime verification")),
                            structured_result=structured,
                            error_code=None,
                            started_at=run.started_at or now,
                            finished_at=now,
                        )
                    )
            verification_id = uuid5(
                NAMESPACE_URL,
                f"runtime-verification:{deployment_id}:{workflow_id}",
            )
            if session.get(VerificationResult, verification_id) is None:
                session.add(
                    VerificationResult(
                        id=verification_id,
                        change_set_id=change_set_id,
                        deployment_id=deployment_id,
                        run_id=run.id,
                        verification_type="post-staging-runtime",
                        verifier_provider=",".join(providers),
                        status="RUNTIME_VERIFIED" if passed else "FAIL",
                        evidence_json={
                            "observation": observation,
                            "browser": browser_result,
                            "reviews": structured_reviews,
                        },
                    )
                )
            run.finished_at = now
            if passed:
                run.status = "SUCCESS"
                run.summary = "deterministic, browser, and independent runtime checks passed"
                deployment.status = "RUNTIME_VERIFIED"
                deployment.finished_at = now
                if change_set.state != ChangeSetState.RUNTIME_VERIFIED.value:
                    change_set.state = ChangeSetState.RUNTIME_VERIFIED.value
                    change_set.version += 1
                return {**payload, "status": "RUNTIME_VERIFIED"}
            run.status = "FAILED"
            run.error_code = "RUNTIME_VERIFICATION_FAILED"
            deployment.status = "VERIFICATION_FAILED"
            if change_set.state != ChangeSetState.INCIDENT_OPEN.value:
                change_set.state = ChangeSetState.INCIDENT_OPEN.value
                change_set.version += 1
            incident_id = uuid5(NAMESPACE_URL, f"runtime-incident:{deployment_id}:{workflow_id}")
            if session.get(Incident, incident_id) is None:
                session.add(
                    Incident(
                        id=incident_id,
                        change_set_id=change_set_id,
                        deployment_id=deployment_id,
                        project=change_set.project,
                        severity="high" if change_set.risk_class in {"R2", "R3"} else "medium",
                        status="OPEN",
                        summary="post-staging runtime verification failed",
                        recovery_state="AWAITING_RECOVERY_DECISION",
                        opened_at=now,
                        closed_at=None,
                    )
                )
            return {**payload, "status": "INCIDENT_OPEN"}

    def _inspect_recovery(self, payload: dict[str, Any]) -> dict[str, Any]:
        deployment_id = UUID(str(payload["deployment_id"]))
        workflow_id = str(payload["orchestration"]["workflow_id"])
        expected_binding = str(payload["expected_artifact_binding_sha256"])
        with self.sessions.begin() as session:
            deployment = session.get(Deployment, deployment_id)
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if (
                deployment is None
                or run is None
                or run.change_set_id != deployment.change_set_id
                or run.summary != str(deployment_id)
            ):
                raise ValueError("recovery deployment/Run binding is missing")
            change_set = session.get(ChangeSet, deployment.change_set_id)
            if change_set is None or change_set.project != deployment.project:
                raise ValueError("recovery ChangeSet binding is missing")
            recovery = self._object(deployment.recovery_json, "deployment recovery metadata")
            stored_binding = recovery.get("deployed_artifact_binding_sha256")
            if not isinstance(stored_binding, str) or stored_binding != expected_binding:
                return self._recovery_block(
                    payload,
                    run,
                    "RECOVERY_ARTIFACT_BINDING_MISMATCH",
                    deployment=deployment,
                    change_set=change_set,
                )
            approval = session.scalar(
                select(Approval).where(
                    Approval.change_set_id == change_set.id,
                    Approval.approval_type == "staging",
                    Approval.artifact_binding_sha256 == expected_binding,
                    Approval.decision == "approve",
                    Approval.invalidated_at.is_(None),
                )
            )
            if approval is None or not self._stored_staging_binding_is_current(
                session, change_set, deployment, recovery, expected_binding
            ):
                return self._recovery_block(
                    payload,
                    run,
                    "RECOVERY_ORIGINAL_APPROVAL_INVALID",
                    deployment=deployment,
                    change_set=change_set,
                )
            release_plan = self._object(recovery.get("release_plan"), "stored release plan")
            preflight = self._object(recovery.get("release_preflight"), "stored release preflight")
            evidence = self._object(recovery.get("release_evidence"), "stored release evidence")
            plan: dict[str, Any]
            action: str
            blocker: str | None = None
            if deployment.project == Project.FALCONE.value:
                if bool(evidence.get("migration", False)):
                    action = "forward-recovery"
                    blocker = "MIGRATION_FORWARD_RECOVERY_REQUIRED"
                    plan = {
                        "strategy": "helm-forward-recovery",
                        "deployment_id": str(deployment.id),
                        "change_set_id": str(change_set.id),
                        "reason": blocker,
                        "original_artifact_binding_sha256": expected_binding,
                    }
                else:
                    revision = preflight.get("previous_revision")
                    manifest_sha = preflight.get("previous_manifest_sha256")
                    if (
                        not isinstance(revision, int)
                        or revision < 1
                        or not self._is_digest(manifest_sha)
                    ):
                        return self._recovery_block(
                            payload,
                            run,
                            "RECOVERY_BASELINE_MISSING",
                            deployment=deployment,
                            change_set=change_set,
                        )
                    action = "helm-rollback"
                    plan = {
                        "strategy": "helm",
                        "deployment_id": str(deployment.id),
                        "change_set_id": str(change_set.id),
                        "release_name": str(release_plan["release_name"]),
                        "namespace": deployment.target_namespace,
                        "kube_context": str(release_plan["kube_context"]),
                        "kubeconfig_path": str(release_plan["kubeconfig_path"]),
                        "previous_revision": revision,
                        "previous_manifest_sha256": str(manifest_sha),
                        "original_artifact_binding_sha256": expected_binding,
                        "recovery_approval_binding_sha256": "0" * 64,
                        "timeout_minutes": int(release_plan.get("timeout_minutes", 30)),
                    }
            else:
                action = "gitops-revert-pr"
                blocker = "PROTECTED_RECOVERY_PR_REQUIRED"
                plan = {
                    "strategy": "argocd-gitops",
                    "deployment_id": str(deployment.id),
                    "change_set_id": str(change_set.id),
                    "deployment_repository": change_set.deployment_repository,
                    "failed_revision": deployment.desired_sha,
                    "revert_target_revision": change_set.deployment_base_sha,
                    "target_context": deployment.target_context,
                    "target_namespace": deployment.target_namespace,
                    "original_artifact_binding_sha256": expected_binding,
                    "required_path": "protected-deployment-repository-pr",
                }
            run.status = "RUNNING"
            run.started_at = run.started_at or datetime.now(UTC)
            return {
                **payload,
                "project": deployment.project,
                "change_set_id": str(change_set.id),
                "risk_class": change_set.risk_class,
                "action": action,
                "status": "RECOVERY_INSPECTED",
                "recovery_plan": plan,
                "recovery_blocker": blocker,
            }

    def _validate_recovery_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(payload["orchestration"]["workflow_id"])
        plan = self._object(payload.get("recovery_plan"), "recovery plan")
        canonical_plan = {
            key: value for key, value in plan.items() if key != "recovery_approval_binding_sha256"
        }
        plan_sha = hashlib.sha256(
            json.dumps(canonical_plan, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if run is None:
                raise ValueError("recovery Run binding is missing")
            blocker = payload.get("recovery_blocker")
            if isinstance(blocker, str):
                deployment = session.get(Deployment, UUID(str(payload["deployment_id"])))
                change_set = session.get(ChangeSet, run.change_set_id)
                if deployment is None or change_set is None:
                    raise ValueError("forward recovery metadata is missing")
                run.status = "BLOCKED"
                run.error_code = blocker
                run.finished_at = datetime.now(UTC)
                change_set.state = ChangeSetState.FORWARD_RECOVERY_REQUIRED.value
                change_set.version += 1
                incident = self._ensure_recovery_incident(session, change_set, deployment)
                incident.recovery_state = blocker
                return {
                    **payload,
                    "status": "FORWARD_RECOVERY_REQUIRED",
                    "recovery_plan_sha256": plan_sha,
                    "blocker": blocker,
                }
            if bool(payload.get("dry_run", True)):
                run.status = "SUCCESS"
                run.summary = "immutable recovery plan validated without mutation"
                run.finished_at = datetime.now(UTC)
                return {
                    **payload,
                    "status": "RECOVERY_PLAN_VALIDATED",
                    "recovery_plan_sha256": plan_sha,
                }
            return {
                **payload,
                "status": "RECOVERY_BINDING_VALIDATED",
                "recovery_plan_sha256": plan_sha,
            }

    def _prepare_recovery_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(payload["orchestration"]["workflow_id"])
        change_set_id = UUID(str(payload["change_set_id"]))
        deployment_id = UUID(str(payload["deployment_id"]))
        plan = self._object(payload.get("recovery_plan"), "recovery plan")
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            change_set = session.get(ChangeSet, change_set_id)
            deployment = session.get(Deployment, deployment_id)
            if (
                run is None
                or change_set is None
                or deployment is None
                or run.change_set_id != change_set_id
                or deployment.change_set_id != change_set_id
            ):
                raise ValueError("recovery candidate metadata binding is missing")
            lease = ComponentLeaseManager().acquire(
                session,
                component_key=f"{change_set.project}:deploy:{deployment.environment}",
                owner_run_id=run.id,
                mode="deployment",
                ttl=timedelta(hours=4),
            )
            current = session.scalar(
                select(ApprovalCandidate).where(
                    ApprovalCandidate.change_set_id == change_set_id,
                    ApprovalCandidate.invalidated_at.is_(None),
                )
            )
            if current is not None:
                current.invalidated_at = datetime.now(UTC)
            approval_payload = {
                "approval_type": "recovery",
                "project": change_set.project,
                "change_set_id": str(change_set.id),
                "deployment_id": str(deployment.id),
                "risk_class": change_set.risk_class,
                "recovery_action": str(payload["action"]),
                "recovery_plan_sha256": str(payload["recovery_plan_sha256"]),
                "original_artifact_binding_sha256": str(
                    payload["expected_artifact_binding_sha256"]
                ),
                "target_context": deployment.target_context,
                "target_namespace": deployment.target_namespace,
            }
            binding = ApprovalService.digest(approval_payload)
            bound_plan = dict(plan)
            bound_plan["recovery_approval_binding_sha256"] = binding
            candidate_id = uuid5(NAMESPACE_URL, f"recovery-approval:{deployment.id}:{binding}")
            session.add(
                ApprovalCandidate(
                    id=candidate_id,
                    change_set_id=change_set.id,
                    workflow_id=workflow_id,
                    risk_class=change_set.risk_class,
                    artifact_binding_sha256=binding,
                    payload_json={
                        **approval_payload,
                        "artifact_binding_sha256": binding,
                    },
                    invalidated_at=None,
                )
            )
            run.status = "AWAITING_APPROVAL"
            return {
                **payload,
                "status": "AWAITING_RECOVERY_APPROVAL",
                "artifact_binding_sha256": binding,
                "recovery_plan": bound_plan,
                "approval_candidate": {
                    **approval_payload,
                    "artifact_binding_sha256": binding,
                },
                "deployment_lease_id": str(lease.id),
            }

    def _validate_recovery_approval(self, payload: dict[str, Any]) -> dict[str, Any]:
        context = self._object(payload.get("context"), "recovery context")
        approval_signal = self._object(payload.get("approval"), "recovery approval")
        workflow_id = str(context["orchestration"]["workflow_id"])
        binding = str(context["artifact_binding_sha256"])
        decision = str(approval_signal.get("decision", ""))
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            change_set = session.get(ChangeSet, UUID(str(context["change_set_id"])))
            deployment = session.get(Deployment, UUID(str(context["deployment_id"])))
            candidate = session.scalar(
                select(ApprovalCandidate).where(
                    ApprovalCandidate.workflow_id == workflow_id,
                    ApprovalCandidate.artifact_binding_sha256 == binding,
                    ApprovalCandidate.invalidated_at.is_(None),
                )
            )
            if run is None or change_set is None or deployment is None or candidate is None:
                raise ValueError("recovery approval metadata binding is missing")
            persisted = session.scalar(
                select(Approval).where(
                    Approval.change_set_id == change_set.id,
                    Approval.approval_type == "recovery",
                    Approval.artifact_binding_sha256 == binding,
                    Approval.decision == decision,
                    Approval.invalidated_at.is_(None),
                )
            )
            candidate_payload = self._object(candidate.payload_json, "recovery candidate")
            digest_payload = {
                key: value
                for key, value in candidate_payload.items()
                if key != "artifact_binding_sha256"
            }
            recovery = self._object(deployment.recovery_json, "deployment recovery metadata")
            recovery_plan = self._object(context.get("recovery_plan"), "recovery plan")
            canonical_plan = {
                key: value
                for key, value in recovery_plan.items()
                if key != "recovery_approval_binding_sha256"
            }
            current_plan_sha = hashlib.sha256(
                json.dumps(canonical_plan, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest()
            original_binding = str(candidate_payload.get("original_artifact_binding_sha256", ""))
            artifacts_current = (
                recovery.get("deployed_artifact_binding_sha256") == original_binding
                and candidate_payload.get("recovery_plan_sha256") == current_plan_sha
                and candidate_payload.get("target_context") == deployment.target_context
                and candidate_payload.get("target_namespace") == deployment.target_namespace
                and self._stored_staging_binding_is_current(
                    session,
                    change_set,
                    deployment,
                    recovery,
                    original_binding,
                )
            )
            current = (
                decision in {"approve", "reject"}
                and approval_signal.get("artifact_binding_sha256") == binding
                and ApprovalService.digest(digest_payload) == binding
                and artifacts_current
            )
            if persisted is None or not current or decision == "reject":
                invalidated_at = datetime.now(UTC)
                candidate.invalidated_at = invalidated_at
                if persisted is not None and not artifacts_current:
                    persisted.invalidated_at = invalidated_at
                blocker = (
                    "RECOVERY_APPROVAL_REJECTED"
                    if decision == "reject"
                    else "RECOVERY_APPROVAL_ARTIFACT_DRIFT"
                    if not artifacts_current
                    else "RECOVERY_APPROVAL_INVALID"
                )
                run.status = "BLOCKED"
                run.error_code = blocker
                run.finished_at = invalidated_at
                self._release_recovery_lease(session, change_set, deployment, run)
                return {
                    **context,
                    "status": "BLOCKED",
                    "blocker": blocker,
                }
            assert persisted is not None
            return {
                **context,
                "status": "RECOVERY_APPROVED",
                "approval_validation": {
                    "current": True,
                    "artifact_binding_sha256": binding,
                    "approval_id": str(persisted.id),
                    "approver_subject": persisted.approver_subject,
                },
            }

    def _verify_recovery(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("status") != "ROLLBACK_APPLIED":
            raise ValueError("recovery verification requires deterministic rollback success")
        plan = self._object(payload.get("recovery_plan"), "recovery plan")
        if payload.get("manifest_sha256") != plan.get("previous_manifest_sha256"):
            raise ValueError("recovery manifest does not match its captured baseline")
        workflow_id = str(payload["orchestration"]["workflow_id"])
        deployment_id = UUID(str(payload["deployment_id"]))
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            deployment = session.get(Deployment, deployment_id)
            change_set = session.get(ChangeSet, deployment.change_set_id) if deployment else None
            if run is None or deployment is None or change_set is None:
                raise ValueError("recovery verification metadata binding is missing")
            run.status = "SUCCESS"
            run.summary = "exact captured Helm manifest restored; runtime retest required"
            run.finished_at = now
            deployment.status = "ROLLBACK_APPLIED"
            deployment.finished_at = now
            recovery = dict(deployment.recovery_json)
            recovery["last_recovery_result"] = {
                "recovery_plan_sha256": str(payload["recovery_plan_sha256"]),
                "manifest_sha256": str(payload["manifest_sha256"]),
                "transaction_sha256": str(payload["transaction_sha256"]),
                "verified_at": now.isoformat(),
            }
            deployment.recovery_json = recovery
            change_set.state = ChangeSetState.ROLLED_BACK.value
            change_set.version += 1
            candidate = session.scalar(
                select(ApprovalCandidate).where(
                    ApprovalCandidate.workflow_id == workflow_id,
                    ApprovalCandidate.invalidated_at.is_(None),
                )
            )
            if candidate is not None:
                candidate.invalidated_at = now
            incident = self._ensure_recovery_incident(session, change_set, deployment)
            incident.recovery_state = "RUNTIME_RETEST_REQUIRED"
            self._release_recovery_lease(session, change_set, deployment, run)
        return {**payload, "status": "ROLLED_BACK", "runtime_retest_required": True}

    def _record_recovery_failure(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(payload["orchestration"]["workflow_id"])
        deployment_id = UUID(str(payload["deployment_id"]))
        blocker = str(payload.get("blocker", "RECOVERY_ACTIVITY_FAILED"))
        allowed = {
            "CANCELLED",
            "ApprovalMismatch",
            "PolicyViolation",
            "ProtectedRecoveryPRRequired",
            "RECOVERY_APPROVAL_REJECTED",
            "RECOVERY_ACTIVITY_FAILED",
            "RECOVERY_DEPLOYMENT_LEASE_CONFLICT",
        }
        if blocker not in allowed:
            blocker = "RECOVERY_ACTIVITY_FAILED"
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            deployment = session.get(Deployment, deployment_id)
            change_set = session.get(ChangeSet, deployment.change_set_id) if deployment else None
            if run is None or deployment is None or change_set is None:
                raise ValueError("recovery failure metadata binding is missing")
            if run.finished_at is None:
                now = datetime.now(UTC)
                run.status = "CANCELLED" if blocker == "CANCELLED" else "BLOCKED"
                run.error_code = blocker
                run.finished_at = now
                if blocker not in {"CANCELLED", "RECOVERY_DEPLOYMENT_LEASE_CONFLICT"}:
                    deployment.status = "RECOVERY_FAILED"
                    change_set.state = ChangeSetState.INCIDENT_OPEN.value
                    change_set.version += 1
                    incident = self._ensure_recovery_incident(session, change_set, deployment)
                    incident.recovery_state = blocker
                candidate = session.scalar(
                    select(ApprovalCandidate).where(
                        ApprovalCandidate.workflow_id == workflow_id,
                        ApprovalCandidate.invalidated_at.is_(None),
                    )
                )
                if candidate is not None:
                    candidate.invalidated_at = now
                self._release_recovery_lease(session, change_set, deployment, run)
        return {**payload, "status": run.status, "blocker": blocker}

    def _stored_staging_binding_is_current(
        self,
        session: Any,
        change_set: ChangeSet,
        deployment: Deployment,
        recovery: dict[str, Any],
        expected_binding: str,
    ) -> bool:
        evidence = self._object(recovery.get("release_evidence"), "stored release evidence")
        preflight = self._object(recovery.get("release_preflight"), "stored release preflight")
        preflight_sha = hashlib.sha256(
            json.dumps(preflight, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        current = ApprovalService.artifact_payload(
            self._changeset_model(session, change_set),
            target_context=deployment.target_context,
            target_namespace=deployment.target_namespace,
            migration=bool(evidence.get("migration", False)),
            backup_requirement=str(evidence.get("backup_requirement", "not-required")),
            rollback_plan_sha256=str(evidence["rollback_plan_sha256"]),
            ci_result_sha256=str(evidence["ci_result_sha256"]),
            independent_review_sha256=str(evidence["independent_review_sha256"]),
            release_preflight_sha256=preflight_sha,
        )
        return ApprovalService.digest(current) == expected_binding

    def _recovery_block(
        self,
        payload: dict[str, Any],
        run: Run,
        blocker: str,
        *,
        deployment: Deployment,
        change_set: ChangeSet,
    ) -> dict[str, Any]:
        run.status = "BLOCKED"
        run.error_code = blocker
        run.finished_at = datetime.now(UTC)
        return {
            **payload,
            "project": deployment.project,
            "change_set_id": str(change_set.id),
            "status": "BLOCKED",
            "blocker": blocker,
        }

    @staticmethod
    def _ensure_recovery_incident(
        session: Any, change_set: ChangeSet, deployment: Deployment
    ) -> Incident:
        incident = session.scalar(
            select(Incident).where(
                Incident.deployment_id == deployment.id,
                Incident.status == "OPEN",
            )
        )
        if incident is None:
            incident = Incident(
                id=uuid5(NAMESPACE_URL, f"recovery-incident:{deployment.id}"),
                change_set_id=change_set.id,
                deployment_id=deployment.id,
                project=change_set.project,
                severity="high" if change_set.risk_class in {"R2", "R3"} else "medium",
                status="OPEN",
                summary="deployment recovery requires operator attention",
                recovery_state="AWAITING_RECOVERY_DECISION",
                opened_at=datetime.now(UTC),
                closed_at=None,
            )
            session.add(incident)
        return cast(Incident, incident)

    @staticmethod
    def _release_recovery_lease(
        session: Any, change_set: ChangeSet, deployment: Deployment, run: Run
    ) -> None:
        ComponentLeaseManager().release(
            session,
            component_key=f"{change_set.project}:deploy:{deployment.environment}",
            owner_run_id=run.id,
        )

    def _discover_cleanup(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("retention_policy") != "standard":
            raise ValueError("cleanup retention policy is not allowlisted")
        workflow_id = str(payload["orchestration"]["workflow_id"])
        now = datetime.now(UTC)
        candidates: list[dict[str, Any]] = []
        component_candidates: list[dict[str, Any]] = []
        file_candidates: list[dict[str, Any]] = []
        preview_candidates: list[dict[str, Any]] = []
        protected_count = 0
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if run is None:
                run = Run(
                    id=uuid5(NAMESPACE_URL, f"cleanup-run:{workflow_id}"),
                    campaign_id=None,
                    change_set_id=None,
                    workflow_id=workflow_id,
                    run_type="cleanup",
                    status="RUNNING",
                    project="platform",
                    started_at=now,
                    summary="safe worktree retention cleanup",
                )
                session.add(run)
            elif run.finished_at is not None:
                return {
                    **payload,
                    "status": run.status,
                    "cleanup_candidates": [],
                    "protected_count": 0,
                }
            else:
                run.status = "RUNNING"
                run.started_at = run.started_at or now
            leases = session.scalars(select(WorktreeLease)).all()
            for lease in leases:
                reason = self._cleanup_lease_eligibility(session, lease, now)
                if reason is None:
                    protected_count += 1
                    continue
                change_set = session.get(ChangeSet, lease.change_set_id)
                if change_set is None:
                    protected_count += 1
                    continue
                expected = (
                    PurePosixPath(self.settings.worktree_root.as_posix())
                    / lease.repository
                    / str(lease.change_set_id)
                )
                if PurePosixPath(lease.worktree_path) != expected:
                    protected_count += 1
                    continue
                candidates.append(
                    {
                        "lease_id": str(lease.id),
                        "change_set_id": str(lease.change_set_id),
                        "repository": lease.repository,
                        "worktree_path": lease.worktree_path,
                        "branch": lease.branch,
                        "base_sha": lease.base_sha,
                        "reason": reason,
                    }
                )
            component_candidates = self._component_cleanup_candidates(session, now)
            file_candidates = self._file_cleanup_candidates(session, now)
            preview_candidates = self._preview_namespace_cleanup_candidates(session, now)
            event_cutoff = now - timedelta(days=self.settings.dashboard_event_retention_days)
            dashboard_event_cleanup_count = int(
                session.scalar(
                    select(func.count(DashboardEvent.id)).where(
                        DashboardEvent.occurred_at < event_cutoff
                    )
                )
                or 0
            )
            log_cutoff = now - timedelta(days=self.settings.dashboard_agent_log_retention_days)
            execution_cutoff = now - timedelta(
                days=self.settings.dashboard_agent_execution_retention_days
            )
            agent_log_cleanup_count = int(
                session.scalar(
                    select(func.count(AgentLogChunk.id)).where(
                        AgentLogChunk.created_at < log_cutoff
                    )
                )
                or 0
            )
            agent_execution_cleanup_count = int(
                session.scalar(
                    select(func.count(AgentExecution.id)).where(
                        AgentExecution.updated_at < execution_cutoff,
                        AgentExecution.ended_at.is_not(None),
                    )
                )
                or 0
            )
            operation_export_cleanup_count = int(
                session.scalar(
                    select(func.count(OperationExport.id)).where(OperationExport.expires_at < now)
                )
                or 0
            )
        return {
            **payload,
            "status": "CLEANUP_CANDIDATES_DISCOVERED",
            "cleanup_candidates": sorted(candidates, key=lambda value: value["lease_id"]),
            "component_lease_cleanup_candidates": component_candidates,
            "file_cleanup_candidates": file_candidates,
            "preview_namespace_cleanup_candidates": preview_candidates,
            "dashboard_event_cleanup_count": dashboard_event_cleanup_count,
            "agent_log_cleanup_count": agent_log_cleanup_count,
            "agent_execution_cleanup_count": agent_execution_cleanup_count,
            "operation_export_cleanup_count": operation_export_cleanup_count,
            "protected_count": protected_count,
        }

    def _authorize_cleanup(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_candidates = payload.get("eligible_candidates")
        raw_file_candidates = payload.get("inspected_file_candidates")
        raw_component_candidates = payload.get("component_lease_cleanup_candidates")
        raw_preview_candidates = payload.get("inspected_preview_namespace_candidates")
        if (
            not isinstance(raw_candidates, list)
            or not isinstance(raw_file_candidates, list)
            or not isinstance(raw_component_candidates, list)
            or not isinstance(raw_preview_candidates, list)
        ):
            raise ValueError("cleanup authorization requires inspected candidates")
        if not self.settings.cleanup_execution_enabled:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "CLEANUP_EXECUTION_DISABLED",
            }
        workflow_id = str(payload["orchestration"]["workflow_id"])
        now = datetime.now(UTC)
        authorized: list[dict[str, Any]] = []
        authorized_files: list[dict[str, Any]] = []
        authorized_components: list[dict[str, Any]] = []
        authorized_previews: list[dict[str, Any]] = []
        fields = {
            "lease_id",
            "change_set_id",
            "repository",
            "worktree_path",
            "branch",
            "base_sha",
            "reason",
        }
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if run is None or run.finished_at is not None:
                raise ValueError("cleanup Run is missing or terminal")
            for raw in raw_candidates:
                if not isinstance(raw, dict):
                    raise ValueError("cleanup candidate must be an object")
                worktree_candidate = {key: raw[key] for key in fields if key in raw}
                if set(worktree_candidate) != fields:
                    raise ValueError("cleanup candidate identity is incomplete")
                lease = session.get(WorktreeLease, UUID(str(worktree_candidate["lease_id"])))
                reason = (
                    self._cleanup_lease_eligibility(session, lease, now)
                    if lease is not None
                    else None
                )
                if (
                    lease is None
                    or reason is None
                    or reason != worktree_candidate["reason"]
                    or str(lease.change_set_id) != worktree_candidate["change_set_id"]
                    or lease.repository != worktree_candidate["repository"]
                    or lease.worktree_path != worktree_candidate["worktree_path"]
                    or lease.branch != worktree_candidate["branch"]
                    or lease.base_sha != worktree_candidate["base_sha"]
                ):
                    raise ValueError("cleanup candidate changed after worker inspection")
                authorized.append(worktree_candidate)
            current_files = {
                value["candidate_id"]: value
                for value in self._file_cleanup_candidates(session, now)
            }
            candidate_fields = set(FileCleanupCandidate.model_fields)
            seen_file_ids: set[str] = set()
            for raw in raw_file_candidates:
                if not isinstance(raw, dict):
                    raise ValueError("inspected file cleanup candidate must be an object")
                inspection = str(raw.get("inspection", ""))
                if inspection == "MODIFIED_AFTER_CUTOFF":
                    continue
                if inspection not in {"CLEAN", "ABSENT"}:
                    raise ValueError("file cleanup candidate was not inspected by its owner")
                file_candidate = FileCleanupCandidate.model_validate(
                    {key: value for key, value in raw.items() if key in candidate_fields}
                )
                if file_candidate.candidate_id in seen_file_ids:
                    raise ValueError("file cleanup candidate inspection is duplicated")
                seen_file_ids.add(file_candidate.candidate_id)
                current = current_files.get(file_candidate.candidate_id)
                if current is None:
                    raise ValueError("file cleanup candidate is no longer metadata-eligible")
                original = file_candidate.model_dump(mode="json")
                if any(
                    original[key] != current[key] for key in original if key != "not_modified_after"
                ) or file_candidate.not_modified_after > datetime.fromisoformat(
                    str(current["not_modified_after"])
                ):
                    raise ValueError("file cleanup candidate identity changed after inspection")
                authorized_file = {**original, "inspection": inspection}
                if inspection == "CLEAN":
                    manifest = str(raw.get("manifest_sha256", ""))
                    entry_count = raw.get("entry_count")
                    newest_mtime_ns = raw.get("newest_mtime_ns")
                    if (
                        not re.fullmatch(r"[a-f0-9]{64}", manifest)
                        or not isinstance(entry_count, int)
                        or entry_count < 1
                        or not isinstance(newest_mtime_ns, int)
                        or newest_mtime_ns < 1
                    ):
                        raise ValueError("file cleanup inspection evidence is incomplete")
                    authorized_file.update(
                        {
                            "manifest_sha256": manifest,
                            "entry_count": entry_count,
                            "newest_mtime_ns": newest_mtime_ns,
                        }
                    )
                authorized_files.append(authorized_file)
            current_components = {
                value["lease_id"]: value
                for value in self._component_cleanup_candidates(session, now)
            }
            for raw in raw_component_candidates:
                if not isinstance(raw, dict) or not isinstance(raw.get("lease_id"), str):
                    raise ValueError("component cleanup candidate must be an object")
                current = current_components.get(raw["lease_id"])
                if current is None or current != raw:
                    raise ValueError("component cleanup candidate changed before authorization")
                authorized_components.append(current)
            current_previews = {
                value["candidate_id"]: value
                for value in self._preview_namespace_cleanup_candidates(session, now)
            }
            preview_fields = set(PreviewNamespaceCleanupCandidate.model_fields)
            seen_preview_ids: set[str] = set()
            for raw in raw_preview_candidates:
                if not isinstance(raw, dict):
                    raise ValueError("preview cleanup inspection must be an object")
                inspection = str(raw.get("inspection", ""))
                if inspection == "PROTECTED":
                    continue
                if inspection not in {"CLEAN", "ABSENT"}:
                    raise ValueError("preview namespace was not inspected by release worker")
                preview_candidate = PreviewNamespaceCleanupCandidate.model_validate(
                    {key: value for key, value in raw.items() if key in preview_fields}
                )
                if preview_candidate.candidate_id in seen_preview_ids:
                    raise ValueError("preview cleanup inspection is duplicated")
                seen_preview_ids.add(preview_candidate.candidate_id)
                original = preview_candidate.model_dump(mode="json")
                if current_previews.get(preview_candidate.candidate_id) != original:
                    raise ValueError("preview cleanup identity changed after inspection")
                authorized_preview = {**original, "inspection": inspection}
                if inspection == "CLEAN":
                    uid = raw.get("uid")
                    resource_version = raw.get("resource_version")
                    labels_sha256 = raw.get("labels_sha256")
                    if (
                        not isinstance(uid, str)
                        or not uid
                        or not isinstance(resource_version, str)
                        or not resource_version
                        or not isinstance(labels_sha256, str)
                        or not re.fullmatch(r"[a-f0-9]{64}", labels_sha256)
                    ):
                        raise ValueError("preview namespace inspection identity is incomplete")
                    authorized_preview.update(
                        {
                            "uid": uid,
                            "resource_version": resource_version,
                            "labels_sha256": labels_sha256,
                        }
                    )
                authorized_previews.append(authorized_preview)

        authorized.sort(key=lambda value: str(value["lease_id"]))
        authorized_files.sort(key=lambda value: str(value["candidate_id"]))
        authorized_components.sort(key=lambda value: str(value["lease_id"]))
        authorized_previews.sort(key=lambda value: str(value["candidate_id"]))
        authorization_sha256 = cleanup_authorization_sha256(
            workflow_id=workflow_id,
            worktrees=authorized,
            files=authorized_files,
            component_leases=authorized_components,
            preview_namespaces=authorized_previews,
        )
        return {
            **payload,
            "status": "CLEANUP_AUTHORIZED",
            "authorized_candidates": authorized,
            "authorized_file_candidates": authorized_files,
            "authorized_component_lease_candidates": authorized_components,
            "authorized_preview_namespace_candidates": authorized_previews,
            "cleanup_authorization_sha256": authorization_sha256,
        }

    def _execute_cleanup_metadata(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.cleanup_execution_enabled:
            raise ApplicationError(
                "metadata cleanup execution is disabled by policy",
                type="SecurityBlocker",
                non_retryable=True,
            )
        workflow_id = str(payload["orchestration"]["workflow_id"])
        worktrees = payload.get("authorized_candidates")
        files = payload.get("authorized_file_candidates")
        components = payload.get("authorized_component_lease_candidates")
        previews = payload.get("authorized_preview_namespace_candidates", [])
        if (
            not isinstance(worktrees, list)
            or not isinstance(files, list)
            or not isinstance(components, list)
            or not isinstance(previews, list)
        ):
            raise ValueError("metadata cleanup authorization lists are incomplete")
        if (
            any(not isinstance(item, dict) for item in worktrees)
            or any(not isinstance(item, dict) for item in files)
            or any(not isinstance(item, dict) for item in components)
            or any(not isinstance(item, dict) for item in previews)
        ):
            raise ValueError("metadata cleanup authorization records must be objects")
        worktree_records = cast(list[dict[str, Any]], worktrees)
        file_records = cast(list[dict[str, Any]], files)
        component_records = cast(list[dict[str, Any]], components)
        preview_records = cast(list[dict[str, Any]], previews)
        expected = cleanup_authorization_sha256(
            workflow_id=workflow_id,
            worktrees=worktree_records,
            files=file_records,
            component_leases=component_records,
            preview_namespaces=preview_records,
        )
        if payload.get("cleanup_authorization_sha256") != expected:
            raise ValueError("metadata cleanup authorization changed after controller validation")
        removed_files = payload.get("removed_file_candidate_ids")
        absent_files = payload.get("absent_file_candidate_ids")
        if not isinstance(removed_files, list) or not isinstance(absent_files, list):
            raise ValueError("file cleanup completion evidence is incomplete")
        completed_files = {str(value) for value in [*removed_files, *absent_files]}
        authorized_file_ids = {
            str(value["candidate_id"]) for value in file_records if "candidate_id" in value
        }
        if completed_files != authorized_file_ids:
            raise ValueError("file cleanup completion does not match controller authorization")
        removed_previews = payload.get("removed_preview_namespace_candidate_ids")
        absent_previews = payload.get("absent_preview_namespace_candidate_ids")
        if not isinstance(removed_previews, list) or not isinstance(absent_previews, list):
            raise ValueError("preview cleanup completion evidence is incomplete")
        completed_previews = {str(value) for value in [*removed_previews, *absent_previews]}
        authorized_preview_ids = {
            str(value["candidate_id"]) for value in preview_records if "candidate_id" in value
        }
        if completed_previews != authorized_preview_ids:
            raise ValueError("preview cleanup completion does not match authorization")
        now = datetime.now(UTC)
        released: list[str] = []
        purged: list[str] = []
        cleaned_previews: list[str] = []
        deleted_dashboard_events = 0
        deleted_agent_logs = 0
        deleted_agent_executions = 0
        deleted_operation_exports = 0
        candidate_fields = set(FileCleanupCandidate.model_fields)
        preview_fields = set(PreviewNamespaceCleanupCandidate.model_fields)
        with self.sessions.begin() as session:
            current_components = {
                value["lease_id"]: value
                for value in self._component_cleanup_candidates(session, now)
            }
            for raw in component_records:
                if not isinstance(raw, dict):
                    raise ValueError("component lease changed after cleanup authorization")
                lease = session.get(ComponentLease, UUID(str(raw["lease_id"])))
                if lease is None:
                    raise ValueError("authorized component lease disappeared")
                expected_component = {
                    "lease_id": str(lease.id),
                    "component_key": lease.component_key,
                    "owner_run_id": str(lease.owner_run_id),
                    "expires_at": self._as_utc(lease.expires_at).isoformat(),
                    "reason": "TERMINAL_OWNER_LEASE_EXPIRED",
                }
                if expected_component != raw:
                    raise ValueError("component lease identity changed after authorization")
                if lease.released_at is None:
                    if current_components.get(str(lease.id)) != raw:
                        raise ValueError("component lease is no longer cleanup-eligible")
                    lease.released_at = now
                released.append(str(lease.id))
            for raw in file_records:
                if not isinstance(raw, dict):
                    raise ValueError("authorized file cleanup candidate must be an object")
                file_candidate = FileCleanupCandidate.model_validate(
                    {key: value for key, value in raw.items() if key in candidate_fields}
                )
                if file_candidate.kind != "evidence-artifact":
                    continue
                artifact = session.get(Artifact, file_candidate.artifact_id)
                if (
                    artifact is None
                    or artifact.run_id != file_candidate.run_id
                    or artifact.relative_path != file_candidate.relative_path
                    or artifact.sha256 != file_candidate.expected_sha256
                    or artifact.size_bytes != file_candidate.expected_size_bytes
                    or artifact.retention_class != "ephemeral"
                    or artifact.producer == "release-runner"
                ):
                    raise ValueError("ephemeral evidence identity changed before tombstoning")
                if artifact.purged_at is None:
                    artifact.purged_at = now
                    artifact.purge_reason = file_candidate.reason
                purged.append(str(artifact.id))
            current_previews = {
                value["candidate_id"]: value
                for value in self._preview_namespace_cleanup_candidates(session, now)
            }
            for raw in preview_records:
                if not isinstance(raw, dict):
                    raise ValueError("authorized preview cleanup candidate must be an object")
                preview_candidate = PreviewNamespaceCleanupCandidate.model_validate(
                    {key: value for key, value in raw.items() if key in preview_fields}
                )
                deployment = session.get(Deployment, preview_candidate.deployment_id)
                if (
                    deployment is None
                    or deployment.change_set_id != preview_candidate.change_set_id
                    or deployment.project != preview_candidate.project
                    or deployment.environment != "preview"
                    or deployment.target_context != preview_candidate.target_context
                    or deployment.target_namespace != preview_candidate.namespace
                    or deployment.finished_at is None
                    or self._as_utc(deployment.finished_at) != preview_candidate.finished_at
                ):
                    raise ValueError("preview deployment identity changed before tombstoning")
                recovery = dict(deployment.recovery_json)
                if deployment.status == "PREVIEW_EXPIRED":
                    if recovery.get("cleanup_candidate_id") != preview_candidate.candidate_id:
                        raise ValueError("preview cleanup retry does not match its tombstone")
                else:
                    original = preview_candidate.model_dump(mode="json")
                    if current_previews.get(preview_candidate.candidate_id) != original:
                        raise ValueError("preview deployment is no longer cleanup-eligible")
                    deployment.status = "PREVIEW_EXPIRED"
                    deployment.recovery_json = {
                        **recovery,
                        "cleanup_candidate_id": preview_candidate.candidate_id,
                        "cleanup_reason": preview_candidate.reason,
                        "cleaned_at": now.isoformat(),
                    }
                cleaned_previews.append(str(deployment.id))

            event_cutoff = now - timedelta(days=self.settings.dashboard_event_retention_days)
            expired_events = session.scalars(
                select(DashboardEvent)
                .where(DashboardEvent.occurred_at < event_cutoff)
                .order_by(DashboardEvent.id)
                .limit(10_000)
            ).all()
            for event in expired_events:
                session.delete(event)
            deleted_dashboard_events = len(expired_events)

            log_cutoff = now - timedelta(days=self.settings.dashboard_agent_log_retention_days)
            expired_logs = session.scalars(
                select(AgentLogChunk)
                .where(AgentLogChunk.created_at < log_cutoff)
                .order_by(AgentLogChunk.id)
                .limit(10_000)
            ).all()
            for chunk in expired_logs:
                session.delete(chunk)
            deleted_agent_logs = len(expired_logs)

            execution_cutoff = now - timedelta(
                days=self.settings.dashboard_agent_execution_retention_days
            )
            expired_executions = session.scalars(
                select(AgentExecution)
                .where(
                    AgentExecution.updated_at < execution_cutoff,
                    AgentExecution.ended_at.is_not(None),
                )
                .order_by(AgentExecution.updated_at)
                .limit(10_000)
            ).all()
            for execution in expired_executions:
                session.delete(execution)
            deleted_agent_executions = len(expired_executions)

            expired_exports = session.scalars(
                select(OperationExport)
                .where(OperationExport.expires_at < now)
                .order_by(OperationExport.expires_at)
                .limit(10_000)
            ).all()
            for operation_export in expired_exports:
                session.delete(operation_export)
            deleted_operation_exports = len(expired_exports)

        return {
            **payload,
            "status": "METADATA_CLEANUP_EXECUTED",
            "released_component_lease_ids": sorted(released),
            "purged_artifact_ids": sorted(purged),
            "cleaned_preview_deployment_ids": sorted(cleaned_previews),
            "deleted_dashboard_event_count": deleted_dashboard_events,
            "deleted_agent_log_count": deleted_agent_logs,
            "deleted_agent_execution_count": deleted_agent_executions,
            "deleted_operation_export_count": deleted_operation_exports,
        }

    def _finalize_cleanup(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(payload["orchestration"]["workflow_id"])
        terminal = str(payload.get("terminal_status", ""))
        if terminal not in {"DRY_RUN", "COMPLETE", "BLOCKED"}:
            raise ValueError("cleanup terminal status is not allowlisted")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if run is None:
                raise ValueError("cleanup finalization Run is missing")
            if run.finished_at is not None:
                return {**payload, "status": terminal}
            if terminal == "COMPLETE":
                raw_authorized = payload.get("authorized_candidates")
                raw_authorized_files = payload.get("authorized_file_candidates")
                raw_authorized_components = payload.get("authorized_component_lease_candidates")
                raw_authorized_previews = payload.get("authorized_preview_namespace_candidates")
                removed = payload.get("removed_lease_ids")
                absent = payload.get("already_absent_lease_ids")
                removed_files = payload.get("removed_file_candidate_ids")
                absent_files = payload.get("absent_file_candidate_ids")
                released_components = payload.get("released_component_lease_ids")
                purged_artifacts = payload.get("purged_artifact_ids")
                removed_previews = payload.get("removed_preview_namespace_candidate_ids")
                absent_previews = payload.get("absent_preview_namespace_candidate_ids")
                cleaned_previews = payload.get("cleaned_preview_deployment_ids")
                if (
                    not isinstance(raw_authorized, list)
                    or not isinstance(raw_authorized_files, list)
                    or not isinstance(raw_authorized_components, list)
                    or not isinstance(raw_authorized_previews, list)
                    or not isinstance(removed, list)
                    or not isinstance(absent, list)
                    or not isinstance(removed_files, list)
                    or not isinstance(absent_files, list)
                    or not isinstance(released_components, list)
                    or not isinstance(purged_artifacts, list)
                    or not isinstance(removed_previews, list)
                    or not isinstance(absent_previews, list)
                    or not isinstance(cleaned_previews, list)
                ):
                    raise ValueError("cleanup completion evidence is incomplete")
                authorized_ids = {
                    str(value["lease_id"])
                    for value in raw_authorized
                    if isinstance(value, dict) and "lease_id" in value
                }
                completed_ids = {str(value) for value in [*removed, *absent]}
                if completed_ids != authorized_ids:
                    raise ValueError("cleanup completion does not match its authorization")
                for lease_id in completed_ids:
                    lease = session.get(WorktreeLease, UUID(lease_id))
                    if lease is None:
                        raise ValueError("cleanup completion lease disappeared")
                    lease.released_at = lease.released_at or now
                authorized_file_ids = {
                    str(value["candidate_id"])
                    for value in raw_authorized_files
                    if isinstance(value, dict) and "candidate_id" in value
                }
                completed_file_ids = {str(value) for value in [*removed_files, *absent_files]}
                if completed_file_ids != authorized_file_ids:
                    raise ValueError("file cleanup completion does not match authorization")
                authorized_component_ids = {
                    str(value["lease_id"])
                    for value in raw_authorized_components
                    if isinstance(value, dict) and "lease_id" in value
                }
                if {str(value) for value in released_components} != authorized_component_ids:
                    raise ValueError("component cleanup completion does not match authorization")
                expected_purged_ids = {
                    str(value["artifact_id"])
                    for value in raw_authorized_files
                    if isinstance(value, dict)
                    and value.get("kind") == "evidence-artifact"
                    and value.get("artifact_id") is not None
                }
                if {str(value) for value in purged_artifacts} != expected_purged_ids:
                    raise ValueError("evidence cleanup tombstones do not match authorization")
                authorized_preview_ids = {
                    str(value["candidate_id"])
                    for value in raw_authorized_previews
                    if isinstance(value, dict) and "candidate_id" in value
                }
                completed_preview_ids = {
                    str(value) for value in [*removed_previews, *absent_previews]
                }
                if completed_preview_ids != authorized_preview_ids:
                    raise ValueError("preview cleanup completion does not match authorization")
                expected_preview_deployments = {
                    str(value["deployment_id"])
                    for value in raw_authorized_previews
                    if isinstance(value, dict) and "deployment_id" in value
                }
                if {str(value) for value in cleaned_previews} != expected_preview_deployments:
                    raise ValueError("preview cleanup tombstones do not match authorization")

                run.status = "SUCCESS"
                run.summary = (
                    f"retained cleanup completed: {len(completed_ids)} worktrees, "
                    f"{len(completed_file_ids)} bounded files, "
                    f"{len(authorized_component_ids)} component leases, "
                    f"{len(authorized_preview_ids)} preview namespaces"
                )
            elif terminal == "DRY_RUN":
                run.status = "SUCCESS"
                run.summary = "cleanup dry run completed without mutation"
            else:
                run.status = "BLOCKED"
                run.error_code = str(payload.get("blocker", "CLEANUP_BLOCKED"))
                run.summary = "cleanup blocked without mutation"
            run.finished_at = now
        return {**payload, "status": terminal}

    def _cleanup_lease_eligibility(
        self, session: Any, lease: WorktreeLease, now: datetime
    ) -> str | None:
        change_set = session.get(ChangeSet, lease.change_set_id)
        owner = session.get(Run, lease.owner_run_id)
        if change_set is None or owner is None:
            return None
        if change_set.state not in {
            ChangeSetState.ISSUE_CLOSED.value,
            ChangeSetState.CANCELLED.value,
            ChangeSetState.SUPERSEDED.value,
            ChangeSetState.REJECTED.value,
        }:
            return None
        if self._run_retention_protected(session, owner):
            return None
        incident = session.scalar(
            select(Incident).where(
                Incident.change_set_id == change_set.id,
                Incident.status == "OPEN",
            )
        )
        if incident is not None:
            return None
        retention_cutoff = now - timedelta(days=self.settings.cleanup_retention_days)
        released_at = self._as_utc(lease.released_at) if lease.released_at else None
        expires_at = self._as_utc(lease.expires_at)
        owner_finished_at = self._as_utc(owner.finished_at)
        if released_at is not None and released_at <= retention_cutoff:
            return "RELEASED_RETENTION_EXPIRED"
        abandonment_cutoff = now - timedelta(hours=self.settings.cleanup_expiry_grace_hours)
        if (
            lease.released_at is None
            and expires_at <= abandonment_cutoff
            and owner_finished_at <= retention_cutoff
        ):
            return "ABANDONED_LEASE_EXPIRED"
        return None

    def _component_cleanup_candidates(self, session: Any, now: datetime) -> list[dict[str, Any]]:
        expiry_cutoff = now - timedelta(hours=self.settings.cleanup_expiry_grace_hours)
        candidates: list[dict[str, Any]] = []
        leases = session.scalars(
            select(ComponentLease).where(ComponentLease.released_at.is_(None))
        ).all()
        for lease in leases:
            owner = session.get(Run, lease.owner_run_id)
            if (
                owner is None
                or self._run_retention_protected(session, owner)
                or self._as_utc(lease.expires_at) > expiry_cutoff
            ):
                continue
            if owner.change_set_id is not None:
                incident = session.scalar(
                    select(Incident).where(
                        Incident.change_set_id == owner.change_set_id,
                        Incident.status == "OPEN",
                    )
                )
                if incident is not None:
                    continue
            candidates.append(
                {
                    "lease_id": str(lease.id),
                    "component_key": lease.component_key,
                    "owner_run_id": str(lease.owner_run_id),
                    "expires_at": self._as_utc(lease.expires_at).isoformat(),
                    "reason": "TERMINAL_OWNER_LEASE_EXPIRED",
                }
            )
        return sorted(candidates, key=lambda value: value["lease_id"])

    def _file_cleanup_candidates(self, session: Any, now: datetime) -> list[dict[str, Any]]:
        retention_cutoff = now - timedelta(days=self.settings.cleanup_retention_days)
        candidates: list[dict[str, Any]] = []
        runs = session.scalars(select(Run).where(Run.finished_at.is_not(None))).all()
        for run in runs:
            if (
                self._run_retention_protected(session, run)
                or self._as_utc(run.finished_at) > retention_cutoff
            ):
                continue
            invocations = session.scalars(
                select(AgentInvocation).where(AgentInvocation.run_id == run.id)
            ).all()
            if any(
                invocation.status == "RATE_LIMITED"
                or "RATE_LIMIT" in str(invocation.error_code or "").upper()
                for invocation in invocations
            ):
                continue
            if run.change_set_id is not None:
                incident = session.scalar(
                    select(Incident).where(
                        Incident.change_set_id == run.change_set_id,
                        Incident.status == "OPEN",
                    )
                )
                if incident is not None:
                    continue
            artifacts = session.scalars(select(Artifact).where(Artifact.run_id == run.id)).all()
            if any(invocation.provider == Provider.CODEX.value for invocation in invocations):
                candidates.append(
                    FileCleanupCandidate(
                        candidate_id=hashlib.sha256(
                            f"cleanup-file:provider-temp:codex:{run.id}".encode()
                        ).hexdigest(),
                        kind="provider-temp",
                        worker="codex",
                        run_id=run.id,
                        reason="PROVIDER_TEMP_RETENTION_EXPIRED",
                        not_modified_after=retention_cutoff,
                    ).model_dump(mode="json")
                )
            if any(artifact.producer == "delivery-runner" for artifact in artifacts):
                candidates.append(
                    FileCleanupCandidate(
                        candidate_id=hashlib.sha256(
                            f"cleanup-file:delivery-run:{run.id}".encode()
                        ).hexdigest(),
                        kind="delivery-run",
                        worker="delivery",
                        run_id=run.id,
                        reason="TERMINAL_RUN_RETENTION_EXPIRED",
                        not_modified_after=retention_cutoff,
                    ).model_dump(mode="json")
                )
            verification = session.scalar(
                select(VerificationResult)
                .where(
                    VerificationResult.run_id == run.id,
                    VerificationResult.deployment_id.is_not(None),
                    VerificationResult.status == "PASS",
                )
                .limit(1)
            )
            if verification is not None and any(
                artifact.producer == "browser-runner" for artifact in artifacts
            ):
                candidates.append(
                    FileCleanupCandidate(
                        candidate_id=hashlib.sha256(
                            (
                                f"cleanup-file:browser-output:{verification.deployment_id}:{run.id}"
                            ).encode()
                        ).hexdigest(),
                        kind="browser-output",
                        worker="browser",
                        run_id=run.id,
                        deployment_id=verification.deployment_id,
                        reason="BROWSER_OUTPUT_RETENTION_EXPIRED",
                        not_modified_after=retention_cutoff,
                    ).model_dump(mode="json")
                )
            for artifact in artifacts:
                path_parts = set(PurePosixPath(artifact.relative_path).parts)
                if (
                    artifact.retention_class != "ephemeral"
                    or artifact.purged_at is not None
                    or self._as_utc(artifact.created_at) > retention_cutoff
                    or artifact.producer == "release-runner"
                    or path_parts.intersection(
                        {"security", "release", "releases", "incident", "incidents"}
                    )
                ):
                    continue
                candidates.append(
                    FileCleanupCandidate(
                        candidate_id=hashlib.sha256(
                            f"cleanup-file:evidence-artifact:{artifact.id}".encode()
                        ).hexdigest(),
                        kind="evidence-artifact",
                        worker="controller",
                        run_id=run.id,
                        artifact_id=artifact.id,
                        relative_path=artifact.relative_path,
                        expected_sha256=artifact.sha256,
                        expected_size_bytes=artifact.size_bytes,
                        reason="EPHEMERAL_EVIDENCE_RETENTION_EXPIRED",
                        not_modified_after=retention_cutoff,
                    ).model_dump(mode="json")
                )
        return sorted(candidates, key=lambda value: value["candidate_id"])

    @staticmethod
    def _run_retention_protected(session: Any, run: Run) -> bool:
        if (
            run.finished_at is None
            or run.status in {"QUEUED", "RUNNING", "AWAITING_APPROVAL", "RATE_LIMITED"}
            or "RATE_LIMIT" in str(run.error_code or "").upper()
        ):
            return True
        invocations = session.scalars(
            select(AgentInvocation).where(AgentInvocation.run_id == run.id)
        ).all()
        return any(
            invocation.status == "RATE_LIMITED"
            or "RATE_LIMIT" in str(invocation.error_code or "").upper()
            for invocation in invocations
        )

    def _preview_namespace_cleanup_candidates(
        self, session: Any, now: datetime
    ) -> list[dict[str, Any]]:
        retention_cutoff = now - timedelta(days=self.settings.cleanup_retention_days)
        terminal_change_set_states = {
            ChangeSetState.ISSUE_CLOSED.value,
            ChangeSetState.CANCELLED.value,
            ChangeSetState.SUPERSEDED.value,
            ChangeSetState.REJECTED.value,
            ChangeSetState.ROLLED_BACK.value,
        }
        targets: dict[str, DeploymentTargetRecord] = {}
        root = self.settings.deployment_target_root.resolve()
        for project in Project:
            target_path = (root / f"{project.value}-preview.yaml").resolve()
            if (
                not target_path.is_file()
                or not target_path.is_relative_to(root)
                or not os.access(target_path, os.R_OK)
            ):
                continue
            target = self._deployment_target(project, "preview")
            if target.enabled and target.configuration.get("cleanup_enabled") is True:
                targets[project.value] = target
        if not targets:
            return []
        candidates: list[dict[str, Any]] = []
        deployments = session.scalars(
            select(Deployment).where(
                Deployment.environment == "preview",
                Deployment.finished_at.is_not(None),
                Deployment.status != "PREVIEW_EXPIRED",
            )
        ).all()
        for deployment in deployments:
            if (
                deployment.finished_at is None
                or self._as_utc(deployment.finished_at) > retention_cutoff
            ):
                continue
            change_set = session.get(ChangeSet, deployment.change_set_id)
            cleanup_target = targets.get(deployment.project)
            if (
                change_set is None
                or change_set.state not in terminal_change_set_states
                or cleanup_target is None
                or deployment.target_context != cleanup_target.target_context
                or deployment.target_namespace != cleanup_target.target_namespace
            ):
                continue
            incident = session.scalar(
                select(Incident).where(
                    Incident.status == "OPEN",
                    (
                        (Incident.change_set_id == change_set.id)
                        | (Incident.deployment_id == deployment.id)
                    ),
                )
            )
            if incident is not None:
                continue
            configuration = cleanup_target.configuration
            namespace_prefix = self._config_string(configuration, "cleanup_namespace_prefix")
            kube_context = self._config_string(configuration, "cleanup_kube_context")
            kubeconfig_path = self._config_string(configuration, "cleanup_kubeconfig_path")
            finished_at = self._as_utc(deployment.finished_at)
            identity = (
                f"cleanup-preview:{deployment.id}:{change_set.id}:"
                f"{deployment.target_namespace}:{finished_at.isoformat()}"
            )
            candidates.append(
                PreviewNamespaceCleanupCandidate(
                    candidate_id=hashlib.sha256(identity.encode()).hexdigest(),
                    deployment_id=deployment.id,
                    change_set_id=change_set.id,
                    project=deployment.project,
                    namespace=deployment.target_namespace,
                    namespace_prefix=namespace_prefix,
                    target_context=deployment.target_context,
                    kube_context=kube_context,
                    kubeconfig_path=Path(kubeconfig_path),
                    finished_at=finished_at,
                    reason="TERMINAL_CHANGESET_PREVIEW_RETENTION_EXPIRED",
                ).model_dump(mode="json")
            )
        return sorted(candidates, key=lambda value: value["candidate_id"])

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def _runtime_target(self, project: Project, environment: str) -> RuntimeTargetRecord:
        if environment != "staging":
            raise ValueError("runtime verification target must be staging")
        root = self.settings.runtime_target_root.resolve()
        path = (root / f"{project.value}-staging.yaml").resolve()
        if not path.is_file() or not path.is_relative_to(root):
            raise ValueError("runtime target configuration is missing")
        target = RuntimeTargetRecord.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
        if target.project is not project or target.environment != environment:
            raise ValueError("runtime target identity does not match its filename")
        return target

    def _candidate_binding_is_current(
        self,
        session: Any,
        change_set: ChangeSet,
        candidate: ApprovalCandidate,
    ) -> bool:
        value = self._object(candidate.payload_json, "approval candidate")
        required = {
            "target_context",
            "target_namespace",
            "migration",
            "backup_requirement",
            "rollback_plan_sha256",
            "ci_result_sha256",
            "independent_review_sha256",
            "release_preflight_sha256",
        }
        if required - set(value):
            return False
        current = ApprovalService.artifact_payload(
            self._changeset_model(session, change_set),
            target_context=str(value["target_context"]),
            target_namespace=str(value["target_namespace"]),
            migration=bool(value["migration"]),
            backup_requirement=str(value["backup_requirement"]),
            rollback_plan_sha256=str(value["rollback_plan_sha256"]),
            ci_result_sha256=str(value["ci_result_sha256"]),
            independent_review_sha256=str(value["independent_review_sha256"]),
            release_preflight_sha256=str(value["release_preflight_sha256"]),
        )
        return ApprovalService.digest(current) == candidate.artifact_binding_sha256

    @staticmethod
    def _runtime_block(
        payload: dict[str, Any],
        run: Run,
        blocker: str,
        *,
        project: str,
        change_set_id: UUID,
    ) -> dict[str, Any]:
        run.status = "RATE_LIMITED" if blocker == "RATE_LIMITED" else "BLOCKED"
        run.error_code = blocker
        run.finished_at = datetime.now(UTC)
        return {
            **payload,
            "project": project,
            "change_set_id": str(change_set_id),
            "status": run.status,
            "blocker": blocker,
        }

    @staticmethod
    def _release_deployment_lease(session: Any, change_set: ChangeSet, run: Run) -> None:
        environment, separator, suffix = run.run_type.partition("-")
        if not separator or suffix != "deployment" or environment not in {"preview", "staging"}:
            return
        ComponentLeaseManager().release(
            session,
            component_key=f"{change_set.project}:deploy:{environment}",
            owner_run_id=run.id,
        )

    @staticmethod
    def _release_implementation_leases(session: Any, run: Run) -> None:
        now = datetime.now(UTC)
        leases = session.scalars(
            select(ComponentLease).where(
                ComponentLease.owner_run_id == run.id,
                ComponentLease.released_at.is_(None),
            )
        ).all()
        for lease in leases:
            lease.released_at = now

    def _deployment_target(
        self, project: Project, environment: Literal["preview", "staging"]
    ) -> DeploymentTargetRecord:
        root = self.settings.deployment_target_root.resolve()
        path = (root / f"{project.value}-{environment}.yaml").resolve()
        if not path.is_file() or not path.is_relative_to(root):
            raise ValueError("deployment target configuration is missing")
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        target = DeploymentTargetRecord.model_validate(value)
        if target.project is not project or target.environment != environment:
            raise ValueError("deployment target identity does not match its filename")
        if project is Project.FALCONE and target.strategy != "helm":
            raise ValueError("Falcone target must use Helm")
        if project is Project.MUSEMATIC and target.strategy != "argocd-gitops":
            raise ValueError("Musematic target must use Argo CD GitOps")
        if project is Project.LLMWIKI and target.strategy != "repo-local-manifest":
            raise ValueError("LLMWiki target must use its bounded repository-local manifest")
        return target

    def _release_plan(
        self,
        change_set: ChangeSet,
        lease: WorktreeLease,
        target: DeploymentTargetRecord,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        configuration = target.configuration
        if target.strategy == "helm":
            chart_subdirectory = self._relative_path(
                configuration.get("chart_subdirectory", "."), "chart_subdirectory"
            )
            chart_directory = Path(lease.worktree_path) / chart_subdirectory
            raw_values = configuration.get("values_files")
            if not isinstance(raw_values, list) or not raw_values:
                raise ValueError("Falcone target requires values_files")
            values_files = [
                str(chart_directory / self._relative_path(value, "values_file"))
                for value in raw_values
            ]
            migration = bool(evidence.get("migration", False))
            return {
                "strategy": "helm",
                "change_set_id": str(change_set.id),
                "chart_repository_sha": str(change_set.deployment_head_sha),
                "chart_version": str(
                    change_set.chart_version or self._config_string(configuration, "chart_version")
                ),
                "release_name": self._config_string(configuration, "release_name"),
                "chart_directory": str(chart_directory),
                "values_files": values_files,
                "image_digests": change_set.image_digests,
                "kube_context": self._config_string(configuration, "kube_context"),
                "namespace": target.target_namespace,
                "kubeconfig_path": self._config_string(configuration, "kubeconfig_path"),
                "migration": migration,
                "backup_evidence_sha256": evidence.get("backup_evidence_sha256"),
                "parity_evidence_sha256": evidence.get("parity_evidence_sha256"),
                "rollback_plan_sha256": evidence["rollback_plan_sha256"],
                "approval_binding_sha256": "0" * 64,
                "timeout_minutes": int(configuration.get("timeout_minutes", 30)),
            }
        return {
            "strategy": "argocd-gitops",
            "change_set_id": str(change_set.id),
            "deployment_repository_sha": str(change_set.deployment_head_sha),
            "deployment_repository": lease.worktree_path,
            "deployment_pr_url": str(change_set.deployment_pr_url),
            "image_digests": change_set.image_digests,
            "argo_application": self._config_string(configuration, "argo_application"),
            "target_revision": str(change_set.deployment_head_sha),
            "target_namespace": target.target_namespace,
            "approval_binding_sha256": "0" * 64,
        }

    @staticmethod
    def _release_missing(change_set: ChangeSet) -> list[str]:
        values = {
            "source_head_sha": change_set.source_head_sha,
            "source_pr_url": change_set.source_pr_url,
            "deployment_head_sha": change_set.deployment_head_sha,
            "deployment_pr_url": change_set.deployment_pr_url,
            "image_digests": change_set.image_digests,
        }
        return [name for name, value in values.items() if not value]

    @classmethod
    def _validated_release_impact(cls, value: Any, label: str) -> dict[str, Any]:
        impact = cls._object(value, f"{label} release impact")
        required = {
            "version",
            "repository",
            "changed_paths_sha256",
            "diff_sha256",
            "categories",
            "migration",
            "risk_floor",
            "sha256",
        }
        if set(impact) != required:
            raise ValueError(f"{label} release impact schema is incomplete")
        categories = impact.get("categories")
        allowed_categories = {
            "migration",
            "secret_lifecycle",
            "cluster_scoped",
            "auth_or_tenant_boundary",
            "destructive_lifecycle",
        }
        if (
            impact.get("version") != 1
            or impact.get("risk_floor") not in {"R1", "R2", "R3"}
            or not isinstance(categories, list)
            or categories != sorted(set(str(item) for item in categories))
            or not set(categories).issubset(allowed_categories)
            or impact.get("migration") != ("migration" in categories)
            or any(
                not cls._is_digest(impact.get(key))
                for key in ("changed_paths_sha256", "diff_sha256", "sha256")
            )
        ):
            raise ValueError(f"{label} release impact is invalid")
        canonical = {key: impact[key] for key in required if key != "sha256"}
        expected = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if impact["sha256"] != expected:
            raise ValueError(f"{label} release impact digest changed")
        return impact

    @staticmethod
    def _is_digest(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @staticmethod
    def _is_git_sha(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 40
            and all(character in "0123456789abcdef" for character in value)
        )

    @staticmethod
    def _relative_path(value: Any, label: str) -> PurePosixPath:
        if not isinstance(value, str):
            raise ValueError(f"{label} must be a relative path")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"{label} escaped its deployment worktree")
        return path

    @staticmethod
    def _config_string(configuration: dict[str, Any], key: str) -> str:
        value = configuration.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"deployment target requires {key}")
        return value

    @staticmethod
    def _changeset_model(session: Any, change_set: ChangeSet) -> ChangeSetModel:
        issue = session.get(Issue, change_set.issue_id) if change_set.issue_id else None
        return ChangeSetModel.model_validate(
            {
                "change_set_id": change_set.id,
                "project": change_set.project,
                "finding_id": change_set.finding_id,
                "github_issue": issue.url if issue and issue.url else None,
                "openspec_change": issue.openspec_change if issue else None,
                "risk_class": change_set.risk_class,
                "state": change_set.state,
                "source_repository": change_set.source_repository,
                "source_branch": change_set.source_branch,
                "source_base_sha": change_set.source_base_sha,
                "source_head_sha": change_set.source_head_sha,
                "source_pr": change_set.source_pr_url,
                "deployment_repository": change_set.deployment_repository,
                "deployment_branch": change_set.deployment_branch,
                "deployment_base_sha": change_set.deployment_base_sha,
                "deployment_head_sha": change_set.deployment_head_sha,
                "deployment_pr": change_set.deployment_pr_url,
                "image_digests": change_set.image_digests,
                "chart_version": change_set.chart_version,
                "version": change_set.version,
            }
        )

    def _persist_quota(self, snapshots: list[QuotaSnapshotRecord]) -> None:
        for snapshot in snapshots:
            try:
                with self.sessions.begin() as session:
                    captured = snapshot.captured_at.astimezone(UTC)
                    identity = uuid5(
                        NAMESPACE_URL,
                        f"engineering-quota:{snapshot.provider.value}:{captured.isoformat()}",
                    )
                    if session.get(QuotaSnapshot, identity) is not None:
                        continue
                    session.add(
                        QuotaSnapshot(
                            id=identity,
                            provider=snapshot.provider.value,
                            captured_at=captured,
                            remaining_capacity=snapshot.remaining_capacity,
                            total_capacity=snapshot.total_capacity,
                            reset_at=(
                                snapshot.reset_at.astimezone(UTC) if snapshot.reset_at else None
                            ),
                            running_jobs=snapshot.running_jobs,
                            rate_limited=snapshot.rate_limited,
                            authenticated=snapshot.authenticated,
                            cli_present=snapshot.cli_present,
                            status=snapshot.status,
                            source=snapshot.source,
                        )
                    )
            except IntegrityError:
                # A concurrent retry may have committed the deterministic primary key first.
                continue

    def _quota_broker(self) -> QuotaBroker:
        broker = QuotaBroker()
        with self.sessions() as session:
            rows = session.scalars(
                select(QuotaSnapshot).order_by(QuotaSnapshot.captured_at.desc())
            ).all()
        seen: set[str] = set()
        for row in rows:
            if row.provider in seen:
                continue
            seen.add(row.provider)
            broker.record(
                BrokerQuotaSnapshot(
                    provider=Provider(row.provider),
                    captured_at=row.captured_at,
                    remaining_capacity=row.remaining_capacity,
                    total_capacity=row.total_capacity,
                    reset_at=row.reset_at,
                    running_jobs=row.running_jobs,
                    rate_limited=row.rate_limited,
                    source=row.source,
                )
            )
        return broker

    @staticmethod
    def _cell_id(campaign_id: UUID, cell: CampaignCellRecord) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            "coverage-cell:"
            f"{campaign_id}:{cell.repository_surface}:{cell.domain}:{cell.persona}:"
            f"{cell.flow}:{cell.lens.value}:{cell.environment}",
        )

    @staticmethod
    def _object(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be an object")
        return value

    @staticmethod
    def _fingerprint(
        campaign: Campaign,
        cell: CampaignCellRecord,
        component: str,
        reproduction: list[str],
    ) -> str:
        canonical = json.dumps(
            {
                "project": campaign.project,
                "repository_surface": cell.repository_surface,
                "domain": cell.domain,
                "persona": cell.persona,
                "flow": cell.flow,
                "lens": cell.lens.value,
                "component": component,
                "reproduction": reproduction,
                "source_sha": campaign.source_sha,
                "deployment_sha": campaign.deployment_sha,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _audit_output_schema() -> dict[str, Any]:
        finding = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "severity",
                "confidence",
                "expected_behavior",
                "actual_behavior",
                "minimal_reproduction",
                "affected_component",
                "security_impact",
                "data_impact",
                "recommended_action",
            ],
            "properties": {
                "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "expected_behavior": {"type": "string", "minLength": 1, "maxLength": 20000},
                "actual_behavior": {"type": "string", "minLength": 1, "maxLength": 20000},
                "minimal_reproduction": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "items": {"type": "string", "minLength": 1, "maxLength": 4000},
                },
                "affected_component": {"type": "string", "minLength": 1, "maxLength": 200},
                "security_impact": {"type": ["string", "null"], "maxLength": 20000},
                "data_impact": {"type": ["string", "null"], "maxLength": 20000},
                "recommended_action": {"type": "string", "minLength": 1, "maxLength": 20000},
            },
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "decision", "evidence", "open_questions", "finding"],
            "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": 20000},
                "decision": {
                    "type": "string",
                    "enum": ["PASS", "FAIL", "BLOCKED", "REQUEST_CHANGES"],
                },
                "evidence": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 500,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind", "reference", "sha256"],
                        "properties": {
                            "kind": {"type": "string", "minLength": 1, "maxLength": 100},
                            "reference": {"type": "string", "minLength": 1, "maxLength": 2000},
                            "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                        },
                    },
                },
                "open_questions": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {"type": "string", "maxLength": 4000},
                },
                "finding": {**finding, "type": ["object", "null"]},
            },
        }

    @staticmethod
    def _verification_output_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "summary",
                "reproduced",
                "positive_control",
                "negative_control",
                "environment_valid",
                "evidence",
                "open_questions",
            ],
            "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": 20000},
                "reproduced": {"type": "boolean"},
                "positive_control": {"type": "boolean"},
                "negative_control": {"type": "boolean"},
                "environment_valid": {"type": "boolean"},
                "evidence": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 500,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind", "reference", "sha256"],
                        "properties": {
                            "kind": {"type": "string", "minLength": 1, "maxLength": 100},
                            "reference": {"type": "string", "minLength": 1, "maxLength": 2000},
                            "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                        },
                    },
                },
                "open_questions": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {"type": "string", "maxLength": 4000},
                },
            },
        }

    @staticmethod
    def _runtime_verification_output_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "decision",
                "observed_revision",
                "observed_digests",
                "checks",
                "evidence",
                "incident_required",
            ],
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["RUNTIME_VERIFIED", "FAIL", "BLOCKED"],
                },
                "observed_revision": {"type": "string", "maxLength": 200},
                "observed_digests": {
                    "type": "object",
                    "maxProperties": 500,
                    "additionalProperties": {
                        "type": "string",
                        "pattern": "^sha256:[a-f0-9]{64}$",
                    },
                },
                "checks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 500,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "status", "summary"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1, "maxLength": 200},
                            "status": {
                                "type": "string",
                                "enum": ["PASS", "FAIL", "BLOCKED"],
                            },
                            "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
                        },
                    },
                },
                "evidence": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 500,
                    "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                },
                "incident_required": {"type": "boolean"},
            },
        }

    @staticmethod
    def _atomic_text(path: Path, value: str) -> None:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(value.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)

    @staticmethod
    def _component_key(project: Project, value: str) -> str:
        lowered = value.lower()
        if project is Project.FALCONE:
            component = (
                "gateway"
                if any(marker in lowered for marker in ("gateway", "apisix"))
                else "web-console"
                if any(marker in lowered for marker in ("web", "console", "ui"))
                else "control-plane"
            )
        else:
            component = (
                "runtime-controller"
                if any(marker in lowered for marker in ("runtime", "sandbox"))
                else "web"
                if any(marker in lowered for marker in ("web", "ui"))
                else "control-plane"
            )
        return f"{project.value}:{component}"

    @staticmethod
    def _spec_output_schema() -> dict[str, Any]:
        string_list = {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {"type": "string", "minLength": 1, "maxLength": 4000},
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "decision",
                "title",
                "problem",
                "scope",
                "non_scope",
                "acceptance_criteria",
                "implementation_constraints",
                "test_plan",
                "risk_notes",
                "open_questions",
            ],
            "properties": {
                "decision": {"type": "string", "enum": ["READY", "BLOCKED"]},
                "title": {"type": "string", "minLength": 1, "maxLength": 300},
                "problem": {"type": "string", "minLength": 1, "maxLength": 20000},
                "scope": string_list,
                "non_scope": string_list,
                "acceptance_criteria": string_list,
                "implementation_constraints": string_list,
                "test_plan": string_list,
                "risk_notes": string_list,
                "open_questions": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {"type": "string", "maxLength": 4000},
                },
            },
        }

    @staticmethod
    def _maker_output_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "decision",
                "summary",
                "changed_files",
                "tests",
                "open_questions",
            ],
            "properties": {
                "decision": {"type": "string", "enum": ["COMPLETE", "BLOCKED"]},
                "summary": {"type": "string", "minLength": 1, "maxLength": 20000},
                "changed_files": {
                    "type": "array",
                    "maxItems": 1000,
                    "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
                "tests": {
                    "type": "array",
                    "maxItems": 500,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["command", "status"],
                        "properties": {
                            "command": {"type": "string", "maxLength": 4000},
                            "status": {"type": "string", "enum": ["PASS", "FAIL", "SKIPPED"]},
                        },
                    },
                },
                "open_questions": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {"type": "string", "maxLength": 4000},
                },
            },
        }

    @staticmethod
    def _checker_output_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "summary", "findings", "evidence", "open_questions"],
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["PASS", "REQUEST_CHANGES", "BLOCKED"],
                },
                "summary": {"type": "string", "minLength": 1, "maxLength": 20000},
                "findings": {
                    "type": "array",
                    "maxItems": 500,
                    "items": {"type": "string", "maxLength": 4000},
                },
                "evidence": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 500,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind", "reference", "sha256"],
                        "properties": {
                            "kind": {"type": "string", "minLength": 1, "maxLength": 100},
                            "reference": {"type": "string", "minLength": 1, "maxLength": 2000},
                            "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                        },
                    },
                },
                "open_questions": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {"type": "string", "maxLength": 4000},
                },
            },
        }

    @staticmethod
    def _release_review_output_schema() -> dict[str, Any]:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "findings", "verified_tests", "risk_assessment"],
            "properties": {
                "decision": {
                    "enum": ["PASS", "REQUEST_CHANGES", "FAIL", "BLOCKED"],
                },
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["severity", "location", "summary"],
                        "properties": {
                            "severity": {"type": "string"},
                            "location": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                    },
                },
                "verified_tests": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "risk_assessment": {"type": "string"},
            },
        }
