"""NEW-214a: a PR behind its base is merged forward by the repair maker, never rebased.

With strict up-to-date branch protection, GitHub reports a PR whose base moved as BEHIND
and refuses to merge it. For a project with ``merge_forward`` enabled:
- the staging gates report SOURCE/DEPLOYMENT_PR_BEHIND_BASE, and the loop starts a
  CI-repair round after refreshing the canonical mirrors;
- the allocation only reports the mirror's base tip (Hermes runs no new git in the
  maker-writable repository);
- the sandboxed maker merges that tip in and resolves any conflict, and the checker
  reviews the diff from the tip;
- publication verifies, in its own controller-side clone, that the head contains both the
  tip and the published head before the non-force push;
- only then does persist_publication move the ChangeSet to the new base.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from temporalio import activity, workflow
from temporalio.worker import Replayer
from test_delivery_activity import (
    FakePublicationRunner,
    _controller_publisher,
    _remove_local_worktrees,
    _with_intent,
    publication_payload,
)
from test_delivery_metadata import (
    _lease_fixture,
    _self_contained_evidence,
    checker_result,
    publication_evidence,
)
from test_staging_gitops import SAFE, FakeGh, _config_root, _plan, _pull_request, _settings
from test_workflow_replay import (
    _GITOPS_PLAN,
    _LOOP_WORKFLOWS,
    _full_delivery_loop_script,
    _run_loop,
    _scheduled_steps,
    _scripted_execute_control_step,
)
from test_worktrees import _advance_main, _resume_fixture, git

from engineering_control.activities.delivery import (
    DeliveryActivityHandler,
    MergeForwardNotMerged,
    RepositoryPublication,
)
from engineering_control.activities.gitops import GitOpsGitHubHandler
from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.adapters.command import CommandFailure, CommandResult, SafeCommandRunner
from engineering_control.config import Settings
from engineering_control.db.models import Base, ChangeSet, Run
from engineering_control.operations.issue_delivery_approval import CI_REPAIR_REGRESSIONS
from engineering_control.workflows.definitions import (
    ExistingGitHubBacklogLoopWorkflow,
    IssueDeliveryInput,
    StagingGitOpsDeliveryWorkflow,
)
from engineering_control.worktrees.manager import WorktreeAllocation

# -- worktree manager: reports the tip, never merges --------------------------------------


def test_a_resumed_allocation_reports_the_moved_base_tip_without_merging(tmp_path: Path) -> None:
    manager, request, path, sha = _resume_fixture(tmp_path)
    published = git(["rev-parse", "HEAD"], path)
    moved = _advance_main(tmp_path)

    allocation = manager.allocate(**request, resume_prior_commits=True)  # type: ignore[arg-type]

    assert (allocation.base_sha, allocation.resumed_from_sha) == (sha, published)
    assert allocation.base_tip_sha == moved
    # Nothing ran in the worktree beyond the existing resume: HEAD is untouched.
    assert git(["rev-parse", "HEAD"], path) == published
    assert not (path / "CHANGELOG.md").exists()


def test_an_unmoved_base_reports_no_tip(tmp_path: Path) -> None:
    manager, request, _path, _sha = _resume_fixture(tmp_path)

    allocation = manager.allocate(**request, resume_prior_commits=True)  # type: ignore[arg-type]

    assert allocation.base_tip_sha is None


# -- scan worker allocation ---------------------------------------------------------------


class _TipManager:
    def __init__(self, root: Path, *, tip: str | None = "e" * 40) -> None:
        self.root = root
        self.tip = tip

    def allocate(self, **kwargs: Any) -> WorktreeAllocation:
        repository = kwargs["repository"]
        source = repository == "musematic"
        return WorktreeAllocation(
            repository=repository,
            path=self.root / repository,
            branch=f"agent/{repository}",
            base_sha=kwargs["expected_base_sha"],
            change_set_id=kwargs["change_set_id"],
            resumed_from_sha="f" * 40 if source else None,
            base_tip_sha=self.tip if source else None,
        )


def _allocation_payload(merge_forward: str | None) -> dict[str, Any]:
    request: dict[str, Any] = {
        "change_set_id": str(uuid4()),
        "project": "musematic",
        "issue": "993",
        "source_repository": "musematic",
        "source_base_sha": "a" * 40,
        "deployment_repository": "musematic-deploy",
        "deployment_base_sha": "b" * 40,
        "maker_provider": "codex",
        "checker_provider": "claude",
    }
    payload: dict[str, Any] = {"allocation_request": request}
    if merge_forward is not None:
        request["merge_forward"] = merge_forward
        payload["merge_forward"] = {"target": merge_forward, "from_sha": "d" * 40}
    return payload


def test_allocation_completes_the_requirement_with_the_mirror_tip(tmp_path: Path) -> None:
    handler = ScanActivityHandler(
        Settings(delivery_resume_prior_commits=True), _TipManager(tmp_path)
    )  # type: ignore[arg-type]

    allocated = handler._allocate(_allocation_payload("source"))

    assert allocated["status"] == "WORKTREES_ALLOCATED"
    assert allocated["merge_forward"] == {
        "target": "source",
        "repository": "musematic",
        "base_sha": "e" * 40,
        "from_sha": "d" * 40,
    }
    # Every later step (prechecks baseline, validation, checker range) measures from the tip.
    assert allocated["source_base_sha"] == "e" * 40
    assert "deployment_base_sha" not in allocated  # only the behind side moves


def test_an_unmoved_base_asks_for_no_merge(tmp_path: Path) -> None:
    handler = ScanActivityHandler(  # type: ignore[arg-type]
        Settings(delivery_resume_prior_commits=True), _TipManager(tmp_path, tip=None)
    )

    allocated = handler._allocate(_allocation_payload("source"))

    assert allocated["merge_forward"] is None
    assert "source_base_sha" not in allocated  # the delivery keeps its recorded base


def test_allocation_without_merge_forward_is_unchanged(tmp_path: Path) -> None:
    handler = ScanActivityHandler(
        Settings(delivery_resume_prior_commits=True), _TipManager(tmp_path)
    )  # type: ignore[arg-type]

    allocated = handler._allocate(_allocation_payload(None))

    assert "merge_forward" not in allocated
    assert "source_base_sha" not in allocated


# -- staging gates ------------------------------------------------------------------------


def _merge_forward_github(tmp_path: Path, *, enabled: bool) -> tuple[GitOpsGitHubHandler, FakeGh]:
    projects = _config_root(tmp_path, safe_repositories=SAFE)
    path = projects / "musematic.yaml"
    text = path.read_text(encoding="utf-8")
    marker = "  staging_flow:\n    enabled: true\n"
    assert marker in text
    if enabled:
        path.write_text(text.replace(marker, marker + "    merge_forward: true\n"), "utf-8")
    settings = _settings(tmp_path, project_config_root=projects, github_writes_enabled=True)
    fake = FakeGh()
    return GitOpsGitHubHandler(settings, runner=fake), fake  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_pr_behind_its_base_blocks_its_checks_and_merge_when_enabled(
    tmp_path: Path,
) -> None:
    handler, fake = _merge_forward_github(tmp_path, enabled=True)
    deployment_head = "e" * 40
    plan = _plan(
        deployment_pr_number=9,
        deployment_pr_url="https://github.com/gntik-ai/musematic-deploy/pull/9",
        deployment_head_sha=deployment_head,
    )
    fake.pull_requests[("gntik-ai/musematic", 77)] = _pull_request(mergeStateStatus="BEHIND")
    fake.pull_requests[("gntik-ai/musematic-deploy", 9)] = _pull_request(
        number=9,
        url="https://github.com/gntik-ai/musematic-deploy/pull/9",
        headRefOid=deployment_head,
        mergeStateStatus="BEHIND",
    )

    source = await handler.handle("gitops.pr_checks", {"plan": plan})
    deployment = await handler.handle("gitops.deployment_pr_checks", {"plan": plan})
    fake.pull_requests[("gntik-ai/musematic", 77)]["isDraft"] = False
    merge = await handler.handle("gitops.merge", {"plan": plan, "target": "source"})

    assert source == {"status": "BLOCKED", "blocker": "SOURCE_PR_BEHIND_BASE"}
    assert deployment == {"status": "BLOCKED", "blocker": "DEPLOYMENT_PR_BEHIND_BASE"}
    assert merge == {"status": "BLOCKED", "blocker": "SOURCE_PR_BEHIND_BASE"}
    assert not any("merge" in " ".join(call) for call in fake.mutations)


@pytest.mark.asyncio
async def test_a_behind_pr_is_unchanged_without_merge_forward(tmp_path: Path) -> None:
    handler, fake = _merge_forward_github(tmp_path, enabled=False)
    fake.pull_requests[("gntik-ai/musematic", 77)] = _pull_request(
        mergeStateStatus="BEHIND", isDraft=False
    )

    merge = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})

    assert merge == {"status": "MERGE_PENDING", "target": "source", "merge_state": "BEHIND"}


@pytest.mark.asyncio
async def test_a_conflicting_pr_goes_to_the_maker_when_enabled(tmp_path: Path) -> None:
    # Review 1: a conflicting PR (DIRTY) needs the base merged in too; the maker resolves it.
    handler, fake = _merge_forward_github(tmp_path, enabled=True)
    fake.pull_requests[("gntik-ai/musematic", 77)] = _pull_request(
        mergeStateStatus="DIRTY", statusCheckRollup=[]
    )

    source = await handler.handle("gitops.pr_checks", {"plan": _plan()})
    fake.pull_requests[("gntik-ai/musematic", 77)]["isDraft"] = False
    merge = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})

    assert source == {"status": "BLOCKED", "blocker": "SOURCE_PR_BEHIND_BASE"}
    assert merge == {"status": "BLOCKED", "blocker": "SOURCE_PR_BEHIND_BASE"}


def test_merge_forward_blockers_authorize_a_repair_round() -> None:
    assert {"SOURCE_PR_BEHIND_BASE", "DEPLOYMENT_PR_BEHIND_BASE"} <= CI_REPAIR_REGRESSIONS


# -- publication: verified from the controller's own clone -----------------------------


class _AncestryRunner(FakePublicationRunner):
    def __init__(self, branch: str, heads: dict[str, str], *, missing: set[str]) -> None:
        super().__init__(branch, heads)
        self.missing = missing
        self.ancestry: list[tuple[Path, str, str]] = []

    def run(
        self, executable: str, arguments: list[str], *, cwd: Path, **kwargs: Any
    ) -> CommandResult:
        if executable == "git" and arguments[:2] == ["merge-base", "--is-ancestor"]:
            self.ancestry.append((cwd, arguments[2], arguments[3]))
            if arguments[2] in self.missing:
                raise CommandFailure("not an ancestor")
            return CommandResult(0, "", "")
        return super().run(executable, arguments, cwd=cwd, **kwargs)


def _merge_forward_publication(tmp_path: Path, missing: set[str]) -> tuple[Any, ...]:
    payload, branch, heads = publication_payload(tmp_path / "worktrees")
    _with_intent(payload)
    _remove_local_worktrees(payload)
    (tmp_path / "mirrors/falcone.git/objects").mkdir(parents=True)
    payload["merge_forward"] = {
        "target": "source",
        "repository": "falcone",
        "base_sha": "1" * 40,
        "from_sha": "2" * 40,
    }
    runner = _AncestryRunner(branch, heads, missing=missing)
    return payload, heads, runner, _controller_publisher(tmp_path, runner)


@pytest.mark.asyncio
async def test_publication_verifies_the_merge_in_its_own_clone_then_pushes(
    tmp_path: Path,
) -> None:
    payload, heads, runner, handler = _merge_forward_publication(tmp_path, set())

    result = await handler.handle("delivery.publish", payload)

    assert result["status"] == "SOURCE_PRS_CREATED"
    assert runner.pushes == ["falcone", "falcone-charts"]
    # Both ancestors are checked against the validated head, in the controller scratch clone.
    assert [(base, head) for _cwd, base, head in runner.ancestry] == [
        ("1" * 40, heads["falcone"]),
        ("2" * 40, heads["falcone"]),
    ]
    scratch = tmp_path / "runs/delivery/publications"
    assert all(cwd.is_relative_to(scratch) for cwd, _base, _head in runner.ancestry)
    verified = {
        item["repository_slug"]: item.get("merge_forward_verified")
        for item in result["publication_results"]
    }
    assert verified == {"falcone": True, "falcone-charts": None}


@pytest.mark.parametrize("missing", ["1" * 40, "2" * 40])
@pytest.mark.asyncio
async def test_a_head_missing_either_ancestor_is_never_pushed(tmp_path: Path, missing: str) -> None:
    payload, _heads, runner, handler = _merge_forward_publication(tmp_path, {missing})

    result = await handler.handle("delivery.publish", payload)

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "SOURCE_PR_MERGE_FORWARD_NOT_MERGED"
    assert "falcone" not in runner.pushes


def _real_git_history(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repository = tmp_path / "trusted-clone"
    repository.mkdir()
    git(["init", "-q", "-b", "main"], repository)
    git(["config", "user.name", "T"], repository)
    git(["config", "user.email", "t@invalid"], repository)
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    git(["add", "base.txt"], repository)
    git(["commit", "-q", "-m", "base"], repository)
    git(["switch", "-q", "-c", "change"], repository)
    (repository / "change.txt").write_text("change\n", encoding="utf-8")
    git(["add", "change.txt"], repository)
    git(["commit", "-q", "-m", "change"], repository)
    published = git(["rev-parse", "HEAD"], repository)
    git(["switch", "-q", "main"], repository)
    (repository / "main.txt").write_text("main moved\n", encoding="utf-8")
    git(["add", "main.txt"], repository)
    git(["commit", "-q", "-m", "main moved"], repository)
    tip = git(["rev-parse", "HEAD"], repository)
    git(["switch", "-q", "change"], repository)
    git(["merge", "-q", "--no-edit", "main"], repository)
    merged = git(["rev-parse", "HEAD"], repository)
    return repository, {"published": published, "tip": tip, "merged": merged}


def test_the_trusted_clone_ancestry_check_uses_real_git(tmp_path: Path) -> None:
    # Review 1: real ancestry, including a missing object (fails closed).
    repository, shas = _real_git_history(tmp_path)
    executable = shutil.which("git")
    assert executable is not None
    handler = DeliveryActivityHandler(
        Settings(run_root=tmp_path / "runs"),
        runner=SafeCommandRunner({"git": Path(executable), "gh": Path(executable)}),
    )
    requirement = {"target": "source", "base_sha": shas["tip"], "from_sha": shas["published"]}
    environment = {"HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0"}

    handler._require_merge_forward_ancestry(repository, environment, shas["merged"], requirement)
    # As in publication: an empty scratch clone that sees the objects only through alternates.
    scratch = tmp_path / "scratch.git"
    git(["init", "-q", "--bare", str(scratch)], tmp_path)
    borrowed = {**environment, "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(repository / ".git/objects")}
    handler._require_merge_forward_ancestry(scratch, borrowed, shas["merged"], requirement)
    with pytest.raises(MergeForwardNotMerged):
        handler._require_merge_forward_ancestry(scratch, environment, shas["merged"], requirement)
    for head, required in (
        (shas["published"], requirement),  # the base tip was never merged in
        (shas["merged"], {**requirement, "from_sha": "1" * 40}),  # unknown object
    ):
        with pytest.raises(MergeForwardNotMerged):
            handler._require_merge_forward_ancestry(repository, environment, head, required)


@pytest.mark.parametrize("contained", [True, False])
def test_a_retry_after_the_pr_merged_verifies_on_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contained: bool
) -> None:
    # Review 1: the push succeeded, its receipt was lost and the PR merged meanwhile.
    change_set_id = uuid4()
    head = "c" * 40
    plan = RepositoryPublication(
        repository="gntik-ai/falcone",
        repository_slug="falcone",
        working_directory=tmp_path,
        branch=f"agent/falcone/42/{change_set_id}",
        head_sha=head,
        title="t",
        body="b",
    )
    handler = DeliveryActivityHandler(Settings(run_root=tmp_path / "runs"))
    merged_pr = {"headRefOid": head, "baseRefName": "main", "state": "MERGED"}
    monkeypatch.setattr(handler.broker, "list_pull_requests", lambda *a, **k: [merged_pr])
    monkeypatch.setattr(handler, "_own_pull_request", lambda item, plan: True)
    monkeypatch.setattr(
        handler,
        "_bind_pull_request",
        lambda plan, context: {
            "repository_slug": "falcone",
            "head_sha": head,
            "pr_state": "MERGED",
        },
    )
    checked: list[tuple[str, str]] = []

    def commit_contains(repository: str, *, ancestor: str, descendant: str) -> bool:
        checked.append((ancestor, descendant))
        return contained

    monkeypatch.setattr(handler.broker, "commit_contains", commit_contains)
    payload = {
        "merge_forward": {
            "target": "source",
            "repository": "falcone",
            "base_sha": "1" * 40,
            "from_sha": "2" * 40,
        }
    }

    if contained:
        result = handler._publish(
            plan, UUID(str(change_set_id)), publication_context=(None, payload, {})
        )  # type: ignore[arg-type]
        assert result["merge_forward_verified"] is True
        assert checked == [("1" * 40, head), ("2" * 40, head)]
    else:
        with pytest.raises(MergeForwardNotMerged):
            handler._publish(
                plan, UUID(str(change_set_id)), publication_context=(None, payload, {})
            )  # type: ignore[arg-type]


# -- metadata: the base moves only for a verified publication ----------------------------


def _merge_forward_change_set(tmp_path: Path) -> tuple[Any, ChangeSet, dict[str, Any]]:
    handler, allocation = _lease_fixture(tmp_path)
    change_set = ChangeSet(
        source_repository="musematic",
        deployment_repository="musematic-deploy",
        source_base_sha="a" * 40,
        deployment_base_sha="c" * 40,
        source_head_sha="f" * 40,
        deployment_head_sha="c" * 40,
        recovery_json={},
    )
    merge_forward = {
        "target": "source",
        "repository": "musematic",
        "base_sha": "e" * 40,
        "from_sha": "d" * 40,
    }
    return handler, change_set, merge_forward


def test_a_verified_merge_forward_moves_the_change_set_base(tmp_path: Path) -> None:
    handler, change_set, merge_forward = _merge_forward_change_set(tmp_path)
    results = {"musematic": {"head_sha": "f" * 40, "merge_forward_verified": True}}

    handler._apply_merge_forward(change_set, {"source_base_sha": "e" * 40}, merge_forward, results)
    handler._apply_merge_forward(change_set, {"source_base_sha": "e" * 40}, merge_forward, results)

    assert change_set.source_base_sha == "e" * 40
    assert change_set.deployment_base_sha == "c" * 40
    history = change_set.recovery_json["merge_forwards"]
    assert [(item["from_sha"], item["base_sha"], item["head_sha"]) for item in history] == [
        ("d" * 40, "e" * 40, "f" * 40)
    ]


@pytest.mark.parametrize(
    "results",
    [
        {"musematic": {"head_sha": "f" * 40}},
        {"musematic": {"head_sha": "9" * 40, "merge_forward_verified": True}},
        {},
    ],
)
def test_an_unverified_merge_forward_never_moves_the_base(
    tmp_path: Path, results: dict[str, Any]
) -> None:
    handler, change_set, merge_forward = _merge_forward_change_set(tmp_path)

    with pytest.raises(ValueError, match="not verified"):
        handler._apply_merge_forward(
            change_set, {"source_base_sha": "e" * 40}, merge_forward, results
        )

    assert change_set.source_base_sha == "a" * 40


@pytest.mark.parametrize(
    ("pr_state", "verified", "accepted"),
    [
        ("OPEN", True, True),  # the push path
        ("MERGED", True, True),  # review 2: a retry after the PR merged, self-contained
        ("MERGED", False, False),  # a merged PR without verified ancestry stays refused
    ],
)
@pytest.mark.asyncio
async def test_a_self_contained_merge_forward_persists_its_new_base(
    tmp_path: Path, pr_state: str, verified: bool, accepted: bool
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'merge-forward.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    change_set_id, run_id = uuid4(), uuid4()
    tip, published, head = "e" * 40, "d" * 40, "b" * 40
    pr_url = "https://github.com/gntik-ai/gntik-ui/pull/42"
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="gntik-ui",
                risk_class="R1",
                state="IMPLEMENTING",
                version=1,
                source_repository="gntik-ui",
                source_branch=f"agent/gntik-ui/26/{change_set_id}",
                source_base_sha="a" * 40,
                source_head_sha=published,
                source_pr_url=pr_url,
                deployment_repository="gntik-ui",
                deployment_branch=None,
                deployment_base_sha="a" * 40,
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
                workflow_id="delivery-merge-forward",
                run_type="issue-delivery",
                status="RUNNING",
                project="gntik-ui",
            )
        )
    branch = f"agent/gntik-ui/26/{change_set_id}"
    evidence = _self_contained_evidence(head, deployment_floor="R1")
    for side in ("source", "deployment"):
        evidence[side] = {**evidence[side], "base_sha": tip}  # type: ignore[dict-item]
    result_record: dict[str, Any] = {
        "repository_slug": "gntik-ui",
        "head_sha": head,
        "pr_state": pr_state,
        "pr_url": pr_url,
    }
    if verified:
        result_record["merge_forward_verified"] = True
    request = {
        "change_set_id": str(change_set_id),
        "run_id": str(run_id),
        "status": "SOURCE_PRS_CREATED",
        "source_base_sha": tip,
        "deployment_base_sha": tip,
        "merge_forward": {
            "target": "source",
            "repository": "gntik-ui",
            "base_sha": tip,
            "from_sha": published,
        },
        "publication_plan": {
            "change_set_id": str(change_set_id),
            "publications": [{"repository_slug": "gntik-ui", "branch": branch, "head_sha": head}],
        },
        "publication_results": [result_record],
        "change_evidence": evidence,
        "agent_result": checker_result(),
        "maker_provider": "codex",
        "checker_provider": "claude",
    }

    if not accepted:
        with pytest.raises(Exception, match="invalid metadata activity input"):
            await handler.handle("delivery.persist_publication", request)
        with handler.sessions() as session:
            change_set = session.get(ChangeSet, change_set_id)
            assert change_set is not None and change_set.source_base_sha == "a" * 40
        return
    result = await handler.handle("delivery.persist_publication", request)

    assert result["status"] == "SOURCE_PR"
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, change_set_id)
        assert change_set is not None
        assert (change_set.source_base_sha, change_set.deployment_base_sha) == (tip, tip)
        assert change_set.source_head_sha == head
        history = change_set.recovery_json["merge_forwards"]
    assert [(item["from_sha"], item["base_sha"], item["head_sha"]) for item in history] == [
        (published, tip, head)
    ]


def _published_by_merged_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    slug: str,
    change_set_id: UUID,
    head: str,
    pr_url: str,
    merge_forward: dict[str, str],
) -> dict[str, Any]:
    """The source result _publish really returns for a merged retry (GitHub stubbed)."""
    project = "gntik-ui" if slug == "gntik-ui" else "falcone"
    plan = RepositoryPublication(
        repository=f"gntik-ai/{slug}",
        repository_slug=slug,
        working_directory=tmp_path,
        branch=f"agent/{project}/26/{change_set_id}",
        head_sha=head,
        title="t",
        body="b",
    )
    handler = DeliveryActivityHandler(Settings(run_root=tmp_path / "runs"))
    merged_pr = {"headRefOid": head, "baseRefName": "main", "state": "MERGED"}
    monkeypatch.setattr(handler.broker, "list_pull_requests", lambda *a, **k: [merged_pr])
    monkeypatch.setattr(handler, "_own_pull_request", lambda item, plan: True)
    monkeypatch.setattr(
        handler,
        "_bind_pull_request",
        lambda plan, context: {
            "repository_slug": slug,
            "head_sha": head,
            "pr_state": "MERGED",
            "pr_url": pr_url,
        },
    )
    monkeypatch.setattr(handler.broker, "commit_contains", lambda *a, **k: True)
    payload = {"merge_forward": merge_forward}
    return handler._publish(plan, change_set_id, publication_context=(None, payload, {}))  # type: ignore[arg-type]


@pytest.mark.parametrize("shape", ["self-contained", "paired", "deferred"])
@pytest.mark.asyncio
async def test_a_merged_retry_persists_through_publication_for_every_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    # Review 3: real _publish output, not an injected flag, for all three repository shapes.
    handler = MetadataActivityHandler(Settings(database_url=f"sqlite:///{tmp_path / 'e2e.sqlite'}"))
    Base.metadata.create_all(handler.engine)
    change_set_id, run_id = uuid4(), uuid4()
    tip, published, head, deployment_head = "e" * 40, "d" * 40, "b" * 40, "c" * 40
    slug = "gntik-ui" if shape == "self-contained" else "falcone"
    deployment_slug = "gntik-ui" if shape == "self-contained" else "falcone-charts"
    pr_url = f"https://github.com/gntik-ai/{slug}/pull/42"
    deployment_url = "https://github.com/gntik-ai/falcone-charts/pull/7"
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="gntik-ui" if shape == "self-contained" else "falcone",
                risk_class="R1",
                state="IMPLEMENTING",
                version=1,
                source_repository=slug,
                source_branch=None,
                source_base_sha="a" * 40,
                source_head_sha=published,
                source_pr_url=pr_url,
                deployment_repository=deployment_slug,
                deployment_branch=None,
                deployment_base_sha="a" * 40 if shape == "self-contained" else deployment_head,
                deployment_head_sha=deployment_head if shape == "paired" else None,
                deployment_pr_url=deployment_url if shape == "paired" else None,
                image_digests={},
                chart_version=None,
                recovery_json={},
            )
        )
        session.add(
            Run(
                id=run_id,
                change_set_id=change_set_id,
                workflow_id="delivery-e2e",
                run_type="issue-delivery",
                status="RUNNING",
                project="gntik-ui" if shape == "self-contained" else "falcone",
            )
        )
    merge_forward = {"target": "source", "repository": slug, "base_sha": tip, "from_sha": published}
    source_record = _published_by_merged_retry(
        tmp_path,
        monkeypatch,
        slug=slug,
        change_set_id=change_set_id,
        head=head,
        pr_url=pr_url,
        merge_forward=merge_forward,
    )
    project = "gntik-ui" if shape == "self-contained" else "falcone"
    branch = f"agent/{project}/26/{change_set_id}"
    publications = [{"repository_slug": slug, "branch": branch, "head_sha": head}]
    results = [source_record]
    plan: dict[str, Any] = {"change_set_id": str(change_set_id), "publications": publications}
    if shape == "self-contained":
        evidence = _self_contained_evidence(head, deployment_floor="R1")
        for side in ("source", "deployment"):
            evidence[side] = {**evidence[side], "base_sha": tip}  # type: ignore[dict-item]
        bases = {"source_base_sha": tip, "deployment_base_sha": tip}
    else:
        evidence = publication_evidence(head, deployment_head, deployment_changed=shape == "paired")
        evidence["source"] = {**evidence["source"], "base_sha": tip}  # type: ignore[dict-item]
        bases = {"source_base_sha": tip}
        if shape == "paired":
            publications.append(
                {"repository_slug": "falcone-charts", "branch": branch, "head_sha": deployment_head}
            )
            results.append(
                {
                    "repository_slug": "falcone-charts",
                    "head_sha": deployment_head,
                    "pr_state": "OPEN",
                    "pr_url": deployment_url,
                }
            )
        else:
            plan["deferred_deployment"] = {
                "repository_slug": "falcone-charts",
                "branch": branch,
                "head_sha": deployment_head,
                "base_sha": deployment_head,
                "reason": "AWAITING_IMMUTABLE_ARTIFACT",
            }

    result = await handler.handle(
        "delivery.persist_publication",
        {
            "change_set_id": str(change_set_id),
            "run_id": str(run_id),
            "status": "SOURCE_PRS_CREATED",
            **bases,
            "merge_forward": merge_forward,
            "publication_plan": plan,
            "publication_results": results,
            "change_evidence": evidence,
            "agent_result": checker_result(),
            "maker_provider": "codex",
            "checker_provider": "claude",
        },
    )

    assert source_record["merge_forward_verified"] is True
    assert result["status"] == "SOURCE_PR"
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, change_set_id)
        assert change_set is not None
        assert change_set.source_base_sha == tip
        expected_deployment_base = tip if shape == "self-contained" else deployment_head
        assert change_set.deployment_base_sha == expected_deployment_base
        assert [item["head_sha"] for item in change_set.recovery_json["merge_forwards"]] == [head]


# -- the loop -----------------------------------------------------------------------------

_BEHIND = {"status": "BLOCKED", "blocker": "SOURCE_PR_BEHIND_BASE"}


async def test_a_pr_behind_its_base_is_merged_forward_by_repair_rounds() -> None:
    history, result, children = await _run_loop(
        _full_delivery_loop_script(**{"gitops.pr_checks": _BEHIND}),
        [("skip", "gntik-ai/musematic#1026|OPERATOR")],
    )

    assert result["status"] == "COMPLETE"
    assert [child.rsplit("-issue-", 1)[1] for child in children] == [
        "1-attempt-1",
        "1-staging-gitops",
        "1-ci-repair-1-attempt-1",
        "1-staging-gitops-retry-1",
        "1-ci-repair-2-attempt-1",
        "1-staging-gitops-retry-2",
    ]
    steps = _scheduled_steps(history)
    paused = [
        request["payload"]["blocker"]
        for request in steps
        if request["step"] == "backlog.record_loop_state"
        and request["payload"].get("loop_status") == "PAUSED"
    ]
    assert paused == ["SOURCE_PR_BEHIND_BASE"]
    names = [request["step"] for request in steps]
    repairs = [index for index, name in enumerate(names) if name == "backlog.record_ci_repair"]
    assert len(repairs) == 2
    # Each round refreshes the canonical mirrors first: the maker merges the new tip.
    for position in repairs:
        assert names[position + 1 : position + 3] == [
            "github.refresh_mirrors",
            "backlog.register_child_run",
        ]
    await Replayer(workflows=_LOOP_WORKFLOWS).replay_workflow(history)


@workflow.defn(name="IssueDeliveryWorkflow", sandboxed=False)
class _UnmergedRepairStub:
    """IssueDeliveryWorkflow whose merge-forward round was refused at publication."""

    @workflow.run
    async def run(self, request: IssueDeliveryInput) -> dict[str, Any]:
        if "-ci-repair-" in workflow.info().workflow_id:
            return {
                "status": "BLOCKED",
                "context": {"status": "BLOCKED", "blocker": "SOURCE_PR_MERGE_FORWARD_NOT_MERGED"},
            }
        return {"status": "SOURCE_PR", "context": {"change_set_id": _GITOPS_PLAN["change_set_id"]}}


async def test_an_unmerged_round_pauses_with_its_named_blocker() -> None:
    workflows = [
        ExistingGitHubBacklogLoopWorkflow,
        _UnmergedRepairStub,
        StagingGitOpsDeliveryWorkflow,
    ]
    script = _full_delivery_loop_script(**{"gitops.pr_checks": _BEHIND})
    scripted = _scripted_execute_control_step(script)

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        if request["step"] == "backlog.restore_published_change_set":
            if request["payload"]["phase"] == "plan":
                return {"status": "READY", "version": 1, "pull_requests": [{"url": "u"}]}
            return {"status": "RESTORED"}
        if request["step"] == "gitops.pr_heads":
            return {"status": "OK", "heads": [{"url": "u"}]}
        return await scripted(request)

    history, result, _children = await _run_loop(
        script,
        [("skip", "gntik-ai/musematic#1026|OPERATOR")],
        workflows=workflows,
        activity_fn=execute_control_step,
    )

    assert result["status"] == "COMPLETE"
    paused = [
        request["payload"]["blocker"]
        for request in _scheduled_steps(history)
        if request["step"] == "backlog.record_loop_state"
        and request["payload"].get("loop_status") == "PAUSED"
    ]
    assert paused == ["CI_REPAIR_SOURCE_PR_MERGE_FORWARD_NOT_MERGED"]
    await Replayer(workflows=workflows).replay_workflow(history)
