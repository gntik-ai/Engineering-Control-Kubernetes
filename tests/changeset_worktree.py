"""NEW-221: give a test's plain repository the production ChangeSet layout.

Production worktrees are always linked worktrees whose Git metadata lives in a separate
bare repository (``<metadata_root>/<repository>/<id>.git``); privileged Git never
discovers a repository from the worktree. Tests that build a plain repository at a
worktree path call this after their last setup commit.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

GIT = shutil.which("git")
assert GIT is not None


def link_changeset_worktree(worktree: Path, metadata_root: Path) -> Path:
    """Move ``worktree/.git`` to ``metadata_root/<repository>/<id>.git`` as a bare common
    directory and make ``worktree`` its linked worktree, keeping the branch, HEAD, index
    and every file. Returns the metadata repository."""
    worktree = worktree.resolve()
    metadata = metadata_root / worktree.parent.name / f"{worktree.name}.git"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(worktree / ".git"), str(metadata))
    admin = metadata / "worktrees" / worktree.name
    admin.mkdir(parents=True)
    (admin / "HEAD").write_bytes((metadata / "HEAD").read_bytes())
    for name in ("index", "ORIG_HEAD"):
        if (metadata / name).exists():
            shutil.move(str(metadata / name), str(admin / name))
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{worktree}/.git\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    subprocess.run(  # noqa: S603 - fixed Git executable in the test harness
        [GIT, "--git-dir", str(metadata), "config", "core.bare", "true"],
        check=True,
        capture_output=True,
    )
    return metadata


def sealed_changeset_worktree(worktree: Path, *, metadata_root: Path, mirror_root: Path) -> Path:
    """An existing directory as a one-commit linked worktree whose metadata repository is
    sealed, as the scan identity leaves it for the delivery and release identities."""
    from engineering_control.worktrees.hardened_git import seal_metadata

    worktree.mkdir(parents=True, exist_ok=True)
    for arguments in (
        ["init", "-q"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "b"],
    ):
        subprocess.run(  # noqa: S603 - fixed Git executable in the test harness
            [GIT, *arguments], cwd=worktree, check=True, capture_output=True
        )
    metadata = link_changeset_worktree(worktree, metadata_root)
    seal_metadata(metadata, origin=mirror_root / f"{worktree.resolve().parent.name}.git")
    return metadata
