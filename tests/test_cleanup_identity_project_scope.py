"""NEW-119: one project's identity-recovery blocker withholds only that project's cleanup.

Workflow tests script every activity except the real ``cleanup.finalize``; the replay tests
run CleanupWorkflow on a local Temporal dev server (no product or network calls).
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import NotificationOutbox, Run, WorkerHeartbeat, WorktreeLease
from engineering_control.engineering.backlog import BACKLOG_PROJECT_PAIRS
from engineering_control.watchdog import OperationalWatchdog
from engineering_control.workflows.cleanup_scope import (
    CLEANUP_REPOSITORY_PROJECTS,
    identity_blocked_projects_from_summary,
    scope_identity_blockers,
)
from engineering_control.workflows.definitions import CleanupWorkflow
from engineering_control.workflows.inputs import CleanupInput
from engineering_control.workflows.worker_registry import (
    CONTROL_TASK_QUEUE,
    ROLE_TASK_QUEUES,
    ROLE_WORKER_REGISTRY,
)

MUSEMATIC_BLOCKED = {"musematic": "SUPERADMIN_AUTHENTICATION_FAILED"}
NEW_MARKER = "cleanup-identity-blocker-project-scope-v1"
ROOT = "/srv/engineering/worktrees"


def _worktree(repository: str, project: str, *, branch_project: str | None = None) -> dict:
    change_set = str(uuid4())
    return {
        "lease_id": str(uuid4()),
        "change_set_id": change_set,
        "repository": repository,
        "worktree_path": f"{ROOT}/{repository}/{change_set}",
        "branch": f"agent/{branch_project or project}/fix/{uuid4()}",
        "base_sha": "a" * 40,
        "reason": "RELEASED_RETENTION_EXPIRED",
        "inspection": "CLEAN",
    }


def _file(candidate_id: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "kind": "provider-temp",
        "worker": "codex",
        "run_id": str(uuid4()),
        "reason": "PROVIDER_TEMP_RETENTION_EXPIRED",
        "not_modified_after": "2026-09-20T00:00:00+00:00",
        "inspection": "ABSENT",
    }


def _preview(project: str) -> dict:
    return {
        "candidate_id": uuid4().hex * 2,
        "deployment_id": str(uuid4()),
        "project": project,
        "inspection": "ABSENT",
    }


class Fixture:
    """Candidates for every removal set, two projects plus platform and unattributable."""

    def __init__(
        self,
        blockers: dict[str, str],
        *,
        only: set[str] | None = None,
        recover_blockers: dict[str, str] | None = None,
        recover_status: str | None = None,
    ) -> None:
        self.blockers = blockers
        self.recover_blockers = recover_blockers or {}
        self.recover_status = recover_status
        self.order: list[str] = []
        wanted = only or {"falcone", "musematic", "platform", "unattributable"}
        self.worktrees: list[dict] = []
        self.components: list[dict] = []
        self.files: list[dict] = []
        self.file_projects: dict[str, str] = {}
        self.previews: list[dict] = []
        self.identities: dict[str, list[dict]] = {}
        if "falcone" in wanted:
            self.worktrees += [
                _worktree("falcone", "falcone"),
                _worktree("falcone-charts", "falcone"),
            ]
            self.components.append(self._component("falcone:charts:staging"))
            self._add_file("f" * 64, "falcone")
            self.previews.append(_preview("falcone"))
            self.identities["falcone"] = [{"kind": "user", "id": "falcone-user"}]
        if "musematic" in wanted:
            self.worktrees.append(_worktree("musematic-deploy", "musematic"))
            self.components.append(self._component("musematic:deploy:staging"))
            self._add_file("e" * 64, "musematic")
            self.previews.append(_preview("musematic"))
            self.identities["musematic"] = [{"kind": "role", "id": "musematic-role"}]
        if "platform" in wanted:
            self._add_file("d" * 64, "platform")
        if "unattributable" in wanted:
            # Repository says falcone, branch says musematic: attribution is inconsistent.
            self.worktrees.append(_worktree("falcone", "falcone", branch_project="musematic"))
            self.components.append(self._component("shared-lock"))
            self.files.append(_file("c" * 64))  # its owning Run's project is unknown
        self.calls: dict[str, dict] = {}

    @staticmethod
    def _component(key: str) -> dict:
        return {"lease_id": str(uuid4()), "component_key": key, "reason": "EXPIRED"}

    def _add_file(self, candidate_id: str, project: str) -> None:
        self.files.append(_file(candidate_id))
        self.file_projects[candidate_id] = project

    def step(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """The scripted activity result, mirroring the keys the real activities return."""
        self.calls[name] = payload
        self.order.append(name)
        result = dict(payload)
        if name == "cleanup.discover":
            result.update(
                cleanup_candidates=self.worktrees,
                component_lease_cleanup_candidates=self.components,
                file_cleanup_candidates=self.files,
                file_cleanup_candidate_projects=self.file_projects,
                preview_namespace_cleanup_candidates=self.previews,
            )
        elif name == "cleanup.protect_active":
            result.update(eligible_candidates=self.worktrees, protected_candidates=[])
        elif name == "cleanup.inspect_evidence_files":
            result["inspected_file_candidates"] = self.files
        elif name == "identity.inspect_stale":
            result.update(
                status="BLOCKED" if self.blockers else "PASS",
                stale_identity_candidates=self.identities,
                identity_recovery_blockers=self.blockers,
            )
        elif name == "cleanup.inspect_preview_namespaces":
            result.update(
                status="PREVIEW_CLEANUP_INSPECTED",
                inspected_preview_namespace_candidates=self.previews,
            )
        elif name == "cleanup.authorize":
            result.update(
                status="CLEANUP_AUTHORIZED",
                authorized_candidates=payload["eligible_candidates"],
                authorized_file_candidates=payload["inspected_file_candidates"],
                authorized_component_lease_candidates=payload["component_lease_cleanup_candidates"],
                authorized_preview_namespace_candidates=payload[
                    "inspected_preview_namespace_candidates"
                ],
                cleanup_authorization_sha256="0" * 64,
            )
        elif name == "cleanup.execute_evidence_files":
            result.update(
                removed_file_candidate_ids=[],
                absent_file_candidate_ids=[
                    value["candidate_id"] for value in payload["authorized_file_candidates"]
                ],
            )
        elif name == "cleanup.execute_preview_namespaces":
            result.update(
                removed_preview_namespace_candidate_ids=[],
                absent_preview_namespace_candidate_ids=[
                    value["candidate_id"]
                    for value in payload["authorized_preview_namespace_candidates"]
                ],
            )
        elif name == "identity.recover_stale":
            result.update(
                status=self.recover_status or ("BLOCKED" if self.recover_blockers else "PASS"),
                identity_recovery_blockers=self.recover_blockers,
                recovered_managed_identities={
                    project: [item["id"] for item in items]
                    for project, items in payload["stale_identity_candidates"].items()
                    if project not in self.recover_blockers
                },
            )
        elif name == "cleanup.execute":
            result.update(
                removed_lease_ids=[value["lease_id"] for value in payload["authorized_candidates"]],
                already_absent_lease_ids=[],
                protected_cleanup_candidates=[],
            )
        elif name == "cleanup.prune_orphan_worktrees":
            result.update(orphan_worktrees=[], orphan_worktrees_kept=[])
        elif name == "cleanup.execute_metadata":
            result.update(
                released_component_lease_ids=[
                    value["lease_id"] for value in payload["authorized_component_lease_candidates"]
                ],
                purged_artifact_ids=[],
                cleaned_preview_deployment_ids=[
                    value["deployment_id"]
                    for value in payload["authorized_preview_namespace_candidates"]
                ],
            )
        elif name == "cleanup.finalize":
            result["status"] = payload["terminal_status"]
        return result


def _handler(tmp_path: Path) -> tuple[MetadataActivityHandler, str, uuid.UUID]:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'fixture.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    identifier = uuid4()
    workflow_id = f"fixture-cleanup-{identifier}"
    with handler.sessions.begin() as session:
        session.add(
            Run(
                id=identifier,
                workflow_id=workflow_id,
                run_type="cleanup",
                status="RUNNING",
                project="platform",
                started_at=datetime.now(UTC),
            )
        )
    return handler, workflow_id, identifier


async def _run_scripted(
    tmp_path: Path, fixture: Fixture, *, dry_run: bool = False
) -> tuple[dict, MetadataActivityHandler, uuid.UUID]:
    handler, workflow_id, identifier = _handler(tmp_path)
    with handler.engine.begin() as connection:
        for worktree in fixture.worktrees:
            connection.execute(
                WorktreeLease.__table__.insert(),
                {
                    "id": uuid.UUID(worktree["lease_id"]),
                    "change_set_id": uuid.UUID(worktree["change_set_id"]),
                    "repository": worktree["repository"],
                    "worktree_path": worktree["worktree_path"],
                    "branch": worktree["branch"],
                    "owner_run_id": identifier,
                    "base_sha": worktree["base_sha"],
                    "expires_at": datetime.now(UTC),
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                },
            )

    async def step(name: str, payload: dict) -> dict:
        payload = {**payload, "orchestration": {"workflow_id": workflow_id}}
        if name == "cleanup.finalize":
            fixture.calls[name] = payload
            fixture.order.append(name)
            return await handler.handle(name, payload)
        return fixture.step(name, payload)

    instance = CleanupWorkflow()
    with (
        patch.object(instance, "_step", side_effect=step),
        patch("engineering_control.workflows.definitions.workflow.patched", return_value=True),
    ):
        result = await instance.run(CleanupInput(dry_run=dry_run))
    return result, handler, identifier


def _ids(values: list[dict], key: str) -> set[str]:
    return {str(value[key]) for value in values}


async def test_musematic_blocker_cleans_falcone_and_protects_musematic(tmp_path: Path) -> None:
    fixture = Fixture(MUSEMATIC_BLOCKED)
    result, handler, identifier = await _run_scripted(tmp_path, fixture)

    assert result["status"] == "PARTIAL"
    assert result["blocked_projects"] == MUSEMATIC_BLOCKED
    authorized = fixture.calls["cleanup.authorize"]
    falcone_worktrees = [w for w in fixture.worktrees[:2]]
    assert _ids(authorized["eligible_candidates"], "lease_id") == _ids(
        falcone_worktrees, "lease_id"
    )
    assert [c["component_key"] for c in authorized["component_lease_cleanup_candidates"]] == [
        "falcone:charts:staging"
    ]
    assert _ids(authorized["inspected_file_candidates"], "candidate_id") == {"f" * 64, "d" * 64}
    assert [p["project"] for p in authorized["inspected_preview_namespace_candidates"]] == [
        "falcone"
    ]
    # Recovery runs before the first removal on the scoped path.
    assert fixture.order.index("identity.recover_stale") < fixture.order.index(
        "cleanup.execute_evidence_files"
    )
    assert fixture.order.count("identity.recover_stale") == 1
    assert fixture.order.count("cleanup.authorize") == 1
    recover = fixture.calls["identity.recover_stale"]
    assert recover["stale_identity_candidates"] == {"falcone": fixture.identities["falcone"]}
    assert recover["cleanup_blocked_projects"] == MUSEMATIC_BLOCKED
    prune = fixture.calls["cleanup.prune_orphan_worktrees"]
    assert prune["cleanup_identity_blocked_repositories"] == ["musematic", "musematic-deploy"]

    protected = fixture.calls["cleanup.finalize"]["identity_blocked_candidates"]
    blocked = "IDENTITY_RECOVERY_BLOCKED:SUPERADMIN_AUTHENTICATION_FAILED"
    by_kind = {(value["kind"], value["project"]): value["protection"] for value in protected}
    assert by_kind == {
        ("worktree", "musematic"): blocked,
        ("component-lease", "musematic"): blocked,
        ("file", "musematic"): blocked,
        ("preview-namespace", "musematic"): blocked,
        ("identity", "musematic"): blocked,
        ("worktree", None): "IDENTITY_RECOVERY_BLOCKED:PROJECT_UNATTRIBUTABLE",
        ("component-lease", None): "IDENTITY_RECOVERY_BLOCKED:PROJECT_UNATTRIBUTABLE",
        ("file", None): "IDENTITY_RECOVERY_BLOCKED:PROJECT_UNATTRIBUTABLE",
    }
    worktree_protection = {
        value["lease_id"]: value["protection"]
        for value in fixture.calls["cleanup.finalize"]["protected_candidates"]
    }
    assert worktree_protection[fixture.worktrees[2]["lease_id"]] == blocked

    with handler.sessions() as session:
        run = session.get(Run, identifier)
        assert run is not None
        assert (run.status, run.error_code) == ("PARTIAL", "CLEANUP_IDENTITY_BLOCKED_PROJECTS")
        assert identity_blocked_projects_from_summary(run.summary) == MUSEMATIC_BLOCKED
        assert "8 candidates protected" in str(run.summary)
        released = {
            str(lease.id)
            for lease in session.scalars(select(WorktreeLease))
            if lease.released_at is not None
        }
        assert released == _ids(fixture.worktrees[:2], "lease_id")  # falcone only
        assert list(session.scalars(select(NotificationOutbox))) == []
    handler.engine.dispose()


async def test_a_partial_dry_run_reports_blocked_projects_without_mutation(
    tmp_path: Path,
) -> None:
    fixture = Fixture(MUSEMATIC_BLOCKED, only={"falcone", "musematic"})
    result, handler, _ = await _run_scripted(tmp_path, fixture, dry_run=True)

    assert result["status"] == "DRY_RUN"
    assert result["blocked_projects"] == MUSEMATIC_BLOCKED
    assert "cleanup.authorize" not in fixture.calls
    assert not any(".execute" in name for name in fixture.calls)
    handler.engine.dispose()


@pytest.mark.parametrize(
    ("blockers", "only"),
    [
        (
            {
                "falcone": "SUPERADMIN_AUTHENTICATION_FAILED",
                "musematic": "SUPERADMIN_TARGET_INVALID",
            },
            {"falcone", "musematic"},
        ),
        (MUSEMATIC_BLOCKED, {"musematic"}),
        ({"global": "SUPERADMIN_UNAVAILABLE"}, None),
        ({"musematic": "X", "unknown-project": "SUPERADMIN_UNAVAILABLE"}, None),
    ],
    ids=["every-project-blocked", "only-blocked-project-has-candidates", "global", "unknown"],
)
async def test_unscopable_blockers_still_block_everything(
    tmp_path: Path, blockers: dict[str, str], only: set[str] | None
) -> None:
    fixture = Fixture(blockers, only=only)
    result, handler, identifier = await _run_scripted(tmp_path, fixture)

    assert result["status"] == "BLOCKED"
    assert "cleanup.authorize" not in fixture.calls
    assert not any(".execute" in name or "recover" in name for name in fixture.calls)
    with handler.sessions() as session:
        run = session.get(Run, identifier)
        assert run is not None
        assert (run.status, run.error_code) == ("BLOCKED", "CLEANUP_IDENTITY_INSPECTION_BLOCKED")
        assert len(list(session.scalars(select(NotificationOutbox)))) == 1
    handler.engine.dispose()


async def test_a_recovery_time_blocker_withholds_that_project_before_any_removal(
    tmp_path: Path,
) -> None:
    falcone_failed = {"falcone": "SUPERADMIN_TARGET_INVALID"}
    fixture = Fixture(
        MUSEMATIC_BLOCKED,
        only={"falcone", "musematic", "platform"},
        recover_blockers=falcone_failed,
    )
    result, handler, identifier = await _run_scripted(tmp_path, fixture)

    assert result["status"] == "PARTIAL"
    assert result["blocked_projects"] == {**MUSEMATIC_BLOCKED, **falcone_failed}
    assert fixture.order.count("cleanup.authorize") == 2
    assert fixture.order.index("identity.recover_stale") < fixture.order.index(
        "cleanup.execute_evidence_files"
    )
    reauthorized = fixture.calls["cleanup.authorize"]
    assert reauthorized["eligible_candidates"] == []
    assert reauthorized["component_lease_cleanup_candidates"] == []
    assert _ids(reauthorized["inspected_file_candidates"], "candidate_id") == {"d" * 64}
    assert reauthorized["inspected_preview_namespace_candidates"] == []
    executed = fixture.calls["cleanup.execute"]
    assert executed["authorized_candidates"] == []
    protected = fixture.calls["cleanup.finalize"]["identity_blocked_candidates"]
    assert {(value["kind"], value["project"]) for value in protected} >= {
        ("worktree", "falcone"),
        ("worktree", "musematic"),
        ("file", "falcone"),
        ("preview-namespace", "musematic"),
    }
    with handler.sessions() as session:
        run = session.get(Run, identifier)
        assert run is not None
        assert (run.status, run.error_code) == ("PARTIAL", "CLEANUP_IDENTITY_BLOCKED_PROJECTS")
        assert identity_blocked_projects_from_summary(run.summary) == {
            **MUSEMATIC_BLOCKED,
            **falcone_failed,
        }
        assert all(lease.released_at is None for lease in session.scalars(select(WorktreeLease)))
    handler.engine.dispose()


@pytest.mark.parametrize(
    ("only", "recover_blockers", "recover_status"),
    [
        ({"falcone", "musematic", "platform"}, {"global": "SUPERADMIN_UNAVAILABLE"}, None),
        ({"falcone", "musematic", "platform"}, {}, "BLOCKED"),
        ({"falcone", "musematic", "platform"}, {}, "FAILED"),
        ({"falcone", "musematic"}, {"falcone": "SUPERADMIN_TARGET_INVALID"}, None),
    ],
    ids=["unattributable", "blocked-without-blocker", "failed", "every-remaining-project"],
)
async def test_an_unscopable_recovery_failure_blocks_before_any_removal(
    tmp_path: Path,
    only: set[str],
    recover_blockers: dict[str, str],
    recover_status: str | None,
) -> None:
    fixture = Fixture(
        MUSEMATIC_BLOCKED,
        only=only,
        recover_blockers=recover_blockers,
        recover_status=recover_status,
    )
    result, handler, identifier = await _run_scripted(tmp_path, fixture)

    assert result["status"] == "BLOCKED"
    assert not any(".execute" in name for name in fixture.order)
    assert fixture.order[-1] == "cleanup.finalize"
    with handler.sessions() as session:
        run = session.get(Run, identifier)
        assert run is not None
        assert (run.status, run.error_code) == ("BLOCKED", "CLEANUP_IDENTITY_RECOVERY_BLOCKED")
    handler.engine.dispose()


def test_pruning_excludes_every_leased_and_protected_path(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from engineering_control.activities.scan import ScanActivityHandler

    handler = ScanActivityHandler(
        Settings(worktree_root=tmp_path / "worktrees", cleanup_execution_enabled=True)
    )
    handler.manager = MagicMock()
    handler.manager.prune_orphan_worktrees.return_value = {"removed": [], "kept": []}
    # The NEW-119 review case: an old detached falcone checkout whose lease branch names
    # musematic, kept by inspection as BRANCH_IDENTITY_MISMATCH while musematic is blocked.
    mismatched = _worktree("falcone", "falcone", branch_project="musematic")
    eligible = _worktree("falcone-charts", "falcone")
    handler._prune_orphan_worktrees(
        {
            "dry_run": False,
            "leased_worktree_paths": [f"{ROOT}/llmwiki/leased"],
            "cleanup_candidates": [mismatched, eligible],
            "eligible_candidates": [eligible],
            "protected_candidates": [{**mismatched, "protection": "BRANCH_IDENTITY_MISMATCH"}],
            "cleanup_identity_blocked_repositories": ["musematic", "musematic-deploy"],
        }
    )

    kwargs = handler.manager.prune_orphan_worktrees.call_args.kwargs
    assert kwargs["excluded_repositories"] == frozenset({"musematic", "musematic-deploy"})
    assert kwargs["excluded_paths"] == frozenset(
        {f"{ROOT}/llmwiki/leased", mismatched["worktree_path"], eligible["worktree_path"]}
    )
    with pytest.raises(ValueError, match="lacks its path"):
        handler._prune_orphan_worktrees({"protected_candidates": [{"lease_id": "x"}]})


def test_an_undeterminable_candidate_is_protected_not_removed() -> None:
    worktree = _worktree("falcone", "falcone")
    escaped = {**_worktree("falcone", "falcone"), "worktree_path": f"{ROOT}/musematic/x"}
    mislabeled = _worktree("llmwiki", "llmwiki", branch_project="falcone")
    context = {
        "identity_recovery_blockers": MUSEMATIC_BLOCKED,
        "eligible_candidates": [worktree, escaped, mislabeled],
        "protected_candidates": [{"lease_id": "earlier", "protection": "DIRTY_WORKTREE"}],
        "component_lease_cleanup_candidates": [],
        "inspected_file_candidates": [_file("1" * 64), _file("2" * 64)],
        "file_cleanup_candidate_projects": {"1" * 64: "falcone", "2" * 64: "somewhere-else"},
        "inspected_preview_namespace_candidates": [{**_preview("falcone"), "project": None}],
        "stale_identity_candidates": {"falcone": [], "mystery": [{"kind": "user", "id": "u"}]},
    }

    scope = scope_identity_blockers(context)

    assert scope.blocked_all is False
    assert scope.context["eligible_candidates"] == [worktree]
    assert _ids(scope.context["inspected_file_candidates"], "candidate_id") == {"1" * 64}
    assert scope.context["inspected_preview_namespace_candidates"] == []
    assert scope.context["stale_identity_candidates"] == {"falcone": []}
    assert {value["protection"] for value in scope.protected} == {
        "IDENTITY_RECOVERY_BLOCKED:PROJECT_UNATTRIBUTABLE"
    }
    assert sorted(value["kind"] for value in scope.protected) == [
        "file",
        "identity",
        "preview-namespace",
        "worktree",
        "worktree",
    ]
    assert scope.context["protected_candidates"][0]["lease_id"] == "earlier"
    assert len(scope.context["protected_candidates"]) == 3
    assert context["eligible_candidates"] == [worktree, escaped, mislabeled]  # input unchanged


def test_the_repository_mapping_matches_the_backlog_project_pairs() -> None:
    expected = {
        repository.removeprefix("gntik-ai/"): project
        for project, repositories in BACKLOG_PROJECT_PAIRS.items()
        for repository in repositories
    }
    assert CLEANUP_REPOSITORY_PROJECTS == expected


# --- cleanup.finalize authorization -----------------------------------------------------


def _finalize_payload(workflow_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "orchestration": {"workflow_id": workflow_id},
        "terminal_status": "PARTIAL",
        "authorized_candidates": [{"lease_id": "kept-by-new-105"}],
        "authorized_file_candidates": [],
        "authorized_component_lease_candidates": [],
        "authorized_preview_namespace_candidates": [],
        "removed_lease_ids": [],
        "already_absent_lease_ids": [],
        "protected_cleanup_candidates": [{"lease_id": "kept-by-new-105", "reason": "maker-owned"}],
        "removed_file_candidate_ids": [],
        "absent_file_candidate_ids": [],
        "released_component_lease_ids": [],
        "purged_artifact_ids": [],
        "removed_preview_namespace_candidate_ids": [],
        "absent_preview_namespace_candidate_ids": [],
        "cleaned_preview_deployment_ids": [],
        "recovered_managed_identities": {"falcone": []},
        "cleanup_blocked_projects": MUSEMATIC_BLOCKED,
        "identity_blocked_candidates": [
            {
                "kind": "worktree",
                "candidate_id": "musematic-lease",
                "project": "musematic",
                "protection": "IDENTITY_RECOVERY_BLOCKED:SUPERADMIN_AUTHENTICATION_FAILED",
            },
            {
                "kind": "file",
                "candidate_id": "c" * 64,
                "project": None,
                "protection": "IDENTITY_RECOVERY_BLOCKED:PROJECT_UNATTRIBUTABLE",
            },
        ],
    }
    payload.update(overrides)
    return payload


async def test_finalize_accepts_partial_with_identity_and_new_105_protection(
    tmp_path: Path,
) -> None:
    handler, workflow_id, identifier = _handler(tmp_path)

    result = await handler.handle("cleanup.finalize", _finalize_payload(workflow_id))

    assert result["status"] == "PARTIAL"
    with handler.sessions() as session:
        run = session.get(Run, identifier)
        assert run is not None and run.finished_at is not None
        assert (run.status, run.error_code) == ("PARTIAL", "CLEANUP_IDENTITY_BLOCKED_PROJECTS")
        assert "1 protected worktrees retained for the operator" in str(run.summary)
        assert identity_blocked_projects_from_summary(run.summary) == MUSEMATIC_BLOCKED
    handler.engine.dispose()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "authorized_file_candidates": [{"candidate_id": "c" * 64}],
                "absent_file_candidate_ids": ["c" * 64],
            },
            "identity-blocked cleanup candidate was authorized",
        ),
        (
            {"protected_cleanup_candidates": []},  # the NEW-105 equality is not weakened
            "cleanup completion does not match its authorization",
        ),
        ({"cleanup_blocked_projects": {}}, "attributable identity-blocked projects"),
        (
            {"cleanup_blocked_projects": {"global": "SUPERADMIN_UNAVAILABLE"}},
            "attributable identity-blocked projects",
        ),
        (
            {"recovered_managed_identities": {"musematic": ["musematic-role"]}},
            "identity-blocked project's identities were recovered",
        ),
        (
            {
                "identity_blocked_candidates": [
                    {
                        "kind": "worktree",
                        "candidate_id": "x",
                        "project": "falcone",
                        "protection": "IDENTITY_RECOVERY_BLOCKED:SUPERADMIN_AUTHENTICATION_FAILED",
                    }
                ]
            },
            "names no blocked project",
        ),
        (
            {
                "identity_blocked_candidates": [
                    {
                        "kind": "worktree",
                        "candidate_id": "x",
                        "project": "musematic",
                        "protection": "DIRTY_WORKTREE",
                    }
                ]
            },
            "identity-blocked cleanup evidence is invalid",
        ),
        (
            {"terminal_status": "COMPLETE"},
            "a cleanup with identity-blocked projects is not complete",
        ),
    ],
    ids=[
        "blocked-candidate-removed",
        "new-105-mismatch",
        "no-blocked-projects",
        "unattributable-blocked-project",
        "blocked-identities-recovered",
        "candidate-for-unblocked-project",
        "foreign-protection",
        "complete-with-blocked-projects",
    ],
)
async def test_finalize_rejects_inconsistent_partial_evidence(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    handler, workflow_id, identifier = _handler(tmp_path)

    with pytest.raises(ApplicationError) as raised:
        await handler.handle("cleanup.finalize", _finalize_payload(workflow_id, **overrides))
    assert message in str(raised.value.__cause__)

    with handler.sessions() as session:
        run = session.get(Run, identifier)
        assert run is not None and run.finished_at is None and run.status == "RUNNING"
    handler.engine.dispose()


# --- watchdog ----------------------------------------------------------------------------


def _watchdog(tmp_path: Path, now: datetime) -> OperationalWatchdog:
    class Sink:
        def send(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'watchdog.sqlite'}",
            watchdog_state_file=tmp_path / "state.json",
        ),
        sink=Sink(),  # type: ignore[arg-type]
        probe_provider=lambda: [],
        now=lambda: now,
    )
    Base.metadata.create_all(watchdog.engine)
    with watchdog.engine.begin() as connection:
        connection.execute(
            WorkerHeartbeat.__table__.insert(),
            [
                {
                    "id": uuid4(),
                    "identity": f"{kind}-fixture",
                    "hostname": "fixture-worker",
                    "worker_kind": kind,
                    "observed_at": now,
                    "created_at": now,
                    "updated_at": now,
                }
                for kind in ROLE_WORKER_REGISTRY
            ],
        )
    return watchdog


def _cleanup_run(
    watchdog: OperationalWatchdog,
    now: datetime,
    *,
    status: str,
    error_code: str | None = None,
    summary: str | None = None,
    age: timedelta = timedelta(minutes=1),
    running: bool = False,
) -> None:
    finished = None if running else now - age
    with watchdog.engine.begin() as connection:
        connection.execute(
            Run.__table__.insert(),
            {
                "id": uuid4(),
                "workflow_id": f"cleanup-{uuid4()}",
                "run_type": "cleanup",
                "status": status,
                "project": "platform",
                "started_at": now - age - timedelta(minutes=1),
                "finished_at": finished,
                "error_code": error_code,
                "summary": summary,
                "created_at": now - age - timedelta(minutes=1),
                "updated_at": now if running else now - age,
            },
        )


PARTIAL_SUMMARY = (
    "retained cleanup completed: 37 worktrees, 4 bounded files, 0 component leases, "
    "0 preview namespaces; identity-blocked projects retained: "
    "musematic=SUPERADMIN_AUTHENTICATION_FAILED (3 candidates protected)"
)


def _probe(watchdog: OperationalWatchdog, now: datetime) -> Any:
    return {probe.key: probe for probe in watchdog._database(now)}["cleanup-schedule"]


def test_watchdog_treats_identity_partial_as_fresh_and_reports_blocked_projects(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 10, 4, 3, 0, tzinfo=UTC)
    watchdog = _watchdog(tmp_path, now)
    _cleanup_run(
        watchdog,
        now,
        status="PARTIAL",
        error_code="CLEANUP_IDENTITY_BLOCKED_PROJECTS",
        summary=PARTIAL_SUMMARY,
    )

    fresh = _probe(watchdog, now)
    assert (fresh.healthy, fresh.status) == (True, "CURRENT_OR_RUNNING")
    assert fresh.summary.endswith(
        "; identity-blocked projects retained: musematic=SUPERADMIN_AUTHENTICATION_FAILED"
    )
    assert fresh.fingerprint is None  # a stable healthy probe raises no daily event

    # A later run in progress keeps the PARTIAL run as its recent successful baseline.
    _cleanup_run(watchdog, now, status="RUNNING", running=True)
    running = _probe(watchdog, now)
    assert running.healthy is True
    watchdog.engine.dispose()


@pytest.mark.parametrize(
    ("status", "error_code", "summary", "age"),
    [
        ("BLOCKED", "CLEANUP_IDENTITY_INSPECTION_BLOCKED", None, timedelta(minutes=1)),
        ("FAILED", None, None, timedelta(minutes=1)),
        ("PARTIAL", "SOMETHING_ELSE", PARTIAL_SUMMARY, timedelta(minutes=1)),
        ("PARTIAL", "CLEANUP_IDENTITY_BLOCKED_PROJECTS", "no parseable projects", timedelta(0)),
        ("PARTIAL", "CLEANUP_IDENTITY_BLOCKED_PROJECTS", PARTIAL_SUMMARY, timedelta(days=3)),
    ],
    ids=["blocked", "failed", "other-partial", "unparseable-partial", "stale-partial"],
)
def test_watchdog_still_alerts_on_blocked_failed_or_stale_cleanup(
    tmp_path: Path, status: str, error_code: str | None, summary: str | None, age: timedelta
) -> None:
    now = datetime(2026, 10, 4, 3, 0, tzinfo=UTC)
    watchdog = _watchdog(tmp_path, now)
    _cleanup_run(watchdog, now, status=status, error_code=error_code, summary=summary, age=age)

    probe = _probe(watchdog, now)
    assert (probe.healthy, probe.status) == (False, "FAILED_OR_MISSED")
    assert probe.summary == (
        "cleanup failed, lacks a recent success, or exceeded its freshness bound"
    )
    watchdog.engine.dispose()


def test_watchdog_does_not_take_an_unparseable_partial_as_a_running_baseline(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 10, 4, 3, 0, tzinfo=UTC)
    watchdog = _watchdog(tmp_path, now)
    _cleanup_run(
        watchdog,
        now,
        status="PARTIAL",
        error_code="CLEANUP_IDENTITY_BLOCKED_PROJECTS",
        summary="tampered",
        age=timedelta(minutes=5),
    )
    _cleanup_run(watchdog, now, status="RUNNING", running=True)

    assert _probe(watchdog, now).healthy is False
    watchdog.engine.dispose()


# --- Temporal replay -----------------------------------------------------------------------

_TEMPORAL_CLI = shutil.which("temporal")
requires_temporal = pytest.mark.skipif(
    _TEMPORAL_CLI is None, reason="requires the `temporal` CLI on PATH to run a local dev server"
)


@workflow.defn(name="CleanupWorkflow", sandboxed=False)
class _PreScopeCleanupWorkflow(CleanupWorkflow):
    """CleanupWorkflow exactly as it ran under cleanup-inspection-outcome-v1 alone."""

    @workflow.run
    async def run(self, request: CleanupInput) -> dict[str, Any]:
        return await super().run(request)

    def _identity_project_scope_patched(self) -> bool:
        return False


async def _run_to_history(workflow_cls: type, fixture: Fixture) -> tuple[Any, dict]:
    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        payload = {**request["payload"], "orchestration": {"workflow_id": "fixture"}}
        return fixture.step(request["step"], payload)

    queues = sorted({CONTROL_TASK_QUEUE, *ROLE_TASK_QUEUES.values()})
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI) as env:
        workers = [
            Worker(
                env.client,
                task_queue=queue,
                workflows=[workflow_cls] if queue == CONTROL_TASK_QUEUE else [],
                activities=[execute_control_step],
            )
            for queue in queues
        ]
        for worker in workers:
            await worker.__aenter__()
        try:
            handle = await env.client.start_workflow(
                workflow_cls.run,
                CleanupInput(dry_run=False),
                id=f"replay-test-cleanup-{uuid4()}",
                task_queue=CONTROL_TASK_QUEUE,
                execution_timeout=timedelta(minutes=1),
            )
            result = await handle.result()
            history = await handle.fetch_history()
        finally:
            for worker in reversed(workers):
                await worker.__aexit__(None, None, None)
    return history, result


def _steps(history: Any) -> list[str]:
    return [
        json.loads(event.activity_task_scheduled_event_attributes.input.payloads[0].data)["step"]
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]


def _has_marker(history: Any, name: str) -> bool:
    return any(
        name.encode() in event.marker_recorded_event_attributes.SerializeToString()
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
    )


@requires_temporal
async def test_new_history_is_partial_and_replays() -> None:
    history, result = await _run_to_history(CleanupWorkflow, Fixture(MUSEMATIC_BLOCKED))

    assert result["status"] == "PARTIAL"
    assert result["blocked_projects"] == MUSEMATIC_BLOCKED
    steps = _steps(history)
    assert "cleanup.authorize" in steps and "cleanup.execute" in steps
    assert steps[-1] == "cleanup.finalize"
    assert _has_marker(history, NEW_MARKER)
    await Replayer(workflows=[CleanupWorkflow]).replay_workflow(history)


@requires_temporal
async def test_an_inspection_outcome_v1_history_still_blocks_and_replays_unchanged() -> None:
    history, result = await _run_to_history(_PreScopeCleanupWorkflow, Fixture(MUSEMATIC_BLOCKED))

    assert result["status"] == "BLOCKED"
    steps = _steps(history)
    assert "cleanup.authorize" not in steps
    assert not any(".execute" in step for step in steps)
    assert steps[-2:] == ["cleanup.inspect_preview_namespaces", "cleanup.finalize"]
    assert _has_marker(history, "cleanup-inspection-outcome-v1")
    assert not _has_marker(history, NEW_MARKER)
    await Replayer(workflows=[CleanupWorkflow]).replay_workflow(history)


# --- activities honouring the withheld projects ------------------------------------------


@pytest.mark.parametrize("step", ["identity.recover_stale", "identity.inspect_stale"])
async def test_recovery_never_logs_into_a_withheld_project(tmp_path: Path, step: str) -> None:
    from engineering_control.activities.browser import BrowserActivityHandler

    loaded: list[str] = []

    class Loader:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def load(self, project: str) -> Any:
            loaded.append(project)
            raise ApplicationError("fixture", type="SUPERADMIN_AUTHENTICATION_FAILED")

    handler = BrowserActivityHandler(
        Settings(browser_credential_root=tmp_path, browser_evidence_root=tmp_path / "evidence")
    )
    with patch("engineering_control.activities.browser.SuperadminCredentialLoader", Loader):
        result = await handler._stale_identity_operation(
            step,
            {
                "dry_run": False,
                "stale_identity_candidates": {"falcone": []},
                "cleanup_blocked_projects": MUSEMATIC_BLOCKED,
            },
        )

    expected = ["falcone"] if step == "identity.recover_stale" else ["falcone", "musematic"]
    assert loaded == expected
    assert sorted(result["identity_recovery_blockers"]) == expected
