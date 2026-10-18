"""NEW-221b: the Codex maker sandbox starts inside a sealed ChangeSet repository.

The first live maker after NEW-221 (falcone #945) failed before its first command:
"bwrap: Can't mkdir <metadata>.git/.git: Permission denied". Codex (0.159.3) bind-mounts
synthetic targets (.git, .codex, .agents, .aws) at the top of every extra writable root,
creating them as the maker when missing, and the adapter passed the linked worktree's
commondir -- whose top level NEW-221 seals against the maker -- as one of those roots.

These tests use the real allocation (sealed metadata) and model the maker's access as
production grants it: a recursive named-user ACL whose mask is the group bits, then the
re-seal that removes group write where the maker must not write.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from test_worktrees import GIT, create_mirror

from engineering_control.adapters.codex.client import CodexCLIAdapter
from engineering_control.worktrees.hardened_git import seal_metadata, verify_metadata
from engineering_control.worktrees.manager import GitWorktreeManager, WorktreeAllocation

# codex-cli 0.159.3: "synthetic bubblewrap mount target"s at each writable root.
SYNTHETIC_MOUNT_TARGETS = (".git", ".codex", ".agents", ".aws")


def _allocation(tmp_path: Path) -> tuple[GitWorktreeManager, WorktreeAllocation, Path]:
    mirror_root, sha = create_mirror(tmp_path)
    manager = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
    )
    allocation = manager.allocate(
        repository="falcone",
        project="falcone",
        issue="945",
        change_set_id=uuid4(),
        base_ref="refs/heads/main",
        expected_base_sha=sha,
    )
    metadata = manager.metadata_path("falcone", allocation.change_set_id)
    # The maker's recursive ACL grant sets the mask (the group bits) to rwX everywhere;
    # allocation then re-seals, removing group write from what the maker must not write.
    for directory, _directories, files in os.walk(metadata):
        for name in (directory, *(os.path.join(directory, item) for item in files)):
            mode = os.lstat(name).st_mode
            if not stat.S_ISLNK(mode):
                os.chmod(name, stat.S_IMODE(mode) | stat.S_IRGRP | stat.S_IWGRP)
    seal_metadata(metadata, origin=manager.mirror_path("falcone"))
    return manager, allocation, metadata


def _maker_can_create_in(directory: Path) -> bool:
    """The maker's named-user ACL entry is limited by the mask, the group bits."""
    return bool(stat.S_IMODE(directory.lstat().st_mode) & stat.S_IWGRP)


def test_every_codex_writable_root_lets_the_sandbox_create_its_mount_targets(
    tmp_path: Path,
) -> None:
    manager, allocation, metadata = _allocation(tmp_path)
    base = allocation.base_sha
    assert not _maker_can_create_in(metadata)  # NEW-221 seals the top level

    roots = [Path(root) for root in CodexCLIAdapter._git_metadata_roots(allocation.path)]

    assert roots
    for root in roots:
        missing = [name for name in SYNTHETIC_MOUNT_TARGETS if not (root / name).exists()]
        assert not missing or _maker_can_create_in(root), (
            f"bwrap: Can't mkdir {root / missing[0]}: Permission denied"
        )
    assert metadata.resolve() not in roots
    assert {root.name for root in roots[1:]} == {"objects", "refs", "logs"}

    # The sandbox's own mkdir, left behind as if Codex were killed before removing them:
    # privileged readers and the re-seal still accept the repository.
    for root in roots:
        for name in SYNTHETIC_MOUNT_TARGETS:
            (root / name).mkdir(exist_ok=True)
    evidence, _ = manager.change_evidence(allocation.path, base_sha=base)
    assert evidence["changed_paths"] == []
    assert manager.worktree_identity(allocation.path)[2] is False
    seal_metadata(metadata, origin=manager.mirror_path("falcone"))


@contextmanager
def _writable_only(metadata: Path, roots: list[Path]) -> Iterator[None]:
    """As the sandbox mounts it: the metadata read-only except the writable roots."""
    entries = [Path(directory) for directory, _, _ in os.walk(metadata)]
    entries += [
        Path(directory) / name for directory, _, names in os.walk(metadata) for name in names
    ]
    modes = {entry: entry.lstat().st_mode for entry in entries}
    try:
        for entry, mode in modes.items():
            if not stat.S_ISLNK(mode):
                os.chmod(entry, stat.S_IMODE(mode) & ~0o222)
        for entry, mode in modes.items():
            if not stat.S_ISLNK(mode) and any(entry.is_relative_to(root) for root in roots):
                os.chmod(entry, stat.S_IMODE(mode) | stat.S_IWUSR)
        yield
    finally:
        for entry, mode in sorted(modes.items(), key=lambda item: len(item[0].parts)):
            if not stat.S_ISLNK(mode):
                os.chmod(entry, stat.S_IMODE(mode))


def _maker_git(worktree: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed Git executable in the test harness
        [GIT, "-c", "user.name=maker", "-c", "user.email=maker@example.invalid", *arguments],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_the_maker_still_commits_merges_and_fetches_within_the_narrowed_roots(
    tmp_path: Path,
) -> None:
    manager, allocation, metadata = _allocation(tmp_path)
    roots = [Path(root) for root in CodexCLIAdapter._git_metadata_roots(allocation.path)]
    worktree = allocation.path
    mirror = manager.mirror_path("falcone")

    with _writable_only(metadata, roots):
        (worktree / "change.txt").write_text("maker change\n", encoding="utf-8")
        _maker_git(worktree, "add", "change.txt")
        _maker_git(worktree, "commit", "-qm", "maker commit")
        _maker_git(worktree, "branch", "maker-side")
        _maker_git(worktree, "fetch", "-q", str(mirror), "refs/heads/main:refs/remotes/base/main")
        _maker_git(worktree, "merge", "-q", "--no-edit", "refs/remotes/base/main")
        (worktree / "change.txt").write_text("stashed\n", encoding="utf-8")
        _maker_git(worktree, "stash", "push", "-q")
        _maker_git(worktree, "stash", "pop", "-q")
        _maker_git(worktree, "commit", "-qam", "second maker commit")
        status = _maker_git(worktree, "status", "--porcelain")

    assert status == ""
    assert _maker_git(worktree, "log", "-1", "--format=%s").strip() == "second maker commit"
    verify_metadata(metadata, origin=mirror)
