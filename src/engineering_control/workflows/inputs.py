from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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
class MultiProjectAuditInput:
    preset: str
    environment: str = "staging"
    publish_findings: bool = False
    campaign_ids: dict[str, str] = field(default_factory=dict)
    child_workflow_ids: dict[str, str] = field(default_factory=dict)
    application_run_id: str | None = None


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
    issue_approval_id: str | None = None

    def payload(self) -> dict[str, Any]:
        """Preserve legacy command bytes when no child approval was supplied."""
        value = asdict(self)
        if self.issue_approval_id is None:
            value.pop("issue_approval_id")
        return value


@dataclass
class DeliveryReconciliationInput:
    change_set_id: str
    candidate_build_run_id: int | None = None
    dry_run: bool = True


@dataclass
class FinalClosureInput:
    backlog_campaign_id: str
    change_set_id: str
    finding_id: str
    project: str
    issue_repository: str
    issue_number: int
    r3_approval_binding: str | None = None


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
class ConnectionsRefreshInput:
    scope: str = "all"


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
    # NEW-215: scope the import to one GitHub label (None: every open issue).
    issue_label: str | None = None


@dataclass
class IssueAmendObservationInput:
    """NEW-115: one backlog issue whose current GitHub body an operator amendment reads."""

    issue_repository: str
    issue_number: int


@dataclass
class PublicationHeadsObservationInput:
    """NEW-145: the bound pull requests whose live heads a publication reconciliation reads."""

    pull_request_urls: list[str]
    # NEW-145b: head branches whose every PR the reconciliation must see (pr_create effects).
    branch_listings: list[dict[str, str]] | None = None


@dataclass
class ExistingGitHubBacklogLoopInput:
    project: str
    backlog_campaign_id: str
    maximum_risk: str
    delivery_mode: str
    maximum_items: int = 1
    target_repository: str | None = None
    target_issue_number: int | None = None
    continue_after_blocked_issue: bool = True
    correction_limit: int = 2
    global_campaign_id: str | None = None
    r3_approval_binding: str | None = None
    runtime_proof: str = "runtime"
    issue_operation_id: str | None = None
    issue_approval_binding: str | None = None
    # NEW-214b: the approved concurrency; earlier histories carry none and replay as 1.
    maximum_concurrency: int = 1


@dataclass
class AutonomousBacklogCampaignInput:
    campaign_id: str
    backlog_campaign_ids: dict[str, str]
    policy_campaign_id: str
    environment: str = "staging"
    maximum_risk: str = "R2"
    global_active_issues: int = 1
    global_deployments: int = 1
    implementation_correction_limit: int = 5
    deployment_correction_limit: int = 3
    production: bool = False


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
class ChangeRequestInput:
    change_request_id: str
    revision: int


@dataclass
class UiMigrationPlanInput:
    """NEW-123: one flow A blueprint run (the row is created by the API first)."""

    blueprint_id: str
    project: str
    requested_by: str
    # Optional: the expected legacy main head (a full SHA); default is the current head.
    source_sha: str | None = None


@dataclass
class UiBlueprintFanOutInput:
    """NEW-123: one resumable fan-out run of an approved blueprint (continue-as-new state)."""

    blueprint_id: str
    max_items: int = 80
    processed: int = 0
    drafted: int = 0
    failed: int = 0
    skipped: int = 0
    source_worktree: str | None = None


@dataclass
class UiGapConsolidationPlanInput:
    """NEW-154: one gap consolidation plan run (its durable record carries the inputs)."""

    consolidation_id: str


@dataclass
class LLMWikiStagingBootstrapInput:
    project: str = "llmwiki"
    environment: str = "staging"
    approval_binding: str | None = None
    execute: bool = False


@dataclass
class StagingGitOpsDeliveryInput:
    change_set_id: str
    project: str
    issue_repository: str
    issue_number: int
    finding_id: str | None = None
    backlog_campaign_id: str | None = None
