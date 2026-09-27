from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.dashboard.status_mapping import (
    NORMALIZED_STATUSES,
    map_status,
    normalize_status,
)
from engineering_control.db.base import Base
from engineering_control.db.models import (
    BacklogIssueRecord,
    Campaign,
    CoverageCell,
    Enhancement,
    Finding,
    GitHubBacklogCampaign,
    Run,
    SolutionLoop,
)
from engineering_control.engineering.models import EnhancementStatus
from engineering_control.models.enums import (
    ChangeSetState,
    CoverageStatus,
    FindingDisposition,
    RunStatus,
)


def _repository(tmp_path: Path) -> DashboardRepository:
    repository = DashboardRepository(f"sqlite+pysqlite:///{tmp_path / 'repository.db'}")
    Base.metadata.create_all(repository.engine)
    return repository


def _backlog_campaign(project: str, workflow: str) -> GitHubBacklogCampaign:
    now = datetime.now(UTC)
    return GitHubBacklogCampaign(
        project=project,
        source_repository=f"gntik-ai/{project}",
        deployment_repository=f"gntik-ai/{project}-deploy",
        imported_at=now,
        issue_query="is:issue is:open",
        import_filter="all",
        source_default_branch="main",
        deployment_default_branch="main",
        source_head_at_import="a" * 40,
        deployment_head_at_import="b" * 40,
        issue_count=2,
        eligible_count=2,
        deferred_count=0,
        duplicate_count=0,
        stale_count=0,
        already_fixed_count=0,
        existing_pr_count=0,
        blocked_count=0,
        dependency_graph_hash="c" * 64,
        plan_hash="d" * 64,
        approval_binding=None,
        approval_json={},
        maximum_risk=None,
        maximum_items=2,
        maximum_concurrency=1,
        delivery_mode=None,
        include_enhancements=False,
        status="QUEUED",
        workflow_id=workflow,
    )


def _issue(campaign_id: object, number: int, title: str, status: str) -> BacklogIssueRecord:
    now = datetime.now(UTC)
    return BacklogIssueRecord(
        backlog_campaign_id=campaign_id,
        repository="gntik-ai/falcone",
        issue_number=number,
        issue_url_reference=f"https://github.com/gntik-ai/falcone/issues/{number}",
        title=title,
        state="OPEN",
        labels_json=[],
        milestone=None,
        issue_type="PRODUCT_BUG",
        openspec_kind=None,
        severity="medium",
        calculated_risk="R1",
        component="api",
        source_or_deployment_owner="source",
        documentation_levels_json=[],
        dependencies_json=[],
        blocking_issues_json=[],
        issues_blocked_json=[],
        linked_prs_json=[],
        linked_branches_json=[],
        linked_commits_json=[],
        duplicate_candidates_json=[],
        current_source_reproduction="NOT_RUN",
        current_runtime_reproduction="NOT_RUN",
        current_runtime_required=False,
        reproduction_mode="SOURCE_ONLY",
        candidate_status=status,
        eligibility_status="ELIGIBLE",
        eligibility_reason="Bearer abcdefghijklmnopqrstuvwxyz should be hidden",
        planned_wave="W1",
        planned_order=number,
        finding_id=None,
        change_set_id=None,
        delivery_status=status,
        final_closure_status="OPEN",
        documentation_impact="DOCUMENTATION_NOT_REQUIRED_WITH_EVIDENCE",
        body_sha256="e" * 64,
        github_updated_at=now,
        github_snapshot_json={},
    )


def test_all_normalized_statuses_are_reachable() -> None:
    raw = {
        "backlog": "BACKLOG",
        "eligible": "ELIGIBLE",
        "queued": "QUEUED",
        "preparing": "PREPARING",
        "implementing": "IMPLEMENTING",
        "reviewing": "REVIEWING",
        "reworking": "REWORKING",
        "testing": "TESTING",
        "awaiting_ci": "AWAITING_CI",
        "awaiting_approval": "AWAITING_APPROVAL",
        "building": "BUILDING",
        "deploying": "DEPLOYING",
        "verifying": "VERIFYING",
        "blocked": "BLOCKED",
        "paused": "PAUSED",
        "completed": "COMPLETED",
        "failed": "FAILED",
        "cancelled": "CANCELLED",
        "skipped": "SKIPPED",
    }
    assert tuple(raw) == NORMALIZED_STATUSES
    assert {normalize_status(value) for value in raw.values()} == set(NORMALIZED_STATUSES)
    assert map_status("UNKNOWN_STATE").normalized == "blocked"
    assert map_status("UNKNOWN_STATE").blocker_code == "UNKNOWN_STATUS_MAPPING"
    assert normalize_status("PAUSED_AFTER_CURRENT") == "paused"
    assert normalize_status("STOPPED_AFTER_CURRENT") == "cancelled"


def test_every_persisted_status_enum_has_an_explicit_non_unknown_mapping() -> None:
    values = [
        *((value.value, "run") for value in RunStatus),
        *((value.value, "coverage_cell") for value in CoverageStatus),
        *((value.value, "change_set") for value in ChangeSetState),
        *((value.value, "finding") for value in FindingDisposition),
        *((value.value, "enhancement") for value in EnhancementStatus),
        ("VERIFIED", "verification_result"),
        ("PASSED", "coverage_cell"),
        ("EXCLUDED", "coverage_cell"),
    ]
    for raw, entity_type in values:
        assert map_status(raw, entity_type=entity_type).blocker_code != "UNKNOWN_STATUS_MAPPING", (
            raw
        )


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("prepare worktree", "preparing"),
        ("maker", "implementing"),
        ("checker review", "reviewing"),
        ("correction", "reworking"),
        ("test source", "testing"),
        ("build artifact", "building"),
        ("deployment", "deploying"),
        ("runtime verify", "verifying"),
    ],
)
def test_ambiguous_running_state_uses_phase(phase: str, expected: str) -> None:
    assert normalize_status("RUNNING", phase=phase) == expected


@pytest.mark.asyncio
async def test_read_only_finding_and_enhancement_views_are_bounded_and_redacted(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    with repository.sessions.begin() as session:
        campaign = Campaign(
            project="falcone",
            name="safe-list",
            status="COMPLETED",
            required_cells=1,
            metadata_json={},
            source_sha=None,
            deployment_sha=None,
        )
        session.add(campaign)
        session.flush()
        cell = CoverageCell(
            campaign_id=campaign.id,
            repository_surface="source",
            domain="api",
            persona="operator",
            flow="read",
            lens="security",
            environment="staging",
            status="CONFIRMED_FINDING",
            blocker=None,
            exclusion_approval_id=None,
            evidence_json=[],
            result_json={},
        )
        session.add(cell)
        session.flush()
        session.add_all(
            [
                Finding(
                    campaign_id=campaign.id,
                    coverage_cell_id=cell.id,
                    fingerprint="finding-list-" + "f" * 52,
                    project="falcone",
                    domain="api",
                    persona="operator",
                    severity="medium",
                    confidence=0.9,
                    risk_class="R1",
                    disposition="CONFIRMED",
                    expected_behavior="expected",
                    actual_behavior="actual",
                    evidence_json=[],
                    minimal_reproduction=[],
                    affected_source_sha="a" * 40,
                    affected_deployment_sha=None,
                    affected_image_digest=None,
                    affected_component="gateway",
                    impact_json={},
                    recommended_action="remove sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
                ),
                Enhancement(
                    enhancement_key="enh-safe-list",
                    project="falcone",
                    campaign_id=str(campaign.id),
                    persona="operator",
                    affected_repository="gntik-ai/falcone",
                    risk_class="R1",
                    status="CANDIDATE_ENHANCEMENT",
                    fingerprint="enhancement-list-" + "e" * 48,
                    approval_binding_sha256=None,
                    payload_json={"summary": "rotate sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"},
                ),
            ]
        )

    findings = await repository.list_findings_view(project="falcone", limit=1)
    enhancements = await repository.list_enhancements_view(project="falcone", limit=1)

    assert findings["items"][0]["recommended_action"] == "remove [REDACTED]"
    assert enhancements["items"][0]["summary"] == "rotate [REDACTED]"
    assert "payload_json" not in enhancements["items"][0]
    assert "evidence_json" not in findings["items"][0]


@pytest.mark.asyncio
async def test_backlog_projects_links_filters_search_pagination_and_ordering(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    now = datetime.now(UTC)
    with repository.sessions.begin() as session:
        falcone = _backlog_campaign("falcone", "falcone-import")
        musematic = _backlog_campaign("musematic", "musematic-import")
        session.add_all([falcone, musematic])
        session.flush()
        session.add_all(
            [
                _issue(falcone.id, 10, "Completed API repair", "COMPLETED"),
                _issue(falcone.id, 11, "Queued API repair", "QUEUED"),
                _issue(musematic.id, 12, "Musematic API repair", "BLOCKED"),
                SolutionLoop(
                    workflow_id="llmwiki-loop",
                    project="llmwiki",
                    campaign_id="llmwiki-campaign",
                    execution_mode="plan-only",
                    maximum_risk="R1",
                    maximum_items=1,
                    status="RUNNING",
                    state_json={
                        "issue_number": 3,
                        "issue_title": "Catalog issue",
                        "issue_url": "https://github.com/gntik-ai/llmwiki/issues/3",
                        "phase": "TESTING",
                    },
                    created_at=now - timedelta(minutes=2),
                    updated_at=now,
                ),
            ]
        )
    all_items = await repository.list_backlog(
        project=None, status=None, search=None, page=1, page_size=10
    )
    assert {item["project"] for item in all_items["items"]} == {
        "falcone",
        "musematic",
        "llmwiki",
    }
    assert all_items["items"][0]["normalized_status"] != "completed"
    blocked = next(item for item in all_items["items"] if item["normalized_status"] == "blocked")
    assert blocked["blocker"] == "Bearer [REDACTED] should be hidden"
    falcone_items = await repository.list_backlog(
        project="falcone", status="queued", search="queued api", page=1, page_size=10
    )
    assert falcone_items["total"] == 1
    item = falcone_items["items"][0]
    assert item["issue_url"] == "https://github.com/gntik-ai/falcone/issues/11"
    assert item["queued_at"].endswith("Z")
    assert item["workflow_id"] == "falcone-import"
    paged = await repository.list_backlog(
        project="falcone", status=None, search=None, page=2, page_size=1
    )
    assert paged["page"] == 2
    assert paged["pages"] == 2


@pytest.mark.asyncio
async def test_backlog_projects_only_latest_campaign_row_for_each_issue(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    now = datetime.now(UTC)
    with repository.sessions.begin() as session:
        older = _backlog_campaign("falcone", "falcone-import-older")
        older.imported_at = now - timedelta(days=1)
        newer = _backlog_campaign("falcone", "falcone-import-newer")
        newer.imported_at = now
        other_project = _backlog_campaign("musematic", "musematic-import-same-reference")
        other_project.imported_at = now + timedelta(hours=1)
        session.add_all([older, newer, other_project])
        session.flush()
        older_issue = _issue(older.id, 11, "Older queued API repair", "QUEUED")
        older_issue.updated_at = now + timedelta(minutes=1)
        current_issue = _issue(newer.id, 11, "Current blocked API repair", "BLOCKED")
        current_issue.updated_at = now
        tied_issue = _issue(newer.id, 12, "Current tied API repair", "BLOCKED")
        tied_issue.updated_at = now
        other_project_issue = _issue(
            other_project.id, 11, "Musematic retained same reference", "BLOCKED"
        )
        other_project_issue.updated_at = now + timedelta(hours=1)
        session.add_all(
            [
                older_issue,
                current_issue,
                tied_issue,
                other_project_issue,
            ]
        )
        session.flush()
        newer_id = newer.id
        expected_tied_order = [
            issue.issue_number
            for issue in sorted((current_issue, tied_issue), key=lambda item: item.id, reverse=True)
        ]

    projected = await repository.list_backlog(
        project="falcone", status=None, search=None, page=1, page_size=10
    )
    old_search = await repository.list_backlog(
        project="falcone", status=None, search="older", page=1, page_size=10
    )
    stale_status = await repository.list_backlog(
        project="falcone", status="queued", search=None, page=1, page_size=10
    )
    current_status = await repository.list_backlog(
        project="falcone", status="blocked", search=None, page=1, page_size=10
    )
    all_projects = await repository.list_backlog(
        project=None, status=None, search=None, page=1, page_size=10
    )
    second_page = await repository.list_backlog(
        project="falcone", status=None, search=None, page=2, page_size=1
    )

    assert projected["total"] == 2
    assert [item["issue_number"] for item in projected["items"]] == expected_tied_order
    selected = next(item for item in projected["items"] if item["issue_number"] == 11)
    assert selected["title"] == "Current blocked API repair"
    assert selected["backlog_campaign_id"] == str(newer_id)
    assert old_search["total"] == 0
    assert stale_status["total"] == 0
    assert current_status["total"] == 2
    assert all_projects["total"] == 3
    assert {item["project"] for item in all_projects["items"] if item["issue_number"] == 11} == {
        "falcone",
        "musematic",
    }
    assert second_page["total"] == 2
    assert second_page["pages"] == 2
    assert [item["issue_number"] for item in second_page["items"]] == [expected_tied_order[1]]
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(BacklogIssueRecord)) == 4


@pytest.mark.asyncio
async def test_audit_totals_progress_zero_cells_dimensions_findings_and_order(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    now = datetime.now(UTC)
    with repository.sessions.begin() as session:
        active = Campaign(
            project="falcone",
            name="security",
            status="RUNNING",
            required_cells=4,
            metadata_json={"preset": "security", "environment": "staging"},
            source_sha=None,
            deployment_sha=None,
        )
        empty = Campaign(
            project="musematic",
            name="empty",
            status="COMPLETED",
            required_cells=0,
            metadata_json={},
            source_sha=None,
            deployment_sha=None,
            created_at=now - timedelta(days=1),
            updated_at=now - timedelta(days=1),
        )
        session.add_all([active, empty])
        session.flush()
        statuses = ["NO_FINDING", "BLOCKED", "FAILED", "IN_PROGRESS"]
        cells = []
        for index, status in enumerate(statuses):
            cells.append(
                CoverageCell(
                    campaign_id=active.id,
                    repository_surface="source",
                    domain=f"domain-{index}",
                    persona="operator",
                    flow="repository-static",
                    lens="security",
                    environment="staging",
                    status=status,
                    blocker="blocked safely" if status == "BLOCKED" else None,
                    exclusion_approval_id=None,
                    evidence_json=[],
                    result_json={},
                )
            )
        session.add_all(cells)
        session.flush()
        session.add_all(
            [
                Run(
                    campaign_id=active.id,
                    change_set_id=None,
                    workflow_id="audit-active",
                    run_type="audit-campaign",
                    status="RUNNING",
                    project="falcone",
                    worker="scan",
                    started_at=now,
                    finished_at=None,
                    error_code=None,
                    summary=None,
                ),
                Finding(
                    campaign_id=active.id,
                    coverage_cell_id=cells[0].id,
                    fingerprint="f" * 64,
                    project="falcone",
                    domain="domain-0",
                    persona="operator",
                    severity="medium",
                    confidence=0.9,
                    risk_class="R1",
                    disposition="CONFIRMED",
                    expected_behavior="expected",
                    actual_behavior="actual",
                    evidence_json=[],
                    minimal_reproduction=["step"],
                    affected_source_sha="a" * 40,
                    affected_deployment_sha=None,
                    affected_image_digest=None,
                    affected_component="api",
                    impact_json={},
                    recommended_action="repair",
                ),
            ]
        )
    audits = await repository.list_audits(project=None, status=None)
    assert audits["items"][0]["campaign_id"] == str(active.id)
    item = audits["items"][0]
    assert item["total_cells"] == 4
    assert item["passed_cells"] == 1
    assert item["blocked_cells"] == 1
    assert item["failed_cells"] == 1
    assert item["running_cells"] == 1
    assert item["completion_percentage"] == 75.0
    assert item["repository_surface"] == "source"
    assert item["confirmed_findings"] == 1
    assert item["blocker"] == "blocked safely"
    empty_item = next(value for value in audits["items"] if value["campaign_id"] == str(empty.id))
    assert empty_item["completion_percentage"] == 0.0
