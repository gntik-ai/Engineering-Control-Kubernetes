from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import ChangeSet, ComponentLease, Run, WorktreeLease


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
async def test_release_delivery_leases_releases_leases_held_by_the_run(tmp_path: Path) -> None:
    """NEW-20, 2026-09-22: delivery.release_leases is the cleanup step
    IssueDeliveryWorkflow now calls from its except handler when a later step
    (e.g. NEW-19's semgrep crash) leaves an implementation lease dangling for
    its full 4h TTL. Confirms it actually releases whatever the run holds.
    """
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'release.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    run_id = uuid4()
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="musematic",
                risk_class="R1",
                state="IMPLEMENTING",
                version=1,
                source_repository="musematic",
                source_branch=None,
                source_base_sha="a" * 40,
                source_head_sha=None,
                source_pr_url=None,
                deployment_repository="musematic-deploy",
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
                workflow_id="delivery-test-crash",
                run_type="issue-delivery",
                status="RUNNING",
                project="musematic",
            )
        )
        session.add(
            ComponentLease(
                component_key="musematic:general",
                owner_run_id=run_id,
                mode="implementation",
                expires_at=datetime.now(UTC) + timedelta(hours=4),
                released_at=None,
            )
        )

    result = await handler.handle("delivery.release_leases", {"run_id": str(run_id)})

    assert result["status"] == "LEASES_RELEASED"
    with handler.sessions() as session:
        lease = session.scalar(select(ComponentLease))
        assert lease is not None and lease.released_at is not None


@pytest.mark.asyncio
async def test_release_delivery_leases_is_a_noop_without_a_run_id(tmp_path: Path) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'release_noop.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)

    result = await handler.handle("delivery.release_leases", {})

    assert result["status"] == "NO_RUN_TO_RELEASE_LEASES_FOR"


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


def _lease_fixture(tmp_path: Path) -> tuple[MetadataActivityHandler, dict[str, str]]:
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'worktree-lease.sqlite'}",
            worktree_root=tmp_path / "worktrees",
        )
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="musematic",
                risk_class="R1",
                state="IMPLEMENTING",
                version=1,
                source_repository="musematic",
                source_branch=None,
                source_base_sha="a" * 40,
                source_head_sha=None,
                source_pr_url=None,
                deployment_repository="musematic-deploy",
                deployment_branch=None,
                deployment_base_sha="c" * 40,
                deployment_head_sha=None,
                deployment_pr_url=None,
                image_digests={},
                chart_version=None,
                recovery_json={},
            )
        )
    root = tmp_path / "worktrees"
    allocation = {
        "change_set_id": str(change_set_id),
        "source_repository": "musematic",
        "source_worktree": str(root / "musematic" / str(change_set_id)),
        "source_branch": f"agent/musematic/1026/{change_set_id}",
        "source_base_sha": "a" * 40,
        "deployment_repository": "musematic-deploy",
        "deployment_worktree": str(root / "musematic-deploy" / str(change_set_id)),
        "deployment_branch": f"agent/musematic-deploy/1026/{change_set_id}",
        "deployment_base_sha": "c" * 40,
    }
    return handler, allocation


def _delivery_run(handler: MetadataActivityHandler, change_set_id: str) -> str:
    run_id = uuid4()
    with handler.sessions.begin() as session:
        session.add(
            Run(
                id=run_id,
                change_set_id=UUID(change_set_id),
                workflow_id=f"delivery-{run_id}",
                run_type="issue-delivery",
                status="RUNNING",
                project="musematic",
            )
        )
    return str(run_id)


@pytest.mark.asyncio
async def test_a_retry_reclaims_worktree_leases_released_by_a_stopped_delivery(
    tmp_path: Path,
) -> None:
    # NEW-32: musematic #1026's first delivery stopped at the spec step and kept its 8h
    # worktree leases, so the retry failed with "active worktree lease conflicts with
    # this delivery run"; after a release, the fixed lease id also collided on insert.
    handler, allocation = _lease_fixture(tmp_path)
    first = _delivery_run(handler, allocation["change_set_id"])
    second = _delivery_run(handler, allocation["change_set_id"])
    await handler.handle("delivery.persist_worktree_leases", {**allocation, "run_id": first})

    released = await handler.handle("delivery.release_leases", {"run_id": first})
    retried = await handler.handle(
        "delivery.persist_worktree_leases", {**allocation, "run_id": second}
    )

    assert released["status"] == "LEASES_RELEASED"
    assert retried["status"] == "WORKTREE_LEASES_PERSISTED"
    with handler.sessions() as session:
        leases = session.scalars(select(WorktreeLease)).all()
    assert len(leases) == 2
    assert all(str(lease.owner_run_id) == second and lease.released_at is None for lease in leases)


@pytest.mark.asyncio
async def test_an_active_worktree_lease_of_another_run_still_conflicts(tmp_path: Path) -> None:
    handler, allocation = _lease_fixture(tmp_path)
    first = _delivery_run(handler, allocation["change_set_id"])
    second = _delivery_run(handler, allocation["change_set_id"])
    await handler.handle("delivery.persist_worktree_leases", {**allocation, "run_id": first})

    with pytest.raises(Exception) as caught:
        await handler.handle("delivery.persist_worktree_leases", {**allocation, "run_id": second})
    assert "active worktree lease conflicts" in str(caught.value.__cause__)


@pytest.mark.asyncio
async def test_worktree_leases_are_kept_once_a_pull_request_exists(tmp_path: Path) -> None:
    handler, allocation = _lease_fixture(tmp_path)
    run = _delivery_run(handler, allocation["change_set_id"])
    await handler.handle("delivery.persist_worktree_leases", {**allocation, "run_id": run})
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(allocation["change_set_id"]))
        assert change_set is not None
        change_set.source_pr_url = "https://github.com/gntik-ai/musematic/pull/1"

    await handler.handle("delivery.release_leases", {"run_id": run})

    with handler.sessions() as session:
        leases = session.scalars(select(WorktreeLease)).all()
    assert leases and all(lease.released_at is None for lease in leases)


def test_spec_prompt_states_the_output_contract_the_schema_enforces(tmp_path: Path) -> None:
    # NEW-34: every live spec run needed all 5 structured-output attempts and two
    # musematic #1026 runs exhausted them: the prompt never stated the schema's limits.
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'spec.sqlite'}")
    )
    prepared = handler._prepare_spec(
        {
            "spec_provider": "claude",
            "change_set_id": str(uuid4()),
            "orchestration": {"workflow_id": "backlog-loop-musematic-issue-1-attempt-1"},
            "expected_behavior": "benchmark exists",
            "actual_behavior": "no benchmark",
            "minimal_reproduction": ["inspect tests/e2e/performance"],
            "issue_url": "https://github.com/gntik-ai/musematic/issues/1026",
            "fingerprint": "f" * 64,
            "risk_class": "R1",
            "source_base_sha": "a" * 40,
            "deployment_base_sha": "b" * 40,
            "deployment_worktree": "/srv/engineering/worktrees/musematic-deploy/x",
            "source_worktree": "/srv/engineering/worktrees/musematic/x",
            "project": "musematic",
        }
    )
    prompt = prepared["spec_request"]["prompt"]
    schema = MetadataActivityHandler._spec_output_schema()

    assert "no others" in prompt
    for key in schema["required"]:
        assert key in prompt
    assert f"at most {schema['properties']['title']['maxLength']} characters" in prompt
    item_limit = schema["properties"]["scope"]["items"]["maxLength"]
    assert f"at most {item_limit} characters each" in prompt
