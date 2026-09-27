from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from engineering_control.models.reconciliation import StagingFlowPolicy

Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")]
GitSHA = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
RepositoryName = Annotated[
    str,
    Field(pattern=r"^gntik-ai/(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)$"),
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
        if self.source.full_name == self.deployment.full_name and str(self.source.full_name) != (
            "gntik-ai/llmwiki"
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


class ToolchainPolicy(EngineeringModel):
    enabled: bool = False
    prepare: list[ToolchainPrepareCommand] = Field(default_factory=list, max_length=10)
    test_commands: list[ToolchainTestCommand] = Field(default_factory=list, max_length=20)
    cleanup_paths_ignored_only: bool = True

    @model_validator(mode="after")
    def enabled_requires_prepare(self) -> ToolchainPolicy:
        if self.enabled and not self.prepare:
            raise ValueError("an enabled toolchain requires at least one prepare command")
        if sum(command.timeout_seconds for command in self.prepare) > TOOLCHAIN_TOTAL_TIMEOUT:
            # IssueDeliveryWorkflow gives delivery.prepare_toolchain 90 minutes in total.
            raise ValueError("toolchain prepare timeouts exceed the step's bounded budget")
        return self


class ProjectManifest(EngineeringModel):
    schema_version: Literal[1]
    project: Literal["falcone", "musematic", "llmwiki"]
    repositories: RepositoryPair
    api_url: HttpUrl
    deployment: DeploymentManifest
    delivery_control: dict[str, object]
    source_test_commands: list[list[str]] = Field(min_length=1, max_length=20)
    deployment_test_commands: list[list[str]] = Field(min_length=1, max_length=20)
    dependencies: dict[str, object] = Field(default_factory=dict)
    classification: dict[str, bool] = Field(default_factory=dict)
    toolchain: ToolchainPolicy | None = None

    @field_validator("delivery_control")
    @classmethod
    def typed_staging_flow(cls, value: dict[str, object]) -> dict[str, object]:
        raw = value.get("staging_flow")
        if raw is not None:
            StagingFlowPolicy.model_validate(raw)
        return value

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
    project: Literal["falcone", "musematic", "llmwiki"]
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
    project: Literal["falcone", "musematic", "llmwiki"]
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
    project: Literal["falcone", "musematic", "llmwiki"]
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
    project: Literal["falcone", "musematic", "llmwiki"]
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
    project: Literal["falcone", "musematic", "llmwiki"]
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
