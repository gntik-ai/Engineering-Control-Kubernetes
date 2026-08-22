from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from engineering_control.leases.file_lease import FileLease
from engineering_control.security.redaction import SecretRedactor


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorktreeAllocation:
    repository: str
    path: Path
    branch: str
    base_sha: str
    change_set_id: UUID


@dataclass(frozen=True)
class AuditSnapshotAllocation:
    repository: str
    path: Path
    base_sha: str
    campaign_id: UUID


@dataclass(frozen=True)
class WorktreeAccess:
    maker_identity: str
    checker_identity: str

    _allowed_identities = frozenset({"codex-runner", "claude-runner"})

    def __post_init__(self) -> None:
        if self.maker_identity not in self._allowed_identities:
            raise ValueError("maker identity is not an allowed model worker")
        if self.checker_identity not in self._allowed_identities:
            raise ValueError("checker identity is not an allowed model worker")
        if self.maker_identity == self.checker_identity:
            raise ValueError("maker and checker identities must differ")


class GitWorktreeManager:
    def __init__(
        self,
        mirror_root: Path = Path("/srv/engineering/mirrors"),
        worktree_root: Path = Path("/srv/engineering/worktrees"),
        lease_root: Path = Path("/var/lib/engineering-control/leases/worktrees"),
    ) -> None:
        self.mirror_root = mirror_root
        self.worktree_root = worktree_root
        self.lease_root = lease_root

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.")
        if not slug or slug in {".", ".."}:
            raise ValueError("invalid Git identifier")
        return slug

    @staticmethod
    def _run(args: list[str], cwd: Path, *, timeout: int = 300) -> str:
        result = subprocess.run(  # noqa: S603 - argument vector only; shell is never used
            args,
            cwd=cwd,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        output = str(SecretRedactor().redact_text(result.stdout + result.stderr).value)
        if result.returncode:
            raise WorktreeError(f"Git command failed ({result.returncode}): {output[-4000:]}")
        return result.stdout.strip()

    def mirror_path(self, repository: str) -> Path:
        return self.mirror_root / f"{self._slug(repository)}.git"

    def verify_mirror(self, repository: str) -> Path:
        mirror = self.mirror_path(repository)
        if not mirror.is_dir():
            raise WorktreeError(f"canonical mirror does not exist: {mirror}")
        if self._run(["git", "rev-parse", "--is-bare-repository"], mirror) != "true":
            raise WorktreeError(f"canonical mirror is not bare: {mirror}")
        return mirror

    def fetch(self, repository: str) -> None:
        mirror = self.verify_mirror(repository)
        # Never use a mirror clone's default +refs/*:refs/* refspec here: pruning it can
        # delete the agent branch that backs an active linked worktree.
        self._run(
            [
                "git",
                "fetch",
                "--prune",
                "origin",
                "+refs/heads/*:refs/remotes/origin/*",
                "+refs/tags/*:refs/tags/*",
            ],
            mirror,
            timeout=900,
        )

    @staticmethod
    def _fetched_ref(base_ref: str) -> str:
        prefix = "refs/heads/"
        if base_ref.startswith(prefix):
            return f"refs/remotes/origin/{base_ref.removeprefix(prefix)}"
        return base_ref

    def snapshot(self, repository: str, *, base_ref: str, expected_sha: str) -> dict[str, str]:
        if not re.fullmatch(r"[a-f0-9]{40}", expected_sha):
            raise ValueError("expected snapshot SHA must contain 40 hexadecimal characters")
        mirror = self.verify_mirror(repository)
        self.fetch(repository)
        fetched_ref = self._fetched_ref(base_ref)
        actual = self._run(["git", "rev-parse", f"{fetched_ref}^{{commit}}"], mirror)
        if actual != expected_sha:
            raise WorktreeError(
                f"snapshot SHA mismatch for {repository}: expected {expected_sha}, got {actual}"
            )
        return {
            "repository": repository,
            "mirror": str(mirror),
            "base_ref": fetched_ref,
            "sha": actual,
        }

    def allocate(
        self,
        *,
        repository: str,
        project: str,
        issue: str,
        change_set_id: UUID,
        base_ref: str,
        expected_base_sha: str,
        access: WorktreeAccess | None = None,
    ) -> WorktreeAllocation:
        if not re.fullmatch(r"[a-f0-9]{40}", expected_base_sha):
            raise ValueError("expected base must be a full 40-character SHA")
        mirror = self.verify_mirror(repository)
        owner = f"changeset:{change_set_id}"
        lease = FileLease(self.lease_root, f"{repository}/{change_set_id}", owner)
        with lease:
            self.fetch(repository)
            fetched_ref = self._fetched_ref(base_ref)
            actual = self._run(["git", "rev-parse", f"{fetched_ref}^{{commit}}"], mirror)
            if actual != expected_base_sha:
                raise WorktreeError(
                    f"base SHA mismatch for {repository}: expected {expected_base_sha}, "
                    f"got {actual}"
                )
            repo_slug = self._slug(repository)
            target = self.worktree_root / repo_slug / str(change_set_id)
            branch = f"agent/{self._slug(project)}/{self._slug(issue)}/{str(change_set_id)}"
            if target.exists():
                actual_head = self._run(["git", "rev-parse", "HEAD"], target)
                actual_branch = self._run(["git", "branch", "--show-current"], target)
                if actual_head != expected_base_sha or actual_branch != branch:
                    raise WorktreeError(
                        f"existing worktree does not match the ChangeSet allocation: {target}"
                    )
                if self.is_dirty(target):
                    raise WorktreeError(
                        f"existing worktree is dirty and will not be reused: {target}"
                    )
                return WorktreeAllocation(repository, target, branch, actual, change_set_id)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            self._run(
                ["git", "worktree", "add", "-b", branch, str(target), expected_base_sha],
                mirror,
                timeout=900,
            )
            if self.is_dirty(target):
                raise WorktreeError("newly allocated worktree is unexpectedly dirty")
            if access is not None:
                try:
                    self._grant_access(target, access)
                except Exception:
                    self._run(["git", "worktree", "remove", str(target)], mirror)
                    raise
            return WorktreeAllocation(repository, target, branch, actual, change_set_id)

    def allocate_audit_snapshot(
        self,
        *,
        repository: str,
        campaign_id: UUID,
        base_ref: str,
        expected_base_sha: str,
    ) -> AuditSnapshotAllocation:
        if not re.fullmatch(r"[a-f0-9]{40}", expected_base_sha):
            raise ValueError("expected base must be a full 40-character SHA")
        mirror = self.verify_mirror(repository)
        lease = FileLease(
            self.lease_root,
            f"audit/{repository}/{campaign_id}",
            f"campaign:{campaign_id}",
        )
        with lease:
            self.fetch(repository)
            fetched_ref = self._fetched_ref(base_ref)
            actual = self._run(["git", "rev-parse", f"{fetched_ref}^{{commit}}"], mirror)
            if actual != expected_base_sha:
                raise WorktreeError(
                    f"audit base SHA mismatch for {repository}: expected {expected_base_sha}, "
                    f"got {actual}"
                )
            target = self.worktree_root / self._slug(repository) / str(campaign_id)
            if target.exists():
                if self._run(["git", "rev-parse", "HEAD"], target) != expected_base_sha:
                    raise WorktreeError("existing audit snapshot has the wrong immutable SHA")
                if self._run(["git", "branch", "--show-current"], target):
                    raise WorktreeError("existing audit snapshot is not detached")
                if self.is_dirty(target):
                    raise WorktreeError("existing audit snapshot is dirty and protected")
                return AuditSnapshotAllocation(repository, target, actual, campaign_id)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            self._run(
                ["git", "worktree", "add", "--detach", str(target), expected_base_sha],
                mirror,
                timeout=900,
            )
            if self.is_dirty(target):
                raise WorktreeError("new audit snapshot is unexpectedly dirty")
            try:
                self._grant_read_access(target, ("codex-runner", "claude-runner"))
            except Exception:
                self._run(["git", "worktree", "remove", str(target)], mirror)
                raise
            return AuditSnapshotAllocation(repository, target, actual, campaign_id)

    def _grant_access(self, target: Path, access: WorktreeAccess) -> None:
        """Grant the maker write and checker read access to exactly one worktree."""
        resolved_root = self.worktree_root.resolve()
        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(resolved_root) or resolved_target == resolved_root:
            raise WorktreeError("refusing to grant access outside a bounded worktree")
        access_acl = f"u:{access.maker_identity}:rwX,u:{access.checker_identity}:r-X,m::rwX"
        default_acl = f"d:u:{access.maker_identity}:rwx,d:u:{access.checker_identity}:r-x,d:m::rwx"
        self._run(["setfacl", "--recursive", "--modify", access_acl, str(target)], target)
        for directory in (target, *(item for item in target.rglob("*") if item.is_dir())):
            self._run(["setfacl", "--modify", default_acl, str(directory)], target)

    def _grant_read_access(self, target: Path, identities: tuple[str, ...]) -> None:
        if any(identity not in WorktreeAccess._allowed_identities for identity in identities):
            raise WorktreeError("audit snapshot reader is not an allowed model worker")
        resolved_root = self.worktree_root.resolve()
        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(resolved_root) or resolved_target == resolved_root:
            raise WorktreeError("refusing to grant access outside a bounded audit snapshot")
        reader_acl = ",".join(f"u:{identity}:r-X" for identity in identities)
        default_acl = ",".join(f"d:u:{identity}:r-x" for identity in identities)
        traverse_acl = ",".join(f"u:{identity}:--x" for identity in identities)
        self._run(["setfacl", "--modify", traverse_acl, str(target.parent)], target)
        self._run(["setfacl", "--recursive", "--modify", reader_acl, str(target)], target)
        for directory in (target, *(item for item in target.rglob("*") if item.is_dir())):
            self._run(["setfacl", "--modify", default_acl, str(directory)], target)

    def is_dirty(self, path: Path) -> bool:
        return bool(self._run(["git", "status", "--porcelain=v1"], path))

    def inspect_snapshot(self, path: Path, *, expected_sha: str) -> dict[str, str | int]:
        root = self.worktree_root.resolve()
        resolved = path.resolve()
        if resolved == root or not resolved.is_dir() or not resolved.is_relative_to(root):
            raise WorktreeError("snapshot inspection escaped the bounded worktree root")
        actual = self._run(["git", "rev-parse", "HEAD"], resolved)
        if actual != expected_sha:
            raise WorktreeError(
                f"snapshot inspection SHA mismatch: expected {expected_sha}, got {actual}"
            )
        if self.is_dirty(resolved):
            raise WorktreeError("snapshot inspection found a dirty protected worktree")
        tracked = self._run(["git", "ls-files", "-z"], resolved)
        files = [item for item in tracked.split("\0") if item]
        canonical = json.dumps(files, separators=(",", ":"), ensure_ascii=True).encode()
        return {
            "sha": actual,
            "file_count": len(files),
            "tracked_files_sha256": hashlib.sha256(canonical).hexdigest(),
        }

    def cleanup(self, allocation: WorktreeAllocation, *, evidence_captured: bool) -> None:
        mirror = self.verify_mirror(allocation.repository)
        if not allocation.path.exists():
            return
        if self.is_dirty(allocation.path):
            if not evidence_captured:
                raise WorktreeError("dirty worktree is protected until evidence is captured")
            raise WorktreeError(
                "dirty worktree cleanup requires the abandoned-run cleanup workflow"
            )
        self._run(["git", "worktree", "remove", str(allocation.path)], mirror)
