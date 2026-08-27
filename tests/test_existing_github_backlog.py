from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.api.schemas import BacklogApprovalCandidateRequest
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import BacklogIssueRecord, GitHubBacklogCampaign
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.engineering.backlog import (
    BACKLOG_PROJECT_PAIRS,
    BacklogFinalClosureEvidence,
    BacklogIssueType,
    DocumentationImpact,
    ExistingPullRequestStatus,
    GitHubBacklogImportSnapshot,
    GitHubIssueSnapshot,
    GitHubPullRequestSnapshot,
    GitHubRepositoryBacklogSnapshot,
    ReproductionMode,
    approval_binding,
    classify_issue,
    evaluate_final_closure,
    plan_records,
)

NOW = datetime(2026, 8, 27, tzinfo=UTC)
ROOT = Path(__file__).parents[1]


def issue(
    number: int,
    *,
    repository: str = "gntik-ai/falcone",
    title: str = "Gateway login returns HTTP 401",
    body: str = "Login is blocked by falcone#9.",
    labels: tuple[str, ...] = ("bug",),
) -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot(
        repository=repository,
        number=number,
        url=f"https://github.com/{repository}/issues/{number}",
        title=title,
        body_excerpt=body,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        labels=labels,
        assignees=(),
        comments=("bounded comment",),
        updated_at=NOW,
    )


def pull_request(
    number: int,
    issue_number: int,
    *,
    updated_at: datetime = NOW,
    merge_state: str = "CLEAN",
) -> GitHubPullRequestSnapshot:
    return GitHubPullRequestSnapshot(
        repository="gntik-ai/falcone",
        number=number,
        url=f"https://github.com/gntik-ai/falcone/pull/{number}",
        title=f"Fixes #{issue_number}",
        body_excerpt=f"Fixes #{issue_number}",
        state="OPEN",
        draft=True,
        head_ref=f"issue-{issue_number}",
        head_sha="a" * 40,
        base_ref="main",
        merge_state=merge_state,
        updated_at=updated_at,
        comments=(),
        commits=(),
    )


def test_import_snapshot_requires_the_exact_authoritative_pair() -> None:
    source = GitHubRepositoryBacklogSnapshot(
        repository="gntik-ai/falcone",
        default_branch="main",
        head_sha="a" * 40,
        issues=(issue(1),),
        pull_requests=(),
    )
    deployment = GitHubRepositoryBacklogSnapshot(
        repository="gntik-ai/falcone-charts",
        default_branch="main",
        head_sha="b" * 40,
        issues=(),
        pull_requests=(),
    )
    snapshot = GitHubBacklogImportSnapshot(
        project="falcone", imported_at=NOW, repositories=(source, deployment)
    )
    assert (
        tuple(item.repository for item in snapshot.repositories) == BACKLOG_PROJECT_PAIRS["falcone"]
    )
    with pytest.raises(ValidationError, match="authoritative repository pair"):
        GitHubBacklogImportSnapshot(
            project="falcone", imported_at=NOW, repositories=(deployment, source)
        )


def test_classifier_separates_bugs_docs_enhancements_and_product_decisions() -> None:
    bug = classify_issue(
        issue(10), (), project="falcone", import_filter="bugs-and-docs", evaluated_at=NOW
    )
    docs = classify_issue(
        issue(11, title="Docs: explain rollback", labels=("documentation",)),
        (),
        project="falcone",
        import_filter="bugs-and-docs",
        evaluated_at=NOW,
    )
    enhancement = classify_issue(
        issue(12, title="Add export", body="kind: enhancement", labels=("enhancement",)),
        (),
        project="falcone",
        import_filter="bugs-and-docs",
        evaluated_at=NOW,
    )
    decision = classify_issue(
        issue(13, title="Choose retention", labels=("needs-decision",)),
        (),
        project="falcone",
        import_filter="all",
        evaluated_at=NOW,
    )
    assert bug.issue_type is BacklogIssueType.PRODUCT_BUG
    assert bug.reproduction_mode is ReproductionMode.RUNTIME_REQUIRED
    assert bug.planned_wave == "W0"
    assert docs.issue_type is BacklogIssueType.DOCUMENTATION_DEFECT
    assert docs.eligibility_status == "ELIGIBLE_FOR_REVALIDATION"
    assert enhancement.issue_type is BacklogIssueType.ENHANCEMENT
    assert enhancement.eligibility_reason == "ENHANCEMENT_DEFERRED"
    assert decision.issue_type is BacklogIssueType.SPEC_DECISION
    assert decision.eligibility_reason == "NEEDS_PRODUCT_DECISION"


def test_classifier_defers_openspec_tasks_and_security_enhancements_without_defect_evidence() -> (
    None
):
    task = classify_issue(
        issue(
            12,
            repository="gntik-ai/musematic",
            title="[E8.13] Test corpus: real exports",
            labels=("P0", "type:task"),
            body="""openspec:
  kind: chore
  change_required: true
  delta: added

Objective: collect a licensed test corpus.
""",
        ),
        (),
        project="musematic",
        import_filter="bugs-and-docs",
        evaluated_at=NOW,
    )
    security_enhancement = classify_issue(
        issue(
            13,
            title="enhancement: encrypt sensitive workflow payloads",
            labels=("enhancement", "security", "severity:high"),
            body="kind: enhancement\nAdd a new encrypted payload capability.",
        ),
        (),
        project="falcone",
        import_filter="bugs-and-docs",
        evaluated_at=NOW,
    )
    deployment_enhancement = classify_issue(
        issue(
            15,
            repository="gntik-ai/falcone-charts",
            title="enhancement: ship baseline alerting rules",
            labels=(),
            body="Add a new SLO rule capability.",
        ),
        (),
        project="falcone",
        import_filter="bugs-and-docs",
        evaluated_at=NOW,
    )
    confirmed_defect = classify_issue(
        issue(
            14,
            title="bug(security): existing payloads leak credentials",
            labels=("bug", "enhancement", "security"),
            body="kind: enhancement\nIndependently confirmed defect in the current runtime.",
        ),
        (),
        project="falcone",
        import_filter="bugs-and-docs",
        evaluated_at=NOW,
    )

    assert task.issue_type is BacklogIssueType.ENHANCEMENT
    assert task.eligibility_reason == "ENHANCEMENT_DEFERRED"
    assert security_enhancement.issue_type is BacklogIssueType.ENHANCEMENT
    assert security_enhancement.eligibility_reason == "ENHANCEMENT_DEFERRED"
    assert deployment_enhancement.issue_type is BacklogIssueType.ENHANCEMENT
    assert deployment_enhancement.eligibility_reason == "ENHANCEMENT_DEFERRED"
    assert confirmed_defect.issue_type is BacklogIssueType.SECURITY_DEFECT
    assert confirmed_defect.eligibility_status == "ELIGIBLE_FOR_REVALIDATION"


def test_dependency_and_existing_pr_adoption_are_typed() -> None:
    record = classify_issue(
        issue(20, body="Blocked by falcone-charts#7"),
        (pull_request(31, 20),),
        project="falcone",
        import_filter="bugs",
        evaluated_at=NOW,
    )
    assert record.dependencies == ("gntik-ai/falcone-charts#7",)
    assert record.linked_prs[0].status is ExistingPullRequestStatus.ADOPTABLE_ACTIVE_PR
    stale = classify_issue(
        issue(21),
        (pull_request(32, 21, updated_at=NOW - timedelta(days=60)),),
        project="falcone",
        import_filter="bugs",
        evaluated_at=NOW,
    )
    assert stale.linked_prs[0].status is ExistingPullRequestStatus.STALE_PR


def test_dependency_plan_and_approval_hashes_are_deterministic_and_bounded() -> None:
    root = classify_issue(issue(30), (), project="falcone", import_filter="bugs", evaluated_at=NOW)
    dependent = classify_issue(
        issue(31, body="Depends on #30"),
        (),
        project="falcone",
        import_filter="bugs",
        evaluated_at=NOW,
    )
    first, first_hash = plan_records([dependent, root])
    second, second_hash = plan_records([root, dependent])
    assert first_hash == second_hash
    assert [(value.issue_number, value.planned_order) for value in first] == [
        (30, 1),
        (31, 2),
    ]
    assert [value.issue_number for value in first] == [value.issue_number for value in second]
    binding = approval_binding(
        campaign_id="11111111-1111-4111-8111-111111111111",
        project="falcone",
        import_filter="bugs-and-docs",
        source_head="a" * 40,
        deployment_head="b" * 40,
        dependency_plan_hash=first_hash,
        maximum_risk="R2",
        maximum_items=1,
        delivery_mode="full-delivery",
        include_enhancements=False,
        now=NOW,
    )
    assert binding.maximum_concurrency == 1
    assert binding.production is False
    assert binding.sha256() == binding.sha256()
    assert set(binding.paired_repositories) == set(BACKLOG_PROJECT_PAIRS["falcone"])


def test_models_reject_extra_or_outside_repository_input() -> None:
    value = issue(40).model_dump()
    value["shell"] = "gh issue close 40"
    with pytest.raises(ValidationError):
        GitHubIssueSnapshot.model_validate(value)
    value.pop("shell")
    value["repository"] = "attacker/repository"
    value["url"] = "https://github.com/attacker/repository/issues/40"
    with pytest.raises(ValidationError):
        GitHubIssueSnapshot.model_validate(value)


@pytest.mark.asyncio
async def test_backlog_import_flushes_campaign_before_foreign_key_children(tmp_path) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'foreign-keys.sqlite'}",
        project_config_root=ROOT / "config/projects",
        persona_config_root=ROOT / "config/personas",
        flow_config_root=ROOT / "config/flows",
        credential_root=credentials,
    )
    handler = EngineeringActivityHandler(settings)

    @event.listens_for(handler.engine, "connect")
    def enforce_foreign_keys(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(handler.engine)
    campaign_id = "22222222-2222-4222-8222-222222222222"
    snapshot = GitHubBacklogImportSnapshot(
        project="falcone",
        imported_at=NOW,
        repositories=(
            GitHubRepositoryBacklogSnapshot(
                repository="gntik-ai/falcone",
                default_branch="main",
                head_sha="a" * 40,
                issues=(issue(42),),
                pull_requests=(),
            ),
            GitHubRepositoryBacklogSnapshot(
                repository="gntik-ai/falcone-charts",
                default_branch="main",
                head_sha="b" * 40,
                issues=(),
                pull_requests=(),
            ),
        ),
    )

    result = await handler.handle(
        "backlog.persist_import",
        {
            "backlog_campaign_id": campaign_id,
            "project": "falcone",
            "import_filter": "bugs-and-docs",
            "maximum_items": 1,
            "snapshot": snapshot.model_dump(mode="json"),
        },
    )

    assert result["status"] == "IMPORTED"
    with handler.sessions() as session:
        assert session.get(GitHubBacklogCampaign, UUID(campaign_id)) is not None
        persisted = session.scalars(select(BacklogIssueRecord)).all()
        assert len(persisted) == 1
        assert persisted[0].backlog_campaign_id == UUID(campaign_id)


@pytest.mark.asyncio
async def test_telegram_approval_queues_only_one_typed_root_policy_request(tmp_path) -> None:
    request_root = tmp_path / "policy-requests"
    request_root.mkdir(mode=0o700)
    repository = SQLMetadataRepository(
        f"sqlite:///{tmp_path / 'metadata.sqlite'}",
        github_policy_request_root=request_root,
    )
    Base.metadata.create_all(repository.engine)
    campaign_id = UUID("11111111-1111-4111-8111-111111111111")
    with repository.sessions.begin() as session:
        session.add(
            GitHubBacklogCampaign(
                id=campaign_id,
                project="falcone",
                source_repository="gntik-ai/falcone",
                deployment_repository="gntik-ai/falcone-charts",
                imported_at=NOW,
                issue_query="state:open",
                import_filter="bugs-and-docs",
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
                maximum_risk=None,
                maximum_items=1,
                maximum_concurrency=1,
                delivery_mode=None,
                include_enhancements=False,
                status="PLANNED",
                workflow_id="backlog-import-test",
            )
        )
    candidate = await repository.prepare_backlog_approval(
        campaign_id,
        BacklogApprovalCandidateRequest(
            project="falcone",
            delivery_mode="full-delivery",
            maximum_risk="R2",
            maximum_items=1,
        ),
    )
    approved = await repository.approve_backlog_campaign(
        campaign_id,
        "falcone",
        candidate["approval_binding"],
        "telegram:42",
    )
    request = json.loads((request_root / "activate.json").read_text())
    assert approved["status"] == "APPROVED_POLICY_PENDING"
    assert request["action"] == "enable-existing-backlog"
    assert request["safe_repositories"] == [
        "gntik-ai/falcone",
        "gntik-ai/falcone-charts",
        "gntik-ai/musematic",
        "gntik-ai/musematic-deploy",
    ]
    assert (request_root / "activate.json").stat().st_mode & 0o777 == 0o600
    assert "token" not in request and "command" not in request


def test_synthetic_existing_backlog_lifecycle_covers_terminal_cases() -> None:
    existing_pr = classify_issue(
        issue(50),
        (pull_request(51, 50),),
        project="falcone",
        import_filter="bugs-and-docs",
        evaluated_at=NOW,
    )
    duplicate = classify_issue(
        issue(52, labels=("duplicate",), body="Duplicate of #50"),
        (),
        project="falcone",
        import_filter="all",
        evaluated_at=NOW,
    )
    stale = classify_issue(
        issue(53, labels=("stale",), title="Old behavior"),
        (),
        project="falcone",
        import_filter="all",
        evaluated_at=NOW,
    )
    runtime_blocker = classify_issue(
        issue(54, title="Login runtime blocker", labels=("runtime", "bug")),
        (),
        project="falcone",
        import_filter="bugs-and-docs",
        evaluated_at=NOW,
    )
    maker_corrections = ["CHECKER_REQUESTED_CORRECTION", "MAKER_CORRECTION_PASS"]
    closure = BacklogFinalClosureEvidence(
        final_source_commit="a" * 40,
        final_deployment_commit="b" * 40,
        immutable_artifact_digests=("sha256:" + "c" * 64,),
        staging_revision_verified=True,
        issue_acceptance_passed=True,
        regression_suite_passed=True,
        maker_provider="codex",
        checker_provider="claude",
        documentation_impact=DocumentationImpact.DOCUMENTATION_NOT_REQUIRED_WITH_EVIDENCE,
        cleanup_passed=True,
        safe_evidence_comment_ready=True,
        deployed_change=True,
    )
    assert existing_pr.linked_prs[0].status is ExistingPullRequestStatus.ADOPTABLE_ACTIVE_PR
    assert duplicate.eligibility_reason == "DUPLICATE"
    assert stale.eligibility_reason == "STALE_REQUIRES_CURRENT_EVIDENCE"
    assert runtime_blocker.current_runtime_required is True
    assert maker_corrections[-1] == "MAKER_CORRECTION_PASS"
    assert evaluate_final_closure(closure) == "FIXED_CLOSURE_ELIGIBLE"


def test_final_issue_closure_fails_closed() -> None:
    evidence = BacklogFinalClosureEvidence(
        final_source_commit="a" * 40,
        final_deployment_commit=None,
        immutable_artifact_digests=(),
        staging_revision_verified=False,
        issue_acceptance_passed=True,
        regression_suite_passed=True,
        maker_provider="claude",
        checker_provider="codex",
        documentation_impact=DocumentationImpact.DOCUMENTATION_UPDATED_IN_CHANGESET,
        cleanup_passed=True,
        safe_evidence_comment_ready=True,
        deployed_change=False,
    )
    assert evaluate_final_closure(evidence) == "BLOCKED_BY_FINAL_CLOSURE_GATE"
