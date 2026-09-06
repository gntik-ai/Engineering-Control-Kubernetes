"""Read-only shared authorization for approved backlog and exact R3 issue execution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.api.schemas import ExistingBacklogLoopStart
from engineering_control.db import models as db
from engineering_control.engineering.backlog import (
    BACKLOG_APPROVAL_PHRASE,
    BACKLOG_PROJECT_PAIRS,
    BacklogApprovalBinding,
    canonical_sha256,
    is_reauthenticated_backlog_operator,
)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _current_backlog_approval(
    campaign: db.GitHubBacklogCampaign | None,
    *,
    delivery_mode: str,
    maximum_risk: str,
    now: datetime,
) -> BacklogApprovalBinding | None:
    if (
        campaign is None
        or campaign.status
        not in {"APPROVED_POLICY_PENDING", "APPROVED_INVENTORY", "APPROVED", "RUNNING"}
        or campaign.delivery_mode != delivery_mode
        or campaign.maximum_risk != maximum_risk
        or not campaign.approval_binding
        or not isinstance(campaign.approval_json, dict)
    ):
        return None
    approval = campaign.approval_json
    raw_binding = approval.get("binding")
    binding_value = raw_binding if isinstance(raw_binding, dict) else approval
    raw_approval = approval.get("approval")
    approval_record = raw_approval if isinstance(raw_approval, dict) else approval
    if (
        approval_record.get("typed_confirmation") != BACKLOG_APPROVAL_PHRASE
        or not isinstance(approval_record.get("approved_by"), str)
        or not approval_record.get("approved_at")
    ):
        return None
    try:
        binding = BacklogApprovalBinding.model_validate(
            {field: binding_value.get(field) for field in BacklogApprovalBinding.model_fields}
        )
    except ValueError:
        return None
    if (
        binding.sha256() != campaign.approval_binding
        or binding.backlog_campaign_id != str(campaign.id)
        or binding.project != campaign.project
        or binding.source_head != campaign.source_head_at_import
        or binding.deployment_head != campaign.deployment_head_at_import
        or binding.dependency_plan_hash != campaign.plan_hash
        or binding.delivery_mode != delivery_mode
        or binding.maximum_risk != maximum_risk
        or _as_utc(binding.expires_at) <= now
    ):
        return None
    return binding


def _global_campaign_authorizes(
    session: Any,
    campaign: db.Campaign | None,
    backlog_campaign_id: UUID,
    maximum_risk: str,
    delivery_mode: str,
    target_repository: str,
    target_issue_number: int,
    r3_approval_binding: str,
) -> bool:
    if (
        campaign is None
        or campaign.project != "platform"
        or campaign.status
        not in {
            "RUNNING",
            "PAUSED",
        }
    ):
        return False
    metadata = campaign.metadata_json if isinstance(campaign.metadata_json, dict) else {}
    ids = metadata.get("backlog_campaign_ids")
    delivery_modes = metadata.get("delivery_modes")
    approval_bindings = metadata.get("inventory_approval_bindings")
    project = (
        next(
            (str(name) for name, value in ids.items() if str(value) == str(backlog_campaign_id)),
            None,
        )
        if isinstance(ids, dict)
        else None
    )
    backlog = session.get(db.GitHubBacklogCampaign, backlog_campaign_id)
    current_approval = _current_backlog_approval(
        backlog,
        delivery_mode=delivery_mode,
        maximum_risk="R2",
        now=datetime.now(UTC),
    )
    return (
        metadata.get("kind") == "autonomous-global-backlog"
        and metadata.get("environment") == "staging"
        and metadata.get("maximum_risk") == "R2"
        and maximum_risk in {"R2", "R3"}
        and project in {"falcone", "musematic", "llmwiki"}
        and isinstance(delivery_modes, dict)
        and delivery_modes.get(project) == delivery_mode
        and delivery_mode in {"source-pr", "full-delivery"}
        and isinstance(approval_bindings, dict)
        and current_approval is not None
        and approval_bindings.get(project) == current_approval.sha256()
        and metadata.get("production") is False
        and isinstance(ids, dict)
        and str(ids.get(project)) == str(backlog_campaign_id)
        and (
            maximum_risk == "R2"
            or (
                bool(target_repository)
                and target_issue_number > 0
                and bool(r3_approval_binding)
                and session.scalar(
                    select(db.ActionCandidate.id)
                    .where(
                        db.ActionCandidate.action == "r3_issue_approval",
                        db.ActionCandidate.target_id
                        == f"{target_repository}#{target_issue_number}",
                        db.ActionCandidate.binding_hash == r3_approval_binding,
                        db.ActionCandidate.used_at.is_not(None),
                        db.ActionCandidate.expires_at > datetime.now(UTC),
                    )
                    .limit(1)
                )
                is not None
            )
        )
    )


def _valid_r3_candidate(
    session: Session,
    issue: db.BacklogIssueRecord,
    binding: str | None = None,
) -> db.ActionCandidate | None:
    query = select(db.ActionCandidate).where(
        db.ActionCandidate.action == "r3_issue_approval",
        db.ActionCandidate.target_id == f"{issue.repository}#{issue.issue_number}",
        db.ActionCandidate.used_at.is_not(None),
        db.ActionCandidate.expires_at > datetime.now(UTC),
    )
    if binding:
        query = query.where(db.ActionCandidate.binding_hash == binding)
    candidates = session.scalars(
        query.order_by(db.ActionCandidate.created_at.desc()).limit(10)
    ).all()
    for candidate in candidates:
        payload = candidate.payload_json
        if not isinstance(payload, dict):
            continue
        try:
            bound_expiry = datetime.fromisoformat(str(payload.get("expires_at")))
        except ValueError:
            continue
        if _as_utc(bound_expiry) != _as_utc(candidate.expires_at):
            continue
        immutable = {
            key: value
            for key, value in payload.items()
            if key not in {"approved_by", "approved_at"}
        }
        campaign = session.get(db.GitHubBacklogCampaign, issue.backlog_campaign_id)
        if (
            campaign is not None
            and issue.calculated_risk == "R3"
            and candidate.project == campaign.project
            and candidate.target_type == "github_issue"
            and candidate.capability_status == "AVAILABLE"
            and not candidate.blockers_json
            and canonical_sha256(immutable) == candidate.binding_hash
            and is_reauthenticated_backlog_operator(str(payload.get("approved_by", "")))
            and bool(payload.get("approved_at"))
            and payload.get("project") == campaign.project
            and payload.get("repository") == issue.repository
            and payload.get("issue_number") == issue.issue_number
            and payload.get("source_repository") == campaign.source_repository
            and payload.get("deployment_repository") == campaign.deployment_repository
            and payload.get("source_head") == campaign.source_head_at_import
            and payload.get("deployment_head") == campaign.deployment_head_at_import
            and payload.get("environment") == "staging"
            and payload.get("source_branch") == campaign.source_default_branch
            and payload.get("deployment_branch") == campaign.deployment_default_branch
            and payload.get("issue_body_sha256") == issue.body_sha256
            and payload.get("issue_updated_at") == issue.github_updated_at.isoformat()
            and payload.get("production") is False
        ):
            return candidate
    return None


def require_alternative_issue_authority(
    session: Session, request: ExistingBacklogLoopStart
) -> None:
    """A marker is a lookup key, never a substitute for an actual current approval."""
    request = ExistingBacklogLoopStart.model_validate(request.model_dump(mode="json"))
    if (
        request.issue_operation_id is not None
        or request.issue_approval_binding is not None
        or request.target_repository is None
        or request.target_issue_number is None
        or (request.global_campaign_id is None and request.r3_approval_binding is None)
    ):
        raise ValueError("direct issue execution has no complete alternative authority")
    backlog = session.get(db.GitHubBacklogCampaign, request.backlog_campaign_id)
    issue = session.scalar(
        select(db.BacklogIssueRecord).where(
            db.BacklogIssueRecord.backlog_campaign_id == request.backlog_campaign_id,
            db.BacklogIssueRecord.repository == request.target_repository,
            db.BacklogIssueRecord.issue_number == request.target_issue_number,
        )
    )
    if (
        backlog is None
        or backlog.project != request.project
        or request.target_repository not in BACKLOG_PROJECT_PAIRS[request.project]
        or issue is None
        or issue.state != "OPEN"
    ):
        raise ValueError("alternative issue authority target is unavailable or changed")
    if request.global_campaign_id is not None:
        global_campaign = session.get(db.Campaign, request.global_campaign_id)
        if not _global_campaign_authorizes(
            session,
            global_campaign,
            request.backlog_campaign_id,
            request.maximum_risk.value,
            request.delivery_mode,
            request.target_repository,
            request.target_issue_number,
            request.r3_approval_binding or "",
        ):
            raise ValueError("global campaign approval is unavailable or changed")
        current = (
            global_campaign.metadata_json.get("current_issue")
            if global_campaign is not None
            else None
        )
        if not isinstance(current, dict) or (
            current.get("repository") != request.target_repository
            or current.get("issue_number") != request.target_issue_number
            or str(current.get("backlog_campaign_id")) != str(request.backlog_campaign_id)
        ):
            raise ValueError("issue is not the global parent's current selected owner")
    else:
        approval = _current_backlog_approval(
            backlog,
            delivery_mode=request.delivery_mode,
            maximum_risk=request.maximum_risk.value,
            now=datetime.now(UTC),
        )
        if approval is None or backlog.status == "APPROVED_INVENTORY":
            raise ValueError("standalone R3 issue requires its current campaign approval")
    if request.maximum_risk.value == "R3" or request.r3_approval_binding is not None:
        if (
            request.maximum_risk.value != "R3"
            or issue.calculated_risk != "R3"
            or request.r3_approval_binding is None
            or _valid_r3_candidate(session, issue, request.r3_approval_binding) is None
        ):
            raise ValueError("exact current R3 issue approval is required")
    elif issue.calculated_risk not in {"R0", "R1", "R2"}:
        raise ValueError("issue risk exceeds global campaign approval")
