import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

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
