from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_metadata_activity import add_changeset_finding

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import ChangeSet, ComponentLease, Run


def release_impact(
    repository: str,
    *,
    changed_paths: list[str],
    diff_sha256: str,
    risk_floor: str = "R1",
) -> dict[str, object]:
    impact: dict[str, object] = {
        "version": 1,
        "repository": repository,
        "changed_paths_sha256": hashlib.sha256(
            "\n".join(path.lower() for path in changed_paths).encode()
        ).hexdigest(),
        "diff_sha256": diff_sha256,
        "categories": [],
        "migration": False,
        "risk_floor": risk_floor,
    }
    impact["sha256"] = hashlib.sha256(
        json.dumps(impact, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return impact


def publication_evidence(
    source_head: str, deployment_head: str, *, deployment_changed: bool
) -> dict[str, object]:
    source_paths = ["src/change.py"]
    deployment_paths = ["values.yaml"] if deployment_changed else []
    source_diff_sha = "2" * 64
    deployment_diff_sha = "8" * 64
    return {
        "source": {
            "base_sha": "a" * 40,
            "head_sha": source_head,
            "changed_file_count": 1,
            "changed_paths": source_paths,
            "status_sha256": "3" * 64,
            "diff_sha256": source_diff_sha,
            "diff_redacted_sha256": "4" * 64,
            "release_impact": release_impact(
                "falcone", changed_paths=source_paths, diff_sha256=source_diff_sha
            ),
        },
        "deployment": {
            "base_sha": "c" * 40,
            "head_sha": deployment_head,
            "changed_file_count": 1 if deployment_changed else 0,
            "changed_paths": deployment_paths,
            "status_sha256": "5" * 64,
            "diff_sha256": deployment_diff_sha,
            "diff_redacted_sha256": "6" * 64,
            "release_impact": release_impact(
                "falcone-charts",
                changed_paths=deployment_paths,
                diff_sha256=deployment_diff_sha,
                risk_floor="R2" if deployment_changed else "R1",
            ),
        },
    }


def checker_result() -> dict[str, object]:
    return {
        "structured_result": {
            "decision": "PASS",
            "summary": "independent review passed",
            "findings": [],
            "evidence": [{"kind": "diff", "reference": "bounded", "sha256": "7" * 64}],
            "open_questions": [],
        }
    }


@pytest.mark.asyncio
async def test_publication_plan_defers_unchanged_deployment_repository(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'plan.sqlite'}")
    )
    change_set_id = uuid4()
    branch = f"agent/falcone/123/{change_set_id}"
    Base.metadata.create_all(handler.engine)
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                project="falcone",
                risk_class="R1",
                state="IMPLEMENTING",
                version=1,
                source_repository="falcone",
                source_base_sha="a" * 40,
                deployment_repository="falcone-charts",
                deployment_base_sha="c" * 40,
                image_digests={},
                recovery_json={},
            )
        )
    add_changeset_finding(handler, change_set_id)
    result = await handler.handle(
        "delivery.prepare_publication",
        {
            "change_set_id": str(change_set_id),
            "change_evidence": {
                "source": {"head_sha": "b" * 40, "changed_file_count": 1},
                "deployment": {"head_sha": "c" * 40, "changed_file_count": 0},
            },
            "source_base_sha": "a" * 40,
            "deployment_base_sha": "c" * 40,
            "source_repository": "falcone",
            "deployment_repository": "falcone-charts",
            "source_worktree": str(tmp_path / "source"),
            "deployment_worktree": str(tmp_path / "deployment"),
            "source_branch": branch,
            "deployment_branch": branch,
            "issue_url": "https://github.com/gntik-ai/falcone/issues/123",
            "openspec_identifier": f"engineering/{change_set_id}",
            "risk_class": "R1",
            "domain": "gateway",
        },
    )

    plan = result["publication_plan"]
    assert [item["repository_slug"] for item in plan["publications"]] == ["falcone"]
    assert plan["deferred_deployment"] == {
        "repository_slug": "falcone-charts",
        "branch": branch,
        "head_sha": "c" * 40,
        "base_sha": "c" * 40,
        "reason": "AWAITING_IMMUTABLE_ARTIFACT",
    }


@pytest.mark.asyncio
async def test_publication_persists_both_prs_and_releases_implementation_leases(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'publication.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    run_id = uuid4()
    branch = f"agent/falcone/123/{change_set_id}"
    source_head = "b" * 40
    deployment_head = "d" * 40
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="falcone",
                risk_class="R1",
                state="IMPLEMENTING",
                version=1,
                source_repository="falcone",
                source_branch=None,
                source_base_sha="a" * 40,
                source_head_sha=None,
                source_pr_url=None,
                deployment_repository="falcone-charts",
                deployment_branch=None,
                deployment_base_sha="c" * 40,
                deployment_head_sha=None,
                deployment_pr_url=None,
                image_digests={},
                chart_version=None,
                recovery_json={},
            )
        )
        session.add(
            Run(
                id=run_id,
                change_set_id=change_set_id,
                workflow_id="delivery-test",
                run_type="issue-delivery",
                status="RUNNING",
                project="falcone",
            )
        )
        session.add(
            ComponentLease(
                component_key="falcone:gateway",
                owner_run_id=run_id,
                mode="implementation",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                released_at=None,
            )
        )
    plan = {
        "change_set_id": str(change_set_id),
        "publications": [
            {
                "repository_slug": "falcone",
                "branch": branch,
                "head_sha": source_head,
            },
            {
                "repository_slug": "falcone-charts",
                "branch": branch,
                "head_sha": deployment_head,
            },
        ],
    }
    results = [
        {
            "repository_slug": "falcone",
            "head_sha": source_head,
            "pr_state": "OPEN",
            "pr_url": "https://github.com/gntik-ai/falcone/pull/1",
        },
        {
            "repository_slug": "falcone-charts",
            "head_sha": deployment_head,
            "pr_state": "OPEN",
            "pr_url": "https://github.com/gntik-ai/falcone-charts/pull/2",
        },
    ]

    result = await handler.handle(
        "delivery.persist_publication",
        {
            "change_set_id": str(change_set_id),
            "run_id": str(run_id),
            "status": "SOURCE_PRS_CREATED",
            "publication_plan": plan,
            "publication_results": results,
            "change_evidence": publication_evidence(
                source_head, deployment_head, deployment_changed=True
            ),
            "agent_result": checker_result(),
            "maker_provider": "codex",
            "checker_provider": "claude",
        },
    )

    assert result["status"] == "SOURCE_PR"
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, change_set_id)
        run = session.get(Run, run_id)
        lease = session.scalar(select(ComponentLease))
        assert change_set is not None and change_set.state == "SOURCE_PR"
        assert change_set.source_head_sha == source_head
        assert change_set.deployment_pr_url == results[1]["pr_url"]
        assert run is not None and run.status == "SUCCESS"
        assert lease is not None and lease.released_at is not None


@pytest.mark.asyncio
async def test_source_pr_persists_with_deployment_bound_but_deferred(tmp_path: Path) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'deferred.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    run_id = uuid4()
    branch = f"agent/falcone/123/{change_set_id}"
    source_head = "b" * 40
    deployment_head = "c" * 40
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="falcone",
                risk_class="R1",
                state="IMPLEMENTING",
                version=1,
                source_repository="falcone",
                source_branch=None,
                source_base_sha="a" * 40,
                source_head_sha=None,
                source_pr_url=None,
                deployment_repository="falcone-charts",
                deployment_branch=None,
                deployment_base_sha=deployment_head,
                deployment_head_sha=None,
                deployment_pr_url=None,
                image_digests={},
                chart_version=None,
                recovery_json={},
            )
        )
        session.add(
            Run(
                id=run_id,
                change_set_id=change_set_id,
                workflow_id="delivery-deferred",
                run_type="issue-delivery",
                status="RUNNING",
                project="falcone",
            )
        )
    plan = {
        "change_set_id": str(change_set_id),
        "publications": [
            {
                "repository_slug": "falcone",
                "branch": branch,
                "head_sha": source_head,
            }
        ],
        "deferred_deployment": {
            "repository_slug": "falcone-charts",
            "branch": branch,
            "head_sha": deployment_head,
            "base_sha": deployment_head,
            "reason": "AWAITING_IMMUTABLE_ARTIFACT",
        },
    }
    results = [
        {
            "repository_slug": "falcone",
            "head_sha": source_head,
            "pr_state": "OPEN",
            "pr_url": "https://github.com/gntik-ai/falcone/pull/3",
        }
    ]

    result = await handler.handle(
        "delivery.persist_publication",
        {
            "change_set_id": str(change_set_id),
            "run_id": str(run_id),
            "status": "SOURCE_PRS_CREATED",
            "publication_plan": plan,
            "publication_results": results,
            "change_evidence": publication_evidence(
                source_head, deployment_head, deployment_changed=False
            ),
            "agent_result": checker_result(),
            "maker_provider": "codex",
            "checker_provider": "claude",
        },
    )

    assert result["status"] == "SOURCE_PR"
    assert result["deployment_deferred"] is True
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, change_set_id)
        run = session.get(Run, run_id)
        assert change_set is not None
        assert change_set.source_pr_url == results[0]["pr_url"]
        assert change_set.deployment_head_sha == deployment_head
        assert change_set.deployment_pr_url is None
        assert run is not None and run.status == "SUCCESS"
