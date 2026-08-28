from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AuditCampaignInput:
    project: str
    name: str = "audit"
    preset: str = "snapshot"
    environment: str = "staging"
    source_sha: str | None = None
    deployment_sha: str | None = None
    repository_surfaces: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    personas: list[str] = field(default_factory=list)
    flows: list[str] = field(default_factory=list)
    lenses: list[str] = field(default_factory=list)
    environments: list[str] = field(default_factory=list)
    dry_run: bool = True
    publish_findings: bool = False
    plan_only: bool = False
    application_data_mutation_approved: bool = False
    campaign_id: str | None = None
    cell_offset: int = 0
    snapshot_blocker: str | None = None
    root_blocker_classification: str | None = None


@dataclass
class FindingVerificationInput:
    finding_id: str
    project: str
    fresh_snapshot: bool = True


@dataclass
class IssueDeliveryInput:
    finding_id: str
    risk_class: str
    dry_run: bool = True


@dataclass
class DeliveryReconciliationInput:
    change_set_id: str
    candidate_build_run_id: int | None = None
    dry_run: bool = True


@dataclass
class PreviewDeploymentInput:
    change_set_id: str
    project: str
    dry_run: bool = True


@dataclass
class StagingDeploymentInput:
    change_set_id: str
    environment: str = "staging"
    dry_run: bool = True


@dataclass
class RuntimeVerificationInput:
    deployment_id: str
    project: str


@dataclass
class RecoveryInput:
    deployment_id: str
    expected_artifact_binding_sha256: str
    dry_run: bool = True


@dataclass
class QuotaRefreshInput:
    providers: list[str] = field(default_factory=lambda: ["codex", "claude"])


@dataclass
class CleanupInput:
    dry_run: bool = True
    retention_policy: str = "standard"


@dataclass
class AcceptanceLifecycleInput:
    change_set_id: str
    validation_root: str
    hold_seconds: int = 5
    activity_task_queue: str = "engineering-acceptance"


@dataclass
class ObservabilityAcceptanceInput:
    workspace_id: str


@dataclass
class IssueSolutionLoopInput:
    project: str
    campaign_id: str
    maximum_risk: str = "R1"
    maximum_items: int = 1
    execution_mode: str = "plan-only"
    component_concurrency: int = 1
    stop_policy: str = "pause-on-blocker"
    dry_run: bool = True


@dataclass
class GitHubBacklogImportInput:
    backlog_campaign_id: str
    project: str
    source: str = "github-open"
    import_filter: str = "bugs"
    maximum_items: int = 500


@dataclass
class ExistingGitHubBacklogLoopInput:
    project: str
    backlog_campaign_id: str
    maximum_risk: str
    delivery_mode: str
    maximum_items: int = 1


@dataclass
class EnhancementDiscoveryInput:
    project: str
    campaign_id: str
    dry_run: bool = True


@dataclass
class EnhancementDeliveryInput:
    enhancement_id: str
    approval_binding: str
    publish: bool = False


@dataclass
class EnhancementSolutionLoopInput:
    enhancement_id: str
    approval_binding: str
    risk: str
    execution_mode: str = "plan-only"
    dry_run: bool = True


@dataclass
class LLMWikiStagingBootstrapInput:
    project: str = "llmwiki"
    environment: str = "staging"
    approval_binding: str | None = None
    execute: bool = False
