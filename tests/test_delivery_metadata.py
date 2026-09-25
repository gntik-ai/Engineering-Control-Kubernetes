from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.activities.provider import ProviderActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import (
    AgentInvocation,
    ChangeSet,
    ComponentLease,
    Issue,
    Run,
    WorktreeLease,
)
from engineering_control.models.core import AgentResult
from engineering_control.models.enums import Provider


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


def _lease_fixture(
    tmp_path: Path, **settings: object
) -> tuple[MetadataActivityHandler, dict[str, str]]:
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'worktree-lease.sqlite'}",
            worktree_root=tmp_path / "worktrees",
            **settings,  # type: ignore[arg-type]
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
    # NEW-53: prepare_spec looks up a reusable OpenSpec of the ChangeSet first.
    Base.metadata.create_all(handler.engine)
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
    # The live failure was unparseable 17-26 KB JSON, not a schema limit.
    assert "well under 8000 characters" in prompt
    assert "must be valid JSON" in prompt and "never raw newlines" in prompt


def test_correction_attempt_maker_sees_the_previous_checker_findings(tmp_path: Path) -> None:
    # NEW-39: the first real checker review (musematic #993) requested changes for a
    # genuine upload-size DoS gap, but correction attempts restart from the base with
    # a prompt holding only the OpenSpec -- the maker never learned what to fix.
    from engineering_control.db.models import AgentInvocation

    handler, allocation = _lease_fixture(tmp_path)
    change_set_id = allocation["change_set_id"]
    previous = _delivery_run(handler, change_set_id)
    with handler.sessions.begin() as session:
        session.add(
            AgentInvocation(
                id=uuid4(),
                run_id=UUID(previous),
                provider="claude",
                role="code-reviewer",
                model_class="judge",
                status="SUCCESS",
                structured_result={
                    "decision": "REQUEST_CHANGES",
                    "findings": ["MAJOR - upload cap runs after the body is spooled"],
                    "summary": "five substantive issues",
                },
                started_at=datetime.now(UTC),
            )
        )
    payload = {
        **allocation,
        "maker_provider": "codex",
        "orchestration": {"workflow_id": "backlog-loop-musematic-issue-1-attempt-2"},
        "issue_url": "https://github.com/gntik-ai/musematic/issues/993",
        "risk_class": "R1",
        "project": "musematic",
        "openspec_document": {"decision": "READY", "title": "import"},
    }

    prompt = handler._prepare_maker(payload, deployment=False)["maker_request"]["prompt"]
    first_attempt = handler._prepare_maker(
        {**payload, "change_set_id": str(uuid4())}, deployment=False
    )["maker_request"]["prompt"]

    assert "upload cap runs after the body is spooled" in prompt
    assert "five substantive issues" in prompt
    assert "Independent checker findings" not in first_attempt


def test_resumed_correction_maker_fixes_findings_on_top_of_the_previous_commits(
    tmp_path: Path,
) -> None:
    # NEW-52: musematic #993 ran 11 rounds / ~60 attempts because every correction
    # re-implemented the feature from the base. A resumed worktree keeps the previous
    # attempt's commits and the maker must build on them.
    from engineering_control.db.models import AgentInvocation

    handler, allocation = _lease_fixture(tmp_path)
    change_set_id = allocation["change_set_id"]
    previous = _delivery_run(handler, change_set_id)
    with handler.sessions.begin() as session:
        session.add(
            AgentInvocation(
                id=uuid4(),
                run_id=UUID(previous),
                provider="claude",
                role="code-reviewer",
                model_class="judge",
                status="SUCCESS",
                structured_result={
                    "decision": "REQUEST_CHANGES",
                    "findings": ["BLOCKER - missing size check on resume"],
                },
                started_at=datetime.now(UTC),
            )
        )
    resumed_head = "d" * 40
    payload = {
        **allocation,
        "maker_provider": "codex",
        "checker_provider": "claude",
        "orchestration": {"workflow_id": "backlog-loop-musematic-issue-1-attempt-2"},
        "issue_url": "https://github.com/gntik-ai/musematic/issues/993",
        "risk_class": "R1",
        "project": "musematic",
        "openspec_document": {"decision": "READY", "title": "import"},
        "source_resumed_from_sha": resumed_head,
        "deployment_resumed_from_sha": None,
        "agent_result": {"structured_result": {"decision": "COMPLETE"}},
        "source_maker_result": {"structured_result": {"decision": "COMPLETE"}},
    }

    resumed = handler._prepare_maker(payload, deployment=False)["maker_request"]["prompt"]
    reset = handler._prepare_maker(
        {**payload, "source_resumed_from_sha": None}, deployment=False
    )["maker_request"]["prompt"]
    deployment = handler._prepare_maker(payload, deployment=True)[
        "deployment_maker_request"
    ]["prompt"]
    checker = handler._prepare_checker(payload)["checker_request"]["prompt"]

    assert "missing size check on resume" in resumed
    assert f"HEAD {resumed_head}" in resumed
    assert "Do not re-implement or revert it" in resumed
    assert "additional local commit" in resumed
    assert "the worktree was reset to base" not in resumed
    # The reset (NEW-38) case keeps the NEW-39 wording.
    assert "the worktree was reset to base; address every finding" in reset
    assert "Do not re-implement" not in reset and resumed_head not in reset
    # Resume is per repository: the deployment worktree was not resumed.
    assert "Do not re-implement" not in deployment
    # The checker reviews the full base..HEAD change, not only the latest commit.
    assert f"git diff {'a' * 40}..HEAD" in checker
    assert "may span several commits" in checker


def test_deployment_maker_may_complete_without_changes_and_ignores_baseline(
    tmp_path: Path,
) -> None:
    # NEW-41: musematic #993's deployment maker blocked a finished, committed source
    # change because no deployment change was needed and the deploy repository already
    # held credential-like findings (baseline, not introduced by the change).
    handler, allocation = _lease_fixture(tmp_path)
    payload = {
        **allocation,
        "maker_provider": "codex",
        "orchestration": {"workflow_id": "backlog-loop-musematic-issue-1-attempt-1"},
        "issue_url": "https://github.com/gntik-ai/musematic/issues/993",
        "risk_class": "R1",
        "project": "musematic",
        "openspec_document": {"decision": "READY", "title": "import"},
        "agent_result": {"structured_result": {"decision": "COMPLETE"}},
    }

    deployment = handler._prepare_maker(payload, deployment=True)
    source = handler._prepare_maker(payload, deployment=False)

    prompt = deployment["deployment_maker_request"]["prompt"]
    assert "return decision COMPLETE with an empty changed_files list" in prompt
    assert "tracked by the platform baseline" in prompt
    # NEW-42: the image version comes from the build + promotion, never from the maker.
    assert "Never pin, change or require an image tag or digest" in prompt
    assert "GitHub Packages" in prompt
    assert "empty changed_files" not in source["maker_request"]["prompt"]


@pytest.mark.parametrize(("project_config", "prepared"), [("config/projects", True), (None, False)])
def test_makers_and_checker_are_told_to_run_the_prepared_toolchain_tests(
    tmp_path: Path, project_config: str | None, prepared: bool
) -> None:
    # Makers run offline in the worktree; without the prepared toolchain and the exact
    # commands, the checker kept rejecting for "missing test evidence".
    root = Path(__file__).resolve().parents[1]
    overrides = (
        {"project_config_root": root / project_config}
        if project_config
        else {"project_config_root": tmp_path / "missing"}
    )
    handler, allocation = _lease_fixture(tmp_path, **overrides)
    payload = {
        **allocation,
        "maker_provider": "codex",
        "checker_provider": "claude",
        "orchestration": {"workflow_id": "backlog-loop-musematic-issue-1-attempt-1"},
        "issue_url": "https://github.com/gntik-ai/musematic/issues/993",
        "risk_class": "R1",
        "project": "musematic",
        "openspec_document": {"decision": "READY", "title": "import"},
        "agent_result": {"structured_result": {"decision": "COMPLETE"}},
        "source_maker_result": {"structured_result": {"decision": "COMPLETE"}},
    }

    source = handler._prepare_maker(payload, deployment=False)["maker_request"]["prompt"]
    deployment = handler._prepare_maker(payload, deployment=True)["deployment_maker_request"][
        "prompt"
    ]
    checker = handler._prepare_checker(payload)["checker_request"]["prompt"]

    for prompt in (source, deployment, checker):
        assert "must be valid JSON" in prompt  # _STRUCTURED_OUTPUT_JSON_RULES intact
    assert ("Toolchain prepared in the source worktree" in source) is prepared
    assert ("Toolchain prepared" in checker) is prepared
    assert "Toolchain prepared" not in deployment  # the deployment worktree has none
    if prepared:
        assert "`timeout --kill-after=30 900 .venv/bin/pytest --import-mode=importlib" in source
        assert "`timeout --kill-after=30 900 pnpm exec vitest run --maxWorkers=2`" in source
        assert "in `apps/web`" in source
        assert "tests[] entry with its real status" in source
        assert "do not install anything (no network)" in source
        assert 'evidence[] entry with kind "test"' in checker
        assert "`timeout --kill-after=30 900 .venv/bin/mypy src/platform`" in checker


def _spec_fixture(
    tmp_path: Path, **settings: object
) -> tuple[MetadataActivityHandler, dict[str, object]]:
    """A ChangeSet bound to a GitHub issue plus a delivery.prepare_spec payload."""
    handler, allocation = _lease_fixture(tmp_path, **settings)
    issue_id = uuid4()
    with handler.sessions.begin() as session:
        session.add(
            Issue(
                id=issue_id,
                finding_id=uuid4(),
                repository="gntik-ai/musematic",
                number=993,
                url="https://github.com/gntik-ai/musematic/issues/993",
                state="OPEN",
                openspec_change=None,
            )
        )
        change_set = session.get(ChangeSet, UUID(allocation["change_set_id"]))
        assert change_set is not None
        change_set.issue_id = issue_id
    payload: dict[str, object] = {
        **allocation,
        "spec_provider": "claude",
        "checker_provider": "claude",
        "orchestration": {"workflow_id": "backlog-loop-musematic-issue-993-attempt-1"},
        "expected_behavior": "uploads are capped before spooling",
        "actual_behavior": "the whole body is spooled first",
        "minimal_reproduction": ["POST a 2 GB upload"],
        "issue_url": "https://github.com/gntik-ai/musematic/issues/993",
        "fingerprint": "f" * 64,
        "risk_class": "R1",
        "project": "musematic",
    }
    return handler, payload


_READY_SPEC = {
    "decision": "READY",
    "title": "Cap uploads before spooling",
    "problem": "The upload body is spooled before the size cap applies.",
    "scope": ["enforce the cap while streaming"],
    "non_scope": [],
    "acceptance_criteria": ["a 2 GB upload is rejected without spooling"],
    "implementation_constraints": [],
    "test_plan": ["unit test the streaming cap"],
    "risk_notes": [],
    "open_questions": [],
}


async def _first_attempt_creates_openspec(
    handler: MetadataActivityHandler, payload: dict[str, object]
) -> dict[str, Any]:
    prepared = await handler.handle("delivery.prepare_spec", payload)
    assert prepared["status"] == "SPEC_READY"
    return await handler.handle(
        "delivery.create_openspec",
        {
            **prepared,
            "status": "SUCCESS",
            "agent_result": {"structured_result": {**_READY_SPEC, "extra": "dropped"}},
        },
    )


class _ExplodingClaudeAdapter:
    provider = Provider.CLAUDE

    async def authenticated(self) -> bool:
        raise AssertionError("a reused spec must not reach the provider")

    async def execute(self, *_args: object, **_kwargs: object) -> AgentResult:
        raise AssertionError("a reused spec must not reach the provider")


@pytest.mark.asyncio
async def test_a_later_attempt_reuses_the_openspec_without_invoking_the_provider(
    tmp_path: Path,
) -> None:
    # NEW-53: every correction/resumed attempt re-ran the spec architect, whose
    # structured output fails ~13% of runs and burned whole attempts.
    handler, payload = _spec_fixture(tmp_path)
    first = await _first_attempt_creates_openspec(handler, payload)
    assert first["status"] == "OPENSPEC_READY"
    assert first["openspec_document"] == _READY_SPEC  # extra keys are projected away

    retry = {**payload, "orchestration": {"workflow_id": "backlog-loop-993-attempt-2"}}
    prepared = await handler.handle("delivery.prepare_spec", retry)
    assert prepared["status"] == "SPEC_REUSED"
    assert "spec_request" not in prepared

    started = await handler.handle(
        "agent.record_start", {**prepared, "provider_step": "delivery.invoke_spec"}
    )
    provider = ProviderActivityHandler(
        Settings(worktree_root=tmp_path / "worktrees", run_root=tmp_path / "runs"),
        "claude",
        _ExplodingClaudeAdapter(),
    )
    invoked = await provider.handle("delivery.invoke_spec", started)
    persisted = await handler.handle("agent.persist_result", invoked)
    created = await handler.handle("delivery.create_openspec", persisted)

    assert started == {**prepared, "provider_step": "delivery.invoke_spec"}
    assert invoked["status"] == persisted["status"] == "SPEC_REUSED"
    assert created["status"] == "OPENSPEC_READY"
    assert created["spec_reused"] is True
    assert created["spec_reused_from_workflow_id"] == "backlog-loop-musematic-issue-993-attempt-1"
    assert created["openspec_document"] == _READY_SPEC
    assert created["openspec_identifier"] == f"engineering/{payload['change_set_id']}"
    with handler.sessions() as session:
        assert session.scalars(select(AgentInvocation)).all() == []
        change_set = session.get(ChangeSet, UUID(str(payload["change_set_id"])))
        assert change_set is not None and change_set.state == "PLANNED"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["issue-edited", "setting-off"])
async def test_the_openspec_is_regenerated_when_the_issue_changed_or_reuse_is_off(
    tmp_path: Path, reason: str
) -> None:
    # NEW-53 / NEW-44: actual_behavior is refreshed from the live issue body, so an
    # edited issue must not reuse a spec generated from the old text.
    settings = {"delivery_reuse_spec": False} if reason == "setting-off" else {}
    handler, payload = _spec_fixture(tmp_path, **settings)
    await _first_attempt_creates_openspec(handler, payload)
    retry = {**payload, "orchestration": {"workflow_id": "backlog-loop-993-attempt-2"}}
    if reason == "issue-edited":
        retry["actual_behavior"] = "operator decision: cap at 100 MB"

    prepared = await handler.handle("delivery.prepare_spec", retry)

    assert prepared["status"] == "SPEC_READY"
    assert "spec_request" in prepared


@pytest.mark.parametrize("configured", [None, "claude-opus-4-9"])
def test_spec_and_checker_model_pins_reach_their_claude_requests(
    tmp_path: Path, configured: str | None
) -> None:
    # NEW-53: plumbing only; None keeps the CLI default.
    handler, payload = _spec_fixture(
        tmp_path, claude_spec_model=configured, claude_checker_model=configured
    )
    spec = handler._prepare_spec(payload)["spec_request"]
    checker = handler._prepare_checker(
        {
            **payload,
            "openspec_document": _READY_SPEC,
            "agent_result": {"structured_result": {"decision": "COMPLETE"}},
            "source_maker_result": {"structured_result": {"decision": "COMPLETE"}},
        }
    )["checker_request"]
    codex_spec = handler._prepare_spec({**payload, "spec_provider": "codex"})["spec_request"]

    assert spec["model"] == configured
    assert checker["model"] == configured
    assert codex_spec["model"] is None


def test_spec_schema_relaxations_keep_the_required_contract() -> None:
    # NEW-53: empty optional lists and extra keys validate; missing core content does not.
    validator = Draft202012Validator(MetadataActivityHandler._spec_output_schema())

    assert validator.is_valid({**_READY_SPEC, "summary": "extra key"})
    assert not validator.is_valid({**_READY_SPEC, "scope": []})
    assert not validator.is_valid({**_READY_SPEC, "decision": "MAYBE"})
    assert not validator.is_valid({k: v for k, v in _READY_SPEC.items() if k != "test_plan"})
