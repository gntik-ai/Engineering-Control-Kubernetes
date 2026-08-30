from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import BacklogIssueRecord, GitHubBacklogCampaign
from engineering_control.operations.repository import OperationsRepository

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
        source_head_at_import=(project[0] * 40),
        deployment_head_at_import=(project[-1] * 40),
        issue_count=3,
        eligible_count=3,
        deferred_count=0,
        duplicate_count=0,
        stale_count=0,
        already_fixed_count=0,
        existing_pr_count=0,
        blocked_count=0,
        dependency_graph_hash=(project[0] * 64),
        plan_hash=(project[-1] * 64),
        approval_binding=None,
        approval_json={},
        maximum_risk="R2" if status == "APPROVED" else None,
        maximum_items=500,
        maximum_concurrency=1,
        delivery_mode="full-delivery" if status == "APPROVED" else None,
        include_enhancements=False,
        status=status,
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
                _campaign(FALCONE_ID, "falcone", status="PLANNED"),
                _campaign(MUSEMATIC_ID, "musematic", status="APPROVED"),
                _campaign(LLMWIKI_ID, "llmwiki", status="PLANNED"),
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

    candidate = await operations.r3_issue_candidate("musematic", "source", 1119)
    same_candidate = await operations.r3_issue_candidate("musematic", "source", 1119)
    assert same_candidate["candidate_id"] == candidate["candidate_id"]
    assert same_candidate["binding"] == candidate["binding"]

    with operations.sessions.begin() as session:
        issue = session.scalar(
            select(BacklogIssueRecord).where(BacklogIssueRecord.issue_number == 1119)
        )
        assert issue is not None
        issue.body_sha256 = "f" * 64
    replaced = await operations.r3_issue_candidate("musematic", "source", 1119)
    assert replaced["binding"] != candidate["binding"]

    handler = EngineeringActivityHandler(Settings(database_url=database_url))
    context = handler._register_global_backlog(
        {
            "campaign_id": str(GLOBAL_ID),
            "backlog_campaign_ids": {
                "falcone": str(FALCONE_ID),
                "musematic": str(MUSEMATIC_ID),
                "llmwiki": str(LLMWIKI_ID),
            },
            "policy_campaign_id": str(MUSEMATIC_ID),
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
    )

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
