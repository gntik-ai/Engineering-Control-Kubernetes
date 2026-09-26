import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from engineering_control.worktrees import manager as manager_module
from engineering_control.worktrees.manager import (
    GitWorktreeManager,
    WorktreeAccess,
    WorktreeError,
)

GIT = shutil.which("git")
assert GIT is not None


def git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(  # noqa: S603 - fixed executable and test-controlled arguments
        [GIT, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def create_mirror(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    git(["init", "-b", "main"], source)
    git(["config", "user.name", "Test"], source)
    git(["config", "user.email", "test@invalid"], source)
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    git(["add", "README.md"], source)
    git(["commit", "-m", "fixture"], source)
    sha = git(["rev-parse", "HEAD"], source)
    mirror_root = tmp_path / "mirrors"
    mirror_root.mkdir()
    subprocess.run(  # noqa: S603 - fixed executable and test-controlled arguments
        [GIT, "clone", "--mirror", str(source), str(mirror_root / "falcone.git")],
        check=True,
        capture_output=True,
    )
    return mirror_root, sha


def test_allocate_and_cleanup_clean_worktree(tmp_path: Path) -> None:
    mirror_root, sha = create_mirror(tmp_path)
    manager = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
    )
    allocation = manager.allocate(
        repository="falcone",
        project="falcone",
        issue="123",
        change_set_id=uuid4(),
        base_ref="refs/heads/main",
        expected_base_sha=sha,
    )
    assert allocation.path.is_dir()
    assert allocation.branch.startswith("agent/falcone/123/")
    manager.cleanup(allocation, evidence_captured=False)
    assert not allocation.path.exists()


def test_allocation_retry_reuses_only_same_clean_changeset(tmp_path: Path) -> None:
    mirror_root, sha = create_mirror(tmp_path)
    manager = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
    )
    change_set_id = uuid4()
    first = manager.allocate(
        repository="falcone",
        project="falcone",
        issue="125",
        change_set_id=change_set_id,
        base_ref="refs/heads/main",
        expected_base_sha=sha,
    )
    second = manager.allocate(
        repository="falcone",
        project="falcone",
        issue="125",
        change_set_id=change_set_id,
        base_ref="refs/heads/main",
        expected_base_sha=sha,
    )
    assert second == first


def test_changeset_git_metadata_isolated_from_canonical_mirror(tmp_path: Path) -> None:
    mirror_root, sha = create_mirror(tmp_path)
    manager = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
    )
    change_set_id = uuid4()
    allocation = manager.allocate(
        repository="falcone",
        project="falcone",
        issue="isolated",
        change_set_id=change_set_id,
        base_ref="refs/heads/main",
        expected_base_sha=sha,
    )
    metadata = manager.metadata_path("falcone", change_set_id)
    canonical = mirror_root / "falcone.git"
    alternates = metadata / "objects" / "info" / "alternates"
    assert metadata.is_dir()
    assert alternates.is_file()
    assert (
        Path(alternates.read_text(encoding="utf-8").strip()).resolve()
        == (canonical / "objects").resolve()
    )

    git(["config", "user.name", "Bounded Maker"], allocation.path)
    git(["config", "user.email", "maker@invalid"], allocation.path)
    (allocation.path / "bounded.txt").write_text("isolated\n", encoding="utf-8")
    git(["add", "bounded.txt"], allocation.path)
    git(["commit", "-m", "bounded change"], allocation.path)
    head = git(["rev-parse", "HEAD"], allocation.path)

    assert git(["rev-parse", allocation.branch], metadata) == head
    canonical_ref = subprocess.run(  # noqa: S603 - fixed test command
        [GIT, "show-ref", "--verify", f"refs/heads/{allocation.branch}"],
        cwd=canonical,
        check=False,
        capture_output=True,
    )
    assert canonical_ref.returncode != 0


def test_dirty_worktree_is_protected(tmp_path: Path) -> None:
    mirror_root, sha = create_mirror(tmp_path)
    manager = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
    )
    allocation = manager.allocate(
        repository="falcone",
        project="falcone",
        issue="124",
        change_set_id=uuid4(),
        base_ref="refs/heads/main",
        expected_base_sha=sha,
    )
    (allocation.path / "dirty.txt").write_text("protected\n", encoding="utf-8")
    with pytest.raises(WorktreeError, match="protected"):
        manager.cleanup(allocation, evidence_captured=False)
    assert allocation.path.exists()


def test_change_evidence_includes_tracked_and_untracked_files(tmp_path: Path) -> None:
    mirror_root, sha = create_mirror(tmp_path)
    manager = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
    )
    allocation = manager.allocate(
        repository="falcone",
        project="falcone",
        issue="126",
        change_set_id=uuid4(),
        base_ref="refs/heads/main",
        expected_base_sha=sha,
    )
    (allocation.path / "README.md").write_text("changed\n", encoding="utf-8")
    generated = allocation.path / "generated"
    generated.mkdir()
    (generated / "evidence.txt").write_text("new\n", encoding="utf-8")

    evidence, diff = manager.change_evidence(allocation.path, base_sha=sha)

    assert evidence["changed_file_count"] == 2
    assert evidence["changed_paths"] == ["README.md", "generated/evidence.txt"]
    assert evidence["diff_sha256"] == hashlib.sha256(diff.encode()).hexdigest()
    assert evidence["untracked"] == [
        {
            "path": "generated/evidence.txt",
            "sha256": hashlib.sha256(b"new\n").hexdigest(),
            "size_bytes": 4,
        }
    ]


def test_maker_and_checker_must_be_independent() -> None:
    with pytest.raises(ValueError, match="must differ"):
        WorktreeAccess("codex-runner", "codex-runner")


def test_only_model_worker_identities_receive_worktree_access() -> None:
    with pytest.raises(ValueError, match="allowed model worker"):
        WorktreeAccess("release-runner", "claude-runner")


def test_worktree_manager_never_receives_github_credentials() -> None:
    manager = GitWorktreeManager()

    assert manager._fetch_environment() == {"GIT_TERMINAL_PROMPT": "0"}
    assert not any("token" in name or "askpass" in name for name in vars(manager))


def test_maker_acl_covers_worktree_traversal_and_isolated_git_metadata(
    tmp_path: Path,
) -> None:
    worktree_root = tmp_path / "worktrees"
    target = worktree_root / "falcone" / str(uuid4())
    target.mkdir(parents=True)
    metadata_root = tmp_path / "cache" / "scan" / "worktree-git"
    metadata = metadata_root / "falcone" / f"{target.name}.git"
    metadata.mkdir(parents=True)
    mirror_root = tmp_path / "mirrors"
    canonical = mirror_root / "falcone.git"
    canonical.mkdir(parents=True)
    manager = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=worktree_root,
        lease_root=tmp_path / "leases",
        metadata_root=metadata_root,
    )
    commands: list[list[str]] = []

    def record(args: list[str], cwd: Path, *, timeout: int = 300) -> str:
        commands.append(args)
        return ""

    manager._run = record  # type: ignore[method-assign]
    manager._grant_access(
        target,
        metadata,
        canonical,
        WorktreeAccess("codex-runner", "claude-runner"),
    )

    assert any(
        command[-1] == str(target.parent)
        and any("u:codex-runner:--x" in argument for argument in command)
        for command in commands
    )
    assert any(
        command[-1] == str(metadata)
        and any("u:codex-runner:rwX" in argument for argument in command)
        for command in commands
    )
    assert any(
        command[-1] == str(canonical)
        and any("u:codex-runner:r-X" in argument for argument in command)
        for command in commands
    )
    # NEW-56: files the maker creates inherit read for the publishing identity.
    assert any(
        command[-1] == str(target) and any("d:u:delivery-runner:r-x" in a for a in command)
        for command in commands
    )
    assert not any("release-runner" in a for command in commands for a in command)

    commands.clear()
    manager._grant_access(
        target,
        metadata,
        canonical,
        WorktreeAccess("codex-runner", "claude-runner"),
        repository="musematic-deploy",
    )
    assert any(
        command[-1] == str(target) and any("d:u:release-runner:r-x" in a for a in command)
        for command in commands
    )


def test_release_read_is_limited_to_exact_deployment_worktree(tmp_path: Path) -> None:
    mirror_root, sha = create_mirror(tmp_path)
    source_mirror = mirror_root / "falcone.git"
    deployment_mirror = mirror_root / "falcone-charts.git"
    subprocess.run(  # noqa: S603 - fixed executable and test-controlled arguments
        [GIT, "clone", "--mirror", str(source_mirror), str(deployment_mirror)],
        check=True,
        capture_output=True,
    )
    manager = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
    )
    allocation = manager.allocate(
        repository="falcone-charts",
        project="falcone",
        issue="127",
        change_set_id=uuid4(),
        base_ref="refs/heads/main",
        expected_base_sha=sha,
    )
    commands: list[list[str]] = []
    original = manager._run

    def record(args: list[str], cwd: Path, *, timeout: int = 300) -> str:
        if args[0] == "setfacl":
            commands.append(args)
            return ""
        return original(args, cwd, timeout=timeout)

    manager._run = record  # type: ignore[method-assign]
    manager.grant_release_read("falcone-charts", allocation.path)
    assert any(str(allocation.path) in command for command in commands)
    assert any(str(deployment_mirror) in command for command in commands)
    with pytest.raises(WorktreeError, match="deployment repositories"):
        manager.grant_release_read("falcone", allocation.path)
    with pytest.raises(WorktreeError, match="exact deployment worktree"):
        manager.grant_release_read("falcone-charts", tmp_path)


def test_retry_archives_and_resets_a_stopped_attempts_worktree(tmp_path: Path) -> None:
    # NEW-38: musematic #1026's Codex maker left 9 uncommitted files; every later retry
    # then failed at allocation ("existing worktree is dirty and will not be reused").
    mirror_root, sha = create_mirror(tmp_path)
    manager = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
    )
    change_set_id = uuid4()
    request = {
        "repository": "falcone",
        "project": "falcone",
        "issue": "1026",
        "change_set_id": change_set_id,
        "base_ref": "refs/heads/main",
        "expected_base_sha": sha,
    }
    first = manager.allocate(**request)
    git(
        [
            "-c",
            "user.name=m",
            "-c",
            "user.email=m@invalid",
            "commit",
            "--allow-empty",
            "-m",
            "maker commit",
        ],
        first.path,
    )
    ahead = git(["rev-parse", "HEAD"], first.path)
    (first.path / "README.md").write_text("maker edit\n", encoding="utf-8")
    (first.path / "new_benchmark.py").write_text("print('x')\n", encoding="utf-8")

    second = manager.allocate(**request)

    assert second.path == first.path
    assert git(["rev-parse", "HEAD"], second.path) == sha
    assert git(["status", "--porcelain=v1"], second.path) == ""
    archived = git(
        ["for-each-ref", "--format=%(refname)", f"refs/archive/{change_set_id}"], second.path
    ).splitlines()
    head_ref = next(ref for ref in archived if ref.endswith("-head"))
    work_ref = next(ref for ref in archived if ref.endswith("-worktree"))
    assert git(["rev-parse", head_ref], second.path) == ahead
    assert "maker edit" in git(["show", f"{work_ref}:README.md"], second.path)
    assert "print('x')" in git(["show", f"{work_ref}^3:new_benchmark.py"], second.path)


def _resume_fixture(tmp_path: Path) -> tuple[GitWorktreeManager, dict[str, object], Path, str]:
    mirror_root, sha = create_mirror(tmp_path)
    manager = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
    )
    request: dict[str, object] = {
        "repository": "falcone",
        "project": "falcone",
        "issue": "993",
        "change_set_id": uuid4(),
        "base_ref": "refs/heads/main",
        "expected_base_sha": sha,
    }
    first = manager.allocate(**request)  # type: ignore[arg-type]
    (first.path / "feature.py").write_text("print('previous attempt')\n", encoding="utf-8")
    git(["add", "feature.py"], first.path)
    git(["commit", "-q", "-m", "previous attempt"], first.path)
    return manager, request, first.path, sha


def test_retry_resumes_from_the_previous_attempts_commits(tmp_path: Path) -> None:
    # NEW-52: musematic #993 needed 11 rounds / ~60 attempts because every correction
    # was reset to the base (NEW-38) and re-implemented the whole feature.
    manager, request, path, sha = _resume_fixture(tmp_path)
    head = git(["rev-parse", "HEAD"], path)

    second = manager.allocate(**request, resume_prior_commits=True)  # type: ignore[arg-type]

    assert second.path == path
    assert second.base_sha == sha
    assert second.resumed_from_sha == head
    assert git(["rev-parse", "HEAD"], path) == head
    assert (path / "feature.py").read_text(encoding="utf-8") == "print('previous attempt')\n"
    assert git(["status", "--porcelain=v1"], path) == ""
    change_set_id = request["change_set_id"]
    assert git(["for-each-ref", f"refs/archive/{change_set_id}"], path) == ""


def test_dirty_resume_archives_leftovers_then_resets_to_the_attempts_head(
    tmp_path: Path,
) -> None:
    manager, request, path, sha = _resume_fixture(tmp_path)
    head = git(["rev-parse", "HEAD"], path)
    (path / "feature.py").write_text("half-finished edit\n", encoding="utf-8")
    (path / "scratch.txt").write_text("untracked\n", encoding="utf-8")

    second = manager.allocate(**request, resume_prior_commits=True)  # type: ignore[arg-type]

    assert second.resumed_from_sha == head
    assert git(["rev-parse", "HEAD"], path) == head
    assert git(["status", "--porcelain=v1"], path) == ""
    assert (path / "feature.py").read_text(encoding="utf-8") == "print('previous attempt')\n"
    assert not (path / "scratch.txt").exists()
    archived = git(
        ["for-each-ref", "--format=%(refname)", f"refs/archive/{request['change_set_id']}"],
        path,
    ).splitlines()
    work_ref = next(ref for ref in archived if ref.endswith("-worktree"))
    assert "half-finished edit" in git(["show", f"{work_ref}:feature.py"], path)
    assert "untracked" in git(["show", f"{work_ref}^3:scratch.txt"], path)


def _advance_main(tmp_path: Path) -> str:
    source = tmp_path / "source"
    (source / "CHANGELOG.md").write_text("main moved\n", encoding="utf-8")
    git(["add", "CHANGELOG.md"], source)
    git(["commit", "-q", "-m", "main moved"], source)
    # The GitHub worker refreshes the canonical mirror in production.
    git(
        ["fetch", "-q", "origin", "+refs/heads/*:refs/heads/*"],
        tmp_path / "mirrors" / "falcone.git",
    )
    return git(["rev-parse", "HEAD"], source)


def test_ci_repair_resume_tolerates_main_moving_ahead(tmp_path: Path) -> None:
    # NEW-64: a CI-repair re-delivery resumes the published branch after main moved on;
    # it keeps the imported base (no rebase) instead of failing on a base mismatch.
    manager, request, path, sha = _resume_fixture(tmp_path)
    head = git(["rev-parse", "HEAD"], path)
    _advance_main(tmp_path)

    second = manager.allocate(**request, resume_prior_commits=True)  # type: ignore[arg-type]

    assert second.base_sha == sha
    assert second.resumed_from_sha == head
    assert git(["rev-parse", "HEAD"], path) == head


def test_moved_main_still_refuses_fresh_or_non_resumed_allocations(tmp_path: Path) -> None:
    manager, request, _path, _sha = _resume_fixture(tmp_path)
    _advance_main(tmp_path)

    with pytest.raises(WorktreeError, match="base SHA mismatch"):
        manager.allocate(**request, resume_prior_commits=False)  # type: ignore[arg-type]
    fresh = {**request, "change_set_id": uuid4()}
    with pytest.raises(WorktreeError, match="base SHA mismatch"):
        manager.allocate(**fresh, resume_prior_commits=True)  # type: ignore[arg-type]


def test_resume_falls_back_to_the_base_when_head_does_not_descend_from_it(
    tmp_path: Path,
) -> None:
    manager, request, path, sha = _resume_fixture(tmp_path)
    # Foreign history: an orphan commit that does not contain the base.
    git(["checkout", "-q", "--orphan", "tmp-orphan"], path)
    git(["commit", "-q", "-m", "foreign"], path)
    foreign = git(["rev-parse", "HEAD"], path)
    branch = f"agent/falcone/993/{request['change_set_id']}"
    git(["checkout", "-q", "-B", branch, foreign], path)

    second = manager.allocate(**request, resume_prior_commits=True)  # type: ignore[arg-type]

    assert second.resumed_from_sha is None
    assert git(["rev-parse", "HEAD"], path) == sha
    assert not (path / "feature.py").exists()


def test_resume_disabled_keeps_the_new38_reset_to_base(tmp_path: Path) -> None:
    manager, request, path, sha = _resume_fixture(tmp_path)
    head = git(["rev-parse", "HEAD"], path)

    second = manager.allocate(**request, resume_prior_commits=False)  # type: ignore[arg-type]

    assert second.resumed_from_sha is None
    assert git(["rev-parse", "HEAD"], path) == sha
    archived = git(
        ["for-each-ref", "--format=%(objectname)", f"refs/archive/{request['change_set_id']}"],
        path,
    )
    assert head in archived


def test_retry_still_refuses_a_worktree_on_another_branch(tmp_path: Path) -> None:
    mirror_root, sha = create_mirror(tmp_path)
    manager = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
    )
    request = {
        "repository": "falcone",
        "project": "falcone",
        "issue": "1026",
        "change_set_id": uuid4(),
        "base_ref": "refs/heads/main",
        "expected_base_sha": sha,
    }
    first = manager.allocate(**request)
    git(["checkout", "-q", "-b", "somewhere-else"], first.path)

    with pytest.raises(WorktreeError, match="does not match the ChangeSet allocation"):
        manager.allocate(**request)


def test_manager_keeps_access_to_what_the_maker_creates(tmp_path: Path) -> None:
    # NEW-38b: archiving a stopped attempt failed with "Permission denied" on
    # openspec/changes/... -- directories the Codex maker created were unreadable to
    # the scan worker that manages the worktree.
    import os
    import pwd

    worktree_root = tmp_path / "worktrees"
    target = worktree_root / "musematic" / str(uuid4())
    target.mkdir(parents=True)
    metadata_root = tmp_path / "cache" / "scan" / "worktree-git"
    metadata = metadata_root / "musematic" / f"{target.name}.git"
    metadata.mkdir(parents=True)
    canonical = tmp_path / "mirrors" / "musematic.git"
    canonical.mkdir(parents=True)
    manager = GitWorktreeManager(
        mirror_root=tmp_path / "mirrors",
        worktree_root=worktree_root,
        lease_root=tmp_path / "leases",
        metadata_root=metadata_root,
    )
    commands: list[list[str]] = []

    def record(args: list[str], cwd: Path, *, timeout: int = 300) -> str:
        commands.append(args)
        return ""

    manager._run = record  # type: ignore[method-assign]
    manager._grant_access(
        target, metadata, canonical, WorktreeAccess("codex-runner", "claude-runner")
    )

    me = pwd.getpwuid(os.geteuid()).pw_name
    for path in (target, metadata):
        acls = [command for command in commands if command[-1] == str(path)]
        assert any(f"u:{me}:rwX" in argument for command in acls for argument in command)
        assert any(f"d:u:{me}:rwx" in argument for command in acls for argument in command)


def test_allocated_worktree_has_a_commit_identity(tmp_path: Path) -> None:
    # NEW-40: musematic #993's corrected implementation was staged but the maker could
    # not commit it -- "Git has no configured author identity".
    mirror_root, sha = create_mirror(tmp_path)
    manager = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
    )
    allocation = manager.allocate(
        repository="falcone",
        project="falcone",
        issue="993",
        change_set_id=uuid4(),
        base_ref="refs/heads/main",
        expected_base_sha=sha,
    )
    (allocation.path / "feature.txt").write_text("x\n", encoding="utf-8")
    git(["add", "feature.txt"], allocation.path)
    subprocess.run(  # noqa: S603 - fixed executable and test-controlled arguments
        [GIT, "commit", "-q", "-m", "maker"],
        cwd=allocation.path,
        check=True,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "empty-home")},
    )
    assert git(["log", "-1", "--format=%an <%ae>"], allocation.path) == (
        "Engineering Control <engineering-control@local.invalid>"
    )


def _eperm_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lines: list[str]
) -> GitWorktreeManager:
    """A manager whose setfacl is refused on the given lines, running as another uid."""
    manager = GitWorktreeManager(
        mirror_root=tmp_path / "mirrors",
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
    )

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "\n".join(lines) + "\n")

    monkeypatch.setattr(manager_module.subprocess, "run", fake_run)

    def refused(args: list[str], cwd: Path, **kwargs: object) -> str:
        raise WorktreeError("Git command failed (1): truncated")

    manager._run = refused  # type: ignore[method-assign]
    monkeypatch.setattr(manager_module.os, "geteuid", lambda: os.getuid() + 12345)
    return manager


def test_read_grant_accepts_foreign_files_that_are_already_world_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEW-55: maker-owned files refuse setfacl but are readable through 'other'."""
    tree = tmp_path / "tree"
    (tree / "dir").mkdir(parents=True)
    os.chmod(tree / "dir", 0o755)  # noqa: S103 - the world-readable case under test
    (tree / "dir" / "file.py").write_text("x", encoding="utf-8")
    os.chmod(tree / "dir" / "file.py", 0o644)
    manager = _eperm_manager(
        tmp_path,
        monkeypatch,
        [
            f"setfacl: {tree / 'dir'}: Operation not permitted",
            f"setfacl: {tree / 'dir' / 'file.py'}: Operation not permitted",
        ],
    )

    manager._grant_tree_read_access(tree, ("delivery-runner",), cwd=tree)


def test_read_grant_still_fails_for_unreadable_or_other_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    private = tree / "private.env"
    private.write_text("x", encoding="utf-8")
    os.chmod(private, 0o600)
    manager = _eperm_manager(
        tmp_path, monkeypatch, [f"setfacl: {private}: Operation not permitted"]
    )
    with pytest.raises(WorktreeError, match="cannot grant read access"):
        manager._grant_tree_read_access(tree, ("delivery-runner",), cwd=tree)

    other = _eperm_manager(tmp_path, monkeypatch, ["setfacl: /x: No such file or directory"])
    with pytest.raises(WorktreeError, match="setfacl failed"):
        other._grant_tree_read_access(tree, ("delivery-runner",), cwd=tree)


def test_read_grant_accepts_foreign_files_whose_acl_already_grants_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEW-56: maker files are 660 but inherit u:delivery-runner:r-x from the default ACL."""
    tree = tmp_path / "tree"
    tree.mkdir()
    owned = tree / "tasks.md"
    owned.write_text("x", encoding="utf-8")
    os.chmod(owned, 0o660)
    manager = _eperm_manager(tmp_path, monkeypatch, [f"setfacl: {owned}: Operation not permitted"])
    acl = "user::rw-\nuser:delivery-runner:r-x\t#effective:r--\ngroup::rw-\nmask::rw-\n"

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "getfacl":
            return subprocess.CompletedProcess(args, 0, acl, "")
        return subprocess.CompletedProcess(
            args, 1, "", f"setfacl: {owned}: Operation not permitted\n"
        )

    monkeypatch.setattr(manager_module.subprocess, "run", fake_run)
    manager._grant_tree_read_access(tree, ("delivery-runner",), cwd=tree)

    missing = "user::rw-\ngroup::rw-\nmask::rw-\n"
    monkeypatch.setattr(
        manager_module.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            0 if args[0] == "getfacl" else 1,
            missing if args[0] == "getfacl" else "",
            "" if args[0] == "getfacl" else f"setfacl: {owned}: Operation not permitted\n",
        ),
    )
    with pytest.raises(WorktreeError, match="cannot grant read access"):
        manager._grant_tree_read_access(tree, ("delivery-runner",), cwd=tree)


def test_publication_transport_identity_reads_only_changeset_git_metadata(
    tmp_path: Path,
) -> None:
    """NEW-61: the controller fetches validated commits over SSH as the operator.

    (Not "hermes" here: the tests run as hermes, which is the manager identity.)
    """
    worktree_root = tmp_path / "worktrees"
    target = worktree_root / "falcone" / str(uuid4())
    target.mkdir(parents=True)
    metadata_root = tmp_path / "cache" / "scan" / "worktree-git"
    metadata = metadata_root / "falcone" / f"{target.name}.git"
    (metadata / "objects").mkdir(parents=True)
    mirror_root = tmp_path / "mirrors"
    canonical = mirror_root / "falcone.git"
    canonical.mkdir(parents=True)
    manager = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=worktree_root,
        lease_root=tmp_path / "leases",
        metadata_root=metadata_root,
        publication_transport_identity="transport",
    )
    commands: list[list[str]] = []

    def record(args: list[str], cwd: Path, *, timeout: int = 300) -> str:
        commands.append(args)
        return ""

    manager._run = record  # type: ignore[method-assign]
    manager._grant_access(
        target, metadata, canonical, WorktreeAccess("codex-runner", "claude-runner")
    )

    def grants(path: Path, entry: str) -> bool:
        return any(
            command[0] == "setfacl" and command[-1] == str(path) and entry in command[-2]
            for command in commands
        )

    # Allocation default ACL: what the maker commits inherits read for the transport.
    assert grants(metadata, "u:transport:r-X")
    assert grants(metadata, "d:u:transport:r-x")
    assert grants(metadata / "objects", "d:u:transport:r-x")
    assert grants(metadata.parent, "u:transport:--x")
    assert grants(canonical, "u:transport:r-X")
    # Never the worktree itself.
    assert not any(
        command[-1] in {str(target), str(target.parent)} and "u:transport:" in command[-2]
        for command in commands
    )

    commands.clear()
    manager.verify_mirror = lambda repository: canonical  # type: ignore[method-assign]
    manager.grant_delivery_read("falcone", target)
    assert grants(metadata, "u:transport:r-X")
    assert grants(metadata / "objects", "d:u:transport:r-x")
    assert grants(canonical, "u:transport:r-X")
    assert grants(target, "u:delivery-runner:r-X")
    assert not any(
        command[-1] in {str(target), str(target.parent)} and "u:transport:" in command[-2]
        for command in commands
    )

    commands.clear()
    without = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=worktree_root,
        lease_root=tmp_path / "leases",
        metadata_root=metadata_root,
    )
    without._run = record  # type: ignore[method-assign]
    without._grant_access(
        target, metadata, canonical, WorktreeAccess("codex-runner", "claude-runner")
    )
    assert not any("u:transport:" in argument for command in commands for argument in command)


@pytest.mark.parametrize("identity", ["codex-runner", "claude-runner", "Hermes", "a b", ""])
def test_publication_transport_identity_must_be_a_plain_non_model_user(identity: str) -> None:
    with pytest.raises(ValueError, match="transport identity"):
        GitWorktreeManager(publication_transport_identity=identity)
