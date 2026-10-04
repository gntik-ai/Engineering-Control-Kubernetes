from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from engineering_control.leases.file_lease import FileLease
from engineering_control.security.redaction import SecretRedactor
from engineering_control.worktrees.agent_config import is_agent_config_path, overlaps


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorktreeAllocation:
    repository: str
    path: Path
    branch: str
    base_sha: str
    change_set_id: UUID
    # NEW-52: the previous attempt's committed HEAD (a descendant of base_sha) this
    # allocation kept instead of resetting to the base; None for a fresh/reset worktree.
    resumed_from_sha: str | None = None


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
    _repositories = frozenset(
        {"falcone", "falcone-charts", "musematic", "musematic-deploy", "llmwiki"}
    )
    _deployment_repositories = frozenset({"falcone-charts", "musematic-deploy", "llmwiki"})

    def __init__(
        self,
        mirror_root: Path = Path("/srv/engineering/mirrors"),
        worktree_root: Path = Path("/srv/engineering/worktrees"),
        lease_root: Path = Path("/var/lib/engineering-control/leases/worktrees"),
        metadata_root: Path | None = None,
        publication_transport_identity: str | None = None,
        transport_syncs_mirrors: bool = False,
    ) -> None:
        self.mirror_root = mirror_root
        self.worktree_root = worktree_root
        self.lease_root = lease_root
        self.metadata_root = metadata_root or (
            worktree_root.parent / "cache" / "scan" / "worktree-git"
        )
        # NEW-61: the controller's GitHub worker fetches the validated commits over SSH
        # as this operator identity; it reads the ChangeSet's Git metadata (and, through
        # its alternates, the canonical mirror) but never the worktree.
        if publication_transport_identity is not None and (
            not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", publication_transport_identity)
            or publication_transport_identity in WorktreeAccess._allowed_identities
        ):
            raise ValueError("publication transport identity is invalid")
        self.publication_transport_identity = publication_transport_identity
        # NEW-90: with NEW-71 the transport identity also pushes the refreshed canonical
        # mirrors to this host, so it owns write access there; a recursive read grant
        # replaced its rwx with r-x and every later mirror refresh failed to unpack.
        self.transport_syncs_mirrors = transport_syncs_mirrors

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
        # GitHub authentication is configured out-of-band with `gh auth setup-git`
        # for this exact Unix identity. Engineering Control never extracts a token.
        return {"GIT_TERMINAL_PROMPT": "0"}

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
        # The authenticated GitHub worker refreshes the canonical mirror before
        # the scan activity; scan-runner never handles GitHub credentials.
        self._run(["git", "remote", "get-url", "origin"], mirror)

    def _fetched_ref(self, base_ref: str, mirror: Path) -> str:
        prefix = "refs/heads/"
        if base_ref.startswith(prefix):
            remote_ref = f"refs/remotes/origin/{base_ref.removeprefix(prefix)}"
            available = self._run(
                ["git", "for-each-ref", "--format=%(refname)", remote_ref], mirror
            )
            return remote_ref if available == remote_ref else base_ref
        return base_ref

    def snapshot(self, repository: str, *, base_ref: str, expected_sha: str) -> dict[str, str]:
        if not re.fullmatch(r"[a-f0-9]{40}", expected_sha):
            raise ValueError("expected snapshot SHA must contain 40 hexadecimal characters")
        mirror = self.verify_mirror(repository)
        self.fetch(repository)
        fetched_ref = self._fetched_ref(base_ref, mirror)
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

    def resolve_head(self, repository: str, *, base_ref: str) -> str:
        """Resolve a freshly fetched immutable head without trusting a caller-supplied SHA."""
        mirror = self.verify_mirror(repository)
        self.fetch(repository)
        fetched_ref = self._fetched_ref(base_ref, mirror)
        actual = self._run(["git", "rev-parse", f"{fetched_ref}^{{commit}}"], mirror)
        if not re.fullmatch(r"[a-f0-9]{40}", actual):
            raise WorktreeError(f"resolved Git head is not immutable for {repository}")
        return actual

    def is_fast_forward(self, repository: str, ancestor: str, head: str) -> bool:
        """NEW-117: True when head equals ancestor or strictly descends from it."""
        if not re.fullmatch(r"[a-f0-9]{40}", ancestor) or not re.fullmatch(r"[a-f0-9]{40}", head):
            return False
        if ancestor == head:
            return True
        return self._descends_from(self.verify_mirror(repository), ancestor, head)

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
        resume_prior_commits: bool = False,
    ) -> WorktreeAllocation:
        if not re.fullmatch(r"[a-f0-9]{40}", expected_base_sha):
            raise ValueError("expected base must be a full 40-character SHA")
        mirror = self.verify_mirror(repository)
        owner = f"changeset:{change_set_id}"
        lease = FileLease(self.lease_root, f"{repository}/{change_set_id}", owner)
        with lease:
            self.fetch(repository)
            fetched_ref = self._fetched_ref(base_ref, mirror)
            actual = self._run(["git", "rev-parse", f"{fetched_ref}^{{commit}}"], mirror)
            mismatch = WorktreeError(
                f"base SHA mismatch for {repository}: expected {expected_base_sha}, got {actual}"
            )
            # NEW-64: a CI-repair re-delivery resumes an already published branch; main may
            # have moved ahead (fast-forward) since import. Only that resume may proceed,
            # and it stays bound to the imported base (never rebased onto the new main).
            base_moved = actual != expected_base_sha
            if base_moved and not (
                resume_prior_commits and self._descends_from(mirror, expected_base_sha, actual)
            ):
                raise mismatch
            metadata = self._ensure_metadata_mirror(repository, change_set_id, mirror)
            # NEW-40: a maker could implement and stage a change yet fail to commit
            # ("Git has no configured author identity"). Give the ChangeSet's repository
            # the same identity the platform uses for its own commits.
            self._run(["git", "config", "user.name", "Engineering Control"], metadata)
            self._run(
                ["git", "config", "user.email", "engineering-control@local.invalid"], metadata
            )
            repo_slug = self._slug(repository)
            target = self.worktree_root / repo_slug / str(change_set_id)
            branch = f"agent/{self._slug(project)}/{self._slug(issue)}/{str(change_set_id)}"
            if target.exists():
                actual_head = self._run(["git", "rev-parse", "HEAD"], target)
                actual_branch = self._run(["git", "branch", "--show-current"], target)
                if actual_branch != branch:
                    raise WorktreeError(
                        f"existing worktree does not match the ChangeSet allocation: {target}"
                    )
                if (
                    resume_prior_commits
                    and actual_head != expected_base_sha
                    and self._descends_from(target, expected_base_sha, actual_head)
                ):
                    # NEW-52: musematic #993 churned through 11 rounds because every
                    # correction re-implemented the feature from the base. Keep the
                    # previous attempt's commits (they descend from this base); only
                    # uncommitted leftovers are archived and dropped.
                    if self.is_dirty(target):
                        self._archive_and_reset(
                            target, expected_base_sha, change_set_id, reset_to=actual_head
                        )
                    return WorktreeAllocation(
                        repository,
                        target,
                        branch,
                        expected_base_sha,
                        change_set_id,
                        resumed_from_sha=actual_head,
                    )
                if base_moved:
                    raise mismatch
                if actual_head != expected_base_sha or self.is_dirty(target):
                    # NEW-38: a stopped attempt's maker left work behind, so no retry or
                    # correction of this ChangeSet could ever start. The exclusive
                    # implementation component lease is held before allocation, so no other
                    # delivery can be using this worktree: archive it, then start clean.
                    self._archive_and_reset(target, expected_base_sha, change_set_id)
                return WorktreeAllocation(repository, target, branch, actual, change_set_id)
            # NEW-85: the cleanup may have removed this ChangeSet's worktree while its
            # branch (and any archived work) stays in the metadata repository.
            self._run(["git", "worktree", "prune"], metadata)
            existing_head = self._branch_head(metadata, branch)
            resumed_from: str | None = None
            if (
                existing_head is not None
                and resume_prior_commits
                and self._descends_from(metadata, expected_base_sha, existing_head)
            ):
                resumed_from = existing_head
            elif base_moved:
                raise mismatch
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            if resumed_from is not None:
                add = ["git", "worktree", "add", str(target), branch]
            elif existing_head is not None:
                if existing_head != expected_base_sha:
                    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                    self._run(
                        [
                            "git",
                            "update-ref",
                            f"refs/archive/{change_set_id}/{stamp}-head",
                            existing_head,
                        ],
                        metadata,
                    )
                add = ["git", "worktree", "add", "-B", branch, str(target), expected_base_sha]
            else:
                add = ["git", "worktree", "add", "-b", branch, str(target), expected_base_sha]
            self._run(add, metadata, timeout=900)
            if self.is_dirty(target):
                raise WorktreeError("newly allocated worktree is unexpectedly dirty")
            if access is not None:
                try:
                    self._grant_access(target, metadata, mirror, access, repository=repository)
                except Exception:
                    self._run(["git", "worktree", "remove", str(target)], metadata)
                    raise
            return WorktreeAllocation(
                repository,
                target,
                branch,
                expected_base_sha if resumed_from is not None else actual,
                change_set_id,
                resumed_from_sha=resumed_from,
            )

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
                fetched_ref = self._fetched_ref(base_ref, mirror)
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
        *,
        repository: str | None = None,
    ) -> None:
        """Grant the maker write and checker read access to exactly one worktree."""
        resolved_root = self.worktree_root.resolve()
        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(resolved_root) or resolved_target == resolved_root:
            raise WorktreeError("refusing to grant access outside a bounded worktree")
        # NEW-38b: the manager's own identity (the scan worker) must keep access to files
        # the maker creates, or it cannot archive/reset (or later diff) that worktree.
        manager = pwd.getpwuid(os.geteuid()).pw_name
        manager_acl = f"u:{manager}:rwX,"
        manager_default_acl = f"d:u:{manager}:rwx,"
        # NEW-56: the publishing identities read the worktree after validation, but only a
        # file's owner may add them to its ACL later, and every file the maker creates is
        # owned by its runner (mode 660/770). Grant them read in the default ACL up front so
        # maker-created files inherit it.
        readers = ["delivery-runner"]
        if repository in self._deployment_repositories:
            readers.append("release-runner")
        reader_acl = "".join(f"u:{reader}:r-X," for reader in readers)
        reader_default_acl = "".join(f"d:u:{reader}:r-x," for reader in readers)
        access_acl = (
            f"{manager_acl}u:{access.maker_identity}:rwX,"
            f"u:{access.checker_identity}:r-X,{reader_acl}m::rwX"
        )
        default_acl = (
            f"{manager_default_acl}d:u:{access.maker_identity}:rwx,"
            f"d:u:{access.checker_identity}:r-x,{reader_default_acl}d:m::rwx"
        )
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
        metadata_acl = access_acl
        metadata_default_acl = default_acl
        transport = self.publication_transport_identity
        if transport is not None:
            # NEW-61: objects and refs the maker commits inherit read for the transport
            # identity, so the controller can fetch the validated branch after checking.
            # Metadata only: the worktree ACL above is unchanged.
            metadata_acl = access_acl.replace(",m::rwX", f",u:{transport}:r-X,m::rwX")
            metadata_default_acl = default_acl.replace(
                ",d:m::rwx", f",d:u:{transport}:r-x,d:m::rwx"
            )
            self._grant_metadata_traverse(metadata, (transport,))
        self._run(["setfacl", "--recursive", "--modify", metadata_acl, str(metadata)], metadata)
        for directory in (metadata, *(item for item in metadata.rglob("*") if item.is_dir())):
            self._run(["setfacl", "--modify", metadata_default_acl, str(directory)], metadata)
        self._grant_canonical_read(canonical_mirror, identities)
        if transport is not None:
            # NEW-61: the metadata repository is a --shared clone; upload-pack reads the
            # base history through its alternates in the canonical mirror.
            self._grant_transport_canonical(canonical_mirror, transport)

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
        self._grant_publication_transport_read(metadata, mirror)

    def _grant_publication_transport_read(self, metadata: Path, mirror: Path) -> None:
        """NEW-61: let the controller's GitHub worker fetch the validated branch over SSH.

        Only the ChangeSet's bare Git metadata repository (and the canonical mirror its
        alternates point at) become readable; the worktree is never granted. Maker-owned
        files must already carry the read entry from the allocation default ACL (or be
        readable otherwise), else this fails closed like every other read grant.
        """
        transport = self.publication_transport_identity
        if transport is None:
            return
        self._grant_metadata_traverse(metadata, (transport,))
        self._grant_tree_read_access(metadata, (transport,), cwd=metadata)
        default_acl = f"d:u:{transport}:r-x"
        for directory in (metadata, *(item for item in metadata.rglob("*") if item.is_dir())):
            self._setfacl_readable(["setfacl", "--modify", default_acl, str(directory)], metadata)
        self._grant_transport_canonical(mirror, transport)

    def _grant_transport_canonical(self, mirror: Path, transport: str) -> None:
        if not self.transport_syncs_mirrors:
            self._grant_canonical_read(mirror, (transport,))
            return
        root = self.mirror_root.resolve()
        resolved = mirror.resolve()
        if resolved.parent != root:
            raise WorktreeError("canonical mirror access escaped its root")
        self._run(["setfacl", "--modify", f"u:{transport}:--x", str(root)], resolved)
        self._setfacl_transport_write(
            ["setfacl", "--recursive", "--modify", f"u:{transport}:rwX", str(resolved)],
            resolved,
            transport,
        )

    def _setfacl_transport_write(self, args: list[str], cwd: Path, transport: str) -> None:
        """NEW-90: objects a mirror push created are owned by the transport identity, and
        only their owner may change their ACL. Tolerate "Operation not permitted" only on
        paths the transport itself owns (an owner can always use its own files, even Git's
        0444 objects); fail closed otherwise."""
        try:
            self._run(args, cwd)
            return
        except WorktreeError:
            pass
        result = subprocess.run(  # noqa: S603 - argument vector only; shell is never used
            args, cwd=cwd, check=False, text=True, capture_output=True, timeout=300
        )
        if not result.returncode:
            return
        try:
            transport_uid = pwd.getpwnam(transport).pw_uid
        except KeyError as error:
            raise WorktreeError("publication transport identity does not exist") from error
        lines = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
        if not lines:
            raise WorktreeError(f"setfacl failed ({result.returncode}) without output")
        for line in lines:
            match = self._SETFACL_EPERM.match(line)
            if match is None:
                output = str(SecretRedactor().redact_text("\n".join(lines)).value)
                raise WorktreeError(f"setfacl failed ({result.returncode}): {output[-4000:]}")
            path = Path(match.group(1))
            try:
                info = path.lstat()
            except OSError as error:
                raise WorktreeError(f"setfacl could not inspect {path}") from error
            if info.st_uid != transport_uid:
                raise WorktreeError(f"setfacl may not grant the transport write on {path}")

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
            self._setfacl_readable(
                ["setfacl", "--modify", default_acl, str(directory)], resolved_target
            )

    def _grant_tree_read_access(
        self,
        target: Path,
        identities: tuple[str, ...],
        *,
        cwd: Path,
    ) -> None:
        reader_acl = ",".join(f"u:{identity}:r-X" for identity in identities)
        self._setfacl_readable(["setfacl", "--recursive", "--modify", reader_acl, str(target)], cwd)

    _SETFACL_EPERM = re.compile(r"^setfacl: (.+): Operation not permitted$")

    def _setfacl_readable(self, args: list[str], cwd: Path) -> None:
        """NEW-55: run a read-granting setfacl, tolerating only files it may not modify.

        Files the maker creates are owned by its runner identity, and only a file's
        owner (or root) may change its ACL, so granting delivery-runner/release-runner
        read after validation failed with "Operation not permitted" on every maker
        file. Such a file is accepted only when it is not ours and is already readable
        by any identity through its "other" permission bits (directories also
        traversable); anything else still fails closed.
        """
        try:
            self._run(args, cwd)
            return
        except WorktreeError:
            pass
        # _run keeps only the last 4000 characters; re-run (idempotent) for every path.
        result = subprocess.run(  # noqa: S603 - argument vector only; shell is never used
            args, cwd=cwd, check=False, text=True, capture_output=True, timeout=300
        )
        if not result.returncode:
            return
        # NEW-56: identities named in the ACL argument (u:<id>:... or d:u:<id>:...).
        identities = tuple(
            dict.fromkeys(re.findall(r"(?:^|,)(?:d:)?u:([a-z][a-z0-9-]*):", args[-2]))
        )
        lines = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
        unreadable: list[str] = []
        for line in lines:
            match = self._SETFACL_EPERM.match(line)
            if match is None:
                output = str(SecretRedactor().redact_text("\n".join(lines)).value)
                raise WorktreeError(f"setfacl failed ({result.returncode}): {output[-4000:]}")
            path = Path(match.group(1))
            try:
                info = path.lstat()
            except OSError:
                unreadable.append(str(path))
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            other_ok = bool(info.st_mode & stat.S_IROTH) and (
                not stat.S_ISDIR(info.st_mode) or bool(info.st_mode & stat.S_IXOTH)
            )
            if info.st_uid == os.geteuid() or not (
                other_ok or self._acl_grants_read(path, identities, stat.S_ISDIR(info.st_mode))
            ):
                unreadable.append(str(path))
        if unreadable:
            raise WorktreeError(
                "cannot grant read access to "
                f"{len(unreadable)} path(s), e.g. {unreadable[0][-300:]}"
            )

    @staticmethod
    def _acl_grants_read(path: Path, identities: tuple[str, ...], directory: bool) -> bool:
        """NEW-56: True when every identity has an effective read (and, for a directory,
        traverse) entry in the path's access ACL, e.g. inherited from a default ACL."""
        if not identities:
            return False
        command = ["getfacl", "--omit-header", "--absolute-names", "--", str(path)]
        result = subprocess.run(  # noqa: S603 - argument vector only; shell is never used
            command,
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
        )
        if result.returncode:
            return False
        effective: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if line.startswith("default:"):
                continue
            match = re.match(r"^user:([^:]+):([rwx-]{3})(?:\s+#effective:([rwx-]{3}))?", line)
            if match:
                effective[match.group(1)] = match.group(3) or match.group(2)
        return all(
            identity in effective
            and effective[identity][0] == "r"
            and (not directory or effective[identity][2] == "x")
            for identity in identities
        )

    def _branch_head(self, metadata: Path, branch: str) -> str | None:
        try:
            head = self._run(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], metadata)
        except WorktreeError:
            return None
        return head if re.fullmatch(r"[a-f0-9]{40}", head) else None

    def _descends_from(self, target: Path, base_sha: str, head: str) -> bool:
        """True when head is a strict descendant of base_sha (NEW-52 resume guard)."""
        if not re.fullmatch(r"[a-f0-9]{40}", head) or head == base_sha:
            return False
        try:
            self._run(["git", "merge-base", "--is-ancestor", base_sha, head], target)
        except WorktreeError:
            return False  # not an ancestor (exit 1) or unknown history: reset to base
        return True

    def _archive_and_reset(
        self,
        target: Path,
        base_sha: str,
        change_set_id: UUID,
        *,
        reset_to: str | None = None,
    ) -> None:
        """Keep a stopped attempt's work under refs/archive/<changeset>/ in the ChangeSet's
        git metadata, then reset the worktree to its base (or, NEW-52, to reset_to: the
        attempt's own committed HEAD, so only uncommitted leftovers are dropped)."""
        destination = reset_to or base_sha
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        prefix = f"refs/archive/{change_set_id}/{stamp}"
        head = self._run(["git", "rev-parse", "HEAD"], target)
        if head != base_sha:
            self._run(["git", "update-ref", f"{prefix}-head", head], target)
        if self.is_dirty(target):
            self._run(
                [
                    "git",
                    "-c",
                    "user.name=engineering-control",
                    "-c",
                    "user.email=engineering-control@localhost",
                    "stash",
                    "push",
                    "--include-untracked",
                    "--message",
                    f"archived stopped delivery attempt {stamp}",
                ],
                target,
            )
            stash = self._run(["git", "rev-parse", "refs/stash"], target)
            self._run(["git", "update-ref", f"{prefix}-worktree", stash], target)
        self._run(["git", "reset", "--hard", destination], target)
        self._run(["git", "clean", "-ffdx"], target)
        if self.is_dirty(target) or self._run(["git", "rev-parse", "HEAD"], target) != destination:
            raise WorktreeError(f"could not reset the archived worktree to its base: {target}")

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
        changed_paths = sorted(committed_or_tracked.union(str(item["path"]) for item in untracked))
        summary: dict[str, object] = {
            "base_sha": base_sha,
            "head_sha": self._run(["git", "rev-parse", "HEAD"], resolved),
            "changed_entries": len(status.splitlines()),
            "changed_file_count": len(changed_paths),
            "changed_paths": changed_paths,
            "status_sha256": hashlib.sha256(status.encode()).hexdigest(),
            "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
            "untracked": untracked,
            "agent_config_paths": self._agent_config_changes(resolved, base_sha=base_sha),
        }
        return summary, diff

    @staticmethod
    def _git_bytes(args: list[str], cwd: Path) -> bytes:
        """Raw stdout (NUL-delimited records, unquoted non-ASCII paths)."""
        result = subprocess.run(  # noqa: S603 - argument vector only; shell is never used
            args, cwd=cwd, check=False, capture_output=True, timeout=300
        )
        if result.returncode:
            error = str(SecretRedactor().redact_text(os.fsdecode(result.stderr)).value)
            raise WorktreeError(f"Git command failed ({result.returncode}): {error[-4000:]}")
        return result.stdout

    def _agent_config_changes(self, worktree: Path, *, base_sha: str) -> list[str]:
        """NEW-130: every way a ChangeSet touches agent configuration (.codex, .claude,
        .mcp.json at any depth): a changed, added, deleted or renamed path (renames are
        split into deletion + addition), a symlink it adds, changes or deletes whose
        target names such a location, and an agent-config path in the resulting tree
        that resolves through a symlink onto something the ChangeSet changed."""
        refused: set[str] = set()
        changed: set[str] = set()

        def display(raw: bytes) -> str:
            return raw.decode("utf-8", "backslashreplace")

        def link_target(sha: str, path: bytes) -> str | None:
            if set(sha) != {"0"}:
                return display(self._git_bytes(["git", "cat-file", "blob", sha], worktree))
            on_disk = worktree / os.fsdecode(path)
            return display(os.fsencode(os.readlink(on_disk))) if on_disk.is_symlink() else None

        def resolved_inside(path: bytes) -> str | None:
            real = Path(os.path.realpath(worktree / os.fsdecode(path)))
            if not real.is_relative_to(worktree):
                return None
            return display(os.fsencode(real.relative_to(worktree).as_posix()))

        def traversed(path: bytes) -> set[str]:
            """Every symlink location traversed while resolving `path` component by
            component (NEW-130 review 2: a changed symlink in the middle of the chain
            counts; the final target is checked separately)."""
            seen: set[str] = set()
            pending = list(Path(os.fsdecode(path)).parts)
            current = worktree
            hops = 0
            while pending and hops < 64:
                part = pending.pop(0)
                candidate = current / part
                relative = (
                    candidate.relative_to(worktree) if candidate.is_relative_to(worktree) else None
                )
                if candidate.is_symlink():
                    if relative is not None:
                        seen.add(relative.as_posix())
                    hops += 1
                    target = Path(os.readlink(candidate))
                    if target.is_absolute():
                        if not target.is_relative_to(worktree):
                            break
                        current = worktree
                        pending = list(target.relative_to(worktree).parts) + pending
                    else:
                        pending = list(target.parts) + pending
                    continue
                current = Path(os.path.normpath(candidate))
                if not current.is_relative_to(worktree):
                    break
            return seen

        def check_symlink(path: bytes, target: str | None) -> None:
            name = display(path)
            if target is not None and is_agent_config_path(target):
                refused.add(f"{name} -> {target}")
            inside = resolved_inside(path)
            if inside is not None and inside != name and is_agent_config_path(inside):
                refused.add(f"{name} -> {inside}")

        tokens = self._git_bytes(
            ["git", "diff", "--raw", "-z", "--no-renames", "--no-abbrev", base_sha, "--"],
            worktree,
        ).split(b"\0")
        index = 0
        while index + 1 < len(tokens):
            meta, path = tokens[index], tokens[index + 1]
            index += 2
            if not meta.startswith(b":"):
                raise WorktreeError("unexpected git diff --raw record")
            src_mode, dst_mode, src_sha, dst_sha, _ = meta[1:].decode("ascii").split(" ")
            name = display(path)
            changed.add(name)
            if is_agent_config_path(name):
                refused.add(name)
            if src_mode == "120000" and set(src_sha) != {"0"}:
                check_symlink(path, link_target(src_sha, path))
            if dst_mode == "120000":
                check_symlink(path, link_target(dst_sha, path))
        for path in self._git_bytes(
            ["git", "ls-files", "-z", "--others", "--exclude-standard"], worktree
        ).split(b"\0"):
            if not path:
                continue
            name = display(path)
            changed.add(name)
            if is_agent_config_path(name):
                refused.add(name)
            on_disk = worktree / os.fsdecode(path)
            if on_disk.is_symlink():
                check_symlink(path, display(os.fsencode(os.readlink(on_disk))))
        if changed:
            # The resulting tree: an agent-config path that resolves through a symlink
            # (pre-existing or not) onto a path this ChangeSet changed.
            for record in self._git_bytes(["git", "ls-files", "-z", "-s"], worktree).split(b"\0"):
                if b"\t" not in record:
                    continue
                path = record.split(b"\t", 1)[1]
                name = display(path)
                if not is_agent_config_path(name):
                    continue
                inside = resolved_inside(path)
                if (
                    inside is not None
                    and inside != name
                    and any(overlaps(inside, item) for item in changed)
                ):
                    refused.add(f"{name} -> {inside}")
                    continue
                visited = traversed(path) - {name}
                hit = sorted(
                    step for step in visited if any(overlaps(step, item) for item in changed)
                )
                if hit:
                    refused.add(f"{name} via {hit[0]}")
        return sorted(refused)

    def prune_orphan_worktrees(
        self,
        *,
        older_than: datetime,
        dry_run: bool,
        limit: int = 200,
        excluded_repositories: frozenset[str] = frozenset(),
        excluded_paths: frozenset[str] = frozenset(),
    ) -> dict[str, list[dict[str, str]]]:
        """NEW-85: remove worktree directories no lease ever tracks.

        Detached audit/revalidation snapshots (keyed by campaign or finding, never by a
        ChangeSet) are removed once untouched since ``older_than``. Branch-bearing
        ChangeSet worktrees are left to the lease-based cleanup. Only the known
        repositories' directories are walked: the worktree root itself is root-owned and
        traverse-only for the scan worker, so anything else under it (old acceptance
        runs) is out of reach by design. Nothing is followed through a symlink; failures
        are reported, never raised.
        """
        root = self.worktree_root.resolve()
        # NEW-119: a path a lease tracks or that cleanup inspection protected is never
        # pruned, whatever the project scoping says.
        excluded = {Path(path).resolve(strict=False) for path in excluded_paths}
        removed: list[dict[str, str]] = []
        kept: list[dict[str, str]] = []
        slugs = sorted(
            {self._slug(repository) for repository in self._repositories}
            - {self._slug(repository) for repository in excluded_repositories}
        )
        for slug in slugs:
            repository_dir = root / slug
            if repository_dir.is_symlink() or not repository_dir.is_dir():
                continue
            for target in sorted(repository_dir.iterdir()):
                if len(removed) >= limit:
                    break
                try:
                    identity = UUID(target.name)
                except ValueError:
                    continue
                if target.is_symlink() or not target.is_dir():
                    continue
                if target in excluded or target.resolve() in excluded:
                    continue
                modified = datetime.fromtimestamp(target.stat().st_mtime, UTC)
                if modified > older_than:
                    continue
                record = {
                    "path": str(target),
                    "modified_at": modified.isoformat(),
                    "kind": "SNAPSHOT",
                }
                try:
                    if self._run(["git", "branch", "--show-current"], target):
                        continue  # a ChangeSet worktree: lease-based cleanup only
                    metadata = self.metadata_path(slug, identity)
                    if dry_run:
                        removed.append(record)
                        continue
                    self._run(["git", "worktree", "remove", "--force", str(target)], metadata)
                    remaining = self._run(["git", "worktree", "list", "--porcelain"], metadata)
                    if remaining.count("worktree ") <= 1 and metadata.is_dir():
                        shutil.rmtree(metadata)
                    removed.append(record)
                except (WorktreeError, OSError) as error:
                    kept.append({**record, "reason": str(error)[:200]})
        return {"removed": removed, "kept": kept}

    def cleanup(
        self,
        allocation: WorktreeAllocation,
        *,
        evidence_captured: bool,
        archive_dirty: bool = False,
    ) -> None:
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
        metadata = self._metadata_for_target(allocation.repository, resolved)
        if self.is_dirty(allocation.path):
            if not archive_dirty:
                if not evidence_captured:
                    raise WorktreeError("dirty worktree is protected until evidence is captured")
                raise WorktreeError(
                    "dirty worktree cleanup requires the abandoned-run cleanup workflow"
                )
            # NEW-85: an abandoned attempt's uncommitted work is kept under
            # refs/archive/<changeset>/ in its git metadata before the worktree goes.
            head = self._run(["git", "rev-parse", "HEAD"], resolved)
            self._archive_and_reset(
                resolved, allocation.base_sha, allocation.change_set_id, reset_to=head
            )
        self._run(["git", "worktree", "remove", "--force", str(allocation.path)], metadata)
