from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import (
    ActionCandidate,
    BacklogIssueRecord,
    DashboardEvent,
    GitHubBacklogCampaign,
)
from engineering_control.engineering.backlog import (
    BACKLOG_APPROVAL_PHRASE,
    BacklogApprovalBinding,
    approval_binding,
)
from engineering_control.operations.repository import (
    OperationsRepository,
    _global_inventory_approval_current,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
GLOBAL_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
FALCONE_ID = UUID("11111111-1111-4111-8111-111111111111")
MUSEMATIC_ID = UUID("22222222-2222-4222-8222-222222222222")
LLMWIKI_ID = UUID("33333333-3333-4333-8333-333333333333")


def _campaign(campaign_id: UUID, project: str, *, status: str) -> GitHubBacklogCampaign:
    repositories = {
        "falcone": ("gntik-ai/falcone", "gntik-ai/falcone-charts", "main"),
        "musematic": ("gntik-ai/musematic", "gntik-ai/musematic-deploy", "main"),
        "llmwiki": ("gntik-ai/llmwiki", "gntik-ai/llmwiki", "master"),
    }
    source, deployment, branch = repositories[project]
    source_head = {"falcone": "a", "musematic": "b", "llmwiki": "c"}[project] * 40
    deployment_head = {"falcone": "d", "musematic": "e", "llmwiki": "f"}[project] * 40
    plan_hash = {"falcone": "1", "musematic": "2", "llmwiki": "3"}[project] * 64
    delivery_mode = "source-pr" if project == "llmwiki" else "full-delivery"
    binding = (
        approval_binding(
            campaign_id=str(campaign_id),
            project=project,  # type: ignore[arg-type]
            import_filter="all",
            source_head=source_head,
            deployment_head=deployment_head,
            dependency_plan_hash=plan_hash,
            maximum_risk="R2",
            maximum_items=50,
            delivery_mode=delivery_mode,  # type: ignore[arg-type]
            include_enhancements=False,
            now=datetime.now(UTC),
        )
        if status == "APPROVED"
        else None
    )
    effective_status = (
        "APPROVED_INVENTORY" if status == "APPROVED" and project != "falcone" else status
    )
    return GitHubBacklogCampaign(
        id=campaign_id,
        project=project,
        source_repository=source,
        deployment_repository=deployment,
        imported_at=NOW,
        issue_query="is:issue is:open",
        import_filter="all",
        source_default_branch=branch,
        deployment_default_branch=branch,
        source_head_at_import=source_head,
        deployment_head_at_import=deployment_head,
        issue_count=3,
        eligible_count=3,
        deferred_count=0,
        duplicate_count=0,
        stale_count=0,
        already_fixed_count=0,
        existing_pr_count=0,
        blocked_count=0,
        dependency_graph_hash=(project[0] * 64),
        plan_hash=plan_hash,
        approval_binding=binding.sha256() if binding is not None else None,
        approval_json=(
            {
                **binding.model_dump(mode="json"),
                "policy_role": "owner" if project == "falcone" else "inventory",
                "approved_by": "telegram:42",
                "approved_at": datetime.now(UTC).isoformat(),
                "typed_confirmation": BACKLOG_APPROVAL_PHRASE,
            }
            if binding is not None
            else {}
        ),
        maximum_risk="R2" if status == "APPROVED" else None,
        maximum_items=50,
        maximum_concurrency=1,
        delivery_mode=delivery_mode if status == "APPROVED" else None,
        include_enhancements=False,
        status=effective_status,
        workflow_id=f"backlog-import-{project}",
    )


def _issue(
    campaign_id: UUID,
    repository: str,
    number: int,
    *,
    risk: str,
    severity: str,
    order: int,
) -> BacklogIssueRecord:
    return BacklogIssueRecord(
        backlog_campaign_id=campaign_id,
        repository=repository,
        issue_number=number,
        issue_url_reference=f"https://github.com/{repository}/issues/{number}",
        title=f"Bounded issue {number}",
        state="OPEN",
        labels_json=["bug"],
        milestone=None,
        issue_type="PRODUCT_BUG",
        openspec_kind=None,
        severity=severity,
        calculated_risk=risk,
        component="identity" if number == 1119 else "runtime",
        source_or_deployment_owner="source",
        documentation_levels_json=[],
        dependencies_json=[],
        blocking_issues_json=[],
        issues_blocked_json=[],
        linked_prs_json=[],
        linked_branches_json=[],
        linked_commits_json=[],
        duplicate_candidates_json=[],
        current_source_reproduction="REPRODUCED_CURRENT_SOURCE",
        current_runtime_reproduction="REPRODUCED_CURRENT_RUNTIME",
        current_runtime_required=True,
        reproduction_mode="RUNTIME_REQUIRED",
        candidate_status="READY",
        eligibility_status="READY",
        eligibility_reason="CURRENT_REVALIDATION_PASS",
        planned_wave="W0",
        planned_order=order,
        finding_id=None,
        change_set_id=None,
        delivery_status="NOT_STARTED",
        final_closure_status="OPEN",
        documentation_impact="DOCUMENTATION_REQUIRED",
        body_sha256=(str(number)[0] * 64),
        github_updated_at=NOW,
        github_snapshot_json={},
    )


def test_nested_inventory_approval_is_current_without_owning_shared_policy() -> None:
    campaign = _campaign(FALCONE_ID, "falcone", status="APPROVED")
    flat = dict(campaign.approval_json)
    campaign.approval_json = {
        "binding": {key: flat[key] for key in BacklogApprovalBinding.model_fields},
        "policy_role": "inventory",
        "approval": {
            "approved_by": flat["approved_by"],
            "approved_at": flat["approved_at"],
            "typed_confirmation": flat["typed_confirmation"],
        },
    }
    campaign.status = "APPROVED_INVENTORY"

    assert _global_inventory_approval_current(
        campaign,
        project="falcone",
        now=datetime.now(UTC),
    )
    drifted = dict(campaign.approval_json)
    drifted_binding = dict(drifted["binding"])
    drifted_binding["source_head"] = "f" * 40
    drifted["binding"] = drifted_binding
    campaign.approval_json = drifted
    assert not _global_inventory_approval_current(
        campaign,
        project="falcone",
        now=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_global_campaign_is_sequential_retries_blocked_and_adopts_exact_r3(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'global.db'}"
    operations = OperationsRepository(database_url)
    Base.metadata.create_all(operations.engine)
    with operations.sessions.begin() as session:
        session.add_all(
            [
                _campaign(FALCONE_ID, "falcone", status="APPROVED"),
                _campaign(MUSEMATIC_ID, "musematic", status="APPROVED"),
                _campaign(LLMWIKI_ID, "llmwiki", status="APPROVED"),
                _issue(
                    MUSEMATIC_ID,
                    "gntik-ai/musematic",
                    1119,
                    risk="R3",
                    severity="critical",
                    order=1,
                ),
                _issue(
                    FALCONE_ID,
                    "gntik-ai/falcone",
                    953,
                    risk="R2",
                    severity="high",
                    order=2,
                ),
                _issue(
                    FALCONE_ID,
                    "gntik-ai/falcone",
                    980,
                    risk="R2",
                    severity="medium",
                    order=3,
                ),
            ]
        )

    with pytest.raises(ValueError, match="has not been created"):
        await operations.r3_issue_candidate_status("musematic", "source", 1119)
    candidate = await operations.r3_issue_candidate("musematic", "source", 1119)
    current_candidate = await operations.r3_issue_candidate_status("musematic", "source", 1119)
    assert current_candidate["candidate_id"] == candidate["candidate_id"]
    same_candidate = await operations.r3_issue_candidate("musematic", "source", 1119)
    assert same_candidate["candidate_id"] == candidate["candidate_id"]
    assert same_candidate["binding"] == candidate["binding"]

    with operations.sessions.begin() as session:
        issue = session.scalar(
            select(BacklogIssueRecord).where(BacklogIssueRecord.issue_number == 1119)
        )
        assert issue is not None
        issue.body_sha256 = "f" * 64
    with pytest.raises(ValueError, match="has not been created"):
        await operations.r3_issue_candidate_status("musematic", "source", 1119)
    replaced = await operations.r3_issue_candidate("musematic", "source", 1119)
    assert replaced["binding"] != candidate["binding"]

    state_root = tmp_path / "state"
    (state_root / "github-policy-requests").mkdir(parents=True, mode=0o700)
    handler = EngineeringActivityHandler(Settings(database_url=database_url, state_root=state_root))
    registration = {
        "campaign_id": str(GLOBAL_ID),
        "backlog_campaign_ids": {
            "falcone": str(FALCONE_ID),
            "musematic": str(MUSEMATIC_ID),
            "llmwiki": str(LLMWIKI_ID),
        },
        "policy_campaign_id": str(FALCONE_ID),
        "environment": "staging",
        "maximum_risk": "R2",
        "global_active_issues": 1,
        "global_deployments": 1,
        "implementation_correction_limit": 5,
        "deployment_correction_limit": 3,
        "production": False,
        "orchestration": {
            "workflow_id": "autonomous-backlog-staging",
            "workflow_run_id": "temporal-global-run",
        },
    }
    with operations.sessions.begin() as session:
        conflicting = session.get(GitHubBacklogCampaign, MUSEMATIC_ID)
        assert conflicting is not None
        approval = dict(conflicting.approval_json)
        approval["policy_role"] = "owner"
        conflicting.approval_json = approval
    with pytest.raises(ValueError, match="one active exact GitHub write policy"):
        handler._register_global_backlog(registration)
    with operations.sessions.begin() as session:
        inventory = session.get(GitHubBacklogCampaign, MUSEMATIC_ID)
        assert inventory is not None
        approval = dict(inventory.approval_json)
        approval["policy_role"] = "inventory"
        inventory.approval_json = approval
    context = handler._register_global_backlog(registration)

    preserved = handler._record_backlog_loop_state(
        {
            "backlog_campaign_id": str(FALCONE_ID),
            "global_campaign_id": str(GLOBAL_ID),
            "loop_status": "PAUSED",
            "blocker": "SYNTHETIC_CHILD_BLOCKER",
        }
    )
    assert preserved["status"] == "PAUSED"
    assert preserved["inventory_status_preserved"] == "APPROVED"
    with operations.sessions() as session:
        assert session.get(GitHubBacklogCampaign, FALCONE_ID).status == "APPROVED"
    assert not (state_root / "github-policy-requests" / "activate.json").exists()

    first = handler._select_global_backlog_issue(context)
    assert first["item"]["issue_number"] == 953
    handler._record_global_backlog_result(
        {**context, "item": first["item"], "result": {"status": "BLOCKED"}}
    )
    second = handler._select_global_backlog_issue(context)
    assert second["item"]["issue_number"] == 980
    handler._record_global_backlog_result(
        {**context, "item": second["item"], "result": {"status": "COMPLETE"}}
    )
    waiting = handler._select_global_backlog_issue(context)
    assert waiting["status"] == "WAITING"
    assert waiting["item"] is None
    assert 30 <= waiting["retry_after_seconds"] <= 900

    approved = await operations.approve_r3_issue(
        "musematic",
        "source",
        1119,
        replaced["binding"],
        replaced["confirmation_phrase"],
        "telegram:42",
    )
    assert approved["status"] == "R3_APPROVED"
    assert approved["approved_by"] == "telegram:42"
    with operations.sessions() as session:
        approved_candidate = session.get(ActionCandidate, UUID(replaced["candidate_id"]))
        assert approved_candidate is not None
        assert approved_candidate.payload_json["approved_by"] == "telegram:42"
        assert approved_candidate.payload_json["approved_at"]
        approval_event = session.scalar(
            select(DashboardEvent).where(DashboardEvent.event_type == "r3_issue_approved")
        )
        assert approval_event is not None
        assert approval_event.metadata_json["approved_by"] == "telegram:42"
    third = handler._select_global_backlog_issue(context)
    assert third["item"]["issue_number"] == 1119
    assert third["item"]["execution_maximum_risk"] == "R3"
    assert third["item"]["r3_approval_binding"] == replaced["binding"]

    authorized = handler._load_backlog_execution(
        {
            **context,
            **third["item"],
            "project": "musematic",
            "backlog_campaign_id": str(MUSEMATIC_ID),
            "maximum_risk": "R3",
            "delivery_mode": "full-delivery",
            "maximum_items": 1,
            "target_repository": "gntik-ai/musematic",
            "target_issue_number": 1119,
            "global_campaign_id": str(GLOBAL_ID),
            "r3_approval_binding": replaced["binding"],
        }
    )
    assert authorized["status"] == "SELECTED"
    assert authorized["items"][0]["issue_number"] == 1119

    terminal = handler._record_global_backlog_result(
        {
            **context,
            "item": third["item"],
            "result": {
                "status": "BLOCKED",
                "blocker": "FINAL_MERGED_REVISION_DELIVERY_SERVICE_REQUIRED",
            },
        }
    )["last_result"]
    assert terminal["retryable"] is False
    assert terminal["retry_after"] is None

    exhausted = None
    for _attempt in range(4):
        exhausted = handler._record_global_backlog_result(
            {
                **context,
                "item": first["item"],
                "result": {"status": "BLOCKED", "blocker": "TRANSIENT_CI_FAILURE"},
            }
        )["last_result"]
    assert exhausted is not None
    assert exhausted["attempt"] == 5
    assert exhausted["retryable"] is False
    assert exhausted["retry_exhausted"] is True
    assert handler._select_global_backlog_issue(context)["status"] == "EMPTY"

    finalized = handler._finalize_global_backlog({**context, "terminal_status": "STOPPED"})
    assert finalized["status"] == "STOPPED"
    deactivation = (state_root / "github-policy-requests" / "activate.json").read_text()
    assert '"action":"disable-existing-backlog"' in deactivation
    assert '"safe_repositories":[]' in deactivation


def test_global_campaign_requires_every_current_inventory_approval(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'approval.db'}"
    operations = OperationsRepository(database_url)
    Base.metadata.create_all(operations.engine)
    with operations.sessions.begin() as session:
        session.add_all(
            [
                _campaign(FALCONE_ID, "falcone", status="PLANNED"),
                _campaign(MUSEMATIC_ID, "musematic", status="APPROVED"),
                _campaign(LLMWIKI_ID, "llmwiki", status="APPROVED"),
            ]
        )
    handler = EngineeringActivityHandler(Settings(database_url=database_url))
    with pytest.raises(ValueError, match="current immutable approval for every inventory"):
        handler._register_global_backlog(
            {
                "campaign_id": str(GLOBAL_ID),
                "backlog_campaign_ids": {
                    "falcone": str(FALCONE_ID),
                    "musematic": str(MUSEMATIC_ID),
                    "llmwiki": str(LLMWIKI_ID),
                },
                "policy_campaign_id": str(FALCONE_ID),
                "orchestration": {"workflow_id": "approval-denied"},
            }
        )


def test_llmwiki_full_delivery_approval_fails_closed_until_inputs_exist() -> None:
    with pytest.raises(ValueError, match="LLMWIKI_FULL_DELIVERY_INPUTS_UNRESOLVED"):
        approval_binding(
            campaign_id=str(LLMWIKI_ID),
            project="llmwiki",
            import_filter="all",
            source_head="c" * 40,
            deployment_head="f" * 40,
            dependency_plan_hash="3" * 64,
            maximum_risk="R2",
            maximum_items=50,
            delivery_mode="full-delivery",
            include_enhancements=False,
        )
