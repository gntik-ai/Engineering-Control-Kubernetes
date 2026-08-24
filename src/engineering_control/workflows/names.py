from enum import StrEnum


class WorkflowName(StrEnum):
    AUDIT_CAMPAIGN = "AuditCampaignWorkflow"
    FINDING_VERIFICATION = "FindingVerificationWorkflow"
    ISSUE_DELIVERY = "IssueDeliveryWorkflow"
    DELIVERY_RECONCILIATION = "DeliveryReconciliationWorkflow"
    PREVIEW_DEPLOYMENT = "PreviewDeploymentWorkflow"
    STAGING_DEPLOYMENT = "StagingDeploymentWorkflow"
    RUNTIME_VERIFICATION = "RuntimeVerificationWorkflow"
    RECOVERY = "RecoveryWorkflow"
    QUOTA_REFRESH = "QuotaRefreshWorkflow"
    CLEANUP = "CleanupWorkflow"
    ACCEPTANCE_LIFECYCLE = "AcceptanceLifecycleWorkflow"
    ISSUE_SOLUTION_LOOP = "IssueSolutionLoopWorkflow"
    ENHANCEMENT_DISCOVERY = "EnhancementDiscoveryWorkflow"
    ENHANCEMENT_DELIVERY = "EnhancementDeliveryWorkflow"
    ENHANCEMENT_SOLUTION_LOOP = "EnhancementSolutionLoopWorkflow"
