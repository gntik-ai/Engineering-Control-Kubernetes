import hashlib
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


def test_git_read_credential_is_passed_by_bounded_file_reference(tmp_path: Path) -> None:
    credential_root = tmp_path / "scan"
    credential_root.mkdir()
    token_file = credential_root / "github-read.token"
    token_file.write_text("synthetic-read-token-value\n", encoding="utf-8")
    token_file.chmod(0o640)
    askpass = tmp_path / "git-askpass.sh"
    askpass.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    askpass.chmod(0o755)
    manager = GitWorktreeManager(
        git_read_token_file=token_file,
        git_read_credential_root=credential_root,
        git_askpass_executable=askpass,
    )

    environment = manager._fetch_environment()

    assert environment == {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": str(askpass),
        "ENGINEERING_GITHUB_TOKEN_FILE": str(token_file),
    }
    assert "synthetic-read-token-value" not in environment.values()


def test_git_read_credential_rejects_broad_permissions(tmp_path: Path) -> None:
    credential_root = tmp_path / "scan"
    credential_root.mkdir()
    token_file = credential_root / "github-read.token"
    token_file.write_text("synthetic-read-token-value\n", encoding="utf-8")
    token_file.chmod(0o644)
    manager = GitWorktreeManager(
        git_read_token_file=token_file,
        git_read_credential_root=credential_root,
    )

    with pytest.raises(WorktreeError, match="permissions are too broad"):
        manager._fetch_environment()


def test_absent_bounded_git_credential_allows_public_mirror_fetch(tmp_path: Path) -> None:
    credential_root = tmp_path / "scan"
    credential_root.mkdir()
    manager = GitWorktreeManager(
        git_read_token_file=credential_root / "github-read.token",
        git_read_credential_root=credential_root,
    )

    assert manager._fetch_environment() == {"GIT_TERMINAL_PROMPT": "0"}


def test_absent_git_credential_outside_bounded_root_is_rejected(tmp_path: Path) -> None:
    credential_root = tmp_path / "scan"
    credential_root.mkdir()
    manager = GitWorktreeManager(
        git_read_token_file=tmp_path / "outside" / "github-read.token",
        git_read_credential_root=credential_root,
    )

    with pytest.raises(WorktreeError, match="outside its bounded root"):
        manager._fetch_environment()


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
