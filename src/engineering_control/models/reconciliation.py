from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engineering_control.models.enums import Project
from engineering_control.projects import (
    PROJECT_ALTERNATION,
    REPOSITORY_SLUG_ALTERNATION,
    SOURCE_REPOSITORY_ALTERNATION,
)


class ReconciliationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateBuildPolicy(ReconciliationModel):
    enabled: bool
    workflow_name: str = Field(min_length=1, max_length=160)
    artifact_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    manifest_file: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.json$")
    signer_workflow: str = Field(
        pattern=rf"^gntik-ai/({SOURCE_REPOSITORY_ALTERNATION})/\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$"
    )
    deny_self_hosted_runners: bool = True
    blocker: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,119}$")

    @model_validator(mode="after")
    def disabled_policy_has_blocker(self) -> CandidateBuildPolicy:
        if not self.enabled and self.blocker is None:
            raise ValueError("disabled candidate build policy requires a blocker")
        if self.enabled and self.blocker is not None:
            raise ValueError("enabled candidate build policy cannot retain a blocker")
        return self


class ImmutablePromotionPolicy(ReconciliationModel):
    enabled: bool
    deployment_file: str
    chart_directory: str | None = None
    values_files: tuple[str, ...] = ()
    component_lease: str = Field(pattern=rf"^({PROJECT_ALTERNATION}):[a-z0-9:-]{{1,180}}$")
    image_repositories: dict[str, str]
    digest_paths: dict[str, str]
    blocker: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,119}$")

    @field_validator("deployment_file", "chart_directory")
    @classmethod
    def relative_repository_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
            raise ValueError("promotion paths must be bounded repository-relative paths")
        return value

    @field_validator("values_files")
    @classmethod
    def relative_values_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
                raise ValueError("values files must be bounded repository-relative paths")
        return values

    @field_validator("image_repositories")
    @classmethod
    def immutable_repository_names(cls, values: dict[str, str]) -> dict[str, str]:
        if not values:
            raise ValueError("promotion policy requires at least one image repository")
        for component, repository in values.items():
            if not component or not repository.startswith("ghcr.io/gntik-ai/"):
                raise ValueError(
                    "promotion image repositories must be named first-party GHCR images"
                )
            if "@" in repository or ":" in repository.removeprefix("ghcr.io/"):
                raise ValueError("promotion repositories cannot embed tags or digests")
        return values

    @model_validator(mode="after")
    def coherent_promotion_policy(self) -> ImmutablePromotionPolicy:
        if set(self.image_repositories) != set(self.digest_paths):
            raise ValueError("promotion digest paths must exactly match image components")
        if len(set(self.digest_paths.values())) != len(self.digest_paths):
            raise ValueError("promotion digest paths must be unique")
        if any(
            not all(
                part and part.replace("-", "").replace("_", "").isalnum()
                for part in path.split(".")
            )
            for path in self.digest_paths.values()
        ):
            raise ValueError("promotion digest paths must be dotted YAML key paths")
        if self.enabled:
            if self.blocker is not None or self.chart_directory is None or not self.values_files:
                raise ValueError("enabled promotion requires chart validation and no blocker")
        elif self.blocker is None:
            raise ValueError("disabled promotion policy requires a blocker")
        return self


_KUBERNETES_NAME = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"
_WORKFLOW_REFERENCE = r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,159}$"


class ReleaseTagPolicy(ReconciliationModel):
    """Optional semver release tag that triggers a tag-driven image build."""

    enabled: bool = False
    prefix: str = Field(default="v", pattern=r"^[A-Za-z]{0,8}$")
    bump: Literal["patch"] = "patch"


class CiBaselineMarker(ReconciliationModel):
    """NEW-63: a marker file that makes one CI path group run on a baseline branch."""

    # The group is included when the delivered PR changes any path starting with scope.
    scope: str = Field(pattern=r"^[A-Za-z0-9._/-]{1,200}$")
    path: str = Field(pattern=r"^[A-Za-z0-9._/-]{1,240}$")

    @field_validator("path")
    @classmethod
    def bounded_marker_path(cls, value: str) -> str:
        parts = PurePosixPath(value).parts
        if value.startswith("/") or ".." in parts or (parts and parts[0] == ".github"):
            raise ValueError("CI baseline markers must be repository-relative, outside .github")
        return value


class CiBaselinePolicy(ReconciliationModel):
    """NEW-63: PR CI jobs that also fail on unchanged main are pre-existing, not blockers.

    Hermes opens a draft PR from main plus marker files (hermes/ci-baseline/*) so the same
    path-filtered jobs run on main's code, then blocks only on jobs that fail in the
    delivered PR but not on that baseline.
    """

    enabled: bool = False
    markers: tuple[CiBaselineMarker, ...] = Field(default=(), max_length=30)

    @model_validator(mode="after")
    def enabled_requires_markers(self) -> CiBaselinePolicy:
        if self.enabled and not self.markers:
            raise ValueError("an enabled CI baseline requires at least one marker")
        return self


class StagingFlowPolicy(ReconciliationModel):
    """Post-merge staging GitOps delivery (Argo CD + argocd-image-updater)."""

    enabled: bool = False
    kind: Literal["argocd-image-updater"] = "argocd-image-updater"
    argo_application: str = Field(pattern=_KUBERNETES_NAME)
    argo_namespace: str = Field(default="argocd", pattern=_KUBERNETES_NAME)
    namespaces: tuple[str, ...] = Field(min_length=1, max_length=10)
    source_ci_workflow: str = Field(pattern=_WORKFLOW_REFERENCE)
    # NEW-103: the deployment repository's PR CI workflow; its checks gate both merges.
    deployment_ci_workflow: str | None = Field(default=None, pattern=_WORKFLOW_REFERENCE)
    main_ci_workflow: str | None = Field(default=None, pattern=_WORKFLOW_REFERENCE)
    image_workflow: str | None = Field(default=None, pattern=_WORKFLOW_REFERENCE)
    release_tag: ReleaseTagPolicy = Field(default_factory=ReleaseTagPolicy)
    deploy_repository: str = Field(pattern=rf"^gntik-ai/({REPOSITORY_SLUG_ALTERNATION})$")
    deploy_values_file: str = Field(pattern=r"^[A-Za-z0-9._/-]{1,240}\.ya?ml$")
    deploy_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9._/-]{1,120}$")
    image_repositories: tuple[str, ...] = Field(min_length=1, max_length=20)
    tag_prefix: str = Field(default="sha-", pattern=r"^[A-Za-z0-9._-]{0,20}$")
    tag_length: int = Field(default=7, ge=7, le=40)
    chart_source_repository: str | None = Field(
        default=None,
        pattern=rf"^https://github\.com/gntik-ai/({REPOSITORY_SLUG_ALTERNATION})\.git$",
    )
    pin_chart_to_merge: bool = False
    # NEW-62: R3 ChangeSets (e.g. database migrations) are never merged unless the project
    # opts in; the operator allows it only for staging-only clusters.
    allow_r3_merge: bool = False
    ci_baseline: CiBaselinePolicy = Field(default_factory=CiBaselinePolicy)
    external_secrets_namespace: str = Field(default="external-secrets", pattern=_KUBERNETES_NAME)
    external_secrets_deployment: str = Field(default="external-secrets", pattern=_KUBERNETES_NAME)

    @field_validator("namespaces")
    @classmethod
    def kubernetes_namespaces(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("staging flow namespaces must be unique")
        for value in values:
            if not re.fullmatch(_KUBERNETES_NAME, value) or value in {
                "kube-system",
                "kube-public",
                "default",
            }:
                raise ValueError("staging flow namespaces must be bounded project namespaces")
        return values

    @field_validator("deploy_values_file")
    @classmethod
    def relative_values_file(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("deploy values file must be a bounded repository-relative path")
        return value

    @field_validator("image_repositories")
    @classmethod
    def first_party_images(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("staging flow image repositories must be unique")
        for value in values:
            if not re.fullmatch(r"ghcr\.io/gntik-ai/[a-z0-9._/-]{1,160}", value):
                raise ValueError("staging flow image repositories must be first-party GHCR images")
        return values

    @model_validator(mode="after")
    def coherent_staging_flow(self) -> StagingFlowPolicy:
        if self.pin_chart_to_merge and self.chart_source_repository is None:
            raise ValueError("pinning the chart to the merge requires chart_source_repository")
        if self.release_tag.enabled and self.image_workflow is None:
            raise ValueError("a release tag requires the image_workflow it triggers")
        return self


_CHECK_NAME = r"^[A-Za-z0-9][A-Za-z0-9 ._()/:-]{0,199}$"
_REPOSITORY_PREFIX = re.compile(r"[A-Za-z0-9._-]{1,100}(?:/[A-Za-z0-9._-]{1,100}){0,5}/")


def _repository_prefix(value: str) -> bool:
    return _REPOSITORY_PREFIX.fullmatch(value) is not None and ".." not in value.split("/")


def _repository_path(value: str) -> bool:
    """A repository prefix (``dir/``) or one repository-relative file path."""
    return _repository_prefix(value if value.endswith("/") else f"{value}/")


class ChangesetGatePolicy(ReconciliationModel):
    """NEW-160: a delivered PR that touches a published package carries a changeset.

    Checked on the PR's file list before Hermes merges: when any changed path starts with
    one of ``package_paths``, at least one added or modified ``<directory><name>.md`` (not
    README.md) must be part of the PR."""

    enabled: bool = False
    directory: str = ".changeset/"
    package_paths: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("directory")
    @classmethod
    def repository_directory(cls, value: str) -> str:
        if not _repository_prefix(value) or value.startswith(".github/"):
            raise ValueError("the changeset directory must be a repository prefix")
        return value

    @field_validator("package_paths")
    @classmethod
    def repository_prefixes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or not all(_repository_prefix(v) for v in values):
            raise ValueError("changeset package paths must be unique repository prefixes")
        return values

    @model_validator(mode="after")
    def enabled_requires_paths(self) -> ChangesetGatePolicy:
        if self.enabled and not self.package_paths:
            raise ValueError("an enabled changeset gate requires package paths")
        return self

    def touches_packages(
        self,
        paths: list[str],
        manifests: Mapping[str, Mapping[str, Any] | None] | None = None,
    ) -> bool:
        """True when any changed path (a rename's previous path included) is a package file.

        NEW-209: with ``manifests`` (package directory -> its package.json at the PR head,
        None when unreadable), a path counts only when that package publishes it: a
        private package publishes nothing, and a ``files`` list publishes only what it
        names plus package.json, README, LICENSE and CHANGELOG. An unknown package, an
        unreadable manifest or a missing ``files`` list keeps the path counted, as does a
        path outside any npm package (gntik-ui #32: PR #87 changed only tests of the
        private mcp package and of tokens, which publishes src/ and dist/)."""
        return self.enabled and any(self._published(path, manifests) for path in paths)

    def package_directory(self, path: str) -> str | None:
        """``<prefix><name>/`` for a path inside a package directory, else None."""
        for prefix in self.package_paths:
            rest = path.removeprefix(prefix) if path.startswith(prefix) else None
            if rest and "/" in rest:
                return f"{prefix}{rest.split('/', 1)[0]}/"
        return None

    def _published(
        self, path: str, manifests: Mapping[str, Mapping[str, Any] | None] | None
    ) -> bool:
        if not any(path.startswith(prefix) for prefix in self.package_paths):
            return False
        directory = self.package_directory(path)
        if manifests is None or directory is None:
            return True
        manifest = manifests.get(directory)
        if not isinstance(manifest, Mapping):
            return True
        if manifest.get("private") is True:
            return False
        files = manifest.get("files")
        if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
            return True
        return _published_by_files(path.removeprefix(directory), files)

    def changeset_candidates(self, changes: list[dict[str, str]]) -> list[str]:
        """Changeset files the PR adds or modifies (a rename or copy counts by its new path).

        A removed file, or the previous path of a rename, never counts; neither does any file
        other than ``<directory><name>.md`` (README.md excluded). The caller still proves each
        candidate exists at the bound head commit."""
        candidates: list[str] = []
        for change in changes:
            path = change.get("path", "")
            name = path.removeprefix(self.directory)
            if (
                change.get("status") in _CHANGESET_STATUSES
                and path.startswith(self.directory)
                and "/" not in name
                and name.endswith(".md")
                and name.lower() != "readme.md"
                and len(name) > len(".md")
            ):
                candidates.append(path)
        return candidates


_CHANGESET_STATUSES = frozenset({"added", "modified", "changed", "renamed", "copied"})

# NEW-209: files npm always publishes from a package root, whatever ``files`` says.
_ALWAYS_PUBLISHED = re.compile(
    r"(?i)^(?:package\.json|readme(?:\..*)?|licen[cs]e(?:\..*)?|changelog(?:\..*)?)$"
)


def _published_by_files(relative: str, files: list[str]) -> bool:
    """Whether npm publishes ``relative`` (a path inside the package) under ``files``.

    Conservative: a negated entry is ignored (the file still counts as published), and a
    glob entry matches through fnmatch on the path and on each of its parent directories."""
    if "/" not in relative and _ALWAYS_PUBLISHED.match(relative):
        return True
    parents = [relative.rsplit("/", depth)[0] for depth in range(1, relative.count("/") + 1)]
    for raw in files:
        entry = raw.strip().removeprefix("./").rstrip("/")
        if not entry or entry.startswith("!"):
            continue
        if any(char in entry for char in "*?["):
            if any(fnmatch.fnmatchcase(candidate, entry) for candidate in (relative, *parents)):
                return True
        elif relative == entry or relative.startswith(f"{entry}/"):
            return True
    return False


class MergeDeliveryPolicy(ReconciliationModel):
    """NEW-160: full delivery that ends at Hermes's protected merge (no runtime).

    For a project without a deployment (``deployment.strategy: none``, e.g. the gntik-ui
    kit): after the maker, the independent checker and green PR CI, Hermes merges the source
    PR and closes the issue with the merge evidence. There is no release tag, image build,
    staging GitOps or runtime verification; publishing packages stays with the operator."""

    enabled: bool = False
    kind: Literal["protected-merge"] = "protected-merge"
    source_ci_workflow: str = Field(pattern=_WORKFLOW_REFERENCE)
    allow_r3_merge: bool = False
    ci_baseline: CiBaselinePolicy = Field(default_factory=CiBaselinePolicy)
    # Check names of continue-on-error jobs (e.g. gntik-ui's visual baselines): neither
    # their failure nor their pending state blocks the merge. Every other check must pass.
    non_blocking_checks: tuple[str, ...] = Field(default=(), max_length=10)
    changeset: ChangesetGatePolicy = Field(default_factory=ChangesetGatePolicy)
    # Paths (``dir/`` prefixes or files) a merge-only PR may never touch; CI configuration
    # by default.
    protected_paths: tuple[str, ...] = Field(default=(".github/",), max_length=20)

    @field_validator("protected_paths")
    @classmethod
    def protected_prefixes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or not all(_repository_path(v) for v in values):
            raise ValueError("protected paths must be unique repository paths")
        return values

    def protected(self, path: str) -> bool:
        """True when ``path`` is (under) one of the protected paths."""
        return any(
            path.startswith(item) if item.endswith("/") else path == item
            for item in self.protected_paths
        )

    @field_validator("non_blocking_checks")
    @classmethod
    def check_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or not all(
            re.fullmatch(_CHECK_NAME, value) for value in values
        ):
            raise ValueError("non-blocking checks must be unique bounded check names")
        return values


class DeliveryControlPolicy(ReconciliationModel):
    source_ci_workflow: str = Field(min_length=1, max_length=160)
    candidate_build: CandidateBuildPolicy
    immutable_promotion: ImmutablePromotionPolicy
    staging_flow: StagingFlowPolicy | None = None


class CIReconciliationPlan(ReconciliationModel):
    change_set_id: UUID
    run_id: UUID
    workflow_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,239}$")
    project: Project
    repository: str = Field(pattern=rf"^gntik-ai/({SOURCE_REPOSITORY_ALTERNATION})$")
    source_pr_number: int = Field(ge=1)
    source_pr_url: str = Field(
        pattern=rf"^https://github\.com/gntik-ai/({SOURCE_REPOSITORY_ALTERNATION})/pull/[1-9][0-9]*$"
    )
    source_branch: str = Field(
        pattern=rf"^agent/({PROJECT_ALTERNATION})/[A-Za-z0-9._-]+/[a-f0-9-]{{36}}$"
    )
    source_head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_base_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9._/-]{1,120}$")
    candidate_build_run_id: int | None = Field(default=None, ge=1)
    policy: DeliveryControlPolicy
    independent_review_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    independent_review_provider: str = Field(pattern=r"^(codex|claude)$")
    maker_provider: str = Field(pattern=r"^(codex|claude)$")

    @model_validator(mode="after")
    def cross_provider_review(self) -> CIReconciliationPlan:
        if self.maker_provider == self.independent_review_provider:
            raise ValueError("reconciliation requires a cross-provider independent review")
        expected_repository = f"gntik-ai/{self.project.value}"
        if self.repository != expected_repository:
            raise ValueError("reconciliation repository does not match project")
        return self


class CandidateImage(ReconciliationModel):
    component: str = Field(pattern=r"^[a-z][a-z0-9-]{0,79}$")
    repository: str = Field(pattern=r"^ghcr\.io/gntik-ai/[a-z0-9._-]{1,160}$")
    digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class CandidateArtifactManifest(ReconciliationModel):
    schema_version: Literal[1]
    project: Project
    source_repository: str = Field(pattern=rf"^gntik-ai/({SOURCE_REPOSITORY_ALTERNATION})$")
    source_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    workflow_run_id: int = Field(ge=1)
    images: tuple[CandidateImage, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_images(self) -> CandidateArtifactManifest:
        components = [image.component for image in self.images]
        repositories = [image.repository for image in self.images]
        if len(set(components)) != len(components) or len(set(repositories)) != len(repositories):
            raise ValueError("candidate manifest image identities must be unique")
        if self.source_repository != f"gntik-ai/{self.project.value}":
            raise ValueError("candidate manifest repository does not match project")
        return self


class PromotionPlan(ReconciliationModel):
    change_set_id: UUID
    run_id: UUID
    workflow_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,239}$")
    project: Literal[Project.FALCONE]
    repository: Literal["gntik-ai/falcone-charts"]
    repository_slug: Literal["falcone-charts"]
    working_directory: Path
    branch: str = Field(pattern=r"^agent/falcone/[A-Za-z0-9._-]+/[a-f0-9-]{36}$")
    expected_head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_file: str
    chart_directory: str
    values_files: tuple[str, ...] = Field(min_length=1)
    digest_paths: dict[str, str]
    image_digests: dict[str, str]
    chart_version: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("image_digests")
    @classmethod
    def exact_digests(cls, values: dict[str, str]) -> dict[str, str]:
        if not values or any(
            not value.startswith("sha256:") or len(value) != 71 for value in values.values()
        ):
            raise ValueError("promotion requires complete immutable image digests")
        return values

    @model_validator(mode="after")
    def aligned_components(self) -> PromotionPlan:
        if set(self.digest_paths) != set(self.image_digests):
            raise ValueError("promotion components do not match their digest paths")
        return self


class PromotionPublication(ReconciliationModel):
    change_set_id: UUID
    repository: Literal["gntik-ai/falcone-charts"]
    repository_slug: Literal["falcone-charts"]
    working_directory: Path
    branch: str = Field(pattern=r"^agent/falcone/[A-Za-z0-9._-]+/[a-f0-9-]{36}$")
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    base_branch: Literal["main"] = "main"
    title: str = Field(min_length=1, max_length=240)
    body: str = Field(min_length=1, max_length=32_000)
