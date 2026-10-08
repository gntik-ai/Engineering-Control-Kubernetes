from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from engineering_control.models.reconciliation import MergeDeliveryPolicy, StagingFlowPolicy
from engineering_control.projects import (
    REPOSITORY_SLUG_ALTERNATION,
    SELF_CONTAINED_REPOSITORIES,
    ProjectName,
)

Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")]
GitSHA = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
RepositoryName = Annotated[
    str,
    Field(pattern=rf"^gntik-ai/({REPOSITORY_SLUG_ALTERNATION})$"),
]
CredentialReference = Annotated[
    str,
    Field(
        pattern=(
            r"^file:/etc/engineering-control/credentials/"
            r"[a-z0-9-]+/[a-z0-9][a-z0-9._-]*\.(env|token|kubeconfig)$"
        )
    ),
]
ReadinessReference = Annotated[
    str,
    Field(
        pattern=(
            r"^(?:file:/etc/engineering-control/credentials/[a-z0-9-]+/"
            r"[a-z0-9][a-z0-9._-]*\.(?:env|token|kubeconfig)|"
            r"worker-attestation:(?:(?:falcone|musematic)-superadmin|"
            r"(?:falcone|musematic|llmwiki)-observer))$"
        )
    ),
]


class EngineeringModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadinessStatus(StrEnum):
    OPERATIONAL = "OPERATIONAL"
    AVAILABLE_UNTESTED = "AVAILABLE_UNTESTED"
    BLOCKED_BY_CREDENTIAL = "BLOCKED_BY_CREDENTIAL"
    DISABLED = "DISABLED"
    NOT_INSTALLED = "NOT_INSTALLED"


class DiscoveryStatus(StrEnum):
    DISCOVERED_ON_DISK = "DISCOVERED_ON_DISK"
    LOADED_BY_RUNTIME = "LOADED_BY_RUNTIME"
    EXPOSED_THROUGH_TELEGRAM = "EXPOSED_THROUGH_TELEGRAM"
    OPERATIONAL_END_TO_END = "OPERATIONAL_END_TO_END"
    BLOCKED_BY_CREDENTIAL = "BLOCKED_BY_CREDENTIAL"
    DISABLED = "DISABLED"


class RepositoryManifest(EngineeringModel):
    full_name: RepositoryName
    url: HttpUrl
    mirror: str = Field(pattern=r"^/srv/engineering/mirrors/[a-z0-9-]+\.git$")
    default_branch: Literal["main", "master"]
    issue_repository: RepositoryName


class RepositoryPair(EngineeringModel):
    source: RepositoryManifest
    deployment: RepositoryManifest

    @model_validator(mode="after")
    def pair_is_distinct(self) -> RepositoryPair:
        # NEW-157: only registered self-contained projects share one repository.
        if (
            self.source.full_name == self.deployment.full_name
            and str(self.source.full_name) not in SELF_CONTAINED_REPOSITORIES
        ):
            raise ValueError("source and deployment repositories must be distinct")
        return self


class BuildWorkflow(EngineeringModel):
    repository: RepositoryName
    workflow: str = Field(pattern=r"^[A-Za-z0-9._-]+\.ya?ml$")
    exact_sha_input: bool
    immutable_digest_output: bool
    enabled: bool = False
    blocker: str | None = Field(default=None, pattern=r"^[A-Z0-9_]+$")


class PreviewPolicy(EngineeringModel):
    enabled: bool = False
    isolated_target_required: bool = True
    namespace_prefix: str | None = Field(default=None, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?-$")
    approval_required: bool = True
    credential_ref: CredentialReference | None = None
    blocker: str | None = Field(default=None, pattern=r"^[A-Z0-9_]+$")


class StagingPolicy(EngineeringModel):
    enabled: bool = False
    production: Literal[False] = False
    approval_required: bool = True
    credential_ref: CredentialReference | None = None
    blocker: str = Field(pattern=r"^[A-Z0-9_]+$")


class DeploymentManifest(EngineeringModel):
    strategy: Literal["helm", "argocd", "repo-local-manifest"]
    adapter: Literal["falcone", "musematic", "llmwiki-staging"]
    chart_repository: Literal["source", "deployment"]
    chart_path: str = Field(pattern=r"^[A-Za-z0-9._/-]+$")
    values_paths: list[str] = Field(default_factory=list, max_length=10)
    secret_bearing_values_paths: list[str] = Field(default_factory=list, max_length=10)
    build_workflows: list[BuildWorkflow] = Field(min_length=1, max_length=10)
    image_repositories: dict[Slug, str] = Field(min_length=1, max_length=20)
    runtime_url: HttpUrl
    helm_release: str | None = Field(default=None, pattern=r"^[a-z0-9-]+$")
    argocd_application: str | None = Field(default=None, pattern=r"^[a-z0-9-]+$")
    kubernetes_context_ref: str = Field(pattern=r"^[a-z0-9-]+$")
    api_server_identity: str = Field(pattern=r"^https://[^\s]+$")
    namespaces: list[str] = Field(min_length=1, max_length=10)
    observer_credential_ref: CredentialReference
    deployment_revision_annotation: str = Field(
        pattern=r"^[a-z0-9]([-.a-z0-9]*[a-z0-9])?/[A-Za-z0-9]([-.A-Za-z0-9]*[A-Za-z0-9])?$"
    )
    helm_release_revision_annotation: str | None = Field(
        default=None,
        pattern=(
            r"^[a-z0-9]([-.a-z0-9]*[a-z0-9])?/"
            r"[A-Za-z0-9]([-.A-Za-z0-9]*[A-Za-z0-9])?$"
        ),
    )
    preview_policy: PreviewPolicy
    staging_policy: StagingPolicy
    environment_policy: Literal["staging-only"] | None = None
    production_supported: Literal[False] = False
    production_installed: Literal[False] = False

    @model_validator(mode="after")
    def strategy_contract(self) -> DeploymentManifest:
        if self.strategy == "helm" and not self.helm_release:
            raise ValueError("Helm strategy requires helm_release")
        if self.strategy == "argocd" and not self.argocd_application:
            raise ValueError("Argo CD strategy requires argocd_application")
        if self.strategy == "repo-local-manifest" and self.adapter != "llmwiki-staging":
            raise ValueError("repo-local manifest strategy is reserved for LLMWiki staging")
        if self.adapter == "llmwiki-staging" and self.environment_policy != "staging-only":
            raise ValueError("LLMWiki must be staging-only")
        overlap = set(self.values_paths) & set(self.secret_bearing_values_paths)
        if overlap:
            raise ValueError("secret-bearing values paths cannot be normal render inputs")
        return self


class PackageReleasePolicy(EngineeringModel):
    """NEW-160: how a no-runtime project's changes reach consumers. Informational for
    Hermes: it never versions or publishes packages; the operator merges the release PR."""

    kind: Literal["changesets"]
    registry: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)+$")
    publisher: Literal["operator"] = "operator"


class NoDeploymentManifest(EngineeringModel):
    """NEW-160: a project without a runtime (e.g. the gntik-ui kit).

    Hermes builds, deploys and observes nothing for it: no staging flow, observer,
    namespaces, images or personas. Its full delivery ends at the protected merge
    (``delivery_control.merge_delivery``)."""

    strategy: Literal["none"]
    adapter: Literal["none"]
    release: PackageReleasePolicy
    production_supported: Literal[False] = False
    production_installed: Literal[False] = False


TOOLCHAIN_PREPARE_EXECUTABLES = frozenset({"uv", "pnpm", "go", "trivy"})
TOOLCHAIN_PLACEHOLDERS = frozenset({"{python312}"})
TOOLCHAIN_TOTAL_TIMEOUT = 80 * 60
_FORBIDDEN_TOOLCHAIN_COMMANDS = frozenset(
    {"sh", "bash", "zsh", "fish", "dash", "env", "sudo", "ssh", "curl", "wget", "kubectl"}
)


def _unwrapped_timeout_command(argv: list[str]) -> str:
    """NEW-46: `timeout [--opt...] DURATION CMD ...` is checked as CMD."""
    if argv[0] != "timeout":
        return argv[0]
    rest = [value for value in argv[1:] if not value.startswith("-")]
    if len(rest) < 2:
        raise ValueError("timeout must wrap a command")
    return rest[1]


def _repository_relative_cwd(value: str) -> str:
    if (
        not value
        or value.startswith(("/", "~"))
        or "\\" in value
        or any(character in value for character in ("\x00", "\n", "\r"))
    ):
        raise ValueError("toolchain cwd must be a repository-relative path")
    parts = value.split("/")
    if any(part == ".." for part in parts) or (value != "." and any(p in {"", "."} for p in parts)):
        raise ValueError("toolchain cwd must be a normalized repository-relative path")
    return value


def _bounded_arguments(argv: list[str]) -> list[str]:
    for argument in argv:
        if not argument or len(argument) > 500:
            raise ValueError("toolchain arguments must be non-empty bounded values")
        if any(character in argument for character in ("\x00", "\n", "\r")):
            raise ValueError("toolchain arguments must be single-line values")
        remainder = argument
        for placeholder in TOOLCHAIN_PLACEHOLDERS:
            remainder = remainder.replace(placeholder, "")
        if "{" in remainder or "}" in remainder:
            raise ValueError("toolchain arguments support only the {python312} placeholder")
    return argv


class ToolchainPrepareCommand(EngineeringModel):
    """One platform-executed dependency installation command (scan worker, network)."""

    cwd: str = Field(min_length=1, max_length=200)
    argv: list[str] = Field(min_length=1, max_length=30)
    timeout_seconds: int = Field(default=1200, ge=10, le=3600)

    @field_validator("cwd")
    @classmethod
    def repository_relative(cls, value: str) -> str:
        return _repository_relative_cwd(value)

    @field_validator("argv")
    @classmethod
    def allowlisted_executable(cls, argv: list[str]) -> list[str]:
        if argv[0] not in TOOLCHAIN_PREPARE_EXECUTABLES:
            raise ValueError(
                "toolchain prepare executable must be one of "
                + ", ".join(sorted(TOOLCHAIN_PREPARE_EXECUTABLES))
            )
        return _bounded_arguments(argv)


class ToolchainTestCommand(EngineeringModel):
    """A command makers and the checker run themselves; never executed by the platform."""

    cwd: str = Field(min_length=1, max_length=200)
    argv: list[str] = Field(min_length=1, max_length=30)
    description: str = Field(min_length=1, max_length=200)

    @field_validator("cwd")
    @classmethod
    def repository_relative(cls, value: str) -> str:
        return _repository_relative_cwd(value)

    @field_validator("argv")
    @classmethod
    def fixed_command(cls, argv: list[str]) -> list[str]:
        command = _unwrapped_timeout_command(argv)
        if command in _FORBIDDEN_TOOLCHAIN_COMMANDS or command.startswith("/"):
            raise ValueError("toolchain test commands must use fixed non-shell executables")
        return _bounded_arguments(argv)


# NEW-159: the Node.js majors a project may select. "22" is the worker's /usr/local/bin/node
# (today's toolchain); others are side-by-side installs under Settings.toolchain_node_roots.
DEFAULT_TOOLCHAIN_NODE: Literal["22"] = "22"
# A pinned pnpm release: exact MAJOR.MINOR.PATCH, no range, tag or pre-release.
_PNPM_VERSION = r"^(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})$"


class ToolchainPolicy(EngineeringModel):
    enabled: bool = False
    prepare: list[ToolchainPrepareCommand] = Field(default_factory=list, max_length=10)
    test_commands: list[ToolchainTestCommand] = Field(default_factory=list, max_length=20)
    cleanup_paths_ignored_only: bool = True
    # NEW-159: the Node.js major for prepare, baseline, dependency resolution and the
    # maker/checker sandbox, and an optional pinned pnpm (None keeps the worker's
    # Settings.toolchain_pnpm_executable). The defaults are exactly today's toolchain.
    node: Literal["22", "24"] = DEFAULT_TOOLCHAIN_NODE
    pnpm: str | None = Field(default=None, pattern=_PNPM_VERSION)

    @field_validator("node", mode="before")
    @classmethod
    def yaml_integer_major(cls, value: object) -> object:
        # `node: 24` in YAML is an integer; accept it as the same major, nothing else.
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value

    @property
    def default_node_toolchain(self) -> bool:
        return self.node == DEFAULT_TOOLCHAIN_NODE and self.pnpm is None

    @model_validator(mode="after")
    def enabled_requires_prepare(self) -> ToolchainPolicy:
        if self.enabled and not self.prepare:
            raise ValueError("an enabled toolchain requires at least one prepare command")
        if sum(command.timeout_seconds for command in self.prepare) > TOOLCHAIN_TOTAL_TIMEOUT:
            # IssueDeliveryWorkflow gives delivery.prepare_toolchain 90 minutes in total.
            raise ValueError("toolchain prepare timeouts exceed the step's bounded budget")
        return self


_PIN_FILE = r"^[A-Za-z0-9_.-][A-Za-z0-9_./-]{0,239}$"


class DeploymentPinPolicy(EngineeringModel):
    """NEW-118: the source repository's CI pin of the deployment repository's commit.

    Each file holds exactly ``occurrences`` lines ``<variable>: '<40-hex sha>'``; Hermes
    sets them to the deployment worktree HEAD after the deployment maker (pin
    convergence), so the source CI tests against the paired deployment change.
    """

    files: tuple[str, ...] = Field(min_length=1, max_length=10)
    variable: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    occurrences: int = Field(default=1, ge=1, le=5)

    @field_validator("files")
    @classmethod
    def bounded_relative_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("deployment pin files must be unique")
        for value in values:
            parts = value.split("/")
            if (
                not re.fullmatch(_PIN_FILE, value)
                or value.startswith("/")
                or any(part in {"", ".", ".."} for part in parts)
                or parts[0] == ".git"
            ):
                raise ValueError("deployment pin files must be bounded repository-relative paths")
        return values


_REPO_PATH = r"^[A-Za-z0-9_@][A-Za-z0-9_.@-]*(?:/[A-Za-z0-9_@][A-Za-z0-9_.@-]*)*$"
# An exact npm version: MAJOR.MINOR.PATCH with an optional pre-release (canary snapshots are
# `0.0.0-canary-<datetime>`). Ranges, dist-tags, `x` wildcards and build metadata are refused.
_EXACT_VERSION = (
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def _repository_path(value: str) -> str:
    """A normalised repository-relative path: no absolute, `..`, `.`, empty or .git parts."""
    parts = value.split("/")
    if (
        not value
        or len(value) > 240
        or not re.fullmatch(_REPO_PATH, value)
        or any(part in {"", ".", ".."} for part in parts)
        or parts[0] == ".git"
    ):
        raise ValueError("ui_migration paths must be normalised repository-relative paths")
    return value


RepositoryPath = Annotated[str, AfterValidator(_repository_path)]


def _unix_socket_path(value: str) -> str:
    """An absolute, normalised filesystem socket path within the sun_path limit."""
    parts = value.split("/")
    if (
        not value.startswith("/")
        or len(value.encode("utf-8")) > 107
        or not re.fullmatch(r"(?:/[A-Za-z0-9_][A-Za-z0-9_.@-]*)+", value)
        or any(part in {".", ".."} for part in parts[1:])
    ):
        raise ValueError("the MCP socket must be a normalised absolute path (no ..)")
    return value


UnixSocketPath = Annotated[str, AfterValidator(_unix_socket_path)]


class GntikUiPackagePin(EngineeringModel):
    """NEW-120: the exact gntik-ui release the migrated console consumes (no ranges)."""

    registry: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)+$")
    scope: str = Field(pattern=r"^@[a-z0-9][a-z0-9-]{0,63}$")
    version: str = Field(pattern=_EXACT_VERSION, max_length=80)


class GntikUiMcpPin(EngineeringModel):
    """NEW-120/121: the gntik-ui MCP image, always run by digest (never by tag)."""

    image: str = Field(
        pattern=r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?(?:/[a-z0-9][a-z0-9._-]*)+$",
        max_length=200,
    )
    digest: Digest
    # NEW-129: the root-owned, fixed-command socket of the gntik-ui MCP service on the scan
    # worker. When set, Hermes connects to it and never runs Docker itself.
    socket: UnixSocketPath | None = None

    def reference(self) -> str:
        return f"{self.image}@{self.digest}"


class UiRouteTables(EngineeringModel):
    """The backend route tables a UI endpoint is cross-checked against."""

    control_plane_routes: RepositoryPath
    public_route_catalog: RepositoryPath
    executor_openapi: RepositoryPath
    gateway_config: RepositoryPath
    # Optional: the control-plane runtime map, unioned with control_plane_routes.
    control_plane_runtime_map: RepositoryPath | None = None


class UiMigrationPolicy(EngineeringModel):
    """NEW-120: the side-by-side console migration contract of one project."""

    legacy_app: RepositoryPath
    target_app: RepositoryPath
    api_package: RepositoryPath
    gntik_ui: GntikUiPackagePin
    mcp: GntikUiMcpPin
    route_tables: UiRouteTables
    staging_base_url: HttpUrl
    staging_next_path: str = Field(default="/next/", pattern=r"^/(?:[a-z0-9-]+/)+$")
    languages: tuple[str, ...] = Field(min_length=1, max_length=10)
    default_language: str = Field(pattern=r"^[a-z]{2}$")

    @field_validator("languages")
    @classmethod
    def language_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or any(
            not re.fullmatch(r"[a-z]{2}", value) for value in values
        ):
            raise ValueError("ui_migration languages must be unique two-letter codes")
        return values

    @model_validator(mode="after")
    def coherent(self) -> UiMigrationPolicy:
        if self.default_language not in self.languages:
            raise ValueError("ui_migration default_language must be one of its languages")
        apps = (self.legacy_app, self.target_app, self.api_package)
        if len(set(apps)) != len(apps) or any(
            a != b and (a.startswith(b + "/") or b.startswith(a + "/")) for a in apps for b in apps
        ):
            raise ValueError("ui_migration app and package paths must be distinct and disjoint")
        if self.staging_base_url.scheme != "https":
            raise ValueError("ui_migration staging_base_url must use https")
        return self


class MakerRules(EngineeringModel):
    """Project-specific standing rules for makers and checkers (operator-owned config).

    Appended verbatim to the maker (and checker) instructions of the matching repository.
    Bounded: these are short operator rules, not a second instruction channel."""

    source: list[Annotated[str, Field(min_length=1, max_length=600)]] = Field(
        default_factory=list, max_length=12
    )
    deployment: list[Annotated[str, Field(min_length=1, max_length=600)]] = Field(
        default_factory=list, max_length=12
    )


class ProjectManifest(EngineeringModel):
    schema_version: Literal[1]
    project: ProjectName
    repositories: RepositoryPair
    api_url: HttpUrl
    # NEW-160: ``strategy: none`` selects the no-runtime variant.
    deployment: Annotated[
        DeploymentManifest | NoDeploymentManifest, Field(discriminator="strategy")
    ]
    delivery_control: dict[str, object]
    source_test_commands: list[list[str]] = Field(min_length=1, max_length=20)
    deployment_test_commands: list[list[str]] = Field(min_length=1, max_length=20)
    dependencies: dict[str, object] = Field(default_factory=dict)
    classification: dict[str, bool] = Field(default_factory=dict)
    toolchain: ToolchainPolicy | None = None
    # NEW-118: paired source/deployment repositories only (see DeploymentPinPolicy).
    deployment_pin: DeploymentPinPolicy | None = None
    # NEW-120: the side-by-side console migration (legacy -> gntik-ui) contract.
    ui_migration: UiMigrationPolicy | None = None
    maker_rules: MakerRules | None = None

    @model_validator(mode="after")
    def deployment_pin_requires_a_pair(self) -> ProjectManifest:
        if self.deployment_pin is not None and str(self.repositories.source.full_name) == str(
            self.repositories.deployment.full_name
        ):
            raise ValueError("a deployment pin requires separate source and deployment repos")
        return self

    @model_validator(mode="after")
    def runtime_matches_delivery(self) -> ProjectManifest:
        """NEW-160: a merge-only delivery is exactly the no-runtime deployment's."""
        runtime = self.has_runtime()
        if not runtime and (
            self.staging_flow() is not None
            or self.deployment_pin is not None
            or self.ui_migration is not None
        ):
            raise ValueError("a project without a runtime has no staging, pin or UI migration")
        if not runtime and self.merge_delivery() is None:
            raise ValueError("a project without a runtime requires delivery_control.merge_delivery")
        if runtime and "merge_delivery" in self.delivery_control:
            raise ValueError("merge-only delivery is reserved for projects without a runtime")
        return self

    def has_runtime(self) -> bool:
        """NEW-160: False for ``deployment.strategy: none`` (nothing to deploy or observe)."""
        return isinstance(self.deployment, DeploymentManifest)

    def runtime_deployment(self) -> DeploymentManifest:
        """The runtime deployment contract; fails closed for a project without one."""
        if not isinstance(self.deployment, DeploymentManifest):
            raise ValueError("PROJECT_HAS_NO_RUNTIME")
        return self.deployment

    @field_validator("delivery_control")
    @classmethod
    def typed_staging_flow(cls, value: dict[str, object]) -> dict[str, object]:
        raw = value.get("staging_flow")
        if raw is not None:
            StagingFlowPolicy.model_validate(raw)
        merge = value.get("merge_delivery")
        if merge is not None:
            MergeDeliveryPolicy.model_validate(merge)
        return value

    def merge_delivery(self) -> MergeDeliveryPolicy | None:
        """NEW-160: the typed merge-only full delivery, or None when absent."""
        raw = self.delivery_control.get("merge_delivery")
        return MergeDeliveryPolicy.model_validate(raw) if raw is not None else None

    def merge_delivery_blockers(self) -> list[str]:
        """NEW-160: cross-check an enabled merge-only delivery against this contract."""
        merge = self.merge_delivery()
        if merge is None or not merge.enabled:
            return ["MERGE_DELIVERY_DISABLED"]
        blockers: list[str] = []
        if self.has_runtime():
            blockers.append("MERGE_DELIVERY_REQUIRES_NO_RUNTIME")
        if str(self.repositories.source.full_name) != str(self.repositories.deployment.full_name):
            blockers.append("MERGE_DELIVERY_REQUIRES_SELF_CONTAINED_REPOSITORY")
        if merge.source_ci_workflow != self.delivery_control.get("source_ci_workflow"):
            blockers.append("MERGE_DELIVERY_SOURCE_CI_WORKFLOW_MISMATCH")
        return blockers

    def staging_flow(self) -> StagingFlowPolicy | None:
        """Return the typed post-merge staging GitOps flow, or None when absent."""
        raw = self.delivery_control.get("staging_flow")
        return StagingFlowPolicy.model_validate(raw) if raw is not None else None

    def staging_flow_blockers(self) -> list[str]:
        """Cross-check an enabled staging flow against the rest of this contract."""
        flow = self.staging_flow()
        if flow is None or not flow.enabled:
            return ["STAGING_FLOW_DISABLED"]
        blockers: list[str] = []
        deployment = self.deployment
        if not isinstance(deployment, DeploymentManifest):
            return ["STAGING_FLOW_REQUIRES_RUNTIME"]
        if deployment.strategy != "argocd":
            blockers.append("STAGING_FLOW_REQUIRES_ARGOCD_STRATEGY")
        if deployment.argocd_application != flow.argo_application:
            blockers.append("STAGING_FLOW_ARGO_APPLICATION_MISMATCH")
        if flow.deploy_repository != self.repositories.deployment.full_name:
            blockers.append("STAGING_FLOW_DEPLOY_REPOSITORY_MISMATCH")
        if not set(flow.namespaces) <= set(deployment.namespaces):
            blockers.append("STAGING_FLOW_NAMESPACE_OUTSIDE_PROJECT")
        if not set(flow.image_repositories) <= set(deployment.image_repositories.values()):
            blockers.append("STAGING_FLOW_IMAGE_REPOSITORY_OUTSIDE_PROJECT")
        if flow.chart_source_repository is not None and flow.chart_source_repository not in {
            str(self.repositories.source.url),
            str(self.repositories.deployment.url),
        }:
            blockers.append("STAGING_FLOW_CHART_REPOSITORY_OUTSIDE_PROJECT")
        if flow.main_ci_workflow is None and flow.image_workflow is None:
            blockers.append("STAGING_FLOW_IMAGE_BUILD_WORKFLOW_MISSING")
        return blockers

    @field_validator("source_test_commands", "deployment_test_commands")
    @classmethod
    def fixed_commands(cls, commands: list[list[str]]) -> list[list[str]]:
        forbidden = {"sh", "bash", "zsh", "fish", "ssh", "kubectl", "argocd"}
        for command in commands:
            if not command or len(command) > 20 or command[0] in forbidden:
                raise ValueError("test commands must use fixed non-shell executables")
            if any("\n" in argument or "\x00" in argument for argument in command):
                raise ValueError("test command arguments must be single bounded values")
        return commands


class Persona(EngineeringModel):
    id: Slug
    product_roles: list[str] = Field(default_factory=list, max_length=20)
    scope: Literal["public", "platform", "tenant", "workspace", "personal", "operator"]
    credential_ref: CredentialReference | None = None
    identity_source: Literal["anonymous", "ephemeral-superadmin", "ephemeral-falcone-oidc"] = (
        "ephemeral-superadmin"
    )
    allowed_flows: list[Slug] = Field(min_length=1, max_length=50)
    forbidden_flows: list[Slug] = Field(default_factory=list, max_length=50)
    expected_permissions: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def runtime_generated_identity(self) -> Persona:
        if self.id == "anonymous" and self.credential_ref is not None:
            raise ValueError("anonymous persona cannot have a credential reference")
        if self.credential_ref is not None:
            raise ValueError("persistent persona credentials are prohibited")
        if self.id == "anonymous" and self.identity_source != "anonymous":
            raise ValueError("anonymous persona must use anonymous identity source")
        if self.id != "anonymous" and self.identity_source == "anonymous":
            raise ValueError("authenticated persona must be runtime generated")
        return self


class PersonaManifest(EngineeringModel):
    schema_version: Literal[1]
    project: ProjectName
    identity_authority: Literal["falcone", "musematic"]
    persistent_contract: CredentialReference | None = None
    personas: list[Persona] = Field(min_length=3, max_length=30)

    @model_validator(mode="after")
    def minimum_lanes(self) -> PersonaManifest:
        ids = {persona.id for persona in self.personas}
        required = {"anonymous", "standard-user", "privileged-admin"}
        if not required.issubset(ids):
            raise ValueError("persona catalog lacks the minimum E2E lanes")
        if len(ids) != len(self.personas):
            raise ValueError("persona IDs must be unique")
        if self.project in {"falcone", "musematic"}:
            expected = f"file:/etc/engineering-control/credentials/{self.project}/superadmin.env"
            if str(self.persistent_contract) != expected:
                raise ValueError("product persona factory requires exactly its superadmin contract")
        elif self.persistent_contract is not None or self.identity_authority != "falcone":
            raise ValueError("LLMWiki personas use Falcone authority without a credential file")
        return self


class FlowStep(EngineeringModel):
    channel: Literal["browser", "api"]
    action: Literal[
        "navigate",
        "request",
        "fill-credential",
        "fill-generated",
        "click",
        "assert-visible",
        "assert-status",
        "assert-url",
        "capture",
    ]
    target: str = Field(min_length=1, max_length=500)
    method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"] | None = None
    expected_status: int | None = Field(default=None, ge=100, le=599)
    value_ref: str | None = Field(default=None, max_length=120)
    body_template: dict[str, object] | None = None

    @field_validator("target")
    @classmethod
    def no_script_target(cls, value: str) -> str:
        if "\n" in value or value.startswith(("javascript:", "file:", "data:")):
            raise ValueError("flow targets cannot contain script or local-file URLs")
        return value

    @model_validator(mode="after")
    def request_contract(self) -> FlowStep:
        if self.action == "request":
            if self.method is None or self.expected_status is None:
                raise ValueError("API requests require an explicit method and expected status")
        elif self.method is not None or self.body_template is not None:
            raise ValueError("HTTP method/body are only valid for API request steps")
        if self.body_template is not None and self.method not in {"POST", "PATCH", "PUT", "DELETE"}:
            raise ValueError("request bodies are limited to explicit mutating methods")
        return self


class RetryPolicy(EngineeringModel):
    maximum_attempts: int = Field(default=2, ge=1, le=3)
    backoff_seconds: int = Field(default=2, ge=0, le=30)


class E2EFlow(EngineeringModel):
    schema_version: Literal[1]
    id: Slug
    project: ProjectName
    persona: Slug
    category: Literal[
        "public",
        "authentication",
        "standard-user",
        "admin",
        "authorization",
        "integrations",
    ]
    purpose: str = Field(min_length=1, max_length=500)
    preconditions: list[str] = Field(default_factory=list, max_length=30)
    steps: list[FlowStep] = Field(min_length=1, max_length=100)
    expected_result: str = Field(min_length=1, max_length=1000)
    positive_controls: list[str] = Field(min_length=1, max_length=30)
    negative_controls: list[str] = Field(min_length=1, max_length=30)
    resources_created: list[str] = Field(default_factory=list, max_length=30)
    cleanup_instructions: list[str] = Field(default_factory=list, max_length=30)
    safe_target_requirements: list[str] = Field(min_length=1, max_length=30)
    evidence_requirements: list[str] = Field(min_length=1, max_length=30)
    timeout_seconds: int = Field(default=300, ge=10, le=1800)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    mutation: Literal["non-mutating", "disposable"]
    mutation_class: Literal["NONE", "IDENTITY_BOOTSTRAP", "APPLICATION_DATA_MUTATION"] = "NONE"

    @model_validator(mode="after")
    def cleanup_required_for_mutation(self) -> E2EFlow:
        if self.mutation == "disposable":
            if not self.resources_created or not self.cleanup_instructions:
                raise ValueError("disposable flows require resources and cleanup")
            if not any("e2e-<campaign-id>-" in item for item in self.resources_created):
                raise ValueError("disposable resources require the campaign prefix")
        if self.mutation_class == "APPLICATION_DATA_MUTATION" and self.mutation != "disposable":
            raise ValueError("application-data mutation must be disposable")
        return self


class RepositorySnapshot(EngineeringModel):
    url: HttpUrl
    branch: Literal["main", "master"]
    sha: GitSHA


class RuntimeSnapshot(EngineeringModel):
    running_image_digests: dict[str, Digest] = Field(min_length=1, max_length=100)
    helm_release_revision: int | None = Field(default=None, ge=1)
    argocd_revision: GitSHA | None = None
    kubernetes_context_identity: str = Field(min_length=1, max_length=253)
    api_server_identity: str = Field(pattern=r"^https://[^\s]+$")
    namespace: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    runtime_url: HttpUrl
    deployed_revision: GitSHA


class ProjectPairSnapshot(EngineeringModel):
    schema_version: Literal[1]
    project: ProjectName
    source: RepositorySnapshot
    deployment: RepositorySnapshot
    runtime: RuntimeSnapshot
    intended_deployment_sha: GitSHA
    deployment_revision_matches: bool
    capture_time: datetime
    status: Literal["PASS", "LIVE_REVISION_DRIFT"]

    @model_validator(mode="after")
    def fail_closed_drift(self) -> ProjectPairSnapshot:
        matches = self.runtime.deployed_revision == self.intended_deployment_sha
        if self.deployment_revision_matches != matches:
            raise ValueError("deployment match flag does not reflect the immutable SHAs")
        if matches != (self.status == "PASS"):
            raise ValueError("snapshot status must fail closed on deployment drift")
        return self


class CredentialReadiness(EngineeringModel):
    reference: ReadinessReference
    present: bool
    permissions_valid: bool
    worker_attested: bool = False
    blocker: Literal[
        "MISSING",
        "PERMISSIONS",
        "ATTESTATION_MISSING",
        "ATTESTATION_STALE",
        "ATTESTATION_INVALID",
        "NONE",
    ]


class CampaignPlan(EngineeringModel):
    project: ProjectName
    preset: Literal[
        "snapshot",
        "static-full",
        "e2e-smoke",
        "full-e2e",
        "security",
        "ux",
        "deployment",
        "falcone-intensive",
    ]
    environment: Slug
    repository_pair: RepositoryPair
    repository_surfaces: list[Literal["source", "deployment", "pair", "runtime"]]
    personas: list[Slug]
    flows: list[Slug]
    lenses: list[Slug]
    mutating_flows: list[Slug]
    credential_readiness: list[CredentialReadiness]
    kubernetes_readiness: ReadinessStatus
    estimated_model_allocation: dict[Literal["codex", "claude"], int]
    blockers: list[str]


class EnhancementStatus(StrEnum):
    CANDIDATE_ENHANCEMENT = "CANDIDATE_ENHANCEMENT"
    EVIDENCE_VALIDATED = "EVIDENCE_VALIDATED"
    DUPLICATE = "DUPLICATE"
    PRODUCT_REVIEWED = "PRODUCT_REVIEWED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PUBLISHED = "PUBLISHED"
    DELIVERY_ACTIVE = "DELIVERY_ACTIVE"
    DELIVERED = "DELIVERED"


class EnhancementRecord(EngineeringModel):
    enhancement_id: str = Field(pattern=r"^enh-[a-f0-9]{16,64}$")
    project: ProjectName
    campaign_id: str = Field(min_length=1, max_length=200)
    persona: Slug
    observed_problem: str = Field(min_length=1, max_length=2000)
    current_behavior: str = Field(min_length=1, max_length=2000)
    evidence_handles: list[str] = Field(min_length=1, max_length=50)
    proposed_outcome: str = Field(min_length=1, max_length=2000)
    user_value: str = Field(min_length=1, max_length=2000)
    success_metric: str = Field(min_length=1, max_length=1000)
    affected_repository: RepositoryName
    dependencies: list[str] = Field(default_factory=list, max_length=50)
    security_data_implications: str = Field(min_length=1, max_length=2000)
    risk: Literal["R0", "R1", "R2", "R3"]
    acceptance_criteria: list[str] = Field(min_length=1, max_length=50)
    rollout_strategy: str = Field(min_length=1, max_length=2000)
    kill_switch: str | None = Field(default=None, max_length=1000)
    status: EnhancementStatus = EnhancementStatus.CANDIDATE_ENHANCEMENT
    approval_binding: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class EnhancementProposal(EngineeringModel):
    persona: Slug
    observed_problem: str = Field(min_length=1, max_length=2000)
    current_behavior: str = Field(min_length=1, max_length=2000)
    evidence_handles: list[str] = Field(min_length=1, max_length=50)
    proposed_outcome: str = Field(min_length=1, max_length=2000)
    user_value: str = Field(min_length=1, max_length=2000)
    success_metric: str = Field(min_length=1, max_length=1000)
    affected_repository: RepositoryName
    dependencies: list[str] = Field(default_factory=list, max_length=50)
    security_data_implications: str = Field(min_length=1, max_length=2000)
    risk: Literal["R0", "R1", "R2", "R3"]
    acceptance_criteria: list[str] = Field(min_length=1, max_length=50)
    rollout_strategy: str = Field(min_length=1, max_length=2000)
    kill_switch: str | None = Field(default=None, max_length=1000)
