from enum import StrEnum


class WorkflowName(StrEnum):
    AUDIT_CAMPAIGN = "AuditCampaignWorkflow"
    FINDING_VERIFICATION = "FindingVerificationWorkflow"
    ISSUE_DELIVERY = "IssueDeliveryWorkflow"
    PREVIEW_DEPLOYMENT = "PreviewDeploymentWorkflow"
    STAGING_DEPLOYMENT = "StagingDeploymentWorkflow"
    RUNTIME_VERIFICATION = "RuntimeVerificationWorkflow"
    RECOVERY = "RecoveryWorkflow"
    QUOTA_REFRESH = "QuotaRefreshWorkflow"
    CLEANUP = "CleanupWorkflow"
    ACCEPTANCE_LIFECYCLE = "AcceptanceLifecycleWorkflow"
