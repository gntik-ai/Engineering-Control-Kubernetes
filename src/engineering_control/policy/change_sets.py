from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from engineering_control.models.core import ChangeSet
from engineering_control.models.enums import ChangeSetState, RiskClass


@dataclass(frozen=True)
class TransitionEvidence:
    finding_confirmed: bool = False
    plan_approved: bool = False
    independent_review_passed: bool = False
    second_provider_review_passed: bool = False
    ci_passed: bool = False
    candidate_digest_present: bool = False
    preview_verified: bool = False
    source_merged: bool = False
    deployment_pr_protected: bool = False
    approval_current: bool = False
    runtime_verified: bool = False
    issue_closed: bool = False
    recovery_evidence_present: bool = False


class ChangeSetPolicy:
    _allowed: dict[ChangeSetState, set[ChangeSetState]] = {
        ChangeSetState.CANDIDATE: {
            ChangeSetState.REJECTED,
            ChangeSetState.BLOCKED,
            ChangeSetState.CONFIRMED,
            ChangeSetState.CANCELLED,
        },
        ChangeSetState.BLOCKED: {
            ChangeSetState.CANDIDATE,
            ChangeSetState.CONFIRMED,
            ChangeSetState.CANCELLED,
        },
        ChangeSetState.CONFIRMED: {
            ChangeSetState.PLANNED,
            ChangeSetState.BLOCKED,
            ChangeSetState.CANCELLED,
        },
        ChangeSetState.PLANNED: {
            ChangeSetState.IMPLEMENTING,
            ChangeSetState.REQUEST_CHANGES,
            ChangeSetState.CANCELLED,
        },
        ChangeSetState.REQUEST_CHANGES: {
            ChangeSetState.PLANNED,
            ChangeSetState.IMPLEMENTING,
            ChangeSetState.CANCELLED,
        },
        ChangeSetState.IMPLEMENTING: {
            ChangeSetState.SOURCE_PR,
            ChangeSetState.BLOCKED,
            ChangeSetState.REQUEST_CHANGES,
            ChangeSetState.CANCELLED,
        },
        ChangeSetState.SOURCE_PR: {
            ChangeSetState.CI_VALIDATED,
            ChangeSetState.REQUEST_CHANGES,
            ChangeSetState.BLOCKED,
        },
        ChangeSetState.CI_VALIDATED: {
            ChangeSetState.CANDIDATE_BUILT,
            ChangeSetState.REQUEST_CHANGES,
        },
        ChangeSetState.CANDIDATE_BUILT: {
            ChangeSetState.PREVIEW_DEPLOYED,
            ChangeSetState.SOURCE_MERGED,
            ChangeSetState.DEPLOY_PR,
            ChangeSetState.REQUEST_CHANGES,
        },
        ChangeSetState.PREVIEW_DEPLOYED: {
            ChangeSetState.PREVIEW_VERIFIED,
            ChangeSetState.DEPLOYMENT_FAILED,
        },
        ChangeSetState.PREVIEW_VERIFIED: {
            ChangeSetState.SOURCE_MERGED,
            ChangeSetState.REQUEST_CHANGES,
        },
        ChangeSetState.SOURCE_MERGED: {
            ChangeSetState.FINAL_ARTIFACT_BUILT,
            ChangeSetState.FORWARD_RECOVERY_REQUIRED,
        },
        ChangeSetState.FINAL_ARTIFACT_BUILT: {
            ChangeSetState.DEPLOY_PR,
            ChangeSetState.FORWARD_RECOVERY_REQUIRED,
        },
        ChangeSetState.DEPLOY_PR: {
            ChangeSetState.AWAITING_STAGING_APPROVAL,
            ChangeSetState.REQUEST_CHANGES,
        },
        ChangeSetState.AWAITING_STAGING_APPROVAL: {
            ChangeSetState.STAGING_APPROVED,
            ChangeSetState.REQUEST_CHANGES,
            ChangeSetState.CANCELLED,
        },
        ChangeSetState.STAGING_APPROVED: {
            ChangeSetState.STAGING_DEPLOYED,
            ChangeSetState.DEPLOYMENT_FAILED,
        },
        ChangeSetState.STAGING_DEPLOYED: {
            ChangeSetState.RUNTIME_VERIFIED,
            ChangeSetState.DEPLOYMENT_FAILED,
            ChangeSetState.INCIDENT_OPEN,
        },
        ChangeSetState.RUNTIME_VERIFIED: {
            ChangeSetState.ISSUE_CLOSED,
            ChangeSetState.INCIDENT_OPEN,
        },
        ChangeSetState.DEPLOYMENT_FAILED: {
            ChangeSetState.ROLLED_BACK,
            ChangeSetState.FORWARD_RECOVERY_REQUIRED,
            ChangeSetState.INCIDENT_OPEN,
        },
        ChangeSetState.INCIDENT_OPEN: {
            ChangeSetState.ROLLED_BACK,
            ChangeSetState.FORWARD_RECOVERY_REQUIRED,
        },
        ChangeSetState.FORWARD_RECOVERY_REQUIRED: {
            ChangeSetState.IMPLEMENTING,
            ChangeSetState.INCIDENT_OPEN,
        },
    }

    @classmethod
    def transition(
        cls,
        change_set: ChangeSet,
        target: ChangeSetState,
        evidence: TransitionEvidence,
    ) -> ChangeSet:
        if target not in cls._allowed.get(change_set.state, set()):
            raise ValueError(f"illegal ChangeSet transition {change_set.state} -> {target}")
        cls._validate_gate(change_set, target, evidence)
        updated = change_set.model_copy(deep=True)
        updated.state = target
        updated.version += 1
        updated.updated_at = datetime.now(UTC)
        return updated

    @staticmethod
    def _validate_gate(
        change_set: ChangeSet, target: ChangeSetState, evidence: TransitionEvidence
    ) -> None:
        required = {
            ChangeSetState.CONFIRMED: ("finding_confirmed",),
            ChangeSetState.IMPLEMENTING: ("plan_approved",),
            ChangeSetState.CI_VALIDATED: ("ci_passed", "independent_review_passed"),
            ChangeSetState.CANDIDATE_BUILT: ("candidate_digest_present",),
            ChangeSetState.PREVIEW_VERIFIED: ("preview_verified",),
            ChangeSetState.SOURCE_MERGED: ("source_merged", "independent_review_passed"),
            ChangeSetState.FINAL_ARTIFACT_BUILT: ("candidate_digest_present",),
            ChangeSetState.AWAITING_STAGING_APPROVAL: ("deployment_pr_protected",),
            ChangeSetState.STAGING_APPROVED: ("approval_current",),
            ChangeSetState.RUNTIME_VERIFIED: ("runtime_verified",),
            ChangeSetState.ISSUE_CLOSED: ("issue_closed", "runtime_verified"),
            ChangeSetState.ROLLED_BACK: ("recovery_evidence_present",),
        }.get(target, ())
        missing = [name for name in required if not getattr(evidence, name)]
        if change_set.risk_class is RiskClass.R3 and target in {
            ChangeSetState.CI_VALIDATED,
            ChangeSetState.SOURCE_MERGED,
            ChangeSetState.STAGING_APPROVED,
        }:
            if not evidence.second_provider_review_passed:
                missing.append("second_provider_review_passed")
        if missing:
            raise ValueError(f"transition evidence missing: {sorted(set(missing))}")
