from uuid import uuid4

import pytest
from pydantic import ValidationError

from engineering_control.models.core import ChangeSet, CoverageCell
from engineering_control.models.enums import (
    AuditLens,
    ChangeSetState,
    CoverageStatus,
    Project,
    RiskClass,
)
from engineering_control.policy.approvals import ApprovalService
from engineering_control.policy.change_sets import ChangeSetPolicy, TransitionEvidence

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def change_set(risk: RiskClass = RiskClass.R2) -> ChangeSet:
    return ChangeSet(
        project=Project.FALCONE,
        finding_id=uuid4(),
        risk_class=risk,
        source_repository="gntik-ai/falcone",
        source_base_sha=SHA_A,
        source_head_sha=SHA_B,
        source_pr="https://github.com/gntik-ai/falcone/pull/1",
        deployment_repository="gntik-ai/falcone-charts",
        deployment_base_sha=SHA_A,
        deployment_head_sha=SHA_B,
        deployment_pr="https://github.com/gntik-ai/falcone-charts/pull/1",
        image_digests={"api": f"sha256:{DIGEST_A}"},
    )


def approval_values() -> dict[str, object]:
    return {
        "target_context": "staging-observer",
        "target_namespace": "falcone-staging",
        "migration": False,
        "backup_requirement": "not-required",
        "rollback_plan_sha256": DIGEST_A,
        "ci_result_sha256": DIGEST_B,
        "independent_review_sha256": "c" * 64,
    }


def test_terminal_coverage_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="requires evidence"):
        CoverageCell(
            campaign_id=uuid4(),
            project=Project.FALCONE,
            domain="gateway",
            persona="tenant-admin",
            lens=AuditLens.SECURITY,
            environment="snapshot",
            status=CoverageStatus.NO_FINDING,
        )


def test_illegal_changeset_transition_is_rejected() -> None:
    with pytest.raises(ValueError, match="illegal ChangeSet transition"):
        ChangeSetPolicy.transition(
            change_set(), ChangeSetState.STAGING_DEPLOYED, TransitionEvidence()
        )


def test_r3_requires_second_provider_evidence() -> None:
    item = change_set(RiskClass.R3).model_copy(update={"state": ChangeSetState.SOURCE_PR})
    with pytest.raises(ValueError, match="second_provider_review_passed"):
        ChangeSetPolicy.transition(
            item,
            ChangeSetState.CI_VALIDATED,
            TransitionEvidence(ci_passed=True, independent_review_passed=True),
        )


def test_r3_typed_approval_and_invalidation() -> None:
    item = change_set(RiskClass.R3)
    payload = ApprovalService.artifact_payload(item, **approval_values())
    digest = ApprovalService.digest(payload)
    confirmation = ApprovalService.typed_confirmation(str(item.change_set_id), digest)
    approval = ApprovalService.bind(
        item,
        approver_subject="telegram:operator-id",
        typed_confirmation=confirmation,
        **approval_values(),
    )
    assert ApprovalService.is_current(approval, item)
    changed = item.model_copy(update={"source_head_sha": "d" * 40})
    assert not ApprovalService.is_current(approval, changed)


def test_r3_rejects_unbound_typed_confirmation() -> None:
    with pytest.raises(ValueError, match="does not bind"):
        ApprovalService.bind(
            change_set(RiskClass.R3),
            approver_subject="telegram:operator-id",
            typed_confirmation="APPROVE R3 wrong wrong",
            **approval_values(),
        )
