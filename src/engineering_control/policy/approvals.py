from __future__ import annotations

import hashlib
import json
from typing import Any

from engineering_control.models.core import ApprovalBinding, ChangeSet
from engineering_control.models.enums import RiskClass


class ApprovalService:
    @staticmethod
    def artifact_payload(
        change_set: ChangeSet,
        *,
        target_context: str,
        target_namespace: str,
        migration: bool,
        backup_requirement: str,
        rollback_plan_sha256: str,
        ci_result_sha256: str,
        independent_review_sha256: str,
        release_preflight_sha256: str,
    ) -> dict[str, Any]:
        if not change_set.source_head_sha or not change_set.deployment_head_sha:
            raise ValueError("source and deployment heads are required before approval")
        return {
            "change_set_id": str(change_set.change_set_id),
            "project": change_set.project.value,
            "risk_class": change_set.risk_class.value,
            "source_pr": str(change_set.source_pr) if change_set.source_pr else None,
            "source_sha": change_set.source_head_sha,
            "deployment_pr": str(change_set.deployment_pr) if change_set.deployment_pr else None,
            "deployment_sha": change_set.deployment_head_sha,
            "image_digests": dict(sorted(change_set.image_digests.items())),
            "target_context": target_context,
            "target_namespace": target_namespace,
            "migration": migration,
            "backup_requirement": backup_requirement,
            "rollback_plan_sha256": rollback_plan_sha256,
            "ci_result_sha256": ci_result_sha256,
            "independent_review_sha256": independent_review_sha256,
            "release_preflight_sha256": release_preflight_sha256,
        }

    @staticmethod
    def digest(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def typed_confirmation(cls, change_set_id: str, artifact_digest: str) -> str:
        return f"APPROVE R3 {change_set_id} {artifact_digest[:16]}"

    @classmethod
    def bind(
        cls,
        change_set: ChangeSet,
        *,
        approver_subject: str,
        typed_confirmation: str | None = None,
        **artifact_values: Any,
    ) -> ApprovalBinding:
        payload = cls.artifact_payload(change_set, **artifact_values)
        artifact_digest = cls.digest(payload)
        if change_set.risk_class is RiskClass.R3:
            expected = cls.typed_confirmation(str(change_set.change_set_id), artifact_digest)
            if typed_confirmation != expected:
                raise ValueError("R3 typed confirmation does not bind to the immutable artifacts")
        return ApprovalBinding(
            **payload,
            artifact_binding_sha256=artifact_digest,
            approver_subject=approver_subject,
            typed_confirmation=typed_confirmation,
        )

    @classmethod
    def is_current(cls, approval: ApprovalBinding, change_set: ChangeSet) -> bool:
        current = cls.artifact_payload(
            change_set,
            target_context=approval.target_context,
            target_namespace=approval.target_namespace,
            migration=approval.migration,
            backup_requirement=approval.backup_requirement,
            rollback_plan_sha256=approval.rollback_plan_sha256,
            ci_result_sha256=approval.ci_result_sha256,
            independent_review_sha256=approval.independent_review_sha256,
            release_preflight_sha256=approval.release_preflight_sha256,
        )
        return cls.digest(current) == approval.artifact_binding_sha256
