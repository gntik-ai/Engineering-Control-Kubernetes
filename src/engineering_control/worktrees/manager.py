from __future__ import annotations

import hashlib
import json
import os
import re
import stat
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
    _repositories = frozenset({"falcone", "falcone-charts", "musematic", "musematic-deploy"})
    _deployment_repositories = frozenset({"falcone-charts", "musematic-deploy"})

    def __init__(
        self,
        mirror_root: Path = Path("/srv/engineering/mirrors"),
        worktree_root: Path = Path("/srv/engineering/worktrees"),
        lease_root: Path = Path("/var/lib/engineering-control/leases/worktrees"),
        metadata_root: Path | None = None,
        git_read_token_file: Path | None = None,
        git_read_credential_root: Path = Path("/etc/engineering-control/env/scan"),
        git_askpass_executable: Path = Path(
            "/opt/engineering-control/source/scripts/git-askpass.sh"
        ),
        production_credentials: bool = False,
    ) -> None:
        self.mirror_root = mirror_root
        self.worktree_root = worktree_root
        self.lease_root = lease_root
        self.metadata_root = metadata_root or (
            worktree_root.parent / "cache" / "scan" / "worktree-git"
        )
        self.git_read_token_file = git_read_token_file
        self.git_read_credential_root = git_read_credential_root
        self.git_askpass_executable = git_askpass_executable
        self.production_credentials = production_credentials

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.")
        if not slug or slug in {".", ".."}:
            raise ValueError("invalid Git identifier")
        return slug

    def _run(
        self,
        args: list[str],
        cwd: Path,
        *,
        timeout: int = 300,
        environment: dict[str, str] | None = None,
    ) -> str:
        process_environment = os.environ.copy()
        if environment:
            process_environment.update(environment)
        result = subprocess.run(  # noqa: S603 - argument vector only; shell is never used
            args,
            cwd=cwd,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=process_environment,
        )
        output = str(SecretRedactor().redact_text(result.stdout + result.stderr).value)
        if result.returncode:
            raise WorktreeError(f"Git command failed ({result.returncode}): {output[-4000:]}")
        return result.stdout.strip()

    def _fetch_environment(self) -> dict[str, str]:
        environment = {"GIT_TERMINAL_PROMPT": "0"}
        token_path = self.git_read_token_file
        if token_path is None:
            return environment
        resolved = token_path.resolve()
        root = self.git_read_credential_root.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise WorktreeError("Git read credential is outside its bounded root")
        metadata = resolved.stat()
        permissions = stat.S_IMODE(metadata.st_mode)
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO):
            raise WorktreeError("Git read credential permissions are too broad")
        if self.production_credentials and (
            metadata.st_uid != 0
            or metadata.st_gid != os.getegid()
            or permissions != 0o640
        ):
            raise WorktreeError("production Git read credential ownership is invalid")
        if not self.git_askpass_executable.resolve().is_file():
            raise WorktreeError("Git askpass executable is unavailable")
        environment.update(
            {
                "GIT_ASKPASS": str(self.git_askpass_executable),
                "ENGINEERING_GITHUB_TOKEN_FILE": str(resolved),
            }
        )
        return environment

    def mirror_path(self, repository: str) -> Path:
        return self.mirror_root / f"{self._slug(repository)}.git"

    def verify_mirror(self, repository: str) -> Path:
        mirror = self.mirror_path(repository)
        if not mirror.is_dir():
            raise WorktreeError(f"canonical mirror does not exist: {mirror}")
        if self._run(["git", "rev-parse", "--is-bare-repository"], mirror) != "true":
            raise WorktreeError(f"canonical mirror is not bare: {mirror}")
        return mirror

    def metadata_path(self, repository: str, identity: UUID) -> Path:
        return self.metadata_root / self._slug(repository) / f"{identity}.git"

    def _ensure_metadata_mirror(
        self,
        repository: str,
        identity: UUID,
        canonical_mirror: Path,
    ) -> Path:
        metadata = self.metadata_path(repository, identity)
        expected_parent = self.metadata_root.resolve() / self._slug(repository)
        if metadata.is_symlink() or metadata.resolve().parent != expected_parent:
            raise WorktreeError("ChangeSet Git metadata path escaped its cache root")
        if not metadata.exists():
            metadata.parent.mkdir(parents=True, exist_ok=True, mode=0o710)
            self._run(
                [
                    "git",
                    "clone",
                    "--bare",
                    "--shared",
                    str(canonical_mirror),
                    str(metadata),
                ],
                canonical_mirror,
                timeout=900,
            )
        if (
            not metadata.is_dir()
            or self._run(["git", "rev-parse", "--is-bare-repository"], metadata) != "true"
        ):
            raise WorktreeError("ChangeSet Git metadata repository is not bare")
        origin = Path(self._run(["git", "remote", "get-url", "origin"], metadata)).resolve()
        if origin != canonical_mirror.resolve():
            raise WorktreeError("ChangeSet Git metadata origin changed after allocation")
        return metadata

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
            environment=self._fetch_environment(),
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
            metadata = self._ensure_metadata_mirror(repository, change_set_id, mirror)
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
                metadata,
                timeout=900,
            )
            if self.is_dirty(target):
                raise WorktreeError("newly allocated worktree is unexpectedly dirty")
            if access is not None:
                try:
                    self._grant_access(target, metadata, mirror, access)
                except Exception:
                    self._run(["git", "worktree", "remove", str(target)], metadata)
                    raise
            return WorktreeAllocation(repository, target, branch, actual, change_set_id)

    def allocate_audit_snapshot(
        self,
        *,
        repository: str,
        campaign_id: UUID,
        base_ref: str,
        expected_base_sha: str,
        require_base_ref_match: bool = True,
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
            if require_base_ref_match:
                fetched_ref = self._fetched_ref(base_ref)
                actual = self._run(["git", "rev-parse", f"{fetched_ref}^{{commit}}"], mirror)
            else:
                actual = self._run(["git", "rev-parse", f"{expected_base_sha}^{{commit}}"], mirror)
            if actual != expected_base_sha:
                raise WorktreeError(
                    f"audit base SHA mismatch for {repository}: expected {expected_base_sha}, "
                    f"got {actual}"
                )
            metadata = self._ensure_metadata_mirror(repository, campaign_id, mirror)
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
                metadata,
                timeout=900,
            )
            if self.is_dirty(target):
                raise WorktreeError("new audit snapshot is unexpectedly dirty")
            try:
                self._grant_read_access(
                    target,
                    metadata,
                    mirror,
                    ("codex-runner", "claude-runner"),
                )
            except Exception:
                self._run(["git", "worktree", "remove", str(target)], metadata)
                raise
            return AuditSnapshotAllocation(repository, target, actual, campaign_id)

    def _grant_access(
        self,
        target: Path,
        metadata: Path,
        canonical_mirror: Path,
        access: WorktreeAccess,
    ) -> None:
        """Grant the maker write and checker read access to exactly one worktree."""
        resolved_root = self.worktree_root.resolve()
        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(resolved_root) or resolved_target == resolved_root:
            raise WorktreeError("refusing to grant access outside a bounded worktree")
        access_acl = f"u:{access.maker_identity}:rwX,u:{access.checker_identity}:r-X,m::rwX"
        default_acl = f"d:u:{access.maker_identity}:rwx,d:u:{access.checker_identity}:r-x,d:m::rwx"
        traverse_acl = f"u:{access.maker_identity}:--x,u:{access.checker_identity}:--x"
        self._run(
            ["setfacl", "--modify", traverse_acl, str(resolved_target.parent)],
            resolved_target,
        )
        self._run(["setfacl", "--recursive", "--modify", access_acl, str(target)], target)
        for directory in (target, *(item for item in target.rglob("*") if item.is_dir())):
            self._run(["setfacl", "--modify", default_acl, str(directory)], target)

        identities = (access.maker_identity, access.checker_identity)
        self._grant_metadata_traverse(metadata, identities)
        metadata_acl = f"u:{access.maker_identity}:rwX,u:{access.checker_identity}:r-X,m::rwX"
        metadata_default_acl = (
            f"d:u:{access.maker_identity}:rwx,d:u:{access.checker_identity}:r-x,d:m::rwx"
        )
        self._run(["setfacl", "--recursive", "--modify", metadata_acl, str(metadata)], metadata)
        for directory in (metadata, *(item for item in metadata.rglob("*") if item.is_dir())):
            self._run(["setfacl", "--modify", metadata_default_acl, str(directory)], metadata)
        self._grant_canonical_read(canonical_mirror, identities)

    def _grant_read_access(
        self,
        target: Path,
        metadata: Path,
        canonical_mirror: Path,
        identities: tuple[str, ...],
    ) -> None:
        if any(identity not in WorktreeAccess._allowed_identities for identity in identities):
            raise WorktreeError("audit snapshot reader is not an allowed model worker")
        self._grant_bounded_read_access(target, identities)
        self._grant_metadata_traverse(metadata, identities)
        self._grant_tree_read_access(metadata, identities, cwd=metadata)
        self._grant_canonical_read(canonical_mirror, identities)

    def grant_release_read(self, repository: str, target: Path) -> None:
        """Grant release-runner read-only access to one validated deployment worktree.

        A linked worktree's Git metadata and objects live in the canonical bare mirror, so
        release preflight needs read access to both the exact deployment worktree and that
        deployment repository's mirror. Source mirrors are never eligible.
        """
        if repository not in self._deployment_repositories:
            raise WorktreeError("release access is limited to deployment repositories")
        resolved_root = self.worktree_root.resolve()
        resolved_target = target.resolve()
        expected_parent = resolved_root / self._slug(repository)
        if (
            not resolved_target.is_dir()
            or resolved_target.parent != expected_parent
            or resolved_target == resolved_root
        ):
            raise WorktreeError("release access target is not an exact deployment worktree")
        mirror = self.verify_mirror(repository)
        metadata = self._metadata_for_target(repository, resolved_target)
        self._grant_bounded_read_access(resolved_target, ("release-runner",))
        self._grant_metadata_traverse(metadata, ("release-runner",))
        self._grant_tree_read_access(metadata, ("release-runner",), cwd=metadata)
        self._grant_canonical_read(mirror, ("release-runner",))

    def grant_delivery_read(self, repository: str, target: Path) -> None:
        """Grant the non-LLM delivery identity read access for exact branch publication."""
        if repository not in self._repositories:
            raise WorktreeError("delivery access is limited to configured repositories")
        resolved_root = self.worktree_root.resolve()
        resolved_target = target.resolve()
        expected_parent = resolved_root / self._slug(repository)
        if (
            not resolved_target.is_dir()
            or resolved_target.parent != expected_parent
            or resolved_target == resolved_root
        ):
            raise WorktreeError("delivery access target is not an exact ChangeSet worktree")
        mirror = self.verify_mirror(repository)
        metadata = self._metadata_for_target(repository, resolved_target)
        self._grant_bounded_read_access(resolved_target, ("delivery-runner",))
        self._grant_metadata_traverse(metadata, ("delivery-runner",))
        self._grant_tree_read_access(metadata, ("delivery-runner",), cwd=metadata)
        self._grant_canonical_read(mirror, ("delivery-runner",))

    def _metadata_for_target(self, repository: str, target: Path) -> Path:
        try:
            identity = UUID(target.name)
        except ValueError as error:
            raise WorktreeError("worktree path has no ChangeSet identity") from error
        configured = self.metadata_path(repository, identity)
        metadata = configured.resolve()
        expected_parent = self.metadata_root.resolve() / self._slug(repository)
        if configured.is_symlink() or metadata.parent != expected_parent:
            raise WorktreeError("ChangeSet Git metadata path escaped its cache root")
        if not metadata.is_dir():
            raise WorktreeError("ChangeSet Git metadata repository is missing")
        return metadata

    def _grant_metadata_traverse(self, metadata: Path, identities: tuple[str, ...]) -> None:
        root = self.metadata_root.resolve()
        resolved = metadata.resolve()
        if not resolved.is_relative_to(root) or resolved == root:
            raise WorktreeError("Git metadata access escaped its bounded cache root")
        traverse_acl = ",".join(f"u:{identity}:--x" for identity in identities)
        for directory in (root.parent, root, resolved.parent):
            self._run(["setfacl", "--modify", traverse_acl, str(directory)], resolved)

    def _grant_canonical_read(self, mirror: Path, identities: tuple[str, ...]) -> None:
        root = self.mirror_root.resolve()
        resolved = mirror.resolve()
        if resolved.parent != root:
            raise WorktreeError("canonical mirror access escaped its root")
        traverse_acl = ",".join(f"u:{identity}:--x" for identity in identities)
        self._run(["setfacl", "--modify", traverse_acl, str(root)], resolved)
        self._grant_tree_read_access(resolved, identities, cwd=resolved)

    def _grant_bounded_read_access(self, target: Path, identities: tuple[str, ...]) -> None:
        resolved_root = self.worktree_root.resolve()
        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(resolved_root) or resolved_target == resolved_root:
            raise WorktreeError("refusing to grant access outside a bounded audit snapshot")
        self._grant_tree_read_access(resolved_target, identities, cwd=resolved_target)
        default_acl = ",".join(f"d:u:{identity}:r-x" for identity in identities)
        traverse_acl = ",".join(f"u:{identity}:--x" for identity in identities)
        self._run(
            ["setfacl", "--modify", traverse_acl, str(resolved_target.parent)],
            resolved_target,
        )
        for directory in (
            resolved_target,
            *(item for item in resolved_target.rglob("*") if item.is_dir()),
        ):
            self._run(["setfacl", "--modify", default_acl, str(directory)], resolved_target)

    def _grant_tree_read_access(
        self,
        target: Path,
        identities: tuple[str, ...],
        *,
        cwd: Path,
    ) -> None:
        reader_acl = ",".join(f"u:{identity}:r-X" for identity in identities)
        self._run(["setfacl", "--recursive", "--modify", reader_acl, str(target)], cwd)

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

    def change_evidence(self, path: Path, *, base_sha: str) -> tuple[dict[str, object], str]:
        root = self.worktree_root.resolve()
        resolved = path.resolve()
        if resolved == root or not resolved.is_dir() or not resolved.is_relative_to(root):
            raise WorktreeError("change evidence escaped the bounded worktree root")
        if not re.fullmatch(r"[a-f0-9]{40}", base_sha):
            raise ValueError("change evidence base must be a full Git SHA")
        self._run(["git", "cat-file", "-e", f"{base_sha}^{{commit}}"], resolved)
        self._run(["git", "diff", "--check", base_sha, "--"], resolved)
        status = self._run(["git", "status", "--porcelain=v1"], resolved)
        diff = self._run(["git", "diff", "--binary", base_sha, "--"], resolved)
        committed_or_tracked = {
            item
            for item in self._run(
                ["git", "diff", "--name-only", base_sha, "--"], resolved
            ).splitlines()
            if item
        }
        untracked: list[dict[str, object]] = []
        for line in status.splitlines():
            if not line.startswith("?? "):
                continue
            candidate = (resolved / line[3:]).resolve()
            candidates = (
                [candidate]
                if candidate.is_file()
                else sorted(item for item in candidate.rglob("*") if item.is_file())
                if candidate.is_dir()
                else []
            )
            for untracked_file in candidates:
                if not untracked_file.is_relative_to(resolved):
                    continue
                untracked.append(
                    {
                        "path": untracked_file.relative_to(resolved).as_posix(),
                        "sha256": hashlib.sha256(untracked_file.read_bytes()).hexdigest(),
                        "size_bytes": untracked_file.stat().st_size,
                    }
                )
        summary: dict[str, object] = {
            "base_sha": base_sha,
            "head_sha": self._run(["git", "rev-parse", "HEAD"], resolved),
            "changed_entries": len(status.splitlines()),
            "changed_file_count": len(
                committed_or_tracked.union(str(item["path"]) for item in untracked)
            ),
            "status_sha256": hashlib.sha256(status.encode()).hexdigest(),
            "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
            "untracked": untracked,
        }
        return summary, diff

    def cleanup(self, allocation: WorktreeAllocation, *, evidence_captured: bool) -> None:
        self.verify_mirror(allocation.repository)
        if not allocation.path.exists():
            return
        expected = (
            self.worktree_root.resolve()
            / self._slug(allocation.repository)
            / str(allocation.change_set_id)
        )
        resolved = allocation.path.resolve()
        if resolved != expected or not resolved.is_dir():
            raise WorktreeError("cleanup target does not match its bounded ChangeSet path")
        actual_branch = self._run(["git", "branch", "--show-current"], resolved)
        if actual_branch != allocation.branch:
            raise WorktreeError("cleanup target branch changed after authorization")
        if self.is_dirty(allocation.path):
            if not evidence_captured:
                raise WorktreeError("dirty worktree is protected until evidence is captured")
            raise WorktreeError(
                "dirty worktree cleanup requires the abandoned-run cleanup workflow"
            )
        metadata = self._metadata_for_target(allocation.repository, resolved)
        self._run(["git", "worktree", "remove", str(allocation.path)], metadata)
