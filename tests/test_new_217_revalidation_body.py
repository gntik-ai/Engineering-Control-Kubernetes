"""NEW-217: a revalidated backlog issue carries its GitHub binding and approved body hash."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import BacklogIssueRecord, Finding, GitHubBacklogCampaign
from engineering_control.operations.issue_amend import AMENDMENT_KEY

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
CAMPAIGN = UUID("21721721-7217-4217-8217-217217217217")
BODY_SHA = "7" * 64


def _handler(tmp_path: Path) -> EngineeringActivityHandler:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'new-217.sqlite'}",
        project_config_root=ROOT / "config/projects",
        persona_config_root=ROOT / "config/personas",
        flow_config_root=ROOT / "config/flows",
        credential_root=credentials,
    )
    handler = EngineeringActivityHandler(settings)
    Base.metadata.create_all(handler.engine)
    with handler.sessions.begin() as session:
        session.add(
            GitHubBacklogCampaign(
                id=CAMPAIGN,
                project="falcone",
                source_repository="gntik-ai/falcone",
                deployment_repository="gntik-ai/falcone-charts",
                imported_at=NOW,
                issue_query="is:issue is:open",
                import_filter="all",
                source_default_branch="main",
                deployment_default_branch="main",
                source_head_at_import="a" * 40,
                deployment_head_at_import="b" * 40,
                issue_count=1,
                eligible_count=1,
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
                maximum_risk="R1",
                maximum_items=1,
                maximum_concurrency=1,
                delivery_mode="full-delivery",
                include_enhancements=False,
                status="RUNNING",
                workflow_id="backlog-loop-falcone-new-217",
            )
        )
        session.add(
            BacklogIssueRecord(
                backlog_campaign_id=CAMPAIGN,
                repository="gntik-ai/falcone",
                issue_number=944,
                issue_url_reference="https://github.com/gntik-ai/falcone/issues/944",
                title="enhancement(add-model-capability-catalog)",
                state="OPEN",
                labels_json=["enhancement"],
                milestone=None,
                issue_type="PRODUCT_BUG",
                openspec_kind=None,
                severity="MEDIUM",
                calculated_risk="R1",
                component="general",
                source_or_deployment_owner="source",
                documentation_levels_json=[],
                dependencies_json=[],
                blocking_issues_json=[],
                issues_blocked_json=[],
                linked_prs_json=[],
                linked_branches_json=[],
                linked_commits_json=[],
                duplicate_candidates_json=[],
                current_source_reproduction="PENDING_CURRENT_STATE_REVALIDATION",
                current_runtime_reproduction="PENDING",
                current_runtime_required=False,
                reproduction_mode="SOURCE_ONLY",
                candidate_status="READY",
                eligibility_status="ELIGIBLE_FOR_REVALIDATION",
                eligibility_reason="FILTER_MATCH_PENDING_REVALIDATION",
                planned_wave="W0",
                planned_order=1,
                finding_id=None,
                change_set_id=None,
                delivery_status="NOT_STARTED",
                final_closure_status="OPEN",
                documentation_impact="DOCUMENTATION_REQUIRED",
                body_sha256=BODY_SHA,
                github_updated_at=NOW,
                github_snapshot_json={"body_excerpt": "Imported issue body"},
            )
        )
    return handler


async def _materialized(handler: EngineeringActivityHandler) -> UUID:
    result = await handler.handle(
        "backlog.materialize_candidate",
        {
            "backlog_campaign_id": str(CAMPAIGN),
            "project": "falcone",
            "item": {
                "repository": "gntik-ai/falcone",
                "issue_number": 944,
                "body_sha256": BODY_SHA,
            },
        },
    )
    assert result["status"] == "CANDIDATE_MATERIALIZED"
    return UUID(result["finding_id"])


async def test_a_materialized_backlog_finding_binds_its_issue_and_imported_body(
    tmp_path: Path,
) -> None:
    handler = _handler(tmp_path)
    finding_id = await _materialized(handler)
    with handler.sessions() as session:
        finding = session.get(Finding, finding_id)
        assert finding is not None
        binding = MetadataActivityHandler._candidate_issue_binding(session, finding)
    assert binding == {
        "issue_repository": "gntik-ai/falcone",
        "issue_number": 944,
        "issue_body_sha256": BODY_SHA,
    }


async def test_a_malformed_amendment_never_widens_what_the_verifier_reads(
    tmp_path: Path,
) -> None:
    handler = _handler(tmp_path)
    finding_id = await _materialized(handler)
    with handler.sessions.begin() as session:
        record = session.scalar(select(BacklogIssueRecord))
        assert record is not None
        record.github_snapshot_json = {
            **record.github_snapshot_json,
            AMENDMENT_KEY: {"version": 1, "body_sha256": "8" * 64},
        }
    with handler.sessions() as session:
        finding = session.get(Finding, finding_id)
        assert finding is not None
        assert MetadataActivityHandler._candidate_issue_binding(session, finding) == {}


async def test_a_finding_that_is_not_an_existing_issue_has_no_binding(tmp_path: Path) -> None:
    handler = _handler(tmp_path)
    finding_id = await _materialized(handler)
    with handler.sessions.begin() as session:
        finding = session.get(Finding, finding_id)
        assert finding is not None
        finding.persona = "operator"
    with handler.sessions() as session:
        finding = session.get(Finding, finding_id)
        assert finding is not None
        assert MetadataActivityHandler._candidate_issue_binding(session, finding) == {}
