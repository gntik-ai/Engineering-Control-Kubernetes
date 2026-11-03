"""NEW-245: cleanup lists are stored by the controller and paged through bounded activities.

The workflow runs with every activity routed to the real controller, scan and codex
retention handlers (sqlite and a temporary filesystem); only identity and preview
inspection are scripted. The paged run is compared with the legacy (unpaged) run of the
same fixture, and every activity's serialized input and output is measured.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import delete, func, insert, select
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from engineering_control.activities.core import ControlActivities
from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.activities.retention import RetentionActivityHandler
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import (
    AgentInvocation,
    ChangeSet,
    CleanupRunItem,
    Run,
    WorktreeLease,
)
from engineering_control.workflows.cleanup_paging import CLEANUP_PAGED_PATCH, CLEANUP_PHASES
from engineering_control.workflows.definitions import CleanupWorkflow
from engineering_control.workflows.inputs import CleanupInput
from engineering_control.workflows.worker_registry import CONTROL_TASK_QUEUE, ROLE_TASK_QUEUES

OLD = datetime.now(UTC) - timedelta(days=30)
MUSEMATIC_BLOCKED = {"musematic": "SUPERADMIN_AUTHENTICATION_FAILED"}
LIMIT = 512 * 1024
_SCAN_STEPS = {
    "cleanup.protect_active",
    "cleanup.execute",
    "cleanup.prune_orphan_worktrees",
    "cleanup.list_orphan_worktrees",
    "cleanup.apply_orphan_worktrees",
    "cleanup.resolve_worktree_paths",
}


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"new-245-fixture:{name}")


def _settings(root: Path, name: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": f"sqlite:///{root / name}",
        "worktree_root": root / "worktrees",
        "worktree_metadata_root": root / "worktree-git",
        "mirror_root": root / "mirrors",
        "lease_root": root / "leases",
        "temporary_root": root / "tmp",
        "run_root": root / "runs",
        "evidence_root": root / "evidence",
        "browser_evidence_root": root / "evidence" / "browser",
        "cleanup_execution_enabled": True,
        "cleanup_retention_days": 14,
        "cleanup_page_size": 3,
    }
    values.update(overrides)
    return Settings(**values)


def _change_set(change_set_id: UUID, project: str, repository: str, state: str) -> dict[str, Any]:
    return {
        "id": change_set_id,
        "finding_id": _id(f"finding:{change_set_id}"),
        "issue_id": None,
        "project": project,
        "risk_class": "R1",
        "state": state,
        "version": 1,
        "source_repository": repository,
        "source_branch": f"agent/{project}/1/{change_set_id}",
        "source_base_sha": "a" * 40,
        "source_head_sha": "b" * 40,
        "source_pr_url": None,
        "deployment_repository": repository,
        "deployment_branch": f"agent/{project}/2/{change_set_id}",
        "deployment_base_sha": "c" * 40,
        "deployment_head_sha": "d" * 40,
        "image_digests": {},
        "chart_version": None,
        "recovery_json": {},
    }


class Seed:
    """Rows (and directories) identical for the legacy and the paged run."""

    def __init__(self) -> None:
        self.change_sets: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.leases: list[dict[str, Any]] = []
        self.invocations: list[dict[str, Any]] = []
        self.directories: list[tuple[str, bool]] = []  # (relative path, recent)
        self.aliases: list[tuple[str, str]] = []  # (relative symlink, relative target)

    def lease(
        self,
        name: str,
        *,
        project: str = "falcone",
        repository: str = "falcone",
        state: str = "ISSUE_CLOSED",
        released: bool = True,
        owner_status: str = "SUCCESS",
        directory: bool = False,
    ) -> None:
        change_set_id, owner, lease = _id(f"cs:{name}"), _id(f"owner:{name}"), _id(f"lease:{name}")
        self.change_sets.append(_change_set(change_set_id, project, repository, state))
        self.runs.append(
            {
                "id": owner,
                "change_set_id": change_set_id,
                "workflow_id": f"delivery-{name}",
                "run_type": "issue-delivery",
                "status": owner_status,
                "project": project,
                "started_at": OLD,
                "finished_at": OLD,
            }
        )
        future = datetime.now(UTC) + timedelta(days=1)
        self.leases.append(
            {
                "id": lease,
                "change_set_id": change_set_id,
                "repository": repository,
                "worktree_path": f"@ROOT@/{repository}/{change_set_id}",
                "branch": f"agent/{project}/1/{change_set_id}",
                "owner_run_id": owner,
                "base_sha": "a" * 40,
                "expires_at": OLD if released else future,
                "released_at": OLD if released else None,
            }
        )
        if directory:
            self.directories.append((f"worktrees/{repository}/{change_set_id}", False))

    def codex_run(self, name: str, *, project: str = "falcone", files: str = "absent") -> None:
        run_id = _id(f"codex:{name}")
        self.runs.append(
            {
                "id": run_id,
                "campaign_id": None,
                "change_set_id": None,
                "workflow_id": f"provider-run-{name}",
                "run_type": "implementation",
                "status": "RATE_LIMITED" if files == "rate-limited" else "SUCCESS",
                "project": project,
                "worker": "codex-runner",
                "started_at": OLD,
                "finished_at": OLD,
                "summary": "bounded provider run",
            }
        )
        self.invocations.append(
            {
                "id": _id(f"invocation:{name}"),
                "run_id": run_id,
                "provider": "codex",
                "role": "implementer",
                "model_class": "worker",
                "provider_session_id": None,
                "status": "SUCCESS",
                "summary": "bounded provider invocation",
                "structured_result": {},
                "error_code": None,
                "started_at": OLD,
                "finished_at": OLD,
            }
        )
        if files in {"old", "recent"}:
            self.directories.append((f"tmp/codex/{run_id}", files == "recent"))

    def orphan(self, repository: str, name: str) -> None:
        self.directories.append((f"worktrees/{repository}/{_id(f'orphan:{name}')}", False))

    def install(self, root: Path, handler: MetadataActivityHandler) -> None:
        worktrees = str((root / "worktrees").resolve())
        leases = [
            {**row, "worktree_path": row["worktree_path"].replace("@ROOT@", worktrees)}
            for row in self.leases
        ]
        with handler.sessions.begin() as session:
            for model, rows in (
                (ChangeSet, self.change_sets),
                (Run, self.runs),
                (WorktreeLease, leases),
                (AgentInvocation, self.invocations),
            ):
                if rows:
                    session.execute(insert(model), rows)
        old = OLD.timestamp()
        for relative, recent in self.directories:
            path = root / relative
            path.mkdir(parents=True, exist_ok=True)
            if relative.startswith("tmp/"):
                (path / "out.log").write_text("x\n", encoding="utf-8")
                if not recent:
                    os.utime(path / "out.log", (old, old))
            if not recent:
                os.utime(path, (old, old))
        for link, target in self.aliases:
            (root / link).symlink_to(root / target, target_is_directory=True)
        for directory in ("tmp/codex", "worktrees", "runs", "evidence/browser"):
            (root / directory).mkdir(parents=True, exist_ok=True)


class Harness:
    """Route every cleanup activity to the real handler; script identity and preview."""

    def __init__(self, root: Path, settings: Settings, blockers: dict[str, str]) -> None:
        self.root = root
        self.workflow_id = f"cleanup-new-245-{uuid4()}"
        self.workflow_run_id = str(uuid4())
        self.metadata = MetadataActivityHandler(settings)
        Base.metadata.create_all(self.metadata.engine)
        self.scan = ScanActivityHandler(settings)
        self.retention = {
            kind: RetentionActivityHandler(settings, kind)
            for kind in ("controller", "delivery", "browser", "codex")
        }
        self.blockers = blockers
        self.sizes: list[tuple[str, int, int]] = []
        self.page_lengths: dict[str, int] = {}
        self.effects: dict[str, set[str]] = {}
        self.order: list[str] = []

    def _effect(self, key: str, values: Any) -> None:
        self.effects.setdefault(key, set()).update(values)

    def _scripted(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if name == "identity.inspect_stale":
            return {
                **payload,
                "status": "BLOCKED" if self.blockers else "PASS",
                "stale_identity_candidates": {"falcone": [{"kind": "user", "id": "u1"}]},
                "identity_recovery_blockers": dict(self.blockers),
            }
        if name == "identity.recover_stale":
            return {
                **payload,
                "status": "PASS",
                "identity_recovery_blockers": {},
                "recovered_managed_identities": {"falcone": ["u1"]},
            }
        if name == "cleanup.inspect_preview_namespaces":
            existing = list(payload.get("inspected_preview_namespace_candidates", []))
            return {
                **payload,
                "inspected_preview_namespace_candidates": existing
                + [
                    {**value, "inspection": "ABSENT"}
                    for value in payload["preview_namespace_cleanup_candidates"]
                ],
            }
        if name == "cleanup.execute_preview_namespaces":
            return {
                **payload,
                "removed_preview_namespace_candidate_ids": [],
                "absent_preview_namespace_candidate_ids": [
                    value["candidate_id"]
                    for value in payload["authorized_preview_namespace_candidates"]
                ],
            }
        raise AssertionError(f"unrouted cleanup step {name}")

    async def step(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = {
            **payload,
            "orchestration": {
                "workflow_id": self.workflow_id,
                "workflow_run_id": self.workflow_run_id,
            },
        }
        self.order.append(name)
        if name in self.metadata.supported_steps:
            result = await self.metadata.handle(name, payload)
        elif name in _SCAN_STEPS:
            result = await self.scan.handle(name, payload)
        elif name.startswith(("cleanup.inspect_", "cleanup.execute_")) and name.endswith("_files"):
            kind = next(k for k, h in self.retention.items() if name in h.supported_steps)
            result = await self.retention[kind].handle(name, payload)
        else:
            result = self._scripted(name, payload)
        self.sizes.append((name, len(json.dumps(payload)), len(json.dumps(result))))
        if not name.startswith(("cleanup.page", "cleanup.stored", "cleanup.discover")):
            longest = max((len(v) for v in payload.values() if isinstance(v, list)), default=0)
            self.page_lengths[name] = max(self.page_lengths.get(name, 0), longest)
        if name == "cleanup.execute":
            self._effect(
                "authorized_worktrees", (v["lease_id"] for v in payload["authorized_candidates"])
            )
            self._effect("removed_worktrees", result["removed_lease_ids"])
            self._effect("absent_worktrees", result["already_absent_lease_ids"])
            self._effect(
                "protected_worktrees",
                (v["lease_id"] for v in result["protected_cleanup_candidates"]),
            )
        if name == "cleanup.execute_codex_files":
            self._effect(
                "authorized_codex_files",
                (
                    v["candidate_id"]
                    for v in payload["authorized_file_candidates"]
                    if v["worker"] == "codex"
                ),
            )
            self._effect("removed_files", result["removed_file_candidate_ids"])
            self._effect("absent_files", result["absent_file_candidate_ids"])
        if name in {"cleanup.prune_orphan_worktrees", "cleanup.apply_orphan_worktrees"}:
            self._effect("orphans_removed", (v["path"] for v in result["orphan_worktrees"]))
            self._effect("orphans_kept", (v["path"] for v in result["orphan_worktrees_kept"]))
        if name == "cleanup.protect_active":
            self._effect("eligible", (v["lease_id"] for v in result["eligible_candidates"]))
            self._effect("protected", (v["lease_id"] for v in result["protected_candidates"]))
        return result

    async def run(self, *, paged: bool, dry_run: bool = False, **kwargs: Any) -> dict[str, Any]:
        instance = CleanupWorkflow()
        with (
            patch.object(instance, "_step", side_effect=self.step),
            patch.object(instance, "_paged_cleanup_patched", return_value=paged),
            patch.object(instance, "_paged_cleanup_history_long", return_value=False),
            patch("engineering_control.workflows.definitions.workflow.patched", return_value=True),
        ):
            return await instance.run(CleanupInput(dry_run=dry_run, **kwargs))

    def cleanup_run(self) -> Run:
        with self.metadata.sessions() as session:
            run = session.scalar(select(Run).where(Run.workflow_id == self.workflow_id))
            assert run is not None
            return run

    def stored_rows(self) -> int:
        with self.metadata.sessions() as session:
            return int(session.scalar(select(func.count(CleanupRunItem.id))) or 0)


def _mixed_seed() -> Seed:
    seed = Seed()
    for index in range(7):  # more than two pages of three
        seed.lease(f"falcone-eligible-{index}")
    seed.lease("falcone-on-disk-unproven", directory=True)  # protect_active protects it
    seed.lease("falcone-active", released=False, directory=True)
    seed.lease("falcone-published", state="SOURCE_PR", directory=True)
    seed.lease("falcone-rate-limited-owner", owner_status="RATE_LIMITED")
    for index in range(2):
        seed.lease(
            f"musematic-eligible-{index}",
            project="musematic",
            repository="musematic-deploy",
            directory=index == 0,
        )
    for index in range(4):
        seed.codex_run(f"falcone-old-{index}", files="old")
    seed.codex_run("falcone-absent")
    seed.codex_run("falcone-recent", files="recent")  # MODIFIED_AFTER_CUTOFF: protected
    seed.codex_run("falcone-rate-limited", files="rate-limited")
    seed.codex_run("musematic-old", project="musematic", files="old")
    for index in range(4):
        seed.orphan("falcone", f"falcone-{index}")
    seed.orphan("musematic-deploy", "musematic")  # identity-blocked repository
    return seed


async def _run_fixture(
    root: Path, seed: Seed, *, paged: bool, blockers: dict[str, str], dry_run: bool = False
) -> tuple[Harness, dict[str, Any]]:
    _mkdir(root)
    settings = _settings(root, "cleanup.sqlite")
    harness = Harness(root, settings, blockers)
    seed.install(root, harness.metadata)
    result = await harness.run(paged=paged, dry_run=dry_run)
    return harness, result


def _is_dir(path: str) -> bool:
    return Path(path).is_dir()


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _relative(root: Path, paths: set[str]) -> set[str]:
    return {str(Path(path).relative_to(root.resolve())) for path in paths}


@pytest.mark.parametrize(
    ("blockers", "dry_run"),
    [({}, False), (MUSEMATIC_BLOCKED, False), (MUSEMATIC_BLOCKED, True)],
    ids=["complete", "identity-blocked", "dry-run"],
)
async def test_paged_cleanup_removes_and_protects_exactly_what_the_legacy_run_does(
    tmp_path: Path, blockers: dict[str, str], dry_run: bool
) -> None:
    seed = _mixed_seed()
    legacy, legacy_result = await _run_fixture(
        tmp_path / "legacy", seed, paged=False, blockers=blockers, dry_run=dry_run
    )
    paged, paged_result = await _run_fixture(
        tmp_path / "paged", seed, paged=True, blockers=blockers, dry_run=dry_run
    )

    expected = "DRY_RUN" if dry_run else "PARTIAL" if blockers else "COMPLETE"
    assert legacy_result["status"] == paged_result["status"] == expected
    assert legacy_result.get("blocked_projects") == paged_result.get("blocked_projects")
    legacy_run, paged_run = legacy.cleanup_run(), paged.cleanup_run()
    assert (legacy_run.status, legacy_run.error_code, legacy_run.summary) == (
        paged_run.status,
        paged_run.error_code,
        paged_run.summary,
    )
    path_effects = {"orphans_removed", "orphans_kept"}
    for key in sorted(set(legacy.effects) | set(paged.effects)):
        if key in path_effects:
            assert _relative(legacy.root, legacy.effects.get(key, set())) == _relative(
                paged.root, paged.effects.get(key, set())
            ), key
        else:
            assert legacy.effects.get(key) == paged.effects.get(key), key
    # Every directory left behind is the same, so is every one removed.
    left = {
        root: sorted(
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_dir() and "worktree-git" not in path.parts
        )
        for root in (legacy.root, paged.root)
    }
    assert left[legacy.root] == left[paged.root]
    # The fixture exercises each outcome (guards against a vacuous comparison).
    assert len(paged.effects["eligible"]) >= 7 and paged.effects["protected"]
    if not dry_run:
        assert paged.effects["removed_files"] and paged.effects["authorized_worktrees"]
        assert len(paged.effects["authorized_worktrees"]) == 7 + (0 if blockers else 1)
    orphans = _relative(paged.root, paged.effects["orphans_kept"])
    blocked_orphans = {path for path in orphans if path.startswith("worktrees/musematic-deploy")}
    # Leased and candidate checkouts are never orphans; a blocked repository is not walked.
    # The released, published lease's checkout is listed in both runs, as before NEW-245
    # (only the unreleased leases' paths are excluded; Git's branch check keeps it).
    published = f"worktrees/falcone/{_id('cs:falcone-published')}"
    assert len(orphans - blocked_orphans) == 5 and published in orphans
    assert len(blocked_orphans) == (0 if blockers else 1)
    # Paged: only references in the context, lists deleted at finalization.
    assert paged.stored_rows() == 0
    # A retried finalization answers from the finished Run, as the legacy step does.
    retried = await paged.step(
        "cleanup.stored",
        {
            **paged_result["context"],
            "terminal_status": expected,
            "cleanup_inner": "cleanup.finalize",
        },
    )
    assert retried["status"] == expected
    assert "cleanup_candidates" not in paged_result["context"]
    assert "cleanup.stored" in paged.order and "cleanup.authorize" not in paged.order
    legacy.metadata.engine.dispose()
    paged.metadata.engine.dispose()


async def test_every_worker_page_holds_at_most_the_page_size(tmp_path: Path) -> None:
    harness, result = await _run_fixture(tmp_path / "paged", _mixed_seed(), paged=True, blockers={})

    assert result["status"] == "COMPLETE"
    # cleanup_page_size=3: no worker step ever receives a longer list.
    worker_steps = {phase.step for phase in CLEANUP_PHASES.values()}
    assert {name: harness.page_lengths[name] for name in worker_steps} == {
        name: min(3, harness.page_lengths[name]) for name in worker_steps
    }
    assert harness.page_lengths["cleanup.protect_active"] == 3
    for name in ("cleanup.protect_active", "cleanup.execute"):
        assert harness.order.count(name) >= 3  # 13 and 8 records: several pages
    # Every phase's worker step runs at least once, as in the legacy run.
    for phase in CLEANUP_PHASES.values():
        assert phase.step in harness.order
    harness.metadata.engine.dispose()


async def test_resumed_run_after_continue_as_new_finishes_identically(tmp_path: Path) -> None:
    seed = _mixed_seed()
    straight, straight_result = await _run_fixture(
        tmp_path / "straight", seed, paged=True, blockers={}
    )
    _mkdir(tmp_path / "resumed")
    harness = Harness(tmp_path / "resumed", _settings(tmp_path / "resumed", "c.sqlite"), {})
    seed.install(tmp_path / "resumed", harness.metadata)

    class Continued(Exception):
        def __init__(self, request: CleanupInput) -> None:
            self.request = request

    def continue_as_new(request: CleanupInput) -> None:
        raise Continued(request)

    checks = 0

    def long_history() -> bool:
        nonlocal checks
        checks += 1
        return checks % 4 == 0  # continue as new at every fourth page boundary

    request = CleanupInput(dry_run=False)
    previous: str | None = None
    continuations = 0
    while True:
        instance = CleanupWorkflow()
        with (
            patch.object(instance, "_step", side_effect=harness.step),
            patch.object(instance, "_paged_cleanup_patched", return_value=True),
            patch.object(instance, "_paged_cleanup_history_long", side_effect=long_history),
            patch.object(instance, "_continued_run_id", return_value=previous),
            patch(
                "engineering_control.workflows.definitions.workflow.continue_as_new",
                side_effect=continue_as_new,
            ),
            patch("engineering_control.workflows.definitions.workflow.patched", return_value=True),
        ):
            try:
                result = await instance.run(request)
                break
            except Continued as continued:
                continuations += 1
                # Only the controller's checkpoint reference crosses the boundary.
                assert set(continued.request.resume or {}) == {"checkpoint"}
                request = continued.request
                previous, harness.workflow_run_id = harness.workflow_run_id, str(uuid4())

    assert continuations >= 3
    assert result["status"] == straight_result["status"] == "COMPLETE"
    assert harness.effects == {
        key: values
        if key not in {"orphans_kept", "orphans_removed"}
        else {value.replace("/straight/", "/resumed/") for value in values}
        for key, values in straight.effects.items()
    }
    assert harness.cleanup_run().summary == straight.cleanup_run().summary
    harness.metadata.engine.dispose()
    straight.metadata.engine.dispose()


def _large_seed(leases: int, files: int, orphans: int) -> tuple[Seed, Callable[[Path], None]]:
    seed = Seed()
    for index in range(leases):
        seed.lease(f"large-{index}")
    for index in range(files):
        seed.codex_run(f"large-{index}")

    def make_orphans(root: Path) -> None:
        old = OLD.timestamp()
        for index in range(orphans):
            path = root / "worktrees" / "falcone" / str(_id(f"large-orphan:{index}"))
            path.mkdir(parents=True)
            os.utime(path, (old, old))

    return seed, make_orphans


async def test_a_large_backlog_never_produces_an_activity_payload_over_512_kib(
    tmp_path: Path,
) -> None:
    seed, make_orphans = _large_seed(leases=30_000, files=5_000, orphans=2_000)
    root = tmp_path / "large"
    _mkdir(root)
    harness = Harness(root, _settings(root, "large.sqlite", cleanup_page_size=100), {})
    seed.install(root, harness.metadata)
    make_orphans(root)

    result = await harness.run(paged=True)

    assert result["status"] == "COMPLETE"
    largest = max(harness.sizes, key=lambda entry: max(entry[1], entry[2]))
    assert max(largest[1], largest[2]) < LIMIT, largest
    # Nothing is deferred: every eligible worktree and file was handled within the run.
    assert len(harness.effects["absent_worktrees"]) == 30_000
    assert len(harness.effects["absent_files"]) == 5_000
    assert len(harness.effects["orphans_kept"]) == 2_000
    assert harness.stored_rows() == 0
    harness.metadata.engine.dispose()


# --- orphan pruning inverted -------------------------------------------------------------


def test_orphan_listing_and_removal_follow_the_controller_decision(tmp_path: Path) -> None:
    from test_worktrees import _resume_fixture, git  # type: ignore[import-not-found]

    manager, _request, changeset_path, sha = _resume_fixture(tmp_path)
    manager._grant_read_access = lambda *_args, **_kwargs: None
    snapshots = [
        manager.allocate_audit_snapshot(
            repository="falcone",
            campaign_id=uuid4(),
            base_ref="refs/heads/main",
            expected_base_sha=sha,
        ).path
        for _ in range(3)
    ]
    old = 1_000_000_000
    for directory in (*snapshots, changeset_path):
        os.utime(directory, (old, old))
    git(["checkout", "-q", "--detach"], changeset_path)  # detached: a snapshot to the walk
    cutoff = datetime.now(UTC) - timedelta(days=14)

    first, cursor = manager.list_orphan_worktrees(older_than=cutoff, limit=2)
    rest, end = manager.list_orphan_worktrees(older_than=cutoff, limit=10, after=cursor)
    listed = [*first, *rest]
    assert cursor is not None and end is None
    assert {entry["path"] for entry in listed} == {str(p) for p in (*snapshots, changeset_path)}
    assert [(e["repository"], e["name"]) for e in listed] == sorted(
        (e["repository"], e["name"]) for e in listed
    )
    # The controller withheld the leased (detached) checkout and one snapshot.
    approved = [
        {key: entry[key] for key in ("path", "repository", "name")}
        for entry in listed
        if entry["path"] not in {str(changeset_path), str(snapshots[0])}
    ]
    dry = manager.apply_orphan_worktrees(approved, older_than=cutoff, dry_run=True, limit=200)
    assert {item["path"] for item in dry["removed"]} == {str(p) for p in snapshots[1:]}
    tampered = [{**approved[0], "path": str(changeset_path)}]
    assert manager.apply_orphan_worktrees(
        tampered, older_than=cutoff, dry_run=False, limit=200
    ) == {"removed": [], "kept": []}
    limited = manager.apply_orphan_worktrees(approved, older_than=cutoff, dry_run=False, limit=1)
    assert len(limited["removed"]) == 1
    done = manager.apply_orphan_worktrees(approved, older_than=cutoff, dry_run=False, limit=200)
    assert len(done["removed"]) == 1
    assert changeset_path.exists() and snapshots[0].exists()
    assert not snapshots[1].exists() and not snapshots[2].exists()


async def _discover(harness: Harness, *, dry_run: bool = False) -> dict[str, Any]:
    return await harness.step(
        "cleanup.discover",
        {"dry_run": dry_run, "retention_policy": "standard", "cleanup_store_mode": "paged-v1"},
    )


async def _phase(harness: Harness, context: dict[str, Any], name: str) -> None:
    """Page one phase exactly as CleanupWorkflow._paged_cleanup_phase does."""
    page, outcome = 0, None
    while True:
        loaded = await harness.step(
            "cleanup.page",
            {
                "cleanup_store": context["cleanup_store"],
                "dry_run": context.get("dry_run", True),
                "cleanup_phase": name,
                "cleanup_page": page,
                "cleanup_page_outcome": outcome,
                "cleanup_authorization_sha256": context.get("cleanup_authorization_sha256"),
            },
        )
        if loaded["cleanup_page_done"]:
            return
        result = await harness.step(CLEANUP_PHASES[name].step, loaded["cleanup_page_payload"])
        outcome = {
            "page": page,
            "values": {key: result[key] for key in loaded["cleanup_page_outcome_keys"]},
        }
        page += 1


def _entry(root: Path, repository: str, name: str, resolved: str | None = None) -> dict[str, str]:
    path = str(root / repository / name)
    return {
        "path": path,
        "resolved": resolved or path,
        "repository": repository,
        "name": name,
        "modified_at": OLD.isoformat(),
    }


async def _decide(
    harness: Harness, context: dict[str, Any], listing: list[dict[str, str]], **extra: Any
) -> dict[str, Any]:
    return await harness.step(
        "cleanup.decide_orphan_worktrees",
        {
            "cleanup_store": context["cleanup_store"],
            "dry_run": False,
            "cleanup_identity_blocked_repositories": ["musematic-deploy"],
            "orphan_listing": listing,
            "orphan_page": 0,
            **extra,
        },
    )


async def test_the_controller_rejects_tracked_and_protected_orphans(tmp_path: Path) -> None:
    seed = Seed()
    seed.lease("tracked-active", released=False)
    seed.lease("eligible")
    harness = Harness(tmp_path, _settings(tmp_path, "decide.sqlite"), {})
    seed.install(tmp_path, harness.metadata)
    root = (tmp_path / "worktrees").resolve()
    context = await _discover(harness)
    await _phase(harness, context, "protect")
    await _phase(harness, context, "resolve_protected")

    tracked = _entry(root, "falcone", str(_id("cs:tracked-active")))
    candidate = _entry(root, "falcone", str(_id("cs:eligible")))
    linked = _entry(root, "falcone", str(uuid4()), resolved=tracked["path"])
    blocked = _entry(root, "musematic-deploy", str(uuid4()))
    orphan = _entry(root, "falcone", str(uuid4()))
    decided = await _decide(harness, context, [tracked, candidate, linked, blocked, orphan])

    apply = decided["orphan_apply_payload"]
    assert [value["path"] for value in apply["orphan_approved"]] == [orphan["path"]]
    assert apply["orphan_limit"] == 200  # counted from the stored removals only
    assert apply["dry_run"] is False
    # Another workflow's reference is refused.
    with pytest.raises(ApplicationError):
        await harness.metadata.handle(
            "cleanup.decide_orphan_worktrees",
            {
                "cleanup_store": context["cleanup_store"],
                "orchestration": {"workflow_id": "another-cleanup"},
                "orphan_listing": [],
                "orphan_page": 0,
            },
        )
    harness.metadata.engine.dispose()


# --- review round: fail-closed regressions ----------------------------------------------


async def test_a_fresh_start_with_forged_resume_state_is_refused(tmp_path: Path) -> None:
    # Codex review (critical): a dry-run request carrying plan and authorization state.
    seed = Seed()
    seed.lease("active-clean", released=False, directory=True)
    harness = Harness(tmp_path, _settings(tmp_path, "forged.sqlite"), {})
    seed.install(tmp_path, harness.metadata)
    active = str((tmp_path / "worktrees").resolve() / "falcone" / str(_id("cs:active-clean")))
    forged = {
        "plan": ["discover", "execute_worktrees", "finalize"],
        "context": {
            "dry_run": False,
            "authorized_candidates": [{"lease_id": str(_id("lease:active-clean"))}],
        },
    }
    for resume in (forged, {"checkpoint": {"run_id": str(uuid4()), "ordinal": 0}}):
        instance = CleanupWorkflow()
        with (
            patch.object(instance, "_step", side_effect=harness.step),
            patch.object(instance, "_paged_cleanup_patched", return_value=True),
            patch.object(instance, "_continued_run_id", return_value=None),
        ):
            with pytest.raises(ApplicationError, match="own continue-as-new"):
                await instance.run(CleanupInput(dry_run=True, resume=resume))
    assert harness.order == []
    assert _is_dir(active)
    harness.metadata.engine.dispose()


async def test_discovery_refuses_injected_lists_and_a_dry_run_never_executes(
    tmp_path: Path,
) -> None:
    seed = Seed()
    seed.lease("eligible")
    harness = Harness(tmp_path, _settings(tmp_path, "dry.sqlite"), {})
    seed.install(tmp_path, harness.metadata)
    with pytest.raises(ApplicationError):
        await harness.step(
            "cleanup.discover",
            {
                "dry_run": False,
                "retention_policy": "standard",
                "cleanup_store_mode": "paged-v1",
                "authorized_candidates": [{"lease_id": str(_id("lease:eligible"))}],
            },
        )
    context = await _discover(harness, dry_run=True)
    with pytest.raises(ApplicationError):  # the stored mode is dry: no execute page
        await _phase(harness, {**context, "dry_run": False}, "execute_worktrees")
    with pytest.raises(ApplicationError):  # nor an authorization
        await harness.step(
            "cleanup.stored", {**context, "dry_run": False, "cleanup_inner": "cleanup.authorize"}
        )
    await _phase(harness, context, "protect")
    await _phase(harness, context, "resolve_protected")
    decided = await _decide(harness, context, [], dry_run=False)
    assert decided["orphan_apply_payload"]["dry_run"] is True
    assert "cleanup.execute" not in harness.order
    harness.metadata.engine.dispose()


async def test_an_execute_page_needs_the_accepted_authorization(tmp_path: Path) -> None:
    seed = Seed()
    seed.lease("eligible")
    harness = Harness(tmp_path, _settings(tmp_path, "digest.sqlite"), {})
    seed.install(tmp_path, harness.metadata)
    context = await _discover(harness)
    await _phase(harness, context, "protect")
    for name in ("inspect_evidence", "inspect_delivery", "inspect_browser", "inspect_codex"):
        await _phase(harness, context, name)
    await _phase(harness, context, "inspect_preview")
    authorized = await harness.step(
        "cleanup.stored", {**context, "dry_run": False, "cleanup_inner": "cleanup.authorize"}
    )
    with pytest.raises(ApplicationError):
        await _phase(
            harness, {**authorized, "cleanup_authorization_sha256": "0" * 64}, "execute_worktrees"
        )
    with pytest.raises(ApplicationError):
        await _phase(
            harness, {**authorized, "cleanup_authorization_sha256": None}, "execute_worktrees"
        )
    await _phase(harness, authorized, "execute_worktrees")
    assert harness.effects["authorized_worktrees"] == {str(_id("lease:eligible"))}
    harness.metadata.engine.dispose()


async def test_a_resume_checkpoint_is_bound_to_its_own_continuation(tmp_path: Path) -> None:
    seed = Seed()
    seed.lease("eligible")
    harness = Harness(tmp_path, _settings(tmp_path, "resume.sqlite"), {})
    seed.install(tmp_path, harness.metadata)
    context = await _discover(harness)
    writer = harness.workflow_run_id
    state = {"plan": ["protect", "scope"], "context": context, "blocked_projects": {}, "cursor": {}}
    token = (
        await harness.step(
            "cleanup.checkpoint",
            {"cleanup_store": context["cleanup_store"], "cleanup_checkpoint_state": state},
        )
    )["cleanup_checkpoint"]
    harness.workflow_run_id = reader = str(uuid4())

    async def resume(**changes: Any) -> dict[str, Any]:
        payload = {"cleanup_checkpoint": token, "cleanup_continued_run_id": writer, **changes}
        return await harness.step("cleanup.resume", payload)

    for bad in (
        {"cleanup_continued_run_id": str(uuid4())},  # not this checkpoint's writer
        {"cleanup_checkpoint": {**token, "sha256": "0" * 64}},
        {"cleanup_checkpoint": {**token, "generation": "0" * 16}},
    ):
        with pytest.raises(ApplicationError):
            await resume(**bad)
    resumed = await resume()
    assert resumed["cleanup_resume_state"] == state and resumed["dry_run"] is False
    assert (await resume())["cleanup_resume_state"] == state  # the same run's retry
    harness.workflow_run_id = str(uuid4())
    with pytest.raises(ApplicationError):  # already consumed by its continuation
        await resume()
    harness.workflow_run_id, original = reader, harness.workflow_id
    harness.workflow_id = "another-cleanup"
    with pytest.raises(ApplicationError):
        await resume()
    # A checkpoint never carries stored lists in its context.
    harness.workflow_id = original
    with pytest.raises(ApplicationError):
        await harness.step(
            "cleanup.checkpoint",
            {
                "cleanup_store": context["cleanup_store"],
                "cleanup_checkpoint_state": {
                    **state,
                    "context": {**context, "cleanup_candidates": []},
                },
            },
        )
    harness.metadata.engine.dispose()


async def test_the_sweep_keeps_a_live_runs_lists_and_missing_protections_fail_closed(
    tmp_path: Path,
) -> None:
    seed = Seed()
    seed.lease("eligible")
    harness = Harness(tmp_path, _settings(tmp_path, "sweep.sqlite"), {})
    seed.install(tmp_path, harness.metadata)
    context = await _discover(harness)
    first = harness.workflow_id
    with harness.metadata.sessions.begin() as session:
        for row in session.scalars(select(CleanupRunItem)):
            row.created_at = OLD - timedelta(days=365)  # long past any retention
    harness.workflow_id = f"cleanup-new-245-second-{uuid4()}"
    second = await _discover(harness)
    harness.workflow_id = first
    await _phase(harness, context, "protect")  # the first, still running, reads its lists
    # A missing protection list is an error, never an empty set.
    with harness.metadata.sessions.begin() as session:
        session.execute(
            delete(CleanupRunItem).where(
                CleanupRunItem.list_name
                == f"{context['cleanup_store']['generation']}/leased_worktree_paths"
            )
        )
    with pytest.raises(ApplicationError):
        await _phase(harness, context, "resolve_protected")
    with pytest.raises(ApplicationError):
        await _decide(harness, context, [])
    # Lists of a finished run are swept by the next discovery.
    with harness.metadata.sessions.begin() as session:
        run = session.scalar(select(Run).where(Run.workflow_id == first))
        assert run is not None
        run.finished_at = datetime.now(UTC)
        run_id = run.id
    harness.workflow_id = f"cleanup-new-245-third-{uuid4()}"
    await _discover(harness)
    with harness.metadata.sessions() as session:
        runs = set(session.scalars(select(CleanupRunItem.run_id)))
    assert run_id not in runs and UUID(second["cleanup_store"]["run_id"]) in runs
    harness.metadata.engine.dispose()


async def test_a_superseded_discovery_attempt_cannot_replace_the_accepted_lists(
    tmp_path: Path,
) -> None:
    seed = Seed()
    seed.lease("tracked-active", released=False)
    harness = Harness(tmp_path, _settings(tmp_path, "generation.sqlite"), {})
    seed.install(tmp_path, harness.metadata)
    accepted = await _discover(harness)
    # A timed-out attempt finishing late, after another lease appeared, writes its own
    # generation; the accepted one is untouched.
    late_seed = Seed()
    late_seed.lease("late-active", released=False)
    late_seed.install(tmp_path, harness.metadata)
    late = await _discover(harness)
    assert late["cleanup_store"]["generation"] != accepted["cleanup_store"]["generation"]
    with harness.metadata.sessions() as session:
        from engineering_control.activities.cleanup_store import CleanupStore, load_cleanup_lists

        store = CleanupStore(
            UUID(accepted["cleanup_store"]["run_id"]), accepted["cleanup_store"]["generation"]
        )
        paths = load_cleanup_lists(session, store, ["leased_worktree_paths"])
    assert [Path(path).name for path in paths["leased_worktree_paths"]] == [
        str(_id("cs:tracked-active"))
    ]
    forged = {**accepted["cleanup_store"], "generation": "f" * 16}
    with pytest.raises(ApplicationError):
        await _phase(harness, {**accepted, "cleanup_store": forged}, "protect")
    harness.metadata.engine.dispose()


async def test_a_symlinked_protected_path_keeps_its_target_as_the_legacy_run_did(
    tmp_path: Path,
) -> None:
    # Codex review: protected /worktrees/falcone/A -> detached worktree B; B must be kept.
    seed = Seed()
    seed.lease("aliased-active", released=False)
    seed.orphan("falcone", "alias-target")
    seed.orphan("falcone", "plain")
    seed.aliases.append(
        (
            f"worktrees/falcone/{_id('cs:aliased-active')}",
            f"worktrees/falcone/{_id('orphan:alias-target')}",
        )
    )
    results = {}
    for paged in (False, True):
        root = tmp_path / ("paged" if paged else "legacy")
        harness, result = await _run_fixture(root, seed, paged=paged, blockers={})
        assert result["status"] == "COMPLETE"
        results[paged] = _relative(root, harness.effects.get("orphans_kept", set()))
        harness.metadata.engine.dispose()
    target = f"worktrees/falcone/{_id('orphan:alias-target')}"
    assert results[True] == results[False] == {f"worktrees/falcone/{_id('orphan:plain')}"}
    assert target not in results[True]


async def test_an_orphan_apply_page_is_bound_to_its_run_and_exact_paths(tmp_path: Path) -> None:
    seed = Seed()
    seed.orphan("falcone", "approved")
    seed.orphan("falcone", "extra")
    harness = Harness(tmp_path, _settings(tmp_path, "apply.sqlite"), {})
    seed.install(tmp_path, harness.metadata)
    root = (tmp_path / "worktrees").resolve()
    context = await _discover(harness)
    await _phase(harness, context, "protect")
    await _phase(harness, context, "resolve_protected")
    approved = _entry(root, "falcone", str(_id("orphan:approved")))
    apply = (await _decide(harness, context, [approved]))["orphan_apply_payload"]
    extra = {
        "path": str(root / "falcone" / str(_id("orphan:extra"))),
        "repository": "falcone",
        "name": str(_id("orphan:extra")),
    }
    for tampered in (
        {**apply, "orphan_approved": [*apply["orphan_approved"], extra]},
        {**apply, "orphan_limit": 1_000},
        {**apply, "dry_run": not apply["dry_run"]},
    ):
        with pytest.raises(ApplicationError):
            await harness.step("cleanup.apply_orphan_worktrees", tampered)
    original = harness.workflow_id
    harness.workflow_id = "another-cleanup"  # replayed under another cleanup run
    with pytest.raises(ApplicationError):
        await harness.step("cleanup.apply_orphan_worktrees", apply)
    harness.workflow_id = original
    applied = await harness.step("cleanup.apply_orphan_worktrees", apply)
    assert [Path(item["path"]).name for item in applied["orphan_worktrees_kept"]] == [
        str(_id("orphan:approved"))
    ]
    assert (root / "falcone" / str(_id("orphan:extra"))).is_dir()
    harness.metadata.engine.dispose()


@pytest.mark.parametrize("worker", ["controller", "codex", "browser", "delivery", "release"])
def test_the_new_scan_steps_fail_closed_off_the_scan_worker(worker: str) -> None:
    for step in ("cleanup.list_orphan_worktrees", "cleanup.apply_orphan_worktrees"):
        assert step in ControlActivities.steps_for_worker("scan")
        assert step not in ControlActivities.steps_for_worker(worker)
    for step in ("cleanup.page", "cleanup.stored", "cleanup.decide_orphan_worktrees"):
        assert step in ControlActivities.steps_for_worker("controller")
        assert step not in ControlActivities.steps_for_worker("scan")


@pytest.mark.parametrize(
    "step", ["cleanup.list_orphan_worktrees", "cleanup.apply_orphan_worktrees"]
)
async def test_an_old_scan_worker_refuses_the_new_orphan_steps(
    monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    monkeypatch.setattr("engineering_control.activities.core.activity.heartbeat", lambda _: None)
    activities = ControlActivities(Settings(synthetic_mode=False), "scan")
    # A pre-NEW-245 build: neither step exists, so the activity errors and deletes nothing.
    old_steps = ControlActivities._allowed_steps - {
        "cleanup.list_orphan_worktrees",
        "cleanup.apply_orphan_worktrees",
    }
    monkeypatch.setattr(ControlActivities, "_allowed_steps", old_steps)
    applied: list[Any] = []
    monkeypatch.setattr(
        activities.scan_handler.manager,
        "apply_orphan_worktrees",
        lambda *args, **kwargs: applied.append(args),
    )

    with pytest.raises(ApplicationError, match="unknown or invalid control step"):
        await activities.execute_control_step(
            {"step": step, "payload": {"dry_run": False, "orphan_approved": [], "orphan_limit": 1}}
        )
    assert applied == []


# --- Temporal: the paged history and its replay ---------------------------------------

_TEMPORAL_CLI = shutil.which("temporal")
requires_temporal = pytest.mark.skipif(
    _TEMPORAL_CLI is None, reason="requires the `temporal` CLI on PATH to run a local dev server"
)


@requires_temporal
async def test_the_paged_history_completes_and_replays(tmp_path: Path) -> None:
    seed = _mixed_seed()
    _mkdir(tmp_path / "temporal")
    harness = Harness(
        tmp_path / "temporal", _settings(tmp_path / "temporal", "t.sqlite"), MUSEMATIC_BLOCKED
    )
    seed.install(tmp_path / "temporal", harness.metadata)

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        harness.workflow_id = activity.info().workflow_id
        return await harness.step(request["step"], request["payload"])

    queues = sorted({CONTROL_TASK_QUEUE, *ROLE_TASK_QUEUES.values()})
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI) as env:
        workers = [
            Worker(
                env.client,
                task_queue=queue,
                workflows=[CleanupWorkflow] if queue == CONTROL_TASK_QUEUE else [],
                activities=[execute_control_step],
            )
            for queue in queues
        ]
        for worker in workers:
            await worker.__aenter__()
        try:
            handle = await env.client.start_workflow(
                CleanupWorkflow.run,
                CleanupInput(dry_run=False),
                id=f"cleanup-new-245-temporal-{uuid4()}",
                task_queue=CONTROL_TASK_QUEUE,
                execution_timeout=timedelta(minutes=2),
            )
            result = await handle.result()
            history = await handle.fetch_history()
        finally:
            for worker in reversed(workers):
                await worker.__aexit__(None, None, None)

    assert result["status"] == "PARTIAL"
    assert any(
        CLEANUP_PAGED_PATCH.encode() in event.marker_recorded_event_attributes.SerializeToString()
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
    )
    await Replayer(workflows=[CleanupWorkflow]).replay_workflow(history)
    harness.metadata.engine.dispose()
