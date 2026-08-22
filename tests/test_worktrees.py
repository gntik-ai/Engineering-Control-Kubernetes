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


def test_maker_and_checker_must_be_independent() -> None:
    with pytest.raises(ValueError, match="must differ"):
        WorktreeAccess("codex-runner", "codex-runner")


def test_only_model_worker_identities_receive_worktree_access() -> None:
    with pytest.raises(ValueError, match="allowed model worker"):
        WorktreeAccess("release-runner", "claude-runner")
