from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engineering_control.models.enums import Project


class ReconciliationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateBuildPolicy(ReconciliationModel):
    enabled: bool
    workflow_name: str = Field(min_length=1, max_length=160)
    artifact_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    manifest_file: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.json$")
    signer_workflow: str = Field(
        pattern=r"^gntik-ai/(falcone|musematic|llmwiki)/\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$"
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
    component_lease: str = Field(pattern=r"^(falcone|musematic|llmwiki):[a-z0-9:-]{1,180}$")
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


class StagingFlowPolicy(ReconciliationModel):
    """Post-merge staging GitOps delivery (Argo CD + argocd-image-updater)."""

    enabled: bool = False
    kind: Literal["argocd-image-updater"] = "argocd-image-updater"
    argo_application: str = Field(pattern=_KUBERNETES_NAME)
    argo_namespace: str = Field(default="argocd", pattern=_KUBERNETES_NAME)
    namespaces: tuple[str, ...] = Field(min_length=1, max_length=10)
    source_ci_workflow: str = Field(pattern=_WORKFLOW_REFERENCE)
    main_ci_workflow: str | None = Field(default=None, pattern=_WORKFLOW_REFERENCE)
    image_workflow: str | None = Field(default=None, pattern=_WORKFLOW_REFERENCE)
    release_tag: ReleaseTagPolicy = Field(default_factory=ReleaseTagPolicy)
    deploy_repository: str = Field(
        pattern=r"^gntik-ai/(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)$"
    )
    deploy_values_file: str = Field(pattern=r"^[A-Za-z0-9._/-]{1,240}\.ya?ml$")
    deploy_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9._/-]{1,120}$")
    image_repositories: tuple[str, ...] = Field(min_length=1, max_length=20)
    tag_prefix: str = Field(default="sha-", pattern=r"^[A-Za-z0-9._-]{0,20}$")
    tag_length: int = Field(default=7, ge=7, le=40)
    chart_source_repository: str | None = Field(
        default=None,
        pattern=r"^https://github\.com/gntik-ai/(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)\.git$",
    )
    pin_chart_to_merge: bool = False
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
    repository: str = Field(pattern=r"^gntik-ai/(falcone|musematic|llmwiki)$")
    source_pr_number: int = Field(ge=1)
    source_pr_url: str = Field(
        pattern=r"^https://github\.com/gntik-ai/(falcone|musematic|llmwiki)/pull/[1-9][0-9]*$"
    )
    source_branch: str = Field(
        pattern=r"^agent/(falcone|musematic|llmwiki)/[A-Za-z0-9._-]+/[a-f0-9-]{36}$"
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
    source_repository: str = Field(pattern=r"^gntik-ai/(falcone|musematic|llmwiki)$")
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
