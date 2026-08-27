from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import case, create_engine, func, or_, select
from sqlalchemy.orm import sessionmaker

from engineering_control.api.schemas import (
    ApprovalRecord,
    BacklogApprovalCandidateRequest,
    CampaignStart,
    DeliveryReconcile,
    DeliveryStart,
    DeploymentPrepare,
    RollbackRequest,
    RuntimeVerificationStart,
)
from engineering_control.db.models import (
    AgentInvocation,
    Approval,
    ApprovalCandidate,
    Artifact,
    BacklogIssueRecord,
    Campaign,
    ChangeSet,
    Deployment,
    Enhancement,
    Finding,
    GitHubBacklogCampaign,
    ProviderUsage,
    QuotaSnapshot,
    Run,
    VerificationResult,
    WorkerHeartbeat,
)
from engineering_control.engineering.backlog import (
    BACKLOG_APPROVAL_PHRASE,
    approval_binding,
    canonical_sha256,
)
from engineering_control.models.core import ArtifactMetadata
from engineering_control.models.enums import Project


def serialize_row(row: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, UUID):
            result[column.name] = str(value)
        elif isinstance(value, datetime):
            result[column.name] = value.astimezone(UTC).isoformat()
        else:
            result[column.name] = value
    return result


class MetadataRepository(Protocol):
    async def create_campaign(
        self, campaign_id: UUID, workflow_id: str, request: CampaignStart
    ) -> None: ...

    async def create_delivery_run(self, workflow_id: str, request: DeliveryStart) -> None: ...
    async def create_finding_verification_run(
        self, workflow_id: str, finding_id: UUID
    ) -> Project: ...
    async def create_delivery_reconciliation_run(
        self, workflow_id: str, request: DeliveryReconcile
    ) -> None: ...
    async def create_deployment_run(
        self, workflow_id: str, request: DeploymentPrepare
    ) -> Project: ...
    async def create_runtime_verification_run(
        self, workflow_id: str, request: RuntimeVerificationStart
    ) -> Project: ...
    async def create_recovery_run(self, workflow_id: str, request: RollbackRequest) -> Project: ...

    async def list_campaigns(self, limit: int) -> list[dict[str, Any]]: ...
    async def list_backlog_campaigns(
        self, project: str | None, limit: int
    ) -> list[dict[str, Any]]: ...
    async def get_backlog_campaign(self, campaign_id: UUID) -> dict[str, Any] | None: ...
    async def list_backlog_issues(self, campaign_id: UUID, limit: int) -> list[dict[str, Any]]: ...
    async def get_backlog_issue(
        self, campaign_id: UUID, repository: str, issue_number: int
    ) -> dict[str, Any] | None: ...
    async def prepare_backlog_approval(
        self, campaign_id: UUID, request: BacklogApprovalCandidateRequest
    ) -> dict[str, Any]: ...
    async def approve_backlog_campaign(
        self, campaign_id: UUID, project: str, binding: str, actor: str
    ) -> dict[str, Any]: ...
    async def get_finding(self, finding_id: UUID) -> dict[str, Any] | None: ...
    async def list_findings(self, limit: int) -> list[dict[str, Any]]: ...
    async def list_enhancements(
        self, project: Project, status: str | None, limit: int
    ) -> list[dict[str, Any]]: ...
    async def get_enhancement(self, enhancement_id: str) -> dict[str, Any] | None: ...
    async def approve_enhancement(
        self, enhancement_id: str, binding: str, actor: str
    ) -> dict[str, Any]: ...
    async def reject_enhancement(
        self, enhancement_id: str, reason: str, actor: str
    ) -> dict[str, Any]: ...
    async def get_artifact(self, artifact_id: UUID) -> dict[str, Any] | None: ...
    async def get_deployment(self, deployment_id: UUID) -> dict[str, Any] | None: ...
    async def latest_quota_snapshots(self) -> dict[str, dict[str, Any]]: ...
    async def provider_usage_history(self, days: int = 7) -> dict[str, dict[str, Any]]: ...
    async def workflow_for_change_set(self, change_set_id: UUID) -> str | None: ...
    async def current_approval_candidate(self, change_set_id: UUID) -> dict[str, Any] | None: ...
    async def persist_approval(self, request: ApprovalRecord, approver_subject: str) -> None: ...
    async def resolve_evidence_binding(
        self,
        workflow_id: str,
        project: Project,
        campaign_id: UUID | None,
        change_set_id: UUID | None,
    ) -> dict[str, UUID | None]: ...

    async def persist_artifact(self, metadata: ArtifactMetadata) -> dict[str, Any]: ...
    async def persist_worker_heartbeat(self, identity: str, hostname: str) -> dict[str, Any]: ...
    async def metrics_snapshot(self) -> dict[str, Any]: ...


class SQLMetadataRepository:
    def __init__(
        self,
        database_url: str,
        *,
        github_policy_request_root: Path = Path(
            "/var/lib/engineering-control/github-policy-requests"
        ),
    ) -> None:
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            connect_args={"connect_timeout": 5} if database_url.startswith("postgresql") else {},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.github_policy_request_root = github_policy_request_root

    async def create_campaign(
        self, campaign_id: UUID, workflow_id: str, request: CampaignStart
    ) -> None:
        await asyncio.to_thread(self._create_campaign, campaign_id, workflow_id, request)

    def _create_campaign(self, campaign_id: UUID, workflow_id: str, request: CampaignStart) -> None:
        with self.sessions.begin() as session:
            session.add(
                Campaign(
                    id=campaign_id,
                    project=request.project.value,
                    name=request.name,
                    status="QUEUED",
                    source_sha=request.source_sha,
                    deployment_sha=request.deployment_sha,
                    required_cells=(
                        len(request.domains)
                        * len(request.personas)
                        * len(request.lenses)
                        * len(request.environments)
                    ),
                    metadata_json={
                        "dry_run": request.dry_run,
                        "preset": request.preset,
                        "environment": request.environment,
                        "publish_findings": request.publish_findings,
                        "snapshot_resolution": (
                            "automatic" if request.source_sha is None else "explicit"
                        ),
                    },
                )
            )
            session.add(
                Run(
                    campaign_id=campaign_id,
                    workflow_id=workflow_id,
                    run_type="audit-campaign",
                    status="QUEUED",
                    project=request.project.value,
                )
            )

    async def create_delivery_run(self, workflow_id: str, request: DeliveryStart) -> None:
        await asyncio.to_thread(self._create_delivery_run, workflow_id, request)

    async def create_finding_verification_run(self, workflow_id: str, finding_id: UUID) -> Project:
        return await asyncio.to_thread(
            self._create_finding_verification_run, workflow_id, finding_id
        )

    def _create_finding_verification_run(self, workflow_id: str, finding_id: UUID) -> Project:
        with self.sessions.begin() as session:
            finding = session.get(Finding, finding_id)
            if finding is None or finding.disposition not in {"CANDIDATE", "FLAKY", "BLOCKED"}:
                raise ValueError("verification requires a CANDIDATE, FLAKY, or BLOCKED finding")
            active = session.scalar(
                select(Run).where(
                    Run.campaign_id == finding.campaign_id,
                    Run.run_type == "finding-verification",
                    Run.summary == str(finding.id),
                    Run.status.in_({"QUEUED", "RUNNING", "RATE_LIMITED"}),
                )
            )
            if active is not None:
                raise ValueError("an active verification already owns this finding")
            project = Project(finding.project)
            session.add(
                Run(
                    campaign_id=finding.campaign_id,
                    workflow_id=workflow_id,
                    run_type="finding-verification",
                    status="QUEUED",
                    project=project.value,
                    summary=str(finding.id),
                )
            )
            return project

    def _create_delivery_run(self, workflow_id: str, request: DeliveryStart) -> None:
        with self.sessions.begin() as session:
            finding = session.get(Finding, request.finding_id)
            if finding is None or finding.disposition != "CONFIRMED":
                raise ValueError("delivery requires a CONFIRMED finding")
            session.add(
                Run(
                    workflow_id=workflow_id,
                    run_type="issue-delivery",
                    status="QUEUED",
                    project=finding.project,
                )
            )

    async def create_delivery_reconciliation_run(
        self, workflow_id: str, request: DeliveryReconcile
    ) -> None:
        await asyncio.to_thread(self._create_delivery_reconciliation_run, workflow_id, request)

    def _create_delivery_reconciliation_run(
        self, workflow_id: str, request: DeliveryReconcile
    ) -> None:
        allowed_states = {"SOURCE_PR", "CI_VALIDATED", "CANDIDATE_BUILT"}
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, request.change_set_id)
            if change_set is None:
                raise ValueError("delivery reconciliation requires an existing ChangeSet")
            if change_set.state not in allowed_states:
                raise ValueError("ChangeSet has not reached a reconcilable source PR state")
            active = session.scalar(
                select(Run).where(
                    Run.change_set_id == change_set.id,
                    Run.run_type == "delivery-reconciliation",
                    Run.status.in_({"QUEUED", "RUNNING"}),
                )
            )
            if active is not None:
                raise ValueError("an active reconciliation already owns this ChangeSet")
            session.add(
                Run(
                    change_set_id=change_set.id,
                    workflow_id=workflow_id,
                    run_type="delivery-reconciliation",
                    status="QUEUED",
                    project=change_set.project,
                    summary=(
                        str(request.candidate_build_run_id)
                        if request.candidate_build_run_id is not None
                        else None
                    ),
                )
            )

    async def create_deployment_run(self, workflow_id: str, request: DeploymentPrepare) -> Project:
        return await asyncio.to_thread(self._create_deployment_run, workflow_id, request)

    def _create_deployment_run(self, workflow_id: str, request: DeploymentPrepare) -> Project:
        deployable_states = {
            "preview": {
                "CANDIDATE_BUILT",
                "DEPLOY_PR",
                "PREVIEW_VERIFIED",
            },
            "staging": {
                "DEPLOY_PR",
                "PREVIEW_VERIFIED",
                "SOURCE_MERGED",
                "FINAL_ARTIFACT_BUILT",
            },
        }
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, request.change_set_id)
            if change_set is None:
                raise ValueError("deployment requires an existing ChangeSet")
            if change_set.state not in deployable_states[request.environment]:
                raise ValueError(
                    f"ChangeSet has not reached a deployable {request.environment} state"
                )
            active = session.scalar(
                select(Run).where(
                    Run.change_set_id == change_set.id,
                    Run.run_type == f"{request.environment}-deployment",
                    Run.status.in_({"QUEUED", "RUNNING", "AWAITING_APPROVAL"}),
                )
            )
            if active is not None:
                raise ValueError("an active deployment workflow already owns this ChangeSet target")
            project = Project(change_set.project)
            if project is Project.LLMWIKI and request.environment != "staging":
                raise ValueError("LLMWiki is staging-only")
            session.add(
                Run(
                    change_set_id=change_set.id,
                    workflow_id=workflow_id,
                    run_type=f"{request.environment}-deployment",
                    status="QUEUED",
                    project=project.value,
                )
            )
            return project

    async def create_runtime_verification_run(
        self, workflow_id: str, request: RuntimeVerificationStart
    ) -> Project:
        return await asyncio.to_thread(self._create_runtime_verification_run, workflow_id, request)

    def _create_runtime_verification_run(
        self, workflow_id: str, request: RuntimeVerificationStart
    ) -> Project:
        with self.sessions.begin() as session:
            deployment = session.get(Deployment, request.deployment_id)
            if deployment is None:
                raise ValueError("runtime verification requires an existing deployment")
            if deployment.status not in {"STAGING_DEPLOYED", "VERIFICATION_FAILED"}:
                raise ValueError("deployment has not reached runtime-verifiable state")
            active = session.scalar(
                select(Run).where(
                    Run.change_set_id == deployment.change_set_id,
                    Run.run_type == "runtime-verification",
                    Run.status.in_({"QUEUED", "RUNNING"}),
                )
            )
            if active is not None:
                raise ValueError("an active runtime verification already owns this deployment")
            project = Project(deployment.project)
            session.add(
                Run(
                    change_set_id=deployment.change_set_id,
                    workflow_id=workflow_id,
                    run_type="runtime-verification",
                    status="QUEUED",
                    project=project.value,
                    summary=str(deployment.id),
                )
            )
            return project

    async def create_recovery_run(self, workflow_id: str, request: RollbackRequest) -> Project:
        return await asyncio.to_thread(self._create_recovery_run, workflow_id, request)

    def _create_recovery_run(self, workflow_id: str, request: RollbackRequest) -> Project:
        with self.sessions.begin() as session:
            deployment = session.get(Deployment, request.deployment_id)
            if deployment is None:
                raise ValueError("recovery requires an existing deployment")
            if deployment.status not in {
                "STAGING_DEPLOYED",
                "RUNTIME_VERIFIED",
                "VERIFICATION_FAILED",
                "DEPLOYMENT_FAILED",
                "RECOVERY_FAILED",
            }:
                raise ValueError("deployment has not reached a recoverable state")
            active = session.scalar(
                select(Run).where(
                    Run.change_set_id == deployment.change_set_id,
                    Run.run_type == "recovery",
                    Run.status.in_({"QUEUED", "RUNNING", "AWAITING_APPROVAL"}),
                )
            )
            if active is not None:
                raise ValueError("an active recovery already owns this deployment")
            project = Project(deployment.project)
            session.add(
                Run(
                    change_set_id=deployment.change_set_id,
                    workflow_id=workflow_id,
                    run_type="recovery",
                    status="QUEUED",
                    project=project.value,
                    summary=str(deployment.id),
                )
            )
            return project

    async def list_campaigns(self, limit: int) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_campaigns, limit)

    def _list_campaigns(self, limit: int) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(Campaign).order_by(Campaign.created_at.desc()).limit(limit)
            ).all()
            return [serialize_row(row) for row in rows]

    async def list_backlog_campaigns(self, project: str | None, limit: int) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_backlog_campaigns, project, limit)

    def _list_backlog_campaigns(self, project: str | None, limit: int) -> list[dict[str, Any]]:
        with self.sessions() as session:
            query = select(GitHubBacklogCampaign)
            if project is not None:
                query = query.where(GitHubBacklogCampaign.project == project)
            rows = session.scalars(
                query.order_by(GitHubBacklogCampaign.imported_at.desc()).limit(limit)
            ).all()
            return [self._serialize_backlog_campaign(row) for row in rows]

    async def get_backlog_campaign(self, campaign_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_backlog_campaign, campaign_id)

    def _get_backlog_campaign(self, campaign_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(GitHubBacklogCampaign, campaign_id)
            return self._serialize_backlog_campaign(row) if row else None

    async def list_backlog_issues(self, campaign_id: UUID, limit: int) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_backlog_issues, campaign_id, limit)

    def _list_backlog_issues(self, campaign_id: UUID, limit: int) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(BacklogIssueRecord)
                .where(BacklogIssueRecord.backlog_campaign_id == campaign_id)
                .order_by(BacklogIssueRecord.planned_order)
                .limit(limit)
            ).all()
            return [self._serialize_backlog_issue(row) for row in rows]

    async def get_backlog_issue(
        self, campaign_id: UUID, repository: str, issue_number: int
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._get_backlog_issue, campaign_id, repository, issue_number
        )

    def _get_backlog_issue(
        self, campaign_id: UUID, repository: str, issue_number: int
    ) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.scalar(
                select(BacklogIssueRecord).where(
                    BacklogIssueRecord.backlog_campaign_id == campaign_id,
                    BacklogIssueRecord.repository == repository,
                    BacklogIssueRecord.issue_number == issue_number,
                )
            )
            return self._serialize_backlog_issue(row) if row else None

    async def prepare_backlog_approval(
        self, campaign_id: UUID, request: BacklogApprovalCandidateRequest
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._prepare_backlog_approval, campaign_id, request)

    def _prepare_backlog_approval(
        self, campaign_id: UUID, request: BacklogApprovalCandidateRequest
    ) -> dict[str, Any]:
        with self.sessions.begin() as session:
            campaign = session.get(GitHubBacklogCampaign, campaign_id)
            if campaign is None or campaign.project != request.project:
                raise ValueError("backlog approval campaign does not exist")
            binding = approval_binding(
                campaign_id=str(campaign.id),
                project=request.project,
                import_filter=campaign.import_filter,  # type: ignore[arg-type]
                source_head=campaign.source_head_at_import,
                deployment_head=campaign.deployment_head_at_import,
                dependency_plan_hash=campaign.plan_hash,
                maximum_risk=request.maximum_risk.value,
                maximum_items=request.maximum_items,
                delivery_mode=request.delivery_mode,
                include_enhancements=campaign.import_filter == "enhancements",
            )
            digest = binding.sha256()
            campaign.approval_binding = digest
            campaign.approval_json = {"binding": binding.model_dump(mode="json")}
            campaign.maximum_risk = request.maximum_risk.value
            campaign.maximum_items = request.maximum_items
            campaign.maximum_concurrency = 1
            campaign.delivery_mode = request.delivery_mode
            campaign.include_enhancements = binding.include_enhancements
            campaign.status = "AWAITING_APPROVAL"
            return {
                "status": "AWAITING_APPROVAL",
                "backlog_campaign_id": str(campaign.id),
                "approval_binding": digest,
                "approval_candidate": binding.model_dump(mode="json"),
                "typed_confirmation": BACKLOG_APPROVAL_PHRASE,
            }

    async def approve_backlog_campaign(
        self, campaign_id: UUID, project: str, binding: str, actor: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._approve_backlog_campaign, campaign_id, project, binding, actor
        )

    def _approve_backlog_campaign(
        self, campaign_id: UUID, project: str, binding: str, actor: str
    ) -> dict[str, Any]:
        if not actor.startswith("telegram:"):
            raise ValueError("existing-backlog approval requires an allowlisted Telegram actor")
        result: dict[str, Any]
        request: dict[str, Any]
        with self.sessions.begin() as session:
            campaign = session.get(GitHubBacklogCampaign, campaign_id)
            if (
                campaign is None
                or campaign.project != project
                or campaign.approval_binding != binding
                or campaign.status not in {"AWAITING_APPROVAL", "APPROVED_POLICY_PENDING"}
            ):
                raise ValueError("existing-backlog approval binding is not current")
            other = session.scalar(
                select(GitHubBacklogCampaign.id).where(
                    GitHubBacklogCampaign.id != campaign.id,
                    GitHubBacklogCampaign.status.in_(
                        {"APPROVED_POLICY_PENDING", "APPROVED", "RUNNING", "PAUSED"}
                    ),
                )
            )
            if other is not None:
                raise ValueError("another backlog campaign already owns GitHub write policy")
            raw_binding = campaign.approval_json.get("binding")
            if not isinstance(raw_binding, dict) or canonical_sha256(raw_binding) != binding:
                raise ValueError("existing-backlog approval candidate changed")
            expires_at = datetime.fromisoformat(str(raw_binding["expires_at"]))
            if expires_at <= datetime.now(UTC):
                raise ValueError("existing-backlog approval candidate expired")
            campaign.status = "APPROVED_POLICY_PENDING"
            campaign.approval_json = {
                "binding": raw_binding,
                "approval": {
                    "approved_by": actor,
                    "approved_at": datetime.now(UTC).isoformat(),
                    "typed_confirmation": BACKLOG_APPROVAL_PHRASE,
                },
            }
            request = {
                "action": "enable-existing-backlog",
                "backlog_campaign_id": str(campaign.id),
                "project": project,
                "approval_binding": binding,
                "safe_repositories": [
                    "gntik-ai/falcone",
                    "gntik-ai/falcone-charts",
                    "gntik-ai/musematic",
                    "gntik-ai/musematic-deploy",
                ],
                "requested_by": actor,
                "requested_at": datetime.now(UTC).isoformat(),
            }
            result = {
                "status": "APPROVED_POLICY_PENDING",
                "backlog_campaign_id": str(campaign.id),
                "approval_binding": binding,
                "policy_activation": "QUEUED_ROOT_RECONCILER",
            }
        self._write_github_policy_request(request)
        return result

    def _write_github_policy_request(self, request: dict[str, Any]) -> None:
        root = self.github_policy_request_root
        metadata = root.stat()
        if not root.is_dir() or metadata.st_mode & 0o022:
            raise ValueError("GitHub policy request directory is unavailable or insecure")
        target = root / "activate.json"
        temporary = root / f".activate-{os.getpid()}.tmp"
        content = json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _serialize_backlog_campaign(row: GitHubBacklogCampaign) -> dict[str, Any]:
        value = serialize_row(row)
        value["backlog_campaign_id"] = value.pop("id")
        value.pop("approval_json", None)
        return value

    @staticmethod
    def _serialize_backlog_issue(row: BacklogIssueRecord) -> dict[str, Any]:
        value = serialize_row(row)
        for source, target in (
            ("labels_json", "labels"),
            ("documentation_levels_json", "documentation_levels"),
            ("dependencies_json", "dependencies"),
            ("blocking_issues_json", "blocking_issues"),
            ("issues_blocked_json", "issues_blocked"),
            ("linked_prs_json", "linked_prs"),
            ("linked_branches_json", "linked_branches"),
            ("linked_commits_json", "linked_commits"),
            ("duplicate_candidates_json", "duplicate_candidates"),
        ):
            value[target] = value.pop(source)
        value["repository_short_name"] = row.repository.rsplit("/", 1)[-1]
        value["github_updated_at"] = row.github_updated_at.astimezone(UTC).isoformat()
        value["finding_id"] = str(row.finding_id) if row.finding_id else None
        value["change_set_id"] = str(row.change_set_id) if row.change_set_id else None
        value.pop("github_snapshot_json", None)
        return value

    async def get_finding(self, finding_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_finding, finding_id)

    def _get_finding(self, finding_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(Finding, finding_id)
            return serialize_row(row) if row else None

    async def list_findings(self, limit: int) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_findings, limit)

    def _list_findings(self, limit: int) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(Finding).order_by(Finding.created_at.desc()).limit(limit)
            ).all()
            return [serialize_row(row) for row in rows]

    async def list_enhancements(
        self, project: Project, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_enhancements, project, status, limit)

    def _list_enhancements(
        self, project: Project, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        with self.sessions() as session:
            query = select(Enhancement).where(Enhancement.project == project.value)
            if status is not None:
                query = query.where(Enhancement.status == status)
            rows = session.scalars(query.order_by(Enhancement.created_at.desc()).limit(limit)).all()
            return [serialize_row(row) for row in rows]

    async def get_enhancement(self, enhancement_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_enhancement, enhancement_id)

    def _get_enhancement(self, enhancement_id: str) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.scalar(
                select(Enhancement).where(Enhancement.enhancement_key == enhancement_id)
            )
            return serialize_row(row) if row else None

    async def approve_enhancement(
        self, enhancement_id: str, binding: str, actor: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._approve_enhancement, enhancement_id, binding, actor)

    def _approve_enhancement(self, enhancement_id: str, binding: str, actor: str) -> dict[str, Any]:
        with self.sessions.begin() as session:
            row = session.scalar(
                select(Enhancement)
                .where(Enhancement.enhancement_key == enhancement_id)
                .with_for_update()
            )
            if row is None:
                raise ValueError("enhancement not found")
            if row.status != "CANDIDATE_ENHANCEMENT":
                raise ValueError("only a candidate enhancement can be approved")
            if row.approval_binding_sha256 != binding:
                raise ValueError("enhancement approval binding changed")
            row.status = "APPROVED"
            row.payload_json = {
                **row.payload_json,
                "approval": {"actor": actor, "recorded_at": datetime.now(UTC).isoformat()},
            }
            session.flush()
            return serialize_row(row)

    async def reject_enhancement(
        self, enhancement_id: str, reason: str, actor: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._reject_enhancement, enhancement_id, reason, actor)

    def _reject_enhancement(self, enhancement_id: str, reason: str, actor: str) -> dict[str, Any]:
        with self.sessions.begin() as session:
            row = session.scalar(
                select(Enhancement)
                .where(Enhancement.enhancement_key == enhancement_id)
                .with_for_update()
            )
            if row is None:
                raise ValueError("enhancement not found")
            if row.status not in {"CANDIDATE_ENHANCEMENT", "APPROVED"}:
                raise ValueError("enhancement cannot be rejected from its current state")
            row.status = "REJECTED"
            row.payload_json = {
                **row.payload_json,
                "rejection": {
                    "actor": actor,
                    "reason": reason,
                    "recorded_at": datetime.now(UTC).isoformat(),
                },
            }
            session.flush()
            return serialize_row(row)

    async def get_artifact(self, artifact_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_artifact, artifact_id)

    def _get_artifact(self, artifact_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(Artifact, artifact_id)
            return serialize_row(row) if row else None

    async def resolve_evidence_binding(
        self,
        workflow_id: str,
        project: Project,
        campaign_id: UUID | None,
        change_set_id: UUID | None,
    ) -> dict[str, UUID | None]:
        return await asyncio.to_thread(
            self._resolve_evidence_binding,
            workflow_id,
            project,
            campaign_id,
            change_set_id,
        )

    def _resolve_evidence_binding(
        self,
        workflow_id: str,
        project: Project,
        campaign_id: UUID | None,
        change_set_id: UUID | None,
    ) -> dict[str, UUID | None]:
        with self.sessions() as session:
            run = session.scalar(
                select(Run)
                .where(Run.workflow_id == workflow_id)
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            if run is None or run.project != project.value:
                raise ValueError("evidence workflow binding does not exist for this project")
            if campaign_id is not None and run.campaign_id != campaign_id:
                raise ValueError("evidence campaign does not match its authoritative run")
            if change_set_id is not None and run.change_set_id != change_set_id:
                raise ValueError("evidence ChangeSet does not match its authoritative run")
            return {
                "run_id": run.id,
                "campaign_id": run.campaign_id,
                "change_set_id": run.change_set_id,
            }

    async def persist_artifact(self, metadata: ArtifactMetadata) -> dict[str, Any]:
        return await asyncio.to_thread(self._persist_artifact, metadata)

    def _persist_artifact(self, metadata: ArtifactMetadata) -> dict[str, Any]:
        with self.sessions.begin() as session:
            existing = session.get(Artifact, metadata.artifact_id)
            expected = metadata.model_dump(mode="json")
            if existing is not None:
                serialized = serialize_row(existing)
                comparable = {
                    "id": expected["artifact_id"],
                    "project": expected["project"],
                    "campaign_id": expected["campaign_id"],
                    "change_set_id": expected["change_set_id"],
                    "run_id": expected["run_id"],
                    "relative_path": expected["relative_path"],
                    "sha256": expected["sha256"],
                    "content_type": expected["content_type"],
                    "producer": expected["producer"],
                    "redaction_status": expected["redaction_status"],
                    "size_bytes": expected["size_bytes"],
                    "retention_class": expected["retention_class"],
                }
                if any(serialized.get(key) != value for key, value in comparable.items()):
                    raise ValueError("artifact idempotency key conflicts with stored evidence")
                return serialized
            row = Artifact(
                id=metadata.artifact_id,
                project=metadata.project.value,
                campaign_id=metadata.campaign_id,
                change_set_id=metadata.change_set_id,
                run_id=metadata.run_id,
                relative_path=metadata.relative_path,
                sha256=metadata.sha256,
                content_type=metadata.content_type,
                producer=metadata.producer,
                redaction_status=metadata.redaction_status.value,
                size_bytes=metadata.size_bytes,
                retention_class=metadata.retention_class,
                purged_at=None,
                purge_reason=None,
            )
            session.add(row)
            session.flush()
            return serialize_row(row)

    async def persist_worker_heartbeat(self, identity: str, hostname: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._persist_worker_heartbeat, identity, hostname)

    def _persist_worker_heartbeat(self, identity: str, hostname: str) -> dict[str, Any]:
        allowed = {
            "codex-runner": "codex",
            "claude-runner": "claude",
            "browser-runner": "browser",
            "scan-runner": "scan",
            "delivery-runner": "delivery",
            "release-runner": "release",
        }
        worker_kind = allowed.get(identity)
        if worker_kind is None:
            raise ValueError("worker heartbeat identity is not allowlisted")
        if not hostname or len(hostname) > 253:
            raise ValueError("worker heartbeat hostname is invalid")
        heartbeat_id = UUID(bytes=hashlib.sha256(f"worker:{identity}".encode()).digest()[:16])
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(WorkerHeartbeat, heartbeat_id)
            if row is None:
                row = WorkerHeartbeat(
                    id=heartbeat_id,
                    identity=identity,
                    hostname=hostname,
                    worker_kind=worker_kind,
                    observed_at=now,
                )
                session.add(row)
            else:
                row.hostname = hostname
                row.worker_kind = worker_kind
                row.observed_at = now
            session.flush()
            return serialize_row(row)

    async def metrics_snapshot(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._metrics_snapshot)

    def _metrics_snapshot(self) -> dict[str, Any]:
        active_run_statuses = {"QUEUED", "RUNNING", "AWAITING_APPROVAL", "RATE_LIMITED"}
        active_change_states = {
            "PLANNED",
            "IMPLEMENTING",
            "SOURCE_PR",
            "CI_VALIDATED",
            "CANDIDATE_BUILT",
            "PREVIEW_DEPLOYED",
            "PREVIEW_VERIFIED",
            "SOURCE_MERGED",
            "FINAL_ARTIFACT_BUILT",
            "DEPLOY_PR",
            "AWAITING_STAGING_APPROVAL",
            "STAGING_APPROVED",
            "STAGING_DEPLOYED",
        }
        with self.sessions() as session:
            active_campaigns = session.execute(
                select(Campaign.project, func.count())
                .where(Campaign.status.in_({"QUEUED", "RUNNING", "IN_PROGRESS", "PAUSED"}))
                .group_by(Campaign.project)
            ).all()
            queued_findings = session.execute(
                select(Finding.project, Finding.disposition, func.count())
                .where(Finding.disposition.in_({"CANDIDATE", "CONFIRMED", "BLOCKED"}))
                .group_by(Finding.project, Finding.disposition)
            ).all()
            active_implementations = session.execute(
                select(ChangeSet.project, ChangeSet.state, func.count())
                .where(ChangeSet.state.in_(active_change_states))
                .group_by(ChangeSet.project, ChangeSet.state)
            ).all()
            runs = session.execute(
                select(Run.project, Run.status, func.count()).group_by(Run.project, Run.status)
            ).all()
            active_runs = session.execute(
                select(Run.project, func.count())
                .where(Run.status.in_(active_run_statuses))
                .group_by(Run.project)
            ).all()
            rate_limits = session.execute(
                select(AgentInvocation.provider, func.count())
                .where(
                    or_(
                        AgentInvocation.status == "RATE_LIMITED",
                        AgentInvocation.error_code == "RATE_LIMITED",
                    )
                )
                .group_by(AgentInvocation.provider)
            ).all()
            heartbeats = session.scalars(select(WorkerHeartbeat)).all()
            deployments = session.scalars(select(Deployment)).all()
            verification_failures = session.execute(
                select(
                    ChangeSet.project,
                    VerificationResult.verification_type,
                    func.count(),
                )
                .join(ChangeSet, ChangeSet.id == VerificationResult.change_set_id)
                .where(VerificationResult.status.not_in({"PASS", "SUCCESS", "VERIFIED"}))
                .group_by(ChangeSet.project, VerificationResult.verification_type)
            ).all()
            evidence = session.execute(
                select(Artifact.project, Artifact.retention_class, func.sum(Artifact.size_bytes))
                .where(Artifact.purged_at.is_(None))
                .group_by(Artifact.project, Artifact.retention_class)
            ).all()
        deployment_durations: dict[tuple[str, str], list[float]] = {}
        deployment_failures: dict[tuple[str, str], int] = {}
        for deployment in deployments:
            key = (deployment.project, deployment.environment)
            if deployment.started_at is not None and deployment.finished_at is not None:
                duration = (deployment.finished_at - deployment.started_at).total_seconds()
                deployment_durations.setdefault(key, []).append(max(0.0, duration))
            if deployment.status in {"DEPLOYMENT_FAILED", "VERIFICATION_FAILED", "RECOVERY_FAILED"}:
                deployment_failures[key] = deployment_failures.get(key, 0) + 1
        return {
            "active_campaigns": [list(row) for row in active_campaigns],
            "queued_findings": [list(row) for row in queued_findings],
            "active_implementations": [list(row) for row in active_implementations],
            "runs": [list(row) for row in runs],
            "active_runs": [list(row) for row in active_runs],
            "provider_rate_limits": [list(row) for row in rate_limits],
            "worker_heartbeats": [serialize_row(row) for row in heartbeats],
            "deployment_durations": [
                [project, environment, sum(values) / len(values)]
                for (project, environment), values in sorted(deployment_durations.items())
            ],
            "deployment_failures": [
                [project, environment, count]
                for (project, environment), count in sorted(deployment_failures.items())
            ],
            "verification_failures": [list(row) for row in verification_failures],
            "evidence_storage": [
                [project, retention, int(size or 0)] for project, retention, size in evidence
            ],
        }

    async def get_deployment(self, deployment_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_deployment, deployment_id)

    def _get_deployment(self, deployment_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(Deployment, deployment_id)
            return serialize_row(row) if row else None

    async def latest_quota_snapshots(self) -> dict[str, dict[str, Any]]:
        return await asyncio.to_thread(self._latest_quota_snapshots)

    def _latest_quota_snapshots(self) -> dict[str, dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(QuotaSnapshot).order_by(QuotaSnapshot.captured_at.desc())
            ).all()
            latest: dict[str, dict[str, Any]] = {}
            for row in rows:
                if row.provider not in latest:
                    latest[row.provider] = serialize_row(row)
            return latest

    async def provider_usage_history(self, days: int = 7) -> dict[str, dict[str, Any]]:
        if not 1 <= days <= 31:
            raise ValueError("provider usage history window must be between 1 and 31 days")
        return await asyncio.to_thread(self._provider_usage_history, days)

    def _provider_usage_history(self, days: int) -> dict[str, dict[str, Any]]:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        with self.sessions() as session:
            invocation_rows = session.execute(
                select(
                    AgentInvocation.provider,
                    AgentInvocation.role,
                    func.count(),
                    func.sum(case((AgentInvocation.status == "SUCCESS", 1), else_=0)),
                )
                .where(AgentInvocation.started_at >= cutoff)
                .group_by(AgentInvocation.provider, AgentInvocation.role)
            ).all()
            usage_rows = session.execute(
                select(
                    ProviderUsage.provider,
                    ProviderUsage.role,
                    func.sum(ProviderUsage.input_tokens),
                    func.sum(ProviderUsage.output_tokens),
                    func.sum(ProviderUsage.cached_input_tokens),
                    func.sum(ProviderUsage.estimated_cost),
                )
                .where(ProviderUsage.created_at >= cutoff)
                .group_by(ProviderUsage.provider, ProviderUsage.role)
            ).all()
            running_rows = session.execute(
                select(AgentInvocation.provider, func.count())
                .where(AgentInvocation.status == "RUNNING")
                .group_by(AgentInvocation.provider)
            ).all()
        history: dict[str, dict[str, Any]] = {}
        for provider, role, invocations, successes in invocation_rows:
            count = int(invocations or 0)
            success_count = int(successes or 0)
            provider_record = history.setdefault(
                str(provider), {"window_days": days, "running_jobs": 0, "roles": {}}
            )
            provider_record["roles"][str(role)] = {
                "invocations": count,
                "successes": success_count,
                "success_rate": success_count / count if count else None,
                "input_tokens": None,
                "output_tokens": None,
                "cached_input_tokens": None,
                "estimated_cost": None,
            }
        for (
            provider,
            role,
            input_tokens,
            output_tokens,
            cached_tokens,
            estimated_cost,
        ) in usage_rows:
            provider_record = history.setdefault(
                str(provider), {"window_days": days, "running_jobs": 0, "roles": {}}
            )
            role_record = provider_record["roles"].setdefault(
                str(role),
                {
                    "invocations": 0,
                    "successes": 0,
                    "success_rate": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "cached_input_tokens": None,
                    "estimated_cost": None,
                },
            )
            role_record.update(
                {
                    "input_tokens": int(input_tokens) if input_tokens is not None else None,
                    "output_tokens": int(output_tokens) if output_tokens is not None else None,
                    "cached_input_tokens": (
                        int(cached_tokens) if cached_tokens is not None else None
                    ),
                    "estimated_cost": (
                        float(estimated_cost) if estimated_cost is not None else None
                    ),
                }
            )
        for provider, running_jobs in running_rows:
            provider_record = history.setdefault(
                str(provider), {"window_days": days, "running_jobs": 0, "roles": {}}
            )
            provider_record["running_jobs"] = int(running_jobs or 0)
        return history

    async def workflow_for_change_set(self, change_set_id: UUID) -> str | None:
        return await asyncio.to_thread(self._workflow_for_change_set, change_set_id)

    def _workflow_for_change_set(self, change_set_id: UUID) -> str | None:
        with self.sessions() as session:
            run = session.scalar(
                select(Run)
                .join(ChangeSet, ChangeSet.id == Run.change_set_id)
                .where(ChangeSet.id == change_set_id)
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            return run.workflow_id if run else None

    async def current_approval_candidate(self, change_set_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._current_approval_candidate, change_set_id)

    def _current_approval_candidate(self, change_set_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            candidate = session.scalar(
                select(ApprovalCandidate)
                .where(
                    ApprovalCandidate.change_set_id == change_set_id,
                    ApprovalCandidate.invalidated_at.is_(None),
                )
                .order_by(ApprovalCandidate.created_at.desc())
                .limit(1)
            )
            return serialize_row(candidate) if candidate else None

    async def persist_approval(self, request: ApprovalRecord, approver_subject: str) -> None:
        await asyncio.to_thread(self._persist_approval, request, approver_subject)

    def _persist_approval(self, request: ApprovalRecord, approver_subject: str) -> None:
        with self.sessions.begin() as session:
            candidate = session.scalar(
                select(ApprovalCandidate)
                .where(
                    ApprovalCandidate.change_set_id == request.change_set_id,
                    ApprovalCandidate.invalidated_at.is_(None),
                )
                .with_for_update()
            )
            if candidate is None:
                raise ValueError("approval candidate is missing or invalidated")
            if candidate.artifact_binding_sha256 != request.artifact_binding_sha256:
                raise ValueError("approval artifact binding changed")
            candidate_payload = (
                candidate.payload_json if isinstance(candidate.payload_json, dict) else {}
            )
            approval_type = str(candidate_payload.get("approval_type", "staging"))
            if approval_type not in {"preview", "staging", "recovery"}:
                raise ValueError("approval candidate type is not allowlisted")
            session.add(
                Approval(
                    change_set_id=request.change_set_id,
                    approval_type=approval_type,
                    decision=request.decision,
                    artifact_binding_sha256=request.artifact_binding_sha256,
                    approver_subject=approver_subject,
                    approved_at=datetime.now(UTC),
                    metadata_json={
                        "candidate_id": str(candidate.id),
                        "typed_confirmation_present": request.typed_confirmation is not None,
                    },
                )
            )


class EmptyMetadataRepository:
    async def create_campaign(
        self, campaign_id: UUID, workflow_id: str, request: CampaignStart
    ) -> None:
        return None

    async def create_delivery_run(self, workflow_id: str, request: DeliveryStart) -> None:
        return None

    async def create_finding_verification_run(self, workflow_id: str, finding_id: UUID) -> Project:
        raise ValueError("finding verification metadata store is unavailable")

    async def create_delivery_reconciliation_run(
        self, workflow_id: str, request: DeliveryReconcile
    ) -> None:
        return None

    async def create_deployment_run(self, workflow_id: str, request: DeploymentPrepare) -> Project:
        raise ValueError("deployment metadata store is unavailable")

    async def create_runtime_verification_run(
        self, workflow_id: str, request: RuntimeVerificationStart
    ) -> Project:
        raise ValueError("runtime verification metadata store is unavailable")

    async def create_recovery_run(self, workflow_id: str, request: RollbackRequest) -> Project:
        raise ValueError("recovery metadata store is unavailable")

    async def list_campaigns(self, limit: int) -> list[dict[str, Any]]:
        return []

    async def list_backlog_campaigns(self, project: str | None, limit: int) -> list[dict[str, Any]]:
        return []

    async def get_backlog_campaign(self, campaign_id: UUID) -> dict[str, Any] | None:
        return None

    async def list_backlog_issues(self, campaign_id: UUID, limit: int) -> list[dict[str, Any]]:
        return []

    async def get_backlog_issue(
        self, campaign_id: UUID, repository: str, issue_number: int
    ) -> dict[str, Any] | None:
        return None

    async def prepare_backlog_approval(
        self, campaign_id: UUID, request: BacklogApprovalCandidateRequest
    ) -> dict[str, Any]:
        raise ValueError("backlog metadata store is unavailable")

    async def approve_backlog_campaign(
        self, campaign_id: UUID, project: str, binding: str, actor: str
    ) -> dict[str, Any]:
        raise ValueError("backlog metadata store is unavailable")

    async def get_finding(self, finding_id: UUID) -> dict[str, Any] | None:
        return None

    async def list_findings(self, limit: int) -> list[dict[str, Any]]:
        return []

    async def list_enhancements(
        self, project: Project, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        return []

    async def get_enhancement(self, enhancement_id: str) -> dict[str, Any] | None:
        return None

    async def approve_enhancement(
        self, enhancement_id: str, binding: str, actor: str
    ) -> dict[str, Any]:
        raise ValueError("enhancement metadata store is unavailable")

    async def reject_enhancement(
        self, enhancement_id: str, reason: str, actor: str
    ) -> dict[str, Any]:
        raise ValueError("enhancement metadata store is unavailable")

    async def get_artifact(self, artifact_id: UUID) -> dict[str, Any] | None:
        return None

    async def get_deployment(self, deployment_id: UUID) -> dict[str, Any] | None:
        return None

    async def latest_quota_snapshots(self) -> dict[str, dict[str, Any]]:
        return {}

    async def provider_usage_history(self, days: int = 7) -> dict[str, dict[str, Any]]:
        return {}

    async def workflow_for_change_set(self, change_set_id: UUID) -> str | None:
        return None

    async def current_approval_candidate(self, change_set_id: UUID) -> dict[str, Any] | None:
        return None

    async def persist_approval(self, request: ApprovalRecord, approver_subject: str) -> None:
        raise ValueError("approval metadata store is unavailable")

    async def resolve_evidence_binding(
        self,
        workflow_id: str,
        project: Project,
        campaign_id: UUID | None,
        change_set_id: UUID | None,
    ) -> dict[str, UUID | None]:
        raise ValueError("evidence metadata store is unavailable")

    async def persist_artifact(self, metadata: ArtifactMetadata) -> dict[str, Any]:
        raise ValueError("evidence metadata store is unavailable")

    async def persist_worker_heartbeat(self, identity: str, hostname: str) -> dict[str, Any]:
        raise ValueError("worker heartbeat metadata store is unavailable")

    async def metrics_snapshot(self) -> dict[str, Any]:
        return {}
