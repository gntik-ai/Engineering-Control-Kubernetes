from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import ChangeSet, ComponentLease, Run, WorktreeLease

ROOT = Path(__file__).parents[1]


def seed_reconciliation(
    handler: MetadataActivityHandler, *, recovery: dict[str, object]
) -> tuple[UUID, UUID, str]:
    change_set_id = uuid4()
    run_id = uuid4()
    workflow_id = f"reconcile-{change_set_id}"
    branch = f"agent/falcone/123/{change_set_id}"
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="falcone",
                risk_class="R1",
                state="SOURCE_PR",
                version=1,
                source_repository="falcone",
                source_branch=branch,
                source_base_sha="0" * 40,
                source_head_sha="1" * 40,
                source_pr_url="https://github.com/gntik-ai/falcone/pull/7",
                deployment_repository="falcone-charts",
                deployment_branch=branch,
                deployment_base_sha="2" * 40,
                deployment_head_sha="2" * 40,
                deployment_pr_url=None,
                image_digests={},
                chart_version=None,
                recovery_json=recovery,
            )
        )
        session.add(
            Run(
                id=run_id,
                change_set_id=change_set_id,
                workflow_id=workflow_id,
                run_type="delivery-reconciliation",
                status="QUEUED",
                project="falcone",
            )
        )
    return change_set_id, run_id, workflow_id


def review_recovery() -> dict[str, object]:
    return {
        "provider_chain": {"maker": "codex", "checker": "claude"},
        "independent_review": {
            "decision": "PASS",
            "provider": "claude",
            "sha256": "3" * 64,
        },
    }


def releasable_recovery() -> dict[str, object]:
    value = review_recovery()
    value["ci"] = {"checks_sha256": "4" * 64}
    value["release_impact"] = {
        "risk_floor": "R1",
        "migration": False,
        "categories": [],
        "classifier_sha256": "5" * 64,
    }
    return value


@pytest.mark.asyncio
async def test_metadata_advances_ci_but_persists_actual_candidate_contract_blocker(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'metadata.sqlite'}",
            project_config_root=ROOT / "config" / "projects",
        )
    )
    Base.metadata.create_all(handler.engine)
    change_set_id, run_id, workflow_id = seed_reconciliation(handler, recovery=review_recovery())
    prepared = await handler.handle(
        "reconcile.prepare",
        {
            "change_set_id": str(change_set_id),
            "candidate_build_run_id": None,
            "dry_run": True,
            "orchestration": {"workflow_id": workflow_id},
        },
    )

    result = await handler.handle(
        "reconcile.persist_ci",
        {
            **prepared,
            "status": "CI_VALIDATED",
            "candidate_status": "FALCONE_CANDIDATE_BUILD_CONTRACT_MISSING",
            "ci_evidence": {
                "source_head_sha": "1" * 40,
                "checks_sha256": "4" * 64,
                "check_count": 3,
                "independent_review_sha256": "3" * 64,
            },
            "ci_evidence_artifact": {"id": str(uuid4())},
        },
    )

    assert result["status"] == "CI_VALIDATED"
    assert result["blocker"] == "FALCONE_CANDIDATE_BUILD_CONTRACT_MISSING"
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, change_set_id)
        run = session.get(Run, run_id)
        assert change_set is not None and change_set.state == "CI_VALIDATED"
        assert change_set.recovery_json["ci"]["checks_sha256"] == "4" * 64
        assert run is not None and run.status == "BLOCKED"
        assert run.error_code == "FALCONE_CANDIDATE_BUILD_CONTRACT_MISSING"


@pytest.mark.asyncio
async def test_metadata_rejects_reconciliation_without_persisted_cross_provider_review(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'review.sqlite'}",
            project_config_root=ROOT / "config" / "projects",
        )
    )
    Base.metadata.create_all(handler.engine)
    change_set_id, run_id, workflow_id = seed_reconciliation(handler, recovery={})

    result = await handler.handle(
        "reconcile.prepare",
        {
            "change_set_id": str(change_set_id),
            "candidate_build_run_id": None,
            "dry_run": True,
            "orchestration": {"workflow_id": workflow_id},
        },
    )

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "INDEPENDENT_REVIEW_EVIDENCE_MISSING"
    with handler.sessions() as session:
        run = session.get(Run, run_id)
        assert run is not None and run.status == "BLOCKED"
        assert run.finished_at is not None


@pytest.mark.asyncio
async def test_signed_candidate_persists_digests_and_dry_run_stops_before_git_mutation(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "projects"
    project_root.mkdir()
    config = yaml.safe_load(
        (ROOT / "config" / "projects" / "falcone.yaml").read_text(encoding="utf-8")
    )
    config["delivery_control"]["candidate_build"]["enabled"] = True
    config["delivery_control"]["candidate_build"]["blocker"] = None
    (project_root / "falcone.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'candidate.sqlite'}",
            project_config_root=project_root,
        )
    )
    Base.metadata.create_all(handler.engine)
    change_set_id, run_id, workflow_id = seed_reconciliation(handler, recovery=review_recovery())
    prepared = await handler.handle(
        "reconcile.prepare",
        {
            "change_set_id": str(change_set_id),
            "candidate_build_run_id": 99,
            "dry_run": True,
            "orchestration": {"workflow_id": workflow_id},
        },
    )
    policy = prepared["reconciliation_plan"]["policy"]["immutable_promotion"]
    images = [
        {
            "component": component,
            "repository": repository,
            "digest": "sha256:" + f"{index:x}" * 64,
        }
        for index, (component, repository) in enumerate(
            policy["image_repositories"].items(), start=1
        )
    ]
    persisted = await handler.handle(
        "reconcile.persist_ci",
        {
            **prepared,
            "status": "CANDIDATE_VERIFIED",
            "candidate_status": "VERIFIED",
            "candidate_manifest": {
                "schema_version": 1,
                "project": "falcone",
                "source_repository": "gntik-ai/falcone",
                "source_sha": "1" * 40,
                "workflow_run_id": 99,
                "images": images,
            },
            "ci_evidence": {
                "source_head_sha": "1" * 40,
                "checks_sha256": "4" * 64,
                "check_count": 3,
                "independent_review_sha256": "3" * 64,
                "candidate": {
                    "workflow_run_id": 99,
                    "source_sha": "1" * 40,
                    "manifest_sha256": "5" * 64,
                    "attestation_sha256": {image["component"]: "6" * 64 for image in images},
                },
            },
            "ci_evidence_artifact": {"id": str(uuid4())},
        },
    )
    stopped = await handler.handle("reconcile.prepare_promotion", {**persisted, "dry_run": True})

    assert persisted["status"] == "CANDIDATE_BUILT"
    assert stopped["status"] == "DRY_RUN"
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, change_set_id)
        run = session.get(Run, run_id)
        assert change_set is not None and change_set.state == "CANDIDATE_BUILT"
        assert len(change_set.image_digests) == len(images)
        assert run is not None and run.status == "SUCCESS"
        assert run.finished_at is not None


@pytest.mark.asyncio
async def test_promotion_metadata_binds_commit_pr_and_releases_component_lease(
    tmp_path: Path,
) -> None:
    worktree_root = tmp_path / "worktrees"
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'promotion.sqlite'}",
            project_config_root=ROOT / "config" / "projects",
            worktree_root=worktree_root,
        )
    )
    Base.metadata.create_all(handler.engine)
    change_set_id, run_id, workflow_id = seed_reconciliation(
        handler, recovery=releasable_recovery()
    )
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, change_set_id)
        assert change_set is not None and change_set.deployment_branch is not None
        change_set.state = "CANDIDATE_BUILT"
        policy = yaml.safe_load(
            (ROOT / "config" / "projects" / "falcone.yaml").read_text(encoding="utf-8")
        )["delivery_control"]["immutable_promotion"]
        change_set.image_digests = {
            component: "sha256:" + f"{index:x}" * 64
            for index, component in enumerate(policy["image_repositories"], start=1)
        }
        session.add(
            WorktreeLease(
                change_set_id=change_set_id,
                repository="falcone-charts",
                worktree_path=(worktree_root / "falcone-charts" / str(change_set_id)).as_posix(),
                branch=change_set.deployment_branch,
                owner_run_id=run_id,
                base_sha="2" * 40,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                released_at=None,
            )
        )
    prepared = await handler.handle(
        "reconcile.prepare",
        {
            "change_set_id": str(change_set_id),
            "candidate_build_run_id": None,
            "dry_run": False,
            "orchestration": {"workflow_id": workflow_id},
        },
    )
    promotion = await handler.handle(
        "reconcile.prepare_promotion", {**prepared, "status": "CANDIDATE_BUILT"}
    )
    promoted_sha = "7" * 40
    persisted = await handler.handle(
        "reconcile.persist_promotion",
        {
            **promotion,
            "status": "PROMOTION_COMMITTED",
            "promoted_head_sha": promoted_sha,
            "promotion_evidence": {
                "base_sha": "2" * 40,
                "promoted_sha": promoted_sha,
                "image_digests": promotion["promotion_plan"]["image_digests"],
                "diff_sha256": "8" * 64,
                "rendered_manifest_sha256": "9" * 64,
                "chart_version": "1.2.3",
            },
            "promotion_artifacts": [{"id": str(uuid4())}],
        },
    )
    published = await handler.handle(
        "reconcile.persist_promotion_publication",
        {
            **persisted,
            "status": "DEPLOYMENT_PR_CREATED",
            "promotion_result": {
                "repository_slug": "falcone-charts",
                "head_sha": promoted_sha,
                "pr_state": "OPEN",
                "pr_url": "https://github.com/gntik-ai/falcone-charts/pull/11",
            },
        },
    )

    assert published["status"] == "RELEASE_REVIEW_REQUIRED"
    prepared_review = await handler.handle("reconcile.prepare_release_review", published)
    now = datetime.now(UTC)
    review_request = prepared_review["release_review_request"]
    structured_review = {
        "decision": "PASS",
        "findings": [],
        "verified_tests": ["release-plan:" + prepared_review["release_review_plan"]["plan_sha256"]],
        "risk_assessment": "immutable release and recovery bindings are coherent",
    }
    result = await handler.handle(
        "reconcile.persist_release_review",
        {
            **prepared_review,
            "agent_result": {
                "run_id": review_request["run_id"],
                "provider": review_request["provider"],
                "role": "release-plan-reviewer",
                "status": "SUCCESS",
                "summary": "review passed",
                "structured_result": structured_review,
                "started_at": (now - timedelta(seconds=1)).isoformat(),
                "finished_at": now.isoformat(),
                "exit_code": 0,
            },
        },
    )

    assert result["status"] == "DEPLOY_PR"
    assert result["release_evidence"]["release_plan_review_sha256"]
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, change_set_id)
        run = session.get(Run, run_id)
        component = session.query(ComponentLease).one()
        assert change_set is not None and change_set.state == "DEPLOY_PR"
        assert change_set.deployment_head_sha == promoted_sha
        assert change_set.chart_version == "1.2.3"
        assert run is not None and run.status == "SUCCESS"
        assert change_set.recovery_json["release_evidence"]["ci_result_sha256"] == "4" * 64
        assert component.released_at is not None
