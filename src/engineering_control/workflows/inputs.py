from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AuditCampaignInput:
    project: str
    name: str
    source_sha: str
    deployment_sha: str
    domains: list[str]
    personas: list[str]
    lenses: list[str]
    environments: list[str]
    dry_run: bool = True
    campaign_id: str | None = None
    cell_offset: int = 0
    snapshot_blocker: str | None = None


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
