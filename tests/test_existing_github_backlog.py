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
from engineering_control.db.models import BacklogIssueRecord, CoverageCell, GitHubBacklogCampaign
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.engineering.backlog import (
    BACKLOG_APPROVAL_PHRASE,
    BACKLOG_PROJECT_PAIRS,
    BACKLOG_RELEASE_OWNERSHIP_PHRASE,
    BacklogApprovalBinding,
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
    canonical_sha256,
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


def test_serialize_backlog_issue_includes_body_sha256() -> None:
    """Regression test for a real production incident, twice: NEW-15
    (2026-09-20/21) found _serialize_backlog_issue omitted body_sha256,
    crashing _materialize_backlog_candidate's `item["body_sha256"]` read with
    KeyError on the first genuine delivery attempt. That fix was only ever
    applied as a live hotfix directly to a running venv's site-packages copy
    -- never committed here, and not even present in the release source tree
    it was hotfixed on top of (confirmed by inspecting that release directly).
    A later clean redeploy (2026-09-22, unrelated to this finding) silently
    reverted it, and the exact same KeyError recurred on the next real
    delivery attempt. Fixed again here, this time actually in git, with this
    test so a future redeploy can't silently lose it a second time.
    """
    record = BacklogIssueRecord(
        backlog_campaign_id=UUID(int=0),
        repository="gntik-ai/llmwiki",
        issue_number=7,
        issue_url_reference="https://github.com/gntik-ai/llmwiki/issues/7",
        title="Bounded issue 7",
        state="OPEN",
        labels_json=["bug"],
        milestone=None,
        issue_type="PRODUCT_BUG",
        openspec_kind=None,
        severity="MEDIUM",
        calculated_risk="R1",
        component="runtime",
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
        planned_order=1,
        finding_id=None,
        change_set_id=None,
        delivery_status="NOT_STARTED",
        final_closure_status="OPEN",
        documentation_impact="DOCUMENTATION_REQUIRED",
        body_sha256="c" * 64,
        github_updated_at=NOW,
        github_snapshot_json={},
    )

    serialized = EngineeringActivityHandler._serialize_backlog_issue(record)

    assert serialized["body_sha256"] == "c" * 64
    # This is the exact read that crashed with KeyError in production twice --
    # assert it directly, not just that the key exists.
    item = serialized
    assert item["body_sha256"]


@pytest.mark.asyncio
async def test_materialize_backlog_candidate_handles_two_issues_sharing_a_component(
    tmp_path,
) -> None:
    """Regression test for a real production incident (2026-09-22, NEW-16
    follow-up): the first campaign that ever attempted more than one backlog
    issue (musematic, once --max-items was raised past 1) crashed with
    'duplicate key value violates unique constraint "uq_coverage_cell"'.

    Root cause: uq_coverage_cell is keyed on (campaign_id, repository_surface,
    domain, persona, flow, lens, environment) -- not each CoverageCell row's
    own primary key. persona/flow/lens/environment are fixed literals for
    every backlog issue, and domain was plain `record.component`, so two
    issues sharing a component collided on that unique constraint even though
    their own coverage_id (primary key) correctly differed.

    Fixed by suffixing domain with the issue reference. This test seeds two
    BacklogIssueRecords in the same campaign with the SAME component and
    confirms both materialize without a constraint violation, and that they
    get distinct domains.
    """
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'coverage-cell.sqlite'}",
        project_config_root=ROOT / "config/projects",
        persona_config_root=ROOT / "config/personas",
        flow_config_root=ROOT / "config/flows",
        credential_root=credentials,
    )
    handler = EngineeringActivityHandler(settings)
    Base.metadata.create_all(handler.engine)

    campaign_id = UUID("33333333-3333-4333-8333-333333333333")
    with handler.sessions.begin() as session:
        session.add(
            GitHubBacklogCampaign(
                id=campaign_id,
                project="musematic",
                source_repository="gntik-ai/musematic",
                deployment_repository="gntik-ai/musematic-deploy",
                imported_at=NOW,
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
                maximum_risk="R1",
                maximum_items=2,
                maximum_concurrency=1,
                delivery_mode="source-pr",
                include_enhancements=False,
                status="RUNNING",
                workflow_id="backlog-loop-musematic-test",
            )
        )
        for number in (1, 2):
            session.add(
                BacklogIssueRecord(
                    backlog_campaign_id=campaign_id,
                    repository="gntik-ai/musematic",
                    issue_number=number,
                    issue_url_reference=f"https://github.com/gntik-ai/musematic/issues/{number}",
                    title=f"Bounded issue {number}",
                    state="OPEN",
                    labels_json=["bug"],
                    milestone=None,
                    issue_type="PRODUCT_BUG",
                    openspec_kind=None,
                    severity="MEDIUM",
                    calculated_risk="R1",
                    component="general",  # same component for both -- this is the collision
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
                    current_runtime_required=False,
                    reproduction_mode="SOURCE_ONLY",
                    candidate_status="READY",
                    eligibility_status="READY",
                    eligibility_reason="CURRENT_REVALIDATION_PASS",
                    planned_wave="W0",
                    planned_order=number,
                    finding_id=None,
                    change_set_id=None,
                    delivery_status="NOT_STARTED",
                    final_closure_status="OPEN",
                    documentation_impact="DOCUMENTATION_REQUIRED",
                    body_sha256=(str(number) * 64),
                    github_updated_at=NOW,
                    github_snapshot_json={},
                )
            )

    for number in (1, 2):
        result = await handler.handle(
            "backlog.materialize_candidate",
            {
                "backlog_campaign_id": str(campaign_id),
                "project": "musematic",
                "item": {
                    "repository": "gntik-ai/musematic",
                    "issue_number": number,
                    "body_sha256": str(number) * 64,
                },
            },
        )
        assert result["status"] == "CANDIDATE_MATERIALIZED"

    with handler.sessions() as session:
        cells = session.scalars(
            select(CoverageCell).where(CoverageCell.campaign_id == campaign_id)
        ).all()
    assert len(cells) == 2
    assert len({cell.domain for cell in cells}) == 2
    assert all(cell.domain.startswith("general:") for cell in cells)


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
        "gntik-ai/llmwiki",
    ]
    assert (request_root / "activate.json").stat().st_mode & 0o777 == 0o600
    assert "token" not in request and "command" not in request


def _plain_campaign(
    campaign_id: UUID, project: str, source_repository: str, deployment_repository: str
) -> GitHubBacklogCampaign:
    return GitHubBacklogCampaign(
        id=campaign_id,
        project=project,
        source_repository=source_repository,
        deployment_repository=deployment_repository,
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
        workflow_id=f"backlog-import-{project}-{campaign_id}",
    )


@pytest.mark.asyncio
async def test_release_ownership_frees_the_active_owner_slot_early(tmp_path) -> None:
    request_root = tmp_path / "policy-requests"
    request_root.mkdir(mode=0o700)
    repository = SQLMetadataRepository(
        f"sqlite:///{tmp_path / 'metadata.sqlite'}",
        github_policy_request_root=request_root,
    )
    Base.metadata.create_all(repository.engine)
    owner_id = UUID("11111111-1111-4111-8111-111111111111")
    inventory_id = UUID("22222222-2222-4222-8222-222222222222")
    fresh_id = UUID("33333333-3333-4333-8333-333333333333")
    with repository.sessions.begin() as session:
        session.add(
            _plain_campaign(owner_id, "falcone", "gntik-ai/falcone", "gntik-ai/falcone-charts")
        )

    owner_candidate = await repository.prepare_backlog_approval(
        owner_id,
        BacklogApprovalCandidateRequest(
            project="falcone", delivery_mode="full-delivery", maximum_risk="R2", maximum_items=1
        ),
    )
    owner_approved = await repository.approve_backlog_campaign(
        owner_id, "falcone", owner_candidate["approval_binding"], "telegram:42"
    )
    assert owner_approved["status"] == "APPROVED_POLICY_PENDING"

    # Simulate the root reconciler promoting the queued owner (matching the established
    # pattern in test_global_inventory_approval_reuses_the_active_shared_policy below --
    # nothing in this test process actually consumes activate.json).
    (request_root / "activate.json").unlink()
    with repository.sessions.begin() as session:
        owner = session.get(GitHubBacklogCampaign, owner_id)
        assert owner is not None
        owner.status = "APPROVED"
        owner.approval_json = {
            key: value for key, value in owner.approval_json.items() if key != "policy_role"
        }

    with repository.sessions.begin() as session:
        session.add(
            _plain_campaign(
                inventory_id, "musematic", "gntik-ai/musematic", "gntik-ai/musematic-deploy"
            )
        )
    inventory_candidate = await repository.prepare_backlog_approval(
        inventory_id,
        BacklogApprovalCandidateRequest(
            project="musematic", delivery_mode="full-delivery", maximum_risk="R2", maximum_items=1
        ),
    )
    inventory_approved = await repository.approve_backlog_campaign(
        inventory_id, "musematic", inventory_candidate["approval_binding"], "telegram:42"
    )
    assert inventory_approved["status"] == "APPROVED_INVENTORY"

    # Wrong actor and wrong phrase are both rejected before anything changes.
    with pytest.raises(ValueError, match="reauthenticated operator"):
        await repository.release_backlog_ownership(
            owner_id, "falcone", BACKLOG_RELEASE_OWNERSHIP_PHRASE, "forged:actor"
        )
    with pytest.raises(ValueError, match="exact typed confirmation"):
        await repository.release_backlog_ownership(
            owner_id, "falcone", "WRONG_PHRASE", "telegram:42"
        )
    # A campaign that only holds "inventory" role has nothing to release.
    with pytest.raises(ValueError, match="does not currently hold ownership"):
        await repository.release_backlog_ownership(
            inventory_id, "musematic", BACKLOG_RELEASE_OWNERSHIP_PHRASE, "telegram:42"
        )

    released = await repository.release_backlog_ownership(
        owner_id, "falcone", BACKLOG_RELEASE_OWNERSHIP_PHRASE, "telegram:42"
    )
    assert released == {
        "status": "CANCELLED",
        "backlog_campaign_id": str(owner_id),
        "policy_activation": "OWNERSHIP_RELEASED",
    }
    with repository.sessions() as session:
        cancelled = session.get(GitHubBacklogCampaign, owner_id)
        assert cancelled.status == "CANCELLED"
        assert cancelled.approval_json["released_by"] == "telegram:42"

    # Releasing again fails closed -- it's already terminal, not pending/approved.
    with pytest.raises(ValueError, match="only a not-yet-executing owner campaign"):
        await repository.release_backlog_ownership(
            owner_id, "falcone", BACKLOG_RELEASE_OWNERSHIP_PHRASE, "telegram:42"
        )

    # A brand-new campaign now becomes owner immediately, without waiting on any TTL.
    with repository.sessions.begin() as session:
        session.add(
            _plain_campaign(fresh_id, "falcone", "gntik-ai/falcone", "gntik-ai/falcone-charts")
        )
    fresh_candidate = await repository.prepare_backlog_approval(
        fresh_id,
        BacklogApprovalCandidateRequest(
            project="falcone", delivery_mode="full-delivery", maximum_risk="R2", maximum_items=1
        ),
    )
    fresh_approved = await repository.approve_backlog_campaign(
        fresh_id, "falcone", fresh_candidate["approval_binding"], "telegram:42"
    )
    assert fresh_approved["status"] == "APPROVED_POLICY_PENDING"
    assert fresh_approved["policy_activation"] == "QUEUED_ROOT_RECONCILER"


@pytest.mark.asyncio
async def test_global_inventory_approval_reuses_the_active_shared_policy(tmp_path) -> None:
    request_root = tmp_path / "policy-requests"
    request_root.mkdir(mode=0o700)
    repository = SQLMetadataRepository(
        f"sqlite:///{tmp_path / 'metadata.sqlite'}",
        github_policy_request_root=request_root,
    )
    Base.metadata.create_all(repository.engine)
    falcone_id = UUID("11111111-1111-4111-8111-111111111111")
    musematic_id = UUID("22222222-2222-4222-8222-222222222222")

    def campaign(
        campaign_id: UUID,
        project: str,
        source: str,
        deployment: str,
        source_head: str,
        deployment_head: str,
    ) -> GitHubBacklogCampaign:
        return GitHubBacklogCampaign(
            id=campaign_id,
            project=project,
            source_repository=source,
            deployment_repository=deployment,
            imported_at=NOW,
            issue_query="state:open",
            import_filter="all",
            source_default_branch="main",
            deployment_default_branch="main",
            source_head_at_import=source_head,
            deployment_head_at_import=deployment_head,
            issue_count=1,
            eligible_count=1,
            deferred_count=0,
            duplicate_count=0,
            stale_count=0,
            already_fixed_count=0,
            existing_pr_count=0,
            blocked_count=0,
            dependency_graph_hash="c" * 64,
            plan_hash=("d" if project == "falcone" else "e") * 64,
            approval_binding=None,
            approval_json={},
            maximum_risk=None,
            maximum_items=1,
            maximum_concurrency=1,
            delivery_mode=None,
            include_enhancements=False,
            status="PLANNED",
            workflow_id=f"backlog-import-{project}",
        )

    with repository.sessions.begin() as session:
        session.add_all(
            [
                campaign(
                    falcone_id,
                    "falcone",
                    "gntik-ai/falcone",
                    "gntik-ai/falcone-charts",
                    "a" * 40,
                    "b" * 40,
                ),
                campaign(
                    musematic_id,
                    "musematic",
                    "gntik-ai/musematic",
                    "gntik-ai/musematic-deploy",
                    "f" * 40,
                    "9" * 40,
                ),
            ]
        )
    falcone_candidate = await repository.prepare_backlog_approval(
        falcone_id,
        BacklogApprovalCandidateRequest(
            project="falcone",
            delivery_mode="full-delivery",
            maximum_risk="R2",
            maximum_items=1,
        ),
    )
    await repository.approve_backlog_campaign(
        falcone_id,
        "falcone",
        falcone_candidate["approval_binding"],
        "cli:engineering-ctl",
    )
    musematic_candidate = await repository.prepare_backlog_approval(
        musematic_id,
        BacklogApprovalCandidateRequest(
            project="musematic",
            delivery_mode="full-delivery",
            maximum_risk="R2",
            maximum_items=1,
        ),
    )
    with pytest.raises(ValueError, match="activation is pending"):
        await repository.approve_backlog_campaign(
            musematic_id,
            "musematic",
            musematic_candidate["approval_binding"],
            "cli:engineering-ctl",
        )
    with repository.sessions.begin() as session:
        pending_owner = session.get(GitHubBacklogCampaign, falcone_id)
        assert pending_owner is not None
        pending_binding = dict(pending_owner.approval_json["binding"])
        pending_binding["expires_at"] = "not-a-timestamp"
        pending_owner.approval_json = {
            **pending_owner.approval_json,
            "binding": pending_binding,
        }
    with pytest.raises(ValueError, match="activation evidence is malformed"):
        await repository.approve_backlog_campaign(
            musematic_id,
            "musematic",
            musematic_candidate["approval_binding"],
            "cli:engineering-ctl",
        )
    with repository.sessions.begin() as session:
        pending_owner = session.get(GitHubBacklogCampaign, falcone_id)
        assert pending_owner is not None
        pending_binding = dict(pending_owner.approval_json["binding"])
        pending_binding["expires_at"] = falcone_candidate["approval_candidate"]["expires_at"]
        pending_owner.approval_json = {
            **pending_binding,
            **pending_owner.approval_json["approval"],
        }
    with pytest.raises(ValueError, match="activation is pending"):
        await repository.approve_backlog_campaign(
            musematic_id,
            "musematic",
            musematic_candidate["approval_binding"],
            "cli:engineering-ctl",
        )
    request_path = request_root / "activate.json"
    request_path.unlink()
    with repository.sessions.begin() as session:
        owner = session.get(GitHubBacklogCampaign, falcone_id)
        assert owner is not None
        owner.status = "APPROVED"
        owner.approval_json = {
            key: value for key, value in owner.approval_json.items() if key != "policy_role"
        }

    approved = await repository.approve_backlog_campaign(
        musematic_id,
        "musematic",
        musematic_candidate["approval_binding"],
        "cli:engineering-ctl",
    )

    assert approved["status"] == "APPROVED_INVENTORY"
    assert approved["policy_activation"] == "EXISTING_ACTIVE_POLICY"
    assert not request_path.exists()
    with repository.sessions() as session:
        assert session.get(GitHubBacklogCampaign, falcone_id).status == "APPROVED"
        assert session.get(GitHubBacklogCampaign, musematic_id).status == "APPROVED_INVENTORY"

    llmwiki_id = UUID("33333333-3333-4333-8333-333333333333")
    with repository.sessions.begin() as session:
        owner = session.get(GitHubBacklogCampaign, falcone_id)
        assert owner is not None
        owner_binding = BacklogApprovalBinding.model_validate(
            {field: owner.approval_json.get(field) for field in BacklogApprovalBinding.model_fields}
        ).model_dump(mode="json")
        owner_binding["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        owner.approval_binding = canonical_sha256(owner_binding)
        owner.approval_json = {
            **owner_binding,
            "approved_by": owner.approval_json["approved_by"],
            "approved_at": owner.approval_json["approved_at"],
            "typed_confirmation": owner.approval_json["typed_confirmation"],
        }
        session.add(
            campaign(
                llmwiki_id,
                "llmwiki",
                "gntik-ai/llmwiki",
                "gntik-ai/llmwiki",
                "7" * 40,
                "7" * 40,
            )
        )
    llmwiki_candidate = await repository.prepare_backlog_approval(
        llmwiki_id,
        BacklogApprovalCandidateRequest(
            project="llmwiki",
            delivery_mode="source-pr",
            maximum_risk="R2",
            maximum_items=1,
        ),
    )
    replacement = await repository.approve_backlog_campaign(
        llmwiki_id,
        "llmwiki",
        llmwiki_candidate["approval_binding"],
        "cli:engineering-ctl",
    )
    assert replacement["status"] == "APPROVED_POLICY_PENDING"
    with repository.sessions() as session:
        assert session.get(GitHubBacklogCampaign, falcone_id).status == "EXPIRED"
        assert session.get(GitHubBacklogCampaign, musematic_id).status == "EXPIRED"
    handler = EngineeringActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'metadata.sqlite'}",
            state_root=tmp_path,
        )
    )
    standalone = handler._load_backlog_execution(
        {
            "project": "musematic",
            "backlog_campaign_id": str(musematic_id),
            "maximum_risk": "R2",
            "delivery_mode": "full-delivery",
            "maximum_items": 1,
        }
    )
    assert standalone["status"] == "BLOCKED"
    assert standalone["blocker"] == "CURRENT_BACKLOG_APPROVAL_REQUIRED"


def test_backlog_stop_after_current_persists_terminal_state_and_policy_deactivation(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    policy_root = state_root / "github-policy-requests"
    policy_root.mkdir(parents=True, mode=0o700)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'stop-after.sqlite'}",
        state_root=state_root,
        project_config_root=ROOT / "config/projects",
        persona_config_root=ROOT / "config/personas",
        flow_config_root=ROOT / "config/flows",
        credential_root=tmp_path / "credentials",
    )
    handler = EngineeringActivityHandler(settings)
    Base.metadata.create_all(handler.engine)
    campaign_id = UUID("33333333-3333-4333-8333-333333333333")
    with handler.sessions.begin() as session:
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
                approval_binding="e" * 64,
                approval_json={"approval": {"approved_by": "dashboard:reauthenticated"}},
                maximum_risk="R2",
                maximum_items=2,
                maximum_concurrency=1,
                delivery_mode="source-pr",
                include_enhancements=False,
                status="RUNNING",
                workflow_id="backlog-loop-stop-after-test",
            )
        )

    result = handler._record_backlog_loop_state(
        {
            "backlog_campaign_id": str(campaign_id),
            "loop_status": "STOPPED",
            "control_state": "STOPPED_AFTER_CURRENT",
        }
    )

    assert result["status"] == "STOPPED"
    with handler.sessions() as session:
        assert session.get(GitHubBacklogCampaign, campaign_id).status == "STOPPED"
    policy = json.loads((policy_root / "activate.json").read_text(encoding="utf-8"))
    assert policy["action"] == "disable-existing-backlog"
    assert policy["requested_by"] == "dashboard:reauthenticated"


def test_legacy_temporal_backlog_approval_rejects_a_malformed_telegram_actor(
    tmp_path: Path,
) -> None:
    handler = EngineeringActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'legacy-approval.sqlite'}")
    )

    result = handler._record_backlog_approval(
        {
            "backlog_campaign_id": "11111111-1111-4111-8111-111111111111",
            "approval_binding": "a" * 64,
            "typed_confirmation": BACKLOG_APPROVAL_PHRASE,
            "actor": "telegram:allowlisted",
        }
    )

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "BACKLOG_APPROVAL_INVALID"


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


def test_a_paused_loop_run_reads_paused_until_it_really_finishes(tmp_path: Path) -> None:
    # NEW-33b: the retry/resume preflight reads the loop Run's status; a paused loop
    # left it RUNNING, so engineering-ctl backlog-retry was refused
    # (WORKFLOW_IS_NOT_RETRYABLE). A pause must not stamp finished_at either.
    from engineering_control.db.models import Run

    state_root = tmp_path / "state"
    (state_root / "github-policy-requests").mkdir(parents=True, mode=0o700)
    handler = EngineeringActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'paused-run.sqlite'}",
            state_root=state_root,
            project_config_root=ROOT / "config/projects",
            persona_config_root=ROOT / "config/personas",
            flow_config_root=ROOT / "config/flows",
            credential_root=tmp_path / "credentials",
        )
    )
    Base.metadata.create_all(handler.engine)
    campaign_id = UUID("44444444-4444-4444-8444-444444444444")
    workflow_id = "backlog-loop-musematic-paused-run"
    with handler.sessions.begin() as session:
        session.add(
            GitHubBacklogCampaign(
                id=campaign_id,
                project="musematic",
                source_repository="gntik-ai/musematic",
                deployment_repository="gntik-ai/musematic-deploy",
                imported_at=NOW,
                issue_query="state:open",
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
                approval_binding="e" * 64,
                approval_json={"approval": {"approved_by": "dashboard:reauthenticated"}},
                maximum_risk="R2",
                maximum_items=1,
                maximum_concurrency=1,
                delivery_mode="source-pr",
                include_enhancements=False,
                status="RUNNING",
                workflow_id=workflow_id,
            )
        )
        session.add(
            Run(
                workflow_id=workflow_id,
                run_type="existing-github-backlog-loop",
                status="RUNNING",
                project="musematic",
            )
        )

    def record(status: str) -> tuple[str, object]:
        handler._record_backlog_loop_state(
            {
                "backlog_campaign_id": str(campaign_id),
                "loop_status": status,
                "workflow_id": workflow_id,
            }
        )
        with handler.sessions() as session:
            run = session.query(Run).filter(Run.workflow_id == workflow_id).one()
            return run.status, run.finished_at

    assert record("PAUSED") == ("PAUSED", None)
    assert record("RUNNING") == ("RUNNING", None)
    status, finished_at = record("STOPPED")
    assert status == "STOPPED" and finished_at is not None
